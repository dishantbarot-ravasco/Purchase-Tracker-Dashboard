"""
Parses RTP ACHHAD MIR FILE 2026-27.xlsx, 'R.M. ' sheet (note the trailing
space - confirmed against the live file), into a flat list of row dicts
ready to upsert into RTPAchhadMIREntry.

Exact header (row 2; data from row 3), verified against the live file this
session:
A Month | B MIR. No. | C Date | D Party Name | E State | F Invoice No. |
G Date | H Purchase Order. No. | I Purchase Order. Date |
J Material Description | K Qty | L UOM | M Rate | N Net | O Discount Rate |
P Discount Amt. | Q Others | R Taxable Value | S Tax rate | T IGST |
U CGST Rate | V CGST AMT | W SGST RATE | X SGST AMT | Y Other exclu. gst |
Z Total Amount | AA TCS RATE | AB TCS Amt. | AC Inv. Final Value | AD Plant |
AE Dept. Uses | AF Category material | AG Remarks

Genuinely different from HRS's MIR layout (see apps/core/parsers/mir.py's
docstring for HRS's columns), not just a relabeling:
  - No SAP GRN Number column at all - Achhad's own register never records
    one, confirmed against the live file (HRS's column D has no Achhad
    equivalent).
  - No "Park Invoice No." / separate "SAP P.O. No." + "SAP P.O. DATE"
    columns either - Achhad has exactly one PO-number field (H) and one PO-
    date field (I), not HRS's four-column PO/SAP-PO split.
  - "Purchase Order. No." (H) sometimes holds a plain SAP PO number typed as
    a number (e.g. 1100000768), which openpyxl returns as a float - run
    through to_code_str() to drop the trailing '.0', not to_str().

Two more columns (A "Month", B "MIR. No.") have a data-entry quirk that
needed reverse-engineering: many cells were typed as e.g. "Apr-26" or
"01/04" and Excel silently reinterpreted them as real dates (a datetime
with number_format 'mmm-d' / 'mm/dd' respectively) rather than storing the
literal text - confirmed by inspecting number_format on the live file (589
of ~610 "Month" cells are dates; 57 of 610 "MIR No." cells are dates, the
rest are already plain strings like '13/04' where day-of-month >12 stopped
Excel's autoconvert). Reformatting a converted cell through its own
number_format's month/day fields reproduces the original typed text
exactly, so this parser does NOT use to_date()/to_str() for either column -
see _month_label() / _mir_no_label() below. The reliable, always-a-string
Date column (C) is used for mir_date instead of trying to parse column A as
a real date.
"""

import datetime
import io
from dataclasses import dataclass

import openpyxl

from apps.services.parsers.common import to_code_str, to_date, to_decimal, to_str

SHEET_NAME = "R.M. "
HEADER_ROW = 2
DATA_START_ROW = 3

EXPECTED_HEADERS = {
    "A": "Month", "B": "MIR. No.", "C": "Date", "D": "Party Name", "E": "State",
    "F": "Invoice No.", "G": "Date", "H": "Purchase Order. No.", "I": "Purchase Order. Date",
    "J": "Material Description", "K": "Qty", "L": "UOM", "M": "Rate", "N": "Net",
    "O": "Discount Rate", "P": "Discount Amt.", "Q": "Others", "R": "Taxable Value",
    "S": "Tax rate", "T": "IGST", "U": "CGST Rate", "V": "CGST AMT", "W": "SGST RATE",
    "X": "SGST AMT", "Y": "Other exclu. gst", "Z": "Total Amount", "AA": "TCS RATE",
    "AB": "TCS Amt.", "AC": "Inv. Final Value", "AD": "Plant", "AE": "Dept. Uses",
    "AF": "Category material", "AG": "Remarks",
}


@dataclass
class ParsedAchhadMirEntry:
    month: str
    mir_no: str
    mir_date: object
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


def _month_label(cell) -> str:
    """See module docstring - reconstructs the originally-typed 'Mon-YY'
    text from a cell Excel silently turned into a date, using the cell's
    own number_format to know whether it should read as abbreviated or
    full month name."""
    v = cell.value
    if not isinstance(v, datetime.datetime):
        return to_str(v)
    month_name = v.strftime("%B") if "mmmm" in (cell.number_format or "") else v.strftime("%b")
    return f"{month_name}-{v.day}"


def _mir_no_label(cell) -> str:
    """See module docstring - reconstructs the originally-typed 'MM/DD'
    MIR-number text from a cell Excel silently turned into a date."""
    v = cell.value
    if not isinstance(v, datetime.datetime):
        return to_str(v)
    return f"{v.month:02d}/{v.day:02d}"


def parse_achhad_mir_xlsx(file_bytes: bytes) -> list[ParsedAchhadMirEntry]:
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
        party_name = to_str(ws[f"D{r}"].value)
        if not party_name:
            continue  # a blank Party Name means an empty row, same convention as HRS's parser

        entries.append(
            ParsedAchhadMirEntry(
                month=_month_label(ws[f"A{r}"]),
                mir_no=_mir_no_label(ws[f"B{r}"]),
                mir_date=to_date(ws[f"C{r}"].value),
                po_number_raw=to_code_str(ws[f"H{r}"].value),
                party_name=party_name,
                state=to_str(ws[f"E{r}"].value),
                invoice_no=to_str(ws[f"F{r}"].value),
                invoice_date=to_date(ws[f"G{r}"].value),
                material_description=to_str(ws[f"J{r}"].value),
                qty=to_decimal(ws[f"K{r}"].value),
                uom=to_str(ws[f"L{r}"].value),
                rate=to_decimal(ws[f"M{r}"].value),
                net=to_decimal(ws[f"N{r}"].value),
                discount_rate_pct=to_decimal(ws[f"O{r}"].value),
                discount_amt=to_decimal(ws[f"P{r}"].value),
                others=to_decimal(ws[f"Q{r}"].value),
                taxable_value=to_decimal(ws[f"R{r}"].value),
                tax_rate_pct=to_decimal(ws[f"S{r}"].value),
                igst=to_decimal(ws[f"T{r}"].value),
                cgst_rate_pct=to_decimal(ws[f"U{r}"].value),
                cgst_amt=to_decimal(ws[f"V{r}"].value),
                sgst_rate_pct=to_decimal(ws[f"W{r}"].value),
                sgst_amt=to_decimal(ws[f"X{r}"].value),
                other_taxes_excl_gst=to_decimal(ws[f"Y{r}"].value),
                total_amount=to_decimal(ws[f"Z{r}"].value),
                tcs_rate_pct=to_decimal(ws[f"AA{r}"].value),
                tcs_amt=to_decimal(ws[f"AB{r}"].value),
                invoice_final_value=to_decimal(ws[f"AC{r}"].value),
                plant_tag=to_str(ws[f"AD{r}"].value),
                dept_use=to_str(ws[f"AE{r}"].value),
                material_category=to_str(ws[f"AF{r}"].value),
                remarks=to_str(ws[f"AG{r}"].value),
                source_row_ref=str(r),
            )
        )
    return entries
