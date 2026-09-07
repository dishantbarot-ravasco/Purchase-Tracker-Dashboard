"""
PO <-> MIR and MIR <-> Stock reconciliation for RTP-Vapi.

Thin per-plant wrapper - see matching.py's module docstring for the shared
pattern and apps/services/matching_core.py for the actual algorithm. What's
genuinely different about Vapi: its MIR has no "Net" column, so
taxable_value is used directly as the value-closeness comparator (no `or
mir.net` fallback - RTPVapiMIREntry has no `net` field at all); its Stock
lot uses HRS's stronger (material, vendor) gate via `supplier_name`
(confirmed a real, distinct vendor column - see RTP-Vapi's section header
comment in apps/core/models.py), not Achhad's material-only gate; and
RTPVapiMIREntry.po_number_raw was 0% populated on every real row confirmed
this session, so the tier-1 PO-number shortcut currently never fires
against real Vapi data - it stays wired up so it starts working
automatically the day that column gets populated.
"""

from decimal import Decimal

from apps.core.models import (
    RTPVapiImportPOLineItem,
    RTPVapiImportPOMirMatch,
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiPOLineItem,
    RTPVapiPOMirMatch,
    RTPVapiStockLot,
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
    po_item_model=RTPVapiPOLineItem,
    import_item_model=RTPVapiImportPOLineItem,
    mir_model=RTPVapiMIREntry,
    po_mir_match_model=RTPVapiPOMirMatch,
    import_po_mir_match_model=RTPVapiImportPOMirMatch,
    mir_stock_match_model=RTPVapiMirStockMatch,
    stock_lot_model=RTPVapiStockLot,
    match_threshold=MATCH_THRESHOLD,
    flag_diff_pct=FLAG_DIFF_PCT,
    value_flag_epsilon=VALUE_FLAG_EPSILON,
    weight_material=_WEIGHT_MATERIAL,
    weight_qty=_WEIGHT_QTY,
    weight_rate=_WEIGHT_RATE,
    weight_value=_WEIGHT_VALUE,
    mir_value=lambda mir: mir.taxable_value,
    stock_rate_field="basic_rate",
    stock_vendor_field="supplier_name",
)


def run_full_match() -> dict:
    return matching_core.run_full_match(MATCH_CONFIG)


def match_po_mir_line_item(po_line_item):
    return matching_core.match_po_mir_line_item(MATCH_CONFIG, po_line_item)


def match_import_po_mir_line_item(import_line_item):
    return matching_core.match_import_po_mir_line_item(MATCH_CONFIG, import_line_item)


def match_mir_entry_stock(mir_entry):
    return matching_core.match_mir_entry_stock(MATCH_CONFIG, mir_entry)
