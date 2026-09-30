"""
/api/stock/... - RM stock entry (2026-09-29). The rules live in
apps/services/stock_service.py; this file only gates, parses and serializes.

Access:
  - Reading stock, lots, the ledger and vouchers: any role, filtered to the
    plants the account may read (user_can_access_plant()).
  - Issuing, returning, adjusting, cancelling and a material's settings:
    Editor or Admin, allowed to edit THAT plant (user_can_edit_plant()).
  - Approving or turning down an adjustment: Admin, at that plant. An
    editor's adjustment waits for this; an admin's posts at once.

Stock is always the plant's own: unlike MIR entry's PO lookups, nothing here
reads another plant's store. Quantities and money travel as strings, never
floats, so the screen shows exactly what was stored.
"""

from decimal import Decimal

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, user_can_access_plant, user_can_edit_plant
from apps.core.models import Material, Plant, StockReasonCode, StockVoucher
from apps.services import procurement_rules as prules
from apps.services import stock_rules, stock_service


def _s(value):
    return None if value is None else str(value)


def _d(value):
    return None if value is None else value.isoformat()


def _bad(exc: stock_service.StockValidationError):
    return Response({"error": exc.errors[0]["message"], "errors": exc.errors}, status=http.HTTP_400_BAD_REQUEST)


def _forbidden(message="You are not allowed to do this for that plant."):
    return Response({"error": message}, status=http.HTTP_403_FORBIDDEN)


def _not_found():
    return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)


def _plant(p):
    return {"code": p.code, "name": p.name} if p else None


def _material(m):
    return {"id": m.id, "name": m.name, "category": m.category, "subcategory": m.subcategory}


def _is_writer(user):
    return getattr(user, "role", "") in ("admin", "editor")


def _readable_plants(user):
    return [p.code for p in Plant.objects.all() if user_can_access_plant(user, p.code)]


# ── Reference data ────────────────────────────────────────────────────────


@api_view(["GET"])
def meta(request):
    """Plants (and what the caller may do at each), reasons, units, today,
    the backdating window, departments used before, and how many
    adjustments wait for approval."""
    user = request.user
    plants, departments, readable = [], {}, []
    for p in Plant.objects.all():
        can_read = user_can_access_plant(user, p.code)
        can_write = can_read and _is_writer(user) and user_can_edit_plant(user, p.code)
        plants.append({**_plant(p), "canRead": can_read, "canWrite": can_write,
                       "canApprove": can_write and getattr(user, "role", "") == "admin"})
        if can_read:
            readable.append(p.code)
            departments[p.code] = stock_service.departments(p)
    pending = StockVoucher.objects.filter(plant__code__in=readable, status="PENDING").count()
    return Response({
        "plants": plants,
        "reasons": [{"code": r.code, "kind": r.kind, "label": r.label, "noteRequired": r.note_required}
                    for r in StockReasonCode.objects.filter(is_active=True)],
        "units": list(prules.KNOWN_UOMS),
        "today": _d(timezone.localdate()), "backdateDays": stock_service.BACKDATE_DAYS,
        "departments": departments, "pendingApprovals": pending, "isAdmin": getattr(user, "role", "") == "admin",
    })


# ── Stock on hand ─────────────────────────────────────────────────────────


def _row(r):
    return {
        "plant": _plant(r["plant"]), "material": _material(r["material"]), "uom": r["uom"], "qty": _s(r["qty"]),
        "value": _s(r["value"]), "otherCurrencyQty": _s(r["other_currency_qty"]), "openLots": r["open_lots"],
        "lots": r["lots"], "lastReceived": _d(r["last_received"]), "oldestHeld": _d(r["oldest_held"]),
        "isStocked": r["is_stocked"], "minLevel": _s(r["min_level"]), "belowMin": r["below_min"],
    }


@api_view(["GET"])
def balances(request):
    """Stock on hand per plant, material and stock unit, for the plants the
    caller may read. ?plant= narrows; ?q= searches material names."""
    plants = _readable_plants(request.user)
    if request.query_params.get("plant"):
        plants = [p for p in plants if p == request.query_params["plant"]]
    rows = stock_service.stock_rows(plants)
    q = (request.query_params.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["material"].name.lower()]
    return Response({"rows": [_row(r) for r in rows]})


def _lot(lot, b):
    source_doc = lot.mir_line.mir.mir_no if lot.source == "MIR" else lot.voucher_line.voucher.voucher_no
    return {
        "id": lot.id, "source": lot.source, "doc": source_doc,
        "mirId": lot.mir_line.mir_id if lot.source == "MIR" else None,
        "voucherId": lot.voucher_line.voucher_id if lot.source != "MIR" else None,
        "receivedDate": _d(lot.received_date), "vendor": lot.vendor.name if lot.vendor else "",
        "billToPlant": _plant(lot.bill_to_plant), "in": _s(b["in"]), "drawn": _s(b["drawn"]), "returned": _s(b["returned"]),
        "balance": _s(b["balance"]), "rate": _s(lot.rate), "currency": lot.currency,
        "value": _s(stock_rules.value(b["balance"], lot.rate)) if b["balance"] > 0 else "0.00",
        "stocked": lot.stocked, "batchNo": lot.batch_no, "factor": _s(lot.factor),
    }


@api_view(["GET"])
def material_stock(request, material_id):
    """One material's lots at one plant (?plant=, ?uom=) and its ledger."""
    material = get_object_or_404(Material, pk=material_id)
    plant = Plant.objects.filter(code=request.query_params.get("plant")).first()
    if plant is None or not user_can_access_plant(request.user, plant.code):
        return _not_found()
    uom = (request.query_params.get("uom") or "").upper()
    detail = stock_service.material_detail(plant, material, uom)
    setting = detail["setting"]
    return Response({
        "plant": _plant(plant), "material": _material(material), "uom": uom,
        "canWrite": _is_writer(request.user) and user_can_edit_plant(request.user, plant.code),
        "setting": {"isStocked": setting.is_stocked if setting else True,
                    "minLevel": _s(setting.min_level) if setting else None,
                    "minLevelUom": setting.min_level_uom if setting else "",
                    "updatedBy": setting.updated_by_email if setting else "",
                    "updatedAt": setting.updated_at.isoformat() if setting else None},
        "lots": [_lot(lot, b) for lot, b in detail["lots"]],
        "ledger": [{"date": _d(e["date"]), "doc": e["doc"], "kind": e["kind"], "qty": _s(e["qty"]), "detail": e["detail"],
                    "counts": e["counts"], "note": e["note"], "balance": _s(e["balance"]), "voucherId": e.get("voucherId")}
                   for e in detail["ledger"]],
    })


@api_view(["POST"])
@permission_classes([IsEditor])
def settings(request):
    """Whether a plant stocks a material, and its minimum level. Body:
    {"plant", "materialId", "isStocked", "minLevel", "minLevelUom"}."""
    data = request.data or {}
    plant = Plant.objects.filter(code=data.get("plant")).first()
    if plant is None or not user_can_edit_plant(request.user, plant.code):
        return _forbidden()
    material = get_object_or_404(Material, pk=data.get("materialId"))
    try:
        stock_service.update_setting(plant, material, request.user, is_stocked=data.get("isStocked") is not False,
                                     min_level=data.get("minLevel"), min_level_uom=data.get("minLevelUom"))
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsEditor])
def material_search(request):
    """The company-wide material master, for adding stock of a material the
    plant holds none of yet (an opening balance). Not plant data."""
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"materials": []})
    qs = Material.objects.filter(Q(name__icontains=q) | Q(item_code=q)).order_by("name")[:25]
    return Response({"materials": [{**_material(m), "uom": stock_rules.stock_unit(m.uom)[0]} for m in qs]})


# ── Vouchers ──────────────────────────────────────────────────────────────


def _draws(draws):
    return [{"lotId": lot.id, "doc": lot.mir_line.mir.mir_no if lot.source == "MIR" else lot.voucher_line.voucher.voucher_no,
             "receivedDate": _d(lot.received_date), "qty": _s(q), "rate": _s(lot.rate)} for lot, q in draws]


def _preview_payload(result):
    return {
        "ok": result["ok"], "errors": result["errors"], "notices": result["notices"],
        "lines": [{"index": ln["index"], "materialId": ln["material"].id if ln.get("material") else None, "uom": ln.get("uom"),
                   "qty": _s(ln.get("qty")), "direction": ln.get("direction"), "value": _s(ln.get("value")),
                   "book": _s(ln.get("book")), "counted": _s(ln.get("counted")), "rate": _s(ln.get("rate")),
                   "draws": _draws(ln.get("draws") or [])} for ln in result["lines"]],
    }


def _plant_writable(request):
    code = (request.data or {}).get("plant")
    return bool(code) and user_can_edit_plant(request.user, code)


@api_view(["POST"])
@permission_classes([IsEditor])
def preview(request):
    """Checks and values the form as it stands; saves nothing."""
    if not _plant_writable(request):
        return _forbidden("You cannot enter stock for that plant.")
    return Response(_preview_payload(stock_service.evaluate(request.data or {})))


@api_view(["POST"])
@permission_classes([IsEditor])
def post_voucher(request):
    if not _plant_writable(request):
        return _forbidden("You cannot enter stock for that plant.")
    try:
        voucher = stock_service.post_voucher(request.data or {}, request.user)
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response(_voucher_detail(voucher, request.user), status=http.HTTP_201_CREATED)


def _line_value(line):
    if line.voucher.kind == "ADJUST" and line.direction > 0:
        return stock_rules.value(line.qty, line.rate)
    total = sum((stock_rules.value(a.qty, a.lot.rate) for a in line.allocations.all() if a.lot.currency == "INR"), Decimal("0.00"))
    return total * line.direction


def _voucher_row(v, lines=None):
    lines = list(v.lines.all()) if lines is None else lines
    return {
        "id": v.id, "voucherNo": v.voucher_no, "kind": v.kind, "kindLabel": v.get_kind_display(), "date": _d(v.voucher_date),
        "plant": _plant(v.plant), "status": v.status, "statusLabel": v.get_status_display(), "department": v.department,
        "issuedTo": v.issued_to, "reference": v.reference,
        "returnOf": {"id": v.return_of_id, "voucherNo": v.return_of.voucher_no} if v.return_of_id else None,
        "lineCount": len(lines), "materials": ", ".join(sorted({ln.material.name for ln in lines}))[:200],
        "createdBy": v.created_by_email, "createdAt": v.created_at.isoformat(),
    }


def _voucher_detail(v, user):
    v = StockVoucher.objects.select_related("plant", "return_of").get(pk=v.pk)
    lines = list(v.lines.select_related("material", "reason", "return_of_line")
                 .prefetch_related("allocations__lot__mir_line__mir", "allocations__lot__voucher_line__voucher").order_by("line_no"))
    can_write = _is_writer(user) and user_can_edit_plant(user, v.plant.code)
    returnable = []
    if v.kind == "ISSUE" and v.status == "POSTED":
        returnable = [{"lineId": r["line"].id, "lineNo": r["line"].line_no, "material": _material(r["line"].material),
                       "uom": r["line"].uom, "issued": _s(r["line"].qty), "returned": _s(r["returned"]), "stillOut": _s(r["still_out"])}
                      for r in stock_service.returnable_lines(v)]
    return {
        **_voucher_row(v, lines), "remarks": v.remarks,
        "decidedBy": v.decided_by_email, "decidedAt": v.decided_at.isoformat() if v.decided_at else None, "decisionNote": v.decision_note,
        "cancelledBy": v.cancelled_by_email, "cancelledAt": v.cancelled_at.isoformat() if v.cancelled_at else None,
        "cancelReason": v.cancel_reason,
        "returns": [{"id": r.id, "voucherNo": r.voucher_no, "status": r.status} for r in v.returns.all()],
        "canCancel": can_write and v.status in ("POSTED", "PENDING"),
        "canApprove": can_write and getattr(user, "role", "") == "admin" and v.status == "PENDING"
                      and v.created_by_id != getattr(user, "pk", None),
        "returnable": returnable,
        "lines": [{
            "lineNo": ln.line_no, "material": _material(ln.material), "uom": ln.uom, "qty": _s(ln.qty), "direction": ln.direction,
            "reason": ln.reason.label if ln.reason else "", "note": ln.note, "rate": _s(ln.rate),
            "counted": _s(ln.counted_qty), "book": _s(ln.book_qty), "value": _s(_line_value(ln)) if v.status != "PENDING" or ln.rate else None,
            "draws": [{"lotId": a.lot_id, "doc": a.lot.mir_line.mir.mir_no if a.lot.source == "MIR" else a.lot.voucher_line.voucher.voucher_no,
                       "receivedDate": _d(a.lot.received_date), "qty": _s(a.qty), "rate": _s(a.lot.rate)} for a in ln.allocations.all()],
        } for ln in lines],
    }


@api_view(["GET"])
def vouchers(request):
    """Issues, returns and adjustments, newest first, for the plants the
    caller may read. Filters: ?plant= ?kind= ?status= ?q= (number,
    department, person, material) ?from= ?to=."""
    plants = _readable_plants(request.user)
    if request.query_params.get("plant"):
        plants = [p for p in plants if p == request.query_params["plant"]]
    qs = StockVoucher.objects.filter(plant__code__in=plants).select_related("plant", "return_of")
    if request.query_params.get("kind") in StockVoucher.Kind.values:
        qs = qs.filter(kind=request.query_params["kind"])
    if request.query_params.get("status") in StockVoucher.Status.values:
        qs = qs.filter(status=request.query_params["status"])
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(voucher_no__icontains=q) | Q(department__icontains=q) | Q(issued_to__icontains=q)
                       | Q(reference__icontains=q) | Q(lines__material__name__icontains=q)).distinct()
    for key, lookup in (("from", "voucher_date__gte"), ("to", "voucher_date__lte")):
        if request.query_params.get(key):
            qs = qs.filter(**{lookup: request.query_params[key]})
    qs = qs.prefetch_related("lines__material").order_by("-voucher_date", "-id")[:500]
    return Response({"vouchers": [_voucher_row(v) for v in qs]})


@api_view(["GET"])
def voucher(request, voucher_id):
    v = get_object_or_404(StockVoucher.objects.select_related("plant"), pk=voucher_id)
    if not user_can_access_plant(request.user, v.plant.code):
        return _not_found()
    return Response(_voucher_detail(v, request.user))


def _writable_voucher(request, voucher_id):
    v = get_object_or_404(StockVoucher.objects.select_related("plant"), pk=voucher_id)
    return v, user_can_edit_plant(request.user, v.plant.code)


@api_view(["POST"])
@permission_classes([IsEditor])
def cancel_voucher(request, voucher_id):
    v, allowed = _writable_voucher(request, voucher_id)
    if not allowed:
        return _forbidden()
    try:
        stock_service.cancel_voucher(v, request.user, (request.data or {}).get("reason"))
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response(_voucher_detail(v, request.user))


@api_view(["POST"])
@permission_classes([IsAdmin])
def approve_voucher(request, voucher_id):
    v, allowed = _writable_voucher(request, voucher_id)
    if not allowed:
        return _forbidden()
    try:
        stock_service.approve_adjustment(v, request.user, (request.data or {}).get("note"))
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response(_voucher_detail(v, request.user))


@api_view(["POST"])
@permission_classes([IsAdmin])
def reject_voucher(request, voucher_id):
    v, allowed = _writable_voucher(request, voucher_id)
    if not allowed:
        return _forbidden()
    try:
        stock_service.reject_adjustment(v, request.user, (request.data or {}).get("note"))
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response(_voucher_detail(v, request.user))
