"""
Read-only API surface for the HRS Purchase Tracker dashboard, plus one
write endpoint (sync_trigger) added 2026-09-04.

Every read view here requires authentication (the default IsAuthenticated
from REST_FRAMEWORK settings - any role, admin/editor/viewer, can read the
dashboard; see apps/api/permissions.py). Plain dict serialization rather
than DRF serializers: the payload shapes are derived/nested (a PO's status
isn't a column, it's computed from its line items' match state) in a way a
straight ModelSerializer wouldn't save much over just building the dict
directly. sync_trigger is IsAdmin-only - it kicks off a real Google Drive
sync (external API calls, real time/cost), not just a read.

This file is the "reference" plant of a deliberate three-way sibling set -
achhad_views.py and vapi_views.py are byte-for-byte-shaped ports of this
file (same function names, same response field shapes), not accidental
duplication. See CLAUDE.md's "Per-plant models, not a shared schema" for
why HRS/Achhad/Vapi get their own model classes, matching modules, and
routers instead of one shared, plant-discriminated table: their real
MIR/Stock spreadsheets have genuinely different column layouts, so a
shared schema would mean permanently-null columns for whichever plant
doesn't have that field. Because this file carries the full docstrings,
achhad_views.py/vapi_views.py mostly just point back here and call out
what's genuinely different for their own plant.
"""

import datetime
import decimal
from decimal import Decimal

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import IsEditor, IsAdmin, user_can_edit_plant
from apps.core.models import (
    DomesticPOCorrection,
    FlagDismissal,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOLineItem,
    HRSPOMirMatch,
    HRSPurchaseOrder,
    HRSStockLot,
    HRSStockSnapshot,
    MaterialCorrection,
    SyncRun,
)
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
from apps.services.matching import run_full_match
from apps.services.sync_trigger import is_sync_in_progress, trigger_plant_sync
from apps.services.validation import is_valid_email, is_valid_gstin

# Inline "Edit Everywhere" - every field a pencil icon in the PO detail modal
# can correct. Mirrors apps/api/routers/imports_views.py's own allow-lists;
# see that file's correct_field docstring for the pattern this is copied
# from. No BOE/BL/license/exchange-rate fields here - those don't exist on
# domestic PO/line-item models at all (Domestic POs never clear customs).
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "tax_type",
    "total_inclusive_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {"description", "hsn", "qty", "uom", "delivery_date", "net_price", "net_value"}
_DATE_FIELDS = {"po_created_date", "delivery_date"}
_DECIMAL_FIELDS = {"total_value", "total_inclusive_value", "qty", "net_price", "net_value"}

# Editing any of these can change a PO's PO<->MIR match outcome (vendor/
# material text used for candidate gating, or a value compared against MIR)
# - see this file's correct_field for why run_full_match() is re-triggered
# synchronously for exactly these fields, not every field.
_REMATCH_TRIGGER_FIELDS = {"vendor_name", "vendor_gstin", "description", "qty", "net_price", "net_value"}

# Inline "Edit Everywhere" for the Raw Material Analysis modal's Stock by
# Plant table - a Stock lot's own editable fields. Deliberately excludes
# quantity/value/date columns (opening_stock, received, issued,
# todays_stock, value, received_date) - those are the whole point of
# syncing the Stock sheet in the first place, unlike a PO's vendor/address
# fields which are just occasionally-mistyped metadata; correcting them here
# would silently diverge from the source of truth instead of fixing a typo.
_MATERIAL_EDITABLE_FIELDS = {"description", "category", "sub_category", "uom", "basic_rate", "party_name"}
_MATERIAL_DECIMAL_FIELDS = {"basic_rate"}
# Editing any of these can change this lot's MIR<->Stock match outcome -
# material/vendor text used for the (material, vendor) gate, or the rate
# compared against MIR - same reasoning as _REMATCH_TRIGGER_FIELDS above.
_MATERIAL_REMATCH_TRIGGER_FIELDS = {"description", "party_name", "basic_rate"}


# ── Serialization helpers ─────────────────────────────────────────────────────

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
        # Raw computed value - still shown as a "flagged" badge, but the
        # UI reads dismissedByOverride separately to render it muted/
        # struck-through instead of hiding the badge outright, so a
        # reviewer who dismissed it can still see what was dismissed.
        "matchFlagged": match.is_flagged if match else False,
        "dismissedByOverride": match.dismissed_by_override if match else False,
        "dismissedReason": match.dismissed_reason if match else "",
        "dismissedBy": match.dismissed_by.email if match and match.dismissed_by else None,
        "dismissedAt": match.dismissed_at.isoformat() if match and match.dismissed_at else None,
        # Exposed separately (previously only folded into the single
        # matchFlagged bool) so the frontend can tell "partial delivery"
        # (qty diff) apart from a real price/value discrepancy instead of
        # showing one ambiguous "review" badge for both.
        "qtyDiffPct": float(match.qty_diff_pct) if match and match.qty_diff_pct is not None else None,
        "rateDiffPct": float(match.rate_diff_pct) if match and match.rate_diff_pct is not None else None,
        "valueDiffPct": float(match.value_diff_pct) if match and match.value_diff_pct is not None else None,
        "matchedMirNo": match.mir_entry.mir_no if match else None,
        # 3rd stepper step ("Stocked") - true if the matched MIR entry
        # itself has a real MIR<->Stock match (HRSMirStockMatch), i.e. the
        # material this PO line item was inwarded as is also currently
        # sitting in the Stock file, not just MIR-received. Relies on
        # purchase_orders()'s prefetch_related including
        # "items__mir_match__mir_entry__stock_matches" so this reads the
        # prefetch cache, not a new query per line item.
        "stockMatched": bool(match and len(match.mir_entry.stock_matches.all()) > 0),
    }


def _correction_dict(c):
    return {
        "fieldName": c.field_name,
        "itemId": c.item_id,
        "oldValue": c.old_value,
        "newValue": c.new_value,
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


def _po_dict(po):
    items = list(po.items.all())
    corrections = DomesticPOCorrection.objects.filter(plant=SyncRun.Plant.HRS, po_number=po.po_number)
    flag_dismissals = FlagDismissal.objects.filter(plant=SyncRun.Plant.HRS, po_number=po.po_number)
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


# ── Purchase order list / inline field corrections ───────────────────────────

@api_view(["GET"])
def purchase_orders(request):
    """GET /api/hrs/purchase-orders - every HRS domestic PO with its line
    items and PO<->MIR match state. IsAuthenticated by default (any role)."""
    qs = HRSPurchaseOrder.objects.prefetch_related(
        "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
    )
    return Response({"purchaseOrders": [_po_dict(po) for po in qs]})


@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_field(request, po_number):
    """Inline-edit endpoint backing every pencil icon in the Domestic PO
    detail modal - same shape as imports_views.correct_field (allow-listed
    field name, PO-level vs item-level branch on whether `itemId` is set,
    date/decimal coercion, mutate + DomesticPOCorrection audit row in one
    transaction), see that view's docstring for the pattern this mirrors.
    Body: {"itemId": "<optional>", "field": "<model field name>", "value": "<new value>"}."""
    if not user_can_edit_plant(request.user, "hrs"):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

    item_id = (request.data.get("itemId") or "").strip()
    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")

    po = HRSPurchaseOrder.objects.filter(po_number=po_number).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    if item_id:
        if field_name not in _ITEM_EDITABLE_FIELDS:
            return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
        target = HRSPOLineItem.objects.filter(purchase_order=po, item_id=item_id).first()
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
            plant=SyncRun.Plant.HRS,
            po_number=po_number,
            item_id=item_id,
            field_name=field_name,
            old_value="" if old_value is None else str(old_value),
            new_value="" if new_value is None else str(new_value),
            corrected_by=request.user if getattr(request.user, "pk", None) else None,
            corrected_by_email=getattr(request.user, "email", ""),
        )

    if field_name in _REMATCH_TRIGGER_FIELDS:
        # Idempotent, safe to re-run (see apps/services/matching.py's own
        # docstring) - this is what makes the qty/rate/value discrepancy
        # badges reflect a correction immediately instead of staying stale
        # until the next scheduled match_hrs run.
        run_full_match()

    response = {"status": "ok", "field": field_name, "value": _serialize(new_value)}
    warning = _field_warning(field_name, new_value)
    if warning:
        response["warning"] = warning
    return Response(response)


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


def _material_correction_dict(c):
    return {
        "fieldName": c.field_name,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "correctedBy": c.corrected_by_email,
        "correctedAt": c.corrected_at.isoformat(),
    }


def _lot_dict(lot):
    corrections = MaterialCorrection.objects.filter(plant=SyncRun.Plant.HRS, lot_id=lot.id)
    return {
        "lotId": lot.id,
        "materialCode": lot.sap_item_code or str(lot.id),
        "description": lot.description,
        "category": lot.category,
        "subCategory": lot.sub_category,
        "corrections": [_material_correction_dict(c) for c in corrections],
        "uom": lot.uom,
        "qty": float(lot.todays_stock),
        "rate": float(lot.basic_rate) if lot.basic_rate is not None else None,
        "value": float(lot.value) if lot.value is not None else None,
        "vendor": lot.party_name,
        "receivedDate": lot.received_date.isoformat() if lot.received_date else None,
        "noOfDays": lot.no_of_days,
        # Real signal (from HRSMirStockMatch, the actual Stock<->MIR match
        # computed by apps/services/matching.py's match_mir_entry_stock()),
        # not a proxy - the frontend's Raw Material Analysis modal/table used
        # to infer "has this material been MIR-received" from whether its
        # best-effort, fuzzy PO<->material text match happened to also carry
        # a matched PO line item, which could show a lot as "in stock but
        # never MIR'd" even when a real match existed, simply because the
        # fuzzy PO link failed - see that frontend code's own comment for the
        # 2026-09-04 fix this field was added for. Relies on `lot` coming
        # from a queryset with .prefetch_related("mir_matches") (see
        # materials() below) so this reads the prefetch cache, not a new
        # query per lot.
        "mirMatched": len(lot.mir_matches.all()) > 0,
        # Per-match detail (id + flag/dismissal state) so the frontend can
        # render a dismiss control per MIR<->Stock pairing instead of just
        # the collapsed boolean above - added alongside the dismiss/override
        # endpoints below.
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


# ── Material corrections (Raw Material Analysis modal) ────────────────────────

@api_view(["GET"])
def materials(request):
    """GET /api/hrs/materials - one row per Stock lot, not one row per
    material - HRS's real Stock sheet is lot-shaped (multiple vendors/rates
    for the same material), and collapsing that into a fake per-material
    aggregate would hide exactly the vendor-lot detail the matching engine
    relies on. IsAuthenticated by default (any role)."""
    qs = HRSStockLot.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
    return Response({"materials": [_lot_dict(lot) for lot in qs]})


@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_material_field(request, lot_id: int):
    """Inline-edit endpoint backing every pencil icon in the Raw Material
    Analysis modal's Stock by Plant table - same mutate + audit-row shape as
    correct_field() above, just against HRSStockLot instead of a PO/line
    item, and MaterialCorrection instead of DomesticPOCorrection.
    Body: {"field": "<model field name>", "value": "<new value>"}. `itemId`
    isn't part of this body (a lot has no line items), unlike correct_field's
    body shape - startFieldEdit()/savePoField() (shared.js) always send an
    itemId key regardless of caller, it's simply unused here."""
    if not user_can_edit_plant(request.user, "hrs"):
        return Response({"error": "You are not permitted to edit this plant's materials."}, status=403)

    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")

    if field_name not in _MATERIAL_EDITABLE_FIELDS:
        return Response({"error": f"{field_name!r} is not an editable material field."}, status=400)

    lot = HRSStockLot.objects.filter(id=lot_id, is_active=True).first()
    if not lot:
        return Response({"error": "Material lot not found."}, status=404)

    old_value = getattr(lot, field_name)
    try:
        new_value = _coerce_material_value(field_name, raw_value)
    except (TypeError, ValueError):
        return Response({"error": f"Invalid value for {field_name!r}: {raw_value!r}"}, status=400)

    with transaction.atomic():
        setattr(lot, field_name, new_value)
        lot.save(update_fields=[field_name])
        MaterialCorrection.objects.create(
            plant=SyncRun.Plant.HRS,
            lot_id=lot_id,
            field_name=field_name,
            old_value="" if old_value is None else str(old_value),
            new_value="" if new_value is None else str(new_value),
            corrected_by=request.user if getattr(request.user, "pk", None) else None,
            corrected_by_email=getattr(request.user, "email", ""),
        )

    if field_name in _MATERIAL_REMATCH_TRIGGER_FIELDS:
        run_full_match()

    return Response({"status": "ok", "field": field_name, "value": _serialize(new_value)})


def _coerce_material_value(field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in _MATERIAL_DECIMAL_FIELDS:
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


# ── Stock trend / sync status / sync trigger ──────────────────────────────────

@api_view(["GET"])
def stock_trend(request, lot_id: int):
    """GET /api/hrs/materials/<lot_id>/trend - daily HRSStockSnapshot history
    for one lot (qty/rate/value over time), used by the Raw Material
    Analysis modal's trend chart. IsAuthenticated by default (any role)."""
    snapshots = HRSStockSnapshot.objects.filter(stock_lot_id=lot_id).order_by("snapshot_date")
    return Response({
        "snapshots": [
            {
                "date": s.snapshot_date.isoformat(),
                "qty": float(s.todays_stock),
                "rate": float(s.basic_rate) if s.basic_rate is not None else None,
                "value": float(s.value) if s.value is not None else None,
            }
            for s in snapshots
        ]
    })


@api_view(["GET"])
def sync_status(request):
    """Latest SyncRun per source, so the dashboard can show "last synced"
    instead of silently trusting stale data (same reasoning as the TDS
    app's per-endpoint cache-vs-freshness notes). syncInProgress reflects
    the sync_trigger lock (see apps/services/sync_trigger.py), not anything
    stored on SyncRun itself - SyncRun rows are only ever written on
    completion, there's no in-flight status to read there."""
    latest_by_source = {}
    for run in SyncRun.objects.filter(plant=SyncRun.Plant.HRS).order_by("source", "-started_at"):
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
        "mirEntryCount": HRSMIREntry.objects.filter(is_active=True).count(),
        "syncInProgress": is_sync_in_progress("hrs"),
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def sync_trigger(request):
    """Kicks off a real Google Drive sync + match pipeline for HRS on a
    background thread (see apps/services/sync_trigger.py) and returns
    immediately - the frontend polls sync_status's new syncInProgress flag
    to know when it's done, same pattern the "Refresh Data" button now uses
    for every plant."""
    started = trigger_plant_sync("hrs")
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "started"}, status=202)


# ── Match dismiss / override, flag dismissal ──────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_po_mir_match(request, match_id: int):
    """PATCH /api/matches/po-mir/<match_id>/dismiss
    Body: {"dismissed": true/false, "reason": "<optional>"}.
    Lets an editor/admin mark a flagged PO<->MIR match as reviewed-and-fine
    (dismissed_by_override) without it re-flagging on the next match_hrs
    run - update_or_create's defaults dict in matching.py never touches
    this field, so a dismissal survives re-matching. Editor-only per the
    role model in CLAUDE.md; IsAdmin isn't required since this is a review
    action, not an account/config change."""
    if not user_can_edit_plant(request.user, "hrs"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(HRSPOMirMatch, match_id, request.user, dismissed, reason)
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
def dismiss_mir_stock_match(request, match_id: int):
    """PATCH /api/matches/mir-stock/<match_id>/dismiss - same shape as
    dismiss_po_mir_match above, for HRSMirStockMatch rows instead."""
    if not user_can_edit_plant(request.user, "hrs"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(HRSMirStockMatch, match_id, request.user, dismissed, reason)
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
def dismiss_flag(request, po_number):
    """PATCH /api/purchase-orders/<po_number>/flags/dismiss
    Body: {"flagKey": "<Quantity Discrepancy|...>", "dismissed": true/false, "reason": "<optional>"}.
    Manual dismiss/reinstate for a PO-level flag shown in the Flags &
    Corrections tab (the critical Quantity/Rate-Value Discrepancy flags and
    the Data Quality Flag category, both computed client-side by main.js's
    computePoFlags() - there's no match row to attach dismissed_by_override
    to for these, hence the separate FlagDismissal table/dismiss_po_flag()
    helper instead of reusing dismiss_match(). Does not require the PO to
    exist as a row lookup first - a PO number typo would just create an
    orphaned dismissal row nobody ever reads, same low-stakes tradeoff
    dismiss_match() accepts for match_id today."""
    if not user_can_edit_plant(request.user, "hrs"):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    flag_key = (request.data.get("flagKey") or "").strip()
    if not flag_key:
        return Response({"error": "flagKey is required."}, status=400)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    fd = dismiss_po_flag(SyncRun.Plant.HRS, po_number, flag_key, request.user, dismissed, reason)
    return Response(_flag_dismissal_dict(fd))
