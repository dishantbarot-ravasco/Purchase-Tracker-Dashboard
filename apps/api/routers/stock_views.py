"""
/api/stock/... - the RM store. The rules live in
apps/services/stock_service.py; this file only gates, parses and serializes.

The page has three views, like MIR entry: Issue (pick a MIR, issue from it),
the RM register (one row per MIR receipt, like the store's Stock sheet, plus
the issue slips) and Open mismatches (stock differences waiting for an
admin).

Access (apps/api/permissions.py):
  - Everything on the page - reading receipts, the register, slips and
    differences; issuing, returning, recording a difference, cancelling and
    a material's settings: Perm.RM_STORE, at THAT plant
    (user_can_access_plant()); reads are filtered to the account's plants.
  - Approving or turning down a difference: Admin. A storekeeper's
    difference waits for this; an admin's posts at once.

Stock is always the plant's own: unlike MIR entry's PO lookups, nothing here
reads another plant's store. Quantities and money travel as strings, never
floats, so the screen shows exactly what was stored.
"""

import datetime
from decimal import Decimal

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import HasAnyAccess, IsAdmin, Perm, has_perm, is_admin, requires, user_can_access_plant
from apps.core.models import Material, Plant, StockLot, StockReasonCode, StockVoucher
from apps.services import materials, stock_rules, stock_service
from apps.services import procurement_rules as prules


def _s(value):
    return None if value is None else str(value)


def _n(value):
    """A factor without trailing zeros and never in exponent form ("1000",
    not "1E+3" or "1000.000000")."""
    return None if value is None else format(value.normalize(), "f")


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
    return has_perm(user, Perm.RM_STORE)


def _readable_plants(user, wanted=None):
    """Plant codes the caller may read, narrowed to `wanted` when given."""
    return [p.code for p in Plant.objects.all() if user_can_access_plant(user, p.code) and (not wanted or p.code == wanted)]


def _date(value, default):
    try:
        return datetime.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _receipt(lot, balance=None):
    """A MIR receipt as the page shows it: everything comes from the MIR."""
    mir_line = lot.mir_line if lot.source == "MIR" else None
    po = mir_line.po_line.purchase_order if mir_line else None
    return {
        "id": lot.id, "source": lot.source, "doc": stock_service.doc_of(lot),
        "mirId": mir_line.mir_id if mir_line else None, "mirNo": mir_line.mir.mir_no if mir_line else None,
        "lineNo": mir_line.line_no if mir_line else None, "invoiceNo": mir_line.mir.invoice_no if mir_line else "",
        "poNumber": po.po_number if po else "", "itemCode": mir_line.po_line.item_code if mir_line else "",
        "voucherId": lot.voucher_line.voucher_id if lot.source != "MIR" else None,
        "receivedDate": _d(lot.received_date), "plant": _plant(lot.plant), "material": _material(lot.material), "uom": lot.uom,
        "vendor": lot.vendor.name if lot.vendor else "", "billToPlant": _plant(lot.bill_to_plant),
        "rate": _s(lot.rate), "currency": lot.currency, "stocked": lot.stocked, "batchNo": lot.batch_no,
        # The MIR line's own unit and how many stock units one of it is, so
        # a converted receipt can show "2 MT" beside "2,000 KG".
        "mirUom": (mir_line.uom or mir_line.po_line.uom) if mir_line else lot.uom, "factor": _n(lot.factor),
        "balance": _s(balance),
        "days": (timezone.localdate() - lot.received_date).days,
    }


# ── Reference data ────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([HasAnyAccess])
def meta(request):
    """Plants (and what the caller may do at each), reasons, today, the
    backdating window, departments used before, categories held, and how
    many differences wait for approval."""
    user = request.user
    plants, departments, readable = [], {}, []
    for p in Plant.objects.all():
        can_read = user_can_access_plant(user, p.code)
        can_write = can_read and _is_writer(user)
        plants.append({**_plant(p), "canRead": can_read, "canWrite": can_write,
                       "canApprove": can_write and is_admin(user)})
        if can_read:
            readable.append(p.code)
            departments[p.code] = stock_service.departments(p)
    pending = StockVoucher.objects.filter(plant__code__in=readable, status="PENDING").count()
    categories = sorted({c for c in StockLot.objects.filter(plant__code__in=readable)
                         .values_list("material__category", flat=True).distinct() if c})
    return Response({
        "plants": plants,
        "reasons": [{"code": r.code, "kind": r.kind, "label": r.label, "noteRequired": r.note_required}
                    # Stock comes in only through a MIR now: no opening-balance reason.
                    for r in StockReasonCode.objects.filter(is_active=True).exclude(code="OPENING_BALANCE")],
        "today": _d(timezone.localdate()), "backdateDays": stock_service.BACKDATE_DAYS,
        "departments": departments, "pendingApprovals": pending, "isAdmin": is_admin(user),
        "categories": categories,
        "baseUnits": [{"code": c, "label": label} for c, label in Material.BaseUnit.choices],
        # Units a pack factor may be entered for: every known unit nothing exact converts.
        "packUnits": [u for u in prules.KNOWN_UOMS if not stock_rules.base_of(u)],
        # Units that convert exactly: {unit: [base unit, factor]}.
        "exactUnits": {u: [b, _n(f)] for u, (b, f) in stock_rules.EXACT.items()},
    })


# ── Picking a MIR ─────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def receipts(request):
    """MIR receipts with stock left, for the issue form and the difference
    form: at ?plant= (404 if the caller may not read it), or at every plant
    the caller may read when it is left out; ?q= MIR, material, vendor,
    invoice, PO or item code; ?all=1 lists every open one (up to 500)."""
    wanted = request.query_params.get("plant")
    plants = [p.code for p in Plant.objects.all() if user_can_access_plant(request.user, p.code) and (not wanted or p.code == wanted)]
    if wanted and not plants:
        return _not_found()
    rows = stock_service.receipts_for_issue(plants, request.query_params.get("q", ""),
                                            limit=500 if request.query_params.get("all") == "1" else 80)
    return Response({"receipts": [_receipt(lot, bal) for lot, bal in rows]})


# ── The RM register ───────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def register(request):
    """One row per MIR receipt for a period, like the store's Stock sheet.
    ?plant= ?from= ?to= (default: this month to today) ?q= ?category=
    ?all=1 keeps receipts that held nothing all period."""
    plants = _readable_plants(request.user, request.query_params.get("plant"))
    today = timezone.localdate()
    date_to = _date(request.query_params.get("to"), today)
    date_from = _date(request.query_params.get("from"), date_to.replace(day=1))
    if date_from > date_to:
        return Response({"error": "The From date is after the To date."}, status=http.HTTP_400_BAD_REQUEST)
    rows = stock_service.register_rows(plants, date_from, date_to, q=request.query_params.get("q", ""),
                                       category=request.query_params.get("category", ""),
                                       include_empty=request.query_params.get("all") == "1")
    return Response({
        "from": _d(date_from), "to": _d(date_to),
        "rows": [{**_receipt(r["lot"]), "opening": _s(r["opening"]), "received": _s(r["received"]), "issued": _s(r["issued"]),
                  "returned": _s(r["returned"]), "adjusted": _s(r["adjusted"]), "closing": _s(r["closing"]),
                  "value": _s(r["value"]), "days": r["days"], "lastIssued": _d(r["last_issued"])} for r in rows],
    })


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def receipt(request, lot_id):
    """One MIR receipt: where it came from, its balances, every movement with
    the running balance, and the plant's setting for the material."""
    lot = get_object_or_404(StockLot.objects.select_related(*stock_service.LOT_RELATED), pk=lot_id)
    if not user_can_access_plant(request.user, lot.plant.code):
        return _not_found()
    detail = stock_service.lot_detail(lot)
    b, setting = detail["balances"], detail["setting"]
    return Response({
        **_receipt(lot, b["balance"]), "in": _s(b["in"]), "issued": _s(b["issued"]), "returned": _s(b["returned"]),
        "adjusted": _s(b["adjusted"]),
        "value": _s(stock_rules.value(b["balance"], lot.rate)) if b["balance"] > 0 and lot.currency == "INR" else "0.00",
        "canWrite": _is_writer(request.user) and user_can_access_plant(request.user, lot.plant.code),
        "setting": {"isStocked": setting.is_stocked if setting else True,
                    "minLevel": _s(setting.min_level) if setting else None,
                    "minLevelUom": setting.min_level_uom if setting else "",
                    "updatedBy": setting.updated_by_email if setting else ""},
        "units": _material_units(lot.material),
        "movements": [{"date": _d(m["date"]), "kind": m["kind"], "doc": m["doc"], "voucherId": m["voucherId"],
                       "qty": _s(m["qty"]), "balance": _s(m["balance"]), "detail": m["detail"]} for m in detail["movements"]],
    })


@api_view(["POST"])
@permission_classes([requires(Perm.RM_STORE)])
def settings(request):
    """Whether a plant stocks a material, and its minimum level. Body:
    {"plant", "materialId", "isStocked", "minLevel", "minLevelUom"}."""
    data = request.data or {}
    plant = Plant.objects.filter(code=data.get("plant")).first()
    if plant is None or not user_can_access_plant(request.user, plant.code):
        return _forbidden()
    material = get_object_or_404(Material, pk=data.get("materialId"))
    try:
        stock_service.update_setting(plant, material, request.user, is_stocked=data.get("isStocked") is not False,
                                     min_level=data.get("minLevel"), min_level_uom=data.get("minLevelUom"))
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response({"ok": True})


def _material_units(material):
    return {
        "baseUom": material.base_uom,
        "factors": [{"uom": f.uom, "factor": _n(f.factor), "by": f.updated_by_email} for f in material.unit_factors.all()],
        "history": [{"field": c.field, "oldValue": c.old_value, "newValue": c.new_value, "reason": c.reason,
                     "by": c.changed_by_email, "at": c.changed_at.isoformat()}
                    for c in material.changes.filter(Q(field="base_uom") | Q(field__startswith="factor ")).order_by("-changed_at")[:10]],
    }


@api_view(["POST"])
@permission_classes([requires(Perm.RM_STORE)])
def material_units(request, material_id):
    """A material's base unit and pack factors. Body: {"baseUom", "factors":
    {unit: factor or ""}, "reason"}. Materials are company-wide, so anyone
    with RM store may, like a category; logged with the reason
    (materials.set_units()). Only MIRs posted afterwards convert by it."""
    material = get_object_or_404(Material, pk=material_id)
    data = request.data or {}
    factors = data.get("factors") or {}
    if not isinstance(factors, dict):
        return Response({"error": "factors must be an object."}, status=http.HTTP_400_BAD_REQUEST)
    try:
        materials.set_units(material, data.get("baseUom"), factors, data.get("reason"), request.user)
    except materials.MaterialError as exc:
        return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
    material.refresh_from_db()
    return Response(_material_units(material))


# ── Vouchers ──────────────────────────────────────────────────────────────


def _draws(draws):
    return [{"lotId": lot.id, "doc": stock_service.doc_of(lot), "receivedDate": _d(lot.received_date), "qty": _s(q),
             "rate": _s(lot.rate)} for lot, q in draws]


def _preview_payload(result):
    return {
        "ok": result["ok"], "errors": result["errors"], "notices": result["notices"],
        "lines": [{"index": ln["index"], "lotId": ln["lot"].id if ln.get("lot") else None, "uom": ln.get("uom"),
                   "qty": _s(ln.get("qty")), "direction": ln.get("direction"), "value": _s(ln.get("value")),
                   "book": _s(ln.get("book")), "counted": _s(ln.get("counted")),
                   "draws": _draws(ln.get("draws") or [])} for ln in result["lines"]],
    }


def _plant_writable(request):
    code = (request.data or {}).get("plant")
    return bool(code) and user_can_access_plant(request.user, code)


@api_view(["POST"])
@permission_classes([requires(Perm.RM_STORE)])
def preview(request):
    """Checks and values the form as it stands; saves nothing."""
    if not _plant_writable(request):
        return _forbidden("You cannot enter stock for that plant.")
    return Response(_preview_payload(stock_service.evaluate(request.data or {})))


@api_view(["POST"])
@permission_classes([requires(Perm.RM_STORE)])
def post_voucher(request):
    if not _plant_writable(request):
        return _forbidden("You cannot enter stock for that plant.")
    try:
        voucher = stock_service.post_voucher(request.data or {}, request.user)
    except stock_service.StockValidationError as exc:
        return _bad(exc)
    return Response(_voucher_detail(voucher, request.user), status=http.HTTP_201_CREATED)


def _line_value(line):
    if line.lot_id is None and line.voucher.kind == "ADJUST" and line.direction > 0:
        return stock_rules.value(line.qty, line.rate)  # an old opening balance
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
    lines = list(v.lines.select_related("material", "reason", "return_of_line", *("lot__" + r for r in stock_service.LOT_RELATED))
                 .prefetch_related("allocations__lot__mir_line__mir", "allocations__lot__voucher_line__voucher").order_by("line_no"))
    can_write = _is_writer(user) and user_can_access_plant(user, v.plant.code)
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
        "canApprove": can_write and is_admin(user) and v.status == "PENDING"
                      and v.created_by_id != getattr(user, "pk", None),
        "returnable": returnable,
        "lines": [{
            "lineNo": ln.line_no, "receipt": _receipt(ln.lot) if ln.lot_id else None, "material": _material(ln.material),
            "uom": ln.uom, "qty": _s(ln.qty), "direction": ln.direction,
            "reason": ln.reason.label if ln.reason else "", "note": ln.note,
            "counted": _s(ln.counted_qty), "book": _s(ln.book_qty),
            "value": _s(_line_value(ln)) if v.status != "PENDING" else None,
            "draws": [{"lotId": a.lot_id, "doc": stock_service.doc_of(a.lot), "receivedDate": _d(a.lot.received_date),
                       "qty": _s(a.qty), "rate": _s(a.lot.rate)} for a in ln.allocations.all()],
        } for ln in lines],
    }


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def vouchers(request):
    """Issue slips, returns and differences, newest first, for the plants
    the caller may read. Filters: ?plant= ?kind= ?status= ?q= (number,
    department, person, material, MIR number) ?from= ?to=."""
    plants = _readable_plants(request.user, request.query_params.get("plant"))
    qs = StockVoucher.objects.filter(plant__code__in=plants).select_related("plant", "return_of")
    if request.query_params.get("kind") in StockVoucher.Kind.values:
        qs = qs.filter(kind=request.query_params["kind"])
    if request.query_params.get("status") in StockVoucher.Status.values:
        qs = qs.filter(status=request.query_params["status"])
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(voucher_no__icontains=q) | Q(department__icontains=q) | Q(issued_to__icontains=q)
                       | Q(reference__icontains=q) | Q(lines__material__name__icontains=q)
                       | Q(lines__lot__mir_line__mir__mir_no__icontains=q)).distinct()
    for key, lookup in (("from", "voucher_date__gte"), ("to", "voucher_date__lte")):
        if request.query_params.get(key):
            qs = qs.filter(**{lookup: request.query_params[key]})
    qs = qs.prefetch_related("lines__material").order_by("-voucher_date", "-id")[:500]
    return Response({"vouchers": [_voucher_row(v) for v in qs]})


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def voucher(request, voucher_id):
    v = get_object_or_404(StockVoucher.objects.select_related("plant"), pk=voucher_id)
    if not user_can_access_plant(request.user, v.plant.code):
        return _not_found()
    return Response(_voucher_detail(v, request.user))


# ── Open mismatches: stock differences ────────────────────────────────────


@api_view(["GET"])
@permission_classes([requires(Perm.RM_STORE)])
def differences(request):
    """Stock differences at the plants the caller may read: ?status=OPEN
    (waiting for an admin, the default) / RESOLVED / CANCELLED / ALL,
    ?plant= to narrow."""
    plants = _readable_plants(request.user, request.query_params.get("plant"))
    out = []
    for ln in stock_service.differences(plants, request.query_params.get("status", "OPEN")):
        v = ln.voucher
        out.append({
            "voucherId": v.id, "voucherNo": v.voucher_no, "date": _d(v.voucher_date), "plant": _plant(v.plant),
            "status": v.status, "statusLabel": v.get_status_display(), "lineNo": ln.line_no,
            "receipt": _receipt(ln.lot) if ln.lot_id else None, "material": _material(ln.material), "uom": ln.uom,
            "kind": "COUNT" if ln.counted_qty is not None else ("GAIN" if ln.direction > 0 else "WRITE_OFF"),
            "direction": ln.direction, "qty": _s(ln.qty), "counted": _s(ln.counted_qty), "book": _s(ln.book_qty),
            "value": _s(stock_rules.value(ln.qty * ln.direction, ln.lot.rate)) if ln.lot_id and ln.lot.currency == "INR" else None,
            "reason": ln.reason.label if ln.reason else "", "note": ln.note, "createdBy": v.created_by_email,
            "decidedBy": v.decided_by_email, "decisionNote": v.decision_note,
        })
    return Response({"differences": out})


def _writable_voucher(request, voucher_id):
    v = get_object_or_404(StockVoucher.objects.select_related("plant"), pk=voucher_id)
    return v, user_can_access_plant(request.user, v.plant.code)


@api_view(["POST"])
@permission_classes([requires(Perm.RM_STORE)])
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
