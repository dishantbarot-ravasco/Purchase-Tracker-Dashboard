"""
apps/services/parsers/vapi_mir.py — parses RTP VAPI MIR FILE 2026-27.xlsx,
' MIR FILE 26-27 RM' sheet (note the leading space - confirmed against the
live file), into a flat list of row dicts ready to upsert into
RTPVapiMIREntry.

Exact header (row 1; data from row 2), verified against the live file this
session:
A MONTH | B MIR NO . | C DATE | D SAP P.O | E SAP GRN NO | F PARK INV NO. |
G POST | H post no correction | I PARTY NAME | J STATE | K INV. NO. |
L DATE | M ITEM NAME | N item code | O QTY | P UOM | Q RATE |
R TAXABLE VALUE | S Others With GST | T GST  | U  IGST | V CGST | W SGST |
X Other exclu. gst | Y TCS | Z Inv. Final Value | AA MATERIAL CATEGORY |
AB DATE SEND TO OFFICE | AC DATE SEND TO H O

See the big "RTP-Vapi" section header comment in apps/core/models.py for the
full rationale behind how this genuinely differs from HRS's and Achhad's MIR
columns (no Net/discount columns, GST split into one overall rate plus
three amount-only IGST/CGST/SGST columns, a single TCS amount rather than a
rate+amount pair, two extra "date sent" columns, and five columns - SAP P.O,
SAP GRN NO, PARK INV NO., POST, post no correction, item code - that were
empty on every one of the ~1,330 real rows in the live file but are still
modeled for forward compatibility).

Unlike Achhad's MIR file, MONTH (A) is always a real Excel date in the live
file - no cells were left as literal typed text, so there's no ambiguity to
reverse-engineer the way achhad_mir.py's _month_label()/_mir_no_label() have
to. MIR NO. (B) is always plain text (e.g. 'MIR01/04'), so it's read with a
plain to_str() too.
"""

import datetime
import io
from dataclasses import dataclass

import openpyxl

from apps.services.parsers.common import to_code_str, to_date, to_decimal, to_str

# ── Column/row layout constants ─────────────────────────────────────────────

SHEET_NAME = " MIR FILE 26-27 RM"
HEADER_ROW = 1
DATA_START_ROW = 2

EXPECTED_HEADERS = {
    "A": "MONTH", "B": "MIR NO .", "C": "DATE", "D": "SAP P.O", "E": "SAP GRN NO",
    "F": "PARK INV NO.", "G": "POST", "H": "post no correction", "I": "PARTY NAME",
    "J": "STATE", "K": "INV. NO.", "L": "DATE", "M": "ITEM NAME", "N": "item code",
    "O": "QTY", "P": "UOM", "Q": "RATE", "R": "TAXABLE VALUE", "S": "Others With GST",
    "T": "GST", "U": "IGST", "V": "CGST", "W": "SGST", "X": "Other exclu. gst",
    "Y": "TCS", "Z": "Inv. Final Value", "AA": "MATERIAL CATEGORY",
    "AB": "DATE SEND TO OFFICE", "AC": "DATE SEND TO H O",
}


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedVapiMirEntry:
    month: str
    mir_no: str
    mir_date: object
    po_number_raw: str
    sap_grn_number: str
    park_invoice_no: str
    post: str
    post_no_correction: str
    party_name: str
    state: str
    invoice_no: str
    invoice_date: object
    material_description: str
    item_code: str
    qty: object
    uom: str
    rate: object
    taxable_value: object
    others_with_gst: object
    gst_rate_pct: object
    igst_amt: object
    cgst_amt: object
    sgst_amt: object
    other_taxes_excl_gst: object
    tcs_amt: object
    invoice_final_value: object
    material_category: str
    date_sent_to_office: object
    date_sent_to_ho: object
    source_row_ref: str


class HeaderMismatch(Exception):
    pass


def _strip(v):
    return (v or "").strip() if isinstance(v, str) else v


# ── Excel-autoconvert workaround (Month column) ─────────────────────────────

def _month_label(cell) -> str:
    """MONTH is always a real datetime in the live file (confirmed this
    session - no Achhad-style typed-text cells to reconstruct), so this just
    formats it as 'Mon-YY' (e.g. 'Apr-26') straight from the cell's own
    value rather than needing achhad_mir.py's number_format inspection."""
    v = cell.value
    if not isinstance(v, datetime.datetime):
        return to_str(v)
    return v.strftime("%b-%y")


# ── Public entry point ───────────────────────────────────────────────────────

def parse_vapi_mir_xlsx(file_bytes: bytes) -> list[ParsedVapiMirEntry]:
    """Reads the ' MIR FILE 26-27 RM' sheet and returns one ParsedVapiMirEntry
    per data row. Raises HeaderMismatch immediately if the sheet name or any
    header cell doesn't match what this parser was built against."""
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
        party_name = to_str(ws[f"I{r}"].value)
        if not party_name:
            continue  # a blank Party Name means an empty row, same convention as HRS's/Achhad's parsers

        entries.append(
            ParsedVapiMirEntry(
                month=_month_label(ws[f"A{r}"]),
                mir_no=to_str(ws[f"B{r}"].value),
                mir_date=to_date(ws[f"C{r}"].value),
                po_number_raw=to_code_str(ws[f"D{r}"].value),  # ~100% blank on real data - matching runs on the weighted score alone for Vapi
                sap_grn_number=to_code_str(ws[f"E{r}"].value),
                park_invoice_no=to_code_str(ws[f"F{r}"].value),
                post=to_str(ws[f"G{r}"].value),
                post_no_correction=to_str(ws[f"H{r}"].value),
                party_name=party_name,
                state=to_str(ws[f"J{r}"].value),
                invoice_no=to_code_str(ws[f"K{r}"].value),
                invoice_date=to_date(ws[f"L{r}"].value),
                material_description=to_str(ws[f"M{r}"].value),
                item_code=to_code_str(ws[f"N{r}"].value),
                qty=to_decimal(ws[f"O{r}"].value),
                uom=to_str(ws[f"P{r}"].value),
                rate=to_decimal(ws[f"Q{r}"].value),
                taxable_value=to_decimal(ws[f"R{r}"].value),
                others_with_gst=to_decimal(ws[f"S{r}"].value),
                # a whole percentage (18.00 = 18%), not a fraction like HRS/Achhad's 0.18 -
                # RTPVapiMIREntry.gst_rate_pct is decimal_places=2 for this reason, don't "fix" it to match
                gst_rate_pct=to_decimal(ws[f"T{r}"].value),
                igst_amt=to_decimal(ws[f"U{r}"].value),
                cgst_amt=to_decimal(ws[f"V{r}"].value),
                sgst_amt=to_decimal(ws[f"W{r}"].value),
                other_taxes_excl_gst=to_decimal(ws[f"X{r}"].value),
                tcs_amt=to_decimal(ws[f"Y{r}"].value),
                invoice_final_value=to_decimal(ws[f"Z{r}"].value),
                material_category=to_str(ws[f"AA{r}"].value),
                date_sent_to_office=to_date(ws[f"AB{r}"].value),
                date_sent_to_ho=to_date(ws[f"AC{r}"].value),
                source_row_ref=str(r),
            )
        )
    return entries
