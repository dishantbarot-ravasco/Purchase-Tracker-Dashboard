"""
Parses RAVASCO VAPI RM STOCK FILE.xlsx, 'Stock' sheet, into a flat list of
row dicts ready to upsert into RTPVapiStockLot.

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

from apps.services.parsers.common import to_code_str, to_date, to_decimal, to_str

SHEET_NAME = "Stock"
HEADER_ROW = 6
DATA_START_ROW = 7

EXPECTED_HEADERS = {
    "A": "SR NO.", "B": "PLANT", "C": "Description", "D": "Category", "E": "Sub Category",
    "F": "UOM", "G": "Opening\nStock", "H": "REC", "I": "ISSUE", "J": "Today\nStock",
    "K": "Basic Rate", "L": "Value", "M": "Rec. DT.", "N": "Supplier Name",
    "O": "BILLING ON PLANT", "P": "MATERIAL LOCATION", "Q": "HSN CODE",
}


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


def parse_vapi_stock_xlsx(file_bytes: bytes) -> list[ParsedVapiStockLot]:
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise HeaderMismatch(f"Expected sheet {SHEET_NAME!r}, found sheets: {wb.sheetnames}")
    ws = wb[SHEET_NAME]

    for col, expected in EXPECTED_HEADERS.items():
        actual = _strip(ws[f"{col}{HEADER_ROW}"].value)
        if actual != expected:
            raise HeaderMismatch(f"Column {col}{HEADER_ROW}: expected {expected!r}, found {actual!r}")

    lots = []
    for r in range(DATA_START_ROW, ws.max_row + 1):
        description = to_str(ws[f"C{r}"].value)
        if not description:
            continue

        sr_no_raw = ws[f"A{r}"].value
        sr_no = int(sr_no_raw) if isinstance(sr_no_raw, (int, float)) else None

        lots.append(
            ParsedVapiStockLot(
                sr_no=sr_no,
                plant_tag=to_str(ws[f"B{r}"].value),
                description=description,
                category=to_str(ws[f"D{r}"].value),
                sub_category=to_str(ws[f"E{r}"].value),
                uom=to_str(ws[f"F{r}"].value),
                opening_stock=to_decimal(ws[f"G{r}"].value) or 0,
                received=to_decimal(ws[f"H{r}"].value) or 0,
                issued=to_decimal(ws[f"I{r}"].value) or 0,
                todays_stock=to_decimal(ws[f"J{r}"].value) or 0,
                basic_rate=to_decimal(ws[f"K{r}"].value),
                value=to_decimal(ws[f"L{r}"].value),
                received_date=to_date(ws[f"M{r}"].value),
                supplier_name=to_str(ws[f"N{r}"].value),
                billing_on_plant=to_str(ws[f"O{r}"].value),
                material_location=to_str(ws[f"P{r}"].value),
                hsn_code=to_code_str(ws[f"Q{r}"].value),
                source_row_ref=str(r),
            )
        )
    return lots
