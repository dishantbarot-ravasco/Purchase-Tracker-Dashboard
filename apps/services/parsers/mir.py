"""
apps/services/parsers/mir.py — parses HRS's MIR FILE 2026-2027.xlsx,
'RAW MATERIAL' sheet, into a flat list of row dicts ready to upsert into
HRSMIREntry.

Exact header (row 6; data from row 7), verified against the live file this
session:
A Month | B MIR. No. | C Date | D SAP GRN No. | E Party Name | F State |
G Invoice No. | H Date | I Park Invoice No. | J Purchase Order. No. |
K Purchase Order. Date | L SAP P.O. No. | M SAP P.O. DATE |
N Material Description | O Qty | P UOM | Q Rate | R Net | S Discount Rate |
T Discount Amt. | U Others | V Taxable Value | W Tax rate | X IGST |
Y CGST Rate | Z CGST AMT | AA SGST RATE | AB SGST AMT |
AC Other exclu. gst | AD Total Amount | AE TCS RATE | AF TCS Amt. |
AG Inv. Final Value | AH Plant | AI Dept. Uses | AJ Category material |
AK Remarks

Column J ("Purchase Order. No.") is the field the docstring on
HRSMIREntry.po_number_raw warns about - confirmed unreliable by audit
(~30% blank, ~25% non-standard format) and never used as a sole join key.

Columns A ("Month") and B ("MIR. No.") have the identical Excel
autoconvert quirk documented in apps/services/parsers/achhad_mir.py's module
docstring - confirmed against the live file this session (previously
undocumented and unhandled here, unlike achhad_mir.py): typed text like
"April-26" / "01/04" gets silently turned into a real datetime cell by
Excel (number_format 'mmmm-d' / 'mm/dd' respectively) rather than staying
literal text. Before this fix, plain to_str() on these columns returned
Python's default str(datetime) repr (e.g. "2026-04-26 00:00:00") instead
of the originally-typed label - confirmed 100% of HRS's `month` values and
16.3% of `mir_no` values (day-of-month <=12, same as achhad_mir.py's
finding) were corrupted this way in the live data before this fix.
_month_label()/_mir_no_label() below are a straight port of
achhad_mir.py's identically-named helpers - the reconstruction logic is
generic, not Achhad-specific, it just never got applied here.
"""

import datetime
import io
from dataclasses import dataclass

import openpyxl

from apps.services.parsers.common import to_date, to_decimal, to_str

# ── Column/row layout constants ─────────────────────────────────────────────

SHEET_NAME = "RAW MATERIAL"
HEADER_ROW = 6
DATA_START_ROW = 7

EXPECTED_HEADERS = {
    "A": "Month", "B": "MIR. No.", "C": "Date", "D": "SAP GRN No.", "E": "Party Name",
    "F": "State", "G": "Invoice No.", "H": "Date", "I": "Park Invoice No.",
    "J": "Purchase Order. No.", "K": "Purchase Order. Date", "L": "SAP P.O. No.",
    "M": "SAP P.O. DATE", "N": "Material Description", "O": "Qty", "P": "UOM", "Q": "Rate",
    "R": "Net", "S": "Discount Rate", "T": "Discount Amt.", "U": "Others",
    "V": "Taxable Value", "W": "Tax rate", "X": "IGST", "Y": "CGST Rate", "Z": "CGST AMT",
    "AA": "SGST RATE", "AB": "SGST AMT", "AC": "Other exclu. gst", "AD": "Total Amount",
    "AE": "TCS RATE", "AF": "TCS Amt.", "AG": "Inv. Final Value", "AH": "Plant",
    "AI": "Dept. Uses", "AJ": "Category material", "AK": "Remarks",
}


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedMirEntry:
    month: str
    mir_no: str
    mir_date: object
    sap_grn_number: str
    po_number_raw: str
    party_name: str
    state: str
    invoice_no: str
    invoice_date: object
    material_description: str
    qty: object
    uom: str
    rate: object
    net: object
    discount_rate_pct: object
    discount_amt: object
    others: object
    taxable_value: object
    tax_rate_pct: object
    igst: object
    cgst_rate_pct: object
    cgst_amt: object
    sgst_rate_pct: object
    sgst_amt: object
    other_taxes_excl_gst: object
    total_amount: object
    tcs_rate_pct: object
    tcs_amt: object
    invoice_final_value: object
    plant_tag: str
    dept_use: str
    material_category: str
    remarks: str
    source_row_ref: str


class HeaderMismatch(Exception):
    pass


def _strip(v):
    return (v or "").strip() if isinstance(v, str) else v


# ── Excel-autoconvert workarounds (Month/MIR No. columns) ───────────────────

def _month_label(cell) -> str:
    """See module docstring - reconstructs the originally-typed 'Mon-YY'
    text from a cell Excel silently turned into a date, using the cell's
    own number_format to know whether it should read as abbreviated or
    full month name. Ported verbatim from achhad_mir.py's identically-named
    helper."""
    v = cell.value
    if not isinstance(v, datetime.datetime):
        return to_str(v)
    month_name = v.strftime("%B") if "mmmm" in (cell.number_format or "") else v.strftime("%b")
    return f"{month_name}-{v.day}"


def _mir_no_label(cell) -> str:
    """See module docstring - reconstructs the originally-typed 'MM/DD'
    MIR-number text from a cell Excel silently turned into a date. Ported
    verbatim from achhad_mir.py's identically-named helper."""
    v = cell.value
    if not isinstance(v, datetime.datetime):
        return to_str(v)
    return f"{v.month:02d}/{v.day:02d}"


# ── Public entry point ───────────────────────────────────────────────────────

def parse_mir_xlsx(file_bytes: bytes) -> list[ParsedMirEntry]:
    """Reads the 'RAW MATERIAL' sheet and returns one ParsedMirEntry per data
    row. Raises HeaderMismatch immediately if the sheet name or any header
    cell doesn't match what this parser was built against, rather than
    silently reading misaligned columns."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise HeaderMismatch(f"Expected sheet {SHEET_NAME!r}, found sheets: {wb.sheetnames}")
    ws = wb[SHEET_NAME]

    for col, expected in EXPECTED_HEADERS.items():
        actual = _strip(ws[f"{col}{HEADER_ROW}"].value)
        if actual != expected:
            raise HeaderMismatch(f"Column {col}{HEADER_ROW}: expected {expected!r}, found {actual!r}")

    entries = []
    for r in range(DATA_START_ROW, ws.max_row + 1):
        party_name = to_str(ws[f"E{r}"].value)
        if not party_name:
            continue  # a blank Party Name means an empty row - MIR has no other reliable "is this row used" signal

        entries.append(
            ParsedMirEntry(
                month=_month_label(ws[f"A{r}"]),
                mir_no=_mir_no_label(ws[f"B{r}"]),
                mir_date=to_date(ws[f"C{r}"].value),
                sap_grn_number=to_str(ws[f"D{r}"].value),
                po_number_raw=to_str(ws[f"J{r}"].value),  # unreliable join key, see module docstring - never used alone
                party_name=party_name,
                state=to_str(ws[f"F{r}"].value),
                invoice_no=to_str(ws[f"G{r}"].value),
                invoice_date=to_date(ws[f"H{r}"].value),
                material_description=to_str(ws[f"N{r}"].value),
                qty=to_decimal(ws[f"O{r}"].value),
                uom=to_str(ws[f"P{r}"].value),
                rate=to_decimal(ws[f"Q{r}"].value),
                net=to_decimal(ws[f"R{r}"].value),
                discount_rate_pct=to_decimal(ws[f"S{r}"].value),
                discount_amt=to_decimal(ws[f"T{r}"].value),
                others=to_decimal(ws[f"U{r}"].value),
                taxable_value=to_decimal(ws[f"V{r}"].value),
                tax_rate_pct=to_decimal(ws[f"W{r}"].value),
                igst=to_decimal(ws[f"X{r}"].value),
                cgst_rate_pct=to_decimal(ws[f"Y{r}"].value),
                cgst_amt=to_decimal(ws[f"Z{r}"].value),
                sgst_rate_pct=to_decimal(ws[f"AA{r}"].value),
                sgst_amt=to_decimal(ws[f"AB{r}"].value),
                other_taxes_excl_gst=to_decimal(ws[f"AC{r}"].value),
                total_amount=to_decimal(ws[f"AD{r}"].value),
                tcs_rate_pct=to_decimal(ws[f"AE{r}"].value),
                tcs_amt=to_decimal(ws[f"AF{r}"].value),
                invoice_final_value=to_decimal(ws[f"AG{r}"].value),
                plant_tag=to_str(ws[f"AH{r}"].value),
                dept_use=to_str(ws[f"AI{r}"].value),
                material_category=to_str(ws[f"AJ{r}"].value),
                remarks=to_str(ws[f"AK{r}"].value),
                source_row_ref=str(r),
            )
        )
    return entries
