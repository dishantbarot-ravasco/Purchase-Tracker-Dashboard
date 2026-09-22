"""
apps/services/license_links.py - the ONE place that decides how an import
PO line item's own "Export Incentive / License Scheme" columns
(`license_type` / `license_number`, straight from each plant's Imports
Purchase Data master CSV) map onto the two company-wide licence ledgers:
RodtepScrollEntry and AdvanceLicense.

WHY THIS EXISTS
---------------
Both ledger panels were built (2026-09-09) as if the import side of a
licence were unknowable from any synced source:

  - RodtepScrollEntry's own docstring says "Drive has no structured link
    between a script and which import it was later used against", and
    RodtepUsage was added as a HAND-ENTERED table to carry that link.
  - AdvanceLicense got its import side only because the project owner's own
    hand-maintained workbook happens to carry BOE/PO/qty/value usage
    columns per material.

That premise was wrong for BOTH, and measurably so. The imports master CSV
has carried `License Type` + `License Number` per line item since the very
first import sync (parsers/import_po_csv.py, models' `license_type` /
`license_number`) - the app simply never joined on them. Measured
2026-09-22 against real synced data:

    license_type   ''  43 rows | 'ADVANCE'  6 rows | 'RODTEP'  3 rows
    every RODTEP number cited by an import ('2603043916', '2603044111')
    is a Script No that RodtepScrollEntry already holds - a 100% join,
    not a fuzzy one.

So the link is already in the data, on both schemes, keyed on an exact
identifier rather than on description text. That is a far stronger join
than anything else in this app (see CLAUDE.md's MIR<->PO / MIR<->Stock
sections for what identification normally costs here), and it is what makes
the hand-entered RodtepUsage table redundant as the *linkage* record.

WHAT THE CSV DOES AND DOES NOT GIVE US
--------------------------------------
It gives WHICH licence/scrip was applied to WHICH import line (plant, PO,
BOE, material, landed value). It does NOT give the AMOUNT of credit
debited - no column carries it, on either scheme. So:

  - RoDTEP "how much of this scrip is left" remains unanswerable from any
    synced source. It is reported as unanswerable rather than implied by a
    Balance column that silently equals Sanctioned (which is exactly what
    that column did while RodtepUsage sat empty - 0 rows ever entered).
  - Advance Licence utilisation IS answerable, because the owner's own
    workbook carries `Value Imported / Duty Saved` per usage row. That
    number comes from the workbook, never from this module.

TWO NORMALISATION RULES, BOTH FORCED BY REAL DATA
-------------------------------------------------
1. Multi-licence cells. One line item can name several licences in one
   cell, slash-joined:
       '0311051817/0311055303'
       '0311047672/0311047922/0311048316/0311048705/0311049174/0311049210'
   A line drawn against six authorisations is a real thing, so the cell is
   split - but only behind a GUARD: every resulting token must itself look
   like a licence number, otherwise the cell is left whole. Splitting
   unconditionally is how the multi-PO hyphen work went wrong before it was
   guarded (see CLAUDE.md's Vapi multi-PO cells): a cell of an unexpected
   shape gets shredded into tokens that match nothing, and the damage is
   invisible because the result is simply "no match" either way.

2. Leading zeros. Advance authorisation numbers are 10 digits, and the CSV
   writes the same licence both ways - '311051817' on one line and
   '0311051817' inside a slash-joined cell on another. Left alone they read
   as two different authorisations (10 distinct numbers where there are
   really 9). Zero-padding to LICENSE_NUMBER_WIDTH collapses them. Harmless
   for RoDTEP scrips, which are already 10 digits.

Neither rule rewrites what was synced: `license_number_raw` on every
citation is the verbatim cell, so a reviewer can always see what the CSV
actually said.

SCOPE
-----
Read-only and derived - nothing here writes, and every value is recomputed
per request from the line items and the ledgers. Active POs only, the same
`is_active` filter everything downstream of the PO sync uses (see
HRSImportPurchaseOrder.is_active's own comment on renamed-PO ghosts).
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from apps.core.models import (
    HRSImportPOLineItem,
    RTPAchhadImportPOLineItem,
    RTPVapiImportPOLineItem,
)

# ── Schemes ──────────────────────────────────────────────────────────────────
# The two values the CSV's own `License Type` column actually carries today,
# confirmed against real synced data (see this module's header). Matched by
# substring rather than equality so the near-certain future spellings
# ('RODTEP SCRIP', 'Advance Licence', 'ADVANCE AUTHORISATION') classify
# correctly instead of silently falling into UNKNOWN.
SCHEME_RODTEP = "RODTEP"
SCHEME_ADVANCE = "ADVANCE"
SCHEME_UNKNOWN = ""

# Advance authorisation and RoDTEP scrip numbers are both 10 digits. Used
# both for zero-padding (rule 2) and by the split guard (rule 1).
LICENSE_NUMBER_WIDTH = 10

# A token only counts as a licence number - and so only lets a cell be
# split - if it is digits of about the right length. Deliberately a little
# looser than exactly 10 so a genuinely 9- or 11-digit number still splits,
# and deliberately not `\d+` so a 4-digit year or a 2-digit serial can't
# authorise shredding a cell of some other shape.
_LICENSE_TOKEN = re.compile(r"^\d{7,12}$")

# Separators seen in real multi-licence cells ('/'), plus the ones a
# hand-typed cell is likely to use next. NOT the hyphen: '-' appears inside
# other identifier series in this data (see CLAUDE.md's HRS short-PO
# series), and nothing has ever shown it joining licence numbers.
_CELL_SEPARATORS = re.compile(r"[\/,;|\n\r]+")

# plant key -> (line item model, display label). Same keys and labels
# imports_views._PLANTS uses, kept here so this module stays importable from
# anywhere (a service must not import from the API layer).
_PLANT_LINE_ITEMS = (
    ("hrs", HRSImportPOLineItem, "HRS-Silvassa"),
    ("achhad", RTPAchhadImportPOLineItem, "RTP-Achhad"),
    ("vapi", RTPVapiImportPOLineItem, "RTP-Vapi"),
)


@dataclass(frozen=True)
class LicenseCitation:
    """One import PO line item naming one licence. A line naming three
    licences produces three citations - one per licence, each carrying the
    other two in `shared_with` so a reader can see that this line's value is
    NOT attributable to this licence alone."""

    scheme: str
    license_number: str       # normalised - the join key
    license_number_raw: str   # the verbatim cell, exactly as synced
    license_type_raw: str
    plant_key: str
    plant_label: str
    po_number: str
    item_id: str
    description: str
    boe_number: str
    qty: object               # qty_as_per_boe - what customs actually cleared
    uom: str
    landed_value: object      # total_inclusive_value, INR (see the model's help_text)
    shared_with: tuple


def classify_scheme(license_type: str) -> str:
    """Which ledger a line item's `License Type` cell refers to. Returns
    SCHEME_UNKNOWN for a blank or unrecognised type - such a line is still
    surfaced (it names a licence number, so somebody meant something by it),
    just not attributed to either ledger."""
    text = (license_type or "").strip().upper()
    if SCHEME_RODTEP in text:
        return SCHEME_RODTEP
    if SCHEME_ADVANCE in text:
        return SCHEME_ADVANCE
    return SCHEME_UNKNOWN


def normalize_license_number(raw: str) -> str:
    """The join key for one licence number: trimmed, and zero-padded to
    LICENSE_NUMBER_WIDTH when it is all digits and shorter than that (rule 2
    in this module's header - '311051817' and '0311051817' are the same
    authorisation). Anything that isn't a plain number is returned trimmed
    and upper-cased, never padded or otherwise reshaped."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text.zfill(LICENSE_NUMBER_WIDTH)
    return text.upper()


def split_license_numbers(raw: str) -> list[str]:
    """Split one `License Number` cell into the licence numbers it names,
    normalised and de-duplicated in source order.

    Splits ONLY when every token that comes out looks like a licence number
    (rule 1 in this module's header). A cell that doesn't split cleanly is
    returned whole - unmatched but intact and visible, which is strictly
    better than several tokens that match nothing and read as a clean miss.
    """
    text = (raw or "").strip()
    if not text:
        return []

    tokens = [t.strip() for t in _CELL_SEPARATORS.split(text) if t.strip()]
    if len(tokens) > 1 and not all(_LICENSE_TOKEN.match(t) for t in tokens):
        tokens = [text]

    seen, out = set(), []
    for token in tokens:
        key = normalize_license_number(token)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def collect_citations(scheme: str | None = None) -> list[LicenseCitation]:
    """Every (import line item, licence) pair across all three plants, for
    one scheme or for all of them. Three queries total - one per plant, with
    the PO joined in - not one per line item."""
    citations: list[LicenseCitation] = []
    for plant_key, model, plant_label in _PLANT_LINE_ITEMS:
        items = (
            model.objects
            .filter(purchase_order__is_active=True)
            .exclude(license_number="")
            .select_related("purchase_order")
            .order_by("purchase_order__po_number", "item_id")
        )
        for item in items:
            item_scheme = classify_scheme(item.license_type)
            if scheme is not None and item_scheme != scheme:
                continue
            numbers = split_license_numbers(item.license_number)
            for number in numbers:
                citations.append(LicenseCitation(
                    scheme=item_scheme,
                    license_number=number,
                    license_number_raw=item.license_number,
                    license_type_raw=item.license_type,
                    plant_key=plant_key,
                    plant_label=plant_label,
                    po_number=item.purchase_order.po_number,
                    item_id=item.item_id,
                    description=item.description,
                    boe_number=item.boe_number,
                    qty=item.qty_as_per_boe,
                    uom=item.uom,
                    landed_value=item.total_inclusive_value,
                    shared_with=tuple(n for n in numbers if n != number),
                ))
    return citations


def citations_by_license(citations: list[LicenseCitation]) -> dict[str, list[LicenseCitation]]:
    """Group citations by their normalised licence number - the shape both
    ledger endpoints read, so neither re-walks the list per row."""
    grouped: dict[str, list[LicenseCitation]] = {}
    for citation in citations:
        grouped.setdefault(citation.license_number, []).append(citation)
    return grouped


def citation_totals(citations: list[LicenseCitation]) -> dict:
    """Roll one licence's citations up to the numbers a panel row shows.

    `landedValue` sums each cited line's FULL landed value and is therefore
    NOT apportioned between the licences a multi-licence line names - there
    is nothing in any source to apportion it by, and inventing a split
    (equal shares? by qty?) would put a made-up number next to real ones.
    `sharedLines` counts the lines this applies to, so the panel can say so
    instead of the reader having to guess."""
    line_keys = {(c.plant_key, c.po_number, c.item_id) for c in citations}
    boes = [c.boe_number for c in citations if c.boe_number]
    return {
        "lineCount": len(line_keys),
        "poCount": len({(c.plant_key, c.po_number) for c in citations}),
        "plants": sorted({c.plant_label for c in citations}),
        "boeNumbers": sorted(set(boes)),
        "landedValue": sum((c.landed_value for c in citations if c.landed_value is not None), Decimal("0")),
        "sharedLines": sum(1 for c in citations if c.shared_with),
    }
