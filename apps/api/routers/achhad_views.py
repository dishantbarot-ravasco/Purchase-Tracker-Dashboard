"""
Read-only API surface for the RTP-Achhad Purchase Tracker dashboard, plus
sync_trigger/dismiss/correction write endpoints - byte-for-byte-shaped
sibling of apps/api/routers/hrs_views.py (same function names, same
response field shapes, same IsAuthenticated-by-default posture), ported
deliberately rather than accidentally duplicated. See CLAUDE.md's
"Per-plant models, not a shared schema" for why HRS/Achhad/Vapi each get
their own model set and router instead of one shared, plant-discriminated
table - the short version: their real MIR/Stock spreadsheets have
genuinely different column layouts, not just different values in the same
columns. This file mostly points back to hrs_views.py for the parts that
are identical, and calls out only what's genuinely different for Achhad:
no vendor column on Stock (RTPAchhadStockLot has no party_name field - see
_lot_dict below), and a real `msl` (Minimum Stock Level) field HRS's sheet
has no equivalent of. The frontend reuses one rendering path for all three
plants - see frontend/js/main.js's plant switch.
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
    MaterialCorrection,
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadPOLineItem,
    RTPAchhadPOMirMatch,
    RTPAchhadPurchaseOrder,
    RTPAchhadStockLot,
    RTPAchhadStockSnapshot,
    SyncRun,
)
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
from apps.services.matching_achhad import run_full_match
from apps.services.sync_trigger import is_sync_in_progress, trigger_plant_sync
from apps.services.validation import is_valid_email, is_valid_gstin

# See hrs_views.py's identical constants/correct_field for the pattern this
# mirrors - kept as a separate per-plant copy rather than shared, matching
# this codebase's existing per-plant-router convention.
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "tax_type",
    "total_inclusive_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {"description", "hsn", "qty", "uom", "delivery_date", "net_price", "net_value"}
_DATE_FIELDS = {"po_created_date", "delivery_date"}
_DECIMAL_FIELDS = {"total_value", "total_inclusive_value", "qty", "net_price", "net_value"}
_REMATCH_TRIGGER_FIELDS = {"vendor_name", "vendor_gstin", "description", "qty", "net_price", "net_value"}

# See hrs_views.py's identical constants for the pattern this mirrors.
# RTPAchhadStockLot has no sub_category/uom/vendor field at all (Achhad's
# Stock sheet is material-shaped, not lot-shaped - see that model's
# docstring), and gained a real Achhad-specific `msl` (Minimum Stock Level)
# column HRS's sheet has no equivalent of - hence this set genuinely
# differs from HRS's/Vapi's, not just a copy-paste.
_MATERIAL_EDITABLE_FIELDS = {"description", "category", "rate", "msl"}
_MATERIAL_DECIMAL_FIELDS = {"rate", "msl"}
# Achhad's MIR<->Stock match gates on material description alone (no vendor
# column to also gate on - see RTPAchhadStockLot's docstring), so only
# description/rate can move a match outcome here; msl never feeds matching.
_MATERIAL_REMATCH_TRIGGER_FIELDS = {"description", "rate"}


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
        "matchFlagged": match.is_flagged if match else False,
        "dismissedByOverride": match.dismissed_by_override if match else False,
        "dismissedReason": match.dismissed_reason if match else "",
        "dismissedBy": match.dismissed_by.email if match and match.dismissed_by else None,
        "dismissedAt": match.dismissed_at.isoformat() if match and match.dismissed_at else None,
        # See hrs_views.py's _line_item_dict for why these are exposed
        # separately from matchFlagged.
        "qtyDiffPct": float(match.qty_diff_pct) if match and match.qty_diff_pct is not None else None,
        "rateDiffPct": float(match.rate_diff_pct) if match and match.rate_diff_pct is not None else None,
        "valueDiffPct": float(match.value_diff_pct) if match and match.value_diff_pct is not None else None,
        "matchedMirNo": match.mir_entry.mir_no if match else None,
        # See hrs_views.py's _line_item_dict for what this is - relies on
        # purchase_orders()'s prefetch_related below including the same
        # "items__mir_match__mir_entry__stock_matches" path.
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
    corrections = DomesticPOCorrection.objects.filter(plant=SyncRun.Plant.RTP_ACHHAD, po_number=po.po_number)
    flag_dismissals = FlagDismissal.objects.filter(plant=SyncRun.Plant.RTP_ACHHAD, po_number=po.po_number)
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
    """See hrs_views.purchase_orders - identical shape, this plant's model."""
    qs = RTPAchhadPurchaseOrder.objects.prefetch_related(
        "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
    )
    return Response({"purchaseOrders": [_po_dict(po) for po in qs]})


@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_field(request, po_number):
    """See hrs_views.correct_field - identical shape, this plant's models."""
    if not user_can_edit_plant(request.user, "achhad"):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

    item_id = (request.data.get("itemId") or "").strip()
    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")

    po = RTPAchhadPurchaseOrder.objects.filter(po_number=po_number).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    if item_id:
        if field_name not in _ITEM_EDITABLE_FIELDS:
            return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
        target = RTPAchhadPOLineItem.objects.filter(purchase_order=po, item_id=item_id).first()
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
            plant=SyncRun.Plant.RTP_ACHHAD,
            po_number=po_number,
            item_id=item_id,
            field_name=field_name,
            old_value="" if old_value is None else str(old_value),
            new_value="" if new_value is None else str(new_value),
            corrected_by=request.user if getattr(request.user, "pk", None) else None,
            corrected_by_email=getattr(request.user, "email", ""),
        )

    if field_name in _REMATCH_TRIGGER_FIELDS:
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
    corrections = MaterialCorrection.objects.filter(plant=SyncRun.Plant.RTP_ACHHAD, lot_id=lot.id)
    return {
        "lotId": lot.id,
        "materialCode": lot.sap_code or str(lot.id),
        "description": lot.description,
        "category": lot.category,
        "subCategory": "",
        "corrections": [_material_correction_dict(c) for c in corrections],
        "uom": "",
        "qty": float(lot.todays_stock),
        "rate": float(lot.rate) if lot.rate is not None else None,
        "value": float(lot.value) if lot.value is not None else None,
        # No vendor column on Achhad's Stock sheet at all - see
        # RTPAchhadStockLot's docstring. Left out rather than faked with a
        # placeholder, unlike HRS's _lot_dict which has a real value here.
        "vendor": None,
        "receivedDate": lot.received_date.isoformat() if lot.received_date else None,
        "noOfDays": None,
        # See hrs_views.py's _lot_dict() for what this is and why it replaced
        # a frontend-side proxy - same reasoning, same field name.
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


# ── Material corrections (Raw Material Analysis modal) ────────────────────────

@api_view(["GET"])
def materials(request):
    """See hrs_views.materials - same lot-shaped reasoning, but note Achhad's
    Stock sheet is material-shaped, not lot-shaped in the vendor sense (no
    party_name column at all - see _lot_dict's `vendor` field below)."""
    qs = RTPAchhadStockLot.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
    return Response({"materials": [_lot_dict(lot) for lot in qs]})


@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_material_field(request, lot_id: int):
    """See hrs_views.correct_material_field - identical shape, this plant's
    model and its own _MATERIAL_EDITABLE_FIELDS."""
    if not user_can_edit_plant(request.user, "achhad"):
        return Response({"error": "You are not permitted to edit this plant's materials."}, status=403)

    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")

    if field_name not in _MATERIAL_EDITABLE_FIELDS:
        return Response({"error": f"{field_name!r} is not an editable material field."}, status=400)

    lot = RTPAchhadStockLot.objects.filter(id=lot_id, is_active=True).first()
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
            plant=SyncRun.Plant.RTP_ACHHAD,
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
    """See hrs_views.stock_trend - identical shape, this plant's snapshot model."""
    snapshots = RTPAchhadStockSnapshot.objects.filter(stock_lot_id=lot_id).order_by("snapshot_date")
    return Response({
        "snapshots": [
            {
                "date": s.snapshot_date.isoformat(),
                "qty": float(s.todays_stock),
                "rate": float(s.rate) if s.rate is not None else None,
                "value": float(s.value) if s.value is not None else None,
            }
            for s in snapshots
        ]
    })


@api_view(["GET"])
def sync_status(request):
    """See hrs_views.sync_status - identical shape, this plant's key."""
    latest_by_source = {}
    for run in SyncRun.objects.filter(plant=SyncRun.Plant.RTP_ACHHAD).order_by("source", "-started_at"):
        if run.source not in latest_by_source:
            latest_by_source[run.source] = {
                "status": run.status,
                "startedAt": run.started_at.isoformat(),
                "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
                "rowsSeen": run.rows_seen,
                "rowsChanged": run.rows_changed,
            }
    return Response({
        "sync": latest_by_source,
        "mirEntryCount": RTPAchhadMIREntry.objects.filter(is_active=True).count(),
        "syncInProgress": is_sync_in_progress("achhad"),
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def sync_trigger(request):
    """See hrs_views.sync_trigger - identical shape, this plant's key."""
    started = trigger_plant_sync("achhad")
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "started"}, status=202)


# ── Match dismiss / override, flag dismissal ──────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_po_mir_match(request, match_id: int):
    """See hrs_views.dismiss_po_mir_match - identical shape, this plant's model."""
    if not user_can_edit_plant(request.user, "achhad"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(RTPAchhadPOMirMatch, match_id, request.user, dismissed, reason)
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
    """See hrs_views.dismiss_mir_stock_match - identical shape, this plant's model."""
    if not user_can_edit_plant(request.user, "achhad"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(RTPAchhadMirStockMatch, match_id, request.user, dismissed, reason)
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
    """See hrs_views.dismiss_flag - identical shape, this plant."""
    if not user_can_edit_plant(request.user, "achhad"):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    flag_key = (request.data.get("flagKey") or "").strip()
    if not flag_key:
        return Response({"error": "flagKey is required."}, status=400)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    fd = dismiss_po_flag(SyncRun.Plant.RTP_ACHHAD, po_number, flag_key, request.user, dismissed, reason)
    return Response(_flag_dismissal_dict(fd))
