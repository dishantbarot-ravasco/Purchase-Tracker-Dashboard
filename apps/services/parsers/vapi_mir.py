"""
apps/services/parsers/vapi_mir.py — parses RTP VAPI MIR FILE 2026-27.xlsx,
' MIR FILE 26-27 RM' sheet (note the leading space - confirmed against the
live file), into a flat list of row dicts ready to upsert into
RTPVapiMIREntry.

**Header changed 2026-09-11** - the project owner had the Vapi plant head
add a new 'PURCHASE ORDER' column specifically to improve PO<->MIR matching
(Vapi's original 'SAP P.O' field, still present, was 100% blank on every
row checked - see the RTP-Vapi section header comment in apps/core/
models.py). Confirmed directly against the live file this session: the new
column was inserted at K, right after 'STATE', shifting every column from
the old K ('INV. NO.') onward one letter to the right, through the end of
the sheet (old AC 'DATE SEND TO H O' is now AD). Columns A-J are unchanged.

Exact header (row 1; data from row 2), verified against the live file this
session:
A MONTH | B MIR NO . | C DATE | D SAP P.O | E SAP GRN NO | F PARK INV NO. |
G POST | H post no correction | I PARTY NAME | J STATE | K PURCHASE ORDER |
L INV. NO. | M DATE | N ITEM NAME | O item code | P QTY | Q UOM | R RATE |
S TAXABLE VALUE | T Others With GST | U GST  | V  IGST | W CGST | X SGST |
Y Other exclu. gst | Z TCS | AA Inv. Final Value | AB MATERIAL CATEGORY |
AC DATE SEND TO OFFICE | AD DATE SEND TO H O

The new column K's real values are plain SAP PO numbers written as a float
by openpyxl (e.g. 1000001437.0, matching this app's own PO-number format
exactly) - read with to_code_str() to drop the trailing '.0', same as every
other PO-number-shaped column across the three plants' MIR parsers.

See the big "RTP-Vapi" section header comment in apps/core/models.py for
the full rationale behind how this genuinely differs from HRS's and
Achhad's MIR columns (no Net/discount columns, GST split into one overall
rate plus three amount-only IGST/CGST/SGST columns, a single TCS amount
rather than a rate+amount pair, two extra "date sent" columns, and four
columns - SAP P.O, SAP GRN NO, PARK INV NO., POST, post no correction,
item code - that were empty on every one of the ~1,330 real rows checked
before the new PURCHASE ORDER column existed, but are still modeled for
forward compatibility).

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
from openpyxl.utils import column_index_from_string

from apps.services.parsers.common import stream_rows, to_code_str, to_date, to_decimal, to_str

# ── Column/row layout constants ─────────────────────────────────────────────

SHEET_NAME = " MIR FILE 26-27 RM"
HEADER_ROW = 1
DATA_START_ROW = 2

EXPECTED_HEADERS = {
    "A": "MONTH", "B": "MIR NO .", "C": "DATE", "D": "SAP P.O", "E": "SAP GRN NO",
    "F": "PARK INV NO.", "G": "POST", "H": "post no correction", "I": "PARTY NAME",
    "J": "STATE", "K": "PURCHASE ORDER", "L": "INV. NO.", "M": "DATE", "N": "ITEM NAME",
    "O": "item code", "P": "QTY", "Q": "UOM", "R": "RATE", "S": "TAXABLE VALUE",
    "T": "Others With GST", "U": "GST", "V": "IGST", "W": "CGST", "X": "SGST",
    "Y": "Other exclu. gst", "Z": "TCS", "AA": "Inv. Final Value", "AB": "MATERIAL CATEGORY",
    "AC": "DATE SEND TO OFFICE", "AD": "DATE SEND TO H O",
}


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedVapiMirEntry:
    month: str
    mir_no: str
    mir_date: object
    po_number_raw: str
    sap_po_number: str
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
    # read_only=True - see parsers/stock.py's own comment on this same line
    # for why (a real production OOM on Render, caused by default-mode
    # loading pivot table caches this app never reads). The data loop below
    # streams via common.stream_rows() rather than per-cell random access -
    # see that function's own docstring for why (ws.max_row/max_column can be
    # None in read_only mode, and random access on a read_only worksheet is
    # O(n) per call, not O(1) - both confirmed against real production data).
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise HeaderMismatch(f"Expected sheet {SHEET_NAME!r}, found sheets: {wb.sheetnames}")
    ws = wb[SHEET_NAME]

    for col, expected in EXPECTED_HEADERS.items():
        actual = _strip(ws[f"{col}{HEADER_ROW}"].value)
        if actual != expected:
            raise HeaderMismatch(f"Column {col}{HEADER_ROW}: expected {expected!r}, found {actual!r}")

    entries = []
    max_col = column_index_from_string("AD")
    for r, c in stream_rows(ws, DATA_START_ROW, max_col):
        party_name = to_str(c["I"].value)
        if not party_name:
            continue  # a blank Party Name means an empty row, same convention as HRS's/Achhad's parsers

        entries.append(
            ParsedVapiMirEntry(
                month=_month_label(c["A"]),
                mir_no=to_str(c["B"].value),
                mir_date=to_date(c["C"].value),
                sap_po_number=to_code_str(c["D"].value),  # ~100% blank on real data - kept for forward compatibility, not used for matching
                po_number_raw=to_code_str(c["K"].value),  # NEW 2026-09-11 - the actually-populated PO-number column, used for matching
                sap_grn_number=to_code_str(c["E"].value),
                park_invoice_no=to_code_str(c["F"].value),
                post=to_str(c["G"].value),
                post_no_correction=to_str(c["H"].value),
                party_name=party_name,
                state=to_str(c["J"].value),
                invoice_no=to_code_str(c["L"].value),
                invoice_date=to_date(c["M"].value),
                material_description=to_str(c["N"].value),
                item_code=to_code_str(c["O"].value),
                qty=to_decimal(c["P"].value),
                uom=to_str(c["Q"].value),
                rate=to_decimal(c["R"].value),
                taxable_value=to_decimal(c["S"].value),
                others_with_gst=to_decimal(c["T"].value),
                # a whole percentage (18.00 = 18%), not a fraction like HRS/Achhad's 0.18 -
                # RTPVapiMIREntry.gst_rate_pct is decimal_places=2 for this reason, don't "fix" it to match
                gst_rate_pct=to_decimal(c["U"].value),
                igst_amt=to_decimal(c["V"].value),
                cgst_amt=to_decimal(c["W"].value),
                sgst_amt=to_decimal(c["X"].value),
                other_taxes_excl_gst=to_decimal(c["Y"].value),
                tcs_amt=to_decimal(c["Z"].value),
                invoice_final_value=to_decimal(c["AA"].value),
                material_category=to_str(c["AB"].value),
                date_sent_to_office=to_date(c["AC"].value),
                date_sent_to_ho=to_date(c["AD"].value),
                source_row_ref=str(r),
            )
        )
    return entries
