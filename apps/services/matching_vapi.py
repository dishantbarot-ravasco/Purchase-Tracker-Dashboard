"""
PO <-> MIR and MIR <-> Stock reconciliation for RTP-Vapi.

Thin per-plant wrapper - see matching.py's module docstring for the shared
pattern and apps/services/matching_core.py for the actual algorithm. What's
genuinely different about Vapi: its MIR has no "Net" column, so
taxable_value is used directly as the value-closeness comparator (no `or
mir.net` fallback - RTPVapiMIREntry has no `net` field at all); its Stock
lot uses HRS's stronger (material, vendor) gate via `supplier_name`
(confirmed a real, distinct vendor column - see RTP-Vapi's section header
comment in apps/core/models.py), not Achhad's material-only gate.

RTPVapiMIREntry.po_number_raw was 0% populated when this comment was first
written - it no longer is. The plant head added a 'PURCHASE ORDER' column to
the live MIR file 2026-09-11 (see parsers/vapi_mir.py); measured 2026-09-19
across 1,489 rows it is 97.5% filled, and 29.0% names an order the master
CSV actually holds (the rest is mostly the literal word 'VERBAL', or several
orders joined by a hyphen - see matching_core.py's _MULTI_PO_HYPHEN_RE for
that shape). The tier-1 PO-number shortcut fires for real now.
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
    ManualMirMatch,
    SyncRun,
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
# Vapi's po_number_raw was 100% blank when this was first tuned, so
# identification depended ENTIRELY on the material threshold with no PO-
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
# it).
#
# RE-CHECKED, NOT JUST CARRIED OVER (2026-09-19): now that po_number_raw is
# genuinely populated, the obvious question is whether identification can
# lean on it and raise this back to HRS/Achhad's 0.3. Measured directly
# (real run_full_match() against a throwaway DB copy, HRS/Achhad's
# threshold substituted in): it COSTS 7 real import matches (Chloroprene
# M-40K/M-42, SSBR, DTDM Powder - all near-zero-overlap free-text
# descriptions against terse MIR codes, same shape as the case above) and
# gains nothing. 0.2 stays right even though the reason first written down
# for it is now stale - see identification_two_of_three below for the
# actual PO-number-fallback change that was safe to make instead.
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
    plant_key="vapi",
    # Manual MIR pins (2026-09-21) - the model and this plant's SyncRun
    # value, injected rather than imported inside matching_core so that
    # module keeps its "no model imports" shape. See ManualMirMatch.
    manual_match_model=ManualMirMatch,
    syncrun_plant=SyncRun.Plant.RTP_VAPI,
    stock_rate_field="basic_rate",
    stock_vendor_field="supplier_name",
    stock_code_field="",
    stock_uom_field="uom",
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
    # MIR<->Stock three-tier identification (2026-09-21). Material is STILL
    # mandatory and still primary - the '8MPA RECLAIM RUBBER' <-> 'GREASE
    # EP 1' failure described above is exactly why date alone never gets to
    # identify, and it still doesn't; the new path needs date AND rate, and
    # is further guarded by the grade-code contradiction gate and tier-1
    # exclusivity (see match_mir_entry_stock()).
    #
    # VAPI'S GAIN IS MOSTLY TEXT CLEANING, NOT SCORING. Its MIR prefixes the
    # SAP material code to the description ('RM00011014 ZINC OXIDE', 173 of
    # 1,489 rows) and its Stock sheet appends the holding plant ('RECLAIM
    # RUBBER 6MPA HRS') - both rare-vocabulary noise that dominated an
    # IDF-weighted comparison. clean_mir_material_for_stock()/
    # clean_stock_material() strip them, worth +27 matched rows on their own
    # and most of why exact-name matches alone go 24 -> 71 here.
    #
    # Measured on live Vapi data: 24 -> 304 matched MIR rows (1.6% -> 20.4%),
    # of which 230 come from the scorer and 3 from the date+rate path.
    # Same-date rate agreement 92-93% across 38 -> 45 such pairs.
    #
    # 20.4% READS LOW AND IS NOT COMPARABLE TO THE OTHER TWO PLANTS - 1,489 is
    # the wrong denominator here. 1,133 of Vapi's MIR rows are for materials
    # that appear in NO plant's RM Stock sheet (conveyor belting, conveyor
    # fabric, rubber compound, MS crates - finished and semi-finished goods,
    # while the Stock sheets hold chemicals and raw rubber). Against the 356
    # rows whose material is actually in the sheet, this matches 304 - 85%,
    # in line with HRS's 84%. Madura alone accounts for 703 of the excluded
    # rows and is now registered in NO_RM_STOCK_VENDORS.
    #
    # Note Vapi's stock_material_threshold is 0.45 like the others, NOT its
    # own lowered MATERIAL_MATCH_THRESHOLD of 0.2 - that constant is PO<->MIR's
    # and was tuned against a different comparison entirely.
    stock_material_threshold=Decimal("0.45"),
    stock_date_rate_path=True,
    # Identification 2-of-3 (2026-09-19, project owner) - Achhad and HRS
    # first (2026-09-18), Vapi once its MIR PO coverage was actually
    # measured rather than assumed still-blank (see the module docstring
    # above). See matching_core._MatchConfig.identification_two_of_three for
    # the rule itself; the case for enabling it here:
    #
    #   RTP VAPI MIR FILE 2026-27.xlsx, 1,489 rows (2026-09-19): A/B/C
    #   measured directly (real run_full_match() against a throwaway DB
    #   copy) - 583 domestic matches with po_number_raw blanked out
    #   entirely, 588 with the column live and vendor still mandatory, 599
    #   with this flag on. The flag is STRICTLY ADDITIVE over the live
    #   default: +11, 0 lost, 0 re-pointed.
    #
    # The 11 split into the same two halves Achhad/HRS's own comments
    # describe, measured separately as those comments say to:
    #   - 7 are the narrowed no-PO-vendor exclusion (_MirCandidateIndex's
    #     docstring) - Tinna Rubber and Eternia Trading are both registered
    #     NO_PO_SUPPLIER, but the master CSV now raises real orders against
    #     them (six agree with the order to the rupee; one is an open
    #     partial receipt, 325 of 500 @182).
    #   - 4 are the 2-of-3 rule proper, all on Madura Technical Textiles'
    #     PO 1000001433 - its receipts are written under the party names
    #     'MADURA INDL TEXTILES LTD'/'MADURA TECHNICAL FABRICS LTD.', ~10
    #     characters short of clearing _vendor_matches() against 'Madura
    #     Technical Textiles Ltd'. One of the four (MIR row 536, rate 250
    #     against the order's own 170) looks like the PO number copied down
    #     a column rather than re-typed per receipt - it binds, and lands
    #     flagged severity=material, which is the right outcome: surfaced,
    #     not hidden.
    #
    # Vapi's own MATERIAL_MATCH_THRESHOLD (0.2, not HRS/Achhad's 0.3) is
    # unaffected by this flag and was re-measured, not just carried over -
    # see that constant's own comment.
    identification_two_of_three=True,
)


def run_full_match() -> dict:
    return matching_core.run_full_match(MATCH_CONFIG)


def match_po_mir_line_item(po_line_item):
    return matching_core.match_po_mir_line_item(MATCH_CONFIG, po_line_item)


def match_import_po_mir_line_item(import_line_item):
    return matching_core.match_import_po_mir_line_item(MATCH_CONFIG, import_line_item)


def match_mir_entry_stock(mir_entry):
    return matching_core.match_mir_entry_stock(MATCH_CONFIG, mir_entry)
