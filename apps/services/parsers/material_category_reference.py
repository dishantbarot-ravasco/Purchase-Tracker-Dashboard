"""
apps/services/parsers/material_category_reference.py — parses the plant
manager's Category/Subcategory reference list (a CSV with columns "SAP Item
Code, Description, HSN Code, Category, Subcategory (SAP Product Group),
UOM") into rows ready to upsert into MaterialCategoryReference.

See MaterialCategoryReference's own docstring (apps/core/models.py) for why
the match key is normalized Material Description, not SAP Item Code (project
owner, 2026-09-08: SAP Item Code "is not trust worthy as it's not maintain
thoroughly").

Two real shape quirks in the source list, both handled here:
  - Description carries the item code as a trailing suffix, e.g.
    "SACK CARBON (22001065)" - our own Stock files' descriptions never do
    this, so left un-stripped this would silently fail to match anything.
    Stripped before normalizing (_strip_trailing_code_suffix()).
  - "Subcategory (SAP Product Group)" is one column holding both a human
    label and a short code, e.g. "CARBON BLACK (RM-CB001)" or
    "RECOVERED CARBON BLA (RM-CB002)" - split into subcategory/
    subcategory_code (_split_subcategory()).
"""

import csv
import io
import re
from dataclasses import dataclass

from apps.services.parsers.common import normalize_material, to_str

EXPECTED_HEADER = ["SAP Item Code", "Description", "HSN Code", "Category", "Subcategory (SAP Product Group)", "UOM"]

_TRAILING_CODE_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")
_SUBCATEGORY_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")


@dataclass
class ParsedMaterialCategoryReference:
    description: str
    normalized_description: str
    category: str
    subcategory: str
    subcategory_code: str
    hsn_code: str
    uom: str
    sap_item_code: str
    source_row_ref: str


class HeaderMismatch(Exception):
    """Raised when the CSV's header row doesn't match what this parser was
    built against - better to fail loudly than silently misread columns."""


def _strip_trailing_code_suffix(description: str) -> str:
    """"SACK CARBON (22001065)" -> "SACK CARBON" - only the description's
    OWN trailing parenthetical, not applied to subcategory (handled
    separately by _split_subcategory, which needs to keep the code, not
    discard it)."""
    return _TRAILING_CODE_SUFFIX_RE.sub("", description).strip()


def _split_subcategory(raw: str) -> tuple[str, str]:
    """"CARBON BLACK (RM-CB001)" -> ("CARBON BLACK", "RM-CB001"). Returns
    (raw, "") unchanged if it doesn't match that shape - some future row
    might not carry a code at all, and the label is still worth keeping."""
    m = _SUBCATEGORY_RE.match(raw.strip())
    if not m:
        return raw.strip(), ""
    return m.group(1).strip(), m.group(2).strip()


def parse_material_category_reference_csv(csv_text: str) -> list[ParsedMaterialCategoryReference]:
    """Parses the reference CSV into one ParsedMaterialCategoryReference per
    row. Raises HeaderMismatch immediately if the header doesn't match what
    this parser was built against. Rows with a blank Description are
    skipped (nothing to key a lookup on)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or [h.strip() for h in reader.fieldnames] != EXPECTED_HEADER:
        raise HeaderMismatch(
            f"CSV header does not match expected schema.\nExpected: {EXPECTED_HEADER}\nGot: {reader.fieldnames}"
        )

    rows = []
    for i, row in enumerate(reader, start=2):  # header is row 1
        raw_description = to_str(row["Description"])
        if not raw_description:
            continue

        description = _strip_trailing_code_suffix(raw_description)
        subcategory, subcategory_code = _split_subcategory(to_str(row["Subcategory (SAP Product Group)"]))

        rows.append(
            ParsedMaterialCategoryReference(
                description=description,
                normalized_description=normalize_material(description),
                category=to_str(row["Category"]),
                subcategory=subcategory,
                subcategory_code=subcategory_code,
                hsn_code=to_str(row["HSN Code"]),
                uom=to_str(row["UOM"]),
                sap_item_code=to_str(row["SAP Item Code"]),
                source_row_ref=str(i),
            )
        )
    return rows
