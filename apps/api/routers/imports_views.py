"""
API surface for the Import Purchase Dashboard - unlike the domestic
hrs_views.py/achhad_views.py/vapi_views.py trio (one plant per URL prefix),
this dashboard is cross-plant by design (spec: "Combine into a single
dataset with an added Plant field", global Plant filter) so it gets one
shared views module parametrized by a `plant` URL segment instead of three
near-duplicate router files. This is a deliberate deviation from the
per-plant-router convention documented in CLAUDE.md - justified there
because MIR/Stock genuinely differ in shape per plant; here all three
plants' Import CSVs are byte-for-byte identical (confirmed live
2026-09-04), so there is no real per-plant divergence to protect against by
splitting the read/derive logic into three copies.

Every read view requires authentication (any role). The PATCH correction
endpoint requires IsEditor (admin/editor) - a viewer can look but not touch,
same role split IsEditor's own docstring describes for a "real mutating
endpoint" beyond what this app has had until now (see apps/core/audit_log.py's
docstring on this app previously being read-only; this endpoint is the first
exception, deliberately, per the build spec's inline-correction requirement).
"""

import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, SyncTriggerThrottle, user_can_access_plant, user_can_edit_plant
from apps.core.models import (
    FlagDismissal,
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSImportPurchaseOrder,
    ImportPOCorrection,
    RTPAchhadImportPOLineItem,
    RTPAchhadImportPOMirMatch,
    RTPAchhadImportPurchaseOrder,
    RTPVapiImportPOLineItem,
    RTPVapiImportPOMirMatch,
    RTPVapiImportPurchaseOrder,
    SyncRun,
)
from apps.services import import_flags as flags
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
from apps.services.matching import run_full_match as _hrs_run_full_match
from apps.services.matching_achhad import run_full_match as _achhad_run_full_match
from apps.services.matching_vapi import run_full_match as _vapi_run_full_match
from apps.services.sync_trigger import is_imports_sync_in_progress, trigger_plant_imports_sync

# plant URL segment -> that plant's run_full_match() - re-run synchronously
# by correct_field() below when an import line item edit could change its
# MIR match outcome, same reasoning as hrs_views.py's/achhad_views.py's/
# vapi_views.py's own _REMATCH_TRIGGER_FIELDS. run_full_match() covers both
# domestic and import PO line items in one idempotent pass (see
# matching.py's own docstring), so this doesn't disturb domestic matches.
_RUN_FULL_MATCH = {"hrs": _hrs_run_full_match, "achhad": _achhad_run_full_match, "vapi": _vapi_run_full_match}

# Editing any of these can change an import line item's PO<->MIR match
# outcome (vendor/material text used for candidate gating, or a value
# compared against MIR) - mirrors _REMATCH_TRIGGER_FIELDS in hrs_views.py/
# achhad_views.py/vapi_views.py, with qty_as_per_boe in place of domestic's
# single `qty` field (see HRSImportPOMirMatch's docstring for why BOE qty,
# not PO qty, is the one compared against MIR).
_REMATCH_TRIGGER_FIELDS = {"vendor_name", "vendor_gstin", "description", "qty_as_per_boe", "net_price", "net_value"}

# plant URL segment -> (PO model, line item model, SyncRun.Plant, display label, import PO<->MIR match model)
_PLANTS = {
    "hrs": (HRSImportPurchaseOrder, HRSImportPOLineItem, SyncRun.Plant.HRS, "HRS-Silvassa", HRSImportPOMirMatch),
    "achhad": (RTPAchhadImportPurchaseOrder, RTPAchhadImportPOLineItem, SyncRun.Plant.RTP_ACHHAD, "RTP-Achhad", RTPAchhadImportPOMirMatch),
    "vapi": (RTPVapiImportPurchaseOrder, RTPVapiImportPOLineItem, SyncRun.Plant.RTP_VAPI, "RTP-Vapi", RTPVapiImportPOMirMatch),
}

# Every field a pencil icon in the mockup can edit. PO-level fields are
# stored on the PO model; item-level fields need an itemId in the PATCH body.
# Identity fields (po_number, po_drive_folder_name, item_id) are deliberately
# absent - those come from the sync, not a manual correction.
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {
    "description", "hsn", "qty_as_per_po", "qty_as_per_boe", "uom", "delivery_date", "net_price", "net_value",
    "tax_type", "currency_after_taxes", "exchange_rate", "total_inclusive_value", "boe_number",
    "bill_of_lading_number", "laden_on_board_date", "country_of_origin", "license_type", "license_number",
}
_DATE_FIELDS = {"po_created_date", "delivery_date", "laden_on_board_date"}
_DECIMAL_FIELDS = {
    "total_value", "qty_as_per_po", "qty_as_per_boe", "net_price", "net_value", "exchange_rate",
    "total_inclusive_value",
}


# ── Serialization helpers ─────────────────────────────────────────────────────

def _f(v):
    return float(v) if v is not None else None


def _d(v):
    return v.isoformat() if v else None


def _mir_match_dict(item):
    """MIR-match info for one import line item, mirroring the domestic
    dashboards' matchStatusHtml() payload shape (matchScore/tier/diff
    percentages) - see HRSImportPOMirMatch's docstring for why this is
    matched against the same MIR table domestic PO line items use.
    `stockMatched` is true only when the specific MIR entry this item
    matched to itself has a real MIR<->Stock match, same "real per-line-item
    chain" semantics as domestic's own stockMatched field, not just "this
    material exists somewhere in stock." Returns None (no `mir_match`
    payload at all) when the item never crossed MATCH_THRESHOLD - same as
    a domestic line item with no match."""
    match = getattr(item, "mir_match", None)
    if match is None:
        return None
    return {
        "matchId": match.id,
        "tier": match.tier,
        "matchScore": float(match.match_score),
        "qtyDiffPct": _f(match.qty_diff_pct),
        "rateDiffPct": _f(match.rate_diff_pct),
        "valueDiffPct": _f(match.value_diff_pct),
        "isFlagged": match.is_flagged,
        # Match Accuracy Programme fixes 2.C/3.F - see matching_core.py and
        # _domestic_base.py's _line_item_dict() (same two fields, same
        # reasoning - the frontend must not re-derive a value flag from
        # valueDiffPct alone, that would ignore the value epsilon).
        "uomMismatch": match.uom_mismatch,
        "severity": match.severity,
        "dismissedByOverride": match.dismissed_by_override,
        "dismissedReason": match.dismissed_reason,
        # Relies on purchase_orders()'s prefetch_related including
        # "items__mir_match__mir_entry__stock_matches" so this reads the
        # prefetch cache, not a new query per line item - same pattern as
        # hrs_views.py's/achhad_views.py's/vapi_views.py's own _line_item_dict().
        "stockMatched": len(match.mir_entry.stock_matches.all()) > 0,
    }


def _item_dict(item):
    is_disc, disc_pct = flags.qty_discrepancy(item)
    return {
        "itemId": item.item_id,
        "description": item.description,
        "hsn": item.hsn,
        "qtyAsPerPo": _f(item.qty_as_per_po),
        "qtyAsPerBoe": _f(item.qty_as_per_boe),
        "uom": item.uom,
        "deliveryDate": _d(item.delivery_date),
        "deliveryDateRaw": item.delivery_date_raw,
        "netPrice": _f(item.net_price),
        "netValue": _f(item.net_value),
        "taxType": item.tax_type,
        "currencyAfterTaxes": item.currency_after_taxes,
        "exchangeRate": _f(item.exchange_rate),
        "totalInclusiveValue": _f(item.total_inclusive_value),
        "boeNumber": item.boe_number,
        "billOfLadingNumber": item.bill_of_lading_number,
        "ladenOnBoardDate": _d(item.laden_on_board_date),
        "countryOfOrigin": item.country_of_origin,
        "licenseType": item.license_type,
        "licenseNumber": item.license_number,
        "shipmentStage": flags.shipment_stage(item),
        "qtyDiscrepancy": is_disc,
        "qtyDiscrepancyPct": disc_pct,
        "deliveryDateStatus": flags.delivery_date_status(item, datetime.date.today()),
        "mirMatch": _mir_match_dict(item),
    }


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


def _flag_dismissal_dict(fd):
    return {
        "flagKey": fd.flag_key,
        "dismissed": fd.dismissed,
        "dismissedBy": fd.dismissed_by_email,
        "dismissedReason": fd.dismissed_reason,
        "dismissedAt": fd.dismissed_at.isoformat() if fd.dismissed_at else None,
    }


def _po_dict(po, plant_key, plant_label, detail=False, sr_plant=None):
    items = list(po.items.all())
    today = datetime.date.today()
    item_dicts = [_item_dict(i) for i in items]
    total_incl_value = sum((i.total_inclusive_value or 0) for i in items)
    # PO-list rows show one BL/country - real POs in the live data ship as
    # one BOE per PO today, so "first item with a value" is representative;
    # the Items tab in the detail payload always shows every item's own value.
    bl_number = next((i.bill_of_lading_number for i in items if i.bill_of_lading_number), "")
    country = next((i.country_of_origin for i in items if i.country_of_origin), "")

    d = {
        "plant": plant_key,
        "plantLabel": plant_label,
        "poNumber": po.po_number,
        "poDriveFolderName": po.po_drive_folder_name,
        "vendorName": po.vendor_name,
        "createdDate": _d(po.po_created_date),
        "countryOfOrigin": country,
        "totalInclusiveValue": float(total_incl_value) if total_incl_value else 0.0,
        "billOfLadingNumber": bl_number,
        "shipmentStage": flags.po_shipment_stage(items),
        "deliveryDateStatus": flags.po_delivery_date_status(items, today),
        "partialDelivery": flags.partial_delivery(items),
        "qtyDiscrepancy": flags.po_has_qty_discrepancy(items),
        "dataQualityFlags": flags.po_flags(po.po_number, items),
        # Always included (not detail-only) - the combined list is the only
        # payload the dashboard's charts/KPIs read from (spec's charts need
        # per-item qty-discrepancy-pct, HSN, per-item country, etc.), and the
        # dataset is small enough (tens of POs today) that duplicating this
        # into every list row costs nothing worth optimizing away.
        "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms,
        "items": item_dicts,
    }
    if detail:
        corrections = (
            ImportPOCorrection.objects.filter(plant=sr_plant, po_number=po.po_number)
            if sr_plant else ImportPOCorrection.objects.none()
        )
        flag_dismissals = (
            FlagDismissal.objects.filter(plant=sr_plant, po_number=po.po_number)
            if sr_plant else FlagDismissal.objects.none()
        )
        d.update({
            "vendorAddress": po.vendor_address,
            "vendorGstin": po.vendor_gstin,
            "vendorEmail": po.vendor_email,
            "vendorCode": po.vendor_code,
            "billingAddress": po.billing_address,
            "shipTo": po.ship_to,
            "currency": po.currency,
            "totalValue": _f(po.total_value),
            "remarks": po.remarks,
            "corrections": [_correction_dict(c) for c in corrections],
            "flagDismissals": [_flag_dismissal_dict(fd) for fd in flag_dismissals],
        })
    return d


def _resolve_plant(plant_key):
    return _PLANTS.get(plant_key)


# ── Purchase order list/detail ────────────────────────────────────────────────

@api_view(["GET"])
def purchase_orders(request):
    """GET /api/imports/purchase-orders - all three plants combined, list
    shape (see _po_dict's non-detail fields). Spec section 1's "single
    dataset with a Plant field". Two of three plants have 0 rows today;
    that's fine, they just contribute nothing to the combined list rather
    than erroring. IsAuthenticated by default (any role) - additionally
    narrowed per-plant by user_can_access_plant() (added 2026-09-05,
    hardening pass): a plant the caller isn't scoped to is silently
    excluded from the combined list rather than erroring the whole
    request, same "you see less, not an error" shape as the rest of this
    filter already has for plants with zero rows."""
    result = []
    for plant_key, (po_model, _item_model, _sr_plant, label, _match_model) in _PLANTS.items():
        if not user_can_access_plant(request.user, plant_key):
            continue
        qs = po_model.objects.prefetch_related(
            "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
        )
        result.extend(_po_dict(po, plant_key, label) for po in qs)
    result.sort(key=lambda d: d["createdDate"] or "", reverse=True)
    return Response({"purchaseOrders": result})


@api_view(["GET"])
def purchase_order_detail(request, plant, po_number):
    """GET /api/imports/purchase-orders/<plant>/<po_number> - one PO, detail
    shape (adds vendor/billing fields, corrections, flag dismissals - see
    _po_dict's detail=True branch). 404s on an unknown `plant` segment or a
    po_number that doesn't exist for that plant."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    # 404, not 403 - matches the "unknown plant" response above rather than
    # confirming a PO exists for a plant the caller isn't scoped to. See
    # apps/api/permissions.py's user_can_access_plant() docstring.
    if not user_can_access_plant(request.user, plant):
        return Response({"error": "Unknown plant."}, status=404)
    po_model, _item_model, sr_plant, label, _match_model = resolved
    po = po_model.objects.prefetch_related(
        "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
    ).filter(po_number=po_number).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)
    return Response(_po_dict(po, plant, label, detail=True, sr_plant=sr_plant))


# ── Inline field corrections ──────────────────────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_field(request, plant, po_number):
    """Inline-edit endpoint backing every pencil icon in the detail modal.
    Body: {"itemId": "<optional>", "field": "<model field name>", "value": "<new value>"}.
    Writes the new value onto the real row AND an ImportPOCorrection audit
    row, in one transaction - see ImportPOCorrection's docstring."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    po_model, item_model, sr_plant, _label, _match_model = resolved

    item_id = (request.data.get("itemId") or "").strip()
    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")
    reason = (request.data.get("reason") or "").strip()

    po = po_model.objects.filter(po_number=po_number).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    if item_id:
        if field_name not in _ITEM_EDITABLE_FIELDS:
            return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
        target = item_model.objects.filter(purchase_order=po, item_id=item_id).first()
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
        ImportPOCorrection.objects.create(
            plant=sr_plant,
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
        _RUN_FULL_MATCH[plant]()

    response = {"status": "ok", "field": field_name, "value": _serialize(new_value)}
    warning = _field_warning(field_name, new_value)
    if warning:
        response["warning"] = warning
    return Response(response)


# _field_warning/_serialize are field-set-independent and byte-for-byte
# identical to _domestic_base's own copies (see CLAUDE.md's "Domestic router
# de-duplication") - shared from there rather than duplicated a fourth time.
# _coerce_value is NOT shared: this router's _DATE_FIELDS/_DECIMAL_FIELDS are
# a strict superset of the domestic routers' (BOE/exchange-rate fields the
# domestic plants don't have), so _domestic_base._coerce_value (which reads
# its own module-level field sets) can't be reused here without a further
# refactor to parametrize it - left as its own copy rather than force a
# fragile shared function for this pass.
from apps.api.routers._domestic_base import _field_warning, _serialize  # noqa: E402


def _coerce_value(field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in _DATE_FIELDS:
        return datetime.date.fromisoformat(raw_value)
    if field_name in _DECIMAL_FIELDS:
        import decimal
        from decimal import Decimal
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


# ── Sync status/trigger ────────────────────────────────────────────────────────

@api_view(["GET"])
def sync_status(request):
    """GET /api/imports/sync-status - latest IMPORT_PO_CSV SyncRun per plant,
    plus each plant's own in-progress flag (see is_imports_sync_in_progress)
    - same "last synced, not just trust stale data" reasoning as the domestic
    routers' own sync_status views."""
    latest_by_plant = {}
    for plant_key, (_po_model, _item_model, sr_plant, _label, _match_model) in _PLANTS.items():
        if not user_can_access_plant(request.user, plant_key):
            continue
        run = SyncRun.objects.filter(plant=sr_plant, source=SyncRun.Source.IMPORT_PO_CSV).order_by("-started_at").first()
        latest_by_plant[plant_key] = {
            "status": run.status if run else None,
            "startedAt": run.started_at.isoformat() if run else None,
            "finishedAt": run.finished_at.isoformat() if run and run.finished_at else None,
            "rowsSeen": run.rows_seen if run else 0,
            "rowsChanged": run.rows_changed if run else 0,
            "errorDetail": (run.error_detail or None) if run else None,
            "syncInProgress": is_imports_sync_in_progress(plant_key),
        }
    return Response({"sync": latest_by_plant})


@api_view(["POST"])
@permission_classes([IsAdmin])
@throttle_classes([SyncTriggerThrottle])
def sync_trigger(request, plant):
    """POST /api/imports/sync-trigger/<plant> - kicks off that plant's
    Imports CSV sync (+ match_<plant>, see apps/services/sync_trigger.py's
    _IMPORT_PLANT_COMMANDS) on a background thread. Admin-only: a real Drive
    API call, not just a read."""
    if plant not in _PLANTS:
        return Response({"error": "Unknown plant."}, status=404)
    started = trigger_plant_imports_sync(plant)
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "started"}, status=202)


# ── Match dismiss / flag dismiss ──────────────────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_import_po_mir_match(request, plant, match_id: int):
    """PATCH /api/imports/matches/po-mir/<plant>/<match_id>/dismiss
    Body: {"dismissed": true/false, "reason": "<optional>"}. Same shape as
    hrs_views.py's/achhad_views.py's/vapi_views.py's own dismiss_po_mir_match
    - see that function's docstring - parametrized by `plant` here instead
    of one copy per plant, matching this module's own cross-plant
    convention (see module docstring)."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    _po_model, _item_model, _sr_plant, _label, match_model = resolved
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(match_model, match_id, request.user, dismissed, reason)
    if not match:
        return Response({"error": "Match not found."}, status=404)
    return Response({
        "status": "ok",
        "matchId": match.id,
        "dismissedByOverride": match.dismissed_by_override,
        "dismissedReason": match.dismissed_reason,
    })


@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_flag(request, plant, po_number):
    """PATCH /api/imports/purchase-orders/<plant>/<po_number>/flags/dismiss
    Body: {"flagKey": "<code>:<item_id|''>", "dismissed": true/false, "reason": "<optional>"}.
    See hrs_views.dismiss_flag's docstring for why this is a separate
    FlagDismissal row rather than a column on a match model - identical
    reasoning, just for apps/services/import_flags.py's per-item flags
    (`code`/`item_id`) instead of Domestic's per-category label. The
    frontend builds flagKey as `f.code + ':' + (f.item_id || '')` from each
    dataQualityFlags entry - this view treats it as an opaque string either way."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    _po_model, _item_model, sr_plant, _label, _match_model = resolved
    flag_key = (request.data.get("flagKey") or "").strip()
    if not flag_key:
        return Response({"error": "flagKey is required."}, status=400)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    fd = dismiss_po_flag(sr_plant, po_number, flag_key, request.user, dismissed, reason)
    return Response(_flag_dismissal_dict(fd))
