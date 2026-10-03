"""
MIR entry (2026-09-28, project owner): the clerk finds an open PO by its
number - at ANY plant, since one plant's store may receive another plant's
order - picks its lines, types the invoice and, per line, the quantity and
rate; everything else is computed here.

`evaluate()` is the one place a MIR is checked and priced. The form's live
preview calls it without saving; `post_mir()` calls it again inside the
transaction, with the PO lines locked, and saves exactly what it returned. So
the screen can never show a figure the server would compute differently.

What a posting checks (each failure is a message on the field it concerns):

  header  - the receiving plant exists; the MIR date is not in the future and
            not before any line's PO date; the invoice number and date are
            entered and the invoice date is not after the MIR date (an
            invoice dated before a line's PO is allowed but needs a reason); one
            vendor across every line; a tax type is known. The invoice
            number is NOT unique (owner, 2026-09-29): one invoice can arrive
            as several deliveries, each its own MIR, so earlier MIRs of the
            same vendor invoice come back as a notice, never an error.
  lines   - each PO line is open (PO and line active, not closed, not
            waiting for review, not already received in full), appears once,
            and has a positive quantity, a rejected quantity within it, a
            non-negative rate/discount, a GST rate on a slab, and a material
            category: the material master's own (apps/services/materials.py)
            when the material is filed, otherwise one picked from the
            reference list, which posting then files on the material for
            good (sub-category optional, but of that category).
  reasons - accepted quantity short of or over the open quantity, any
            rejected quantity, a rate different from the PO's, a GST rate
            other than the one the PO's totals imply, an invoice total more
            than the rounding rupee away from the computed one, and a tax
            type other than the states imply: each needs a reason of its
            kind (and a note where the reason says so). Each becomes a
            MirMismatch that stays OPEN until a purchase manager resolves
            it.

Quantities are compared EXACTLY. There is no tolerance: an over-delivery of
weighed material is recorded with the "Weighbridge variance" reason, not
waved through.

STOCK (2026-09-29). A posted MIR is also the store's receipt: post_mir()
makes one stock lot per line (stock_service.receive_mir()), holding the
line's accepted quantity. Cancelling the MIR or recording a rejection later
takes that stock back out, so both are refused once it has been issued
(stock_service.check_mir_cancel() / check_mir_rejection()).
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.services import materials, stock_service
from apps.services import procurement_rules as rules

MAX_LINES = 50


class MirValidationError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


# ── Reading ───────────────────────────────────────────────────────────────


def accepted_by_line(line_ids) -> dict:
    """{po_line_id: accepted qty} over POSTED MIR lines - received minus
    rejected. Never stored anywhere, so a cancelled MIR drops out at once."""
    from apps.core.models import MirLine

    rows = (MirLine.objects.filter(po_line_id__in=list(line_ids), mir__status="POSTED")
            .values("po_line_id").annotate(acc=Sum(F("qty_received") - F("qty_rejected"))))
    return {r["po_line_id"]: r["acc"] or Decimal("0") for r in rows}


def _accepted_annotation():
    return Coalesce(
        Sum(F("mir_lines__qty_received") - F("mir_lines__qty_rejected"), filter=Q(mir_lines__mir__status="POSTED")),
        Value(Decimal("0")),
        output_field=DecimalField(max_digits=16, decimal_places=3),
    )


def line_state(line, accepted: Decimal) -> dict:
    """Where a PO line stands and whether it can take a receipt now."""
    ordered = line.qty_ordered
    open_qty = (ordered - accepted) if ordered is not None else None
    blocked = ""
    po = line.purchase_order
    if po.kind == "import":
        # Import receipts are checked against their shipment's Bill of Entry
        # (quantity landed, customs exchange rate) - not built yet.
        blocked = "Import receipts are entered against the shipment's Bill of Entry, which is not in the app yet."
    elif not po.is_active:
        blocked = "The PO is no longer in the master PO sheet."
    elif po.billing_plant_id is not None and po.billing_plant_id != po.plant_id:
        # A PO belongs to its billing plant (owner, 2026-10-03); one filed in
        # another plant's sheet waits until it is moved there.
        blocked = (f"The PO is billed to {po.billing_plant.name} but sits in {po.plant.name}'s PO sheet - "
                   f"move it to {po.billing_plant.name}'s sheet before receiving against it.")
    elif not line.is_active:
        blocked = "This line is no longer on the PO."
    elif line.needs_review:
        blocked = "The PO sheet changed this line after receipts - a purchase manager must review it first."
    elif line.closed_at is not None:
        blocked = "Short-closed" + (f": {line.close_note}" if line.close_note else "") + " - a purchase manager can reopen it."
    elif line.material_id is None:
        # No description means no material, and so no stock lot - a receipt
        # that would never reach RM stock is refused instead.
        blocked = "The PO sheet gives no material description for this line."
    elif po.po_date is None:
        # The PO created date is compulsory on a MIR (owner, 2026-10-03).
        blocked = "The PO sheet gives no PO date for this order."
    elif ordered is None or ordered <= 0:
        blocked = "The PO sheet gives no quantity for this line."
    elif line.rate is None:
        blocked = "The PO sheet gives no rate for this line."
    elif open_qty <= 0:
        blocked = "Already received in full."
    if line.closed_at is not None:
        status = "closed"
    elif ordered is not None and accepted >= ordered:
        status = "received"
    elif accepted > 0:
        status = "partial"
    else:
        status = "open"
    return {"accepted": accepted, "open_qty": open_qty, "status": status, "receivable": not blocked, "blocked_reason": blocked}


def search_open_pos(query: str, plant_codes, limit: int = 25) -> list:
    """Active POs at the plants in `plant_codes` (the caller's own - a PO is
    received only at its own plant, owner 2026-10-03) with at least one line
    not yet received in
    full - open, short-closed or waiting for a purchase manager's review -
    whose PO number contains `query` (case-insensitive). Newest first.
    Domestic orders only: an import receipt waits for its shipment's Bill of
    Entry in the app.
    Closed and review-flagged lines are included so a purchase manager can
    find the PO to reopen or confirm them; they still take no receipt
    (line_state()).

    PO number only (project owner, 2026-09-29): the clerk has the PO number
    in hand from the delivery papers, and a vendor-name search offered every
    open order of that vendor, which is how a receipt lands on the wrong
    one."""
    from apps.core.models import PurchaseOrder, PurchaseOrderLine

    query = (query or "").strip()
    if len(query) < 2:
        return []
    open_line = (PurchaseOrderLine.objects.filter(is_active=True, qty_ordered__gt=0, rate__isnull=False)
                 .annotate(acc=_accepted_annotation()).filter(acc__lt=F("qty_ordered")))
    return list(
        PurchaseOrder.objects.filter(is_active=True, kind=PurchaseOrder.Kind.DOMESTIC, lines__in=open_line,
                                     plant__code__in=list(plant_codes))
        .filter(po_number__icontains=query)
        .select_related("plant", "vendor", "billing_plant").distinct().order_by("-po_date", "-id")[:limit]
    )


def category_options() -> dict:
    """{category: [sub-category, ...]} from MaterialCategoryReference, the
    one list the MIR form's category pickers offer."""
    from apps.core.models import MaterialCategoryReference

    out: dict[str, set] = {}
    for category, sub in MaterialCategoryReference.objects.values_list("category", "subcategory"):
        category = (category or "").strip()
        if category:
            out.setdefault(category, set())
            if (sub or "").strip():
                out[category].add(sub.strip())
    return {c: sorted(subs) for c, subs in sorted(out.items())}


def po_lines_with_state(po) -> list:
    lines = list(po.lines.select_related("purchase_order__plant", "material", "closed_reason").order_by("line_no"))
    accepted = accepted_by_line(line.id for line in lines)
    return [(line, line_state(line, accepted.get(line.id, Decimal("0")))) for line in lines]


# ── Parsing ───────────────────────────────────────────────────────────────


def _dec(value, field, errors, *, required=True, places=None, default=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.append({"field": field, "message": "Required."})
        return default
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        errors.append({"field": field, "message": "Not a number."})
        return default
    if not d.is_finite():
        errors.append({"field": field, "message": "Not a number."})
        return default
    if places is not None and d != d.quantize(Decimal(1).scaleb(-places)):
        errors.append({"field": field, "message": f"At most {places} decimal places."})
        return default
    return d


def _date(value, field, errors):
    if not value:
        errors.append({"field": field, "message": "Required."})
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        errors.append({"field": field, "message": "Not a date (YYYY-MM-DD)."})
        return None


def _text(value, limit) -> str:
    return str(value or "").strip()[:limit]


def _reason(code, kind, field, note, errors, reasons):
    """The active MirReasonCode for a detected mismatch, checking it is of
    the right kind and that a required note is there."""
    if not code:
        errors.append({"field": field, "message": "Choose a reason."})
        return None
    reason = reasons.get(code)
    if reason is None or reason.kind != kind or not reason.is_active:
        errors.append({"field": field, "message": "Not a reason for this difference."})
        return None
    if reason.note_required and not (note or "").strip():
        errors.append({"field": field.replace("reason", "note"), "message": "This reason needs a note."})
    return reason


# ── Evaluate (preview and post share this) ────────────────────────────────


def evaluate(payload: dict, *, lock: bool = False) -> dict:
    """Check and price a MIR. Returns {"ok", "errors", "mismatches",
    "lines", "totals", "vendor", "tax_type", ...}. Never saves. With
    lock=True (post_mir only) the PO lines are row-locked first, so the open
    quantities it compares against cannot move before the save."""
    from apps.core.models import Mir, MirReasonCode, Plant, PurchaseOrderLine, Vendor

    errors: list[dict] = []
    mismatches: list[dict] = []
    reasons = {r.code: r for r in MirReasonCode.objects.all()}
    today = timezone.localdate()

    plant = Plant.objects.filter(code=payload.get("plant")).first()
    if plant is None:
        errors.append({"field": "plant", "message": "Choose the receiving plant."})
    mir_date = _date(payload.get("mir_date"), "mir_date", errors)
    if mir_date and mir_date > today:
        errors.append({"field": "mir_date", "message": "The MIR date cannot be in the future."})
    notices: list[str] = []
    categories = category_options()
    invoice_no = _text(payload.get("invoice_no"), 60)
    if not rules.invoice_key(invoice_no):
        errors.append({"field": "invoice_no", "message": "Required."})
    invoice_date = _date(payload.get("invoice_date"), "invoice_date", errors)
    if invoice_date and mir_date and invoice_date > mir_date:
        errors.append({"field": "invoice_date", "message": "The invoice date cannot be after the MIR date."})
    invoice_total = _dec(payload.get("invoice_total"), "invoice_total", errors, places=2)
    if invoice_total is not None and invoice_total < 0:
        errors.append({"field": "invoice_total", "message": "Cannot be negative."})
    # Required (owner, 2026-10-03): 0 when the invoice shows none, but typed,
    # so a missed TCS is not mistaken for none.
    tcs = _dec(payload.get("tcs_amount"), "tcs_amount", errors, places=2)
    if tcs is None:
        tcs = Decimal("0")
    elif tcs < 0:
        errors.append({"field": "tcs_amount", "message": "Cannot be negative."})
        tcs = Decimal("0")

    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        errors.append({"field": "lines", "message": "Pick at least one PO line."})
        raw_lines = []
    if len(raw_lines) > MAX_LINES:
        errors.append({"field": "lines", "message": f"At most {MAX_LINES} lines on one MIR."})
        raw_lines = raw_lines[:MAX_LINES]

    ids = []
    for i, raw in enumerate(raw_lines):
        try:
            ids.append(int(raw.get("po_line_id")))
        except (TypeError, ValueError, AttributeError):
            errors.append({"field": f"lines.{i}.po_line_id", "message": "Not a PO line."})
            ids.append(None)
    wanted = [x for x in ids if x is not None]
    qs = PurchaseOrderLine.objects.select_related("purchase_order__plant", "purchase_order__vendor",
                                                  "purchase_order__billing_plant", "material")
    if lock:
        # Plain FOR UPDATE on the lines only (the joined tables are not
        # locked), in id order so two posts never deadlock.
        list(PurchaseOrderLine.objects.select_for_update().filter(id__in=wanted).order_by("id"))
    po_lines = {line.id: line for line in qs.filter(id__in=wanted)}
    accepted = accepted_by_line(po_lines)

    # ── Vendor: one per MIR, from the POs (or chosen when none names one).
    po_vendors = {po_lines[x].purchase_order.vendor for x in wanted if x in po_lines and po_lines[x].purchase_order.vendor}
    vendor = None
    chosen_vendor_id = payload.get("vendor_id")
    if chosen_vendor_id not in (None, ""):
        vendor = Vendor.objects.filter(pk=chosen_vendor_id).first()
        if vendor is None:
            errors.append({"field": "vendor_id", "message": "Unknown vendor."})
    if len(po_vendors) > 1:
        errors.append({"field": "lines", "message": "One MIR is one vendor's invoice - these lines are from POs of "
                                                    + " and ".join(sorted(v.name for v in po_vendors)) + "."})
    elif po_vendors:
        po_vendor = next(iter(po_vendors))
        if vendor is not None and vendor != po_vendor:
            errors.append({"field": "vendor_id", "message": f"The PO is with {po_vendor.name}."})
        vendor = po_vendor
    elif wanted and vendor is None and chosen_vendor_id in (None, ""):
        errors.append({"field": "vendor_id", "message": "This PO names no vendor - choose the vendor on the invoice."})

    # ── The same invoice on earlier MIRs at this plant: allowed (one invoice
    # can come as several deliveries), but said, so a clerk entering the same
    # delivery twice sees it before saving. This plant only - a PO is received
    # only at its own plant, and another plant's MIRs are not this clerk's to
    # see.
    invoice_key = rules.invoice_key(invoice_no)
    invoice_fy = rules.financial_year(invoice_date) if invoice_date else ""
    if vendor is not None and invoice_key and invoice_fy and plant is not None:
        earlier = (Mir.objects.filter(plant=plant, vendor=vendor, invoice_key=invoice_key, invoice_fy=invoice_fy, status="POSTED")
                   .select_related("plant").order_by("mir_date", "id")[:5])
        for dup in earlier:
            notices.append(f"Invoice {dup.invoice_no} is already on {dup.mir_no} ({dup.plant.name}, "
                           f"{dup.mir_date:%d-%m-%Y}, entered by {dup.created_by_email}). "
                           "Save only if this is another delivery.")

    # ── The vendor's State (required, owner 2026-10-03): its GSTIN says it;
    # with no GSTIN on file the clerk picks it from the invoice.
    chosen_state = str(payload.get("vendor_state") or "").strip()
    gst_state = rules.gstin_state(vendor.gstin) if vendor else ""
    vendor_state = ""
    if gst_state:
        vendor_state = gst_state
        if chosen_state and chosen_state != gst_state:
            errors.append({"field": "vendor_state", "message": f"The vendor's GSTIN is from {rules.GST_STATES.get(gst_state, gst_state)}."})
    elif chosen_state in rules.GST_STATES:
        vendor_state = chosen_state
    elif vendor is not None:
        errors.append({"field": "vendor_state", "message": "Choose the vendor's State, as the invoice prints it."})

    # ── Tax type.
    po_tax = next((po_lines[x].purchase_order.tax_type for x in wanted if x in po_lines and po_lines[x].purchase_order.tax_type), "")
    expected_tax = rules.expected_tax_type(vendor.gstin if vendor else "", plant.state_code if plant else "",
                                           plant.is_union_territory if plant else False, po_tax, vendor_state)
    tax_type = payload.get("tax_type") or expected_tax
    if tax_type not in rules.TaxType.ALL:
        errors.append({"field": "tax_type", "message": "Choose IGST, CGST + SGST or CGST + UGST."})
        tax_type = ""
    if tax_type and expected_tax and tax_type != expected_tax:
        reason = _reason(payload.get("tax_type_reason"), "TAX_TYPE", "tax_type_reason", payload.get("tax_type_note"), errors, reasons)
        mismatches.append({"line": None, "kind": "TAX_TYPE", "expected": None, "actual": None, "difference_pct": None,
                           "expected_text": rules.TaxType.LABELS[expected_tax], "actual_text": rules.TaxType.LABELS[tax_type],
                           "reason": reason, "note": _text(payload.get("tax_type_note"), 2000)})

    # ── Lines.
    lines_out = []
    seen = set()
    for i, raw in enumerate(raw_lines):
        pid = ids[i]
        if pid is None:
            continue
        f = f"lines.{i}"
        line = po_lines.get(pid)
        if line is None:
            errors.append({"field": f"{f}.po_line_id", "message": "No such PO line."})
            continue
        if pid in seen:
            errors.append({"field": f"{f}.po_line_id", "message": "This PO line is already on the MIR."})
            continue
        seen.add(pid)
        state = line_state(line, accepted.get(pid, Decimal("0")))
        if not state["receivable"]:
            errors.append({"field": f"{f}.po_line_id", "message": state["blocked_reason"]})
            continue
        po = line.purchase_order
        if plant is not None and po.plant_id != plant.id:
            # Only the PO's own (billing) plant receives against it (owner,
            # 2026-10-03).
            errors.append({"field": f"{f}.po_line_id",
                           "message": f"PO {po.po_number} belongs to {po.plant.name} - only {po.plant.name} can enter a MIR against it."})
            continue
        if mir_date and po.po_date and mir_date < po.po_date:
            errors.append({"field": "mir_date", "message": f"The MIR date is before PO {po.po_number}'s date ({po.po_date:%d-%m-%Y})."})
        qty = _dec(raw.get("qty_received"), f"{f}.qty_received", errors, places=3)
        if qty is not None and qty <= 0:
            errors.append({"field": f"{f}.qty_received", "message": "Must be more than zero."})
            qty = None
        rejected = _dec(raw.get("qty_rejected"), f"{f}.qty_rejected", errors, required=False, places=3, default=Decimal("0"))
        if rejected is not None and (rejected < 0 or (qty is not None and rejected > qty)):
            errors.append({"field": f"{f}.qty_rejected", "message": "Between zero and the quantity received."})
            rejected = None
        rate = _dec(raw.get("rate"), f"{f}.rate", errors, places=4)
        if rate is not None and rate < 0:
            errors.append({"field": f"{f}.rate", "message": "Cannot be negative."})
            rate = None
        discount = _dec(raw.get("discount"), f"{f}.discount", errors, required=False, places=2, default=Decimal("0"))
        other = _dec(raw.get("other_charges"), f"{f}.other_charges", errors, required=False, places=2, default=Decimal("0"))
        for name, val in (("discount", discount), ("other_charges", other)):
            if val is not None and val < 0:
                errors.append({"field": f"{f}.{name}", "message": "Cannot be negative."})
        gst = _dec(raw.get("gst_rate"), f"{f}.gst_rate", errors, places=2)
        if gst is not None and not rules.is_gst_slab(gst):
            errors.append({"field": f"{f}.gst_rate", "message": "Not a GST rate (0, 0.1, 0.25, 1.5, 3, 5, 12, 18, 28, 40)."})
            gst = None
        # A filed material's category is the material master's, whatever the
        # form sends; an unfiled one takes the clerk's pick (checked here,
        # filed on the material when the MIR is posted).
        material = line.material
        from_master = bool(material and material.category)
        if from_master:
            category, subcategory = material.category, material.subcategory
        else:
            category = _text(raw.get("material_category"), 200)
            subcategory = _text(raw.get("material_subcategory"), 200)
            if not category:
                errors.append({"field": f"{f}.material_category", "message": "Required."})
            elif categories and category not in categories:
                errors.append({"field": f"{f}.material_category", "message": "Choose a category from the list."})
            elif subcategory and categories and subcategory not in categories[category]:
                errors.append({"field": f"{f}.material_subcategory", "message": f"Not a sub-category of {category}."})
        rolls = raw.get("rolls")
        if rolls not in (None, ""):
            try:
                rolls = int(rolls)
                if rolls < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append({"field": f"{f}.rolls", "message": "A whole number of rolls."})
                rolls = None
        else:
            rolls = None

        out = {"index": i, "po_line": line, "state": state, "qty_received": qty, "qty_rejected": rejected, "rate": rate,
               "discount": discount, "other_charges": other, "gst_rate": gst, "rolls": rolls,
               "batch_no": _text(raw.get("batch_no"), 60), "dept_use": _text(raw.get("dept_use"), 60),
               "material_category": category, "material_subcategory": subcategory, "category_from_master": from_master,
               "remarks": _text(raw.get("remarks"), 2000), "amounts": None}
        ready = None not in (qty, rejected, rate, discount, other, gst) and discount >= 0 and other >= 0
        if ready:
            amounts = rules.line_amounts(qty, rate, discount, other, gst, tax_type or rules.TaxType.IGST)
            if amounts["taxable"] < 0:
                errors.append({"field": f"{f}.discount", "message": "The discount is more than the line's value."})
            out["amounts"] = amounts
        if qty is not None and rejected is not None:
            accepted_qty = qty - rejected
            open_qty = state["open_qty"]
            if accepted_qty != open_qty:
                kind = "QTY_SHORT" if accepted_qty < open_qty else "QTY_OVER"
                reason = _reason(raw.get("qty_reason"), kind, f"{f}.qty_reason", raw.get("qty_note"), errors, reasons)
                mismatches.append({"line": i, "kind": kind, "expected": open_qty, "actual": accepted_qty,
                                   "difference_pct": rules.pct_of(accepted_qty - open_qty, open_qty),
                                   "reason": reason, "note": _text(raw.get("qty_note"), 2000)})
        if rejected is not None and rejected > 0:
            reason = _reason(raw.get("reject_reason"), "REJECTION", f"{f}.reject_reason", raw.get("reject_note"), errors, reasons)
            mismatches.append({"line": i, "kind": "QTY_REJECTED", "expected": Decimal("0"), "actual": rejected,
                               "difference_pct": rules.pct_of(rejected, qty) if qty else None,
                               "reason": reason, "note": _text(raw.get("reject_note"), 2000)})
        po_gst = rules.po_gst_rate(po.total_value, po.total_inclusive_value)
        if gst is not None and po_gst is not None and gst != po_gst:
            reason = _reason(raw.get("gst_reason"), "GST_RATE", f"{f}.gst_reason", raw.get("gst_note"), errors, reasons)
            mismatches.append({"line": i, "kind": "GST_RATE", "expected": po_gst, "actual": gst,
                               "difference_pct": None, "reason": reason, "note": _text(raw.get("gst_note"), 2000)})
        if rate is not None and rules.rate_differs(line.rate, rate):
            kind = "RATE_HIGH" if rate > line.rate else "RATE_LOW"
            reason = _reason(raw.get("rate_reason"), "RATE", f"{f}.rate_reason", raw.get("rate_note"), errors, reasons)
            mismatches.append({"line": i, "kind": kind, "expected": line.rate, "actual": rate,
                               "difference_pct": rules.pct_of(rate - line.rate, line.rate),
                               "reason": reason, "note": _text(raw.get("rate_note"), 2000)})
        lines_out.append(out)

    # ── Invoice dated before its PO (owner, 2026-09-29). Not refused: in the
    # Drive MIRs 22 of 430 Vapi receipts with a known PO carry an invoice
    # dated before the PO (a verbal order, advance billing), almost all with
    # the goods arriving after it. So it needs a reason and goes to Open
    # mismatches; receiving goods before the PO date stays refused above.
    po_dates = [po_lines[x].purchase_order.po_date for x in wanted if x in po_lines and po_lines[x].purchase_order.po_date]
    if invoice_date and po_dates and invoice_date < max(po_dates):
        latest = max(po_dates)
        reason = _reason(payload.get("invoice_date_reason"), "INVOICE_DATE", "invoice_date_reason",
                         payload.get("invoice_date_note"), errors, reasons)
        mismatches.append({"line": None, "kind": "INVOICE_BEFORE_PO", "expected": Decimal("0"),
                           "actual": Decimal((latest - invoice_date).days), "difference_pct": None,
                           "expected_text": f"{latest:%d-%m-%Y}", "actual_text": f"{invoice_date:%d-%m-%Y}",
                           "reason": reason, "note": _text(payload.get("invoice_date_note"), 2000)})

    # ── Invoice total.
    priced = [ln["amounts"] for ln in lines_out if ln["amounts"]]
    computed_total = None
    if priced and len(priced) == len(lines_out) and tcs is not None:
        computed_total = sum((a["total"] for a in priced), Decimal("0")) + tcs
        if invoice_total is not None and abs(invoice_total - computed_total) > rules.INVOICE_ROUNDING_TOLERANCE:
            reason = _reason(payload.get("invoice_total_reason"), "INVOICE_TOTAL", "invoice_total_reason",
                             payload.get("invoice_total_note"), errors, reasons)
            mismatches.append({"line": None, "kind": "INVOICE_TOTAL", "expected": computed_total, "actual": invoice_total,
                               "difference_pct": rules.pct_of(invoice_total - computed_total, computed_total),
                               "reason": reason, "note": _text(payload.get("invoice_total_note"), 2000)})

    return {
        "ok": not errors, "errors": errors, "mismatches": mismatches, "lines": lines_out,
        "plant": plant, "vendor": vendor, "mir_date": mir_date, "invoice_no": invoice_no, "invoice_key": invoice_key,
        "invoice_date": invoice_date, "invoice_fy": invoice_fy, "invoice_total": invoice_total, "tcs_amount": tcs,
        "vendor_state": vendor_state,
        "tax_type": tax_type, "tax_type_expected": expected_tax, "computed_total": computed_total,
        # Each cut to its own column (EDITABLE_HEADER): a flat 60 let a
        # 31-60 character vehicle or e-way bill number through to a 500.
        "header": {k: _text(payload.get(k), EDITABLE_HEADER[k]) for k in (
            "challan_no", "lr_no", "vehicle_no", "eway_bill_no", "gate_entry_no", "weighbridge_slip_no")}
                  | {"sap_grn_number": _text(payload.get("sap_grn_number"), 50)},
        "notices": notices,
        "remarks": _text(payload.get("remarks"), 2000),
    }


# ── Writing ───────────────────────────────────────────────────────────────


def _next_seq(plant, fy) -> int:
    from apps.core.models import MirSequence

    seq, _ = MirSequence.objects.select_for_update().get_or_create(plant=plant, fy=fy)
    seq.last_seq += 1
    seq.save(update_fields=["last_seq"])
    return seq.last_seq


def post_mir(payload: dict, user):
    """Validate and save a MIR in one transaction. Raises MirValidationError
    with the field errors; returns the saved Mir."""
    from apps.core.models import Mir, MirLine, MirMismatch

    with transaction.atomic():
        result = evaluate(payload, lock=True)
        if not result["ok"]:
            raise MirValidationError(result["errors"])
        plant = result["plant"]
        fy = rules.financial_year(result["mir_date"])
        seq = _next_seq(plant, fy)
        mir = Mir.objects.create(
            plant=plant, fy=fy, seq=seq, mir_no=rules.mir_number(plant.mir_prefix, fy, seq),
            mir_date=result["mir_date"], vendor=result["vendor"], vendor_state=result["vendor_state"], invoice_no=result["invoice_no"],
            invoice_key=result["invoice_key"], invoice_date=result["invoice_date"], invoice_fy=result["invoice_fy"],
            invoice_total=result["invoice_total"], tcs_amount=result["tcs_amount"], tax_type=result["tax_type"],
            tax_type_expected=result["tax_type_expected"], remarks=result["remarks"],
            created_by=user, created_by_email=getattr(user, "email", ""), **result["header"],
        )
        saved = {}
        for n, ln in enumerate(result["lines"], start=1):
            a = ln["amounts"]
            saved[ln["index"]] = MirLine.objects.create(
                mir=mir, line_no=n, po_line=ln["po_line"], description=ln["po_line"].description[:500],
                uom=ln["po_line"].uom[:20], qty_received=ln["qty_received"],
                qty_rejected=ln["qty_rejected"], rate=ln["rate"], po_rate=ln["po_line"].rate,
                open_qty_before=ln["state"]["open_qty"], discount=ln["discount"], other_charges=ln["other_charges"],
                gst_rate=ln["gst_rate"], gross=a["gross"], taxable=a["taxable"], igst=a["igst"], cgst=a["cgst"],
                sgst=a["sgst"], line_total=a["total"], rolls=ln["rolls"], batch_no=ln["batch_no"],
                dept_use=ln["dept_use"], remarks=ln["remarks"],
            )
            if not ln["category_from_master"]:
                materials.set_category(ln["po_line"].material, ln["material_category"], ln["material_subcategory"], user)
        now = timezone.now()
        for mm in result["mismatches"]:
            mir_line = saved[mm["line"]] if mm["line"] is not None else None
            MirMismatch.objects.create(
                mir=mir, mir_line=mir_line, kind=mm["kind"], expected=mm["expected"], actual=mm["actual"],
                difference_pct=mm["difference_pct"], reason=mm["reason"], note=mm["note"],
            )
            # "Close the line": no balance is coming for this PO line.
            if mm["kind"] == "QTY_SHORT" and mm["reason"].closes_line:
                po_line = mir_line.po_line
                po_line.closed_at, po_line.closed_by, po_line.closed_reason = now, user, mm["reason"]
                po_line.close_note, po_line.closed_by_mir_line = mm["note"] or mm["reason"].label, mir_line
                po_line.save(update_fields=["closed_at", "closed_by", "closed_reason", "close_note", "closed_by_mir_line"])
        # Into the store: one lot per line, holding its accepted quantity.
        stock_service.receive_mir(mir)
        return mir


def _stock_check(check, *args):
    """A stock refusal, as the MIR form's own error."""
    try:
        check(*args)
    except stock_service.StockValidationError as exc:
        raise MirValidationError(exc.errors) from exc


def cancel_mir(mir, user, reason: str):
    """Cancel a posted MIR: its lines stop counting at once (received is
    summed from posted MIRs only), its open mismatches become VOID, and any
    PO line it closed is reopened. Nothing is deleted. Its stock lots then
    hold nothing - refused while any of that stock is issued."""
    from apps.core.models import Mir, MirMismatch, PurchaseOrderLine

    reason = (reason or "").strip()
    if not reason:
        raise MirValidationError([{"field": "reason", "message": "Say why the MIR is cancelled."}])
    with transaction.atomic():
        mir = Mir.objects.select_for_update().get(pk=mir.pk)
        if mir.status != Mir.Status.POSTED:
            raise MirValidationError([{"field": "status", "message": "This MIR is already cancelled."}])
        # Its stock leaves with it - refused if any of it has been issued.
        _stock_check(stock_service.check_mir_cancel, mir)
        mir.status = Mir.Status.CANCELLED
        mir.cancelled_by, mir.cancelled_by_email = user, getattr(user, "email", "")
        mir.cancelled_at, mir.cancel_reason = timezone.now(), reason
        mir.save(update_fields=["status", "cancelled_by", "cancelled_by_email", "cancelled_at", "cancel_reason"])
        MirMismatch.objects.filter(mir=mir, status=MirMismatch.Status.OPEN).update(status=MirMismatch.Status.VOID)
        PurchaseOrderLine.objects.filter(closed_by_mir_line__mir=mir).update(
            closed_at=None, closed_by=None, closed_reason=None, close_note="", closed_by_mir_line=None)
    return mir


def resolve_mismatch(mismatch, user, note: str):
    from apps.core.models import MirMismatch

    note = (note or "").strip()
    if not note:
        raise MirValidationError([{"field": "note", "message": "Say how it was resolved."}])
    with transaction.atomic():
        mm = MirMismatch.objects.select_for_update().get(pk=mismatch.pk)
        if mm.status != MirMismatch.Status.OPEN:
            raise MirValidationError([{"field": "status", "message": "Only an open mismatch can be resolved."}])
        mm.status, mm.resolved_by, mm.resolved_by_email = MirMismatch.Status.RESOLVED, user, getattr(user, "email", "")
        mm.resolved_at, mm.resolution_note = timezone.now(), note
        mm.save(update_fields=["status", "resolved_by", "resolved_by_email", "resolved_at", "resolution_note"])
    return mm


def close_po_line(line, user, reason_code: str, note: str):
    """A purchase manager closes a line with no MIR (the balance is not
    coming). Only a "close the line" shortfall reason may be used."""
    from apps.core.models import MirReasonCode, PurchaseOrderLine

    errors: list[dict] = []
    reason = MirReasonCode.objects.filter(code=reason_code, kind="QTY_SHORT", closes_line=True, is_active=True).first()
    if reason is None:
        errors.append({"field": "reason", "message": "Choose a reason that closes the line."})
    elif reason.note_required and not (note or "").strip():
        errors.append({"field": "note", "message": "This reason needs a note."})
    if errors:
        raise MirValidationError(errors)
    with transaction.atomic():
        line = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk)
        if line.closed_at is not None:
            raise MirValidationError([{"field": "status", "message": "This line is already closed."}])
        line.closed_at, line.closed_by, line.closed_reason = timezone.now(), user, reason
        line.close_note, line.closed_by_mir_line = (note or "").strip() or reason.label, None
        line.save(update_fields=["closed_at", "closed_by", "closed_reason", "close_note", "closed_by_mir_line"])
    return line


def reopen_po_line(line):
    from apps.core.models import PurchaseOrderLine

    with transaction.atomic():
        line = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk)
        if line.closed_at is None:
            raise MirValidationError([{"field": "status", "message": "This line is not closed."}])
        line.closed_at, line.closed_by, line.closed_reason, line.close_note, line.closed_by_mir_line = None, None, None, "", None
        line.save(update_fields=["closed_at", "closed_by", "closed_reason", "close_note", "closed_by_mir_line"])
    return line


def clear_line_review(line, user, note: str):
    """A purchase manager confirms the receipts still belong to a line the
    CSV changed; the confirmation is logged as a line change."""
    from apps.core.models import PurchaseOrderLine, PurchaseOrderLineChange

    note = (note or "").strip()
    if not note:
        raise MirValidationError([{"field": "note", "message": "Say what was checked."}])
    with transaction.atomic():
        line = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk)
        if not line.needs_review:
            raise MirValidationError([{"field": "status", "message": "This line is not waiting for review."}])
        PurchaseOrderLineChange.objects.create(po_line=line, field="needs_review", old_value=line.review_note,
                                               new_value=f"Reviewed by {getattr(user, 'email', '')}: {note}")
        line.needs_review, line.review_note = False, ""
        line.save(update_fields=["needs_review", "review_note"])
    return line


# ── Editing a posted MIR (2026-09-29) ─────────────────────────────────────
#
# Deliberately narrow. What a receipt MEANS - quantities received, rates,
# GST, discounts, the tax type, the invoice total, which PO lines - is never
# edited: those figures fed the PO's open quantity and the mismatches, so a
# wrong one is fixed by cancelling the MIR and entering it again (a new
# number). What can change is paperwork and a rejection found later, each
# with a reason, each kept in MirChange.

EDIT_WINDOW_DAYS = 7          # paperwork fields and a line's department/remarks
REJECTION_WINDOW_DAYS = 30    # a rejection found after posting (QC report)

# Header fields, and how many characters each takes. The SAP GRN number is
# usually known only days later, so it may be filled in any time.
EDITABLE_HEADER = {
    "invoice_no": 60, "invoice_date": None, "challan_no": 60, "lr_no": 60, "vehicle_no": 30,
    "eway_bill_no": 30, "gate_entry_no": 40, "weighbridge_slip_no": 40, "remarks": 2000, "sap_grn_number": 50,
}
ANYTIME_HEADER = {"sap_grn_number"}
EDITABLE_LINE = {"dept_use": 60, "remarks": 2000}


def edit_window(mir) -> dict:
    """What may still be edited on this MIR, and until when."""
    posted = timezone.localdate(mir.created_at)
    today = timezone.localdate()
    edit_until = posted + datetime.timedelta(days=EDIT_WINDOW_DAYS)
    reject_until = mir.mir_date + datetime.timedelta(days=REJECTION_WINDOW_DAYS)
    live = mir.status == "POSTED"
    return {"edit_until": edit_until, "reject_until": reject_until,
            "can_edit": live and today <= edit_until, "can_edit_grn": live,
            "can_reject": live and today <= reject_until}


def _log(mir, line, field, old, new, reason, user):
    from apps.core.models import MirChange

    MirChange.objects.create(mir=mir, mir_line=line, field=field, old_value="" if old is None else str(old),
                             new_value="" if new is None else str(new), reason=reason, changed_by=user,
                             changed_by_email=getattr(user, "email", ""))


def edit_mir(mir, user, header: dict, lines: dict, reason: str):
    """Change a posted MIR's paperwork. `header` is {field: value} for
    EDITABLE_HEADER fields, `lines` {line_no: {field: value}} for
    EDITABLE_LINE fields; anything else is refused. Returns the number of
    fields changed (0 is refused: nothing to save)."""
    from apps.core.models import Mir

    reason = (reason or "").strip()
    errors: list[dict] = []
    if not reason:
        errors.append({"field": "reason", "message": "Say why the MIR is being changed."})
    # Each set checked on its own: a header field sent as a line field (or
    # the reverse) passed a combined check and then raised a KeyError below.
    wrong = [k for k in header if k not in EDITABLE_HEADER] + [
        k for fields in lines.values() for k in fields if k not in EDITABLE_LINE]
    for key in wrong:
        errors.append({"field": key, "message": "This cannot be edited. Cancel the MIR and enter it again."})
    if errors:
        raise MirValidationError(errors)
    changed = 0
    with transaction.atomic():
        mir = Mir.objects.select_for_update().get(pk=mir.pk)
        window = edit_window(mir)
        if mir.status != "POSTED":
            raise MirValidationError([{"field": "status", "message": "A cancelled MIR cannot be edited."}])
        needs_window = [k for k in header if k not in ANYTIME_HEADER] + [k for f in lines.values() for k in f]
        if needs_window and not window["can_edit"]:
            raise MirValidationError([{"field": "status", "message": (
                f"The edit window closed on {window['edit_until']:%d-%m-%Y} ({EDIT_WINDOW_DAYS} days after entry). "
                "Only the SAP GRN number can still be filled in; anything else means cancelling and re-entering.")}])
        update = []
        for key, raw in header.items():
            if key == "invoice_date":
                errs: list[dict] = []
                value = _date(raw, key, errs)
                if errs or value is None:
                    raise MirValidationError(errs or [{"field": key, "message": "Required."}])
                if value > mir.mir_date:
                    raise MirValidationError([{"field": key, "message": "The invoice date cannot be after the MIR date."}])
                latest_po = max((d for d in mir.lines.values_list("po_line__purchase_order__po_date", flat=True) if d), default=None)
                if latest_po and value < latest_po and mir.invoice_date >= latest_po:
                    raise MirValidationError([{"field": key, "message": (
                        f"That is before the PO date ({latest_po:%d-%m-%Y}), which needs a reason at entry - "
                        "cancel this MIR and enter it again with the right date.")}])
            else:
                value = _text(raw, EDITABLE_HEADER[key])
                if key == "invoice_no" and not rules.invoice_key(value):
                    raise MirValidationError([{"field": key, "message": "Required."}])
            old = getattr(mir, key)
            if old == value:
                continue
            _log(mir, None, key, old, value, reason, user)
            setattr(mir, key, value)
            update.append(key)
            if key == "invoice_no":
                mir.invoice_key = rules.invoice_key(value)
                update.append("invoice_key")
            if key == "invoice_date":
                mir.invoice_fy = rules.financial_year(value)
                update.append("invoice_fy")
            changed += 1
        if update:
            mir.save(update_fields=update)
        by_no = {ln.line_no: ln for ln in mir.lines.all()}
        for line_no, fields in lines.items():
            ln = by_no.get(int(line_no))
            if ln is None:
                raise MirValidationError([{"field": "lines", "message": f"This MIR has no line {line_no}."}])
            line_update = []
            for key, raw in fields.items():
                value = _text(raw, EDITABLE_LINE[key])
                if getattr(ln, key) == value:
                    continue
                _log(mir, ln, key, getattr(ln, key), value, reason, user)
                setattr(ln, key, value)
                line_update.append(key)
                changed += 1
            if line_update:
                ln.save(update_fields=line_update)
    if not changed:
        raise MirValidationError([{"field": "", "message": "Nothing was changed."}])
    return changed


def record_rejection(mir_line, user, qty_rejected, reason_code: str, note: str):
    """A rejection found after posting - typically the QC report, days after
    the gate. Raises the line's rejected quantity (never lowers it: a
    rejection wrongly recorded means cancelling and re-entering), within
    REJECTION_WINDOW_DAYS of the MIR date and up to the quantity received.
    The accepted quantity, and so the PO line's open quantity, follow at
    once (both are summed from qty_received - qty_rejected); the invoice's
    amounts stay as billed, and the rejection opens (or re-opens) a
    QTY_REJECTED mismatch for the purchase team's debit note or
    replacement."""
    from apps.core.models import Mir, MirLine, MirMismatch, MirReasonCode

    errors: list[dict] = []
    new_total = _dec(qty_rejected, "qty_rejected", errors, places=3)
    reasons = {r.code: r for r in MirReasonCode.objects.filter(kind="REJECTION")}
    reason = _reason(reason_code, "REJECTION", "reason", note, errors, reasons)
    if errors:
        raise MirValidationError(errors)
    with transaction.atomic():
        mir = Mir.objects.select_for_update().get(pk=mir_line.mir_id)
        line = MirLine.objects.select_for_update().get(pk=mir_line.pk)
        window = edit_window(mir)
        if mir.status != "POSTED":
            raise MirValidationError([{"field": "status", "message": "A cancelled MIR cannot be changed."}])
        if not window["can_reject"]:
            raise MirValidationError([{"field": "status", "message": (
                f"Rejections can be recorded up to {window['reject_until']:%d-%m-%Y} "
                f"({REJECTION_WINDOW_DAYS} days after the MIR date).")}])
        if new_total <= line.qty_rejected:
            raise MirValidationError([{"field": "qty_rejected", "message": (
                f"Enter the new total rejected, more than the {line.qty_rejected.normalize():f} already recorded. "
                "A rejection cannot be reduced here.")}])
        if new_total > line.qty_received:
            raise MirValidationError([{"field": "qty_rejected", "message": "Cannot be more than the quantity received."}])
        # The rejected material leaves the store - refused if it was issued.
        _stock_check(stock_service.check_mir_rejection, line, new_total)
        note = _text(note, 2000)
        _log(mir, line, "qty_rejected", f"{line.qty_rejected.normalize():f}", f"{new_total.normalize():f}",
             f"{reason.label}. {note}".strip(), user)
        line.qty_rejected = new_total
        line.save(update_fields=["qty_rejected"])
        pct = rules.pct_of(new_total, line.qty_received)
        mm = MirMismatch.objects.filter(mir_line=line, kind="QTY_REJECTED").first()
        if mm is None:
            MirMismatch.objects.create(mir=mir, mir_line=line, kind="QTY_REJECTED", expected=Decimal("0"), actual=new_total,
                                       difference_pct=pct, reason=reason, note=note)
        else:
            mm.actual, mm.difference_pct, mm.reason, mm.note = new_total, pct, reason, note
            mm.status, mm.resolved_by, mm.resolved_by_email, mm.resolved_at, mm.resolution_note = "OPEN", None, "", None, ""
            mm.save()
    return line

