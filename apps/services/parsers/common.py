"""Shared parsing helpers used by all three parsers (po_csv, mir, stock).
Kept dependency-free (no Django imports) so these can be unit-tested with
nothing but plain Python.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d.%m.%y"]


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
    return [t for t in normalize_material(text).split(" ") if t]


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
