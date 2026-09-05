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
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, SyncTriggerThrottle, user_can_edit_plant
from apps.core.models import DomesticPOCorrection, FlagDismissal, MaterialCorrection
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
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
        "matchedMirNo": match.mir_entry.mir_no if match else None,
        "stockMatched": bool(match and len(match.mir_entry.stock_matches.all()) > 0),
    }


def _po_dict(cfg: _PlantConfig, po):
    items = list(po.items.all())
    corrections = DomesticPOCorrection.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
    flag_dismissals = FlagDismissal.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
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
    }


def _lot_dict(cfg: _PlantConfig, lot):
    corrections = MaterialCorrection.objects.filter(plant=cfg.syncrun_plant, lot_id=lot.id)
    rate = getattr(lot, cfg.lot_rate_field)
    return {
        "lotId": lot.id,
        "materialCode": getattr(lot, cfg.lot_code_field) or str(lot.id),
        "description": lot.description,
        "category": lot.category,
        # Achhad's model has no sub_category/uom columns at all - getattr's
        # default "" reproduces achhad_views.py's own hardcoded "" exactly.
        "subCategory": getattr(lot, "sub_category", ""),
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
        "mirMatched": len(lot.mir_matches.all()) > 0,
        "mirStockMatches": [
            {
                "matchId": m.id,
                "isFlagged": m.is_flagged,
                "dismissedByOverride": m.dismissed_by_override,
                "qtyDiffPct": float(m.qty_diff_pct) if m.qty_diff_pct is not None else None,
                "rateDiffPct": float(m.rate_diff_pct) if m.rate_diff_pct is not None else None,
            }
            for m in lot.mir_matches.all()
        ],
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
        qs = cfg.po_model.objects.prefetch_related(
            "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
        )
        return Response({"purchaseOrders": [_po_dict(cfg, po) for po in qs]})

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


def make_materials(cfg: _PlantConfig):
    @api_view(["GET"])
    def materials(request):
        qs = cfg.stock_lot_model.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
        return Response({"materials": [_lot_dict(cfg, lot) for lot in qs]})

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


def make_sync_status(cfg: _PlantConfig):
    @api_view(["GET"])
    def sync_status(request):
        from apps.core.models import SyncRun

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
        return Response({
            "sync": latest_by_source,
            "mirEntryCount": cfg.mir_model.objects.filter(is_active=True).count(),
            "syncInProgress": is_sync_in_progress(cfg.key),
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
