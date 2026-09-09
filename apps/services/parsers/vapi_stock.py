"""
apps/services/parsers/vapi_stock.py — parses RAVASCO VAPI RM STOCK
FILE.xlsx, 'Stock' sheet, into a flat list of row dicts ready to upsert
into RTPVapiRMLot.

Exact header (row 6; data from row 7), verified against the live file this
session:
A SR NO. | B PLANT | C Description | D Category | E Sub Category | F UOM |
G Opening Stock | H REC | I ISSUE | J Today Stock | K Basic Rate | L Value |
M Rec. DT. | N Supplier Name | O BILLING ON PLANT | P MATERIAL LOCATION |
Q HSN CODE

Rows 1-5 are a document-control title block (company name/address, Document
No./Issue No./Revision No./Effective Date), not part of the table - the
real header starts at row 6, one row lower than HRS's Stock sheet (row 6 is
also HRS's header row, purely coincidentally the same number).

See the big "RTP-Vapi" section header comment in apps/core/models.py for the
full rationale behind PLANT/Supplier Name/the three extra columns this
sheet has that HRS's doesn't. No category-divider-row convention like
Achhad's Stock sheet - plain single-header-row shape like HRS's, with a real
per-row serial number (1-168 in the live file, no gaps, confirmed this
session), so this parser doesn't need achhad_stock.py's divider-tracking
logic at all.

Rec. DT. (M) is a mix of real datetime cells and dd/mm/yyyy text cells in
the live file (confirmed) - to_date() already handles both. HSN CODE (Q) is
a float in the live file for a numeric HSN (e.g. 40012200.0) - read with
to_code_str() to drop the trailing '.0', same reasoning as Achhad's SAP Code
column.
"""

import io
from dataclasses import dataclass

import openpyxl
from openpyxl.utils import column_index_from_string

from apps.services.parsers.common import stream_rows, to_code_str, to_date, to_decimal, to_str

# ── Column/row layout constants ─────────────────────────────────────────────

SHEET_NAME = "Stock"
HEADER_ROW = 6
DATA_START_ROW = 7

EXPECTED_HEADERS = {
    "A": "SR NO.", "B": "PLANT", "C": "Description", "D": "Category", "E": "Sub Category",
    "F": "UOM", "G": "Opening\nStock", "H": "REC", "I": "ISSUE", "J": "Today\nStock",
    "K": "Basic Rate", "L": "Value", "M": "Rec. DT.", "N": "Supplier Name",
    "O": "BILLING ON PLANT", "P": "MATERIAL LOCATION", "Q": "HSN CODE",
}


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedVapiStockLot:
    sr_no: object
    plant_tag: str
    description: str
    category: str
    sub_category: str
    uom: str
    opening_stock: object
    received: object
    issued: object
    todays_stock: object
    basic_rate: object
    value: object
    received_date: object
    supplier_name: str
    billing_on_plant: str
    material_location: str
    hsn_code: str
    source_row_ref: str


class HeaderMismatch(Exception):
    pass


def _strip(v):
    return (v or "").strip() if isinstance(v, str) else v


# ── Public entry point ───────────────────────────────────────────────────────

def parse_vapi_stock_xlsx(file_bytes: bytes) -> list[ParsedVapiStockLot]:
    """Reads the 'Stock' sheet and returns one ParsedVapiStockLot per lot row.
    Raises HeaderMismatch immediately if the sheet name or any header cell
    doesn't match what this parser was built against."""
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

    lots = []
    max_col = column_index_from_string("Q")
    for r, c in stream_rows(ws, DATA_START_ROW, max_col):
        description = to_str(c["C"].value)
        if not description:
            continue

        sr_no_raw = c["A"].value
        sr_no = int(sr_no_raw) if isinstance(sr_no_raw, (int, float)) else None

        lots.append(
            ParsedVapiStockLot(
                sr_no=sr_no,
                plant_tag=to_str(c["B"].value),  # this sheet is a shared multi-plant ledger (RTP-1/HRS/RTP-2 rows all seen live), not Vapi-exclusive
                description=description,
                category=to_str(c["D"].value),
                sub_category=to_str(c["E"].value),
                uom=to_str(c["F"].value),
                opening_stock=to_decimal(c["G"].value) or 0,
                received=to_decimal(c["H"].value) or 0,
                issued=to_decimal(c["I"].value) or 0,
                todays_stock=to_decimal(c["J"].value) or 0,
                basic_rate=to_decimal(c["K"].value),
                value=to_decimal(c["L"].value),
                received_date=to_date(c["M"].value),
                supplier_name=to_str(c["N"].value),
                billing_on_plant=to_str(c["O"].value),
                material_location=to_str(c["P"].value),
                hsn_code=to_code_str(c["Q"].value),
                source_row_ref=str(r),
            )
        )
    return lots
