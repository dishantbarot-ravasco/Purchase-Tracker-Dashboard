"""
RM stock entry (2026-09-29, project owner: "the Raw Material entry like we
have for MIR entry ... directly connecting with MIR entry"). The models are
apps/core/models/stock.py; the pure rules apps/services/stock_rules.py.

HOW STOCK COMES IN. A posted MIR line IS the receipt: mir_service.post_mir()
calls receive_mir(), which makes one StockLot per line at the RECEIVING
plant. The lot stores no quantity - it holds the MIR line's accepted
quantity (received less rejected, in the stock unit) while the MIR is
posted. So cancelling the MIR, or recording a rejection found later, changes
stock at once; and both are refused (check_mir_cancel(),
check_mir_rejection()) when the stock they would remove has already been
issued - that material cannot be un-received.

HOW STOCK GOES OUT AND BACK. Three documents, each numbered per plant, kind
and financial year, posted once and never edited (a wrong one is cancelled
and entered again, like a MIR):
  ISSUE   material to a department, drawn from the oldest lots first (FIFO)
          and valued at their rates;
  RETURN  unused material back to the store, against the issue it left on,
          back into the lots that issue drew from;
  ADJUST  what the books cannot explain: an opening balance, a physical
          count, damage, a sample. An editor's adjustment waits for an
          admin's approval and moves nothing until then; an admin's posts at
          once.

WHAT EVERY POSTING CHECKS. `evaluate()` is the one place a voucher is checked
and valued; the form previews through it and post_voucher() runs it again
inside the transaction with the lots locked. Beyond the fields themselves:
  - stock never goes below zero on ANY day (stock_rules.min_running_balance):
    an issue dated three days back must fit what the lot held then and every
    day since, not just today;
  - a voucher is dated today or up to BACKDATE_DAYS back, never ahead;
  - a return cannot exceed what its issue line still has out, and is dated
    on or after the issue;
  - cancelling a return, or an adjustment that added stock, is refused once
    that stock has been issued again.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.services import procurement_rules as prules
from apps.services import stock_rules as rules

MAX_LINES = 50
BACKDATE_DAYS = 7
KIND_CODES = {"ISSUE": "ISS", "RETURN": "RET", "ADJUST": "ADJ"}
ZERO = Decimal("0")
_LOT_RELATED = ("mir_line__mir", "voucher_line__voucher", "material", "vendor", "plant")


class StockValidationError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


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
        uom, factor = rules.stock_unit(po_line.uom)
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
    """[(lot_id, day, signed qty, voucher_no, voucher_id)] for posted vouchers."""
    from apps.core.models import StockAllocation

    qs = StockAllocation.objects.filter(lot_id__in=list(lot_ids), voucher_line__voucher__status="POSTED")
    if exclude_voucher_ids:
        qs = qs.exclude(voucher_line__voucher_id__in=list(exclude_voucher_ids))
    return [(lot_id, day, qty * direction, vno, vid) for lot_id, day, direction, qty, vno, vid in qs.values_list(
        "lot_id", "voucher_line__voucher__voucher_date", "voucher_line__direction", "qty",
        "voucher_line__voucher__voucher_no", "voucher_line__voucher_id")]


def lot_events(lots, *, exclude_voucher_ids=(), in_override=None) -> dict:
    """{lot_id: [(date, signed qty), ...]}: the lot's receipt, then every
    posted voucher's draw or return. `in_override` {lot_id: qty} replaces a
    lot's receipt (a MIR being cancelled or rejected); `exclude_voucher_ids`
    leaves vouchers out (one being cancelled)."""
    events = {lot.id: [] for lot in lots}
    for lot in lots:
        received = in_override[lot.id] if in_override and lot.id in in_override else _lot_in(lot)
        if received:
            events[lot.id].append((lot.received_date, received))
    for lot_id, day, signed, _vno, _vid in _posted_allocations(events, exclude_voucher_ids):
        events[lot_id].append((day, signed))
    return events


def lot_balances(lots) -> dict:
    """{lot_id: {"in", "drawn", "returned", "balance"}} as of now."""
    out = {lot.id: {"in": _lot_in(lot), "drawn": ZERO, "returned": ZERO} for lot in lots}
    for lot_id, _day, signed, _vno, _vid in _posted_allocations(out):
        key = "returned" if signed > 0 else "drawn"
        out[lot_id][key] += abs(signed)
    for b in out.values():
        b["balance"] = b["in"] - b["drawn"] + b["returned"]
    return out


def _vouchers_drawing(lot_ids) -> list[str]:
    return sorted({vno for _l, _d, signed, vno, _v in _posted_allocations(lot_ids) if signed < 0})


def _removal_error(lot, where: str) -> dict:
    uom = lot.uom or "units"
    drawn = ", ".join(_vouchers_drawing([lot.id])[:5])
    return {"field": "stock", "message": (
        f"{lot.material.name}{where}: that stock has already been issued ({drawn}). Put it back first - "
        f"cancel or return the issue - or record the loss with an adjustment. Stock cannot go below zero ({uom}).")}


def check_mir_cancel(mir):
    """Refuse to cancel a MIR whose received stock has since been issued:
    without its receipt a lot would have issued material it never held.
    Called inside cancel_mir()'s transaction; locks the MIR's lots."""
    from apps.core.models import StockLot

    ids = list(StockLot.objects.select_for_update().filter(mir_line__mir=mir).order_by("id").values_list("id", flat=True))
    lots = list(StockLot.objects.filter(id__in=ids).select_related(*_LOT_RELATED))
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
    lot = StockLot.objects.select_related(*_LOT_RELATED).get(pk=lot_id)
    events = lot_events([lot], in_override={lot.id: _lot_in(lot, rejected_override=new_rejected)})
    if rules.min_running_balance(events[lot.id]) < 0:
        raise StockValidationError([_removal_error(lot, "")])


def mir_line_stock(mir) -> dict:
    """{mir_line_id: {"uom", "in", "balance", "stocked", "lotId"}} - what each
    line of a MIR put into stock and how much of it is still there."""
    from apps.core.models import StockLot

    lots = list(StockLot.objects.filter(mir_line__mir=mir).select_related(*_LOT_RELATED))
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


def stock_rows(plant_codes, *, material_id=None) -> list[dict]:
    """One row per plant, material and stock unit with any lot: quantity on
    hand, INR value, open lots, last receipt, the oldest stock still held,
    the minimum level and whether the plant stocks it."""
    from apps.core.models import StockLot

    qs = StockLot.objects.filter(plant__code__in=list(plant_codes)).select_related(*_LOT_RELATED)
    if material_id is not None:
        qs = qs.filter(material_id=material_id)
    lots = list(qs)
    bal = lot_balances(lots)
    settings = _settings({lot.plant_id for lot in lots}, {lot.material_id for lot in lots})
    rows: dict = {}
    for lot in lots:
        key = (lot.plant_id, lot.material_id, lot.uom)
        b = bal[lot.id]
        row = rows.get(key)
        if row is None:
            setting = settings.get((lot.plant_id, lot.material_id))
            row = rows[key] = {
                "plant": lot.plant, "material": lot.material, "uom": lot.uom, "qty": ZERO, "value": ZERO,
                "other_currency_qty": ZERO, "open_lots": 0, "lots": 0, "last_received": None, "oldest_held": None,
                "is_stocked": setting.is_stocked if setting else True,
                "min_level": setting.min_level if setting and setting.min_level_uom in ("", lot.uom) else None,
            }
        row["lots"] += 1
        row["qty"] += b["balance"]
        if b["balance"] > 0:
            row["open_lots"] += 1
            if lot.currency == "INR":
                row["value"] += rules.value(b["balance"], lot.rate)
            else:
                row["other_currency_qty"] += b["balance"]
            if row["oldest_held"] is None or lot.received_date < row["oldest_held"]:
                row["oldest_held"] = lot.received_date
        if b["in"] > 0 and (row["last_received"] is None or lot.received_date > row["last_received"]):
            row["last_received"] = lot.received_date
    out = list(rows.values())
    for row in out:
        row["below_min"] = row["min_level"] is not None and row["qty"] < row["min_level"]
    return sorted(out, key=lambda r: (r["plant"].id, r["material"].name.lower(), r["uom"]))


def _doc_of(lot) -> str:
    return lot.mir_line.mir.mir_no if lot.source == "MIR" else lot.voucher_line.voucher.voucher_no


def material_detail(plant, material, uom) -> dict:
    """A material's lots at a plant (in one stock unit) and its ledger: every
    receipt, issue, return and adjustment in date order with the running
    balance."""
    from apps.core.models import StockAllocation, StockLot

    lots = list(StockLot.objects.filter(plant=plant, material=material, uom=uom)
                .select_related(*_LOT_RELATED, "voucher_line__reason"))
    bal = lot_balances(lots)
    ledger = []
    for lot in lots:
        if lot.source == "MIR":
            # Shown even when it holds nothing - a cancelled MIR, or a receipt
            # that went straight to use - so the ledger explains itself.
            line = lot.mir_line
            posted = line.mir.status == "POSTED"
            accepted = rules.qty((line.qty_received - line.qty_rejected) * lot.factor) if posted else ZERO
            ledger.append({"date": lot.received_date, "doc": _doc_of(lot), "kind": "RECEIPT", "qty": accepted,
                           "detail": lot.vendor.name if lot.vendor else "", "counts": posted and lot.stocked,
                           "note": "" if posted and lot.stocked else ("MIR cancelled" if not posted else "Went straight to use - not stocked"),
                           "sort": (lot.received_date, 0, lot.id)})
        elif bal[lot.id]["in"] > 0:
            ledger.append({"date": lot.received_date, "doc": _doc_of(lot), "kind": "ADJUST", "qty": bal[lot.id]["in"],
                           "detail": lot.voucher_line.reason.label if lot.voucher_line.reason else "", "counts": True,
                           "note": "", "voucherId": lot.voucher_line.voucher_id, "sort": (lot.received_date, 0, lot.id)})
    allocs = (StockAllocation.objects.filter(lot__in=lots, voucher_line__voucher__status="POSTED")
              .select_related("voucher_line__voucher", "voucher_line__reason"))
    by_line: dict = {}
    for a in allocs:
        vl = a.voucher_line
        entry = by_line.setdefault(vl.id, {"line": vl, "qty": ZERO})
        entry["qty"] += a.qty
    for entry in by_line.values():
        vl = entry["line"]
        v = vl.voucher
        detail = v.department if v.kind == "ISSUE" else (vl.reason.label if vl.reason else "")
        ledger.append({"date": v.voucher_date, "doc": v.voucher_no, "kind": v.kind, "qty": entry["qty"] * vl.direction,
                       "detail": detail, "counts": True, "note": "", "voucherId": v.id, "sort": (v.voucher_date, 1, v.id)})
    ledger.sort(key=lambda e: e["sort"])
    running = ZERO
    for e in ledger:
        if e["counts"]:
            running += e["qty"]
        e["balance"] = running
        del e["sort"]
    return {"lots": [(lot, bal[lot.id]) for lot in sorted(lots, key=lambda x: (x.received_date, x.id))],
            "ledger": ledger, "setting": _settings([plant.id], [material.id]).get((plant.id, material.id))}


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


def _candidate_lots(plant, material, uom, *, lock):
    """The plant's stocked lots of a material in one stock unit, oldest first -
    the FIFO order. With lock=True they are row-locked, in id order so two
    postings never deadlock."""
    from apps.core.models import StockLot

    base = StockLot.objects.filter(plant=plant, material=material, uom=uom, stocked=True)
    if lock:
        list(base.select_for_update().order_by("id").values_list("id", flat=True))
    return list(base.select_related(*_LOT_RELATED).order_by("received_date", "id"))


def _draw(plant, material, uom, day, quantity, *, lock, field, errors, held):
    """FIFO draw of `quantity` on `day`: [(lot, qty)], each lot giving no more
    than it holds on that day and every day after (so a backdated draw can
    never take a lot negative in between). `held` accumulates this voucher's
    earlier draws per lot. Adds an error, and returns what could be drawn,
    when there is not enough."""
    lots = _candidate_lots(plant, material, uom, lock=lock)
    events = lot_events(lots)
    for lot_id, taken in held.items():
        if lot_id in events:
            events[lot_id].append((day, -taken))
    remaining = quantity
    picks = []
    for lot in lots:
        if remaining <= 0:
            break
        room = rules.min_running_balance(events[lot.id], day)
        take = min(room, remaining)
        if take > 0:
            picks.append((lot, take))
            events[lot.id].append((day, -take))
            held[lot.id] = held.get(lot.id, ZERO) + take
            remaining -= take
    if remaining > 0:
        # What this line could draw on its day, against what the store holds
        # today: more today means stock that arrived after the voucher date.
        on_day = quantity - remaining
        now = sum((b["balance"] for b in lot_balances(lots).values()), ZERO)
        unit = f" {uom}" if uom else ""
        msg = f"Only {on_day.normalize():f}{unit} of this can be taken on {day:%d-%m-%Y}"
        if now > on_day:
            msg += f" - {now.normalize():f}{unit} is in stock today, the rest arrived after that date"
        errors.append({"field": field, "message": msg + "."})
    return picks


def _book_on(plant, material, uom, day) -> Decimal:
    """What the books held at the end of `day`."""
    lots = _candidate_lots(plant, material, uom, lock=False)
    events = lot_events(lots)
    return sum((sum((q for d, q in events[lot.id] if d <= day), ZERO) for lot in lots), ZERO)


def _latest_rate(plant, material, uom):
    from apps.core.models import StockLot

    lot = (StockLot.objects.filter(plant=plant, material=material, uom=uom, rate__isnull=False, currency="INR")
           .order_by("-received_date", "-id").first())
    return lot.rate if lot else None


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
        errors.append({"field": "lines", "message": f"At most {MAX_LINES} lines on one voucher."})
        raw = raw[:MAX_LINES]
    return [r if isinstance(r, dict) else {} for r in raw]


def _material(raw, f, errors):
    from apps.core.models import Material

    mid = _int(raw.get("material_id"))
    material = Material.objects.filter(pk=mid).first() if mid is not None else None
    if material is None:
        errors.append({"field": f"{f}.material_id", "message": "Choose a material."})
    return material


def evaluate(payload: dict, *, lock: bool = False, earliest: datetime.date | None = None) -> dict:
    """Check and value a voucher. Returns {"ok", "errors", "notices", "kind",
    "plant", "voucher_date", "lines", ...}; each line carries its draws
    [(lot, qty)] and value. Never saves. With lock=True (posting only) every
    lot it reads is row-locked first. `earliest` widens the backdating window
    - only approve_adjustment() passes it, for an adjustment entered days
    before it was approved."""
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
    header = {"department": _text(payload.get("department"), 60), "issued_to": _text(payload.get("issued_to"), 120),
              "reference": _text(payload.get("reference"), 60), "remarks": _text(payload.get("remarks"), 2000)}
    result = {"kind": kind, "plant": plant, "voucher_date": day, "header": header, "errors": errors, "notices": notices,
              "lines": [], "return_of": None}
    if kind == "ISSUE":
        if not header["department"]:
            errors.append({"field": "department", "message": "Required."})
        if not header["issued_to"]:
            errors.append({"field": "issued_to", "message": "Required."})
        _evaluate_issue(result, raw_lines, lock=lock)
    elif kind == "RETURN":
        _evaluate_return(result, payload, raw_lines, lock=lock)
    else:
        _evaluate_adjust(result, raw_lines, lock=lock)
    result["ok"] = not errors
    return result


def _evaluate_issue(result, raw_lines, *, lock):
    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    seen, held = set(), {}
    for i, raw in enumerate(raw_lines):
        f = f"lines.{i}"
        material = _material(raw, f, errors)
        uom = _text(raw.get("uom"), 20).upper()
        quantity = _dec(raw.get("qty"), f"{f}.qty", errors, places=3)
        if quantity is not None and quantity <= 0:
            errors.append({"field": f"{f}.qty", "message": "Must be more than zero."})
            quantity = None
        out = {"index": i, "material": material, "uom": uom, "qty": quantity, "direction": -1, "draws": [], "value": None,
               "reason": None, "note": _text(raw.get("note"), 2000)}
        result["lines"].append(out)
        if material is None:
            continue
        if (material.id, uom) in seen:
            errors.append({"field": f"{f}.material_id", "message": "Already on this issue - enter the total on one line."})
            continue
        seen.add((material.id, uom))
        if plant is None or day is None or quantity is None:
            continue
        out["draws"] = _draw(plant, material, uom, day, quantity, lock=lock, field=f"{f}.qty", errors=errors, held=held)
        out["value"] = sum((rules.value(q, lot.rate) for lot, q in out["draws"] if lot.currency == "INR"), Decimal("0.00"))
        _notice_below_min(result, plant, material, uom, quantity)


def _notice_below_min(result, plant, material, uom, quantity):
    from apps.core.models import StockSetting

    setting = StockSetting.objects.filter(plant=plant, material=material).first()
    if not setting or setting.min_level is None or setting.min_level_uom not in ("", uom):
        return
    after = sum((b["balance"] for b in lot_balances(_candidate_lots(plant, material, uom, lock=False)).values()), ZERO) - quantity
    if after < setting.min_level:
        result["notices"].append(f"{material.name}: {after.normalize():f} {uom} will be left, below its minimum level of "
                                 f"{setting.min_level.normalize():f} {uom}. Tell the purchase team.")


def _evaluate_return(result, payload, raw_lines, *, lock):
    from apps.core.models import StockAllocation, StockReasonCode, StockVoucher, StockVoucherLine

    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    reasons = {r.code: r for r in StockReasonCode.objects.filter(kind="RETURN")}
    issue = StockVoucher.objects.filter(pk=_int(payload.get("return_of")), kind="ISSUE").select_related("plant").first()
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
        line = StockVoucherLine.objects.filter(pk=_int(raw.get("issue_line_id")), voucher=issue).select_related("material").first()
        quantity = _dec(raw.get("qty"), f"{f}.qty", errors, places=3)
        if quantity is not None and quantity <= 0:
            errors.append({"field": f"{f}.qty", "message": "Must be more than zero."})
            quantity = None
        note = _text(raw.get("note"), 2000)
        reason = _reason(raw.get("reason"), ("RETURN",), f"{f}.reason", note, errors, reasons)
        out = {"index": i, "material": line.material if line else None, "uom": line.uom if line else "", "qty": quantity,
               "direction": 1, "draws": [], "value": None, "reason": reason, "note": note, "return_of_line": line}
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
        # Back into the lots the issue drew, newest first, each no more than
        # it gave this issue line less what earlier returns already put back.
        allocs = list(StockAllocation.objects.filter(voucher_line=line).select_related(*("lot__" + r for r in _LOT_RELATED))
                      .order_by("-lot__received_date", "-lot_id"))
        if lock:
            from apps.core.models import StockLot
            list(StockLot.objects.select_for_update().filter(id__in=[a.lot_id for a in allocs]).order_by("id").values_list("id", flat=True))
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
        out["value"] = sum((rules.value(q, lot.rate) for lot, q in out["draws"] if lot.currency == "INR"), Decimal("0.00"))


def _evaluate_adjust(result, raw_lines, *, lock):
    from apps.core.models import StockReasonCode

    errors, plant, day = result["errors"], result["plant"], result["voucher_date"]
    reasons = {r.code: r for r in StockReasonCode.objects.filter(kind__in=("ADJUST_IN", "ADJUST_OUT"))}
    seen, held = set(), {}
    for i, raw in enumerate(raw_lines):
        f = f"lines.{i}"
        mode = raw.get("mode")
        if mode not in ("add", "remove", "count"):
            errors.append({"field": f"{f}.mode", "message": "Choose add, write off or count."})
            continue
        material = _material(raw, f, errors)
        uom = _text(raw.get("uom"), 20).upper()
        note = _text(raw.get("note"), 2000)
        out = {"index": i, "mode": mode, "material": material, "uom": uom, "qty": None, "direction": None, "draws": [],
               "value": None, "reason": None, "note": note, "rate": None, "counted": None, "book": None}
        result["lines"].append(out)
        if material is not None:
            if (material.id, uom) in seen:
                errors.append({"field": f"{f}.material_id", "message": "Already on this adjustment - one line per material."})
                continue
            seen.add((material.id, uom))
        if mode == "count":
            counted = _dec(raw.get("counted"), f"{f}.counted", errors, places=3)
            if counted is not None and counted < 0:
                errors.append({"field": f"{f}.counted", "message": "Cannot be negative."})
                counted = None
            out["counted"] = counted
            if material is None or plant is None or day is None or counted is None:
                continue
            book = _book_on(plant, material, uom, day)
            out["book"] = book
            diff = counted - book
            if diff == 0:
                errors.append({"field": f"{f}.counted", "message": "That is what the books hold - nothing to adjust."})
                continue
            out["qty"], out["direction"] = abs(diff), (1 if diff > 0 else -1)
        else:
            quantity = _dec(raw.get("qty"), f"{f}.qty", errors, places=3)
            if quantity is not None and quantity <= 0:
                errors.append({"field": f"{f}.qty", "message": "Must be more than zero."})
                quantity = None
            out["qty"], out["direction"] = quantity, (1 if mode == "add" else -1)
        direction = out["direction"]
        if direction is None:
            continue
        out["reason"] = _reason(raw.get("reason"), ("ADJUST_IN",) if direction > 0 else ("ADJUST_OUT",), f"{f}.reason",
                                note, errors, reasons)
        if direction > 0:
            rate = _dec(raw.get("rate"), f"{f}.rate", errors, required=False, places=4)
            if rate is not None and rate < 0:
                errors.append({"field": f"{f}.rate", "message": "Cannot be negative."})
                rate = None
            if rate is None and material is not None and plant is not None:
                rate = _latest_rate(plant, material, uom)
                if rate is None and not any(e["field"] == f"{f}.rate" for e in errors):
                    errors.append({"field": f"{f}.rate", "message": "No earlier receipt to value it at - enter the rate per unit."})
            out["rate"] = rate
            if not uom:
                errors.append({"field": f"{f}.uom", "message": "Choose the unit."})
            if out["qty"] is not None and rate is not None:
                out["value"] = rules.value(out["qty"], rate)
        elif material is not None and plant is not None and day is not None and out["qty"] is not None:
            out["draws"] = _draw(plant, material, uom, day, out["qty"], lock=lock, field=f"{f}.qty" if mode != "count" else f"{f}.counted",
                                 errors=errors, held=held)
            out["value"] = -sum((rules.value(q, lot.rate) for lot, q in out["draws"] if lot.currency == "INR"), Decimal("0.00"))


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
    """Validate and save a voucher in one transaction. An adjustment by
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
        now = timezone.now()
        voucher = StockVoucher.objects.create(
            plant=plant, kind=kind, fy=fy, seq=seq, voucher_no=rules.voucher_number(plant.mir_prefix, KIND_CODES[kind], fy, seq),
            voucher_date=result["voucher_date"], return_of=result["return_of"],
            status=StockVoucher.Status.PENDING if pending else StockVoucher.Status.POSTED,
            created_by=user, created_by_email=getattr(user, "email", ""),
            decided_by=None if pending or kind != "ADJUST" else user,
            decided_by_email="" if pending or kind != "ADJUST" else getattr(user, "email", ""),
            decided_at=None if pending or kind != "ADJUST" else now,
            decision_note="" if pending or kind != "ADJUST" else "Entered by an admin - no separate approval.",
            **result["header"],
        )
        _save_lines(voucher, result, apply=not pending)
    return voucher


def _save_lines(voucher, result, *, apply: bool):
    """The voucher's lines, and - when it moves stock now - their draws and
    the lots an addition creates."""
    from apps.core.models import StockAllocation, StockLot, StockVoucherLine

    for n, ln in enumerate(result["lines"], start=1):
        line = StockVoucherLine.objects.create(
            voucher=voucher, line_no=n, material=ln["material"], uom=ln["uom"], qty=ln["qty"], direction=ln["direction"],
            reason=ln["reason"], note=ln["note"], rate=ln.get("rate"), counted_qty=ln.get("counted"), book_qty=ln.get("book"),
            return_of_line=ln.get("return_of_line"),
        )
        if not apply:
            continue
        for lot, q in ln["draws"]:
            StockAllocation.objects.create(voucher_line=line, lot=lot, qty=q)
        if voucher.kind == "ADJUST" and ln["direction"] > 0:
            StockLot.objects.create(plant=voucher.plant, material=ln["material"], uom=ln["uom"], source=StockLot.Source.ADJUSTMENT,
                                    voucher_line=line, received_date=voucher.voucher_date, factor=1, rate=ln["rate"],
                                    currency="INR", stocked=True)


def _payload_of(voucher) -> dict:
    """A pending adjustment as the payload it was entered with, for
    re-checking at approval. A count keeps the difference it was entered
    with: the count was true on its day, whatever moved since."""
    lines = []
    for ln in voucher.lines.select_related("reason").order_by("line_no"):
        mode = "add" if ln.direction > 0 else "remove"
        lines.append({"mode": mode, "material_id": ln.material_id, "uom": ln.uom, "qty": str(ln.qty),
                      "reason": ln.reason.code if ln.reason else "", "note": ln.note,
                      "rate": str(ln.rate) if ln.rate is not None else ""})
    return {"kind": "ADJUST", "plant": voucher.plant.code, "voucher_date": voucher.voucher_date.isoformat(), "lines": lines}


def approve_adjustment(voucher, user, note: str):
    """An admin approves a pending adjustment: it is re-checked now (stock
    may have moved since it was entered) and, if it still fits, posted. The
    approval date is not a new voucher date - the stock moves on the day the
    adjustment was entered for, so it may no longer fit the backdating window
    and is checked against its own day instead."""
    from apps.core.models import StockVoucher

    with transaction.atomic():
        voucher = StockVoucher.objects.select_for_update().select_related("plant").get(pk=voucher.pk)
        if voucher.kind != "ADJUST" or voucher.status != "PENDING":
            raise StockValidationError([{"field": "status", "message": "Only an adjustment waiting for approval can be approved."}])
        if voucher.created_by_id is not None and voucher.created_by_id == getattr(user, "pk", None):
            raise StockValidationError([{"field": "status", "message": "Someone other than the person who entered it must approve it."}])
        result = evaluate(_payload_of(voucher), lock=True, earliest=voucher.voucher_date)
        if not result["ok"]:
            raise StockValidationError(result["errors"])
        for saved, ln in zip(voucher.lines.order_by("line_no"), result["lines"], strict=True):
            for lot, q in ln["draws"]:
                saved.allocations.create(lot=lot, qty=q)
            if ln["direction"] > 0:
                from apps.core.models import StockLot
                StockLot.objects.create(plant=voucher.plant, material=saved.material, uom=saved.uom, source=StockLot.Source.ADJUSTMENT,
                                        voucher_line=saved, received_date=voucher.voucher_date, factor=1, rate=saved.rate,
                                        currency="INR", stocked=True)
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
            raise StockValidationError([{"field": "status", "message": "Only an adjustment waiting for approval can be turned down."}])
        voucher.status = StockVoucher.Status.REJECTED
        voucher.decided_by, voucher.decided_by_email = user, getattr(user, "email", "")
        voucher.decided_at, voucher.decision_note = timezone.now(), note
        voucher.save(update_fields=["status", "decided_by", "decided_by_email", "decided_at", "decision_note"])
    return voucher


def cancel_voucher(voucher, user, reason: str):
    """Cancel a posted voucher (or withdraw a pending adjustment). Its draws
    stop counting at once. Refused when that would leave stock below zero on
    any day: a return, or an addition, whose stock has been issued again; and
    an issue with a posted return against it (cancel the return first)."""
    from apps.core.models import StockLot, StockVoucher

    reason = (reason or "").strip()
    if not reason:
        raise StockValidationError([{"field": "reason", "message": "Say why it is being cancelled."}])
    with transaction.atomic():
        voucher = StockVoucher.objects.select_for_update().get(pk=voucher.pk)
        if voucher.status not in ("POSTED", "PENDING"):
            raise StockValidationError([{"field": "status", "message": "This voucher is not posted."}])
        if voucher.status == "POSTED":
            if voucher.kind == "ISSUE":
                returns = list(voucher.returns.filter(status="POSTED").values_list("voucher_no", flat=True))
                if returns:
                    raise StockValidationError([{"field": "status", "message": (
                        "Material from this issue has come back on " + ", ".join(returns) + ". Cancel that return first.")}])
            gives = voucher.kind == "RETURN" or voucher.kind == "ADJUST"
            if gives:
                lot_ids = set(StockLot.objects.filter(allocations__voucher_line__voucher=voucher,
                                                      allocations__voucher_line__direction=1).values_list("id", flat=True))
                created = set(StockLot.objects.filter(voucher_line__voucher=voucher).values_list("id", flat=True))
                ids = sorted(lot_ids | created)
                list(StockLot.objects.select_for_update().filter(id__in=ids).order_by("id").values_list("id", flat=True))
                lots = list(StockLot.objects.filter(id__in=ids).select_related(*_LOT_RELATED))
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
