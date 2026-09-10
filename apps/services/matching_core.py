"""
Shared PO<->MIR<->Stock matching implementation for all three plants (HRS,
RTP-Achhad, RTP-Vapi). Extracted from what used to be three near-identical
copies in matching.py/matching_achhad.py/matching_vapi.py (see Match
Accuracy Programme doc 03, Phase 0) - those three modules now each build one
_MatchConfig at import time and re-export the functions below under their
existing names, the same _PlantConfig-per-plant pattern already proven in
apps/api/routers/_domestic_base.py for the HTTP layer. Only what's genuinely
different per plant is injected via _MatchConfig: which model classes to
query, and how to read a MIR entry's pre-tax value / a stock lot's rate and
vendor fields (Vapi has no MIR `net` fallback; Achhad's stock lot has no
vendor field at all and uses `rate` not `basic_rate`). Everything else -
scoring weights, thresholds, the identification/financial-check algorithm,
exclusive MIR claiming - is one implementation, not three.

Vendor is always a hard gate, never scored - two records for different
vendors are never candidates for each other, no matter how well material/
amount line up.

**Identification/Financial-Check redesign (2026-09-07, project owner spec)**
supersedes the old Tier-1/Tier-2 blended-score design. PO<->MIR matching is
now two separate, differently-purposed passes instead of one blended score:

  IDENTIFICATION - decides which MIR row IS this PO line item's row, gated
  (not scored) on top of the vendor hard gate:
    Vendor        MANDATORY (the existing hard gate, unchanged)
    Material      token-overlap >= config.material_match_threshold
    PO Number     _po_number_matches() exact/substring hit
  A candidate must pass vendor AND at least one of {material, PO number} -
  "any 2 of these 3" per the spec, with vendor pinned mandatory rather than
  a free member of the 2-of-3 (project owner, 2026-09-07: "Vendor mandatory
  plus one of the other two" - a plain 2-of-3 with no mandatory field would
  let material+PO-number win with no vendor match at all, reopening the
  exact cross-vendor false-positive risk vendor-as-hard-gate exists to
  close). See _identification_pool().

  Once a line item has one or more identification-passing candidates, the
  best one is picked by financial closeness (qty/rate/net-value weighted
  score, _score()) - this is a tie-breaker among already-identified
  candidates, NOT a threshold that can reject an identified candidate
  outright. An identification-passing candidate is always accepted; there
  is no "identified but still below threshold, so unmatched" outcome
  anymore. match_threshold/MATCH_THRESHOLD stays as a per-plant constant
  (still asserted by tests, still exposed to the frontend as a confidence
  signal) but no longer gates whether a match is created.

  FINANCIAL CHECK - once identification has picked a row, every relevant
  field is compared, but only Qty and Rate produce a hard "Mismatched"
  error (`qty_mismatched`/`rate_mismatched`, zero-tolerance per
  config.flag_diff_pct, same policy as before). Every other financial
  field - UOM family, Net vs MIR's Net, Taxable Value (only meaningful
  against a single-line-item PO - see match_po_mir_line_item()'s own note
  on why), GST type structural consistency (IGST vs CGST+SGST,
  _tax_type_mismatch()), and Final/Invoice value - folds into one
  `data_mismatch` boolean instead of being blended into is_flagged/severity
  the way it used to be. `is_flagged` now means specifically "a qty or rate
  mismatch exists" - the two "real errors" the spec calls out - not "any
  discrepancy at all"; `data_mismatch` is the everything-else bucket.
  Absence of any identification-passing candidate at all is this system's
  "PO Not Found" outcome - still represented as no match row (see
  match_po_mir_line_item()'s docstring), not a stored enum value.

Exclusive MIR claiming (fix 2.B): confirmed against real data (31-39% of
current matches shared a MIR row across multiple line items, traced back to
the old tier-1 next() bug and to cross-PO Tier-2 collisions - not legitimate
partial receipts; no schema field or source-data pattern supports one MIR
row genuinely satisfying more than one PO line item) that a MIR row should
be claimed by at most one line item per run_full_match() pass. run_full_match()
considers every (line item, MIR) pair that passes identification across every
domestic AND import line item at once (they share one MIR table), sorts every
pair by financial score descending, then assigns greedily - a MIR row already
claimed by a higher-scoring pair is skipped, so a line item can fall through
to its own next-best identified candidate rather than losing its match
entirely.
match_po_mir_line_item()/match_import_po_mir_line_item() remain as simple,
single-item entry points (used by tests and any future single-item caller)
that pick the best candidate in isolation, with no knowledge of other line
items' claims - only run_full_match() enforces cross-item exclusivity, since
that's inherently a whole-run property.

MIR <-> Stock (per MIR entry): no exclusivity constraint - this is a
deliberate many-to-many relationship (the same material/vendor is received
into stock across multiple lots over time), unaffected by fix 2.B.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, NamedTuple, Optional

from django.db import transaction
from django.utils import timezone

from apps.services.parsers.common import normalize_material, normalize_uom, normalize_vendor, tokenize

TIER_PO_NUMBER = "po_number"
# TIER_MATERIAL replaces the old TIER_WEIGHTED label (2026-09-07 redesign -
# see module docstring): identification no longer runs a single blended
# score, so "tier" now just records which identification factor fired for
# the winning candidate - PO number, or material description alone.
TIER_MATERIAL = "material"

# *_diff_pct columns are DecimalField(max_digits=6, decimal_places=2) - see
# CLAUDE.md's "*_diff_pct columns need a clamp" note.
_MAX_DIFF_PCT = Decimal("9999.99")


@dataclass(frozen=True)
class _MatchConfig:
    """Everything genuinely different between HRS/Achhad/Vapi's matchers.
    Model classes are injected by reference, not branched on; `mir_value`/
    `stock_rate_field`/`stock_vendor_field` cover the three real schema
    differences (see module docstring)."""

    po_item_model: type
    import_item_model: type
    mir_model: type
    po_mir_match_model: type
    import_po_mir_match_model: type
    mir_stock_match_model: type
    stock_lot_model: type

    match_threshold: Decimal
    flag_diff_pct: Decimal
    # Fix 3.F: value_diff_pct is derived (qty x rate, plus tax-split
    # rounding), so a rupee or two of rounding isn't a real discrepancy the
    # way any nonzero qty/rate diff is under the zero-tolerance policy.
    # Applied as an ABSOLUTE currency difference, not a percentage - a small
    # percentage of a huge value can still be many rupees, and a huge
    # percentage of a tiny value can still be under a rupee. Reused as the
    # same epsilon for the Taxable-Value and Final-Value data-mismatch
    # checks below, not just the primary net-value one.
    value_flag_epsilon: Decimal
    weight_qty: Decimal
    weight_rate: Decimal
    weight_value: Decimal

    # Identification redesign (2026-09-07): material description is no
    # longer a scored factor, it's a boolean identification gate - token
    # overlap at/above this threshold counts as "material matches" for the
    # vendor-mandatory-plus-one-of-{material,PO number} rule. See
    # _identification_pool().
    material_match_threshold: Decimal = Decimal("0.3")

    # MIR entry -> its value for the primary net-value financial-check/
    # scoring comparison. HRS/Achhad compare against MIR's own `Net` column
    # (the project owner's explicit PO-Net-Value <-> MIR-Net mapping,
    # 2026-09-07 - both are pre-discount); Vapi has no `net` field at all and
    # stays on `taxable_value` (unchanged - see matching_vapi.py).
    mir_value: Callable[[object], object] = lambda mir: getattr(mir, "net", None)

    # Data-mismatch-only comparisons (2026-09-07 spec: "Total Value" -> MIR's
    # Taxable Value, "Total Inclusive Value" -> MIR's Final/Invoice value).
    # Both default to reading the obvious MIR column so HRS/Achhad need no
    # extra config; a plant with a differently-named final-value column can
    # override mir_final_value.
    mir_taxable_value: Callable[[object], object] = lambda mir: getattr(mir, "taxable_value", None)
    mir_final_value: Callable[[object], object] = (
        lambda mir: getattr(mir, "invoice_final_value", None) or getattr(mir, "total_amount", None)
    )

    stock_rate_field: str = ""  # "basic_rate" (HRS/Vapi) or "rate" (Achhad)
    stock_vendor_field: Optional[str] = None  # "party_name"/"supplier_name", or None (Achhad has no vendor column)

    # Imports identification/financial-check redesign (2026-09, HRS/Achhad
    # only - see matching_vapi.py's own comment on why Vapi stays out for
    # now): when True, import line items get the same tax_type/total_value/
    # total_inclusive_value data-mismatch treatment domestic line items
    # already have (_import_matchable()), and match_import_po_mir_line_item()/
    # run_full_match() write the extended identification/data-mismatch
    # columns to config.import_po_mir_match_model. False (the default, and
    # Vapi's setting) keeps the original 4-field _Matchable shape and the
    # original short defaults dict - required, since Vapi's own
    # RTPVapiImportPOMirMatch model has no columns to hold the extra fields.
    import_extended_fields: bool = False

    # MIR<->Stock identification/financial-check extension (2026-09-08, HRS
    # only for now - see match_mir_entry_stock()'s own docstring for the
    # full design and why it can't just reuse the PO<->MIR shape verbatim).
    # False (the default, and Achhad's/Vapi's setting) keeps the original
    # material-mandatory-vendor-gated behavior and the original 2-field
    # defaults dict - required, since RTPAchhadMirStockMatch/
    # RTPVapiMirStockMatch have no columns for the extra fields.
    stock_extended_fields: bool = False


# ── Internal scoring/gating helpers ─────────────────────────────────────────
# Pure, dependency-free functions - unit-tested directly
# (apps/services/tests/test_matching.py). One copy now, not three.

def _closeness(a, b) -> Decimal | None:
    """1.0 for an exact match, decaying linearly to 0 at a >=50% relative
    difference. None (treated as 0 in scoring) when either side is missing -
    a blank field should never look like a perfect match."""
    if a is None or b is None:
        return None
    a, b = Decimal(a), Decimal(b)
    if a == 0 and b == 0:
        return Decimal("1")
    denom = max(abs(a), abs(b))
    if denom == 0:
        return Decimal("1")
    diff_ratio = abs(a - b) / denom
    return max(Decimal("0"), Decimal("1") - diff_ratio * 2)


def _diff_pct(a, b) -> Decimal | None:
    """Percent difference of b relative to a, or None if either side is
    missing (a missing side means "nothing to compare yet", not "0% off" -
    callers must not treat None as a flaggable diff). a == 0 with a nonzero
    b has no finite percentage, so it's clamped to _MAX_DIFF_PCT instead of
    raising a ZeroDivisionError."""
    if a is None or b is None:
        return None
    a, b = Decimal(a), Decimal(b)
    if a == 0:
        return None if b == 0 else _MAX_DIFF_PCT
    return min(abs(a - b) / abs(a) * Decimal("100"), _MAX_DIFF_PCT)


def _token_overlap(a: str, b: str) -> Decimal:
    """Jaccard similarity (intersection / union) of the two strings'
    normalized token sets - the 30%-weighted "material description" factor
    in the scored match. An empty token set on either side scores 0 rather
    than dividing by zero, since a blank description can never be considered
    a real match on this factor."""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return Decimal("0")
    return Decimal(len(ta & tb)) / Decimal(len(ta | tb))


def _vendor_matches(a: str, b: str) -> bool:
    """Containment, not equality - HRS's Stock sheet appends a city suffix
    to its party_name that neither MIR nor the PO CSV carry (confirmed:
    'Rubamin Private Limited' in MIR/PO vs 'Rubamin Private Limited -
    Vadodara' in Stock, both normalizing to 'rubamin...' with no exact
    match). The shorter normalized name appearing inside the longer one
    catches this without loosening the gate into a fuzzy/scored check -
    vendor stays a hard yes/no, just not a strict string equality.
    A length floor avoids a short normalized name trivially matching
    everything (e.g. an empty or near-empty normalization)."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter in longer


# Fix 2.E: token-boundary PO-number matching, not a plain substring test.
# matching.py:130's old plain `in` test made 'HRS/HO/26-27/003' a false
# tier-1 hit against 'HRS/HO/26-27/0031' (a genuinely different PO). Splitting
# both sides on the same separators and comparing whole tokens (as a
# contiguous run, so 'HRS/HO/26-27/003' still matches inside the documented
# multi-PO shape 'HRS/HO/26-27/003 & 004') rejects the prefix collision while
# keeping that real multi-PO case working.
_PO_TOKEN_SPLIT = re.compile(r"[,&/;\s]+")
# Real data shape: openpyxl reads some PO-number cells as floats, so
# po_number_raw can carry a literal '.0' suffix (e.g. '3000001081.0') that
# the PO's own po_number field never has - strip it so token comparison
# doesn't regress every one of these real rows.
_TRAILING_ZERO_DECIMAL = re.compile(r"^(\d+)\.0+$")


def _po_tokens(value: str) -> list[str]:
    tokens = [t for t in _PO_TOKEN_SPLIT.split(value.strip().upper()) if t]
    out = []
    for t in tokens:
        m = _TRAILING_ZERO_DECIMAL.match(t)
        out.append(m.group(1) if m else t)
    return out


def _po_number_matches(po_number: str, po_number_raw: str) -> bool:
    """MIR's PO-number field is free text, not a guaranteed clean value -
    accept an exact match, or po_number's own tokens appearing as a
    contiguous run inside po_number_raw's tokens (some rows are typed as
    e.g. 'HRS/HO/26-27/003 & 004'). Whole-token comparison (not substring)
    rejects a prefix collision like '...003' matching '...0031'."""
    if not po_number or not po_number_raw:
        return False
    po_tokens = _po_tokens(po_number)
    if not po_tokens:
        return False
    raw_tokens = _po_tokens(po_number_raw)
    n = len(po_tokens)
    return any(raw_tokens[i : i + n] == po_tokens for i in range(len(raw_tokens) - n + 1))


# ── Shared scoring building blocks ──────────────────────────────────────────

def _candidate_mir_entries(config: _MatchConfig, vendor_name: str) -> list:
    vendor = normalize_vendor(vendor_name)
    candidates = list(config.mir_model.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name=""))
    return [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), vendor)]


def _material_matches(config: _MatchConfig, description: str, mir_description: str) -> bool:
    """Identification's material factor: token overlap at/above
    config.material_match_threshold counts as a match - a boolean gate now,
    not a scored factor (see module docstring's 2026-09-07 redesign)."""
    return _token_overlap(description, mir_description) >= config.material_match_threshold


def _identification_pool(config: _MatchConfig, candidates: list, item: "_Matchable", po_number: str) -> tuple[list, dict]:
    """Vendor is already satisfied by `candidates` (the hard gate ran in
    _candidate_mir_entries). Filters to candidates where at least one of
    {material, PO number} also matches - "vendor mandatory plus one of the
    other two" (project owner, 2026-09-07), not a plain unweighted 2-of-3
    (which would let material+PO-number win with no vendor match at all).
    Returns (pool, id_flags_by_mir_id) - id_flags_by_mir_id lets callers
    record which identification field(s) actually fired for the winning
    candidate (material_matched/po_number_matched), for transparency on the
    stored match row."""
    pool = []
    id_flags: dict[int, tuple[bool, bool]] = {}
    for c in candidates:
        material_matched = _material_matches(config, item.description, c.material_description)
        po_number_matched = _po_number_matches(po_number, c.po_number_raw)
        if material_matched or po_number_matched:
            pool.append(c)
            id_flags[c.id] = (material_matched, po_number_matched)
    return pool, id_flags


class _Matchable(NamedTuple):
    """One side's worth of fields the scoring loop needs, for either a
    domestic PO line item or an import line item (with its rate/value
    already currency-converted - see _import_rate_value_inr()). `value` is
    the PO's pre-discount Net Value (compared against MIR's own Net column
    per config.mir_value - see _MatchConfig's docstring). `tax_type`/
    `total_value`/`total_inclusive_value` feed the data-mismatch-only checks
    (2026-09-07 spec) and are None when not evaluated - import line items
    and any plant not yet wired for this (Vapi) simply never set them."""

    description: str
    qty: Decimal | None
    uom: str
    rate: Decimal | None
    value: Decimal | None
    tax_type: str | None = None
    total_value: Decimal | None = None
    total_inclusive_value: Decimal | None = None


def _uom_adjust(qty_a, uom_a, qty_b, uom_b, rate_a, rate_b):
    """Fix 2.C: converts both sides' qty/rate to a common base unit before
    scoring/diffing, so a PO in MT against a MIR in KG doesn't collapse a
    true match's qty/rate score to near-zero (or, symmetrically, look like a
    huge qty/rate discrepancy once matched). Rate is inverted relative to
    qty's factor - a rate quoted per MT converts to per-KG by dividing by
    1000, since price-per-unit scales inversely to the unit's own size.

    Returns (qty_a, qty_b, rate_a, rate_b, uom_mismatch):
      - Both units recognized, same family (e.g. KG vs MT): values converted
        to that family's base unit.
      - Either unit blank/unrecognized: values passed through unconverted -
        normalize_uom()'s own docstring explains why guessing is worse than
        not converting.
      - Both units recognized but different families (e.g. mass vs count):
        qty/rate returned as (None, None, None, None) so the caller scores/
        diffs them as "not comparable" rather than a nonsense percentage,
        and uom_mismatch=True flags this as a real, distinct problem."""
    family_a, factor_a = normalize_uom(uom_a)
    family_b, factor_b = normalize_uom(uom_b)
    if family_a is None or family_b is None:
        return qty_a, qty_b, rate_a, rate_b, False
    if family_a != family_b:
        return None, None, None, None, True
    qty_a_norm = qty_a * factor_a if qty_a is not None else None
    qty_b_norm = qty_b * factor_b if qty_b is not None else None
    rate_a_norm = rate_a / factor_a if rate_a is not None else None
    rate_b_norm = rate_b / factor_b if rate_b is not None else None
    return qty_a_norm, qty_b_norm, rate_a_norm, rate_b_norm, False


def _score_components(config: _MatchConfig, item: _Matchable, mir) -> tuple[list[tuple[Decimal, Decimal | None]], bool]:
    """Returns [(weight, score_or_None), ...] for the three financial-
    closeness factors used to pick the best candidate among an
    identification-passing pool (material is no longer one of these - see
    module docstring's 2026-09-07 redesign, it's a boolean identification
    gate now). A None score means that factor's data was missing on at
    least one side (fix 2.D) - excluded entirely from the weighted average
    rather than counted as a 0, so a sparse MIR row (blank rate, blank net)
    isn't punished as though it were a bad match. A uom_mismatch (fix 2.C)
    is different from missing data - qty/rate are real values that just
    can't be compared, so they're scored an explicit 0 (still counted, still
    a real signal), not excluded."""
    qty_a, qty_b, rate_a, rate_b, uom_mismatch = _uom_adjust(item.qty, item.uom, mir.qty, mir.uom, item.rate, mir.rate)
    if uom_mismatch:
        qty_score, rate_score = Decimal("0"), Decimal("0")
    else:
        qty_score = _closeness(qty_a, qty_b)
        rate_score = _closeness(rate_a, rate_b)
    value_score = _closeness(item.value, config.mir_value(mir))
    components = [
        (config.weight_qty, qty_score),
        (config.weight_rate, rate_score),
        (config.weight_value, value_score),
    ]
    return components, uom_mismatch


def _score(config: _MatchConfig, item: _Matchable, mir) -> tuple[Decimal, Decimal, bool]:
    """Returns (score, field_coverage, uom_mismatch) - purely a financial-
    closeness tie-breaker among candidates that already passed
    identification (see module docstring). field_coverage (fix 2.D) is the
    summed weight of factors actually present (0..1) - callers store it so
    the interface can distinguish a confident match from one resting on
    thin evidence."""
    components, uom_mismatch = _score_components(config, item, mir)
    present = [(w, s) for w, s in components if s is not None]
    coverage = sum((w for w, _ in present), Decimal("0"))
    if coverage == 0:
        return Decimal("0"), Decimal("0"), uom_mismatch
    score = sum((w * s for w, s in present), Decimal("0")) / coverage
    return score, coverage, uom_mismatch


def _scored_pairs_above_threshold(config: _MatchConfig, pool: list, item: _Matchable):
    """Yields (mir_entry, score, field_coverage) for every candidate in
    `pool` - `pool` is already identification-filtered (see
    _identification_pool()), so every entry here is a legitimate candidate;
    financial score is used only for ranking/tie-breaking and for
    run_full_match()'s exclusive-claim sort, not as an accept/reject
    threshold (see module docstring's 2026-09-07 redesign - an
    identification-passing candidate is never rejected for a low financial
    score)."""
    for mir in pool:
        score, coverage, _uom_mismatch = _score(config, item, mir)
        yield mir, score, coverage


def _best_candidate(config: _MatchConfig, pool: list, item: _Matchable):
    """Returns (best_entry, best_score, best_coverage) - the best-scoring
    (by financial closeness) candidate in `pool`, or (None, 0, 0) if the
    pool is empty. `pool` is already identification-filtered, so the first
    candidate is always accepted as a floor even at score 0 - there is no
    threshold to fail here, only ranking among already-identified rows."""
    best_entry, best_score, best_coverage = None, Decimal("-1"), Decimal("0")
    for mir in pool:
        score, coverage, _uom_mismatch = _score(config, item, mir)
        if score > best_score:
            best_entry, best_score, best_coverage = mir, score, coverage
    return best_entry, best_score, best_coverage


# ── Multi-shipment aggregation (fix 2.F) ────────────────────────────────────
# A PO line item's ordered quantity is sometimes fulfilled across several
# separate MIR rows (multiple truckloads/invoices for the same order)
# instead of one. Comparing the PO's full ordered qty against a single
# best-scoring MIR row then reports a false "quantity mismatch" even when
# the rows reconcile exactly in aggregate. Confirmed real case: HRS PO
# 3000001079 (Malaya Trade Impex, Natural Rubber ISNR 20) - 200,000 KG
# ordered, fulfilled across 6 separate MIR rows (34,000 x5 + 30,000 =
# 200,000 KG exactly), but the old single-row comparison picked one 34,000 KG
# row and reported an 83% "mismatch".
#
# Grouping key is vendor + material/PO-number identification (the pool
# passed in is already filtered to this - see _identification_pool()) PLUS
# rate: multiple MIR rows are only treated as this order's split shipments
# when their rate is close to the PO line's own rate, within
# _SHIPMENT_RATE_TOLERANCE_PCT. A genuinely different, concurrently-open PO
# to the same vendor for the same material usually differs in rate (price
# moves over time) - confirmed empirically against real HRS/Achhad/Vapi data
# this resolves the large majority of overlapping-PO cases. A vendor holding
# a flat/contract price for the same material across two real concurrent POs
# is a real, smaller residual risk this alone cannot separate (confirmed on
# real data too - e.g. a recurring Zinc Oxide order at a flat rate) -
# _SHIPMENT_GROUP_MAX_OVERSHOOT_PCT below is the deliberately blunt guard
# against that, not a full fix (a full fix would need a genuine
# ambiguous-group review queue, not attempted here).
_SHIPMENT_RATE_TOLERANCE_PCT = Decimal("2")

# Safety valve against the residual flat-rate-vendor ambiguity above: if the
# grouped rows' total quantity overshoots the PO's own ordered quantity by
# more than this margin, that's a signal the group likely mixes in a
# different, concurrently-open PO's deliveries to the same vendor/material/
# rate rather than a legitimate over-delivery on this one PO. When
# triggered, aggregation is skipped entirely and matching falls back to
# picking a single best row (today's behavior) for this item - this
# function never guesses which rows belong to which PO.
_SHIPMENT_GROUP_MAX_OVERSHOOT_PCT = Decimal("50")


class _ShipmentGroup(NamedTuple):
    """Result of a successful multi-shipment grouping (see
    _shipment_group()). `entries` is every MIR row folded into the
    aggregate (always 2+ - a single-row "group" isn't worth the label, see
    _shipment_group()'s own early return). `qty` is the summed quantity,
    already converted to the same base unit item.uom's family uses (i.e.
    directly comparable to the qty_a side _uom_adjust() would produce for
    this item). `rate` is the value-weighted average rate across the group
    (None if any member's value was unavailable to weight by). `value` is
    the summed net value (None under the same condition as `rate`)."""

    entries: list
    qty: Decimal
    rate: Decimal | None
    value: Decimal | None


def _shipment_group(config: _MatchConfig, item: _Matchable, pool: list) -> Optional[_ShipmentGroup]:
    """Finds the subset of `pool` (already identification-passed) whose rate
    is close enough to `item`'s own rate to plausibly be split shipments of
    the SAME order, then aggregates their quantity/value. Returns None when
    aggregation doesn't apply - fewer than 2 rate-compatible rows (nothing
    to aggregate; callers fall back to _best_candidate()'s single-row
    behavior), or the group's summed quantity overshoots the PO's ordered
    quantity by more than _SHIPMENT_GROUP_MAX_OVERSHOOT_PCT (see that
    constant's own comment)."""
    if item.rate is None or item.qty is None:
        return None

    same_rate: list[tuple[object, Decimal]] = []  # (mir, qty already in item's base unit)
    item_qty_base: Decimal | None = None
    for mir in pool:
        qty_a, qty_b, rate_a, rate_b, uom_mismatch = _uom_adjust(item.qty, item.uom, mir.qty, mir.uom, item.rate, mir.rate)
        if uom_mismatch or qty_a is None or qty_b is None or rate_a is None or rate_b is None:
            continue
        rate_diff = _diff_pct(rate_a, rate_b)
        if rate_diff is not None and rate_diff <= _SHIPMENT_RATE_TOLERANCE_PCT:
            same_rate.append((mir, qty_b))
            item_qty_base = qty_a  # identical every iteration once uom is recognized - item.uom alone determines it

    if len(same_rate) < 2 or item_qty_base is None:
        return None

    total_qty = Decimal("0")
    total_value = Decimal("0")
    value_complete = True
    for mir, qty_b in same_rate:
        total_qty += qty_b
        mir_value = config.mir_value(mir)
        if mir_value is None:
            value_complete = False
        else:
            total_value += mir_value

    overshoot_cap = item_qty_base * (Decimal("1") + _SHIPMENT_GROUP_MAX_OVERSHOOT_PCT / Decimal("100"))
    if total_qty > overshoot_cap:
        return None

    effective_rate = (total_value / total_qty) if value_complete and total_qty else None
    return _ShipmentGroup(
        entries=[m for m, _ in same_rate],
        qty=total_qty,
        rate=effective_rate,
        value=total_value if value_complete else None,
    )


def _import_rate_value_inr(import_line_item) -> tuple[Decimal | None, Decimal | None]:
    """Import line items are priced in the PO's own currency, but MIR's
    rate/taxable_value/net are always INR - comparing them raw understates a
    real match's score by ~90x (India's USD/INR rate), not a few percent, so
    this MUST run before scoring/diffing against MIR, not be optional.
      - rate: net_price * exchange_rate. Falls back to bare net_price if
        exchange_rate is missing.
      - value: total_inclusive_value (the real landed-in-India INR figure)
        when present, else net_value * exchange_rate."""
    exchange_rate = import_line_item.exchange_rate
    if import_line_item.net_price is None:
        rate_inr = None
    elif exchange_rate is not None:
        rate_inr = import_line_item.net_price * exchange_rate
    else:
        rate_inr = import_line_item.net_price

    if import_line_item.total_inclusive_value is not None:
        value_inr = import_line_item.total_inclusive_value
    elif import_line_item.net_value is not None and exchange_rate is not None:
        value_inr = import_line_item.net_value * exchange_rate
    else:
        value_inr = import_line_item.net_value

    return rate_inr, value_inr


def _import_total_value_inr(total_value, exchange_rate):
    """Converts an import PO's 'Total Value (As per PO)' figure - foreign-
    currency, PO-level, same single-line-item-PO-only caveat as domestic's
    own Total Value (see _po_matchable()'s docstring) - to INR using this
    specific line item's own exchange rate, same reasoning
    _import_rate_value_inr() already uses for rate/value. Falls back to the
    bare figure when exchange_rate is missing (2 of 37 real Vapi rows had
    none - the same real gap _import_rate_value_inr() already tolerates)."""
    if total_value is None:
        return None
    if exchange_rate is None:
        return total_value
    return total_value * exchange_rate


_SEVERITY_MATERIAL_PCT = Decimal("20")  # matches frontend/js/flags.js's rowTintClass() "severe" cutoff
_SEVERITY_MINOR_PCT = Decimal("5")  # matches rowTintClass()'s "moderate" cutoff


def _severity(uom_mismatch: bool, qty_diff, rate_diff, value_diff) -> str | None:
    """Fix 3.F: buckets a match's discrepancy magnitude into rounding/minor/
    material, reusing flags.js's existing 5%/20% cut points rather than
    inventing new ones. A uom_mismatch is always "material" regardless of
    the (None) qty/rate diffs - it's a real, serious problem, not a rounding
    artifact. None means no measurable discrepancy at all."""
    if uom_mismatch:
        return "material"
    diffs = [d for d in (qty_diff, rate_diff, value_diff) if d is not None]
    if not diffs or max(diffs) <= 0:
        return None
    max_diff = max(diffs)
    if max_diff >= _SEVERITY_MATERIAL_PCT:
        return "material"
    if max_diff >= _SEVERITY_MINOR_PCT:
        return "minor"
    return "rounding"


def _tax_type_mismatch(tax_type: str | None, mir) -> bool:
    """Structural GST-type check (2026-09-07 spec): if the PO says IGST, MIR
    should show IGST > 0 and CGST/SGST == 0, and vice versa for CGST+SGST -
    the two are mutually exclusive on a real invoice (an interstate purchase
    is never split IGST *and* CGST/SGST). Returns False (never a mismatch)
    when either side has nothing to check against - `tax_type` blank (not
    every plant/PO has this parsed yet) or MIR showing zero GST on all three
    columns (nothing recorded to compare) - this is a diagnostic signal for
    `data_mismatch`, not a hard gate, so it never blocks a match."""
    if not tax_type:
        return False
    normalized = tax_type.strip().upper()
    # HRS/Achhad's MIR column is `igst`; Vapi's is `igst_amt` (see
    # RTPVapiMIREntry) - read whichever exists rather than adding a new
    # per-plant config field for one differently-named column.
    igst = getattr(mir, "igst", None) or getattr(mir, "igst_amt", None) or Decimal("0")
    cgst = mir.cgst_amt or Decimal("0")
    sgst = mir.sgst_amt or Decimal("0")
    if igst == 0 and cgst == 0 and sgst == 0:
        return False
    has_igst_word = "IGST" in normalized
    has_cgst_sgst_word = "CGST" in normalized or "SGST" in normalized
    if has_igst_word and not has_cgst_sgst_word:
        return not (igst > 0 and cgst == 0 and sgst == 0)
    if has_cgst_sgst_word:
        return not (igst == 0 and (cgst > 0 or sgst > 0))
    return False  # tax_type text doesn't recognizably say either - don't guess


def _diffs_and_flag(
    config: _MatchConfig, item: _Matchable, mir, *,
    qty_override: Decimal | None = None, rate_override: Decimal | None = None, value_override: Decimal | None = None,
):
    """Returns (qty_diff_pct, rate_diff_pct, value_diff_pct, is_flagged,
    uom_mismatch, severity, qty_mismatched, rate_mismatched, data_mismatch,
    tax_type_mismatch, taxable_value_diff_pct, final_value_diff_pct,
    net_value_mismatched, taxable_value_mismatched, final_value_mismatched).

    The last three (added 2026-09-08, Data Quality Flags clarity pass) were
    already being computed here the whole time as value_flagged/
    taxable_value_flagged/final_value_flagged - they just got folded into
    the single data_mismatch bucket and discarded instead of being kept
    around for the caller to store individually. Surfacing them lets the
    frontend show *which* specific check failed (net value vs taxable value
    vs final/invoice value) instead of one opaque "data mismatch" flag -
    see flags.js's computePoFlags() for where these land as their own
    filterable Data Quality Flag categories.

    Identification/Financial-Check redesign (2026-09-07, see module
    docstring): only Qty and Rate produce a hard error now
    (qty_mismatched/rate_mismatched, still zero-tolerance per
    config.flag_diff_pct) - `is_flagged` means exactly "qty or rate
    mismatched", nothing else. Every other financial-check field - UOM
    family, Net-value (item.value vs config.mir_value), Taxable Value
    (item.total_value vs config.mir_taxable_value - only set by the caller
    for a single-line-item PO, since a multi-item PO's own "Total Value"
    column is a whole-PO aggregate repeated on every row, not a per-line
    figure, and comparing it against one MIR line's Taxable Value would be
    comparing the wrong things), GST-type structural consistency
    (_tax_type_mismatch), and Final/Invoice Value (item.total_inclusive_value
    vs config.mir_final_value, same single-line-item caveat) - all fold into
    one `data_mismatch` boolean instead.

    Fix 2.C: under a uom_mismatch, qty/rate diffs are None (not a nonsense
    percentage) - it drives data_mismatch instead (a mass-vs-count pair is a
    real, distinct problem, but the 2026-09-07 spec scopes it out of the
    two "real errors").

    Fix 3.F: every value-shaped comparison (Net/Taxable/Final) flags only
    when the ABSOLUTE currency difference exceeds config.value_flag_epsilon,
    since each is derived (qty x rate, plus tax-split rounding) and a rupee
    or two of rounding isn't a real discrepancy the way any nonzero qty/rate
    diff is.

    Fix 2.F (multi-shipment aggregation): qty_override/rate_override/
    value_override, when given by a caller that found a _shipment_group()
    for this item, replace the qty/rate/net-value comparison with the
    group's summed/weighted-average figures instead of `mir`'s own
    single-row fields - this is what actually fixes the false quantity/rate
    mismatch a multi-shipment PO would otherwise report. Every other
    financial-check field below (Taxable Value, Final/Invoice Value, GST
    type) still compares against `mir` alone - those are only ever
    meaningful against a single-line-item PO to begin with (see
    _po_matchable()'s docstring on why), and aggregating them is out of
    scope for this fix. uom_mismatch is forced False in this path -
    _shipment_group() already excludes any candidate that didn't cleanly
    UOM-convert against the PO line item, so there's nothing left here to
    flag as incompatible."""
    aggregated = qty_override is not None or rate_override is not None or value_override is not None
    qty_a, qty_b, rate_a, rate_b, uom_mismatch = _uom_adjust(item.qty, item.uom, mir.qty, mir.uom, item.rate, mir.rate)
    if aggregated:
        uom_mismatch = False
        qty_diff = _diff_pct(qty_a, qty_override) if qty_override is not None else None
        rate_diff = _diff_pct(rate_a, rate_override) if rate_override is not None else None
    elif uom_mismatch:
        qty_diff, rate_diff = None, None
    else:
        qty_diff = _diff_pct(qty_a, qty_b)
        rate_diff = _diff_pct(rate_a, rate_b)

    def _value_flagged(a, b):
        return a is not None and b is not None and abs(a - b) > config.value_flag_epsilon

    mir_value = value_override if value_override is not None else config.mir_value(mir)
    value_diff = _diff_pct(item.value, mir_value)
    value_flagged = _value_flagged(item.value, mir_value)

    taxable_value_diff = None
    taxable_value_flagged = False
    if item.total_value is not None:
        mir_taxable = config.mir_taxable_value(mir)
        taxable_value_diff = _diff_pct(item.total_value, mir_taxable)
        taxable_value_flagged = _value_flagged(item.total_value, mir_taxable)

    final_value_diff = None
    final_value_flagged = False
    if item.total_inclusive_value is not None:
        mir_final = config.mir_final_value(mir)
        final_value_diff = _diff_pct(item.total_inclusive_value, mir_final)
        final_value_flagged = _value_flagged(item.total_inclusive_value, mir_final)

    tax_type_flagged = _tax_type_mismatch(item.tax_type, mir)

    qty_mismatched = qty_diff is not None and qty_diff > config.flag_diff_pct
    rate_mismatched = rate_diff is not None and rate_diff > config.flag_diff_pct
    is_flagged = qty_mismatched or rate_mismatched
    data_mismatch = uom_mismatch or value_flagged or taxable_value_flagged or final_value_flagged or tax_type_flagged
    severity = _severity(uom_mismatch, qty_diff, rate_diff, value_diff)
    return (
        qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
        qty_mismatched, rate_mismatched, data_mismatch, tax_type_flagged,
        taxable_value_diff, final_value_diff,
        value_flagged, taxable_value_flagged, final_value_flagged,
    )


# ── Public API ───────────────────────────────────────────────────────────────
# Every function below writes to the DB (upsert-or-delete a match row) and is
# safe to call repeatedly.

def _po_matchable(po_line_item, is_single_item_po: bool) -> _Matchable:
    """Builds a domestic line item's _Matchable, including the
    identification/financial-check redesign's tax_type/total_value/
    total_inclusive_value fields (2026-09-07). total_value/
    total_inclusive_value are only set for a single-line-item PO - the PO
    CSV's "Total Value"/"Total Inclusive Value" columns are a whole-PO
    aggregate repeated on every row (confirmed against real multi-item POs),
    not a genuine per-line figure, so comparing them against one MIR line's
    Taxable/Final Value would compare the wrong things for a multi-item PO."""
    po = po_line_item.purchase_order
    return _Matchable(
        po_line_item.description, po_line_item.qty, po_line_item.uom, po_line_item.net_price, po_line_item.net_value,
        tax_type=po.tax_type or None,
        total_value=po.total_value if is_single_item_po else None,
        total_inclusive_value=po.total_inclusive_value if is_single_item_po else None,
    )


def _import_matchable(config: _MatchConfig, import_line_item, is_single_item_po: bool) -> _Matchable:
    """Builds an import line item's _Matchable. Only plants opted into the
    imports identification/financial-check redesign (config.
    import_extended_fields - HRS/Achhad, see _MatchConfig's docstring) get
    tax_type/total_value/total_inclusive_value populated; every other plant
    (Vapi) keeps the original 4-field shape, since its import match model
    has no columns to store the extra fields in yet.

    tax_type/total_inclusive_value live on the import LINE ITEM already
    (unlike domestic, where they're PO-level) - total_inclusive_value in
    particular is already a genuine per-line INR figure (the CSV's own
    'Total Inclusive Value (Final Bill Paid...)' column), so unlike
    domestic's total_inclusive_value it needs neither the single-line-item-
    PO caveat nor a currency conversion. total_value ('Total Value (As per
    PO)') is still PO-level and in the PO's own foreign currency, so it
    keeps both: only set for a single-line-item PO (same reasoning as
    _po_matchable()), and converted to INR via this line's own exchange
    rate (_import_total_value_inr()) since imports - unlike domestic - are
    priced in the PO's own currency, not INR."""
    po = import_line_item.purchase_order
    rate_inr, value_inr = _import_rate_value_inr(import_line_item)
    if not config.import_extended_fields:
        return _Matchable(import_line_item.description, import_line_item.qty_as_per_boe, import_line_item.uom, rate_inr, value_inr)
    total_value_inr = (
        _import_total_value_inr(po.total_value, import_line_item.exchange_rate) if is_single_item_po else None
    )
    return _Matchable(
        import_line_item.description, import_line_item.qty_as_per_boe, import_line_item.uom, rate_inr, value_inr,
        tax_type=import_line_item.tax_type or None,
        total_value=total_value_inr,
        total_inclusive_value=import_line_item.total_inclusive_value,
    )


def _pick_match(config: _MatchConfig, item: _Matchable, pool: list):
    """Picks what to match a line item against, preferring a multi-shipment
    group (_shipment_group()) over a single best row when one applies.
    Returns (primary_entry, score, coverage, group) - `group` is None for an
    ordinary single-row match (unchanged pre-2.F behavior), or the
    _ShipmentGroup when aggregation applies. `primary_entry` is always a
    single MIR row (the group's own best-scoring member, when grouped) -
    used as the stored mir_entry FK and for every financial-check field
    _diffs_and_flag() doesn't aggregate (see that function's own docstring
    on fix 2.F)."""
    group = _shipment_group(config, item, pool)
    if group is not None:
        primary, score, coverage = _best_candidate(config, group.entries, item)
        return primary, score, coverage, group
    best_entry, best_score, coverage = _best_candidate(config, pool, item)
    return best_entry, best_score, coverage, None


def match_po_mir_line_item(config: _MatchConfig, po_line_item):
    """Finds the best MIR match for one domestic PO line item, in isolation
    (no knowledge of other line items' claims - see module docstring), and
    upserts config.po_mir_match_model, or deletes any existing match if no
    candidate passes identification ("PO Not Found" - see module docstring).
    Returns the resulting match (or None)."""
    po = po_line_item.purchase_order
    is_single_item_po = po.items.count() == 1
    item = _po_matchable(po_line_item, is_single_item_po)
    candidates = _candidate_mir_entries(config, po.vendor_name)
    pool, id_flags = _identification_pool(config, candidates, item, po.po_number)
    best_entry, best_score, coverage, group = _pick_match(config, item, pool)

    if best_entry is None:
        config.po_mir_match_model.objects.filter(po_line_item=po_line_item).delete()
        return None

    material_matched, po_number_matched = id_flags[best_entry.id]
    tier = TIER_PO_NUMBER if po_number_matched else TIER_MATERIAL
    qty_override, rate_override, value_override = (group.qty, group.rate, group.value) if group is not None else (None, None, None)
    (
        qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
        qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
        taxable_value_diff, final_value_diff,
        net_value_mismatched, taxable_value_mismatched, final_value_mismatched,
    ) = _diffs_and_flag(config, item, best_entry, qty_override=qty_override, rate_override=rate_override, value_override=value_override)
    match, _ = config.po_mir_match_model.objects.update_or_create(
        po_line_item=po_line_item,
        defaults=dict(
            mir_entry=best_entry,
            tier=tier,
            match_score=best_score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
            uom_mismatch=uom_mismatch,
            field_coverage=coverage.quantize(Decimal("0.01")),
            severity=severity,
            material_matched=material_matched,
            po_number_matched=po_number_matched,
            qty_mismatched=qty_mismatched,
            rate_mismatched=rate_mismatched,
            data_mismatch=data_mismatch,
            tax_type_mismatch=tax_type_mismatch,
            taxable_value_diff_pct=taxable_value_diff,
            final_value_diff_pct=final_value_diff,
            net_value_mismatched=net_value_mismatched,
            taxable_value_mismatched=taxable_value_mismatched,
            final_value_mismatched=final_value_mismatched,
        ),
    )
    return match


def match_import_po_mir_line_item(config: _MatchConfig, import_line_item):
    """Same shape as match_po_mir_line_item() - the only real differences
    are the qty field (qty_as_per_boe, not qty_as_per_po - see this
    function's own module docstring on the two separate import qty checks:
    PO-vs-BOE is a different, already-existing check in
    apps/services/import_flags.py, unaffected by this function; this one
    is BOE-vs-MIR), the currency conversion _import_matchable()/
    _import_rate_value_inr() do before scoring, and the match model class.
    Matches against the same MIR table domestic matching uses - MIR is a
    shared Drive file across domestic and import purchases.

    Only plants with config.import_extended_fields=True (HRS/Achhad) get the
    same tax_type/total_value/total_inclusive_value data-mismatch treatment
    domestic line items already have, and only those plants' defaults dict
    below gets the extended identification/data-mismatch columns written -
    Vapi (import_extended_fields=False, see matching_vapi.py) keeps the
    original 4-field _Matchable shape and the original short defaults dict
    unchanged, since RTPVapiImportPOMirMatch has no columns for the rest."""
    po = import_line_item.purchase_order
    is_single_item_po = po.items.count() == 1
    item = _import_matchable(config, import_line_item, is_single_item_po)
    candidates = _candidate_mir_entries(config, po.vendor_name)
    pool, id_flags = _identification_pool(config, candidates, item, po.po_number)
    best_entry, best_score, coverage, group = _pick_match(config, item, pool)

    if best_entry is None:
        config.import_po_mir_match_model.objects.filter(po_line_item=import_line_item).delete()
        return None

    material_matched, po_number_matched = id_flags[best_entry.id]
    tier = TIER_PO_NUMBER if po_number_matched else TIER_MATERIAL
    qty_override, rate_override, value_override = (group.qty, group.rate, group.value) if group is not None else (None, None, None)
    (
        qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
        qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
        taxable_value_diff, final_value_diff,
        net_value_mismatched, taxable_value_mismatched, final_value_mismatched,
    ) = _diffs_and_flag(config, item, best_entry, qty_override=qty_override, rate_override=rate_override, value_override=value_override)
    defaults = dict(
        mir_entry=best_entry,
        tier=tier,
        match_score=best_score.quantize(Decimal("0.0001")),
        qty_diff_pct=qty_diff,
        rate_diff_pct=rate_diff,
        value_diff_pct=value_diff,
        is_flagged=is_flagged,
        uom_mismatch=uom_mismatch,
        field_coverage=coverage.quantize(Decimal("0.01")),
        severity=severity,
    )
    if config.import_extended_fields:
        defaults.update(dict(
            material_matched=material_matched,
            po_number_matched=po_number_matched,
            qty_mismatched=qty_mismatched,
            rate_mismatched=rate_mismatched,
            data_mismatch=data_mismatch,
            tax_type_mismatch=tax_type_mismatch,
            taxable_value_diff_pct=taxable_value_diff,
            final_value_diff_pct=final_value_diff,
            net_value_mismatched=net_value_mismatched,
            taxable_value_mismatched=taxable_value_mismatched,
            final_value_mismatched=final_value_mismatched,
        ))
    match, _ = config.import_po_mir_match_model.objects.update_or_create(
        po_line_item=import_line_item,
        defaults=defaults,
    )
    return match


def match_mir_entry_stock(config: _MatchConfig, mir_entry):
    """Finds every Stock lot that identifies against one MIR entry and
    upserts config.mir_stock_match_model for each - a many-to-many
    relationship on purpose (the same material/vendor is received into stock
    across multiple lots over time), unaffected by fix 2.B's PO<->MIR
    exclusivity. Vendor gating is skipped entirely when
    config.stock_vendor_field is None (Achhad's Stock sheet has no vendor
    column - see module docstring).

    Identification/Financial-Check extension (2026-09-08, HRS/Achhad/Vapi -
    config.stock_extended_fields gates this, see this function's own
    per-plant callers): unlike PO<->MIR, date is NEVER an alternative
    identification path here - Material description (normalized exact
    match, same comparison as before this extension existed) stays the
    sole, mandatory identification factor for every plant, vendor-gated
    when config.stock_vendor_field is set (HRS/Vapi) or not (Achhad).

    An earlier version of this function let Rec. DT. (exact date match
    against MIR's own Date) stand in for material as an identification
    path when a vendor gate was present, mirroring PO<->MIR's "vendor
    mandatory plus one of the other two" shape. Confirmed unsound against
    real data from ALL THREE plants this session, not just the vendor-less
    Achhad case it was first caught on: a single vendor commonly delivers
    several genuinely different materials on the same day (one truck, one
    invoice, multiple line items) - e.g. real HRS pairs 'China Clay Powder'
    <-> 'VULKACIT MBTS' and 'NBR 3345' <-> 'NBR 2675' (different grades
    cross-matched), and real Vapi pairs '8MPA RECLAIM RUBBER' <-> 'GREASE
    EP 1' - both wrongly matched purely because vendor+date coincided,
    despite having nothing to do with each other. Vendor anchoring alone
    does not fix this; only material does. Reverted before this ever
    shipped as the general rule - date_matched is still computed and still
    gates the Qty/Value checks below (requiring BOTH material_matched AND
    date_matched, not date alone), it just never admits a candidate into
    the identification pool by itself, for any plant.

    Basic Rate is a hard "Mismatched" error (rate_mismatched, zero
    tolerance, same policy as PO<->MIR) - but, like Qty and Value, ONLY when
    date_matched also fired alongside material_matched. Confirmed against
    real data this session why this gating matters for rate specifically,
    not just qty/value: MIR is a full delivery LOG (one row per historical
    delivery, every date it ever happened), while Stock is a current-
    snapshot table (one row per material, whose Rec. DT. reflects only the
    MOST RECENT receipt) - matching on material alone (as identification
    requires) pairs an MIR row against a Stock row that may represent a
    receipt months apart from the one that MIR row actually recorded.
    Real HRS example: MIR shows Sulphur Powder at Rs.105.50 (delivered
    2026-05-29), Stock's current lot shows Rs.128.00 (last received
    2026-08-13, 76 days later) - a 21% "rate mismatch" that's ordinary
    commodity price drift over 2.5 months, not a data error. Measured
    directly: ~97-98% of rate_mismatched pairs (both HRS and Achhad) had
    date_matched=False, averaging an 85-93 day gap between the two dates
    being compared (max 227-241 days) - confirming most of what
    zero-tolerance rate flagging was catching was this artifact, not real
    discrepancies. Restricting rate_diff_pct/rate_mismatched to the
    date_matched case only compares a rate against the one Stock snapshot
    we can actually confirm is the same delivery event that MIR row
    recorded.

    Qty (Stock's REC vs MIR's Qty) is a genuinely different shape from
    PO<->MIR's qty check, not just copied over: REC reads 0 for ~90% of
    real HRS lots (confirmed against the live file this session) - a
    running-total artifact, not a real "nothing received" signal - so it's
    only compared when REC is nonzero AT ALL, on top of the same
    date_matched requirement rate now shares.

    Value is not Stock's own `value` column - confirmed against the live
    file this session that column is `Basic Rate x Today's Stock` (the
    lot's current running BALANCE value), not the value of any one receipt,
    so comparing it against one MIR line's Net Value would compare
    unrelated numbers (this produced a ~99% false data_mismatch rate in an
    earlier version of this function, caught before shipping). Instead,
    under the exact same date_matched-and-REC-nonzero condition qty uses,
    Value here means the DERIVED value of that specific receipt (REC x
    Basic Rate) compared against MIR's own net-value (config.mir_value) -
    the one figure that's actually comparable to a single MIR line."""
    mir_material = normalize_material(mir_entry.material_description)
    mir_vendor = normalize_vendor(mir_entry.party_name) if config.stock_vendor_field else None
    if not mir_material or (config.stock_vendor_field and not mir_vendor):
        return []

    lots = config.stock_lot_model.objects.filter(is_active=True).exclude(description="")
    if config.stock_vendor_field:
        lots = lots.exclude(**{config.stock_vendor_field: ""})

    candidates = []  # (lot, material_matched, date_matched)
    for lot in lots:
        if config.stock_vendor_field and not _vendor_matches(normalize_vendor(getattr(lot, config.stock_vendor_field)), mir_vendor):
            continue
        material_matched = normalize_material(lot.description) == mir_material
        if not material_matched:
            # Material is the sole, mandatory identification factor for
            # every plant - see this function's own docstring for why date
            # is never allowed to substitute for it here, unlike PO<->MIR.
            continue
        date_matched = (
            config.stock_extended_fields
            and mir_entry.mir_date is not None
            and lot.received_date is not None
            and mir_entry.mir_date == lot.received_date
        )
        candidates.append((lot, material_matched, date_matched))

    matches = []
    matched_lot_ids = set()
    for lot, material_matched, date_matched in candidates:
        if config.stock_extended_fields:
            rate_diff = None
            rate_mismatched = False
            qty_diff = None
            qty_mismatched = False
            value_diff = None
            value_flagged = False
            uom_mismatch = False
            if material_matched and date_matched:
                # Fix (found during a full-codebase audit, 2026-09-10): this
                # branch used to compare mir_entry.rate/qty directly against
                # the Stock lot's own rate/received with no unit conversion
                # at all, unlike PO<->MIR (_score_components/_diffs_and_flag
                # above) which always runs both sides through _uom_adjust()
                # first. A material logged in MIR as MT against a Stock lot
                # recorded in KG would report a ~1000x "rate mismatch" that's
                # actually just a unit-mismatch artifact, not a real
                # discrepancy. Normalizing here the same way PO<->MIR already
                # does closes that gap - uom_mismatch=True (different,
                # non-convertible unit families) skips the qty/rate
                # comparisons entirely rather than reporting a nonsense
                # percentage, same convention as _diffs_and_flag's own
                # uom_mismatch handling. Value is a currency amount, not a
                # per-unit figure, so it needs no unit conversion here.
                lot_uom = getattr(lot, "uom", None)
                mir_qty_adj, lot_qty_adj, mir_rate_adj, lot_rate_adj, uom_mismatch = _uom_adjust(
                    mir_entry.qty, mir_entry.uom, lot.received, lot_uom, mir_entry.rate, getattr(lot, config.stock_rate_field),
                )
                # Rate is only meaningful against the ONE Stock snapshot we
                # can confirm represents the same delivery MIR recorded -
                # see this function's own docstring for why comparing it
                # against every material-matched lot (regardless of date)
                # mostly measured commodity price drift over months, not
                # real discrepancies.
                if not uom_mismatch:
                    rate_diff = _diff_pct(mir_rate_adj, lot_rate_adj)
                    rate_mismatched = rate_diff is not None and rate_diff > config.flag_diff_pct

                    if lot.received:
                        qty_diff = _diff_pct(mir_qty_adj, lot_qty_adj)
                        qty_mismatched = qty_diff is not None and qty_diff > config.flag_diff_pct

                        lot_rate = getattr(lot, config.stock_rate_field)
                        received_value = lot.received * lot_rate if lot_rate is not None else None
                        mir_value = config.mir_value(mir_entry)
                        value_diff = _diff_pct(mir_value, received_value)
                        value_flagged = (
                            mir_value is not None and received_value is not None
                            and abs(mir_value - received_value) > config.value_flag_epsilon
                        )
            defaults = dict(
                qty_diff_pct=qty_diff,
                rate_diff_pct=rate_diff,
                value_diff_pct=value_diff,
                is_flagged=qty_mismatched or rate_mismatched,
                material_matched=material_matched,
                date_matched=date_matched,
                qty_mismatched=qty_mismatched,
                rate_mismatched=rate_mismatched,
                data_mismatch=value_flagged or uom_mismatch,
                uom_mismatch=uom_mismatch,
            )
        else:
            # Unchanged pre-extension shape (no plant left uses this branch
            # today, kept for a hypothetical future plant that hasn't been
            # extended yet) - qty was never comparable, rate had no
            # date-confirmation requirement at all.
            rate_diff = _diff_pct(mir_entry.rate, getattr(lot, config.stock_rate_field))
            rate_mismatched = rate_diff is not None and rate_diff > config.flag_diff_pct
            defaults = dict(qty_diff_pct=None, rate_diff_pct=rate_diff, is_flagged=rate_mismatched)

        match, _ = config.mir_stock_match_model.objects.update_or_create(
            mir_entry=mir_entry,
            stock_lot=lot,
            defaults=defaults,
        )
        matches.append(match)
        matched_lot_ids.add(lot.id)

    config.mir_stock_match_model.objects.filter(mir_entry=mir_entry).exclude(stock_lot_id__in=matched_lot_ids).delete()
    return matches


@transaction.atomic
def run_full_match(config: _MatchConfig) -> dict:
    """Re-runs every matching pass for one plant - domestic PO line items
    AND import PO line items (both against the same shared MIR table, so
    fix 2.B's exclusive claiming considers them together), plus MIR<->Stock.

    Fix 2.B: builds every (line item, MIR) pair scoring above threshold
    across every domestic and import line item, sorts by score descending,
    then assigns greedily - a MIR row already claimed by a higher-scoring
    pair is skipped, so a line item can fall through to its own next-best
    candidate rather than losing its match entirely. Caps at exactly one
    claim per MIR row per run (confirmed safe against real data - see
    matching_core.py's module docstring).

    Fix 2.F (multi-shipment aggregation): when a line item has a valid
    _shipment_group(), that item contributes exactly ONE pair to `pairs` -
    not one per pool row - representing the whole group at once, scored by
    its own best-scoring (primary) member. Winning that pair claims every
    MIR row in the group together, atomically, rather than one row at a
    time - a group can never be half-claimed by this item and half up for
    grabs by another. An item without a valid group is entirely unaffected,
    same per-row pairs as before fix 2.F existed.

    Safe to call repeatedly (idempotent upserts) - intended to run after
    each sync, and synchronously after any "Edit Everywhere" field edit that
    feeds matching.
    """
    po_items = list(config.po_item_model.objects.select_related("purchase_order").all())
    import_items = list(config.import_item_model.objects.select_related("purchase_order").all())

    # Item counts per PO, computed from the already-fetched po_items list
    # rather than one .count() query per line item - feeds _po_matchable()'s
    # single-line-item-PO check (see that function's docstring for why it
    # matters) with no N+1 query cost.
    item_counts: dict[int, int] = {}
    for item in po_items:
        item_counts[item.purchase_order_id] = item_counts.get(item.purchase_order_id, 0) + 1

    # Same reasoning, for import items - feeds _import_matchable()'s own
    # single-line-item-PO check (only relevant when config.
    # import_extended_fields is True; harmless, unused work otherwise).
    import_item_counts: dict[int, int] = {}
    for item in import_items:
        import_item_counts[item.purchase_order_id] = import_item_counts.get(item.purchase_order_id, 0) + 1

    items_by_key: dict[tuple[str, int], _Matchable] = {}
    id_flags_by_key: dict[tuple[str, int, int], tuple[bool, bool]] = {}  # (kind, item_id, mir_id) -> (material_matched, po_number_matched)
    pairs = []  # (score, "po"|"import", item_key, mir_entry (primary), coverage, group_or_None)
    for item in po_items:
        po = item.purchase_order
        matchable = _po_matchable(item, item_counts[item.purchase_order_id] == 1)
        items_by_key[("po", item.id)] = matchable
        candidates = _candidate_mir_entries(config, po.vendor_name)
        pool, id_flags = _identification_pool(config, candidates, matchable, po.po_number)
        group = _shipment_group(config, matchable, pool)
        if group is not None:
            primary, score, coverage = _best_candidate(config, group.entries, matchable)
            id_flags_by_key[("po", item.id, primary.id)] = id_flags[primary.id]
            pairs.append((score, "po", item.id, primary, coverage, group))
        else:
            for mir, score, coverage in _scored_pairs_above_threshold(config, pool, matchable):
                id_flags_by_key[("po", item.id, mir.id)] = id_flags[mir.id]
                pairs.append((score, "po", item.id, mir, coverage, None))

    for item in import_items:
        po = item.purchase_order
        matchable = _import_matchable(config, item, import_item_counts[item.purchase_order_id] == 1)
        items_by_key[("import", item.id)] = matchable
        candidates = _candidate_mir_entries(config, po.vendor_name)
        pool, id_flags = _identification_pool(config, candidates, matchable, po.po_number)
        group = _shipment_group(config, matchable, pool)
        if group is not None:
            primary, score, coverage = _best_candidate(config, group.entries, matchable)
            id_flags_by_key[("import", item.id, primary.id)] = id_flags[primary.id]
            pairs.append((score, "import", item.id, primary, coverage, group))
        else:
            for mir, score, coverage in _scored_pairs_above_threshold(config, pool, matchable):
                id_flags_by_key[("import", item.id, mir.id)] = id_flags[mir.id]
                pairs.append((score, "import", item.id, mir, coverage, None))

    pairs.sort(key=lambda p: p[0], reverse=True)
    claimed_mir_ids: set[int] = set()
    assigned: dict[tuple[str, int], tuple] = {}  # (kind, item.id) -> (mir, score, coverage, group_or_None)
    for score, kind, item_id, mir, coverage, group in pairs:
        key = (kind, item_id)
        # A grouped pair claims every member MIR row atomically - it can
        # never be half-claimed by this item and half still up for grabs by
        # another (see this function's own docstring, fix 2.F).
        member_ids = {m.id for m in group.entries} if group is not None else {mir.id}
        if key in assigned or (member_ids & claimed_mir_ids):
            continue
        assigned[key] = (mir, score, coverage, group)
        claimed_mir_ids.update(member_ids)

    po_matched = 0
    for item in po_items:
        result = assigned.get(("po", item.id))
        if result is None:
            config.po_mir_match_model.objects.filter(po_line_item=item).delete()
            continue
        mir, score, coverage, group = result
        matchable = items_by_key[("po", item.id)]
        material_matched, po_number_matched = id_flags_by_key[("po", item.id, mir.id)]
        tier = TIER_PO_NUMBER if po_number_matched else TIER_MATERIAL
        qty_override, rate_override, value_override = (group.qty, group.rate, group.value) if group is not None else (None, None, None)
        (
            qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
            qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
            taxable_value_diff, final_value_diff,
            net_value_mismatched, taxable_value_mismatched, final_value_mismatched,
        ) = _diffs_and_flag(config, matchable, mir, qty_override=qty_override, rate_override=rate_override, value_override=value_override)
        config.po_mir_match_model.objects.update_or_create(
            po_line_item=item,
            defaults=dict(
                mir_entry=mir,
                tier=tier,
                match_score=score.quantize(Decimal("0.0001")),
                qty_diff_pct=qty_diff,
                rate_diff_pct=rate_diff,
                value_diff_pct=value_diff,
                is_flagged=is_flagged,
                uom_mismatch=uom_mismatch,
                field_coverage=coverage.quantize(Decimal("0.01")),
                severity=severity,
                material_matched=material_matched,
                po_number_matched=po_number_matched,
                qty_mismatched=qty_mismatched,
                rate_mismatched=rate_mismatched,
                data_mismatch=data_mismatch,
                tax_type_mismatch=tax_type_mismatch,
                taxable_value_diff_pct=taxable_value_diff,
                final_value_diff_pct=final_value_diff,
                net_value_mismatched=net_value_mismatched,
                taxable_value_mismatched=taxable_value_mismatched,
                final_value_mismatched=final_value_mismatched,
            ),
        )
        po_matched += 1

    import_po_matched = 0
    for item in import_items:
        result = assigned.get(("import", item.id))
        if result is None:
            config.import_po_mir_match_model.objects.filter(po_line_item=item).delete()
            continue
        mir, score, coverage, group = result
        matchable = items_by_key[("import", item.id)]
        material_matched, po_number_matched = id_flags_by_key[("import", item.id, mir.id)]
        tier = TIER_PO_NUMBER if po_number_matched else TIER_MATERIAL
        qty_override, rate_override, value_override = (group.qty, group.rate, group.value) if group is not None else (None, None, None)
        (
            qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
            qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
            taxable_value_diff, final_value_diff,
            net_value_mismatched, taxable_value_mismatched, final_value_mismatched,
        ) = _diffs_and_flag(config, matchable, mir, qty_override=qty_override, rate_override=rate_override, value_override=value_override)
        defaults = dict(
            mir_entry=mir,
            tier=tier,
            match_score=score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
            uom_mismatch=uom_mismatch,
            field_coverage=coverage.quantize(Decimal("0.01")),
            severity=severity,
        )
        if config.import_extended_fields:
            defaults.update(dict(
                material_matched=material_matched,
                po_number_matched=po_number_matched,
                qty_mismatched=qty_mismatched,
                rate_mismatched=rate_mismatched,
                data_mismatch=data_mismatch,
                tax_type_mismatch=tax_type_mismatch,
                taxable_value_diff_pct=taxable_value_diff,
                final_value_diff_pct=final_value_diff,
                net_value_mismatched=net_value_mismatched,
                taxable_value_mismatched=taxable_value_mismatched,
                final_value_mismatched=final_value_mismatched,
            ))
        config.import_po_mir_match_model.objects.update_or_create(
            po_line_item=item,
            defaults=defaults,
        )
        import_po_matched += 1

    # Deactivated MIR entries are skipped by the loop below, so
    # match_mir_entry_stock() never runs for them to clean up its own match
    # rows - drop those explicitly instead of leaving them to reference a
    # now-inactive entry forever.
    config.mir_stock_match_model.objects.filter(mir_entry__is_active=False).delete()

    mir_matched = 0
    for entry in config.mir_model.objects.filter(is_active=True):
        if match_mir_entry_stock(config, entry):
            mir_matched += 1

    return {
        "po_line_items_matched": po_matched,
        "import_po_line_items_matched": import_po_matched,
        "mir_entries_stock_matched": mir_matched,
        "ran_at": timezone.now(),
    }
