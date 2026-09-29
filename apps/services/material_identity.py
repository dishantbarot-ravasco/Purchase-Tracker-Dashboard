"""
apps/services/material_identity.py - what makes two PO line descriptions
the same material, for the material master (apps/services/materials.py).

Dependency-free (no Django imports), like stock_identity.py, so migration
0077 can import it.

The one rule beyond normalize_material(): a fabric PO describes each ROLL,
not the material - "EE-160 fabric roll, width 67cm, GSM 570, length 512m,
1 roll, total weight 447.078". The length, roll count and weight belong to
that delivery, so they are dropped from the name: the material is the
product, width and GSM. Measured 2026-09-29 on the local PO data, this turns
565 roll-level "materials" into 248 real ones.
"""

import re

from apps.services.parsers.common import normalize_material

_DELIVERY_PARTS = re.compile(r"^\s*(length\b|total\s+weight\b|\d+(\.\d+)?\s*rolls?\b)", re.IGNORECASE)


def material_name(description: str) -> str:
    """The description with the per-delivery parts of a comma-separated
    roll description removed; anything else unchanged."""
    text = (description or "").strip()
    if "," not in text or "roll" not in text.lower():
        return text
    kept = [part.strip() for part in text.split(",") if not _DELIVERY_PARTS.match(part)]
    return ", ".join(p for p in kept if p)


def material_key(description: str) -> str:
    return normalize_material(material_name(description))
