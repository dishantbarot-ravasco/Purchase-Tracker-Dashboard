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
    RTPVapiDomesticPOLineItem,
    RTPVapiPOMirMatch,
    RTPVapiRMLot,
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

# Updated 2026-09-07: matching_core.py's _MatchConfig dropped weight_material
# for an identification boolean gate - see matching.py's own comment for the
# full reasoning. Vapi's own tax_type/total_value/total_inclusive_value
# checks ARE wired (Vapi's PO CSV has the same columns as HRS/Achhad's - see
# matching_core.py's module docstring); Vapi's own weight ratio is unchanged
# in spirit (2:2:3 renormalized).
#
# MATERIAL_MATCH_THRESHOLD is deliberately LOWER than HRS/Achhad's 0.3, not
# copied from it - confirmed against a real sync+match run this session:
# Vapi's po_number_raw is 100% blank (see the module docstring above), so
# identification depends ENTIRELY on the material threshold with no PO-
# number fallback, unlike HRS/Achhad where a PO-number hit can carry a match
# whose material score is weak. At 0.3, real legitimate matches were
# rejected outright - e.g. PO "PTFE Coated Fabric, brown, 0.38mm, width
# 950mm x 50m, 1 Roll" (qty/rate exact match otherwise) against MIR "PTFE
# COATED F G FABRIC 16NP BROWN" scores only 0.27 token overlap (PO
# descriptions here are verbose per-roll-dimension free text; MIR's are
# terse abbreviated codes - genuinely different vocabularies for the same
# item, not a bad match). 0.2 recovers this and similar real cases seen in
# the same run without visibly worse false-positive risk (still gated by
# vendor + picked by qty/rate/value closeness among candidates that clear
# it) - re-check with real data again if this needs further tuning up or
# down.
_WEIGHT_QTY = Decimal("0.29")
_WEIGHT_RATE = Decimal("0.29")
_WEIGHT_VALUE = Decimal("0.42")
MATERIAL_MATCH_THRESHOLD = Decimal("0.2")

MATCH_CONFIG = _MatchConfig(
    po_item_model=RTPVapiDomesticPOLineItem,
    import_item_model=RTPVapiImportPOLineItem,
    mir_model=RTPVapiMIREntry,
    po_mir_match_model=RTPVapiPOMirMatch,
    import_po_mir_match_model=RTPVapiImportPOMirMatch,
    mir_stock_match_model=RTPVapiMirStockMatch,
    stock_lot_model=RTPVapiRMLot,
    match_threshold=MATCH_THRESHOLD,
    flag_diff_pct=FLAG_DIFF_PCT,
    value_flag_epsilon=VALUE_FLAG_EPSILON,
    weight_qty=_WEIGHT_QTY,
    weight_rate=_WEIGHT_RATE,
    weight_value=_WEIGHT_VALUE,
    material_match_threshold=MATERIAL_MATCH_THRESHOLD,
    mir_value=lambda mir: mir.taxable_value,
    stock_rate_field="basic_rate",
    stock_vendor_field="supplier_name",
    # Imports identification/financial-check redesign (2026-09): HRS/Achhad
    # first, Vapi added the same day (project owner: "use HRS/Achhad's
    # settings for Vapi imports too, but vapi's MIR file structure is quite
    # different so keep the domestic in reference"). RTPVapiImportPOMirMatch
    # now has the extended columns (see that model's docstring). "HRS/Achhad's
    # settings" means this flag - the WRITE-PATH treatment (extended
    # identification/data-mismatch fields) - not the numeric thresholds
    # above: MATERIAL_MATCH_THRESHOLD stays Vapi's own tuned 0.2 (not HRS/
    # Achhad's 0.3) and mir_value stays taxable_value (not mir.net, which
    # doesn't exist on RTPVapiMIREntry) precisely because those already
    # correctly account for Vapi's genuinely different MIR schema - "keep
    # the domestic in reference" means don't re-tune what Vapi's own
    # domestic matching already validated against real data, just extend
    # imports to write the same richer field set HRS/Achhad's imports do.
    import_extended_fields=True,
    # MIR<->Stock identification/financial-check extension (2026-09-08,
    # project owner: replicate HRS's/Achhad's Raw Material treatment for
    # Vapi too). Material stays the sole, mandatory identification factor
    # here too (vendor-gated, via Supplier Name - confirmed genuine per the
    # RTP-Vapi section header comment in models.py, not just an echo of the
    # PLANT tag) - date was tried as an alternative identification path and
    # reverted after real Vapi data confirmed the same same-vendor-
    # same-day-different-material false positives found on HRS (e.g.
    # '8MPA RECLAIM RUBBER' wrongly matched to 'GREASE EP 1' purely on
    # vendor+date) - see match_mir_entry_stock()'s docstring for the full
    # story. This flag only adds the Qty/Value data-mismatch checks on top
    # of the existing material(+vendor)-only gate.
    stock_extended_fields=True,
)


def run_full_match() -> dict:
    return matching_core.run_full_match(MATCH_CONFIG)


def match_po_mir_line_item(po_line_item):
    return matching_core.match_po_mir_line_item(MATCH_CONFIG, po_line_item)


def match_import_po_mir_line_item(import_line_item):
    return matching_core.match_import_po_mir_line_item(MATCH_CONFIG, import_line_item)


def match_mir_entry_stock(mir_entry):
    return matching_core.match_mir_entry_stock(MATCH_CONFIG, mir_entry)
