"""
/api/mir/... - MIR entry against the normalized POs (2026-09-28).
The rules live in apps/services/mir_service.py; this file only gates, parses
and serializes.

Access:
  - Reading MIRs and mismatches: any role, filtered to the plants the account
    may read (user_can_access_plant()).
  - Entering, previewing and cancelling a MIR: Editor or Admin, and the
    account must be allowed to edit the RECEIVING plant.
  - Finding a PO to receive against (open-pos, purchase-orders/<id>,
    vendors): Editor or Admin, across EVERY plant, by the project owner's
    rule - any plant's store may receive any plant's open PO. These three
    return PO data for plants the account may not otherwise read, which is
    the point; test_endpoint_permission_guard.py records it.
  - Resolving a mismatch, closing/reopening a PO line and clearing a line's
    review flag: Editor or Admin at the PO's plant (or the MIR's plant, for
    a mismatch).

Money and quantities travel as strings, never floats, so what the screen
shows is exactly what was stored.
"""

from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import IsEditor, user_can_access_plant, user_can_edit_plant
from apps.core.models import (
    Mir,
    MirMismatch,
    MirReasonCode,
    Plant,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from apps.services import mir_service
from apps.services import procurement_rules as rules


def _s(value):
    """Decimal -> string as stored; None stays None."""
    return None if value is None else str(value)


def _d(value):
    return None if value is None else value.isoformat()


def _bad(exc: mir_service.MirValidationError):
    return Response({"error": exc.errors[0]["message"], "errors": exc.errors}, status=http.HTTP_400_BAD_REQUEST)


def _forbidden(message="You are not allowed to do this for that plant."):
    return Response({"error": message}, status=http.HTTP_403_FORBIDDEN)


def _vendor(v):
    if v is None:
        return None
    return {"id": v.id, "name": v.name, "gstin": v.gstin, "code": v.vendor_code}


def _plant(p):
    return {"code": p.code, "name": p.name}


def _po_line(line, state, suggested=("", "")):
    po = line.purchase_order
    return {
        "id": line.id, "lineNo": line.line_no, "itemCode": line.item_code, "description": line.description,
        "hsn": line.hsn, "uom": line.uom, "uomKnown": line.uom in rules.KNOWN_UOMS, "qtyOrdered": _s(line.qty_ordered),
        "rate": _s(line.rate), "deliveryDate": _d(line.delivery_date), "accepted": _s(state["accepted"]),
        "openQty": _s(state["open_qty"]), "status": state["status"], "receivable": state["receivable"],
        "blockedReason": state["blocked_reason"], "needsReview": line.needs_review, "reviewNote": line.review_note,
        "closeNote": line.close_note, "poId": po.id, "poNumber": po.po_number, "plant": po.plant.code,
        "currency": po.currency, "poGstRate": _s(rules.po_gst_rate(po.total_value, po.total_inclusive_value)),
        "suggestedCategory": suggested[0], "suggestedSubcategory": suggested[1],
    }


def _po_summary(po):
    return {
        "id": po.id, "poNumber": po.po_number, "poDate": _d(po.po_date), "plant": _plant(po.plant),
        "vendor": _vendor(po.vendor), "taxType": po.tax_type, "currency": po.currency,
        "gstRate": _s(rules.po_gst_rate(po.total_value, po.total_inclusive_value)),
    }


def _po_header(po):
    """The PO as the purchase team raised it - shown above its lines so the
    clerk can check the delivery against the order."""
    v = po.vendor
    return {
        "vendorAddress": v.address if v else "", "vendorEmail": v.email if v else "",
        "billingAddress": po.billing_address, "shipTo": po.ship_to, "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms, "taxTypeRaw": po.tax_type_raw, "totalValue": _s(po.total_value),
        "totalInclusiveValue": _s(po.total_inclusive_value), "remarks": po.remarks,
    }


# ── Reference data ────────────────────────────────────────────────────────


@api_view(["GET"])
def meta(request):
    """Plants (and which the caller may receive at), reasons, tax types,
    GST slabs - everything the form's dropdowns need."""
    user = request.user
    plants = []
    for p in Plant.objects.all():
        can_read = user_can_access_plant(user, p.code)
        can_receive = can_read and getattr(user, "role", "") in ("admin", "editor") and user_can_edit_plant(user, p.code)
        plants.append({**_plant(p), "stateCode": p.state_code, "canRead": can_read, "canReceive": can_receive})
    reasons = [{"code": r.code, "kind": r.kind, "label": r.label, "closesLine": r.closes_line, "noteRequired": r.note_required}
               for r in MirReasonCode.objects.filter(is_active=True)]
    return Response({
        "plants": plants, "reasons": reasons,
        "taxTypes": [{"code": c, "label": rules.TaxType.LABELS[c]} for c in rules.TaxType.ALL],
        "gstSlabs": [str(s) for s in rules.GST_SLABS],
        "categories": [{"name": c, "subcategories": subs} for c, subs in mir_service.category_options().items()],
        "invoiceRoundingTolerance": str(rules.INVOICE_ROUNDING_TOLERANCE),
        "today": _d(timezone.localdate()),
    })


# ── Finding a PO (cross-plant by the owner's rule) ────────────────────────


@api_view(["GET"])
@permission_classes([IsEditor])
def open_pos(request):
    """Open POs at every plant whose PO number contains ?q=."""
    results = []
    for po in mir_service.search_open_pos(request.query_params.get("q", "")):
        lines = mir_service.po_lines_with_state(po)
        open_lines = [1 for _line, st in lines if st["receivable"]]
        results.append({**_po_summary(po), "openLines": len(open_lines), "totalLines": len(lines)})
    return Response({"purchaseOrders": results})


@api_view(["GET"])
@permission_classes([IsEditor])
def purchase_order(request, po_id):
    po = get_object_or_404(PurchaseOrder.objects.select_related("plant", "vendor"), pk=po_id)
    lines = mir_service.po_lines_with_state(po)
    suggested = mir_service.suggested_categories([line for line, _st in lines])
    return Response({**_po_summary(po), **_po_header(po), "isActive": po.is_active,
                     "lines": [_po_line(line, st, suggested[line.id]) for line, st in lines]})


@api_view(["GET"])
@permission_classes([IsEditor])
def vendors(request):
    """Vendor picker, for a PO that names no vendor."""
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"vendors": []})
    qs = Vendor.objects.filter(Q(name__icontains=q) | Q(gstin__iexact=q) | Q(vendor_code=q)).order_by("name")[:20]
    return Response({"vendors": [_vendor(v) for v in qs]})


# ── Preview and post ──────────────────────────────────────────────────────


def _preview_payload(result):
    lines = []
    for ln in result["lines"]:
        a = ln["amounts"] or {}
        lines.append({"index": ln["index"], "poLineId": ln["po_line"].id, "openQty": _s(ln["state"]["open_qty"]),
                      **{k: _s(a.get(k)) for k in ("gross", "taxable", "igst", "cgst", "sgst", "total")}})
    return {
        "ok": result["ok"], "errors": result["errors"],
        "mismatches": [{"line": m["line"], "kind": m["kind"], "expected": _s(m["expected"]), "actual": _s(m["actual"]),
                        "expectedText": m.get("expected_text"), "actualText": m.get("actual_text"),
                        "differencePct": _s(m["difference_pct"]), "reason": m["reason"].code if m["reason"] else None}
                       for m in result["mismatches"]],
        "lines": lines, "vendor": _vendor(result["vendor"]), "taxType": result["tax_type"],
        "taxTypeExpected": result["tax_type_expected"], "computedTotal": _s(result["computed_total"]),
        "invoiceTotal": _s(result["invoice_total"]), "notices": result["notices"],
    }


def _receiving_plant_allowed(request):
    code = (request.data or {}).get("plant")
    return bool(code) and user_can_edit_plant(request.user, code)


@api_view(["POST"])
@permission_classes([IsEditor])
def preview(request):
    """Checks and prices the form as it stands; saves nothing."""
    if not _receiving_plant_allowed(request):
        return _forbidden("You cannot enter MIRs for that plant.")
    return Response(_preview_payload(mir_service.evaluate(request.data)))


@api_view(["POST"])
@permission_classes([IsEditor])
def post_entry(request):
    if not _receiving_plant_allowed(request):
        return _forbidden("You cannot enter MIRs for that plant.")
    try:
        mir = mir_service.post_mir(request.data, request.user)
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mir_detail(mir), status=http.HTTP_201_CREATED)


# ── Register ──────────────────────────────────────────────────────────────


def _mir_row(mir, total):
    return {
        "id": mir.id, "mirNo": mir.mir_no, "mirDate": _d(mir.mir_date), "plant": _plant(mir.plant),
        "vendor": _vendor(mir.vendor), "invoiceNo": mir.invoice_no, "invoiceDate": _d(mir.invoice_date),
        "invoiceTotal": _s(mir.invoice_total), "computedTotal": _s(total), "status": mir.status,
        "createdBy": mir.created_by_email, "createdAt": mir.created_at.isoformat(),
    }


def _mir_detail(mir):
    mir = Mir.objects.select_related("plant", "vendor").get(pk=mir.pk)
    lines = list(mir.lines.select_related("po_line__purchase_order__plant").order_by("line_no"))
    total = sum((ln.line_total for ln in lines), Decimal("0")) + mir.tcs_amount
    return {
        **_mir_row(mir, total), "taxType": mir.tax_type, "taxTypeExpected": mir.tax_type_expected,
        "tcsAmount": _s(mir.tcs_amount), "challanNo": mir.challan_no, "lrNo": mir.lr_no, "vehicleNo": mir.vehicle_no,
        "ewayBillNo": mir.eway_bill_no, "gateEntryNo": mir.gate_entry_no, "weighbridgeSlipNo": mir.weighbridge_slip_no,
        "sapGrnNumber": mir.sap_grn_number, "remarks": mir.remarks, "cancelledBy": mir.cancelled_by_email, "cancelledAt": mir.cancelled_at.isoformat() if mir.cancelled_at else None,
        "cancelReason": mir.cancel_reason,
        "lines": [{
            "lineNo": ln.line_no, "poNumber": ln.po_line.purchase_order.po_number, "poPlant": ln.po_line.purchase_order.plant.code,
            "poLineNo": ln.po_line.line_no, "description": ln.po_line.description, "itemCode": ln.po_line.item_code,
            "uom": ln.po_line.uom, "qtyReceived": _s(ln.qty_received), "qtyRejected": _s(ln.qty_rejected),
            "openQtyBefore": _s(ln.open_qty_before), "rate": _s(ln.rate), "poRate": _s(ln.po_rate), "discount": _s(ln.discount),
            "otherCharges": _s(ln.other_charges), "gstRate": _s(ln.gst_rate), "taxable": _s(ln.taxable), "igst": _s(ln.igst),
            "cgst": _s(ln.cgst), "sgst": _s(ln.sgst), "lineTotal": _s(ln.line_total), "rolls": ln.rolls,
            "batchNo": ln.batch_no, "deptUse": ln.dept_use, "remarks": ln.remarks,
            "materialCategory": ln.material_category, "materialSubcategory": ln.material_subcategory,
            "currency": ln.po_line.purchase_order.currency,
        } for ln in lines],
        "mismatches": [_mismatch(m) for m in mir.mismatches.select_related("reason", "mir_line").all()],
    }


def _mismatch(m):
    return {
        "id": m.id, "kind": m.kind, "kindLabel": m.get_kind_display(), "lineNo": m.mir_line.line_no if m.mir_line else None,
        "expected": _s(m.expected), "actual": _s(m.actual), "differencePct": _s(m.difference_pct),
        "reason": m.reason.code, "reasonLabel": m.reason.label, "note": m.note, "status": m.status,
        "resolvedBy": m.resolved_by_email, "resolvedAt": m.resolved_at.isoformat() if m.resolved_at else None,
        "resolutionNote": m.resolution_note,
    }


def _readable_plants(user):
    return [p.code for p in Plant.objects.all() if user_can_access_plant(user, p.code)]


@api_view(["GET"])
def entries(request):
    """The MIR register, newest first, for the plants the caller may read.
    Filters: ?plant= ?status= ?q= (MIR, invoice or vendor) ?from= ?to=."""
    plants = _readable_plants(request.user)
    wanted = request.query_params.get("plant")
    if wanted:
        plants = [p for p in plants if p == wanted]
    qs = Mir.objects.filter(plant__code__in=plants).select_related("plant", "vendor")
    if request.query_params.get("status") in (Mir.Status.POSTED, Mir.Status.CANCELLED):
        qs = qs.filter(status=request.query_params["status"])
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(mir_no__icontains=q) | Q(invoice_no__icontains=q) | Q(vendor__name__icontains=q)
                       | Q(lines__po_line__purchase_order__po_number__icontains=q)).distinct()
    for key, lookup in (("from", "mir_date__gte"), ("to", "mir_date__lte")):
        if request.query_params.get(key):
            qs = qs.filter(**{lookup: request.query_params[key]})
    # Explicit order: Django ignores Meta.ordering on an aggregate (GROUP BY)
    # query, so without it the register came back oldest first.
    qs = qs.annotate(lines_total=Sum("lines__line_total")).order_by("-mir_date", "-id")[:500]
    return Response({"entries": [_mir_row(m, (m.lines_total or Decimal("0")) + m.tcs_amount) for m in qs]})


@api_view(["GET"])
def entry(request, mir_id):
    mir = get_object_or_404(Mir, pk=mir_id)
    if not user_can_access_plant(request.user, mir.plant.code):
        return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)
    return Response(_mir_detail(mir))


@api_view(["POST"])
@permission_classes([IsEditor])
def cancel_entry(request, mir_id):
    mir = get_object_or_404(Mir, pk=mir_id)
    if not user_can_edit_plant(request.user, mir.plant.code):
        return _forbidden()
    try:
        mir_service.cancel_mir(mir, request.user, (request.data or {}).get("reason"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mir_detail(mir))


# ── Mismatches ────────────────────────────────────────────────────────────


@api_view(["GET"])
def mismatches(request):
    """Mismatches of MIRs at the plants the caller may read; ?status=OPEN by
    default (RESOLVED / VOID / ALL), ?plant= to narrow."""
    plants = _readable_plants(request.user)
    if request.query_params.get("plant"):
        plants = [p for p in plants if p == request.query_params["plant"]]
    qs = MirMismatch.objects.filter(mir__plant__code__in=plants).select_related(
        "mir__plant", "mir__vendor", "reason", "mir_line__po_line__purchase_order")
    wanted = request.query_params.get("status", "OPEN")
    if wanted != "ALL":
        qs = qs.filter(status=wanted)
    out = []
    for m in qs.order_by("-mir__mir_date", "-id")[:500]:
        line = m.mir_line
        out.append({**_mismatch(m), "mirId": m.mir_id, "mirNo": m.mir.mir_no, "mirDate": _d(m.mir.mir_date),
                    "plant": _plant(m.mir.plant), "vendor": _vendor(m.mir.vendor), "invoiceNo": m.mir.invoice_no,
                    "poNumber": line.po_line.purchase_order.po_number if line else None,
                    "description": line.po_line.description if line else None})
    return Response({"mismatches": out})


@api_view(["POST"])
@permission_classes([IsEditor])
def resolve(request, mismatch_id):
    mm = get_object_or_404(MirMismatch.objects.select_related("mir__plant", "mir_line__po_line__purchase_order__plant"), pk=mismatch_id)
    po_plant = mm.mir_line.po_line.purchase_order.plant.code if mm.mir_line else None
    if not (user_can_edit_plant(request.user, mm.mir.plant.code) or (po_plant and user_can_edit_plant(request.user, po_plant))):
        return _forbidden()
    try:
        mm = mir_service.resolve_mismatch(mm, request.user, (request.data or {}).get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mismatch(mm))


# ── PO line upkeep (purchase managers) ────────────────────────────────────


def _line_for_edit(request, line_id):
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("purchase_order__plant"), pk=line_id)
    return line, user_can_edit_plant(request.user, line.purchase_order.plant.code)


def _line_response(line):
    line = PurchaseOrderLine.objects.select_related("purchase_order__plant").get(pk=line.pk)
    state = mir_service.line_state(line, mir_service.accepted_by_line([line.id]).get(line.id, Decimal("0")))
    return Response(_po_line(line, state))


@api_view(["POST"])
@permission_classes([IsEditor])
def close_line(request, line_id):
    line, allowed = _line_for_edit(request, line_id)
    if not allowed:
        return _forbidden()
    try:
        mir_service.close_po_line(line, request.user, (request.data or {}).get("reason"), (request.data or {}).get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return _line_response(line)


@api_view(["POST"])
@permission_classes([IsEditor])
def reopen_line(request, line_id):
    line, allowed = _line_for_edit(request, line_id)
    if not allowed:
        return _forbidden()
    try:
        mir_service.reopen_po_line(line)
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return _line_response(line)


@api_view(["POST"])
@permission_classes([IsEditor])
def review_line(request, line_id):
    line, allowed = _line_for_edit(request, line_id)
    if not allowed:
        return _forbidden()
    try:
        mir_service.clear_line_review(line, request.user, (request.data or {}).get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return _line_response(line)
