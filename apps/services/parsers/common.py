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


def to_str(value) -> str:
    """Coerces to a plain trimmed string; 'NULL' and None both become ''."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.upper() == "NULL" else s


def to_date(value) -> datetime.date | None:
    """Handles the several date shapes actually seen in these source files:
    a real datetime (openpyxl gives these for real Excel date cells), an
    ISO string, or a few common slash/dot formats. Never raises - an
    unparseable date becomes None, since MIR is known to have at least one
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
