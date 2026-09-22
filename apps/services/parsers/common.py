"""
apps/services/parsers/common.py - shared cell/CSV-value coercion and name-
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
            # noqa DTZ007 is deliberate: this parses a DATE STRING out of a
            # spreadsheet cell ("15/09/2026") and immediately takes .date().
            # There is no instant-in-time being represented, so attaching a
            # tzinfo would invent a timezone the source data never had - unlike
            # the `date.today()` sites fixed 2026-09-15, which genuinely meant
            # "now, in the plant's timezone" and had to become timezone.localdate().
            return datetime.datetime.strptime(s, fmt).date()  # noqa: DTZ007
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


# ── No-PO vendors (2026-09-17) ──────────────────────────────────────────────
# Vendors whose MIR rows will NEVER have a purchase order to match against.
# Until now the matcher had no concept of this: their rows sat in every
# plant's vendor-gated MIR pool forever, matched nothing, and nothing in the
# system said WHY - indistinguishable, on screen and in the accuracy numbers,
# from a real order the matcher had simply failed to find. The knowledge
# already existed in this file as prose (see _VENDOR_ALIAS_SOURCE's rule 2,
# which forbids aliasing onto a group company precisely because "their MIR
# rows are inter-plant jobwork/ex-work transfers with no PO raised at all") -
# this registry turns that comment into behaviour.
#
# ONE LIST, APPLIED TO ALL THREE PLANTS. The first version of this registry
# was scoped per plant, on the assumption that a vendor bought without a PO
# at one plant might be properly PO'd at another. The project owner's list
# says otherwise (2026-09-17): these vendors are no-PO everywhere, so scoping
# them per plant would only create a way for the three copies to drift apart.
# If a genuine per-plant exception ever appears, that is the point to
# reintroduce scoping - not before.
#
# Two genuinely different reasons a vendor lands here, kept as separate
# categories rather than one "ignore" flag, because they have opposite
# futures:
#
#   INTERNAL_TRANSFER - the company's own plants and sister units. An
#       inter-plant jobwork/ex-work movement is not a purchase and will never
#       generate a PO, by design. This is permanent.
#   NO_PO_SUPPLIER - a real third-party supplier that is genuinely bought
#       from, but without a PO being raised today. This is a PROCESS gap, not
#       a fact about the data model: any of these can start being PO'd, at
#       which point its entry here must be removed or its orders will be
#       silently excluded from reconciliation. Kept visible in the UI (see
#       _domestic_base.py's sync_status) for exactly that reason - this
#       registry suppresses MATCHING, it must never suppress the row itself.
#
# MATCHING IS EXACT NORMALIZED EQUALITY, NOT THE _vendor_matches() GATE.
# This is the important design decision here, and it is deliberately stricter
# than everywhere else vendor names are compared. A false positive in this
# registry silently removes a real supplier's receipts from reconciliation -
# strictly worse than a false negative, which merely leaves a row unmatched
# exactly as it already is today. _vendor_matches()'s containment arm would
# make that failure easy to hit with a short name ("mit" is a substring of
# "limited"; "import" of "xyzimport"), and its 0.90-similarity arm scores
# genuinely different companies as high as 0.857. So: add every real spelling
# variant you actually see, one line each. That is the cost of the
# strictness, and it is the right trade here.
#
# Normalization already collapses casing, punctuation, "&"/"and", legal
# suffixes and trailing plurals, so most real variants need no extra line -
# "STAR POLYMER"/"STAR POLYMERS INC." and "K-Flex"/"Kflex" each collapse to a
# single key on their own. Only a genuinely different WORD needs its own
# entry; the three that do are marked below.
#
# Source: project owner's list, 2026-09-17.

INTERNAL_TRANSFER = "internal_transfer"
NO_PO_SUPPLIER = "no_po_supplier"

# {readable vendor name: (category, reason)}.
_NO_PO_VENDOR_SOURCE: dict[str, tuple[str, str]] = {
    # ── The company's own plants and sister units ──────────────────────────
    "Ravasco Transmission & Packing Private Limited": (
        INTERNAL_TRANSFER, "Own plant (Achhad) - inter-plant transfer, no PO raised."),
    # "Packaging" is a different WORD from "Packing", not a spelling variant
    # normalization can fold, so the Vapi-facing name needs its own entry.
    "Ravasco Transmission and Packaging Pvt Ltd": (
        INTERNAL_TRANSFER, "Own plant (Vapi) - inter-plant transfer, no PO raised."),
    # Vapi's MIR appends the originating plant to the party name.
    "Ravasco Transmission And Packing Pvt Ltd ACHHAD": (
        INTERNAL_TRANSFER, "Own plant (Achhad), as written in Vapi's MIR."),
    "Hindustan Rubbers Industries Pvt Ltd": (
        INTERNAL_TRANSFER, "Group company (Achhad) - inter-plant transfer, no PO raised."),
    "Hindustan Rubbers (Silvassa)": (
        INTERNAL_TRANSFER, "Own plant (HRS) - inter-plant jobwork/ex-work transfer, no PO raised."),

    # ── Real third-party suppliers bought from without a PO ────────────────
    "Gangamani Enterprise Pvt Ltd": (NO_PO_SUPPLIER, "Bharuch - bought from without a PO being raised."),
    "Eternia Trading Private Limited": (NO_PO_SUPPLIER, "Mumbai - bought from without a PO being raised."),
    "K-Flex": (NO_PO_SUPPLIER, "Silli - bought from without a PO being raised."),
    "Harsha Impex": (NO_PO_SUPPLIER, "Mumbai - bought from without a PO being raised."),
    "2M Elastomers Private Limited": (NO_PO_SUPPLIER, "Sarigam - bought from without a PO being raised."),
    "Gurvinder Singh HUF": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "Forech Mining & Construction International LLP": (
        NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "Star Polymers Inc.": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "Sumitra Enterprise": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "DS Industries": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "Tinna Rubber and Infrastructure Ltd": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    "JMF Performance Materials Pvt. Ltd.": (NO_PO_SUPPLIER, "Bought from without a PO being raised."),
    # Achhad's MIR misspells it ("Perfomance", missing the r) on every one of
    # its real rows. Elsewhere that class of typo is absorbed generically by
    # _vendor_matches()'s similarity arm, which this registry deliberately
    # does not use - so the misspelling needs its own line or Achhad's rows
    # silently stay in the pool.
    "JMF Perfomance Materials Pvt Ltd": (
        NO_PO_SUPPLIER, "Spelling as written in Achhad's MIR - same supplier as the entry above."),
}

# {normalized vendor: (category, reason)}, built at import time so callers
# never pay the normalization cost per MIR row.
NO_PO_VENDORS: dict[str, tuple[str, str]] = {
    _normalize_vendor_for_matching_base(name): entry
    for name, entry in _NO_PO_VENDOR_SOURCE.items()
}


def no_po_vendor_entry(name: str) -> tuple[str, str] | None:
    """(category, reason) when no plant raises a PO against `name`, else None.

    Deliberately EXACT on the normalized name - see the section header above
    for why this one comparison does not use matching_core's _vendor_matches()
    gate.

    Normalizes through _normalize_vendor_for_matching_base(), NOT
    normalize_vendor_for_matching(): the VENDOR_ALIASES layer exists to fold
    spelling drift between two files naming the SAME supplier, and letting it
    also rewrite the key that decides "this vendor is excluded from matching"
    would mean adding an alias could quietly widen this registry's reach. The
    two lookups stay independent on purpose."""
    if not name:
        return None
    return NO_PO_VENDORS.get(_normalize_vendor_for_matching_base(name))


def is_no_po_vendor(name: str) -> bool:
    """True when no plant raises a PO against `name` - see
    no_po_vendor_entry()."""
    return no_po_vendor_entry(name) is not None


# ── Vendors whose goods never reach the RM Stock sheet ────────────────────
#
# A SECOND, SEPARATE REGISTRY FROM NO_PO_VENDORS ABOVE, answering a different
# question about a different pairing. NO_PO_VENDORS says "no purchase order
# exists for this vendor" and gates PO<->MIR. This one says "this vendor's
# goods are not tracked in the RM Stock sheet" and gates MIR<->Stock. A vendor
# can be in either, both, or neither: Madura is properly PO'd (it is the single
# biggest source of PO<->MIR matches at Vapi) and simply never appears in a
# stock file, while Tinna Rubber is the reverse - no PO, but its reclaim rubber
# really does land in stock.
#
# Measured 2026-09-21, at the project owner's prompt, across all three plants:
#
#     plant    MIR rows from Madura   share of that plant's MIR   RM stock lots
#     Vapi     703                    47.2%                       0
#     HRS      112                    22.3%                       0
#     Achhad    15                     2.3%                       0
#
# 830 rows and Rs 30.7 crore of receipts, against ZERO stock lots anywhere -
# not "a few missing", nothing at all. Deliveries run 2026-04-01 to 2026-09-18,
# so this is current, not a historical backlog. Madura supplies conveyor fabric
# (the EE/NN/EP series); the RM sheets hold chemicals and raw rubber, which is
# a different inventory class. Vapi's Madura rows alone are 703 of the 1,133
# MIR rows that plant has no stock counterpart for - 62% of its entire
# MIR<->Stock gap.
#
# SAME RULE AS NO_PO_VENDORS: suppress the match, never the row. The rows stay
# in the MIR table, still reconcile against their purchase orders, and are
# counted and labelled for the dashboard by services/no_rm_stock_vendors.py.
# Excluding them silently would recreate exactly the confusion that registry
# exists to end - an unmatched Madura row and a genuinely failed match are
# indistinguishable on screen otherwise.
#
# DELIBERATELY A VENDOR LIST, AND DELIBERATELY NARROW. The wider question -
# whether to exclude every material class the RM sheets do not track (conveyor
# belting, rubber compound, MS crates) - was measured and is NOT implemented
# here, because it cannot be keyed the same way: "rubber compound" is genuinely
# stocked at Achhad (27 real matches, most on exact names) and never at Vapi,
# so a shared material list would destroy real matches. That registry would
# have to be per-plant and material-keyed, which is a different design; it is
# on hold pending the project owner's own scope list. Do not widen this dict
# into that - add the new thing separately.
#
# Source: project owner, 2026-09-21.

_NO_RM_STOCK_VENDOR_SOURCE: dict[str, str] = {
    # Every spelling seen across the three MIR files. Lookup is exact on the
    # normalized name (same reasoning as no_po_vendor_entry() - a false
    # positive here removes a real supplier's receipts from stock
    # reconciliation), so a genuinely different WORD needs its own line.
    # "Textiles" vs "Textile" folds on its own via the trailing-plural rule;
    # "Technical Fabrics" does not.
    "Madura Industrial Textiles Ltd.": "Conveyor fabric - not tracked in any plant's RM Stock sheet.",
    "Madura Technical Fabrics Ltd.": "Conveyor fabric - Vapi's second spelling for the same supplier.",
    "Madura Technical Textiles Ltd": "Conveyor fabric - spelling as written in Vapi's PO master.",
    "Madura Indl Textiles Ltd": "Conveyor fabric - spelling as written in Vapi's MIR.",
}

# {normalized vendor: reason}, built at import time - same shape and same
# reasoning as NO_PO_VENDORS above.
NO_RM_STOCK_VENDORS: dict[str, str] = {
    _normalize_vendor_for_matching_base(name): reason
    for name, reason in _NO_RM_STOCK_VENDOR_SOURCE.items()
}


def no_rm_stock_vendor_reason(name: str) -> str | None:
    """Why `name`'s receipts are excluded from MIR<->Stock matching, or None.

    Exact on the normalized name, and normalized through
    _normalize_vendor_for_matching_base() rather than
    normalize_vendor_for_matching() - both for the same reasons
    no_po_vendor_entry() does it that way; see its docstring."""
    if not name:
        return None
    return NO_RM_STOCK_VENDORS.get(_normalize_vendor_for_matching_base(name))


def is_no_rm_stock_vendor(name: str) -> bool:
    """True when `name`'s goods never reach the RM Stock sheet - see
    no_rm_stock_vendor_reason()."""
    return no_rm_stock_vendor_reason(name) is not None


# ── Material classes the RM Stock sheet does not track ────────────────────
#
# The material-keyed half of the same idea as NO_RM_STOCK_VENDORS above, and
# the third registry in this file. MIR logs everything that comes through the
# gate; the RM Stock sheets hold chemicals and raw rubber. What is booked
# inward but never stocked is listed here so an excluded row reads as
# "out of scope" rather than as a matcher failure - the same rule, and the
# same reason, as the two registries above: SUPPRESS THE MATCH, NEVER THE ROW.
# Counted and labelled for the dashboard by services/rm_untracked.py.
#
# Source: project owner's scope list, 2026-09-21.
#
# EVERY ENTRY HERE PASSED TWO TESTS AGAINST LIVE DATA, and the second one is
# what the wording of each pattern is actually for:
#
#   A. No stock lot at ANY plant matches the pattern. If a lot exists, the
#      class is tracked and an unmatched row is a MATCHING gap, not a scope
#      one - excluding it would hide the very thing worth fixing.
#   B. No currently-matched MIR row matches the pattern, i.e. adding it
#      destroys no existing reconciliation.
#
# Two classes from the original six-class list were REJECTED by those tests
# and are deliberately absent. Do not add them back without re-running both:
#
#   - "Printing / labels / logo work" (9 Achhad rows) FAILS TEST A. Achhad's
#     Stock sheet carries 'Lamor Logo 160mm X 70Mic (12290)', and its MIR's
#     'Lamor Logo Print' rows are that same product. They are unmatched
#     because the scorer misses the pair, which is a bug to fix, not a class
#     to exclude.
#   - "Grease" inside the spares class FAILS TEST A twice over - both HRS's
#     and Vapi's Stock sheets hold 'GREASE EP 1'. The pattern below therefore
#     lists the spares words explicitly and omits it.
#
# The rubber-compound and packing classes survive only in NARROWED form, for
# the same reason:
#
#   - 'Rubber Compound' as a bare phrase is Vapi's un-stocked semi-finished
#     goods ('RUBBER COMPOUND (KGS)', 'COMPOUNDED RUBBER UNVULCANISED'), but
#     Achhad genuinely stocks named compounds ('Rubber Compound-EAR 11560',
#     'Rubber Compound-SHRC T23') and HRS stocks 'SILSHEET RUBBER'. A blanket
#     /rubber comp|silsheet/ destroyed 43 real matches. The anchored pattern
#     below matches ONLY the bare phrase with an optional unit suffix, so a
#     named grade can never be caught by it.
#   - Packing narrowed from bags/drums/wooden to CRATES AND PALLETS ONLY. All
#     three plants stock EVA/LD/BATA bags, and HRS stocks 'WOODEN STOPPER 12"'
#     and 'WOODEN CIRCLE 4"'. Only MS crates are genuinely untracked.
#
# Matched against the RAW description (case-insensitively), not
# normalize_material()'s output, so a pattern can use punctuation and anchors.

_NOT_STOCKED_MATERIAL_SOURCE: list[tuple[str, str, str]] = [
    # (class label, regex, reason shown on the dashboard)
    (
        "Conveyor fabric",
        r"\b(?:EE|NN|EP)\s?\d{2,3}\b|rubberi[sz]ed\s+textile|fabric.*(?:polyester|polyster)",
        "Conveyor fabric (EE/NN/EP series) - not held in any plant's RM Stock sheet.",
    ),
    (
        # NOT a bare /\bbelts?\b/, which is what this was first written as.
        # That caught Achhad's 'Rubber Compound Cushion Belts' - a COMPOUND
        # the plant genuinely stocks (lot: 'Rubber Compound-Cushion') that
        # merely names a belt as its application. One real match destroyed,
        # and the two safety tests above did not catch it because the row was
        # unmatched at the moment they ran; it turned up by re-running the
        # whole matcher with this registry disabled and diffing. Every
        # genuine belting row at both plants says "conveyor" or "belting"
        # outright, so requiring one of those costs nothing and closes it.
        "Conveyor belting",
        r"conveyor|belting|transmission\s+belt",
        "Conveyor and transmission belting - a finished good, not a raw material.",
    ),
    (
        "Rubber compound",
        r"^\s*(?:RM\d+\s+)?rubber\s+compound\s*(?:\(?\s*(?:kgs?|mtrs?)\s*\)?)?\s*$"
        r"|compounded\s+rubber",
        "Un-named semi-finished rubber compound - named compounds ARE stocked and are not excluded.",
    ),
    (
        # CRATES ONLY, not pallets. "Pallet" appears in a real Achhad stock
        # lot - 'Carbon Black Pallets (Majestique)', where it describes the
        # FORM the carbon black arrives in, not the pallet as the goods. No
        # plant has a single MIR row naming a pallet as the thing bought, so
        # the word earns nothing and only creates the risk of excluding that
        # carbon black if a receipt is ever worded to match. Found by running
        # the shipped patterns back over each plant's stock sheet separately
        # (2026-09-21) - the per-plant sweep is worth repeating when this
        # list changes.
        "Crates",
        r"\bcrates?\b",
        "MS crates - returnable handling equipment, not stock.",
    ),
]

# ── The two classes that genuinely differ per plant ───────────────────────
#
# SCOPED PER PLANT, unlike NO_PO_VENDORS and NO_RM_STOCK_VENDORS, which are
# deliberately one shared list each. The difference is real and measured
# (2026-09-21, per-plant sweep of every class against every plant's own Stock
# sheet), not defensive: what a plant stocks is a fact about THAT plant's
# warehouse, whereas whether a vendor is PO'd is a fact about the company.
#
# The four classes above are shared because all three plants agree on them -
# zero stock lots anywhere. These two do not agree:
#
#   class     HRS            Achhad                      Vapi
#   grease    'GREASE EP 1'  -                           'GREASE EP 1'
#   logo      -              'Lamor Logo 160mm X 70Mic'  -
#
# So Achhad may exclude grease and must NOT exclude logo work; HRS and Vapi
# are the exact opposite on both counts.

_SPARES_WITHOUT_GREASE = (
    "Spares and services",
    r"\bspares?\b|\bbearings?\b|\bbolts?\b|\bnuts?\b|\bservice\b|\brepair\b|freight|labour",
    "Spares, consumables and services - not a material held in stock.",
)
_SPARES_WITH_GREASE = (
    "Spares and services",
    r"\bspares?\b|\bbearings?\b|\bbolts?\b|\bnuts?\b|grease|\bservice\b|\brepair\b|freight|labour",
    "Spares, consumables and services - not a material held in stock at this plant.",
)
_PRINTING = (
    "Printing and labels",
    r"\blogos?\b|\blabels?\b|\bprints?\b|\bprinting\b|\bstickers?\b",
    "Printing, labels and logo film - not held in this plant's RM Stock sheet.",
)

# {plant key (matching _PlantConfig.key): [(label, pattern, reason), ...]}.
_NOT_STOCKED_BY_PLANT: dict[str, list[tuple[str, str, str]]] = {
    # HRS stocks 'GREASE EP 1', so grease stays matchable here; it has no
    # logo/label stock and no such MIR rows either, so the printing class is
    # correct-but-currently-vacuous - carried for when a row appears.
    "hrs": [*_NOT_STOCKED_MATERIAL_SOURCE, _SPARES_WITHOUT_GREASE, _PRINTING],
    # Achhad is the mirror image: no grease anywhere in its Stock sheet, but
    # it DOES stock 'Lamor Logo 160mm X 70Mic (12290)'. Its nine
    # 'Lamor Logo Print' MIR rows are that same product and are a SCORER miss,
    # not an out-of-scope receipt - excluding them here would bury the one
    # genuinely fixable thing in this whole registry. See CLAUDE.md.
    "achhad": [*_NOT_STOCKED_MATERIAL_SOURCE, _SPARES_WITH_GREASE],
    # Same as HRS on both counts.
    "vapi": [*_NOT_STOCKED_MATERIAL_SOURCE, _SPARES_WITHOUT_GREASE, _PRINTING],
}

NOT_STOCKED_MATERIALS: dict[str, list[tuple[str, "re.Pattern", str]]] = {
    plant: [(label, re.compile(pattern, re.IGNORECASE), reason)
            for label, pattern, reason in entries]
    for plant, entries in _NOT_STOCKED_BY_PLANT.items()
}


def not_stocked_material_entry(description: str, plant_key: str) -> tuple[str, str] | None:
    """(class label, reason) when `plant_key`'s RM Stock sheet does not track
    this kind of material, else None.

    `plant_key` is required, not optional with a shared default: an omitted
    plant would silently fall back to some other plant's scope, which is the
    one failure mode this split exists to prevent. An UNKNOWN key excludes
    nothing rather than guessing - a new plant must state its own scope, and
    until it does, its rows all stay in the pool exactly as they would have
    before this registry existed.

    First match wins, so order matters: the narrowly-anchored rubber-compound
    pattern sits after the two broad conveyor ones, so a description naming
    both ('conveyor belt rubber compound') is reported under the class a
    reader would expect."""
    if not description:
        return None
    for label, pattern, reason in NOT_STOCKED_MATERIALS.get(plant_key, ()):
        if pattern.search(description):
            return label, reason
    return None


def is_not_stocked_material(description: str, plant_key: str) -> bool:
    """True when `plant_key`'s RM Stock sheet does not track this kind of
    material - see not_stocked_material_entry()."""
    return not_stocked_material_entry(description, plant_key) is not None


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


# ── MIR<->Stock description cleaning (2026-09-21) ─────────────────────────
#
# Both files write the SAME material with a fixed piece of bookkeeping noise
# bolted on, and that noise dominates the token comparison because it is rare
# vocabulary - exactly what IDF weights most heavily. Stripping it is worth
# more at Vapi than any scoring change: +27 matched MIR rows on its own, and it
# is what lifts Vapi's exact-name matches from 24 to 71 (many names simply ARE
# identical once the noise is gone).
#
# Applied ONLY on the MIR<->Stock comparison path, never inside
# normalize_material() itself - that function's output is a persisted join key
# (MaterialCategoryReference.normalized_description, stock_identity.py's
# lot_natural_key()), and changing it would silently re-key existing rows. Same
# confinement, and the same reason, as tokenize()'s own letter/digit split.

# Vapi's MIR prefixes the SAP material code to the description:
# "RM00011014 ZINC OXIDE", "RMP001105002 EVA BAG". 173 of its 1,489 rows do
# this; no Stock sheet carries the code, and HRS/Achhad never write it.
_SAP_MATERIAL_CODE_RE = re.compile(r"\bRMP?0\d{5,}\b", re.IGNORECASE)

# Vapi's and HRS's Stock sheets append the holding plant/warehouse to the
# description: "RECLAIM RUBBER 6MPA HRS", "BAYPRENE RUBBER RTP2". It is a
# location tag, not part of the material name - both files also carry it as its
# own column (RTPVapiRMLot.plant_tag, HRSRMLot.location_tag). Stripped only
# from the END, and only as a whole trailing token, so it can never eat into a
# real name.
_STOCK_LOCATION_TAGS = frozenset({"hrs", "rtp", "rtp1", "rtp2", "grp"})


def clean_mir_material_for_stock(description: str) -> str:
    """A MIR material description with its SAP code prefix removed.

    Returns the original string when stripping would leave nothing - a row
    whose description is ONLY a code still has to compare as something."""
    if not description:
        return ""
    stripped = _SAP_MATERIAL_CODE_RE.sub(" ", description).strip()
    return stripped or description


def clean_stock_material(description: str) -> str:
    """A Stock lot description with any trailing plant/location tag removed.

    Loops because a handful of real rows carry two ("... SILDEC HRS"). Same
    empty-result guard as above."""
    if not description:
        return ""
    tokens = description.split()
    while tokens and tokens[-1].lower().replace("-", "") in _STOCK_LOCATION_TAGS:
        tokens.pop()
    return " ".join(tokens) or description


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
# data: BAG, BQ2, Bottle (too ambiguous to confidently classify from the code
# alone) and Sqm/SQMT/SQMTR/M2 (area - not one of the four families
# matching.py's scoring compares). These pass through unrecognized rather
# than risk a wrong guess - see normalize_uom()'s own docstring.
#
# TO and MTS were resolved by evidence on 2026-09-18, having previously been
# an exclusion and a wrong guess respectively. Both were settled by looking at
# what MATERIAL each one actually sits on, across every plant's MIR and PO
# data at once - the code alone genuinely is ambiguous, which is why the
# original caution was right and why only a census could lift it:
#
#   TO   16 rows, all RTP-Achhad PO CSV, every one of them "Steam Coal
#        Imported (Non Cooking)". Tonnes. It was excluded on the reasoning
#        that it "plausibly means Tonne but could be something else
#        entirely"; nothing else is bought by the TO, so it isn't.
#   MTS  2 rows, both RTP-Achhad MIR, both "Imported Coal". Tonnes - and it
#        was previously mapped to LENGTH, which is a genuine mis-parse, not
#        merely a missing entry: 28.14 tonnes of coal read as 28.14 metres.
#        Nobody writes metres as MTS: that is MTR (Vapi, 127 rows) or MTRS
#        (Achhad, 163 rows), and both stay length below.
#
# The lesson for the next one of these: settle a unit by the materials it
# appears against, not by what the abbreviation looks like.
_UOM_FAMILIES: dict[str, tuple[str, Decimal]] = {
    # mass -> base KG
    "KG": ("mass", Decimal("1")),
    "KGS": ("mass", Decimal("1")),
    "GM": ("mass", Decimal("0.001")),
    "MT": ("mass", Decimal("1000")),
    "MTS": ("mass", Decimal("1000")),   # tonnes, not metres - see above
    "TO": ("mass", Decimal("1000")),
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


# ── Legacy slashed PO numbers: cross-file format drift (2026-09-18) ─────────
# The 10-digit SAP PO numbers reconcile between the master CSV and the MIR
# sheets on their own - they are one atomic token and both files copy it
# verbatim. The LEGACY slashed form does not: the two files write the same
# order differently, confirmed against Achhad's live pair this session.
#
#   MIR sheet writes          master CSV writes                same order?
#   Eng/0007/2026-27          RTP2/HO/26-27/ENGG-0007          yes
#   RTP-ACHHAD/26-27/001      RTP2/HO/26-27/001                yes
#   RTP/HO/25-26/003          RTP2/HO/25-26/0003               yes
#
# _po_number_matches()'s whole-token comparison cannot see through any of
# these - the token sets genuinely differ - so five real Achhad orders carry
# a PO number in both files that the matcher reads as "no PO evidence".
#
# The key below reduces a legacy reference to the two parts both files do
# agree on: the FISCAL YEAR ('2026-27' and '26-27' fold together) and the
# SERIAL number with leading zeros stripped ('0007'/'007'/'7' fold together).
#
# THE SERIES TOKEN IS REQUIRED, and that requirement is the whole safety
# argument. (fiscal year, serial) alone is NOT unique in this data: Achhad's
# MIR carries both '0014/2026-27' (Triambakam Impex) and '14/26-27' (Polyols
# & Polymers Pvt Ltd), which reduce to the identical ('26-27', 14) and would
# then both claim RTP2/HO/26-27/ENGG-0014. Demanding that the two sides also
# share an alphabetic series token - by prefix, so 'Eng' reaches 'ENGG' and
# 'RTP' reaches 'RTP2' - rejects both of those (neither carries one at all)
# while still folding the three real pairs above.
#
# Refusing the digits-only shapes costs nothing: a PO number is only ever ONE
# of the identification factors, so those rows still identify on vendor plus
# material exactly as they do today. This helper only ever ADDS positive
# evidence - `_po_number_contradicts()` deliberately does not consult it, so a
# loose fold here can never become evidence AGAINST a match.
_FY_RE = re.compile(r"^(?:20)?(\d{2})\s*-\s*(?:20)?(\d{2})$")
# Splits on the separators BETWEEN parts of a reference, deliberately NOT on
# '-': a hyphen binds a part together here ('26-27' is one fiscal year,
# 'ENGG-0007' is one series+serial) and splitting on it would destroy both.
_LEGACY_SPLIT_RE = re.compile(r"[/\s,;&]+")
# The serial is read only from a token that is all digits ('001', '0014') or
# from the digit run after a hyphen ('ENGG-0007'). Digits fused directly onto
# letters are deliberately NOT a serial: 'RTP2' is a series name whose '2'
# would otherwise be read as order number 2.
_HYPHEN_SERIAL_RE = re.compile(r"-(\d+)$")


def legacy_po_key(value: str) -> tuple[str, int] | None:
    """(fiscal year, serial) for a legacy slashed PO reference, else None.

    Returns None for anything this fold must not touch - a bare SAP numeral,
    a reference with no fiscal year, or one with no serial - so a caller can
    use `legacy_po_key(a) is not None and legacy_po_key(a) == legacy_po_key(b)`
    as a complete test. Pair it with `legacy_po_series()` (see that function
    and the section header for why the series check is mandatory).

    When several tokens could be the serial, the LAST one wins - both files
    write the order number at the end of the reference, and the fiscal year
    (checked first, and skipped) is the only part that ever follows it."""
    s = (value or "").strip()
    if not s or "/" not in s:
        return None
    fy = None
    serial = None
    for token in (t for t in _LEGACY_SPLIT_RE.split(s.upper()) if t):
        m = _FY_RE.match(token)
        if m:
            # Only a genuine consecutive-year pair is a fiscal year, so a
            # hyphenated serial can never be mistaken for one.
            start, end = int(m.group(1)), int(m.group(2))
            if fy is None and (end - start) % 100 == 1:
                fy = f"{start:02d}-{end:02d}"
                continue
        if token.isdigit():
            serial = int(token)
            continue
        hyphenated = _HYPHEN_SERIAL_RE.search(token)
        if hyphenated:
            serial = int(hyphenated.group(1))
    if fy is None or serial is None:
        return None
    return fy, serial


def legacy_po_series(value: str) -> set[str]:
    """The alphabetic series names of a legacy PO reference, upper-cased
    ('RTP2/HO/26-27/ENGG-0007' -> {'RTP', 'HO', 'ENGG'},
    'RTP-ACHHAD/26-27/001' -> {'RTP', 'ACHHAD'}).

    Each token is split further on '-' and has any trailing digits stripped,
    so 'RTP2' contributes 'RTP' and 'ENGG-0007' contributes 'ENGG'. Compared
    by prefix in `legacy_po_matches()` below."""
    out = set()
    for token in (t for t in _LEGACY_SPLIT_RE.split((value or "").upper()) if t):
        for part in token.split("-"):
            word = re.sub(r"\d+$", "", part)
            if word.isalpha() and len(word) >= 2:
                out.add(word)
    return out


def legacy_po_matches(a: str, b: str) -> bool:
    """True when two legacy slashed PO references name the same order
    despite being written in the two files' different house formats - equal
    (fiscal year, serial), plus a shared series name by prefix. See the
    section header above for the collision that second condition prevents.

    A multi-PO cell ('HRS/HO/26-27/003 & 004') reduces to only its LAST
    serial here and so will not fold onto '...003'. That is not a gap: this
    function is a FALLBACK behind matching_core._po_number_matches()'s own
    contiguous-token-run test, which already handles that shape, and which
    runs first."""
    key = legacy_po_key(a)
    if key is None or key != legacy_po_key(b):
        return False
    sa, sb = legacy_po_series(a), legacy_po_series(b)
    if not sa or not sb:
        return False
    return any(x.startswith(y) or y.startswith(x) for x in sa for y in sb)


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
