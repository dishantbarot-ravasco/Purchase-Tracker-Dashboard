"""
RM store entry. The models are apps/core/models/stock.py; the pure rules
apps/services/stock_rules.py.

THE MIR IS THE ONLY WAY IN (2026-09-30, project owner: "if a material has not
been entered in the MIR the RM can't issue it"). mir_service.post_mir() calls
receive_mir(), which makes one StockLot per line at the RECEIVING plant. The
lot stores no quantity - it holds the MIR line's accepted quantity (received
less rejected, in the stock unit) while the MIR is posted. So cancelling the
MIR, or recording a rejection found later, changes stock at once; and both
are refused (check_mir_cancel(), check_mir_rejection()) when the stock they
would remove has already been issued - that material cannot be un-received.
Nothing else creates stock: no opening balances or additions by hand. The
source ADJUSTMENT lots entered before this rule still count.

THE STOREKEEPER PICKS THE MIR ("in the RM the user will have the option to
select the MIR and issue the quantity etc all other data gets transferred
from the MIR data"). Every voucher line names one MIR receipt (a lot) and a
quantity; material, unit, vendor, rate and date all come from that receipt.
Three documents, each numbered per plant, kind and financial year, posted
once and never edited (a wrong one is cancelled and entered again, like a
MIR):
  ISSUE   material out of a MIR receipt to production;
  RETURN  unused material back, against the issue it left on, into the
          receipt it came from;
  ADJUST  a stock difference on one receipt - a physical count that differs
          from the books, or a write-off (damage, a sample). These are the
          store's "open mismatches": an editor's waits for an admin's
          approval and moves nothing until then; an admin's posts at once.

WHAT EVERY POSTING CHECKS. `evaluate()` is the one place a voucher is checked
and valued; the form previews through it and post_voucher() runs it again
inside the transaction with the lots locked. Beyond the fields themselves:
  - a receipt never goes below zero on ANY day (stock_rules.min_running_balance):
    an issue dated three days back must fit what the receipt held then and
    every day since, not just today;
  - a voucher is dated today or up to BACKDATE_DAYS back, never ahead, and
    never before the receipt came in;
  - a return cannot exceed what its issue line still has out, and is dated
    on or after the issue;
  - cancelling a return, or a count that found more, is refused once that
    stock has been issued again.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.services import materials
from apps.services import procurement_rules as prules
from apps.services import stock_rules as rules

MAX_LINES = 50
BACKDATE_DAYS = 7
KIND_CODES = {"ISSUE": "ISS", "RETURN": "RET", "ADJUST": "ADJ"}
ZERO = Decimal("0")
LOT_RELATED = ("mir_line__mir", "mir_line__po_line__purchase_order", "voucher_line__voucher", "material", "vendor",
                "plant", "bill_to_plant", "location")


class StockValidationError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def doc_of(lot) -> str:
    """The document a lot came in on: its MIR number (or, for an old
    opening balance, the adjustment's number)."""
    return lot.mir_line.mir.mir_no if lot.source == "MIR" else lot.voucher_line.voucher.voucher_no


# ── Receipts: the MIR side ────────────────────────────────────────────────


def is_stocked(plant, material) -> bool:
    from apps.core.models import StockSetting

    setting = StockSetting.objects.filter(plant=plant, material=material).first()
    return True if setting is None else setting.is_stocked


def receive_mir(mir) -> list:
    """One lot per line of a just-posted MIR (called inside post_mir()'s
    transaction). A line whose PO line names no material (a blank PO
    description) makes no lot: stock is kept per material."""
    from apps.core.models import StockLot

    lots = []
    for line in mir.lines.select_related("po_line__purchase_order", "po_line__material"):
        po_line = line.po_line
        if po_line.material_id is None:
            continue
        # In the material's base unit - KG, L, NOS or M - converted exactly
        # (MT into KG) or by its pack factor (ROLL into M); otherwise the MIR
        # line's own unit (stock_rules BASE UNITS, materials.unit_factor()) -
        # the unit snapshotted on the MIR line, the one its quantity is in.
        uom, factor = materials.unit_factor(po_line.material, line.uom or po_line.uom)
        po = po_line.purchase_order
        lots.append(StockLot.objects.create(
            plant=mir.plant, material=po_line.material, uom=uom, source=StockLot.Source.MIR, mir_line=line,
            received_date=mir.mir_date, vendor=mir.vendor, bill_to_plant=po.plant if po.plant_id != mir.plant_id else None,
            factor=factor, rate=rules.lot_rate(line.taxable, line.qty_received, factor), currency=po.currency or "INR",
            stocked=is_stocked(mir.plant, po_line.material), batch_no=line.batch_no,
        ))
    return lots


def _lot_in(lot, *, rejected_override=None) -> Decimal:
    """What a lot received, in stock units: nothing if it went straight to use
    or its document is not posted."""
    if not lot.stocked:
        return ZERO
    if lot.source == "MIR":
        line = lot.mir_line
        if line.mir.status != "POSTED":
            return ZERO
        rejected = line.qty_rejected if rejected_override is None else rejected_override
        return rules.qty((line.qty_received - rejected) * lot.factor)
    vl = lot.voucher_line
    return vl.qty if vl.voucher.status == "POSTED" else ZERO


def _posted_allocations(lot_ids, exclude_voucher_ids=()):
    """[(lot_id, day, signed qty, kind, voucher_no, voucher_id, line)] for
    posted vouchers."""
    from apps.core.models import StockAllocation

    qs = (StockAllocation.objects.filter(lot_id__in=list(lot_ids), voucher_line__voucher__status="POSTED")
          .select_related("voucher_line__voucher", "voucher_line__reason"))
    if exclude_voucher_ids:
        qs = qs.exclude(voucher_line__voucher_id__in=list(exclude_voucher_ids))
    out = []
    for a in qs:
        vl = a.voucher_line
        v = vl.voucher
        out.append((a.lot_id, v.voucher_date, a.qty * vl.direction, v.kind, v.voucher_no, v.id, vl))
    return out


def lot_events(lots, *, exclude_voucher_ids=(), in_override=None) -> dict:
    """{lot_id: [(date, signed qty), ...]}: the lot's receipt, then every
    posted voucher's take or give-back. `in_override` {lot_id: qty} replaces
    a lot's receipt (a MIR being cancelled or rejected); `exclude_voucher_ids`
    leaves vouchers out (one being cancelled)."""
    events = {lot.id: [] for lot in lots}
    for lot in lots:
        received = in_override[lot.id] if in_override and lot.id in in_override else _lot_in(lot)
        if received:
            events[lot.id].append((lot.received_date, received))
    for lot_id, day, signed, *_rest in _posted_allocations(events, exclude_voucher_ids):
        events[lot_id].append((day, signed))
    return events


_TOTAL_KEY = {"ISSUE": "issued", "RETURN": "returned", "ADJUST": "adjusted"}


def lot_balances(lots) -> dict:
    """{lot_id: {"in", "issued", "returned", "adjusted", "balance"}} as of
    now. `issued` is positive; `adjusted` is the net of stock differences."""
    out = {lot.id: {"in": _lot_in(lot), "issued": ZERO, "returned": ZERO, "adjusted": ZERO} for lot in lots}
    for lot_id, _day, signed, kind, *_rest in _posted_allocations(out):
        key = _TOTAL_KEY[kind]
        out[lot_id][key] += -signed if key == "issued" else signed
    for b in out.values():
        b["balance"] = b["in"] - b["issued"] + b["returned"] + b["adjusted"]
    return out


def _vouchers_drawing(lot_ids) -> list[str]:
    return sorted({vno for _l, _d, signed, _k, vno, *_rest in _posted_allocations(lot_ids) if signed < 0})


def _removal_error(lot, where: str) -> dict:
    uom = lot.uom or "units"
    drawn = ", ".join(_vouchers_drawing([lot.id])[:5])
    return {"field": "stock", "message": (
        f"{lot.material.name}{where}: that stock has already been issued ({drawn}). Put it back first - "
        f"cancel or return the issue - or record the loss as a stock difference. Stock cannot go below zero ({uom}).")}


def check_mir_cancel(mir):
    """Refuse to cancel a MIR whose received stock has since been issued:
    without its receipt a lot would have issued material it never held.
    Called inside cancel_mir()'s transaction; locks the MIR's lots."""
    from apps.core.models import StockLot

    ids = list(StockLot.objects.select_for_update().filter(mir_line__mir=mir).order_by("id").values_list("id", flat=True))
    lots = list(StockLot.objects.filter(id__in=ids).select_related(*LOT_RELATED))
    events = lot_events(lots, in_override={lot.id: ZERO for lot in lots})
    errors = [_removal_error(lot, f" (line {lot.mir_line.line_no})") for lot in lots
              if rules.min_running_balance(events[lot.id]) < 0]
    if errors:
        raise StockValidationError(errors)


def check_mir_rejection(mir_line, new_rejected):
    """Refuse a rejection found after posting when the stock it would take
    out has already been issued. Called inside record_rejection()'s
    transaction; locks the lot."""
    from apps.core.models import StockLot

    lot_id = StockLot.objects.select_for_update().filter(mir_line=mir_line).values_list("id", flat=True).first()
    if lot_id is None:
        return
    lot = StockLot.objects.select_related(*LOT_RELATED).get(pk=lot_id)
    events = lot_events([lot], in_override={lot.id: _lot_in(lot, rejected_override=new_rejected)})
    if rules.min_running_balance(events[lot.id]) < 0:
        raise StockValidationError([_removal_error(lot, "")])


def mir_line_stock(mir) -> dict:
    """{mir_line_id: {"uom", "in", "balance", "stocked", "lotId"}} - what each
    line of a MIR put into stock and how much of it is still there."""
    from apps.core.models import StockLot

    lots = list(StockLot.objects.filter(mir_line__mir=mir).select_related(*LOT_RELATED))
    bal = lot_balances(lots)
    return {lot.mir_line_id: {"uom": lot.uom, "in": bal[lot.id]["in"], "balance": bal[lot.id]["balance"],
                              "stocked": lot.stocked, "lotId": lot.id} for lot in lots}


# ── Reading stock ─────────────────────────────────────────────────────────


def _settings(plant_ids, material_ids=None) -> dict:
    from apps.core.models import StockSetting

    qs = StockSetting.objects.filter(plant_id__in=list(plant_ids))
    if material_ids is not None:
        qs = qs.filter(material_id__in=list(material_ids))
    return {(s.plant_id, s.material_id): s for s in qs}


def _search(qs, q: str):
    """Narrow a StockLot queryset to a MIR number, material, vendor,
    invoice, PO number, item code or store location."""
    q = (q or "").strip()
    if not q:
        return qs
    return qs.filter(Q(mir_line__mir__mir_no__icontains=q) | Q(material__name__icontains=q) | Q(vendor__name__icontains=q)
                     | Q(mir_line__mir__invoice_no__icontains=q) | Q(mir_line__po_line__purchase_order__po_number__icontains=q)
                     | Q(mir_line__po_line__item_code__icontains=q) | Q(location__name__icontains=q))


def receipts_for_issue(plant_codes, q: str = "", *, limit: int = 80) -> list:
    """[(lot, balance)] - the MIR receipts with stock left at these plants,
    for the issue form's picker: the ones matching `q` (all of them when `q`
    is empty), oldest MIR first so the oldest stock is the obvious pick. The
    form takes its plant from the MIR picked, so it searches every plant the
    storekeeper may issue at.

    Read in batches until `limit` receipts with stock are found: a fixed
    cap on the oldest lots, taken before the balance filter, filled up with
    fully issued receipts once a plant had a few thousand and newer stock
    vanished from the picker."""
    from apps.core.models import StockLot

    qs = _search(StockLot.objects.filter(plant__code__in=list(plant_codes), stocked=True, source="MIR",
                                         mir_line__mir__status="POSTED"), q)
    qs = qs.select_related(*LOT_RELATED).order_by("received_date", "mir_line__mir__mir_no", "mir_line__line_no", "id")
    out: list = []
    start = 0
    while len(out) < limit:
        lots = list(qs[start:start + _PICKER_BATCH])
        if not lots:
            break
        bal = lot_balances(lots)
        out += [(lot, bal[lot.id]["balance"]) for lot in lots if bal[lot.id]["balance"] > 0]
        start += _PICKER_BATCH
    return out[:limit]


_PICKER_BATCH = 500


def _receipt_kind(lot) -> str:
    return "RECEIPT" if lot.source == "MIR" else "OPENING"


def _movements(lots) -> dict:
    """{lot_id: [{"date", "kind", "qty" (signed), "doc", "voucherId", "line"}]}:
    the receipt (when it counts) and every posted voucher, unsorted."""
    out = {lot.id: [] for lot in lots}
    for lot in lots:
        received = _lot_in(lot)
        if received:
            out[lot.id].append({"date": lot.received_date, "kind": _receipt_kind(lot), "qty": received, "doc": doc_of(lot),
                                "voucherId": None, "line": None})
    for lot_id, day, signed, kind, vno, vid, vl in _posted_allocations(out):
        out[lot_id].append({"date": day, "kind": kind, "qty": signed, "doc": vno, "voucherId": vid, "line": vl})
    return out


def register_rows(plant_codes, date_from: datetime.date, date_to: datetime.date, *, q: str = "", category: str = "",
                  include_empty: bool = False, location: str = "") -> list[dict]:
    """The RM register, one row per MIR receipt, like the store's own Stock
    sheet: what it held at the start of the period (opening), what came in,
    went out, came back and was adjusted within it, and what it held at the
    end (closing), with its rate, value and days in store. A receipt that
    held nothing all period and moved nothing is left out unless
    `include_empty`; so is one that went straight to use."""
    from apps.core.models import StockLot

    qs = StockLot.objects.filter(plant__code__in=list(plant_codes), received_date__lte=date_to)
    if category:
        qs = qs.filter(material__category=category)
    if location:
        qs = qs.filter(location__name_key=location_key(location))
    lots = list(_search(qs, q).select_related(*LOT_RELATED))
    moves = _movements(lots)
    today = timezone.localdate()
    as_of = min(date_to, today)
    rows = []
    for lot in lots:
        mv = moves[lot.id]
        opening = sum((m["qty"] for m in mv if m["date"] < date_from), ZERO)
        within = [m for m in mv if date_from <= m["date"] <= date_to]
        totals = {"received": ZERO, "issued": ZERO, "returned": ZERO, "adjusted": ZERO}
        for m in within:
            if m["kind"] in ("RECEIPT", "OPENING"):
                totals["received"] += m["qty"]
            else:
                key = _TOTAL_KEY[m["kind"]]
                totals[key] += -m["qty"] if key == "issued" else m["qty"]
        closing = opening + sum((m["qty"] for m in within), ZERO)
        if not include_empty and (not lot.stocked or (opening == 0 and closing == 0 and not within)):
            continue
        issues = [m["date"] for m in mv if m["kind"] == "ISSUE"]
        rows.append({"lot": lot, "opening": opening, "closing": closing, **totals,
                     "value": rules.value(closing, lot.rate) if closing > 0 and lot.currency == "INR" else Decimal("0.00"),
                     "days": (as_of - lot.received_date).days, "last_issued": max(issues) if issues else None})
    rows.sort(key=lambda r: ((r["lot"].material.category or "~").lower(), r["lot"].material.name.lower(),
                             r["lot"].received_date, r["lot"].id))
    return rows


def lot_detail(lot) -> dict:
    """One MIR receipt's story: its balances and every movement in date
    order with the running balance, plus the plant's setting for the
    material."""
    bal = lot_balances([lot])[lot.id]
    moves = sorted(_movements([lot])[lot.id], key=lambda m: (m["date"], m["kind"] not in ("RECEIPT", "OPENING"), m["voucherId"] or 0))
    running = ZERO
    for m in moves:
        running += m["qty"]
        m["balance"] = running
        vl = m.pop("line")
        v = vl.voucher if vl else None
        if v is None:
            m["detail"] = lot.vendor.name if lot.vendor else ""
        elif v.kind == "ISSUE":
            m["detail"] = ", ".join(x for x in (v.department, v.issued_to, v.reference) if x)
        else:
            m["detail"] = vl.reason.label if vl.reason else ""
    return {"balances": bal, "movements": moves,
            "setting": _settings([lot.plant_id], [lot.material_id]).get((lot.plant_id, lot.material_id))}


def differences(plant_codes, status: str = "OPEN") -> list:
    """Stock differences (ADJUST lines), newest first - the store's open
    mismatches. OPEN is waiting for an admin; RESOLVED is approved or turned
    down; CANCELLED was withdrawn or cancelled; ALL is everything."""
    from apps.core.models import StockVoucherLine

    qs = (StockVoucherLine.objects.filter(voucher__kind="ADJUST", voucher__plant__code__in=list(plant_codes))
          .select_related("voucher__plant", "reason", "material", *("lot__" + r for r in LOT_RELATED)))
    wanted = {"OPEN": ("PENDING",), "RESOLVED": ("POSTED", "REJECTED"), "CANCELLED": ("CANCELLED",)}.get(status)
    if wanted:
        qs = qs.filter(voucher__status__in=wanted)
    return list(qs.order_by("-voucher__voucher_date", "-voucher_id", "line_no")[:500])


def update_setting(plant, material, user, *, is_stocked, min_level, min_level_uom):
    from apps.core.models import StockSetting

    errors: list[dict] = []
    level = None
    if min_level not in (None, ""):
        level = _dec(min_level, "min_level", errors, places=3)
        if level is not None and level < 0:
            errors.append({"field": "min_level", "message": "Cannot be negative."})
    if errors:
        raise StockValidationError(errors)
    setting, _ = StockSetting.objects.get_or_create(plant=plant, material=material)
    setting.is_stocked = bool(is_stocked)
    setting.min_level = level
    setting.min_level_uom = (min_level_uom or "").strip().upper()[:20] if level is not None else ""
    setting.updated_by, setting.updated_by_email = user, getattr(user, "email", "")
    setting.save()
    return setting


# ── Parsing ───────────────────────────────────────────────────────────────


def _dec(value, field, errors, *, required=True, places=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.append({"field": field, "message": "Required."})
        return None
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        errors.append({"field": field, "message": "Not a number."})
        return None
    if not d.is_finite():
        errors.append({"field": field, "message": "Not a number."})
        return None
    if places is not None and d != d.quantize(Decimal(1).scaleb(-places)):
        errors.append({"field": field, "message": f"At most {places} decimal places."})
        return None
    return d


def _text(value, limit) -> str:
    return str(value or "").strip()[:limit]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive(raw, key, f, errors):
    quantity = _dec(raw.get(key), f"{f}.{key}", errors, places=3)
    if quantity is not None and quantity <= 0:
        errors.append({"field": f"{f}.{key}", "message": "Must be more than zero."})
        return None
    return quantity


def _reason(code, kinds, field, note, errors, reasons):
    if not code:
        errors.append({"field": field, "message": "Choose a reason."})
        return None
    reason = reasons.get(code)
    if reason is None or reason.kind not in kinds or not reason.is_active:
        errors.append({"field": field, "message": "Not a reason for this."})
        return None
    if reason.note_required and not (note or "").strip():
        errors.append({"field": field.replace("reason", "note"), "message": "This reason needs a note."})
    return reason


# ── Evaluate (preview and post share this) ────────────────────────────────


def _lock_lots(lot_ids):
    """Row-lock lots in id order, so two postings never deadlock."""
    from apps.core.models import StockLot

    list(StockLot.objects.select_for_update().filter(id__in=sorted(lot_ids)).order_by("id").values_list("id", flat=True))


def _receipt(raw, f, plant, errors, seen):
    """The MIR receipt a line names, checked: at this plant, held in store,
    its MIR posted, and not already on this voucher."""
    from apps.core.models import StockLot

    lot_id = _int(raw.get("lot_id"))
    lot = StockLot.objects.filter(pk=lot_id).select_related(*LOT_RELATED).first() if lot_id is not None else None
    field = f"{f}.lot_id"
    if lot is None:
        errors.append({"field": field, "message": "Choose the MIR it comes from."})
        return None
    if plant is not None and lot.plant_id != plant.id:
        errors.append({"field": field, "message": f"{doc_of(lot)} was received at {lot.plant.name} - its stock is there."})
        return None
    if not lot.stocked:
        errors.append({"field": field, "message": f"{doc_of(lot)} went straight to use - nothing of it is held in store."})
        return None
    if _lot_in(lot) == 0:
        errors.append({"field": field, "message": f"{doc_of(lot)} is cancelled - it holds no stock."})
        return None
    if lot.id in seen:
        errors.append({"field": field, "message": f"{doc_of(lot)} line {lot.mir_line.line_no if lot.mir_line_id else ''} "
                                                  "is already on this entry - enter the total on one line."})
        return None
    seen.add(lot.id)
    return lot


def _room(lot, day) -> Decimal:
    """How much can leave this receipt on `day` without it going below zero
    on that day or any day after."""
    return rules.min_running_balance(lot_events([lot])[lot.id], day)


def _take(lot, day, quantity, field, errors) -> list:
    """[(lot, quantity)] when the receipt can give it on `day`, else an
    error saying what it can give."""
    unit = f" {lot.uom}" if lot.uom else ""
    if day < lot.received_date:
        errors.append({"field": field, "message": f"{doc_of(lot)} came in on {lot.received_date:%d-%m-%Y} - nothing of it "
                                                  "can go out before that."})
        return []
    room = _room(lot, day)
    if quantity > room:
        now = lot_balances([lot])[lot.id]["balance"]
        msg = f"Only {room.normalize():f}{unit} of {doc_of(lot)} can go out on {day:%d-%m-%Y}"
        if now != room:
            msg += f" ({now.normalize():f}{unit} is left today)"
        errors.append({"field": field, "message": msg + "."})
        return []
    return [(lot, quantity)]


def _material_on_hand(plant, material, uom) -> Decimal:
    from apps.core.models import StockLot

    lots = list(StockLot.objects.filter(plant=plant, material=material, uom=uom, stocked=True).select_related(*LOT_RELATED))
    return sum((b["balance"] for b in lot_balances(lots).values()), ZERO)


def _header(payload, errors, *, today, earliest):
    from apps.core.models import Plant

    plant = Plant.objects.filter(code=payload.get("plant")).first()
    if plant is None:
        errors.append({"field": "plant", "message": "Choose the plant."})
    day = None
    raw = payload.get("voucher_date")
    if not raw:
        errors.append({"field": "voucher_date", "message": "Required."})
    else:
        try:
            day = datetime.date.fromisoformat(str(raw))
        except ValueError:
            errors.append({"field": "voucher_date", "message": "Not a date (YYYY-MM-DD)."})
    if day and day > today:
        errors.append({"field": "voucher_date", "message": "Cannot be a future date."})
        day = None
    elif day and day < earliest:
        errors.append({"field": "voucher_date", "message": f"Can be dated at most {BACKDATE_DAYS} days back."})
        day = None
    return plant, day


def _raw_lines(payload, errors):
    raw = payload.get("lines") or []
    if not isinstance(raw, list) or not raw:
        errors.append({"field": "lines", "message": "Add at least one line."})
        return []
    if len(raw) > MAX_LINES:
        errors.append({"field": "lines", "message": f"At most {MAX_LINES} lines on one entry."})
        raw = raw[:MAX_LINES]
    return [r if isinstance(r, dict) else {} for r in raw]


def _inr_value(draws) -> Decimal | None:
    """The INR value of what a line moves; None when it moves nothing yet
    (the quantity does not fit), so the form shows no figure."""
    if not draws:
        return None
    return sum((rules.value(q, lot.rate) for lot, q in draws if lot.currency == "INR"), Decimal("0.00"))


def evaluate(payload: dict, *, lock: bool = False, earliest: datetime.date | None = None) -> dict:
    """Check and value a voucher. Returns {"ok", "errors", "notices", "kind",
    "plant", "voucher_date", "lines", ...}; each line carries its receipt
    (`lot`), its draws [(lot, qty)] and value. Never saves. With lock=True
    (posting only) every lot it names is row-locked first. `earliest` widens
    the backdating window - only approve_adjustment() passes it, for a
    difference entered days before it was approved."""
    kind = payload.get("kind")
    errors: list[dict] = []
    notices: list[str] = []
    if kind not in KIND_CODES:
        return {"ok": False, "errors": [{"field": "kind", "message": "Unknown voucher kind."}], "notices": [], "kind": kind,
                "plant": None, "voucher_date": None, "lines": []}
    today = timezone.localdate()
    if earliest is None:
        earliest = today - datetime.timedelta(days=BACKDATE_DAYS)
    plant, day = _header(payload, errors, today=today, earliest=earliest)
    raw_lines = _raw_lines(payload, errors)
    if lock:
        _lock_lots({i for i in (_int(r.get("lot_id")) for r in raw_lines) if i is not None})
    header = {"department": _text(payload.get("department"), 60), "issued_to": _text(payload.get("issued_to"), 120),
              "reference": _text(payload.get("reference"), 60), "remarks": _text(payload.get("remarks"), 2000)}
    result = {"kind": kind, "plant": plant, "voucher_date": day, "header": header, "errors": errors, "notices": notices,
              "lines": [], "return_of": None}
    if kind == "ISSUE":
        _evaluate_issue(result, raw_lines)
    elif kind == "RETURN":
        _evaluate_return(result, payload, raw_lines, lock=lock)
    else:
        _evaluate_adjust(result, raw_lines)
    result["ok"] = not errors
    return result


def _line(i, lot, **extra) -> dict:
    return {"index": i, "lot": lot, "material": lot.material if lot else None, "uom": lot.uom if lot else "", "qty": None,
            "direction": -1, "draws": [], "value": None, "reason": None, "note": "", **extra}


def _evaluate_issue(result, raw_lines):
    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    seen: set = set()
    for i, raw in enumerate(raw_lines):
        f = f"lines.{i}"
        lot = _receipt(raw, f, plant, errors, seen)
        out = _line(i, lot, qty=_positive(raw, "qty", f, errors), note=_text(raw.get("note"), 2000))
        result["lines"].append(out)
        if lot is None or day is None or out["qty"] is None:
            continue
        out["draws"] = _take(lot, day, out["qty"], f"{f}.qty", errors)
        out["value"] = _inr_value(out["draws"])
        if out["draws"]:
            _notice_below_min(result, lot, out["qty"])


def _notice_below_min(result, lot, quantity):
    from apps.core.models import StockSetting

    setting = StockSetting.objects.filter(plant=lot.plant, material=lot.material).first()
    if not setting or setting.min_level is None or setting.min_level_uom not in ("", lot.uom):
        return
    after = _material_on_hand(lot.plant, lot.material, lot.uom) - quantity
    if after < setting.min_level:
        result["notices"].append(f"{lot.material.name}: {after.normalize():f} {lot.uom} will be left, below its minimum level of "
                                 f"{setting.min_level.normalize():f} {lot.uom}. Tell the purchase team.")


def _evaluate_return(result, payload, raw_lines, *, lock):
    from apps.core.models import StockAllocation, StockReasonCode, StockVoucher, StockVoucherLine

    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    reasons = {r.code: r for r in StockReasonCode.objects.filter(kind="RETURN")}
    issues = StockVoucher.objects.filter(pk=_int(payload.get("return_of")), kind="ISSUE").select_related("plant")
    # Posting locks the issue row, the one cancel_voucher() locks: otherwise
    # a return and the issue's cancellation could both commit, and the return
    # would add back stock that never left the store.
    issue = (issues.select_for_update(of=("self",)) if lock else issues).first()
    if issue is None:
        errors.append({"field": "return_of", "message": "Choose the issue the material is coming back from."})
        return
    result["return_of"] = issue
    if issue.status != "POSTED":
        errors.append({"field": "return_of", "message": f"{issue.voucher_no} is cancelled - nothing to return against it."})
        return
    if plant is not None and issue.plant_id != plant.id:
        errors.append({"field": "return_of", "message": f"{issue.voucher_no} was issued at {issue.plant.name}; return it there."})
        return
    if day and day < issue.voucher_date:
        errors.append({"field": "voucher_date", "message": f"Cannot be before the issue ({issue.voucher_date:%d-%m-%Y})."})
    seen = set()
    for i, raw in enumerate(raw_lines):
        f = f"lines.{i}"
        line = (StockVoucherLine.objects.filter(pk=_int(raw.get("issue_line_id")), voucher=issue)
                .select_related("material", *("lot__" + r for r in LOT_RELATED)).first())
        quantity = _positive(raw, "qty", f, errors)
        note = _text(raw.get("note"), 2000)
        reason = _reason(raw.get("reason"), ("RETURN",), f"{f}.reason", note, errors, reasons)
        out = {"index": i, "lot": line.lot if line else None, "material": line.material if line else None,
               "uom": line.uom if line else "", "qty": quantity, "direction": 1, "draws": [], "value": None,
               "reason": reason, "note": note, "return_of_line": line}
        result["lines"].append(out)
        if line is None:
            errors.append({"field": f"{f}.issue_line_id", "message": f"Not a line of {issue.voucher_no}."})
            continue
        if line.id in seen:
            errors.append({"field": f"{f}.issue_line_id", "message": "This issue line is already on the return."})
            continue
        seen.add(line.id)
        if quantity is None:
            continue
        # Back into the receipts the issue took from, newest first, each no
        # more than it gave this issue line less what earlier returns put back.
        allocs = list(StockAllocation.objects.filter(voucher_line=line).select_related(*("lot__" + r for r in LOT_RELATED))
                      .order_by("-lot__received_date", "-lot_id"))
        if lock:
            _lock_lots({a.lot_id for a in allocs})
        back = {}
        for a in StockAllocation.objects.filter(voucher_line__return_of_line=line, voucher_line__voucher__status="POSTED"):
            back[a.lot_id] = back.get(a.lot_id, ZERO) + a.qty
        still_out = line.qty - sum(back.values(), ZERO)
        if quantity > still_out:
            errors.append({"field": f"{f}.qty", "message": (
                f"At most {still_out.normalize():f} {line.uom} of this is still out on {issue.voucher_no}.")})
            continue
        remaining = quantity
        for a in allocs:
            room = a.qty - back.get(a.lot_id, ZERO)
            take = min(room, remaining)
            if take > 0:
                out["draws"].append((a.lot, take))
                remaining -= take
            if remaining <= 0:
                break
        out["value"] = _inr_value(out["draws"])


# A stock difference on one receipt: "count" (what was counted left of it),
# "remove" (a write-off) or "gain" (more than the books - what an approved
# count that found more is re-checked as).
ADJUST_MODES = ("count", "remove", "gain")


def _evaluate_adjust(result, raw_lines):
    from apps.core.models import StockReasonCode

    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    reasons = {r.code: r for r in StockReasonCode.objects.filter(kind__in=("ADJUST_IN", "ADJUST_OUT"))}
    seen: set = set()
    for i, raw in enumerate(raw_lines):
        f = f"lines.{i}"
        mode = raw.get("mode")
        if mode not in ADJUST_MODES:
            errors.append({"field": f"{f}.mode", "message": "Choose a physical count or a write-off. Stock comes in only through a MIR."})
            continue
        lot = _receipt(raw, f, plant, errors, seen)
        note = _text(raw.get("note"), 2000)
        out = _line(i, lot, mode=mode, direction=None, note=note, counted=None, book=None)
        result["lines"].append(out)
        if mode == "count":
            counted = _dec(raw.get("counted"), f"{f}.counted", errors, places=3)
            if counted is not None and counted < 0:
                errors.append({"field": f"{f}.counted", "message": "Cannot be negative."})
                counted = None
            out["counted"] = counted
            if lot is None or day is None or counted is None:
                continue
            book = sum((q for d, q in lot_events([lot])[lot.id] if d <= day), ZERO)
            out["book"] = book
            diff = counted - book
            if diff == 0:
                errors.append({"field": f"{f}.counted", "message": "That is what the books hold - no difference to record."})
                continue
            out["qty"], out["direction"] = abs(diff), (1 if diff > 0 else -1)
        else:
            out["qty"], out["direction"] = _positive(raw, "qty", f, errors), (1 if mode == "gain" else -1)
        if out["direction"] is None:
            continue
        out["reason"] = _reason(raw.get("reason"), ("ADJUST_IN",) if out["direction"] > 0 else ("ADJUST_OUT",), f"{f}.reason",
                                note, errors, reasons)
        if lot is None or day is None or out["qty"] is None:
            continue
        if out["direction"] < 0:
            out["draws"] = _take(lot, day, out["qty"], f"{f}.counted" if mode == "count" else f"{f}.qty", errors)
            value = _inr_value(out["draws"])
            out["value"] = -value if value is not None else None
        elif day < lot.received_date:
            errors.append({"field": f"{f}.lot_id", "message": f"{doc_of(lot)} came in on {lot.received_date:%d-%m-%Y}."})
        else:
            out["draws"] = [(lot, out["qty"])]
            out["value"] = _inr_value(out["draws"])


# ── Writing ───────────────────────────────────────────────────────────────


def _next_seq(plant, kind, fy) -> int:
    from apps.core.models import StockSequence

    seq, _ = StockSequence.objects.select_for_update().get_or_create(plant=plant, kind=kind, fy=fy)
    seq.last_seq += 1
    seq.save(update_fields=["last_seq"])
    return seq.last_seq


def _is_admin(user) -> bool:
    return getattr(user, "role", "") == "admin"


def post_voucher(payload: dict, user):
    """Validate and save a voucher in one transaction. A stock difference by
    anyone but an admin is saved PENDING, moving no stock until approved.
    Raises StockValidationError; returns the StockVoucher."""
    from apps.core.models import StockVoucher

    with transaction.atomic():
        result = evaluate(payload, lock=True)
        if not result["ok"]:
            raise StockValidationError(result["errors"])
        plant, kind = result["plant"], result["kind"]
        fy = prules.financial_year(result["voucher_date"])
        seq = _next_seq(plant, kind, fy)
        pending = kind == "ADJUST" and not _is_admin(user)
        decided = kind == "ADJUST" and not pending
        voucher = StockVoucher.objects.create(
            plant=plant, kind=kind, fy=fy, seq=seq, voucher_no=rules.voucher_number(plant.mir_prefix, KIND_CODES[kind], fy, seq),
            voucher_date=result["voucher_date"], return_of=result["return_of"],
            status=StockVoucher.Status.PENDING if pending else StockVoucher.Status.POSTED,
            created_by=user, created_by_email=getattr(user, "email", ""),
            decided_by=user if decided else None, decided_by_email=getattr(user, "email", "") if decided else "",
            decided_at=timezone.now() if decided else None,
            decision_note="Entered by an admin - no separate approval." if decided else "",
            **result["header"],
        )
        _save_lines(voucher, result, apply=not pending)
    return voucher


def _save_lines(voucher, result, *, apply: bool):
    """The voucher's lines and - when it moves stock now - their draws."""
    from apps.core.models import StockAllocation, StockVoucherLine

    for n, ln in enumerate(result["lines"], start=1):
        line = StockVoucherLine.objects.create(
            voucher=voucher, line_no=n, lot=ln["lot"], material=ln["material"], uom=ln["uom"], qty=ln["qty"],
            direction=ln["direction"], reason=ln["reason"], note=ln["note"], counted_qty=ln.get("counted"),
            book_qty=ln.get("book"), return_of_line=ln.get("return_of_line"),
        )
        if apply:
            for lot, q in ln["draws"]:
                StockAllocation.objects.create(voucher_line=line, lot=lot, qty=q)


def _payload_of(voucher) -> dict:
    """A pending difference as the payload it was entered with, for
    re-checking at approval. A count keeps the difference it was entered
    with: the count was true on its day, whatever moved since."""
    lines = []
    for ln in voucher.lines.select_related("reason").order_by("line_no"):
        lines.append({"mode": "gain" if ln.direction > 0 else "remove", "lot_id": ln.lot_id, "qty": str(ln.qty),
                      "reason": ln.reason.code if ln.reason else "", "note": ln.note})
    return {"kind": "ADJUST", "plant": voucher.plant.code, "voucher_date": voucher.voucher_date.isoformat(), "lines": lines}


def approve_adjustment(voucher, user, note: str):
    """An admin approves a pending stock difference: it is re-checked now
    (stock may have moved since it was entered) and, if it still fits,
    posted. The stock moves on the day the difference was entered for, so it
    may be older than the backdating window and is checked against its own
    day instead."""
    from apps.core.models import StockVoucher

    with transaction.atomic():
        voucher = StockVoucher.objects.select_for_update().select_related("plant").get(pk=voucher.pk)
        if voucher.kind != "ADJUST" or voucher.status != "PENDING":
            raise StockValidationError([{"field": "status", "message": "Only a difference waiting for approval can be approved."}])
        if voucher.created_by_id is not None and voucher.created_by_id == getattr(user, "pk", None):
            raise StockValidationError([{"field": "status", "message": "Someone other than the person who entered it must approve it."}])
        if voucher.lines.filter(lot__isnull=True).exists():
            raise StockValidationError([{"field": "status", "message": (
                "This was entered before differences named their MIR - turn it down and enter it again against the MIR.")}])
        result = evaluate(_payload_of(voucher), lock=True, earliest=voucher.voucher_date)
        if not result["ok"]:
            raise StockValidationError(result["errors"])
        for saved, ln in zip(voucher.lines.order_by("line_no"), result["lines"], strict=True):
            for lot, q in ln["draws"]:
                saved.allocations.create(lot=lot, qty=q)
        voucher.status = StockVoucher.Status.POSTED
        voucher.decided_by, voucher.decided_by_email = user, getattr(user, "email", "")
        voucher.decided_at, voucher.decision_note = timezone.now(), (note or "").strip()
        voucher.save(update_fields=["status", "decided_by", "decided_by_email", "decided_at", "decision_note"])
    return voucher


def reject_adjustment(voucher, user, note: str):
    from apps.core.models import StockVoucher

    note = (note or "").strip()
    if not note:
        raise StockValidationError([{"field": "note", "message": "Say why it is not approved."}])
    with transaction.atomic():
        voucher = StockVoucher.objects.select_for_update().get(pk=voucher.pk)
        if voucher.kind != "ADJUST" or voucher.status != "PENDING":
            raise StockValidationError([{"field": "status", "message": "Only a difference waiting for approval can be turned down."}])
        voucher.status = StockVoucher.Status.REJECTED
        voucher.decided_by, voucher.decided_by_email = user, getattr(user, "email", "")
        voucher.decided_at, voucher.decision_note = timezone.now(), note
        voucher.save(update_fields=["status", "decided_by", "decided_by_email", "decided_at", "decision_note"])
    return voucher


def cancel_voucher(voucher, user, reason: str):
    """Cancel a posted voucher (or withdraw a pending difference). Its draws
    stop counting at once. Refused when that would leave a receipt below
    zero on any day: a return, a count that found more, or an old opening
    balance whose stock has been issued again; and an issue with a posted
    return against it (cancel the return first)."""
    from apps.core.models import StockLot, StockVoucher

    reason = (reason or "").strip()
    if not reason:
        raise StockValidationError([{"field": "reason", "message": "Say why it is being cancelled."}])
    with transaction.atomic():
        voucher = StockVoucher.objects.select_for_update().get(pk=voucher.pk)
        if voucher.status not in ("POSTED", "PENDING"):
            raise StockValidationError([{"field": "status", "message": "This entry is not posted."}])
        if voucher.status == "POSTED":
            if voucher.kind == "ISSUE":
                returns = list(voucher.returns.filter(status="POSTED").values_list("voucher_no", flat=True))
                if returns:
                    raise StockValidationError([{"field": "status", "message": (
                        "Material from this issue has come back on " + ", ".join(returns) + ". Cancel that return first.")}])
            else:
                given = set(StockLot.objects.filter(allocations__voucher_line__voucher=voucher,
                                                    allocations__voucher_line__direction=1).values_list("id", flat=True))
                created = set(StockLot.objects.filter(voucher_line__voucher=voucher).values_list("id", flat=True))
                ids = sorted(given | created)
                _lock_lots(ids)
                lots = list(StockLot.objects.filter(id__in=ids).select_related(*LOT_RELATED))
                events = lot_events(lots, exclude_voucher_ids=[voucher.id], in_override={i: ZERO for i in created})
                bad = [lot for lot in lots if rules.min_running_balance(events[lot.id]) < 0]
                if bad:
                    raise StockValidationError([_removal_error(lot, "") for lot in bad])
        voucher.status = StockVoucher.Status.CANCELLED
        voucher.cancelled_by, voucher.cancelled_by_email = user, getattr(user, "email", "")
        voucher.cancelled_at, voucher.cancel_reason = timezone.now(), reason
        voucher.save(update_fields=["status", "cancelled_by", "cancelled_by_email", "cancelled_at", "cancel_reason"])
    return voucher


# ── For the forms ─────────────────────────────────────────────────────────


def returnable_lines(issue) -> list[dict]:
    """An issue's lines with what is still out on each (issued less posted
    returns) - what the Return form offers."""
    from apps.core.models import StockAllocation

    out = []
    for line in issue.lines.select_related("material").order_by("line_no"):
        back = sum((a.qty for a in StockAllocation.objects.filter(voucher_line__return_of_line=line,
                                                                     voucher_line__voucher__status="POSTED")), ZERO)
        out.append({"line": line, "returned": back, "still_out": line.qty - back})
    return out


def departments(plant, limit: int = 30) -> list[str]:
    """Departments this plant has issued to, most used first - the issue
    form's suggestions."""
    from django.db.models import Count

    from apps.core.models import StockVoucher

    rows = (StockVoucher.objects.filter(plant=plant, kind="ISSUE").exclude(department="")
            .values("department").annotate(n=Count("id")).order_by("-n", "department")[:limit])
    return [r["department"] for r in rows]


# ── Where a receipt sits (owner, 2026-10-03) ──────────────────────────────
LOCATION_MAX = 60


def location_key(name: str) -> str:
    return " ".join((name or "").split()).upper()


def locations(plant) -> list[str]:
    """The plant's store locations, by name - the location picker's list."""
    from apps.core.models import StockLocation

    return list(StockLocation.objects.filter(plant=plant).values_list("name", flat=True))


@transaction.atomic
def set_location(lot, name: str, user):
    """Say where in its plant's store a receipt sits. A name the plant has
    not used before becomes one of its locations (case and spacing do not
    make a new one); a blank name clears it. Records who and when."""
    from apps.core.models import StockLocation, StockLot

    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    name = " ".join((name or "").split())
    if len(name) > LOCATION_MAX:
        raise StockValidationError([{"field": "location", "message": f"At most {LOCATION_MAX} characters."}])
    location = None
    if name:
        location, _ = StockLocation.objects.get_or_create(
            plant_id=lot.plant_id, name_key=location_key(name),
            defaults={"name": name, "created_by_email": getattr(user, "email", "") or ""})
    lot.location = location
    lot.location_set_by_email = getattr(user, "email", "") or ""
    lot.location_set_at = timezone.now()
    lot.save(update_fields=["location", "location_set_by_email", "location_set_at"])
    return lot


def plant_holdings(plant, *, window_days: int = 45) -> list[dict]:
    """Every receipt the plant keeps in store, with what it holds today and
    how much of it was issued (net of returns) in the last `window_days` -
    what the Inventory / On Order / Stock & Orders tabs read when the plant's
    stock source is the app (apps/services/app_stock_source.py)."""
    from apps.core.models import StockLot

    lots = list(StockLot.objects.filter(plant=plant, stocked=True).select_related(*LOT_RELATED))
    moves = _movements(lots)
    since = timezone.localdate() - datetime.timedelta(days=window_days - 1)
    out = []
    for lot in lots:
        mv = moves[lot.id]
        out.append({
            "lot": lot,
            "balance": sum((m["qty"] for m in mv), ZERO),
            "used": -sum((m["qty"] for m in mv if m["kind"] in ("ISSUE", "RETURN") and m["date"] >= since), ZERO),
        })
    return out
