"""
apps/services/parsers/achhad_stock.py — parses RAVASCO ACHHAD RM STOCK
FILE.xlsx into a flat list of row dicts ready to upsert into
RTPAchhadStockLot.

Genuinely different shape from HRS's Stock file (apps/services/parsers/
stock.py), confirmed against the live file this session - not just
relabeled columns:

  - The sheet is a single tab named after the current month (e.g.
    'Aug 26-27'), re-titled every month rather than kept at a fixed name
    like HRS's 'Stock' tab. This parser reads whichever sheet is present
    (wb.sheetnames[0] - the file has exactly one tab) instead of matching a
    literal sheet name.
  - No vendor/party-name column at all. HRS's Stock sheet is one row per
    (material, vendor lot); Achhad's is one row per material, full stop -
    there is no per-vendor split to key off of. This is why
    RTPAchhadMirStockMatch (apps/core/matching_achhad.py) can only gate on
    normalized material description, not (material, vendor) the way HRS's
    matcher does (see apps/services/matching_achhad.py) - a real,
    weaker-confidence difference, not an oversight.
  - Header sits across two rows for the numbered day-of-month columns
    (a 'Recp./Issue' sub-header under a bare day number), and the sheet
    embeds a full daily receipt/issue transaction matrix (one Recp/Issue
    column pair per day of the month) plus a same-day dispatch breakdown by
    destination sub-plant (HRA/HRS/RTP VAPI/SILLI/RETURN TO PARTY/MGPL/2M
    Elastomers) to the right of that. Deliberately NOT parsed here, same
    scoping decision HRS's Stock parser made for its own formula-only
    Receipt/Issue tab references - this parser captures the per-material
    summary row only (opening/received/issued/closing/value/physical),
    mirroring the level HRS's own StockLot reached. A future pass could add
    a daily-snapshot-shaped model for the per-day matrix if that level of
    detail turns out to matter.
  - Category is not a column - it's a section-divider row (only the
    'NAME OF MATERIAL' cell filled, e.g. 'Natural Rubber' / 'Synthetic
    Rubbers') scattered through the data. This parser tracks the most
    recent divider seen and stamps it onto every material row under it,
    same idea as a merged-cell category column would give if the sheet
    used one. A trailing 'TOTAL ...' row uses the same "column A blank"
    shape as a divider row but is a totals footer, not a category - it's
    filtered out the same way divider rows are (never emitted as a lot),
    so no special-case check for the word 'TOTAL' is needed.

Exact header (row 2 for the fixed columns; data from row 5), verified
against the live file this session:
A (unlabeled - overall serial) | B (unlabeled - per-category serial) |
C NAME OF MATERIAL | D Rec.Date | E SAP Code | F RATE | G Zone | H MSL |
I Opening | J Received | K Issued | L Closing | M Value | N Physical
"""

import io
from dataclasses import dataclass

import openpyxl

from apps.services.parsers.common import to_code_str, to_date, to_decimal, to_str

# ── Column/row layout constants ─────────────────────────────────────────────

HEADER_ROW = 2
DATA_START_ROW = 5

# A and B are genuinely unlabeled in the source file (confirmed this
# session) - only C-N are header-checked.
EXPECTED_HEADERS = {
    "C": "NAME OF MATERIAL", "D": "Rec.Date", "E": "SAP Code", "F": "RATE",
    "G": "Zone", "H": "MSL", "I": "Opening", "J": "Received", "K": "Issued",
    "L": "Closing", "M": "Value", "N": "Physical",
}


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedAchhadStockLot:
    overall_sr_no: object
    category_sr_no: object
    description: str
    category: str
    sap_code: str
    rate: object
    zone: str
    msl: object
    opening_stock: object
    received: object
    issued: object
    todays_stock: object
    value: object
    physical_stock: object
    received_date: object
    source_row_ref: str


class HeaderMismatch(Exception):
    pass


def _strip(v):
    return (v or "").strip() if isinstance(v, str) else v


# ── Public entry point ───────────────────────────────────────────────────────

def parse_achhad_stock_xlsx(file_bytes: bytes) -> list[ParsedAchhadStockLot]:
    """Reads the workbook's single tab and returns one ParsedAchhadStockLot
    per material row, skipping category-divider and totals-footer rows.
    Raises HeaderMismatch immediately if the workbook has no sheets or any
    checked header cell doesn't match what this parser was built against."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    if not wb.sheetnames:
        raise HeaderMismatch("Workbook has no sheets.")
    ws = wb[wb.sheetnames[0]]  # tab is renamed every month (e.g. 'Aug 26-27') - see module docstring

    for col, expected in EXPECTED_HEADERS.items():
        actual = _strip(ws[f"{col}{HEADER_ROW}"].value)
        if actual != expected:
            raise HeaderMismatch(f"Column {col}{HEADER_ROW}: expected {expected!r}, found {actual!r}")

    lots = []
    current_category = ""
    for r in range(DATA_START_ROW, ws.max_row + 1):
        overall_sr_no_raw = ws[f"A{r}"].value
        description = to_str(ws[f"C{r}"].value)

        if overall_sr_no_raw is None:
            # A category-divider row (or the trailing TOTAL footer, which
            # has the same shape) - never a material row. Only a divider
            # actually names a category worth remembering.
            if description and not description.upper().startswith("TOTAL"):
                current_category = description
            continue
        if not description:
            continue

        lots.append(
            ParsedAchhadStockLot(
                overall_sr_no=int(overall_sr_no_raw) if isinstance(overall_sr_no_raw, (int, float)) else None,
                category_sr_no=int(ws[f"B{r}"].value) if isinstance(ws[f"B{r}"].value, (int, float)) else None,
                description=description,
                category=current_category,
                sap_code=to_code_str(ws[f"E{r}"].value),
                rate=to_decimal(ws[f"F{r}"].value),
                zone=to_str(ws[f"G{r}"].value),
                msl=to_decimal(ws[f"H{r}"].value),
                opening_stock=to_decimal(ws[f"I{r}"].value) or 0,
                received=to_decimal(ws[f"J{r}"].value) or 0,
                issued=to_decimal(ws[f"K{r}"].value) or 0,
                todays_stock=to_decimal(ws[f"L{r}"].value) or 0,
                value=to_decimal(ws[f"M{r}"].value),
                physical_stock=to_decimal(ws[f"N{r}"].value),
                received_date=to_date(ws[f"D{r}"].value),
                source_row_ref=str(r),
            )
        )
    return lots
