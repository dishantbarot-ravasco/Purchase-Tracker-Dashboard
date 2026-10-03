"""
/api/mir/... - MIR entry against the normalized POs (2026-09-28).
The rules live in apps/services/mir_service.py; this file only gates, parses
and serializes.

Access (apps/api/permissions.py):
  - Reading MIRs and mismatches: Perm.MIR_ENTRY, PO_UPLOAD or RM_STORE (the
    RM store issues against a MIR), filtered to the account's plants.
  - Entering, previewing, editing and cancelling a MIR, and a material's
    category: Perm.MIR_ENTRY, at the RECEIVING plant.
  - Finding a PO to receive against (open-pos, purchase-orders/<id>):
    Perm.MIR_ENTRY or PO_UPLOAD, at the account's own plants only. A PO
    belongs to the plant on its billing address and only that plant may
    receive against it (project owner, 2026-10-03, replacing the 2026-09-28
    rule that any plant could receive any plant's PO).
  - Resolving a mismatch: Perm.MIR_ENTRY or PO_UPLOAD at the MIR's plant.
    Closing/reopening a PO line and clearing a line's review flag: the same
    permissions at the PO's plant.

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

from apps.api.permissions import HasAnyAccess, Perm, has_perm, requires, user_can_access_plant
from apps.api.routers.document_views import invoice_files, po_files
from apps.core.models import (
    Material,
    Mir,
    MirMismatch,
    MirReasonCode,
    Plant,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from apps.services import materials, mir_service, stock_service
from apps.services import procurement_rules as rules

# Who may read MIRs, and who may manage a PO's lines and mismatches.
MIR_READERS = (Perm.MIR_ENTRY, Perm.PO_UPLOAD, Perm.RM_STORE)
PO_MANAGERS = (Perm.MIR_ENTRY, Perm.PO_UPLOAD)


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


def _po_line(line, state):
    po = line.purchase_order
    material = line.material
    return {
        "id": line.id, "lineNo": line.line_no, "itemCode": line.item_code, "description": line.description,
        "hsn": line.hsn, "uom": line.uom, "uomKnown": line.uom in rules.KNOWN_UOMS, "qtyOrdered": _s(line.qty_ordered),
        "rate": _s(line.rate), "deliveryDate": _d(line.delivery_date), "accepted": _s(state["accepted"]),
        "openQty": _s(state["open_qty"]), "status": state["status"], "receivable": state["receivable"],
        "blockedReason": state["blocked_reason"], "needsReview": line.needs_review, "reviewNote": line.review_note,
        "closeNote": line.close_note, "closed": line.closed_at is not None,
        "closedAt": line.closed_at.isoformat() if line.closed_at else None,
        "closedReason": line.closed_reason.label if line.closed_reason_id else "",
        "closedByMir": line.closed_by_mir_line_id is not None, "poId": po.id, "poNumber": po.po_number, "plant": po.plant.code,
        "currency": po.currency, "poGstRate": _s(rules.po_gst_rate(po.total_value, po.total_inclusive_value)),
        # The material master's category, when the material is filed; the
        # form then shows it read-only (apps/services/materials.py).
        "materialId": material.id if material else None,
        "materialCategory": material.category if material else "",
        "materialSubcategory": material.subcategory if material else "",
    }


def _po_summary(po):
    return {
        "id": po.id, "poNumber": po.po_number, "poDate": _d(po.po_date), "plant": _plant(po.plant),
        "billingPlant": _plant(po.billing_plant) if po.billing_plant_id else None,
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
@permission_classes([HasAnyAccess])
def meta(request):
    """Plants (and which the caller may receive at), reasons, tax types,
    GST slabs - everything the form's dropdowns need."""
    user = request.user
    plants = []
    for p in Plant.objects.all():
        can_read = user_can_access_plant(user, p.code)
        can_receive = can_read and has_perm(user, Perm.MIR_ENTRY)
        plants.append({**_plant(p), "stateCode": p.state_code, "canRead": can_read, "canReceive": can_receive})
    reasons = [{"code": r.code, "kind": r.kind, "label": r.label, "closesLine": r.closes_line, "noteRequired": r.note_required}
               for r in MirReasonCode.objects.filter(is_active=True)]
    return Response({
        "plants": plants, "reasons": reasons,
        # What this account may do, for the pages that share this endpoint
        # (mir.html, po-files.html). The endpoints enforce it again.
        "can": {"mirEntry": has_perm(user, Perm.MIR_ENTRY), "poUpload": has_perm(user, Perm.PO_UPLOAD),
                "importDocs": has_perm(user, Perm.IMPORT_DOCS), "manageLines": has_perm(user, *PO_MANAGERS)},
        "taxTypes": [{"code": c, "label": rules.TaxType.LABELS[c]} for c in rules.TaxType.ALL],
        "gstSlabs": [str(s) for s in rules.GST_SLABS],
        "gstStates": [{"code": c, "name": n} for c, n in rules.GST_STATES.items()],
        "categories": [{"name": c, "subcategories": subs} for c, subs in mir_service.category_options().items()],
        "invoiceRoundingTolerance": str(rules.INVOICE_ROUNDING_TOLERANCE),
        "today": _d(timezone.localdate()),
    })


# ── Finding a PO (the account's own plants) ───────────────────────────────


@api_view(["GET"])
@permission_classes([requires(*PO_MANAGERS)])
def open_pos(request):
    """Open POs at the caller's plants whose PO number contains ?q=."""
    results = []
    plants = [p.code for p in Plant.objects.all() if user_can_access_plant(request.user, p.code)]
    for po in mir_service.search_open_pos(request.query_params.get("q", ""), plants):
        lines = mir_service.po_lines_with_state(po)
        open_lines = [1 for _line, st in lines if st["receivable"]]
        results.append({**_po_summary(po), "openLines": len(open_lines), "totalLines": len(lines)})
    return Response({"purchaseOrders": results})


@api_view(["GET"])
@permission_classes([requires(*PO_MANAGERS)])
def purchase_order(request, po_id):
    po = get_object_or_404(PurchaseOrder.objects.select_related("plant", "vendor", "billing_plant"), pk=po_id)
    # Another plant's PO answers 404, not 403, so its id does not even
    # confirm that it exists.
    if not user_can_access_plant(request.user, po.plant.code):
        return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)
    lines = mir_service.po_lines_with_state(po)
    # Short-close / reopen / confirm need PO_MANAGERS at the PO's plant
    # (close_line and friends check the same); the page offers the buttons
    # only when this is true.
    can_manage = has_perm(request.user, *PO_MANAGERS)
    # What does not add up on the PO as written - shown above its lines, never
    # blocking (procurement_rules.po_checks()).
    checks = rules.po_checks(
        [{"line_no": ln.line_no, "description": ln.description, "qty": ln.qty_ordered, "uom": ln.uom, "rate": ln.rate,
          "net_value": ln.net_value} for ln, _st in lines if ln.is_active],
        po.total_value, po.total_inclusive_value, po.vendor.gstin if po.vendor else "", po.po_date)
    return Response({**_po_summary(po), **_po_header(po), "isActive": po.is_active, "canManage": can_manage,
                     "poFiles": po_files(po), "checks": checks, "lines": [_po_line(line, st) for line, st in lines]})


@api_view(["GET"])
@permission_classes([requires(Perm.MIR_ENTRY)])
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
        "lines": lines, "vendor": _vendor(result["vendor"]), "vendorState": result["vendor_state"], "taxType": result["tax_type"],
        "taxTypeExpected": result["tax_type_expected"], "computedTotal": _s(result["computed_total"]),
        "invoiceTotal": _s(result["invoice_total"]), "notices": result["notices"],
    }


def _receiving_plant_allowed(request):
    code = (request.data or {}).get("plant")
    return bool(code) and user_can_access_plant(request.user, code)


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
def preview(request):
    """Checks and prices the form as it stands; saves nothing."""
    if not _receiving_plant_allowed(request):
        return _forbidden("You cannot enter MIRs for that plant.")
    return Response(_preview_payload(mir_service.evaluate(request.data)))


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
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
    lines = list(mir.lines.select_related("po_line__purchase_order__plant", "po_line__material").order_by("line_no"))
    window = mir_service.edit_window(mir)
    total = sum((ln.line_total for ln in lines), Decimal("0")) + mir.tcs_amount
    # What each line put into the store, and how much of it is still there.
    stock = stock_service.mir_line_stock(mir)
    return {
        **_mir_row(mir, total), "taxType": mir.tax_type, "taxTypeExpected": mir.tax_type_expected,
        "vendorState": mir.vendor_state, "vendorStateName": rules.GST_STATES.get(mir.vendor_state, ""),
        "tcsAmount": _s(mir.tcs_amount), "challanNo": mir.challan_no, "lrNo": mir.lr_no, "vehicleNo": mir.vehicle_no,
        "ewayBillNo": mir.eway_bill_no, "gateEntryNo": mir.gate_entry_no, "weighbridgeSlipNo": mir.weighbridge_slip_no,
        "sapGrnNumber": mir.sap_grn_number, "remarks": mir.remarks, "cancelledBy": mir.cancelled_by_email, "cancelledAt": mir.cancelled_at.isoformat() if mir.cancelled_at else None,
        "cancelReason": mir.cancel_reason,
        "editUntil": _d(window["edit_until"]), "rejectUntil": _d(window["reject_until"]),
        "canEdit": window["can_edit"], "canEditGrn": window["can_edit_grn"], "canReject": window["can_reject"],
        "history": [{"field": c.field, "lineNo": c.mir_line.line_no if c.mir_line else None, "oldValue": c.old_value,
                     "newValue": c.new_value, "reason": c.reason, "by": c.changed_by_email, "at": c.changed_at.isoformat()}
                    for c in mir.changes.select_related("mir_line").all()],
        "lines": [{
            "lineNo": ln.line_no, "poNumber": ln.po_line.purchase_order.po_number, "poPlant": ln.po_line.purchase_order.plant.code,
            "poLineNo": ln.po_line.line_no, "description": ln.description or ln.po_line.description, "itemCode": ln.po_line.item_code,
            "uom": ln.uom or ln.po_line.uom, "qtyReceived": _s(ln.qty_received), "qtyRejected": _s(ln.qty_rejected),
            "openQtyBefore": _s(ln.open_qty_before), "rate": _s(ln.rate), "poRate": _s(ln.po_rate), "discount": _s(ln.discount),
            "otherCharges": _s(ln.other_charges), "gstRate": _s(ln.gst_rate), "taxable": _s(ln.taxable), "igst": _s(ln.igst),
            "cgst": _s(ln.cgst), "sgst": _s(ln.sgst), "lineTotal": _s(ln.line_total), "rolls": ln.rolls,
            "batchNo": ln.batch_no, "deptUse": ln.dept_use, "remarks": ln.remarks,
            "materialCategory": ln.po_line.material.category if ln.po_line.material else "",
            "materialSubcategory": ln.po_line.material.subcategory if ln.po_line.material else "",
            "currency": ln.po_line.purchase_order.currency,
            "stock": ({"uom": stock[ln.id]["uom"], "in": _s(stock[ln.id]["in"]), "balance": _s(stock[ln.id]["balance"]),
                       "stocked": stock[ln.id]["stocked"]} if ln.id in stock else None),
        } for ln in lines],
        "mismatches": [_mismatch(m) for m in mir.mismatches.select_related("reason", "mir_line").all()],
        "invoiceFiles": invoice_files(mir),
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
@permission_classes([requires(*MIR_READERS)])
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
@permission_classes([requires(*MIR_READERS)])
def entry(request, mir_id):
    mir = get_object_or_404(Mir, pk=mir_id)
    if not user_can_access_plant(request.user, mir.plant.code):
        return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)
    return Response(_mir_detail(mir))


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
def cancel_entry(request, mir_id):
    mir = get_object_or_404(Mir, pk=mir_id)
    if not user_can_access_plant(request.user, mir.plant.code):
        return _forbidden()
    try:
        mir_service.cancel_mir(mir, request.user, (request.data or {}).get("reason"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mir_detail(mir))


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
def material_category(request, material_id):
    """Correct a material's category. Body: {"category", "subcategory",
    "reason"}. Materials are company-wide, so anyone with MIR entry may; the
    change is logged with its reason (materials.change_category())."""
    material = get_object_or_404(Material, pk=material_id)
    data = request.data or {}
    try:
        materials.change_category(material, data.get("category"), data.get("subcategory"), data.get("reason"), request.user)
    except materials.MaterialError as exc:
        return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
    material.refresh_from_db()
    return Response({"id": material.id, "category": material.category, "subcategory": material.subcategory,
                     "history": [{"field": c.field, "oldValue": c.old_value, "newValue": c.new_value, "reason": c.reason,
                                  "by": c.changed_by_email, "at": c.changed_at.isoformat()} for c in material.changes.all()]})


def _editable_mir(request, mir_id):
    mir = get_object_or_404(Mir.objects.select_related("plant"), pk=mir_id)
    return mir, user_can_access_plant(request.user, mir.plant.code)


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
def edit_entry(request, mir_id):
    """Change a posted MIR's paperwork, within mir_service's limits.
    Body: {"header": {field: value}, "lines": {lineNo: {field: value}},
    "reason": "..."}. Figures are never editable here."""
    mir, allowed = _editable_mir(request, mir_id)
    if not allowed:
        return _forbidden()
    data = request.data or {}
    header, lines = data.get("header") or {}, data.get("lines") or {}
    if not isinstance(header, dict) or not isinstance(lines, dict) or not all(isinstance(v, dict) for v in lines.values()):
        return Response({"error": "header and lines must be objects."}, status=http.HTTP_400_BAD_REQUEST)
    try:
        mir_service.edit_mir(mir, request.user, header, lines, data.get("reason"))
    except (mir_service.MirValidationError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return Response({"error": "A line number is not a number."}, status=http.HTTP_400_BAD_REQUEST)
        return _bad(exc)
    return Response(_mir_detail(mir))


@api_view(["POST"])
@permission_classes([requires(Perm.MIR_ENTRY)])
def reject_line(request, mir_id, line_no):
    """Record a rejection found after posting. Body: {"qtyRejected": new
    total, "reason": code, "note": "..."}."""
    mir, allowed = _editable_mir(request, mir_id)
    if not allowed:
        return _forbidden()
    line = get_object_or_404(mir.lines, line_no=line_no)
    data = request.data or {}
    try:
        mir_service.record_rejection(line, request.user, data.get("qtyRejected"), data.get("reason"), data.get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mir_detail(mir))


# ── Mismatches ────────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([requires(*MIR_READERS)])
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
                    "description": (line.description or line.po_line.description) if line else None})
    return Response({"mismatches": out})


@api_view(["POST"])
@permission_classes([requires(*PO_MANAGERS)])
def resolve(request, mismatch_id):
    mm = get_object_or_404(MirMismatch.objects.select_related("mir__plant"), pk=mismatch_id)
    if not user_can_access_plant(request.user, mm.mir.plant.code):
        return _forbidden()
    try:
        mm = mir_service.resolve_mismatch(mm, request.user, (request.data or {}).get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return Response(_mismatch(mm))


# ── PO line upkeep (purchase managers) ────────────────────────────────────


def _line_for_edit(request, line_id):
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("purchase_order__plant"), pk=line_id)
    return line, user_can_access_plant(request.user, line.purchase_order.plant.code)


def _line_response(line):
    line = PurchaseOrderLine.objects.select_related("purchase_order__plant").get(pk=line.pk)
    state = mir_service.line_state(line, mir_service.accepted_by_line([line.id]).get(line.id, Decimal("0")))
    return Response(_po_line(line, state))


@api_view(["POST"])
@permission_classes([requires(*PO_MANAGERS)])
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
@permission_classes([requires(*PO_MANAGERS)])
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
@permission_classes([requires(*PO_MANAGERS)])
def review_line(request, line_id):
    line, allowed = _line_for_edit(request, line_id)
    if not allowed:
        return _forbidden()
    try:
        mir_service.clear_line_review(line, request.user, (request.data or {}).get("note"))
    except mir_service.MirValidationError as exc:
        return _bad(exc)
    return _line_response(line)
