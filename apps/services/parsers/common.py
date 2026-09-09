"""
apps/services/parsers/common.py — shared cell/CSV-value coercion and name-
normalization helpers used by every plant's parser module (mir/stock/po_csv
and their achhad_/vapi_ variants).

Kept dependency-free (no Django imports) so these can be unit-tested with
nothing but plain Python, and so a parser module never has to special-case a
plant's own quirky raw value - each plant's parser calls the same handful of
`to_*()` helpers and gets back a clean, uniform Python type regardless of
which spreadsheet layout it came from.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

from openpyxl.utils import get_column_letter

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d.%m.%y"]


# ── Cell/CSV-value coercion helpers ─────────────────────────────────────────

def to_decimal(value) -> Decimal | None:
    """Coerces a raw cell/CSV value to Decimal, or None. 'NULL' (the literal
    string used throughout these source files), blanks, and unparseable
    values all become None rather than raising - a bad number is a data
    problem to surface downstream (e.g. as a sync warning), not a crash."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s or s.upper() == "NULL":
        return None
    s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def stream_rows(ws, min_row: int, max_col: int):
    """Streams data rows forward from min_row, one underlying XML <row>
    element at a time - the correct replacement for the
    `range(DATA_START_ROW, ws.max_row + 1)` + per-cell random access
    (`ws[f'{col}{r}'].value` / `ws.cell(row=, column=)`) pattern every
    plant's MIR/Stock parser used to use on a `read_only=True` worksheet.
    That pattern had two real, confirmed-in-production problems (2026-09-09):

    1. `ws.max_row`/`ws.max_column` are read straight off the workbook XML's
       <dimension> tag in read_only mode, rather than computed, and come back
       None when that tag is missing or stale - every one of this app's live
       HRS/Achhad/Vapi MIR/Stock files hit this (confirmed directly; also
       visible in production as the openpyxl "invalid dependency definitions"
       warnings on their pivotCache parts, logged right before the crash).
       `ws.max_row + 1` then raised `TypeError: unsupported operand type(s)
       for +: 'NoneType' and 'int'` on every single sync attempt across all
       three plants, immediately, for 16+ hours straight until this fix.
    2. Even with a manually-computed row count standing in for max_row,
       per-cell RANDOM access on a read_only worksheet is NOT O(1) the way it
       is on a normally-loaded one - confirmed empirically against a real
       996-row file: 50 single-cell accesses (`ws[f'E{r}'].value`) near row 7
       took 0.28s; the same 50 accesses near row 900 took 4.8s. Cost scales
       with row depth, making a full per-row/per-column parse loop
       effectively O(n^2) - slow enough, on real file sizes here
       (~1,000-3,000 rows), to hang well past this app's own 900s sync-lock
       timeout (sync_trigger.py) rather than crash cleanly. This would have
       replaced problem 1's instant, obvious crash with a much worse silent
       hang - confirmed directly: a full parse of the smallest real MIR file
       timed out at 30s using the max_row-fallback-only fix, versus 0.13s
       using this function.

    Forward streaming via ws.iter_rows() reads each row's XML element exactly
    once - independent of the (possibly broken) dimension tag, and immune to
    problem 2 since nothing is ever re-accessed out of order. Confirmed no
    row-number gaps across a real file with sparse/blank rows (divider rows,
    542 rows checked) - plain sequential numbering from min_row is reliable,
    no need to trust an individual Cell's own `.row` (which read_only's
    EmptyCell doesn't even have).

    `max_col` (an int - pass `openpyxl.utils.column_index_from_string(...)`
    for a fixed lettered header) pins every yielded row to the same width, so
    a row whose own last real cell sits earlier than a field this parser
    reads still has every needed column present (as an EmptyCell, `.value`
    None) instead of raising KeyError.

    Yields (row_number, cells) where cells is a dict keyed by BOTH the
    1-based column index and its letter (e.g. cells[5] is cells['E'], the
    same Cell object) - every current caller has a fixed lettered header and
    indexes by letter, but the integer key is kept available for any future
    parser that needs to address a dynamically-discovered column. These are
    Cell objects, not raw values, so a caller needing more than `.value`
    (e.g. this app's Month/MIR-No. Excel-autoconvert workarounds, which also
    read `cell.number_format`) keeps working unchanged."""
    for row_num, row in enumerate(ws.iter_rows(min_row=min_row, max_col=max_col), start=min_row):
        cells = {}
        for i, cell in enumerate(row, start=1):
            cells[i] = cell
            cells[get_column_letter(i)] = cell
        yield row_num, cells


def to_str(value) -> str:
    """Coerces to a plain trimmed string; 'NULL' and None both become ''."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.upper() == "NULL" else s


_EXCEL_EPOCH = datetime.date(1899, 12, 30)
# Bare digits in this range parse as an Excel/Sheets date serial number
# (days since 1899-12-30, the classic Lotus-1-2-3-compatible epoch Excel
# still uses) - roughly 2000-01-01 (36526) to 2099-12-31 (73050). Narrow
# range deliberately: to_date() is only ever called on genuine date fields,
# but a tight bound still avoids ever misreading a stray small integer
# (e.g. a miskeyed quantity) as a date by accident.
_EXCEL_SERIAL_RANGE = range(36526, 73051)


def to_date(value) -> datetime.date | None:
    """Handles the several date shapes actually seen in these source files:
    a real datetime (openpyxl gives these for real Excel date cells), an
    ISO string, a few common slash/dot formats, or a bare Excel date serial
    number as a string (confirmed 2026-09-07: RTP-Vapi's live Imports PO CSV
    started exporting "PO Created Date" as a raw serial like "46064" instead
    of a formatted date string - a spreadsheet column-format regression, not
    a new date shape this parser was ever designed against). Never raises -
    an unparseable date becomes None, since MIR is known to have at least one
    date typo (delivery date printed earlier than the PO's own created
    date) that shouldn't crash a whole sync."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    if not s or s.upper() == "NULL":
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if s.isdigit() and int(s) in _EXCEL_SERIAL_RANGE:
        return _EXCEL_EPOCH + datetime.timedelta(days=int(s))
    return None


# ── Vendor/material name normalization (used by PO<->MIR<->Stock matching) ──

def normalize_vendor(name: str) -> str:
    """Strips common legal suffixes and punctuation so "Kedar Metals Pvt
    Ltd" and "KEDAR METALS PVT. LTD." compare equal. Used as the hard gate
    in PO<->MIR matching - never as a display value."""
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"\b(private limited|pvt\.?\s*ltd\.?|pvt\.?|ltd\.?|limited|llp|inc\.?|corp(oration)?\.?|co\.?|company)\b", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n.strip()


def normalize_material(name: str) -> str:
    """Loose normalization for material-name matching (MIR<->Stock): lowercase,
    strip punctuation/whitespace variance. Deliberately looser than vendor
    normalization since material descriptions vary more in free text."""
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return " ".join(n.split())


def tokenize(text: str) -> list[str]:
    """Splits a normalized material description into words, for the token-
    overlap component of the PO<->MIR weighted match score."""
    return [t for t in normalize_material(text).split(" ") if t]


# Match Accuracy Programme, fix 2.C: built from the actual distinct `uom`
# values found across every plant's PO/MIR/Stock tables
# (dev_smoke_test.sqlite3, 2026-09-05) - not guessed. Each entry maps a
# recognized unit to (family, factor_to_base) - qty/rate on both sides of a
# comparison get converted to the same family's base unit
# (mass->KG, volume->LTR, count->NOS, length->M) before scoring, so a PO in
# MT against a MIR in KG doesn't collapse a true match's qty/rate score to
# near-zero.
#
# Deliberately excludes a handful of real but ambiguous codes seen in that
# data: TO, BAG, BQ2, Bottle (too ambiguous to confidently classify from the
# code alone - e.g. "TO" plausibly means "Tonne" but could be something
# else entirely) and Sqm/SQMT/SQMTR/M2 (area - not one of the four families
# matching.py's scoring compares). These pass through unrecognized rather
# than risk a wrong guess - see normalize_uom()'s own docstring.
_UOM_FAMILIES: dict[str, tuple[str, Decimal]] = {
    # mass -> base KG
    "KG": ("mass", Decimal("1")),
    "KGS": ("mass", Decimal("1")),
    "GM": ("mass", Decimal("0.001")),
    "MT": ("mass", Decimal("1000")),
    "TON": ("mass", Decimal("1000")),
    "QTL": ("mass", Decimal("100")),
    # volume -> base LTR
    "L": ("volume", Decimal("1")),
    "LTR": ("volume", Decimal("1")),
    "LTRS": ("volume", Decimal("1")),
    "KL": ("volume", Decimal("1000")),
    "ML": ("volume", Decimal("0.001")),
    # count -> base NOS
    "NOS": ("count", Decimal("1")),
    "PCS": ("count", Decimal("1")),
    "PC": ("count", Decimal("1")),
    "EA": ("count", Decimal("1")),
    "UNIT": ("count", Decimal("1")),
    "SET": ("count", Decimal("1")),
    # length -> base M
    "M": ("length", Decimal("1")),
    "CM": ("length", Decimal("0.01")),
    "MM": ("length", Decimal("0.001")),
    "MTR": ("length", Decimal("1")),
    "MTRS": ("length", Decimal("1")),
    "MTS": ("length", Decimal("1")),
}


def normalize_uom(value: str) -> tuple[str | None, Decimal | None]:
    """Returns (family, factor_to_base) for a recognized unit of measure, or
    (None, None) for a blank or unrecognized one. A trailing '.' is stripped
    before lookup ('MT.' -> 'MT', a real variant seen in RTP-Achhad's MIR
    data) alongside the usual case-insensitivity. An unrecognized unit is a
    deliberate outcome, not a bug - see _UOM_FAMILIES' own comment for which
    real codes are excluded and why; the caller must leave qty/rate
    unconverted (not silently mis-scaled) whenever this returns (None, None)
    on either side."""
    if not value:
        return None, None
    key = value.strip().upper().rstrip(".")
    return _UOM_FAMILIES.get(key, (None, None))


def to_code_str(value) -> str:
    """Coerces a SAP code / PO number cell to a clean string, no trailing
    '.0' - openpyxl hands these back as float when the source column has no
    text formatting (confirmed on Achhad's Stock 'SAP Code' column and MIR's
    'Purchase Order. No.' column, e.g. 22001002.0 / 1100000768.0). A plain
    to_str() would keep the '.0'; this drops it for a value that is a whole
    number, and falls back to to_str() for anything else."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return to_str(value)
