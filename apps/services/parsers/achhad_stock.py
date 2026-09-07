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
  - Header sits across two rows for the numbered day-of-month columns
    (a 'Recp./Issue' sub-header under a bare day number). The sheet embeds a
    full daily receipt/issue transaction matrix (one Recp/Issue column pair
    per day of the month) - parsed here as of 2026-09-08 (see
    ParsedAchhadStockLot.daily_movements below) after confirming against the
    live file this session that it reconciles exactly: summed across all 31
    day-columns, Recp equals the row's own `Received` summary and Issue
    equals `Issued`, for 323 of 325 real material rows exactly (the other 2
    differ only by float rounding in the verification script, not the
    source data) - not noisy, safe to trust. The point of parsing it isn't
    that it's more accurate than the summary (it's numerically identical) -
    it's that it gives a real CALENDAR DATE for each receipt/issue, which
    the monthly summary alone can't (see stock_consumption.py's own
    docstring on why RTP-Achhad's `issued` column couldn't be used as a
    Days-Left Engine signal - that concern was about the *monthly* summary
    resetting each period, not about this daily-dated data underneath it).
    Same-day dispatch breakdown by destination sub-plant (HRA/HRS/RTP VAPI/
    SILLI/RETURN TO PARTY/MGPL/2M Elastomers) to the right of the day matrix
    is still NOT parsed - no feature consumes inter-plant dispatch tracking
    today, unlike the day-matrix's Days-Left Engine use.
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

import datetime
import io
import re
from dataclasses import dataclass, field

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

# Row 1 carries a literal "RM STOCK - DD.MM.YYYY" title (confirmed against
# the live file - a single value in A1, nowhere else on that row) - the only
# place in the sheet that names a full 4-digit year, needed to turn the day
# matrix's bare day numbers (1-31) into real dates. The day-of-month tab name
# itself ("Aug 26-27") is a fiscal-year-range label, not precise enough.
_TITLE_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedDailyMovement:
    """One day's real receipt/issue activity for one material row - see
    module docstring for why this is trusted (reconciles exactly with the
    row's own Received/Issued summary) and why it's captured (a real
    calendar date the summary alone can't give)."""

    movement_date: datetime.date
    received: object
    issued: object


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
    daily_movements: list = field(default_factory=list)  # list[ParsedDailyMovement], activity days only


class HeaderMismatch(Exception):
    pass


def _strip(v):
    return (v or "").strip() if isinstance(v, str) else v


def _sheet_month_year(ws) -> tuple[int, int]:
    """Extracts (year, month) from row 1's "RM STOCK - DD.MM.YYYY" title.
    Raises HeaderMismatch if that title isn't there in the expected shape -
    better to fail loudly than silently misdate every movement in the file."""
    title = _strip(ws["A1"].value) or ""
    m = _TITLE_DATE_RE.search(title)
    if not m:
        raise HeaderMismatch(f"Expected a 'RM STOCK - DD.MM.YYYY' title in A1, found {title!r}")
    _day, month, year = m.groups()
    return int(year), int(month)


def _day_columns(ws) -> dict[int, tuple[int, int]]:
    """Scans HEADER_ROW for bare day numbers (1-31) - each one marks a
    (Recp., Issue) column pair, confirmed against the live file to always
    appear as two adjacent columns per day. Returns {day: (recp_col, issue_col)}
    using 1-based openpyxl column numbers. Column O is where the day matrix
    starts in the live file, but this scans dynamically rather than
    hardcoding that, since a template revision could shift it."""
    day_cols: dict[int, tuple[int, int]] = {}
    col = 1
    max_col = ws.max_column
    while col <= max_col:
        v = ws.cell(row=HEADER_ROW, column=col).value
        if isinstance(v, (int, float)) and float(v).is_integer() and 1 <= int(v) <= 31:
            day_cols[int(v)] = (col, col + 1)
            col += 2
        else:
            col += 1
    return day_cols


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

    year, month = _sheet_month_year(ws)
    day_cols = _day_columns(ws)

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

        daily_movements = []
        for day, (recp_col, issue_col) in day_cols.items():
            try:
                movement_date = datetime.date(year, month, day)
            except ValueError:
                continue  # e.g. day 31 in a 30-day month - the column exists, the date doesn't
            recp = to_decimal(ws.cell(row=r, column=recp_col).value) or 0
            issue = to_decimal(ws.cell(row=r, column=issue_col).value) or 0
            if recp == 0 and issue == 0:
                continue  # only real activity days are worth a row - see module docstring
            daily_movements.append(ParsedDailyMovement(movement_date=movement_date, received=recp, issued=issue))
        daily_movements.sort(key=lambda m: m.movement_date)

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
                daily_movements=daily_movements,
            )
        )
    return lots
