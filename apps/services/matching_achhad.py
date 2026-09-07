"""
PO <-> MIR and MIR <-> Stock reconciliation for RTP-Achhad.

Thin per-plant wrapper - see matching.py's module docstring for the shared
pattern and apps/services/matching_core.py for the actual algorithm. What's
genuinely different about Achhad: its Stock sheet has no vendor column at
all (confirmed - see RTPAchhadStockLot's docstring), so MIR<->Stock matching
here gates on normalized material description alone, not (material,
vendor) - a materially weaker guarantee than HRS's/Vapi's matcher, and its
Stock lot's rate field is named `rate`, not `basic_rate`.
"""

from decimal import Decimal

from apps.core.models import (
    RTPAchhadImportPOLineItem,
    RTPAchhadImportPOMirMatch,
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadPOLineItem,
    RTPAchhadPOMirMatch,
    RTPAchhadStockLot,
)
from apps.services import matching_core
from apps.services.matching_core import _MatchConfig

MATCH_THRESHOLD = Decimal("0.55")
# Zero tolerance (2026-09-04, project owner) on quantity and rate - see
# matching.py's own comment on this same constant for the full reasoning;
# kept identical across all three plants on purpose.
FLAG_DIFF_PCT = Decimal("0")
# Fix 3.F: see matching.py's own comment - value gets a small absolute
# epsilon instead of exact-zero tolerance, kept identical across all three
# plants.
VALUE_FLAG_EPSILON = Decimal("1.00")

_WEIGHT_MATERIAL = Decimal("0.30")
_WEIGHT_QTY = Decimal("0.20")
_WEIGHT_RATE = Decimal("0.20")
_WEIGHT_VALUE = Decimal("0.30")

MATCH_CONFIG = _MatchConfig(
    po_item_model=RTPAchhadPOLineItem,
    import_item_model=RTPAchhadImportPOLineItem,
    mir_model=RTPAchhadMIREntry,
    po_mir_match_model=RTPAchhadPOMirMatch,
    import_po_mir_match_model=RTPAchhadImportPOMirMatch,
    mir_stock_match_model=RTPAchhadMirStockMatch,
    stock_lot_model=RTPAchhadStockLot,
    match_threshold=MATCH_THRESHOLD,
    flag_diff_pct=FLAG_DIFF_PCT,
    value_flag_epsilon=VALUE_FLAG_EPSILON,
    weight_material=_WEIGHT_MATERIAL,
    weight_qty=_WEIGHT_QTY,
    weight_rate=_WEIGHT_RATE,
    weight_value=_WEIGHT_VALUE,
    mir_value=lambda mir: mir.taxable_value or mir.net,
    stock_rate_field="rate",
    stock_vendor_field=None,
)


def run_full_match() -> dict:
    return matching_core.run_full_match(MATCH_CONFIG)


def match_po_mir_line_item(po_line_item):
    return matching_core.match_po_mir_line_item(MATCH_CONFIG, po_line_item)


def match_import_po_mir_line_item(import_line_item):
    return matching_core.match_import_po_mir_line_item(MATCH_CONFIG, import_line_item)


def match_mir_entry_stock(mir_entry):
    return matching_core.match_mir_entry_stock(MATCH_CONFIG, mir_entry)
