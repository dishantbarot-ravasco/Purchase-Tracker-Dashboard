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

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(private limited|pvt\.?\s*ltd\.?|pvt\.?|ltd\.?|limited|llp|inc\.?|corp(oration)?\.?|co\.?|company)\b"
)


def normalize_vendor(name: str) -> str:
    """Strips common legal suffixes and punctuation so "Kedar Metals Pvt
    Ltd" and "KEDAR METALS PVT. LTD." compare equal.

    This is a STABLE, persisted-identity function - stock_identity.py's
    lot_natural_key() uses its output as (part of) the natural key it
    upserts HRSRMLot/RTPAchhadRMLot/RTPVapiRMLot rows on across every sync
    run (see that module's own docstring on the row-number-key incident
    this replaced). Changing this function's output for any real vendor
    name would silently make every existing lot with that vendor look like
    a brand-new lot on the next sync, forking its snapshot history exactly
    the way that incident did - so this function's algorithm must never
    change for the sake of a looser fuzzy match. For PO<->MIR/MIR<->Stock
    vendor-gate MATCHING (never for a persisted key), use
    normalize_vendor_for_matching() instead - see its own docstring for
    why matching needs a looser fold than identity can safely tolerate."""
    if not name:
        return ""
    n = _LEGAL_SUFFIX_RE.sub("", name.lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n.strip()


def normalize_vendor_for_matching(name: str) -> str:
    """Loosened vendor normalization for the PO<->MIR/MIR<->Stock vendor
    hard gate ONLY (_vendor_matches() in matching_core.py) - never use this
    for a persisted identity key (see normalize_vendor()'s own docstring on
    why: its output must stay stable across syncs, which this function's
    extra folding does not guarantee, deliberately, in exchange for
    catching more real spelling variants at comparison time).

    Same legal-suffix stripping as normalize_vendor(), plus two further
    real-world spelling-variant classes, confirmed against every distinct
    real vendor name across all three plants (2026-09-10) to introduce zero
    new collisions between genuinely different vendors before being applied
    here - vendor is the one hard gate the whole matcher depends on, so
    this was checked against real data, not guessed:
      - The connector word "and" folds the same way "&" already silently
        does via the alphanumeric-only strip below - e.g. "Yogleela
        Sulphur and Agchem Industries" vs "...Sulphur & Agchem Ind." used
        to fail containment purely because "and" sat as literal letters in
        the middle of one side and nothing in the other.
      - A trailing plural "s" on any individual word longer than 3
        characters - e.g. "Shreeji Minerals & Chemical Co" vs "Shreeji
        Mineral & Chemical Co". Deliberately applied per-WORD (split on
        non-alphanumeric runs first, before the final alphanumeric-only
        join), not to the whole concatenated string, so it can only ever
        drop one real trailing "s" per word, never eat into an unrelated
        part of a longer joined name.
      - A standalone "private" left behind when the name reads "Private
        Ltd" rather than "Private Limited" (added 2026-09-11). The shared
        _LEGAL_SUFFIX_RE matches the PHRASE "private limited" and the word
        "ltd" separately, so "Shreeji Rubtech Private Ltd" normalized to
        "shreejirubtechprivate" and stopped containment-matching MIR's
        "SHREEJI RUBTECH PVT. LTD. (GUJARAT)" -> "shreejirubtechgujarat".
        This strip is applied HERE and not by adding "private" to
        _LEGAL_SUFFIX_RE itself, because normalize_vendor() shares that
        pattern and its output is a persisted natural key - see
        normalize_vendor()'s own docstring for the snapshot-forking
        incident that would cause.

    Finally, VENDOR_ALIASES (below) is applied as a last-resort lookup for
    the handful of real variants none of the generic rules above can reach.
    """
    n = _normalize_vendor_for_matching_base(name)
    return VENDOR_ALIASES.get(n, n)


def _normalize_vendor_for_matching_base(name: str) -> str:
    """normalize_vendor_for_matching() minus the VENDOR_ALIASES lookup.
    Split out only so VENDOR_ALIASES can be built from readable vendor
    names at import time without recursing into its own lookup."""
    if not name:
        return ""
    n = _LEGAL_SUFFIX_RE.sub("", name.lower())
    n = re.sub(r"\band\b", " ", n)
    n = re.sub(r"\bprivate\b", " ", n)
    words = [w for w in re.split(r"[^a-z0-9]+", n) if w]
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
    return "".join(words)


# Explicit vendor-name equivalences for the PO<->MIR/MIR<->Stock vendor gate
# ONLY - never for a persisted identity key (same rule as
# normalize_vendor_for_matching() itself, which is the only caller).
#
# **Keep this map small, and add to it only as a last resort.** The generic
# rules above, plus matching_core.py's own exact-equality and 0.90-similarity
# fallback in _vendor_matches(), already absorb the large majority of real
# spelling drift between the PO CSV and the MIR sheets - including typos on
# vendors nobody has seen yet, which is the whole point of preferring a
# generic rule to a lookup table. An entry here only earns its place when the
# two spellings are NOT reachable generically: an abbreviation or a genuinely
# different word, not a mistyped character.
#
# Bar for adding an entry:
#   1. Confirm the two names are the same legal supplier, not two suppliers
#      with similar names (e.g. "Bp Chemicals" and "LBG Chemicals" score 0.857
#      similar and are different companies - that near-miss is exactly why
#      _VENDOR_SIMILARITY_THRESHOLD sits at 0.90 and not lower).
#   2. Neither side may be a group company. Ravasco Transmission & Packing
#      (Vapi/Achhad) and Hindustan Rubbers (Silvassa) are the company's own
#      plants; their MIR rows are inter-plant jobwork/ex-work transfers with
#      no PO raised at all, so folding one onto a real supplier would
#      attribute an internal transfer to a third-party purchase order.
#      Enforced by test_alias_map_never_targets_a_group_company.
#   3. Prefer fixing the spelling at source. Every entry here is a standing
#      workaround for a data-entry inconsistency that will keep producing new
#      variants until the two files draw vendor names from the same master.
#
# Written as readable names and normalized at import time, so a reader can
# see what each entry actually means.
_VENDOR_ALIAS_SOURCE = {
    # "INDL" is an abbreviation of "Industrial", not a typo - it scores only
    # 0.850 similarity (below the 0.90 threshold) and no generic folding rule
    # can expand an abbreviation safely. Confirmed same supplier: the PO CSV
    # writes "Madura Industrial Textiles Ltd" on HRS/Achhad/Vapi POs, Vapi's
    # MIR writes "MADURA INDL TEXTILES LTD".
    "MADURA INDL TEXTILES LTD": "Madura Industrial Textiles Ltd",
}

VENDOR_ALIASES: dict[str, str] = {
    _normalize_vendor_for_matching_base(variant): _normalize_vendor_for_matching_base(canonical)
    for variant, canonical in _VENDOR_ALIAS_SOURCE.items()
}


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
    overlap component of the PO<->MIR weighted match score.

    Also splits at letter<->digit boundaries within a single alnum run (e.g.
    "180p" and "g260a"), so a code written with vs. without an internal space
    ("180 P" on one side, "180P" on the other) still tokenizes to a shared
    token. Confirmed real gap in Achhad PO-vs-MIR material descriptions
    (Match Accuracy Programme): e.g. PO "AKSIL 180P" vs MIR "Aksil 180 P"
    scored 0.167 Jaccard overlap purely from this, not genuine vocabulary
    mismatch. Deliberately NOT applied inside normalize_material() itself -
    that function's output is also used as an exact-match join key elsewhere
    (MaterialCategoryReference.normalized_description, MIR<->Stock identity
    matching in matching_core.py/stock_identity.py) where changing the
    normalization would silently break an existing exact-match comparison
    against already-stored values. This split only ever feeds token-overlap
    scoring, never an equality check, so it's confined here."""
    normalized = normalize_material(text)
    split_at_alnum_boundaries = re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", normalized)
    return [t for t in split_at_alnum_boundaries.split(" ") if t]


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


# ── PO-number hygiene (PO<->MIR matching, 2026-09-12) ───────────────────────
# MIR's PO-number column is hand-typed free text. Before it can be used as a
# decisive join key (see matching_core.py's PO-number contradiction gate), the
# values that are NOT a PO number at all have to be separated from the ones
# that are - a sentinel like "VERBAL" or a stray "36" must never be allowed to
# either match a PO or, worse, contradict one.

# Literal non-PO markers seen in the real MIR PO columns (RTP-Vapi's
# 'PURCHASE ORDER' column, 2026-09-12: "VERBAL" appears on real rows meaning
# "ordered by phone, no PO raised"). Compared case-insensitively after strip.
_NON_PO_SENTINELS = {
    "VERBAL", "VERBAL PO", "VERBAL ORDER", "NA", "N.A.", "N/A", "NIL", "NONE",
    "-", "--", "TBD", "OPEN", "DIRECT", "CASH", "URGENT",
}

# A PO reference has to look like one. Real PO numbers across all three plants
# take exactly two shapes: a 10-digit SAP numeral (3000001155, 1100000913,
# 1000001703) or the legacy slashed form (HRS/HO/26-27/003). Anything shorter
# is data-entry noise - confirmed against the live Vapi MIR PO column, which
# carries bare values like "36" and "100000" alongside real PO numbers.
# All-digit values are held to a stricter 8-digit floor than mixed ones,
# precisely because a short bare number is the shape junk takes here.
_PO_MIN_DIGITS = 8
_PO_MIN_MIXED_LEN = 7
_PO_SHAPED_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9/\-& .,;]+$")


def is_usable_po_reference(value: str) -> bool:
    """True when `value` can be trusted as naming a real purchase order.

    Deliberately conservative: this function's False answer is what keeps a
    junk cell from being treated as evidence *against* a match (the
    contradiction gate in matching_core.py), so the cost of wrongly
    returning True is much higher than the cost of wrongly returning False.
    An unusable value simply falls back to material-based identification,
    exactly as if the cell were blank."""
    s = (value or "").strip()
    if not s or s.upper() in _NON_PO_SENTINELS:
        return False
    if not _PO_SHAPED_RE.match(s):
        return False
    if s.isdigit():
        return len(s) >= _PO_MIN_DIGITS
    return len(s) >= _PO_MIN_MIXED_LEN


# Trailing parenthetical annotations a human added to a PO number in the
# master CSV - e.g. "3000001104 (Changed Purchase Order)", "1000001445
# (Rev 01)", "1000001488 (Changed Purchase Order, supersedes original)".
# All 22 real cases across the three plants (2026-09-12) are a single
# trailing "( ... )" group, so the pattern is anchored to the end rather
# than stripping parentheses anywhere in the string.
_PO_ANNOTATION_RE = re.compile(r"\s*\([^()]*\)\s*$")


def clean_po_number(value: str) -> str:
    """Strips a trailing human annotation from a PO number, leaving the bare
    order number ("3000001104 (Changed Purchase Order)" -> "3000001104").

    Used for MATCHING only, never as a persisted natural key - the stored
    po_number must keep whatever the master CSV says, or a sync would fork
    every annotated order into a second row (same rule, same reason, as
    normalize_vendor() vs normalize_vendor_for_matching())."""
    s = (value or "").strip()
    if not s:
        return ""
    cleaned = _PO_ANNOTATION_RE.sub("", s).strip()
    return cleaned or s


def repair_month_swapped_date(value: datetime.date | None, expected_month: int | None) -> datetime.date | None:
    """Repairs a date whose day and month were transposed, using a month the
    caller knows independently.

    Real defect this exists for (confirmed 2026-09-12 against the live RTP
    VAPI MIR file): 267 of its 782 real date cells are stored by Excel
    itself with day and month swapped - a row whose MIR number is
    'MIR01/04' (April) carries the datetime 2026-01-04. The sheet was typed
    as "01-04-2026" into cells formatted US month-first, so Excel committed
    the wrong date to the file; openpyxl hands back a genuine datetime and
    there is nothing a date *parser* can do about it. The MIR number's own
    '/MM' suffix is an independent record of the real month, which makes the
    repair deterministic rather than a guess.

    Only rewrites when all three conditions hold, so a legitimately
    cross-month row is never touched:
      - the stored month disagrees with `expected_month`, AND
      - the stored DAY equals `expected_month` (i.e. the two are exactly
        transposed, not merely different), AND
      - the resulting date is real (guards e.g. day 31 of a 30-day month).
    Returns `value` unchanged in every other case, including when either
    input is missing."""
    if value is None or not expected_month or not 1 <= expected_month <= 12:
        return value
    if value.month == expected_month or value.day != expected_month:
        return value
    if value.month > 12:
        return value
    try:
        return datetime.date(value.year, value.day, value.month)
    except ValueError:
        return value
