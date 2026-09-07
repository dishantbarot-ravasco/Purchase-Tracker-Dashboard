"""
Shared implementation behind the three domestic plant routers (hrs_views.py /
vapi_views.py / achhad_views.py). See CLAUDE.md's "Domestic router
de-duplication" for the history: those three files used to duplicate this
logic near-verbatim (confirmed byte-for-byte identical except model classes,
which run_full_match to call, and a handful of real schema differences -
Achhad's Stock lot has rate/msl instead of basic_rate/sub_category/uom and
no vendor column at all). This module holds the one real implementation;
each plant's own router file builds a `_PlantConfig` and re-exports these
functions under the same names urls.py already points at, so urls.py and
every view's external behavior are unchanged by this refactor - only where
the code physically lives changed.

Not shared: which matching module's run_full_match to call (matching.py /
matching_achhad.py / matching_vapi.py are separately tuned scoring modules,
see CLAUDE.md's "PO<->MIR<->Stock matching") and the model classes
themselves - both are supplied per-plant via `_PlantConfig`, not branched on
inline here.
"""

import datetime
import decimal
import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, SyncTriggerThrottle, user_can_access_plant, user_can_edit_plant
from apps.core.models import DataQualityFlag, DomesticPOCorrection, FlagDismissal, MaterialCategoryReference, MaterialCorrection
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
from apps.services.parsers.common import normalize_material
from apps.services.stock_consumption import DEFAULT_WINDOW_DAYS, consumption_stats
from apps.services.sync_trigger import is_sync_in_progress, trigger_plant_sync
from apps.services.validation import is_valid_email, is_valid_gstin

# Identical across all three plants (confirmed byte-for-byte in the prior
# audit) - one copy here rather than three.
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "tax_type",
    "total_inclusive_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {"description", "hsn", "qty", "uom", "delivery_date", "net_price", "net_value"}
_DATE_FIELDS = {"po_created_date", "delivery_date"}
_DECIMAL_FIELDS = {"total_value", "total_inclusive_value", "qty", "net_price", "net_value"}
_REMATCH_TRIGGER_FIELDS = {"vendor_name", "vendor_gstin", "description", "qty", "net_price", "net_value"}


@dataclass(frozen=True)
class _PlantConfig:
    """Everything genuinely different between HRS/Vapi/Achhad's domestic
    routers. `lot_vendor_field`/`lot_code_field`/`lot_rate_field` are model
    attribute names read via getattr, not branched-on plant checks -
    Achhad's model simply has no vendor field at all, so lot_vendor_field is
    None there rather than a fake placeholder."""

    key: str  # "hrs" / "vapi" / "achhad" - passed to user_can_edit_plant/is_sync_in_progress/trigger_plant_sync
    syncrun_plant: str  # SyncRun.Plant.HRS / .RTP_VAPI / .RTP_ACHHAD

    po_model: type
    item_model: type
    mir_model: type
    po_mir_match_model: type
    mir_stock_match_model: type
    stock_lot_model: type
    stock_snapshot_model: type

    run_full_match: Callable[[], object]

    material_editable_fields: frozenset
    material_decimal_fields: frozenset
    material_rematch_trigger_fields: frozenset

    lot_rate_field: str  # "basic_rate" (HRS/Vapi) or "rate" (Achhad)
    lot_code_field: str  # "sap_item_code" (HRS) / "hsn_code" (Vapi) / "sap_code" (Achhad)
    lot_vendor_field: Optional[str] = None  # "party_name" (HRS) / "supplier_name" (Vapi) / None (Achhad)

    # Days-Left Engine extension (2026-09-08, Achhad only): RTPAchhadRMDailyMovement's
    # sparse (stock_lot, movement_date) -> (received, issued) rows, parsed
    # from Achhad's own daily Recp./Issue matrix - see that model's
    # docstring for why this is trustworthy. None for HRS/Vapi, whose Stock
    # files have no day-by-day matrix to parse in the first place.
    daily_movement_model: Optional[type] = None


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize(value):
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _coerce_value(field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in _DATE_FIELDS:
        return datetime.date.fromisoformat(raw_value)
    if field_name in _DECIMAL_FIELDS:
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


def _field_warning(field_name, value):
    if field_name == "vendor_gstin" and not is_valid_gstin(value):
        return f"{value!r} doesn't look like a standard 15-character GSTIN."
    if field_name == "vendor_email" and not is_valid_email(value):
        return f"{value!r} doesn't look like a valid email address."
    return None


def _coerce_material_value(cfg: _PlantConfig, field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in cfg.material_decimal_fields:
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


def _correction_dict(c):
    return {
        "fieldName": c.field_name,
        "itemId": c.item_id,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "reason": c.reason,
        "correctedBy": c.corrected_by_email,
        "correctedAt": c.corrected_at.isoformat(),
    }


def _material_correction_dict(c):
    return {
        "fieldName": c.field_name,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "reason": c.reason,
        "correctedBy": c.corrected_by_email,
        "correctedAt": c.corrected_at.isoformat(),
    }


def _flag_dismissal_dict(fd):
    return {
        "flagKey": fd.flag_key,
        "dismissed": fd.dismissed,
        "dismissedBy": fd.dismissed_by_email,
        "dismissedReason": fd.dismissed_reason,
        "dismissedAt": fd.dismissed_at.isoformat() if fd.dismissed_at else None,
    }


def _data_quality_flag_dict(flag):
    """Match Accuracy Programme fix 3.G: one apps/services/arithmetic_checks.py
    mismatch, surfaced in the Flags & Corrections tab alongside
    FlagDismissal-backed flags (see poFlagHtml()'s own comment in
    frontend/js/flags.js for the rendering side)."""
    return {
        "sourceType": flag.source_type,
        "checkName": flag.check_name,
        "expected": float(flag.expected),
        "actual": float(flag.actual),
        "detectedAt": flag.detected_at.isoformat() if flag.detected_at else None,
    }


def _line_item_dict(item):
    match = getattr(item, "mir_match", None)
    return {
        "description": item.description,
        "qty": float(item.qty) if item.qty is not None else None,
        "uom": item.uom,
        "netPrice": float(item.net_price) if item.net_price is not None else None,
        "deliveryDate": item.delivery_date.isoformat() if item.delivery_date else None,
        "matched": match is not None,
        "matchId": match.id if match else None,
        "matchTier": match.tier if match else None,
        "matchScore": float(match.match_score) if match else None,
        "matchFlagged": match.is_flagged if match else False,
        "dismissedByOverride": match.dismissed_by_override if match else False,
        "dismissedReason": match.dismissed_reason if match else "",
        "dismissedBy": match.dismissed_by.email if match and match.dismissed_by else None,
        "dismissedAt": match.dismissed_at.isoformat() if match and match.dismissed_at else None,
        "qtyDiffPct": float(match.qty_diff_pct) if match and match.qty_diff_pct is not None else None,
        "rateDiffPct": float(match.rate_diff_pct) if match and match.rate_diff_pct is not None else None,
        "valueDiffPct": float(match.value_diff_pct) if match and match.value_diff_pct is not None else None,
        # Match Accuracy Programme fixes 2.C/3.F - see matching_core.py.
        # uomMismatch/severity are the real, stored backend decision; the
        # frontend must not re-derive a value flag from valueDiffPct alone
        # anymore (that would ignore the value epsilon) - see flags.js's
        # matchStatusHtml() for how these are actually used.
        "uomMismatch": bool(match and match.uom_mismatch),
        "severity": match.severity if match else None,
        "matchedMirNo": match.mir_entry.mir_no if match else None,
        "stockMatched": bool(match and len(match.mir_entry.stock_matches.all()) > 0),
        # Identification/Financial-Check redesign (2026-09-07, see
        # matching_core.py's module docstring): matchFlagged (is_flagged)
        # above now means specifically "qty or rate mismatched" - these
        # fields expose that split plus everything routed into the new
        # dataMismatch bucket (UOM/Net/Taxable-Value/GST-type/Final-Value)
        # instead of blending it back into one boolean.
        "materialMatched": bool(match and match.material_matched),
        "poNumberMatched": bool(match and match.po_number_matched),
        "qtyMismatched": bool(match and match.qty_mismatched),
        "rateMismatched": bool(match and match.rate_mismatched),
        "dataMismatch": bool(match and match.data_mismatch),
        "taxTypeMismatch": bool(match and match.tax_type_mismatch),
        "taxableValueDiffPct": (
            float(match.taxable_value_diff_pct) if match and match.taxable_value_diff_pct is not None else None
        ),
        "finalValueDiffPct": (
            float(match.final_value_diff_pct) if match and match.final_value_diff_pct is not None else None
        ),
    }


def _group_by(objects, attr):
    """One query's results, grouped into a dict of lists keyed by `attr` -
    the batching primitive _po_dict/_lot_dict use to avoid a per-row query,
    same reasoning as _consumption_by_lot()'s own docstring."""
    grouped: dict = {}
    for obj in objects:
        grouped.setdefault(getattr(obj, attr), []).append(obj)
    return grouped


def _po_dict(cfg: _PlantConfig, po, corrections_by_po=None, flag_dismissals_by_po=None,
             item_flags_by_item=None, mir_flags_by_mir_entry=None):
    items = list(po.items.all())
    # N+1 fix (see CLAUDE.md): make_purchase_orders() batches these four
    # queries once for the whole queryset and passes per-PO/per-item maps in,
    # rather than this function querying per PO. The `is None` fallback (one
    # query per call) only exists for a caller outside that batched path -
    # there isn't one today, but it keeps this function safe to call
    # standalone (e.g. from a future detail endpoint or a test) without
    # silently returning empty corrections/flags.
    if corrections_by_po is not None:
        corrections = corrections_by_po.get(po.po_number, [])
    else:
        corrections = DomesticPOCorrection.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
    if flag_dismissals_by_po is not None:
        flag_dismissals = flag_dismissals_by_po.get(po.po_number, [])
    else:
        flag_dismissals = FlagDismissal.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
    # Match Accuracy Programme fix 3.G: this PO's own line items' arithmetic
    # flags, plus any flag on the MIR entry a line item matched to (a real
    # MIR data-quality issue is still relevant here even though MIR has no
    # detail view of its own to attach it to directly).
    if item_flags_by_item is not None and mir_flags_by_mir_entry is not None:
        data_quality_flags = [f for i in items for f in item_flags_by_item.get(i.id, [])] + [
            f for i in items if getattr(i, "mir_match", None)
            for f in mir_flags_by_mir_entry.get(i.mir_match.mir_entry_id, [])
        ]
    else:
        item_ids = [i.id for i in items]
        mir_entry_ids = [i.mir_match.mir_entry_id for i in items if getattr(i, "mir_match", None)]
        data_quality_flags = list(DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.PO_LINE_ITEM, source_id__in=item_ids,
        )) + list(DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.MIR_ENTRY, source_id__in=mir_entry_ids,
        ))
    return {
        "poNumber": po.po_number,
        "vendorName": po.vendor_name,
        "vendorGstin": po.vendor_gstin,
        "vendorAddress": po.vendor_address,
        "vendorEmail": po.vendor_email,
        "vendorCode": po.vendor_code,
        "billingAddress": po.billing_address,
        "shipTo": po.ship_to,
        "currency": po.currency,
        "taxType": po.tax_type,
        "createdDate": po.po_created_date.isoformat() if po.po_created_date else None,
        "totalValue": float(po.total_value) if po.total_value is not None else None,
        "totalInclTax": float(po.total_inclusive_value) if po.total_inclusive_value is not None else None,
        "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms,
        "remarks": po.remarks,
        "isOldFormat": po.is_old_format_template,
        "items": [_line_item_dict(i) for i in items],
        "corrections": [_correction_dict(c) for c in corrections],
        "flagDismissals": [_flag_dismissal_dict(fd) for fd in flag_dismissals],
        "dataQualityFlags": [_data_quality_flag_dict(f) for f in data_quality_flags],
    }


def _category_reference_map() -> dict[str, MaterialCategoryReference]:
    """One query for the whole (small, shared-across-plants) canonical
    Category/Subcategory table - see MaterialCategoryReference's own
    docstring (apps/core/models.py) for the full design. Called once per
    request (make_materials()), not once per lot."""
    return {ref.normalized_description: ref for ref in MaterialCategoryReference.objects.all()}


def _lot_dict(cfg: _PlantConfig, lot, consumption_by_lot=None, category_reference=None,
              corrections_by_lot=None, flags_by_lot=None):
    # N+1 fix (see CLAUDE.md): make_materials() batches these two queries
    # once for the whole queryset and passes per-lot maps in, same reasoning
    # as _po_dict's own corrections_by_po/flag_dismissals_by_po above. The
    # `is None` fallback keeps this function safe to call standalone.
    if corrections_by_lot is not None:
        corrections = corrections_by_lot.get(lot.id, [])
    else:
        corrections = MaterialCorrection.objects.filter(plant=cfg.syncrun_plant, lot_id=lot.id)
    # Match Accuracy Programme fix 3.G: this lot's own stock-balance
    # arithmetic flag, if any (opening + received - issued vs todays_stock).
    if flags_by_lot is not None:
        data_quality_flags = flags_by_lot.get(lot.id, [])
    else:
        data_quality_flags = DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.STOCK_LOT, source_id=lot.id,
        )
    rate = getattr(lot, cfg.lot_rate_field)
    consumption = (consumption_by_lot or {}).get(lot.id)
    # Achhad's Stock sheet has a Minimum Stock Level column HRS/Vapi lack
    # entirely (RTPAchhadRMLot.msl) - getattr's default None means this
    # is always None on HRS/Vapi rows without any per-plant branching, same
    # pattern as no_of_days/sub_category above. daysToMsl is 0 the moment
    # current stock is at/below msl (independent of whether a consumption
    # rate is even known yet - "already need to reorder" shouldn't wait on
    # 30 days of snapshot history to surface), else an ETA once avgDaily is
    # available, else None (below msl not yet reached, no rate to project).
    msl = getattr(lot, "msl", None)
    days_to_msl = None
    if msl is not None:
        remaining = float(lot.todays_stock) - float(msl)
        if remaining <= 0:
            days_to_msl = 0.0
        elif consumption and consumption.get("avgDaily"):
            days_to_msl = remaining / consumption["avgDaily"]
    # Canonical Category/Subcategory lookup (2026-09-08) - see
    # MaterialCategoryReference's own docstring for why this REPLACES the
    # lot's own raw category/sub_category for display rather than merely
    # filling gaps: HRS's/Vapi's own Stock files carry a free-text per-row
    # Category column real data fills inconsistently (confirmed - grouping
    # by it produced close to one bucket per material), so a populated-but-
    # noisy raw value is exactly as unusable for grouping as a blank one.
    # The raw fields are untouched in the DB (still there for audit/the
    # existing "Edit Everywhere" correction feature) - only this API
    # response's display value changes.
    ref = (category_reference or {}).get(normalize_material(lot.description))
    canonical_category = ref.category if ref else "Uncategorized"
    canonical_subcategory = ref.subcategory if ref else ""
    return {
        "lotId": lot.id,
        "materialCode": getattr(lot, cfg.lot_code_field) or str(lot.id),
        "description": lot.description,
        "category": canonical_category,
        "subCategory": canonical_subcategory,
        "corrections": [_material_correction_dict(c) for c in corrections],
        "uom": getattr(lot, "uom", ""),
        "qty": float(lot.todays_stock),
        "rate": float(rate) if rate is not None else None,
        "value": float(lot.value) if lot.value is not None else None,
        "vendor": getattr(lot, cfg.lot_vendor_field, None) if cfg.lot_vendor_field else None,
        "receivedDate": lot.received_date.isoformat() if lot.received_date else None,
        # HRS's model has a real no_of_days field; Vapi/Achhad's models have
        # none - getattr's default None reproduces both hrs_views.py's real
        # value and vapi_views.py's/achhad_views.py's hardcoded None.
        "noOfDays": getattr(lot, "no_of_days", None),
        # See apps/services/stock_consumption.py's module docstring for why
        # this is drawdown-of-todays_stock based rather than issued/received
        # based - None when there isn't yet a second snapshot to draw down
        # from (a brand-new lot, or one whose history reset - see CLAUDE.md's
        # source_row_ref/is_active notes on lot identity not being stable
        # across a source-sheet row shift).
        "consumption": consumption,
        "daysToMsl": days_to_msl,
        "mirMatched": len(lot.mir_matches.all()) > 0,
        "mirStockMatches": [
            {
                "matchId": m.id,
                "isFlagged": m.is_flagged,
                "dismissedByOverride": m.dismissed_by_override,
                "qtyDiffPct": float(m.qty_diff_pct) if m.qty_diff_pct is not None else None,
                "rateDiffPct": float(m.rate_diff_pct) if m.rate_diff_pct is not None else None,
                # MIR<->Stock identification/financial-check extension
                # (2026-09-08, HRS only for now - see matching_core.py's
                # match_mir_entry_stock() docstring). getattr with a None
                # default so Achhad/Vapi's rows (which don't set these,
                # config.stock_extended_fields=False for them) still
                # serialize cleanly instead of raising.
                "valueDiffPct": float(m.value_diff_pct) if getattr(m, "value_diff_pct", None) is not None else None,
                "materialMatched": getattr(m, "material_matched", None),
                "dateMatched": getattr(m, "date_matched", None),
                "qtyMismatched": getattr(m, "qty_mismatched", None),
                "rateMismatched": getattr(m, "rate_mismatched", None),
                "dataMismatch": getattr(m, "data_mismatch", None),
            }
            for m in lot.mir_matches.all()
        ],
        "dataQualityFlags": [_data_quality_flag_dict(f) for f in data_quality_flags],
    }


# ── View factories ─────────────────────────────────────────────────────────
# Each returns a plain function shaped exactly like the one hrs_views.py's
# module docstring describes - the plant router calls these once at import
# time and re-exports the result under the original name (e.g.
# `correct_field = make_correct_field(HRS_CONFIG)`), so urls.py needs no
# changes.

def make_purchase_orders(cfg: _PlantConfig):
    @api_view(["GET"])
    def purchase_orders(request):
        # See apps/api/permissions.py's user_can_access_plant() docstring -
        # low blast-radius: only affects accounts an admin already scoped
        # to specific plants (empty plants list = "all plants", unchanged).
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's purchase orders."}, status=403)
        qs = cfg.po_model.objects.prefetch_related(
            "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
        )
        pos = list(qs)

        # Batch every PO's corrections/flag-dismissals/data-quality-flags in
        # 4 queries total instead of ~4 per PO (N+1 fix - see CLAUDE.md's
        # "Domestic router de-duplication" section).
        corrections_by_po = _group_by(
            DomesticPOCorrection.objects.filter(plant=cfg.syncrun_plant), "po_number",
        )
        flag_dismissals_by_po = _group_by(
            FlagDismissal.objects.filter(plant=cfg.syncrun_plant), "po_number",
        )
        all_items = [i for po in pos for i in po.items.all()]
        item_ids = [i.id for i in all_items]
        mir_entry_ids = [i.mir_match.mir_entry_id for i in all_items if getattr(i, "mir_match", None)]
        item_flags_by_item = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.PO_LINE_ITEM, source_id__in=item_ids,
            ),
            "source_id",
        )
        mir_flags_by_mir_entry = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.MIR_ENTRY, source_id__in=mir_entry_ids,
            ),
            "source_id",
        )

        return Response({
            "purchaseOrders": [
                _po_dict(cfg, po, corrections_by_po, flag_dismissals_by_po, item_flags_by_item, mir_flags_by_mir_entry)
                for po in pos
            ]
        })

    return purchase_orders


def make_correct_field(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def correct_field(request, po_number):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

        item_id = (request.data.get("itemId") or "").strip()
        field_name = (request.data.get("field") or "").strip()
        raw_value = request.data.get("value")
        reason = (request.data.get("reason") or "").strip()

        po = cfg.po_model.objects.filter(po_number=po_number).first()
        if not po:
            return Response({"error": "Purchase order not found."}, status=404)

        if item_id:
            if field_name not in _ITEM_EDITABLE_FIELDS:
                return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
            target = cfg.item_model.objects.filter(purchase_order=po, item_id=item_id).first()
            if not target:
                return Response({"error": "Line item not found."}, status=404)
        else:
            if field_name not in _PO_EDITABLE_FIELDS:
                return Response({"error": f"{field_name!r} is not an editable PO field."}, status=400)
            target = po

        old_value = getattr(target, field_name)
        try:
            new_value = _coerce_value(field_name, raw_value)
        except (TypeError, ValueError):
            return Response({"error": f"Invalid value for {field_name!r}: {raw_value!r}"}, status=400)

        with transaction.atomic():
            setattr(target, field_name, new_value)
            target.save(update_fields=[field_name])
            DomesticPOCorrection.objects.create(
                plant=cfg.syncrun_plant,
                po_number=po_number,
                item_id=item_id,
                field_name=field_name,
                old_value="" if old_value is None else str(old_value),
                new_value="" if new_value is None else str(new_value),
                reason=reason,
                corrected_by=request.user if getattr(request.user, "pk", None) else None,
                corrected_by_email=getattr(request.user, "email", ""),
            )

        if field_name in _REMATCH_TRIGGER_FIELDS:
            cfg.run_full_match()

        response = {"status": "ok", "field": field_name, "value": _serialize(new_value)}
        warning = _field_warning(field_name, new_value)
        if warning:
            response["warning"] = warning
        return Response(response)

    return correct_field


def _daily_movement_points(cfg: _PlantConfig, window_start) -> dict[int, list[tuple]]:
    """Reconstructs a dense (date, todays_stock, received_cum, issued_cum)
    series per lot from cfg.daily_movement_model's sparse activity-day rows,
    anchored on that lot's own `opening_stock` - Days-Left Engine extension,
    2026-09-08 (see RTPAchhadRMDailyMovement's own docstring for why this is
    trustworthy, and stock_consumption.py's module docstring for why
    RTP-Achhad's own `issued` column couldn't be used before this: that
    concern was about the *monthly* summary resetting each period, not about
    this daily-dated data underneath it). `received`/`issued` here are
    running CUMULATIVE totals within this reconstructed window, matching
    what consumption_stats()'s _issued_cross_check() expects (the same shape
    a real snapshot's `received`/`issued` columns already have) - not the
    per-day deltas the source rows themselves store.

    Returns {} immediately when cfg.daily_movement_model is None (HRS/Vapi -
    their Stock files have no day-by-day matrix to have parsed in the first
    place). One query for every active lot's movements, not one per lot -
    same reasoning as _consumption_by_lot()'s own snapshot query."""
    if cfg.daily_movement_model is None:
        return {}
    rows = list(
        cfg.daily_movement_model.objects
        .filter(stock_lot__is_active=True, movement_date__gte=window_start)
        .values_list("stock_lot_id", "movement_date", "received", "issued")
        .order_by("stock_lot_id", "movement_date")
    )
    if not rows:
        return {}
    lot_ids = {r[0] for r in rows}
    openings = dict(cfg.stock_lot_model.objects.filter(id__in=lot_ids).values_list("id", "opening_stock"))

    points_by_lot: dict[int, list[tuple]] = {}
    for lot_id, group in itertools.groupby(rows, key=lambda row: row[0]):
        running_stock = openings.get(lot_id) or Decimal(0)
        running_received = Decimal(0)
        running_issued = Decimal(0)
        points = []
        for _, date, received, issued in group:
            running_stock = running_stock + received - issued
            running_received += received
            running_issued += issued
            points.append((date, running_stock, running_received, running_issued))
        points_by_lot[lot_id] = points
    return points_by_lot


def _consumption_by_lot(cfg: _PlantConfig):
    """One query for every active lot's snapshot history inside the
    consumption engine's window, grouped by lot id and run through
    consumption_stats() - not one query per lot, which would be an N+1
    across however many hundred lots a plant has. See
    apps/services/stock_consumption.py for the algorithm itself.

    Days-Left Engine extension (2026-09-08): for plants with a
    daily_movement_model (Achhad only today), the reconstructed daily-matrix
    points from _daily_movement_points() are merged in per lot before
    scoring - consumption_stats() sorts by date internally, so simple
    concatenation is enough; an overlapping date between a real snapshot and
    a reconstructed point just costs one wasted interval (gap=0, skipped),
    not a correctness problem."""
    window_start = datetime.date.today() - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    rows = (
        cfg.stock_snapshot_model.objects
        .filter(stock_lot__is_active=True, snapshot_date__gte=window_start)
        .values_list("stock_lot_id", "snapshot_date", "todays_stock", "received", "issued")
        .order_by("stock_lot_id", "snapshot_date")
    )
    points_by_lot: dict[int, list[tuple]] = {
        lot_id: [(date, stock, received, issued) for _, date, stock, received, issued in group]
        for lot_id, group in itertools.groupby(rows, key=lambda row: row[0])
    }

    for lot_id, extra_points in _daily_movement_points(cfg, window_start).items():
        points_by_lot.setdefault(lot_id, []).extend(extra_points)

    return {lot_id: consumption_stats(points) for lot_id, points in points_by_lot.items()}


def make_materials(cfg: _PlantConfig):
    @api_view(["GET"])
    def materials(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's materials."}, status=403)
        qs = cfg.stock_lot_model.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
        lots = list(qs)
        consumption_by_lot = _consumption_by_lot(cfg)
        category_reference = _category_reference_map()

        # Batch every lot's corrections/data-quality-flags in 2 queries total
        # instead of ~2 per lot (N+1 fix - see CLAUDE.md's "Domestic router
        # de-duplication" section).
        lot_ids = [lot.id for lot in lots]
        corrections_by_lot = _group_by(
            MaterialCorrection.objects.filter(plant=cfg.syncrun_plant, lot_id__in=lot_ids), "lot_id",
        )
        flags_by_lot = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.STOCK_LOT, source_id__in=lot_ids,
            ),
            "source_id",
        )

        return Response({
            "materials": [
                _lot_dict(cfg, lot, consumption_by_lot, category_reference, corrections_by_lot, flags_by_lot)
                for lot in lots
            ]
        })

    return materials


def make_correct_material_field(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def correct_material_field(request, lot_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's materials."}, status=403)

        field_name = (request.data.get("field") or "").strip()
        raw_value = request.data.get("value")
        reason = (request.data.get("reason") or "").strip()

        if field_name not in cfg.material_editable_fields:
            return Response({"error": f"{field_name!r} is not an editable material field."}, status=400)

        lot = cfg.stock_lot_model.objects.filter(id=lot_id, is_active=True).first()
        if not lot:
            return Response({"error": "Material lot not found."}, status=404)

        old_value = getattr(lot, field_name)
        try:
            new_value = _coerce_material_value(cfg, field_name, raw_value)
        except (TypeError, ValueError):
            return Response({"error": f"Invalid value for {field_name!r}: {raw_value!r}"}, status=400)

        with transaction.atomic():
            setattr(lot, field_name, new_value)
            lot.save(update_fields=[field_name])
            MaterialCorrection.objects.create(
                plant=cfg.syncrun_plant,
                lot_id=lot_id,
                field_name=field_name,
                old_value="" if old_value is None else str(old_value),
                new_value="" if new_value is None else str(new_value),
                reason=reason,
                corrected_by=request.user if getattr(request.user, "pk", None) else None,
                corrected_by_email=getattr(request.user, "email", ""),
            )

        if field_name in cfg.material_rematch_trigger_fields:
            cfg.run_full_match()

        return Response({"status": "ok", "field": field_name, "value": _serialize(new_value)})

    return correct_material_field


def make_stock_trend(cfg: _PlantConfig):
    @api_view(["GET"])
    def stock_trend(request, lot_id: int):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock trend."}, status=403)
        snapshots = cfg.stock_snapshot_model.objects.filter(stock_lot_id=lot_id).order_by("snapshot_date")
        return Response({
            "snapshots": [
                {
                    "date": s.snapshot_date.isoformat(),
                    "qty": float(s.todays_stock),
                    "rate": float(getattr(s, cfg.lot_rate_field)) if getattr(s, cfg.lot_rate_field) is not None else None,
                    "value": float(s.value) if s.value is not None else None,
                }
                for s in snapshots
            ]
        })

    return stock_trend


def make_stock_snapshot_dates(cfg: _PlantConfig):
    """Snapshot Pipeline Rebuild, Phase C.1 (see CLAUDE.md) - distinct
    snapshot dates for this plant, each with a lot count. Drives a date
    picker; the count doubles as a health signal - a date with far fewer
    lots than its neighbours means a partial sync."""

    @api_view(["GET"])
    def stock_snapshot_dates(request):
        from django.db.models import Count

        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock snapshots."}, status=403)
        rows = (
            cfg.stock_snapshot_model.objects.values("snapshot_date")
            .annotate(lotCount=Count("id"))
            .order_by("-snapshot_date")
        )
        return Response({"dates": [{"date": r["snapshot_date"].isoformat(), "lotCount": r["lotCount"]} for r in rows]})

    return stock_snapshot_dates


def make_stock_snapshots_for_date(cfg: _PlantConfig):
    """Snapshot Pipeline Rebuild, Phase C.2 (see CLAUDE.md) - a whole
    plant's stock position on one date, joined to stock_lot for
    description/category/vendor. Defaults to the latest available date
    when `date` is omitted. Returns 404 with the nearest available dates
    when the requested (or defaulted) date has no snapshot rows at all -
    never an empty 200, which a client would render as "zero stock
    everywhere" and a reader would believe."""

    @api_view(["GET"])
    def stock_snapshots_for_date(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock snapshots."}, status=403)

        date_param = request.query_params.get("date")
        if date_param:
            try:
                target_date = datetime.date.fromisoformat(date_param)
            except ValueError:
                return Response({"error": f"{date_param!r} is not a valid YYYY-MM-DD date."}, status=400)
        else:
            target_date = (
                cfg.stock_snapshot_model.objects.order_by("-snapshot_date")
                .values_list("snapshot_date", flat=True)
                .first()
            )
            if target_date is None:
                return Response({"error": "No snapshot history exists yet for this plant.", "availableDates": []}, status=404)

        snapshots = (
            cfg.stock_snapshot_model.objects.filter(snapshot_date=target_date)
            .select_related("stock_lot")
            .order_by("stock_lot__description")
        )
        if not snapshots.exists():
            nearest = (
                cfg.stock_snapshot_model.objects.values_list("snapshot_date", flat=True)
                .distinct()
                .order_by("-snapshot_date")[:5]
            )
            return Response({
                "error": f"No snapshot exists for {target_date.isoformat()}.",
                "availableDates": [d.isoformat() for d in nearest],
            }, status=404)

        return Response({
            "date": target_date.isoformat(),
            "lots": [
                {
                    "lotId": s.stock_lot_id,
                    "materialCode": getattr(s.stock_lot, cfg.lot_code_field) or str(s.stock_lot_id),
                    "description": s.stock_lot.description,
                    "category": getattr(s.stock_lot, "category", ""),
                    "vendor": getattr(s.stock_lot, cfg.lot_vendor_field, None) if cfg.lot_vendor_field else None,
                    "qty": float(s.todays_stock),
                    "rate": float(getattr(s, cfg.lot_rate_field)) if getattr(s, cfg.lot_rate_field) is not None else None,
                    "value": float(s.value) if s.value is not None else None,
                }
                for s in snapshots
            ],
        })

    return stock_snapshots_for_date


def make_sync_status(cfg: _PlantConfig):
    @api_view(["GET"])
    def sync_status(request):
        from django.utils import timezone

        from apps.core.models import SyncRun

        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's sync status."}, status=403)

        latest_by_source = {}
        for run in SyncRun.objects.filter(plant=cfg.syncrun_plant).order_by("source", "-started_at"):
            if run.source not in latest_by_source:
                latest_by_source[run.source] = {
                    "status": run.status,
                    "startedAt": run.started_at.isoformat(),
                    "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
                    "rowsSeen": run.rows_seen,
                    "rowsChanged": run.rows_changed,
                    "errorDetail": run.error_detail or None,
                }

        # Snapshot Pipeline Rebuild, Phase B.4 (see CLAUDE.md) - makes a
        # silently-dead qcluster visible instead of indistinguishable from a
        # healthy one: without this, the only symptom of a missed daily
        # snapshot is the Days-Left Engine going quietly wrong weeks later.
        last_snapshot_date = (
            cfg.stock_snapshot_model.objects.order_by("-snapshot_date")
            .values_list("snapshot_date", flat=True)
            .first()
        )
        snapshot_gap_days = (timezone.localdate() - last_snapshot_date).days if last_snapshot_date else None

        return Response({
            "sync": latest_by_source,
            "mirEntryCount": cfg.mir_model.objects.filter(is_active=True).count(),
            "syncInProgress": is_sync_in_progress(cfg.key),
            "lastSnapshotDate": last_snapshot_date.isoformat() if last_snapshot_date else None,
            "snapshotGapDays": snapshot_gap_days,
        })

    return sync_status


def make_sync_trigger(cfg: _PlantConfig):
    @api_view(["POST"])
    @permission_classes([IsAdmin])
    @throttle_classes([SyncTriggerThrottle])
    def sync_trigger(request):
        started = trigger_plant_sync(cfg.key)
        if not started:
            return Response({"status": "already_running"}, status=409)
        return Response({"status": "started"}, status=202)

    return sync_trigger


def make_dismiss_po_mir_match(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_po_mir_match(request, match_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        match = dismiss_match(cfg.po_mir_match_model, match_id, request.user, dismissed, reason)
        if not match:
            return Response({"error": "Match not found."}, status=404)
        return Response({
            "status": "ok",
            "matchId": match.id,
            "dismissedByOverride": match.dismissed_by_override,
            "dismissedReason": match.dismissed_reason,
        })

    return dismiss_po_mir_match


def make_dismiss_mir_stock_match(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_mir_stock_match(request, match_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        match = dismiss_match(cfg.mir_stock_match_model, match_id, request.user, dismissed, reason)
        if not match:
            return Response({"error": "Match not found."}, status=404)
        return Response({
            "status": "ok",
            "matchId": match.id,
            "dismissedByOverride": match.dismissed_by_override,
            "dismissedReason": match.dismissed_reason,
        })

    return dismiss_mir_stock_match


def make_dismiss_flag(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_flag(request, po_number):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
        flag_key = (request.data.get("flagKey") or "").strip()
        if not flag_key:
            return Response({"error": "flagKey is required."}, status=400)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        fd = dismiss_po_flag(cfg.syncrun_plant, po_number, flag_key, request.user, dismissed, reason)
        return Response(_flag_dismissal_dict(fd))

    return dismiss_flag
