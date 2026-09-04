"""
Read-only API surface for the RTP-Vapi Purchase Tracker dashboard. Mirrors
apps/api/routers/hrs_views.py's shape (Vapi's Stock sheet is lot-shaped with
a real vendor column, like HRS's, not material-shaped like Achhad's - see
the RTP-Vapi section header comment in apps/core/models.py) so the frontend
can reuse one rendering path for all three plants - see
frontend/js/main.js's plant switch.
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
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiPOLineItem,
    RTPVapiPOMirMatch,
    RTPVapiPurchaseOrder,
    RTPVapiStockLot,
    RTPVapiStockSnapshot,
    SyncRun,
)
from apps.services.match_dismiss import dismiss_match
from apps.services.matching_vapi import run_full_match
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


def _po_dict(po):
    items = list(po.items.all())
    corrections = DomesticPOCorrection.objects.filter(plant=SyncRun.Plant.RTP_VAPI, po_number=po.po_number)
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
    }


@api_view(["GET"])
def purchase_orders(request):
    qs = RTPVapiPurchaseOrder.objects.prefetch_related(
        "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
    )
    return Response({"purchaseOrders": [_po_dict(po) for po in qs]})


@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_field(request, po_number):
    """See hrs_views.correct_field - identical shape, this plant's models."""
    if not user_can_edit_plant(request.user, "vapi"):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

    item_id = (request.data.get("itemId") or "").strip()
    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")

    po = RTPVapiPurchaseOrder.objects.filter(po_number=po_number).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    if item_id:
        if field_name not in _ITEM_EDITABLE_FIELDS:
            return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
        target = RTPVapiPOLineItem.objects.filter(purchase_order=po, item_id=item_id).first()
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
            plant=SyncRun.Plant.RTP_VAPI,
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


def _lot_dict(lot):
    return {
        "lotId": lot.id,
        "materialCode": lot.hsn_code or str(lot.id),
        "description": lot.description,
        "category": lot.category,
        "subCategory": lot.sub_category,
        "uom": lot.uom,
        "qty": float(lot.todays_stock),
        "rate": float(lot.basic_rate) if lot.basic_rate is not None else None,
        "value": float(lot.value) if lot.value is not None else None,
        "vendor": lot.supplier_name,
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


@api_view(["GET"])
def materials(request):
    """One row per Stock lot - Vapi's real Stock sheet has a genuine
    Supplier Name column, same lot-shaped reasoning as HRS's own
    materials() view (see that view's docstring)."""
    qs = RTPVapiStockLot.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
    return Response({"materials": [_lot_dict(lot) for lot in qs]})


@api_view(["GET"])
def stock_trend(request, lot_id: int):
    snapshots = RTPVapiStockSnapshot.objects.filter(stock_lot_id=lot_id).order_by("snapshot_date")
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
    latest_by_source = {}
    for run in SyncRun.objects.filter(plant=SyncRun.Plant.RTP_VAPI).order_by("source", "-started_at"):
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
        "mirEntryCount": RTPVapiMIREntry.objects.filter(is_active=True).count(),
        "syncInProgress": is_sync_in_progress("vapi"),
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def sync_trigger(request):
    """See hrs_views.sync_trigger - identical shape, this plant's key."""
    started = trigger_plant_sync("vapi")
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "started"}, status=202)


@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_po_mir_match(request, match_id: int):
    """See hrs_views.dismiss_po_mir_match - identical shape, this plant's model."""
    if not user_can_edit_plant(request.user, "vapi"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(RTPVapiPOMirMatch, match_id, request.user, dismissed, reason)
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
    if not user_can_edit_plant(request.user, "vapi"):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    dismissed = bool(request.data.get("dismissed", True))
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(RTPVapiMirStockMatch, match_id, request.user, dismissed, reason)
    if not match:
        return Response({"error": "Match not found."}, status=404)
    return Response({
        "status": "ok",
        "matchId": match.id,
        "dismissedByOverride": match.dismissed_by_override,
        "dismissedReason": match.dismissed_reason,
    })
