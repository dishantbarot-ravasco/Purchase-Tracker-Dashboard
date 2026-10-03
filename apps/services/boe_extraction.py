"""
Reading a Bill of Entry - the CHA's checklist, corrected until right (owner,
2026-10-03) - into an import shipment. The same pipeline as PO reading
(po_extraction.py: one structured-output call on the worker, a draft, a
person's review, approval): this module is the BOE's half - its fields, the
model's instructions, what blocks approval, what does not add up, and what
approval writes.

A BOE item is paired to the import PO line it clears (`po_line_id`). The
model reads only the document; the pairing is proposed here (the PO the file
is filed under first, then same HSN and the most words in common) and the
reviewer confirms or changes it. Approval writes an ImportShipment (source
APP) with one ImportShipmentLine per item; a shipment the import CSV already
made for the same BOE is taken over (the CSV then leaves it alone). The BOE's
licence section becomes `debits` - one row per (item, licence): quantity and
CIF value for an Advance Authorisation, duty foregone for a RoDTEP scrip -
written as LicenceDebit rows (apps/services/licences.py keeps the balances).
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

HEADER_FIELDS = (
    "boe_number", "boe_date", "bill_of_lading_number", "laden_on_board_date", "supplier_name", "po_numbers",
    "country_of_origin", "currency", "exchange_rate", "currency_after_taxes", "total_inclusive_value",
)
LINE_FIELDS = ("po_line_id", "po_number", "description", "hsn", "qty", "uom", "unit_price", "total_inclusive_value",
               "license_type", "license_number")
MODEL_LINE_FIELDS = tuple(f for f in LINE_FIELDS if f != "po_line_id")
REQUIRED_HEADER = ("boe_number", "boe_date", "bill_of_lading_number", "country_of_origin", "currency", "exchange_rate",
                   "total_inclusive_value")
REQUIRED_LINE = ("po_line_id", "description", "qty", "uom")
LICENSE_TYPES = ("", "ADVANCE", "RODTEP")
DEBIT_FIELDS = ("item", "license_type", "license_number", "qty", "value", "duty")
REQUIRED_DEBIT = ("item", "license_type", "license_number")

LABELS = {
    "boe_number": "BOE number", "boe_date": "BOE date", "bill_of_lading_number": "Bill of Lading number",
    "laden_on_board_date": "Laden on board date", "supplier_name": "Supplier", "po_numbers": "PO numbers printed",
    "country_of_origin": "Country of origin", "currency": "Invoice currency", "exchange_rate": "Exchange rate (customs)",
    "currency_after_taxes": "Currency after taxes", "total_inclusive_value": "Total payable (INR)",
    "po_line_id": "PO line", "po_number": "PO number", "description": "Material description", "hsn": "HSN",
    "qty": "Quantity (as per BOE)", "uom": "Unit", "unit_price": "Unit price (invoice currency)",
    "license_type": "Licence type", "license_number": "Licence number",
    "item": "Item no.", "value": "CIF value debited (INR)", "duty": "Duty foregone (INR)",
}

_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        **{f: _S for f in HEADER_FIELDS},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {**{f: _S for f in MODEL_LINE_FIELDS},
                               "license_type": {"type": "string", "enum": list(LICENSE_TYPES)}},
                "required": list(MODEL_LINE_FIELDS),
                "additionalProperties": False,
            },
        },
        "debits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {**{f: _S for f in DEBIT_FIELDS},
                               "license_type": {"type": "string", "enum": ["ADVANCE", "RODTEP"]}},
                "required": list(DEBIT_FIELDS),
                "additionalProperties": False,
            },
        },
        "notes": _S,
    },
    "required": [*HEADER_FIELDS, "lines", "debits", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read one Indian customs Bill of Entry for home consumption - usually the customs broker's (CHA's) checklist - for goods imported by Ravasco Transmission and Packing or Hindustan Rubbers, and return its fields exactly as the document states them.

Return every field of the schema. Use an empty string for anything the document does not state; never guess.

Header:
- boe_number: the Bill of Entry number (on a checklist, the BE number if allotted, else the job / checklist number as printed). boe_date: its date, YYYY-MM-DD.
- bill_of_lading_number: the BL / HAWB / MAWB number. laden_on_board_date: the shipped-on-board date if printed, YYYY-MM-DD; else empty.
- supplier_name: the overseas supplier. po_numbers: any purchase order numbers printed (invoice details, remarks), comma separated.
- country_of_origin: as printed (the country of origin of the goods).
- currency: the invoice currency (USD, EUR ...). exchange_rate: the customs exchange rate the BOE applies to that currency (INR per 1 unit), digits only.
- currency_after_taxes: the currency the duty is paid in (INR).
- total_inclusive_value: the total amount payable as printed - assessable value plus all duties - in INR.

Lines: one entry per item of the BOE, in the order printed.
- po_number: the purchase order number for this item if printed; else empty.
- description, hsn (the CTH / tariff item), qty and uom as assessed, unit_price in the invoice currency.
- total_inclusive_value: this item's assessable value plus its duties, in INR, if printed per item; else empty.
- license_type: "ADVANCE" for an Advance Authorisation, "RODTEP" for a RoDTEP scrip, empty when the item clears without a licence. license_number: the licence / scrip number(s) as printed, several separated by " / ".

debits: the BOE's licence section - one entry per (item, licence) debited. item: the item's position in your lines list (1 for the first). license_type ADVANCE or RODTEP; license_number as printed; qty: the quantity debited; value: the CIF / assessable value debited in INR (Advance); duty: the duty foregone in INR (the duty the licence covered). One item drawn on two licences is two entries. Empty when no licence was used.

Numbers: digits only with a decimal point - no currency symbols, no thousands separators, no units.

notes: anything a reviewer should check - a figure that is unreadable, a checklist marked provisional, items whose values do not add up. Empty if nothing."""


def _clean(value, limit=2000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _num(text):
    text = (text or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return "bad"


def _day(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return "bad"


def _ok(v):
    return None if v == "bad" else v


def _words(text) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def candidate_lines(ext):
    """The import PO lines a BOE item at this plant may clear: active lines of
    active import POs at the file's plant, the PO it is filed under first."""
    from apps.core.models import PurchaseOrderLine

    qs = (PurchaseOrderLine.objects.filter(purchase_order__plant=ext.plant, purchase_order__kind="import",
                                           purchase_order__is_active=True, is_active=True)
          .select_related("purchase_order").order_by("purchase_order__po_number", "line_no"))
    return list(qs)


def line_options(ext) -> list[dict]:
    return [{"value": str(ln.id), "label": f"PO {ln.purchase_order.po_number} line {ln.line_no} - {ln.description[:60]}"}
            for ln in candidate_lines(ext)]


def _propose(item, candidates, filed_po, printed_pos):
    """The likeliest PO line for one BOE item, or "" when nothing fits."""
    wanted_pos = {p for p in [item.get("po_number"), filed_po, *printed_pos] if p}
    hsn = re.sub(r"\D", "", item.get("hsn") or "")[:6]
    words = _words(item.get("description"))
    best, best_score = "", 0.0
    for ln in candidates:
        score = 0.0
        if ln.purchase_order.po_number in wanted_pos:
            score += 3
        if hsn and re.sub(r"\D", "", ln.hsn or "")[:6] == hsn:
            score += 2
        lw = _words(ln.description)
        if words and lw:
            score += 3 * len(words & lw) / len(words | lw)
        if score > best_score:
            best, best_score = str(ln.id), score
    return best if best_score >= 2 else ""


def normalize(raw, ext=None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    draft = {f: _clean(raw.get(f)) for f in HEADER_FIELDS}
    draft["currency"] = draft["currency"].upper()[:10]
    draft["currency_after_taxes"] = (draft["currency_after_taxes"] or "INR").upper()[:10]
    lines = raw.get("lines") if isinstance(raw.get("lines"), list) else []
    draft["lines"] = []
    for ln in lines[:200]:
        ln = ln if isinstance(ln, dict) else {}
        item = {f: _clean(ln.get(f)) for f in LINE_FIELDS}
        item["license_type"] = item["license_type"].upper() if item["license_type"].upper() in LICENSE_TYPES else ""
        draft["lines"].append(item)
    debits = raw.get("debits") if isinstance(raw.get("debits"), list) else []
    draft["debits"] = []
    for d in debits[:400]:
        d = d if isinstance(d, dict) else {}
        row = {f: _clean(d.get(f)) for f in DEBIT_FIELDS}
        row["license_type"] = row["license_type"].upper() if row["license_type"].upper() in ("ADVANCE", "RODTEP") else ""
        draft["debits"].append(row)
    draft["notes"] = _clean(raw.get("notes"))
    if ext is not None and not any(item["po_line_id"] for item in draft["lines"]):
        candidates = candidate_lines(ext)
        printed = [p.strip() for p in draft["po_numbers"].split(",") if p.strip()]
        for item in draft["lines"]:
            item["po_line_id"] = _propose(item, candidates, ext.document.po_number, printed)
    return draft


def problems(draft: dict, ext) -> list[dict]:
    out = []

    def add(field, message, line=None):
        out.append({"field": field, "line": line, "message": message})

    filed = ext.document.reference
    if filed and draft.get("boe_number") and "".join(draft["boe_number"].split()).upper() != "".join(filed.split()).upper():
        add("boe_number", f"The file was filed under BOE {filed} but this says {draft['boe_number']} - correct the number, "
                          "or withdraw the file and upload it under the right one.")
    for f in REQUIRED_HEADER:
        if not draft.get(f):
            add(f, f"{LABELS[f]} is missing.")
    for f in ("boe_date", "laden_on_board_date"):
        if _day(draft.get(f)) == "bad":
            add(f, f"{LABELS[f]} must be a date (YYYY-MM-DD).")
    for f in ("exchange_rate", "total_inclusive_value"):
        v = _num(draft.get(f))
        if v == "bad" or (v is not None and v <= 0):
            add(f, f"{LABELS[f]} is not a positive number.")
    lines = draft.get("lines") or []
    if not lines:
        add("lines", "The BOE has no items.")
    valid = {str(ln.id): ln for ln in candidate_lines(ext)}
    seen = set()
    for i, ln in enumerate(lines, start=1):
        for f in REQUIRED_LINE:
            if not ln.get(f):
                add(f, f"Item {i}: {LABELS[f]} is missing." if f != "po_line_id" else f"Item {i}: choose the PO line it clears.", i)
        if ln.get("po_line_id"):
            if ln["po_line_id"] not in valid:
                add("po_line_id", f"Item {i}: not an open import PO line at {ext.plant.name}.", i)
            elif ln["po_line_id"] in seen:
                add("po_line_id", f"Item {i}: that PO line is already on another item.", i)
            seen.add(ln["po_line_id"])
        for f in ("qty", "unit_price", "total_inclusive_value"):
            v = _num(ln.get(f))
            if v == "bad" or (v is not None and v < 0):
                add(f, f"Item {i}: {LABELS[f]} is not a number.", i)
        if _num(ln.get("qty")) == Decimal("0"):
            add("qty", f"Item {i}: Quantity must be more than zero.", i)
        if ln.get("license_type") and not ln.get("license_number"):
            add("license_number", f"Item {i}: the licence number is missing.", i)
        if ln.get("license_number") and not ln.get("license_type"):
            add("license_type", f"Item {i}: say whether it is an Advance licence or a RoDTEP scrip.", i)
    for j, d in enumerate(draft.get("debits") or [], start=1):
        for f in REQUIRED_DEBIT:
            if not d.get(f):
                add(f"debits.{f}", f"Licence debit {j}: {LABELS[f]} is missing.")
        item = _item_no(d.get("item"))
        if d.get("item") and (item is None or item > len(lines)):
            add("debits.item", f"Licence debit {j}: item {d.get('item')} is not on this BOE.")
        for f in ("qty", "value", "duty"):
            v = _num(d.get(f))
            if v == "bad" or (v is not None and v < 0):
                add(f"debits.{f}", f"Licence debit {j}: {LABELS[f]} is not a number.")
        need = "value" if d.get("license_type") == "ADVANCE" else "duty" if d.get("license_type") == "RODTEP" else None
        if need and not d.get(need):
            add(f"debits.{need}", f"Licence debit {j}: {LABELS[need]} is missing.")
    return out


def _item_no(text):
    try:
        n = int(str(text or "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def _debit_values(draft) -> list[dict]:
    return [{"item": _item_no(d.get("item")), "license_type": d.get("license_type", ""), "license_number": d.get("license_number", ""),
             "qty": _ok(_num(d.get("qty"))), "value": _ok(_num(d.get("value"))), "duty": _ok(_num(d.get("duty")))}
            for d in draft.get("debits") or [] if d.get("license_type") and d.get("license_number")]


def checks(draft: dict, ext) -> list[dict]:
    """What does not add up - warnings, never blocks."""
    from apps.services import mir_service

    out = []
    valid = {str(ln.id): ln for ln in candidate_lines(ext)}
    lines = draft.get("lines") or []
    totals = [_ok(_num(ln.get("total_inclusive_value"))) for ln in lines]
    header_total = _ok(_num(draft.get("total_inclusive_value")))
    if header_total is not None and totals and all(t is not None for t in totals) and abs(sum(totals) - header_total) > Decimal("1"):
        out.append({"check": "total", "line": None,
                    "message": f"The items add up to {sum(totals)} but the BOE total payable says {header_total}."})
    from apps.services import licences

    boe_date = _day(draft.get("boe_date"))
    existing = _existing(ext, draft)
    out += licences.check_debits(_debit_values(draft), lines, boe_date if isinstance(boe_date, datetime.date) else None,
                                 existing.id if existing else None)
    named = {i for i, ln in enumerate(lines, start=1) if ln.get("license_type")}
    debited_items = {d["item"] for d in _debit_values(draft)}
    for i in sorted(named - debited_items):
        out.append({"check": "licence_amount", "line": i,
                    "message": f"Item {i} names a licence but no debit amounts - add them, or the licence's balance will not show it."})
    accepted = mir_service.accepted_by_line([ln.id for ln in valid.values()])
    for i, item in enumerate(lines, start=1):
        po_line = valid.get(item.get("po_line_id") or "")
        if po_line is None:
            continue
        po = po_line.purchase_order
        if draft.get("currency") and po.currency and draft["currency"] != po.currency:
            out.append({"check": "currency", "line": i, "message": f"Item {i}: the BOE is in {draft['currency']} but PO {po.po_number} is in {po.currency}."})
        price = _ok(_num(item.get("unit_price")))
        if price is not None and po_line.rate is not None and price != po_line.rate:
            out.append({"check": "price", "line": i, "message": f"Item {i}: unit price {price} differs from PO {po.po_number}'s {po_line.rate}."})
        qty = _ok(_num(item.get("qty")))
        if qty is not None and po_line.qty_ordered is not None:
            left = po_line.qty_ordered - accepted.get(po_line.id, Decimal("0"))
            if qty > left:
                out.append({"check": "qty", "line": i, "message": f"Item {i}: {qty} on the BOE is more than the {left} still open on PO {po.po_number} line {po_line.line_no}."})
    return out


def _existing(ext, draft):
    from apps.core.models import ImportShipment

    rows = list(ImportShipment.objects.filter(plant=ext.plant, boe_number=draft.get("boe_number") or ""))
    bl = draft.get("bill_of_lading_number") or ""
    return next((r for r in rows if r.bill_of_lading_number == bl), rows[0] if rows else None)


def sheet_differences(draft: dict, ext) -> dict | None:
    """How the draft differs from the shipment the import PO sheet already
    made for this BOE. Compared, never added."""
    shipment = _existing(ext, draft)
    if shipment is None:
        return None
    diffs = []

    def cmp(label, sheet, pdf, numeric=False):
        if numeric:
            a, b = _ok(_num("" if sheet is None else str(sheet))), _ok(_num(pdf))
            same = (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= Decimal("0.01"))
        else:
            same = " ".join(str(sheet or "").lower().split()) == " ".join(str(pdf or "").lower().split())
        if not same:
            diffs.append({"field": label, "sheet": "" if sheet is None else str(sheet), "pdf": pdf or ""})

    cmp("Bill of Lading number", shipment.bill_of_lading_number, draft.get("bill_of_lading_number"))
    cmp("Exchange rate", shipment.exchange_rate, draft.get("exchange_rate"), numeric=True)
    cmp("Country of origin", shipment.country_of_origin, draft.get("country_of_origin"))
    sheet_lines = {str(sl.po_line_id): sl for sl in shipment.lines.filter(is_active=True)}
    for i, item in enumerate(draft.get("lines") or [], start=1):
        sl = sheet_lines.pop(item.get("po_line_id") or "", None)
        if sl is None:
            diffs.append({"field": f"Item {i}", "sheet": "not on the sheet", "pdf": item.get("description", "")})
            continue
        cmp(f"Item {i} quantity", sl.qty_as_per_boe, item.get("qty"), numeric=True)
        cmp(f"Item {i} licence", f"{sl.license_type} {sl.license_number}".strip(),
            f"{item.get('license_type', '')} {item.get('license_number', '')}".strip())
    for sl in sheet_lines.values():
        diffs.append({"field": f"PO line {sl.po_line.line_no}", "sheet": str(sl.qty_as_per_boe), "pdf": "not on this BOE"})
    return {"source": shipment.source, "differences": diffs}


@transaction.atomic
def approve(ext, draft: dict, user):
    """Write the reviewed draft as the plant's shipment. The caller has
    checked problems() is empty and the reading is READY."""
    from apps.core.models import ImportShipment, ImportShipmentLine, LicenceDebit
    from apps.services.license_links import normalize_license_number

    shipment = _existing(ext, draft)
    if shipment is None:
        shipment = ImportShipment(plant=ext.plant, boe_number=draft["boe_number"][:50])
    else:
        shipment = ImportShipment.objects.select_for_update().get(pk=shipment.pk)
    shipment.bill_of_lading_number = draft["bill_of_lading_number"][:100]
    shipment.boe_date = _day(draft["boe_date"])
    lob = _day(draft.get("laden_on_board_date"))
    shipment.laden_on_board_date = lob if isinstance(lob, datetime.date) else None
    shipment.country_of_origin = draft["country_of_origin"][:100]
    shipment.currency = draft["currency"][:10]
    shipment.currency_after_taxes = (draft.get("currency_after_taxes") or "INR")[:10]
    shipment.exchange_rate = _num(draft["exchange_rate"])
    shipment.total_inclusive_value = _num(draft["total_inclusive_value"])
    shipment.source = ImportShipment.Source.APP
    shipment.document = ext.document
    shipment.is_active = True
    shipment.save()
    kept = []
    for item in draft["lines"]:
        values = {"qty_as_per_boe": _num(item["qty"]), "total_inclusive_value": _ok(_num(item.get("total_inclusive_value"))),
                  "license_type": item.get("license_type", "")[:20], "license_number": item.get("license_number", "")[:100],
                  "is_active": True}
        line, made = ImportShipmentLine.objects.get_or_create(shipment=shipment, po_line_id=int(item["po_line_id"]), defaults=values)
        if not made:
            for f, v in values.items():
                setattr(line, f, v)
            line.save()
        kept.append(line.id)
    shipment.lines.exclude(id__in=kept).update(is_active=False)
    # This BOE's licence debits are exactly the approved reading's: earlier
    # ones (a previous approval of this BOE) are retired, never deleted.
    LicenceDebit.objects.filter(shipment_line__shipment=shipment, is_active=True).update(is_active=False)
    for d in _debit_values(draft):
        LicenceDebit.objects.create(shipment_line_id=kept[d["item"] - 1], license_type=d["license_type"],
                                    license_number=normalize_license_number(d["license_number"])[:50],
                                    qty=d["qty"], value_inr=d["value"], duty_foregone=d["duty"])
    return shipment
