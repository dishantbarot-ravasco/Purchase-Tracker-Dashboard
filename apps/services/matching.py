"""
PO <-> MIR and MIR <-> Stock reconciliation for HRS.

Thin per-plant wrapper: builds this plant's _MatchConfig and re-exports
run_full_match()/match_po_mir_line_item()/match_import_po_mir_line_item()/
match_mir_entry_stock() under the same names every caller already uses
(match_hrs.py, apps/api/routers/_domestic_base.py, imports_views.py). The
actual scoring/gating/exclusive-claiming logic lives in
apps/services/matching_core.py, shared with matching_achhad.py/
matching_vapi.py - see that module's docstring for the full algorithm.
What's genuinely different about HRS lives here: HRS's MIR value falls back
to `net` when `taxable_value` is blank, and its Stock lot has both a vendor
column (`party_name`) and a `basic_rate` field (not Achhad's vendor-less,
`rate`-named shape).
"""

from decimal import Decimal

from apps.core.models import (
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSDomesticPOLineItem,
    HRSPOMirMatch,
    HRSRMLot,
)
from apps.services import matching_core
from apps.services.matching_core import _MatchConfig

MATCH_THRESHOLD = Decimal("0.55")
# Zero tolerance (2026-09-04, project owner) on quantity and rate - ANY
# nonzero diff on an already-matched pair flags it, even 1kg out of 1000kg.
# This supersedes the earlier "5%, deliberately not the artifact's 10%"
# decision (see CLAUDE.md) - kept as a named constant rather than inlining
# `> 0` everywhere it's used, so a future policy change again still has
# exactly one place to edit. Value is the one exception - see
# VALUE_FLAG_EPSILON below (Match Accuracy Programme fix 3.F, confirmed with
# the project owner 2026-09-05): value is derived (qty x rate, plus
# tax-split rounding), so it gets a small absolute epsilon instead of exact
# zero tolerance.
FLAG_DIFF_PCT = Decimal("0")
# Fix 3.F: an absolute currency epsilon on value_diff_pct only - quantity
# and rate are directly reported by the source sheets and stay at exact-zero
# tolerance (FLAG_DIFF_PCT above); value accumulates rounding from qty x
# rate and from tax splits, so a difference of a rupee or two is
# representational, not a discrepancy.
VALUE_FLAG_EPSILON = Decimal("1.00")

# Identification/Financial-Check redesign (2026-09-07): material is a
# boolean identification gate now (see MATERIAL_MATCH_THRESHOLD/_MatchConfig
# below), not a scored factor - these three are the financial-closeness
# tie-breaker weights only. Ratio kept at qty:rate:value = 2:2:3 (the same
# relative weight the old 4-factor score used), renormalized to sum to 1
# now that material's 0.30 share is gone.
_WEIGHT_QTY = Decimal("0.29")
_WEIGHT_RATE = Decimal("0.29")
_WEIGHT_VALUE = Decimal("0.42")
MATERIAL_MATCH_THRESHOLD = Decimal("0.3")

MATCH_CONFIG = _MatchConfig(
    po_item_model=HRSDomesticPOLineItem,
    import_item_model=HRSImportPOLineItem,
    mir_model=HRSMIREntry,
    po_mir_match_model=HRSPOMirMatch,
    import_po_mir_match_model=HRSImportPOMirMatch,
    mir_stock_match_model=HRSMirStockMatch,
    stock_lot_model=HRSRMLot,
    match_threshold=MATCH_THRESHOLD,
    flag_diff_pct=FLAG_DIFF_PCT,
    value_flag_epsilon=VALUE_FLAG_EPSILON,
    weight_qty=_WEIGHT_QTY,
    weight_rate=_WEIGHT_RATE,
    weight_value=_WEIGHT_VALUE,
    material_match_threshold=MATERIAL_MATCH_THRESHOLD,
    # PO's Net Value <-> MIR's own Net column - both pre-discount (project
    # owner's explicit mapping, 2026-09-07). This replaces the previous
    # `mir.taxable_value or mir.net` comparator, which compared PO's
    # pre-discount Net Value against MIR's post-discount Taxable Value -
    # usually indistinguishable in practice (MIR's discount columns are
    # almost always blank/zero on real data) but not the same field.
    # Taxable Value is now its own separate data-mismatch-only comparison
    # (mir_taxable_value's default, against PO's "Total Value") rather than
    # standing in for Net.
    mir_value=lambda mir: mir.net,
    stock_rate_field="basic_rate",
    stock_vendor_field="party_name",
    # Imports identification/financial-check redesign (2026-09, project
    # owner: same treatment as domestic for HRS/Achhad, Vapi excluded for
    # now - see matching_core.py's _MatchConfig docstring and
    # matching_vapi.py's own comment on why Vapi stays False).
    import_extended_fields=True,
    # MIR<->Stock identification/financial-check extension (2026-09-08) -
    # see matching_core.py's match_mir_entry_stock() docstring for the full
    # design (material stays the sole, mandatory identification factor;
    # Qty/Value are only ever compared when the candidate ALSO matched via
    # Rec. DT., not for every material-matched candidate - date alone was
    # tried and reverted as an identification path, real cross-vendor same-
    # day-delivery false positives confirmed against live HRS/Vapi data).
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
