"""
PO extraction (owner, 2026-10-03): an uploaded PO file is read into every
field of the order and each of its lines, reviewed by a purchase manager, and
only then written to the procurement tables that MIR entry receives against.

The flow:
  1. A PO file is uploaded (documents.upload_po()); request() makes a
     PoExtraction row (QUEUED) and queues run() on the background worker -
     never inside the request.
  2. run() sends the file to Claude in ONE structured-output call (no tools,
     no database access - the model only reads the document) and stores the
     reply as `extracted` and, normalized, as `draft` (READY), or the reason
     it could not (FAILED).
  3. review() shows the draft, what does not add up on it
     (procurement_rules.po_checks()), what is still missing, which plant its
     billing address names, and - when the PO sheet already holds the order -
     how the two differ. The two are compared, never added.
  4. approve() takes the reviewer's corrected draft and writes it: a new
     PurchaseOrder, or the PO sheet's row of the same number taken over
     (source APP, so the CSV projection no longer writes it), its lines
     diffed through procurement_sync.write_lines(). Refused unless the
     billing address names the uploading plant - a PO belongs to the plant
     it is billed to.

Line identity is the line's position on the PO (line N is the PO's Nth
item), as in the CSV projection, so an order moving from the sheet to the
app keeps its lines and their receipts. The PO's own item code is kept on
the line and may be blank.
"""

from __future__ import annotations

import base64
import datetime
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.services import boe_extraction, documents, materials, rematch
from apps.services import procurement_rules as rules
from apps.services.procurement_sync import ProjectionResult, upsert_vendor, write_lines

log = logging.getLogger(__name__)

MAX_TOKENS = 16000
MAX_LINES = 200

HEADER_FIELDS = (
    "order_type", "po_number", "po_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_sap_code",
    "billing_address", "shipping_address", "payment_terms", "incoterms", "currency", "tax_type",
    "total_value", "tax_amount", "total_inclusive_value", "remarks",
)
LINE_FIELDS = ("item_code", "description", "hsn", "qty", "uom", "rate", "net_value", "delivery_date", "remarks")
ORDER_TYPES = ("domestic", "import")
# Compulsory on approval (owner: everything but the remarks; the item code
# too may be blank - not every PO numbers its items). An import order has
# no GST at order time - its tax, landed total and HSN come per shipment
# from the Bill of Entry - and a supplier abroad has no GSTIN.
REQUIRED_HEADER = {
    "domestic": tuple(f for f in HEADER_FIELDS if f not in ("remarks", "tax_amount")),
    "import": tuple(f for f in HEADER_FIELDS
                    if f not in ("remarks", "tax_amount", "vendor_gstin", "tax_type", "total_inclusive_value")),
}
REQUIRED_LINE = {
    "domestic": ("description", "hsn", "qty", "uom", "rate", "net_value", "delivery_date"),
    "import": ("description", "qty", "uom", "rate", "net_value", "delivery_date"),
}

LABELS = {
    "order_type": "Order type", "po_number": "PO number", "po_date": "PO date", "vendor_name": "Vendor name", "vendor_address": "Vendor address",
    "vendor_gstin": "Vendor GSTIN", "vendor_email": "Vendor email", "vendor_sap_code": "Vendor SAP code",
    "billing_address": "Billing address", "shipping_address": "Shipping address", "payment_terms": "Payment terms",
    "incoterms": "Incoterms", "currency": "Currency", "tax_type": "Tax type", "total_value": "Total value",
    "tax_amount": "Tax amount", "total_inclusive_value": "Total including tax", "remarks": "Remarks",
    "item_code": "Item code", "description": "Material description", "hsn": "HSN", "qty": "Quantity", "uom": "Unit",
    "rate": "Net price", "net_value": "Net value", "delivery_date": "Delivery date",
}

_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        **{f: _S for f in HEADER_FIELDS},
        "order_type": {"type": "string", "enum": list(ORDER_TYPES)},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {f: _S for f in LINE_FIELDS},
                "required": list(LINE_FIELDS),
                "additionalProperties": False,
            },
        },
        "notes": _S,
    },
    "required": [*HEADER_FIELDS, "lines", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read one purchase order issued by Ravasco Transmission and Packing or Hindustan Rubbers to a supplier, and return its fields exactly as the document states them.

Return every field of the schema. Use an empty string for anything the document does not state; never guess, infer from other documents, or fill in a typical value.

Header:
- order_type: "import" when the supplier is outside India (a foreign address, no GSTIN, a foreign currency, incoterms such as FOB / CIF / CFR); otherwise "domestic".
- po_number: the purchase order number as printed, without labels ("PO No.") or revision words.
- po_date: the PO's own date (created / issued), as YYYY-MM-DD.
- vendor_*: the SUPPLIER's name, address, GSTIN, email and the supplier/vendor code the buyer's system assigns (often labelled Vendor Code or Supplier Code). Never the buyer's own details.
- billing_address: the Bill To / Invoice To address (the buying plant), in full. shipping_address: the Ship To / Deliver To address, in full.
- payment_terms, incoterms, currency (ISO code, e.g. INR, USD): as printed.
- tax_type: IGST, CGST+SGST or CGST+UGST, as the PO applies tax; "mixed" if lines differ.
- total_value: the total before tax. tax_amount: the total tax. total_inclusive_value: the grand total including tax.
- remarks: notes or special instructions printed for the supplier, briefly.

Lines: one entry per ordered item, in the order printed. Do not merge identical items and do not split one item.
- item_code: the item / material code printed for the line; empty if none.
- description: the material description in full, including grade, size and specification.
- hsn: the HSN / SAC code. uom: the unit as printed (KG, MT, NOS, ROLL, L, M ...).
- qty, rate (price per unit before tax), net_value (line value before tax), delivery_date (YYYY-MM-DD; the header delivery date when the line has none).
- Madura or other conveyor-belt fabric ordered in rolls with GSM, width and length printed: give qty as the weight in KG = GSM x width (m) x length (m) x number of rolls / 1000, uom KG, and keep the roll count, GSM, width and length in the description ("NN-400 fabric roll, width 178cm, GSM 720, length 210m, 5 rolls, total weight 1682.100kgs"). With no GSM printed, never invent one: keep qty in ROLL as printed.

Import orders: give currency, prices and totals in the PO's own currency as printed; leave vendor_gstin, tax_type, tax_amount and total_inclusive_value empty unless the PO itself prints them - duty, IGST and the landed value come later from the Bill of Entry. HSN as printed, empty if not.

Numbers: digits only with a decimal point - no currency symbols, no thousands separators, no units (12345.50, not "Rs. 12,345.50").

notes: anything a reviewer should check - an unreadable figure, totals that do not add up, a second page missing, a handwritten correction. Empty if nothing."""


class ExtractionError(ValueError):
    """Shown to the user as it is (a ValueError, like DocumentError)."""


def is_configured() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


# ── Queueing and running ──────────────────────────────────────────────────


def request(document, user):
    """Queue a reading of an uploaded PO file. Returns the PoExtraction."""
    from apps.core.models import Document, PoExtraction

    if document.kind not in (Document.Kind.PO, Document.Kind.BOE):
        raise ExtractionError("Only a PO copy or a Bill of Entry is read.")
    if document.status == Document.Status.WITHDRAWN:
        raise ExtractionError("This file was withdrawn.")
    if not is_configured():
        raise ExtractionError("PO extraction is not set up on this server (ANTHROPIC_API_KEY).")
    kind = PoExtraction.Kind.BOE if document.kind == Document.Kind.BOE else PoExtraction.Kind.PO
    ext = PoExtraction.objects.create(document=document, plant=document.plant, kind=kind,
                                      requested_by_email=getattr(user, "email", "") or "")
    if rematch._inline():
        transaction.on_commit(lambda: run(ext.id))
    else:
        from django_q.tasks import async_task

        transaction.on_commit(lambda: async_task("apps.services.po_extraction.run", ext.id))
    return ext


def _call_claude(data: bytes, content_type: str, schema=None, prompt=None, ask="Read this purchase order.") -> dict:
    """One structured-output call: the file in, the schema's JSON out.
    Returns {"json", "model", "input_tokens", "output_tokens"}; raises
    ExtractionError with a readable reason."""
    import json

    import anthropic

    b64 = base64.standard_b64encode(data).decode("ascii")
    if content_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64}}
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=600.0, max_retries=2)
    try:
        response = client.beta.messages.create(
            model=settings.PO_EXTRACTION_MODEL,
            max_tokens=MAX_TOKENS,
            # A safety decline on this model re-runs the request on a fallback
            # model inside the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": prompt or SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema or SCHEMA}},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": ask}]}],
        )
    except anthropic.BadRequestError as exc:
        raise ExtractionError(f"The file could not be read: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError("The extraction service is busy - try again in a few minutes.") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"The extraction service answered {exc.status_code} - try again later.") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError("The extraction service could not be reached - try again later.") from exc
    if response.stop_reason == "refusal":
        raise ExtractionError("The extraction service declined to read this file.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("The PO is too long to read in one go - enter it by hand.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ExtractionError("The extraction came back unreadable - try again.") from exc
    return {"json": parsed, "model": response.model, "input_tokens": response.usage.input_tokens or 0,
            "output_tokens": response.usage.output_tokens or 0}


def run(extraction_id: int) -> None:
    """The worker task: read the file, store the draft. Never raises - a
    failure is recorded on the row for the page to show."""
    from apps.core.models import PoExtraction

    ext = PoExtraction.objects.select_related("document").filter(pk=extraction_id, status="QUEUED").first()
    if ext is None:
        return
    ext.status = PoExtraction.Status.RUNNING
    ext.save(update_fields=["status"])
    try:
        data = documents.read_bytes(ext.document)
        if ext.kind == PoExtraction.Kind.BOE:
            result = _call_claude(data, ext.document.content_type, boe_extraction.SCHEMA, boe_extraction.SYSTEM_PROMPT,
                                  "Read this Bill of Entry.")
            draft = boe_extraction.normalize(result["json"], ext)
        else:
            result = _call_claude(data, ext.document.content_type)
            draft = normalize(result["json"])
        ext.extracted = result["json"]
        ext.draft = draft
        ext.model_name = result["model"][:60]
        ext.input_tokens, ext.output_tokens = result["input_tokens"], result["output_tokens"]
        ext.status = PoExtraction.Status.READY
        ext.error = ""
    except ExtractionError as exc:
        ext.status, ext.error = PoExtraction.Status.FAILED, str(exc)
    except Exception as exc:  # the worker must record, not crash
        log.exception("PO extraction %s failed", extraction_id)
        ext.status, ext.error = PoExtraction.Status.FAILED, f"Unexpected error: {type(exc).__name__}"
    ext.finished_at = timezone.now()
    ext.save()


# ── The draft ─────────────────────────────────────────────────────────────


def _clean(value, limit=2000) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize(raw) -> dict:
    """The model's reply (or a reviewer's edit) as a draft: every field a
    trimmed string, lines in order, at most MAX_LINES."""
    raw = raw if isinstance(raw, dict) else {}
    header = {f: _clean(raw.get(f)) for f in HEADER_FIELDS}
    header["vendor_gstin"] = header["vendor_gstin"].replace(" ", "").upper()
    header["order_type"] = header["order_type"].lower() if header["order_type"].lower() in ORDER_TYPES else "domestic"
    lines = raw.get("lines") if isinstance(raw.get("lines"), list) else []
    return {
        **header,
        "lines": [{f: _clean((ln if isinstance(ln, dict) else {}).get(f)) for f in LINE_FIELDS} for ln in lines[:MAX_LINES]],
        "notes": _clean(raw.get("notes")),
    }


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


def problems(draft: dict, plant, filed_as: str = "") -> list[dict]:
    """What stops approval: a missing compulsory field, a figure or date
    that does not parse, a billing address that does not name `plant`, or a
    PO number other than the one the file was filed under (`filed_as`).
    [{"field", "line", "message"}]."""
    out = []

    def add(field, message, line=None):
        out.append({"field": field, "line": line, "message": message})

    if filed_as and draft.get("po_number") and "".join(draft["po_number"].split()).upper() != "".join(filed_as.split()).upper():
        add("po_number", f"The file was filed under PO {filed_as} but this says {draft['po_number']} - correct the "
                         "number, or withdraw the file and upload it under the right one.")

    kind = draft.get("order_type") if draft.get("order_type") in ORDER_TYPES else "domestic"
    for f in REQUIRED_HEADER[kind]:
        if not draft.get(f):
            add(f, f"{LABELS[f]} is missing.")
    for f in ("total_value", "tax_amount", "total_inclusive_value"):
        if _num(draft.get(f)) == "bad":
            add(f, f"{LABELS[f]} is not a number.")
    if _day(draft.get("po_date")) == "bad":
        add("po_date", "PO date must be a date (YYYY-MM-DD).")
    if draft.get("vendor_gstin") and not rules.clean_gstin(draft["vendor_gstin"]):
        add("vendor_gstin", "Vendor GSTIN is not a valid GSTIN.")
    if draft.get("billing_address"):
        billed = rules.billing_plant_code(draft["billing_address"])
        if not billed:
            add("billing_address", "The billing address names none of the plants - check it.")
        elif billed != plant.code:
            from apps.core.models import Plant

            other = Plant.objects.filter(code=billed).first()
            add("billing_address", f"The PO is billed to {other.name if other else billed}, not {plant.name} - "
                                   "only that plant can add it.")
    lines = draft.get("lines") or []
    if not lines:
        add("lines", "The PO has no lines.")
    for i, ln in enumerate(lines, start=1):
        for f in REQUIRED_LINE[kind]:
            if not ln.get(f):
                add(f, f"Line {i}: {LABELS[f]} is missing.", i)
        for f in ("qty", "rate", "net_value"):
            v = _num(ln.get(f))
            if v == "bad" or (v is not None and v < 0):
                add(f, f"Line {i}: {LABELS[f]} is not a number.", i)
        if _num(ln.get("qty")) == Decimal("0"):
            add("qty", f"Line {i}: Quantity must be more than zero.", i)
        if _day(ln.get("delivery_date")) == "bad":
            add("delivery_date", f"Line {i}: Delivery date must be a date (YYYY-MM-DD).", i)
    return out


def checks(draft: dict) -> list[dict]:
    """procurement_rules.po_checks() over the draft - what does not add up."""
    lines = [{"line_no": i, "description": ln.get("description"), "qty": _ok(_num(ln.get("qty"))), "uom": ln.get("uom"),
              "rate": _ok(_num(ln.get("rate"))), "net_value": _ok(_num(ln.get("net_value")))}
             for i, ln in enumerate(draft.get("lines") or [], start=1)]
    po_date = _day(draft.get("po_date"))
    found = rules.po_checks(lines, _ok(_num(draft.get("total_value"))), _ok(_num(draft.get("total_inclusive_value"))),
                            draft.get("vendor_gstin") or "", po_date if isinstance(po_date, datetime.date) else None)
    # Missing fields are already in problems(); keep the arithmetic here.
    return [c for c in found if c["check"] in ("line_value", "total_value", "total_inclusive_value", "tax_rate")
            and "has no" not in c["message"]]


def _ok(v):
    return None if v == "bad" else v


def sheet_differences(draft: dict, plant) -> dict | None:
    """When the PO sheet already holds this PO number at the plant: how the
    draft differs from it, field by field and line by line. None when the
    sheet does not hold it."""
    from apps.core.models import PurchaseOrder

    po = (PurchaseOrder.objects.filter(plant=plant, kind=draft.get("order_type") or "domestic",
                                       po_number=draft.get("po_number") or "").select_related("vendor").first())
    if po is None:
        return None
    diffs = []

    def cmp(label, sheet, pdf, numeric=False):
        if numeric:
            a, b = _ok(_num(str(sheet) if sheet is not None else "")), _ok(_num(pdf))
            same = (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= Decimal("0.01"))
        else:
            same = " ".join(str(sheet or "").lower().split()) == " ".join(str(pdf or "").lower().split())
        if not same:
            diffs.append({"field": label, "sheet": "" if sheet is None else str(sheet), "pdf": pdf or ""})

    cmp("PO date", po.po_date.isoformat() if po.po_date else "", draft.get("po_date"))
    cmp("Vendor name", po.vendor.name if po.vendor else "", draft.get("vendor_name"))
    cmp("Vendor GSTIN", po.vendor.gstin if po.vendor else "", draft.get("vendor_gstin"))
    cmp("Total value", po.total_value, draft.get("total_value"), numeric=True)
    cmp("Total including tax", po.total_inclusive_value, draft.get("total_inclusive_value"), numeric=True)
    sheet_lines = list(po.lines.filter(is_active=True).order_by("line_no"))
    pdf_lines = draft.get("lines") or []
    if len(sheet_lines) != len(pdf_lines):
        diffs.append({"field": "Number of lines", "sheet": str(len(sheet_lines)), "pdf": str(len(pdf_lines))})
    for i, (s, p) in enumerate(zip(sheet_lines, pdf_lines, strict=False), start=1):
        cmp(f"Line {i} description", s.description, p.get("description"))
        cmp(f"Line {i} quantity", s.qty_ordered, p.get("qty"), numeric=True)
        cmp(f"Line {i} net price", s.rate, p.get("rate"), numeric=True)
        cmp(f"Line {i} unit", s.uom, rules.canonical_uom(p.get("uom") or "")[0])
    return {"source": po.source, "poId": po.id, "differences": diffs}


# ── Approve / reject ──────────────────────────────────────────────────────


@transaction.atomic
def approve(extraction, draft_in, user):
    """Write the reviewed draft to the procurement tables. Raises
    ExtractionError (with every problem) when it cannot."""
    from apps.core.models import Plant, PoExtraction, PurchaseOrder

    ext = PoExtraction.objects.select_for_update().select_related("plant", "document").get(pk=extraction.pk)
    if ext.status != PoExtraction.Status.READY:
        raise ExtractionError("Only a reading that is ready for review can be approved.")
    if ext.kind == PoExtraction.Kind.BOE:
        return _approve_boe(ext, draft_in, user)
    draft = normalize(draft_in if draft_in is not None else ext.draft)
    found = problems(draft, ext.plant, ext.document.po_number)
    if found:
        err = ExtractionError(found[0]["message"])
        err.problems = found
        raise err
    plant = Plant.objects.select_for_update().get(pk=ext.plant_id)
    kind = PurchaseOrder.Kind.IMPORT if draft["order_type"] == "import" else PurchaseOrder.Kind.DOMESTIC
    vendor = upsert_vendor(draft["vendor_gstin"], draft["vendor_name"], draft["vendor_sap_code"],
                           draft["vendor_address"], draft["vendor_email"])
    header = {
        "po_date": _day(draft["po_date"]), "vendor": vendor, "currency": draft["currency"].upper()[:10] or "INR",
        "tax_type": "" if kind == PurchaseOrder.Kind.IMPORT else rules.canonical_tax_type(draft["tax_type"]),
        "tax_type_raw": draft["tax_type"][:100],
        "payment_terms": draft["payment_terms"], "incoterms": draft["incoterms"],
        "billing_address": draft["billing_address"], "billing_plant": plant, "ship_to": draft["shipping_address"],
        "total_value": _num(draft["total_value"]), "total_inclusive_value": _num(draft["total_inclusive_value"]),
        "remarks": draft["remarks"], "is_active": True, "source": PurchaseOrder.Source.APP,
        "synced_at": timezone.now(),
    }
    po = PurchaseOrder.objects.select_for_update().filter(plant=plant, kind=kind, po_number=draft["po_number"]).first()
    if po is None:
        po = PurchaseOrder.objects.create(plant=plant, kind=kind, po_number=draft["po_number"][:100], **header)
    else:
        for f, v in header.items():
            setattr(po, f, v)
        po.save()
    values = []
    for ln in draft["lines"]:
        uom, _known = rules.canonical_uom(ln["uom"])
        values.append({
            "item_code": ln["item_code"][:50], "description": ln["description"][:500], "hsn": ln["hsn"][:20],
            "uom": uom, "uom_raw": ln["uom"][:20], "qty_ordered": _num(ln["qty"]), "rate": _num(ln["rate"]),
            "net_value": _num(ln["net_value"]), "delivery_date": _day(ln["delivery_date"]),
            "material": materials.material_for(ln["description"], ln["item_code"], uom, ln["hsn"]),
        })
    write_lines(po, values, ProjectionResult(), "The approved PO file")
    ext.draft = draft
    ext.status = PoExtraction.Status.APPROVED
    ext.purchase_order = po
    ext.reviewed_by_email = getattr(user, "email", "") or ""
    ext.reviewed_at = timezone.now()
    ext.save()
    return ext


def _approve_boe(ext, draft_in, user):
    """A Bill of Entry reading into the plant's import shipment."""
    from apps.core.models import PoExtraction

    draft = boe_extraction.normalize(draft_in if draft_in is not None else ext.draft)
    found = boe_extraction.problems(draft, ext)
    if found:
        err = ExtractionError(found[0]["message"])
        err.problems = found
        raise err
    shipment = boe_extraction.approve(ext, draft, user)
    ext.draft = draft
    ext.status = PoExtraction.Status.APPROVED
    ext.shipment = shipment
    ext.reviewed_by_email = getattr(user, "email", "") or ""
    ext.reviewed_at = timezone.now()
    ext.save()
    return ext


def review(ext) -> dict:
    """Everything the review screen shows for a reading, by what was read."""
    from apps.core.models import PoExtraction

    draft = ext.draft or {}
    if ext.kind == PoExtraction.Kind.BOE:
        return {
            "headerFields": boe_extraction.HEADER_FIELDS, "lineFields": boe_extraction.LINE_FIELDS,
            "labels": boe_extraction.LABELS,
            "requiredHeader": {"boe": boe_extraction.REQUIRED_HEADER}, "requiredLine": {"boe": boe_extraction.REQUIRED_LINE},
            "lineOptions": {"po_line_id": boe_extraction.line_options(ext)} if ext.draft else {},
            "problems": boe_extraction.problems(draft, ext) if ext.draft else [],
            "checks": boe_extraction.checks(draft, ext) if ext.draft else [],
            "sheet": boe_extraction.sheet_differences(draft, ext) if ext.draft else None,
        }
    return {
        "headerFields": HEADER_FIELDS, "lineFields": LINE_FIELDS, "labels": LABELS,
        "requiredHeader": REQUIRED_HEADER, "requiredLine": REQUIRED_LINE, "lineOptions": {},
        "problems": problems(draft, ext.plant, ext.document.po_number) if ext.draft else [],
        "checks": checks(draft) if ext.draft else [],
        "sheet": sheet_differences(draft, ext.plant) if ext.draft else None,
    }


def save_draft(extraction, draft_in, user):
    """Keep a reviewer's corrections without approving."""
    from apps.core.models import PoExtraction

    ext = PoExtraction.objects.select_for_update().get(pk=extraction.pk)
    if ext.status != PoExtraction.Status.READY:
        raise ExtractionError("Only a reading that is ready for review can be edited.")
    ext.draft = boe_extraction.normalize(draft_in) if ext.kind == PoExtraction.Kind.BOE else normalize(draft_in)
    ext.save(update_fields=["draft"])
    return ext


def reject(extraction, note, user):
    from apps.core.models import PoExtraction

    note = (note or "").strip()
    if not note:
        raise ExtractionError("Say why it is rejected.")
    with transaction.atomic():
        ext = PoExtraction.objects.select_for_update().get(pk=extraction.pk)
        if ext.status not in (PoExtraction.Status.READY, PoExtraction.Status.FAILED):
            raise ExtractionError("This reading is already decided.")
        ext.status = PoExtraction.Status.REJECTED
        ext.review_note = note[:2000]
        ext.reviewed_by_email = getattr(user, "email", "") or ""
        ext.reviewed_at = timezone.now()
        ext.save()
    return ext
