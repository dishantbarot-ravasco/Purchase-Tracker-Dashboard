"""
apps/services/stock_identity.py — stable, sheet-row-independent identity for
a stock lot, replacing `source_row_ref` (the openpyxl row index) as the
upsert key for HRSRMLot/RTPAchhadRMLot/RTPVapiRMLot.

A row number is not an identity: inserting one row mid-sheet shifts every
row below it, so the next sync re-labels an existing lot as whatever
material now occupies its old row while that lot's *StockSnapshot history
stays attached by foreign key - silently splicing two materials' histories
together. See CLAUDE.md's "Snapshot pipeline rebuild" section for the full
incident this replaces.

Dependency-free (no Django imports), same convention as parsers/common.py
and validation.py, so this unit-tests as plain Python and is safe to import
from a migration's RunPython step.
"""

from apps.services.parsers.common import normalize_material, normalize_vendor


def lot_natural_key(*, code, description, vendor, location="", occurrence=0) -> str:
    """Composes `<code or normalize_material(description)>|<normalize_vendor(vendor)>`,
    with `#{occurrence+1}` appended when `occurrence > 0` to disambiguate a
    genuine duplicate (same material, same vendor, two separate lots).

    An explicit material code wins over the description when present - a
    SAP/HSN code survives a description being reworded in the sheet, which
    happens; the normalized description is the fallback, so casing/
    punctuation drift can't fork one lot into two.

    `location` is accepted but never folded into the key - it's warehouse
    state, not identity (see the per-plant segment mapping in CLAUDE.md/the
    Snapshot Pipeline Rebuild plan for why: a material relocating between
    zones must keep one continuous history, not fork it at the move date).

    Returns "" when neither `code` nor `description` identifies the row at
    all - callers must skip an empty key, never upsert every such row onto
    one shared blank.
    """
    ident = (code or "").strip() or normalize_material(description)
    if not ident:
        return ""
    key = f"{ident}|{normalize_vendor(vendor or '')}"
    if occurrence:
        key = f"{key}#{occurrence + 1}"
    return key


class OccurrenceCounter:
    """Tracks how many times each base natural key (before an occurrence
    suffix) has been seen in sheet order during one parse/sync run, so two
    genuine duplicate lots (same material, same vendor) get distinguishable
    keys instead of colliding on the same one. Stable as long as the
    relative order of those duplicate rows holds between syncs - a far
    weaker assumption than "no row is ever inserted anywhere in the sheet",
    which is what the old row-number key required.

    One instance per sync run - never share across plants or across
    separate command invocations.
    """

    def __init__(self):
        self._seen: dict[str, int] = {}

    def key_for(self, *, code, description, vendor, location="") -> str:
        base = lot_natural_key(code=code, description=description, vendor=vendor, location=location)
        if not base:
            return ""
        occurrence = self._seen.get(base, 0)
        self._seen[base] = occurrence + 1
        return lot_natural_key(code=code, description=description, vendor=vendor, location=location, occurrence=occurrence)
