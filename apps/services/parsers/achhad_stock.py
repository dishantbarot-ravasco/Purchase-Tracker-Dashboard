"""
apps/services/parsers/achhad_stock.py — parses RAVASCO ACHHAD RM STOCK
FILE.xlsx into a flat list of row dicts ready to upsert into
RTPAchhadRMLot.

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

**Superseded (2026-09-09): the daily Recp./Issue day-matrix (columns O
onward) is deliberately NOT parsed here, per the project owner's own
decision - this parser reads only the fixed A-N header columns, the same
shape as HRS's/Vapi's Stock parsers. A day-matrix parse (ParsedDailyMovement,
_day_columns(), _sheet_month_year()) existed briefly (added 2026-09-08,
backing RTPAchhadRMDailyMovement's "Issued Today" dated signal - see
consumption_report.py's own module docstring) and was removed the same week,
before it ever needed a schema change to undo - RTPAchhadRMDailyMovement's
model/migration/consumers are left as-is (they already degrade gracefully to
their own existing monthly-summary `isEstimate` fallback when no new rows
land - see consumption_report.py), just no longer written to by this parser.
If a per-day Achhad signal is wanted again later, it needs to be rebuilt from
scratch - no removed code is kept around half-wired.
"""

import io
from dataclasses import dataclass

import openpyxl

from apps.services.parsers.common import stream_rows, to_code_str, to_date, to_decimal, to_str

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
    # read_only=True - see parsers/stock.py's own comment on this same line
    # for why (a real production OOM on Render, caused by default-mode
    # loading pivot table caches this app never reads). The data loop below
    # streams via common.stream_rows() rather than per-cell random access -
    # see that function's own docstring for why (ws.max_row/max_column can be
    # None in read_only mode, and random access on a read_only worksheet is
    # O(n) per call, not O(1) - both confirmed against real production data).
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    if not wb.sheetnames:
        raise HeaderMismatch("Workbook has no sheets.")
    ws = wb[wb.sheetnames[0]]  # tab is renamed every month (e.g. 'Aug 26-27') - see module docstring

    for col, expected in EXPECTED_HEADERS.items():
        actual = _strip(ws[f"{col}{HEADER_ROW}"].value)
        if actual != expected:
            raise HeaderMismatch(f"Column {col}{HEADER_ROW}: expected {expected!r}, found {actual!r}")

    lots = []
    current_category = ""
    max_col = 14  # column N - the last fixed column this parser reads
    for r, c in stream_rows(ws, DATA_START_ROW, max_col):
        overall_sr_no_raw = c["A"].value
        description = to_str(c["C"].value)

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
                category_sr_no=int(c["B"].value) if isinstance(c["B"].value, (int, float)) else None,
                description=description,
                category=current_category,
                sap_code=to_code_str(c["E"].value),
                rate=to_decimal(c["F"].value),
                zone=to_str(c["G"].value),
                msl=to_decimal(c["H"].value),
                opening_stock=to_decimal(c["I"].value) or 0,
                received=to_decimal(c["J"].value) or 0,
                issued=to_decimal(c["K"].value) or 0,
                todays_stock=to_decimal(c["L"].value) or 0,
                value=to_decimal(c["M"].value),
                physical_stock=to_decimal(c["N"].value),
                received_date=to_date(c["D"].value),
                source_row_ref=str(r),
            )
        )
    return lots
