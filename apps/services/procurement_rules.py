"""
The pure rules behind MIR entry (2026-09-28): units, GST, financial years,
invoice numbers and every amount the MIR form shows. No Django imports, like
parsers/common.py, so each rule is tested in isolation and the preview and
the post compute from the same code (apps/services/mir_service.py).

Nothing here is fuzzy. The MIR form is entered against a PO line the clerk
picked, so a figure either agrees exactly or is a mismatch that needs a
reason. The one allowance is the rupee an invoice rounds its total to
(INVOICE_ROUNDING_TOLERANCE): that is how invoices are printed, not a
tolerance on the goods.
"""

from __future__ import annotations

import datetime
import re
from decimal import ROUND_HALF_UP, Decimal

MONEY = Decimal("0.01")
RATE = Decimal("0.0001")
QTY = Decimal("0.001")

# GST slabs a line may carry, in percent. 40 is the 2025 demerit rate.
GST_SLABS = (
    Decimal("0"), Decimal("0.1"), Decimal("0.25"), Decimal("1.5"), Decimal("3"),
    Decimal("5"), Decimal("12"), Decimal("18"), Decimal("28"), Decimal("40"),
)

# An invoice prints its grand total rounded to the rupee; a computed total
# within this of the typed one agrees.
INVOICE_ROUNDING_TOLERANCE = Decimal("1.00")


class TaxType:
    IGST = "IGST"
    CGST_SGST = "CGST_SGST"
    CGST_UGST = "CGST_UGST"
    ALL = (IGST, CGST_SGST, CGST_UGST)
    LABELS = {IGST: "IGST", CGST_SGST: "CGST + SGST", CGST_UGST: "CGST + UGST"}


# ── Units ────────────────────────────────────────────────────────────────────
# One code per unit, whatever the PO sheet typed. Only spellings of the SAME
# unit are folded together; KG and MT stay different units (a quantity is
# never converted behind the clerk's back).
_UOM_ALIASES = {
    "KG": ("KG", "KGS", "KILO", "KILOS", "KILOGRAM", "KILOGRAMS"),
    "MT": ("MT", "TO", "TON", "TONS", "TONNE", "TONNES"),
    "G": ("G", "GM", "GMS", "GRAM", "GRAMS"),
    "L": ("L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "LITER", "LITERS"),
    "ML": ("ML", "MLS", "MILLILITRE", "MILLILITRES", "MILLILITER", "MILLILITERS"),
    "KL": ("KL", "KILOLITRE", "KILOLITRES", "KILOLITER", "KILOLITERS"),
    "M": ("M", "MTR", "MTRS", "METER", "METERS", "METRE", "METRES"),
    "CM": ("CM", "CMS", "CENTIMETRE", "CENTIMETRES", "CENTIMETER", "CENTIMETERS"),
    "MM": ("MM", "MILLIMETRE", "MILLIMETRES", "MILLIMETER", "MILLIMETERS"),
    "M2": ("M2", "SQM", "SQMT", "SQMTR", "SQMTRS", "SQ M", "SQ MTR"),
    "NOS": ("NOS", "NO", "NOS.", "EA", "EACH", "PC", "PCS", "PIECE", "PIECES", "UNIT", "UNITS"),
    "ROLL": ("ROLL", "ROLLS", "RL", "RLS"),
    "SET": ("SET", "SETS"),
    "BAG": ("BAG", "BAGS"),
}
_UOM_LOOKUP = {alias: code for code, aliases in _UOM_ALIASES.items() for alias in aliases}
KNOWN_UOMS = tuple(_UOM_ALIASES)


def canonical_uom(raw: str) -> tuple[str, bool]:
    """(code, known) for a unit as the PO sheet typed it. An unknown unit
    keeps its own upper-cased spelling and known=False, so it is shown and
    flagged rather than guessed ("BQ2" on Vapi's sheet is not a unit this
    app can name)."""
    key = re.sub(r"\s+", " ", (raw or "").strip().upper()).rstrip(".")
    if not key:
        return "", False
    if key in _UOM_LOOKUP:
        return _UOM_LOOKUP[key], True
    return key[:20], False


# ── GST ──────────────────────────────────────────────────────────────────────
_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def clean_gstin(raw: str) -> str:
    """Upper-cased, spaces removed; "" for anything that is not a GSTIN."""
    value = re.sub(r"\s+", "", (raw or "")).upper()
    return value if _GSTIN.match(value) else ""


def gstin_state(gstin: str) -> str:
    """The two-digit state code a GSTIN starts with, or ""."""
    return gstin[:2] if clean_gstin(gstin) else ""


# GST state codes - the vendor's State on a MIR (where the supply comes
# from). 25 (Daman and Diu) merged into 26 in 2020 and 28 (old Andhra
# Pradesh) is retired, so neither is offered; 96 is a supplier abroad.
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "96": "Outside India", "97": "Other Territory",
}


# ── Which plant a PO is billed to ──────────────────────────────────────────
# A PO belongs to the plant on its BILLING address (project owner,
# 2026-10-03): only that plant may file the PO or receive against it,
# whatever Ship To says. The billing addresses the three plants print are
# stable (checked over every PO on 2026-10-03), so a few words of each
# identify it; an address naming none of them, or more than one plant, is
# unresolved ("") rather than guessed.
PLANT_ADDRESS_MARKERS = {
    "hrs": ("hindustan rubber", "silvassa", "naroli", "kharadpada"),
    "achhad": ("achhad", "acchad", "talasari", "talsari"),
    "vapi": ("vapi",),
}


def billing_plant_code(billing_address: str) -> str:
    """The plant code a PO's billing address names, or "" when it names none
    or several."""
    text = re.sub(r"\s+", " ", (billing_address or "").lower())
    hits = {code for code, words in PLANT_ADDRESS_MARKERS.items() if any(w in text for w in words)}
    return next(iter(hits)) if len(hits) == 1 else ""


def canonical_tax_type(raw: str) -> str:
    """The PO sheet's tax type as one of TaxType, or "" when it is blank or
    not one type ("CGST+SGST+IGST (mixed - flag)")."""
    text = re.sub(r"[\s_]+", "", (raw or "").upper())
    if not text or "MIXED" in text:
        return ""
    has_igst = "IGST" in text
    has_cgst = "CGST" in text
    if has_igst and not has_cgst:
        return TaxType.IGST
    if has_cgst and not has_igst:
        return TaxType.CGST_UGST if "UGST" in text else TaxType.CGST_SGST
    return ""


def expected_tax_type(vendor_gstin: str, plant_state: str, plant_is_union_territory: bool, po_tax_type: str,
                      vendor_state: str = "") -> str:
    """The tax type a receipt at this plant should carry: CGST with SGST (or
    UGST in a union territory) when the vendor is in the plant's state, IGST
    otherwise. The vendor's state is its GSTIN's, else `vendor_state` (the
    State the MIR clerk chose); falls back to the PO's own tax type when
    neither is known; "" when nothing says."""
    vendor_state = gstin_state(vendor_gstin) or (vendor_state if vendor_state in GST_STATES else "")
    if vendor_state and plant_state:
        if vendor_state != plant_state:
            return TaxType.IGST
        return TaxType.CGST_UGST if plant_is_union_territory else TaxType.CGST_SGST
    return po_tax_type or ""


def po_gst_rate(total_value, total_inclusive_value) -> Decimal | None:
    """The GST percent a PO's two totals imply, when it lands on a slab
    (within half a paisa per rupee) - the default a line's GST field starts
    from. None when either total is missing or the PO mixes rates."""
    if not total_value or total_inclusive_value is None:
        return None
    total_value, total_inclusive_value = Decimal(total_value), Decimal(total_inclusive_value)
    if total_value <= 0 or total_inclusive_value < total_value:
        return None
    pct = (total_inclusive_value - total_value) / total_value * 100
    for slab in GST_SLABS:
        if abs(pct - slab) <= Decimal("0.05"):
            return slab
    return None


def is_gst_slab(rate) -> bool:
    return rate is not None and Decimal(rate) in GST_SLABS


# ── Checks on a PO as written (owner, 2026-10-03) ──────────────────────────
# Shown wherever a PO is read before it is relied on (the MIR form's PO
# header, the extraction review). They flag, never block or correct: the PO
# is the purchase team's document, and a mixed-rate order legitimately has
# a tax total on no single slab. Each sum is allowed the rupee the printed
# figures round to (INVOICE_ROUNDING_TOLERANCE).
PO_CHECK_TOLERANCE = INVOICE_ROUNDING_TOLERANCE


def _d(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def po_checks(lines, total_value, total_inclusive_value, vendor_gstin: str, po_date=None) -> list[dict]:
    """What does not add up on a PO. `lines` is a list of dicts with
    line_no, description, qty, uom, rate, net_value. Returns
    [{"check", "line", "message"}], empty when everything agrees."""
    out = []

    def add(check, message, line=None):
        out.append({"check": check, "line": line, "message": message})

    if po_date is None:
        add("po_date", "The PO has no created date.")
    if not clean_gstin(vendor_gstin or ""):
        add("vendor_gstin", "The vendor has no valid GSTIN on the PO." if (vendor_gstin or "").strip()
            else "The vendor's GSTIN is missing.")
    line_sum, sum_known = Decimal("0"), True
    for ln in lines:
        n = ln.get("line_no")
        qty, rate, net = _d(ln.get("qty")), _d(ln.get("rate")), _d(ln.get("net_value"))
        if not (ln.get("description") or "").strip():
            add("line_description", f"Line {n} has no material description.", n)
        if qty is None or qty <= 0:
            add("line_qty", f"Line {n} has no quantity.", n)
        if not (ln.get("uom") or "").strip():
            add("line_uom", f"Line {n} has no unit.", n)
        if rate is None:
            add("line_rate", f"Line {n} has no net price.", n)
        if net is None:
            sum_known = False
        elif qty is not None and rate is not None and abs(qty * rate - net) > PO_CHECK_TOLERANCE:
            add("line_value", f"Line {n}: quantity x price is {money(qty * rate)} but its net value says {money(net)}.", n)
        if net is not None:
            line_sum += net
    total, inclusive = _d(total_value), _d(total_inclusive_value)
    if total is None:
        add("total_value", "The PO has no total value.")
    elif lines and sum_known and abs(line_sum - total) > PO_CHECK_TOLERANCE:
        add("total_value", f"The lines' net values add up to {money(line_sum)} but the PO total says {money(total)}.")
    if inclusive is None:
        add("total_inclusive_value", "The PO has no total including tax.")
    elif total is not None and inclusive < total:
        add("total_inclusive_value", f"The total including tax ({money(inclusive)}) is less than the total ({money(total)}).")
    elif total is not None and inclusive > total and po_gst_rate(total, inclusive) is None:
        add("tax_rate", f"The tax ({money(inclusive - total)}) is not one GST rate of the total - "
                        "check the totals, unless the PO mixes GST rates.")
    return out


# ── Identity helpers ────────────────────────────────────────────────────────
def financial_year(day: datetime.date) -> str:
    """"2026-27" for any date from 1 April 2026 to 31 March 2027."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def mir_number(prefix: str, fy: str, seq: int) -> str:
    """"HRS/26-27/0007" - the plant prefix, the short financial year, and a
    per-plant, per-year running number."""
    short = fy[2:4] + "-" + fy[5:7]
    return f"{prefix}/{short}/{seq:04d}"


def invoice_key(invoice_no: str) -> str:
    """The form of an invoice number duplicates are checked on: upper case,
    spaces removed, and leading zeros of every digit run dropped, so
    "INV-0042", "inv-42" and "INV - 42" are the same invoice. Letters and
    separators stay, because "A/42" and "B/42" are different invoices."""
    text = re.sub(r"\s+", "", (invoice_no or "")).upper()
    return re.sub(r"\d+", lambda m: str(int(m.group())), text)


def vendor_name_key(name: str) -> str:
    """Lower-case letters and digits only: the key a vendor with no GSTIN
    is recognised by across PO sheets. A leading "M/s." is dropped, so
    "M/s. Prime Chemicals" and "PRIME CHEMICALS" are one vendor."""
    text = (name or "").lower()
    text = re.sub(r"^\s*m\s*/\s*s\.?\s*", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


# ── Amounts ─────────────────────────────────────────────────────────────────
def money(value) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def line_amounts(qty, rate, discount, other_charges, gst_rate, tax_type) -> dict:
    """Every figure of one MIR line, in rupees, from what the clerk typed:
    gross = qty x rate; taxable = gross - discount + freight/packing;
    GST on taxable, split half CGST / half SGST-or-UGST unless IGST; total =
    taxable + GST. Each tax is rounded on its own, as invoices print them."""
    qty, rate = Decimal(qty), Decimal(rate)
    discount, other_charges = Decimal(discount or 0), Decimal(other_charges or 0)
    gst_rate = Decimal(gst_rate)
    gross = money(qty * rate)
    taxable = money(gross - discount + other_charges)
    igst = cgst = sgst = Decimal("0.00")
    if tax_type == TaxType.IGST:
        igst = money(taxable * gst_rate / 100)
    else:
        cgst = sgst = money(taxable * gst_rate / 200)
    return {
        "gross": gross, "taxable": taxable, "igst": igst, "cgst": cgst, "sgst": sgst,
        "total": taxable + igst + cgst + sgst,
    }


def rate_differs(po_rate, invoice_rate) -> bool:
    """Exact comparison at the PO sheet's own 4 decimal places."""
    return Decimal(po_rate).quantize(RATE, rounding=ROUND_HALF_UP) != Decimal(invoice_rate).quantize(RATE, rounding=ROUND_HALF_UP)


# MirMismatch.difference_pct is numeric(9, 2). A tiny open quantity against
# a full truck (0.010 left, 1000.02 received) is ~1e7 %, which overflowed it
# and turned a MIR that previewed fine into a 500 on posting.
MAX_PCT = Decimal("9999999.99")


def pct_of(diff, base) -> Decimal | None:
    if not base:
        return None
    pct = (Decimal(diff) / Decimal(base) * 100).quantize(MONEY, rounding=ROUND_HALF_UP)
    return max(-MAX_PCT, min(MAX_PCT, pct))
