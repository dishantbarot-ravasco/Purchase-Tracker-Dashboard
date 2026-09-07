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
scoring weights, thresholds, the tier-1/tier-2 algorithm, exclusive MIR
claiming - is one implementation, not three.

Vendor is always a hard gate, never scored - two records for different
vendors are never candidates for each other, no matter how well material/
amount line up.

PO <-> MIR, per line item:
  Tier 1 - PO_NUMBER: mir.po_number_raw resolves to this exact PO (see
    _po_number_matches). A PO-number hit NARROWS the candidate pool to every
    MIR row sharing that PO number - it does not select a winner outright.
    The same weighted scoring loop (Tier 2's algorithm) then runs over that
    narrowed pool, with a +0.25 score bonus (capped at 1) rather than a
    hardcoded 1.0000, and MATCH_THRESHOLD still applies - a PO-number hit
    against a completely unrelated material still fails to match. (Match
    Accuracy Programme doc 03, fix 2.A - the previous version used
    next(...) to take the *first* PO-number hit unconditionally, which
    collapsed every line item on a multi-line PO onto one MIR row.)
  Tier 2 - WEIGHTED, scored against every vendor-gated MIR candidate:
    material description token overlap  30%
    qty closeness                       20%
    rate closeness                      20%
    total/final value closeness         30%
  The best-scoring candidate above MATCH_THRESHOLD wins; below that, the
  line item is left unmatched rather than forced onto a poor candidate.

Exclusive MIR claiming (fix 2.B): confirmed against real data (31-39% of
current matches shared a MIR row across multiple line items, traced back to
the old tier-1 next() bug and to cross-PO Tier-2 collisions - not legitimate
partial receipts; no schema field or source-data pattern supports one MIR
row genuinely satisfying more than one PO line item) that a MIR row should
be claimed by at most one line item per run_full_match() pass. run_full_match()
scores every (line item, MIR) pair above threshold across every domestic AND
import line item at once (they share one MIR table), sorts every pair by
score descending, then assigns greedily - a MIR row already claimed by a
higher-scoring pair is skipped, so a line item can fall through to its own
next-best candidate rather than losing its match entirely.
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
TIER_WEIGHTED = "weighted"

# *_diff_pct columns are DecimalField(max_digits=6, decimal_places=2) - see
# CLAUDE.md's "*_diff_pct columns need a clamp" note.
_MAX_DIFF_PCT = Decimal("9999.99")

# Tier-1's score bonus once a PO-number hit narrows the pool (fix 2.A) -
# never a hardcoded 1.0000, but still a real advantage over an unnarrowed
# weighted match, capped so it can't push a score above the valid 0..1 range.
_TIER1_SCORE_BONUS = Decimal("0.25")


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
    # percentage of a tiny value can still be under a rupee.
    value_flag_epsilon: Decimal
    weight_material: Decimal
    weight_qty: Decimal
    weight_rate: Decimal
    weight_value: Decimal

    # MIR entry -> its pre-tax value for value-closeness scoring. HRS/Achhad
    # fall back to `net` when `taxable_value` is blank; Vapi has no `net`
    # field to fall back to at all.
    mir_value: Callable[[object], object]

    stock_rate_field: str  # "basic_rate" (HRS/Vapi) or "rate" (Achhad)
    stock_vendor_field: Optional[str] = None  # "party_name"/"supplier_name", or None (Achhad has no vendor column)


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


def _tier1_pool(candidates: list, po_number: str) -> tuple[list, str]:
    """Fix 2.A: a PO-number hit narrows the candidate pool, it doesn't pick
    a winner. Returns (pool, tier) - the narrowed pool tagged PO_NUMBER if
    any hit exists, otherwise the full candidate set tagged WEIGHTED."""
    tier1 = [c for c in candidates if _po_number_matches(po_number, c.po_number_raw)]
    return (tier1, TIER_PO_NUMBER) if tier1 else (candidates, TIER_WEIGHTED)


class _Matchable(NamedTuple):
    """One side's worth of fields the scoring loop needs, for either a
    domestic PO line item or an import line item (with its rate/value
    already currency-converted - see _import_rate_value_inr())."""

    description: str
    qty: Decimal | None
    uom: str
    rate: Decimal | None
    value: Decimal | None


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
    """Returns [(weight, score_or_None), ...] for the four scoring factors.
    A None score means that factor's data was missing on at least one side
    (fix 2.D) - excluded entirely from the weighted average rather than
    counted as a 0, so a sparse MIR row (blank rate, blank taxable value)
    isn't punished as though it were a bad match. A uom_mismatch (fix 2.C)
    is different from missing data - qty/rate are real values that just
    can't be compared, so they're scored an explicit 0 (still counted, still
    a real signal), not excluded."""
    material_score = _token_overlap(item.description, mir.material_description)
    qty_a, qty_b, rate_a, rate_b, uom_mismatch = _uom_adjust(item.qty, item.uom, mir.qty, mir.uom, item.rate, mir.rate)
    if uom_mismatch:
        qty_score, rate_score = Decimal("0"), Decimal("0")
    else:
        qty_score = _closeness(qty_a, qty_b)
        rate_score = _closeness(rate_a, rate_b)
    value_score = _closeness(item.value, config.mir_value(mir))
    components = [
        (config.weight_material, material_score),
        (config.weight_qty, qty_score),
        (config.weight_rate, rate_score),
        (config.weight_value, value_score),
    ]
    return components, uom_mismatch


def _score(config: _MatchConfig, item: _Matchable, mir, tier: str) -> tuple[Decimal, Decimal, bool]:
    """Returns (score, field_coverage, uom_mismatch). field_coverage (fix
    2.D) is the summed weight of factors actually present (0..1) - callers
    store it so the interface can distinguish a confident match from one
    resting on thin evidence, and so report_match_accuracy can split
    accuracy by coverage band."""
    components, uom_mismatch = _score_components(config, item, mir)
    present = [(w, s) for w, s in components if s is not None]
    coverage = sum((w for w, _ in present), Decimal("0"))
    if coverage == 0:
        return Decimal("0"), Decimal("0"), uom_mismatch
    score = sum((w * s for w, s in present), Decimal("0")) / coverage
    if tier == TIER_PO_NUMBER:
        score = min(Decimal("1"), score + _TIER1_SCORE_BONUS)
    return score, coverage, uom_mismatch


def _scored_pairs_above_threshold(config: _MatchConfig, pool: list, tier: str, item: _Matchable):
    """Yields (mir_entry, score, tier, field_coverage) for every candidate in
    `pool` whose score clears MATCH_THRESHOLD - used by run_full_match() to
    build the global pool of above-threshold pairs that fix 2.B's
    exclusive-claim pass sorts and assigns from."""
    for mir in pool:
        score, coverage, _uom_mismatch = _score(config, item, mir, tier)
        if score >= config.match_threshold:
            yield mir, score, tier, coverage


def _best_candidate(config: _MatchConfig, pool: list, tier: str, item: _Matchable):
    """Returns (best_entry, best_score, best_tier, best_coverage) - the best-
    scoring candidate in `pool`, or (None, 0, None, 0) if the pool is empty."""
    best_entry, best_score, best_tier, best_coverage = None, Decimal("0"), None, Decimal("0")
    for mir in pool:
        score, coverage, _uom_mismatch = _score(config, item, mir, tier)
        if score > best_score:
            best_entry, best_score, best_tier, best_coverage = mir, score, tier, coverage
    return best_entry, best_score, best_tier, best_coverage


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


def _diffs_and_flag(config: _MatchConfig, item: _Matchable, mir):
    """Returns (qty_diff_pct, rate_diff_pct, value_diff_pct, is_flagged,
    uom_mismatch, severity).

    Fix 2.C: under a uom_mismatch, qty/rate diffs are None (not a nonsense
    percentage) and uom_mismatch itself drives is_flagged instead - a
    mass-vs-count pair is a real, distinct problem, not "qty happens to
    differ by 9999.99%".

    Fix 3.F: qty/rate stay at zero-tolerance (config.flag_diff_pct) per the
    locked policy; value only flags when the ABSOLUTE currency difference
    exceeds config.value_flag_epsilon, since value is derived (qty x rate,
    plus tax-split rounding) and a rupee or two of rounding isn't a real
    discrepancy the way any nonzero qty/rate diff is."""
    qty_a, qty_b, rate_a, rate_b, uom_mismatch = _uom_adjust(item.qty, item.uom, mir.qty, mir.uom, item.rate, mir.rate)
    if uom_mismatch:
        qty_diff, rate_diff = None, None
    else:
        qty_diff = _diff_pct(qty_a, qty_b)
        rate_diff = _diff_pct(rate_a, rate_b)

    mir_value = config.mir_value(mir)
    value_diff = _diff_pct(item.value, mir_value)
    value_flagged = (
        item.value is not None and mir_value is not None
        and abs(item.value - mir_value) > config.value_flag_epsilon
    )

    qty_flagged = qty_diff is not None and qty_diff > config.flag_diff_pct
    rate_flagged = rate_diff is not None and rate_diff > config.flag_diff_pct
    is_flagged = uom_mismatch or qty_flagged or rate_flagged or value_flagged
    severity = _severity(uom_mismatch, qty_diff, rate_diff, value_diff)
    return qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity


# ── Public API ───────────────────────────────────────────────────────────────
# Every function below writes to the DB (upsert-or-delete a match row) and is
# safe to call repeatedly.

def match_po_mir_line_item(config: _MatchConfig, po_line_item):
    """Finds the best MIR match for one domestic PO line item, in isolation
    (no knowledge of other line items' claims - see module docstring), and
    upserts config.po_mir_match_model, or deletes any existing match if
    nothing clears the threshold. Returns the resulting match (or None)."""
    po = po_line_item.purchase_order
    item = _Matchable(po_line_item.description, po_line_item.qty, po_line_item.uom, po_line_item.net_price, po_line_item.net_value)
    candidates = _candidate_mir_entries(config, po.vendor_name)
    pool, tier = _tier1_pool(candidates, po.po_number)
    best_entry, best_score, best_tier, coverage = _best_candidate(config, pool, tier, item)

    if best_entry is None or best_score < config.match_threshold:
        config.po_mir_match_model.objects.filter(po_line_item=po_line_item).delete()
        return None

    qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity = _diffs_and_flag(config, item, best_entry)
    match, _ = config.po_mir_match_model.objects.update_or_create(
        po_line_item=po_line_item,
        defaults=dict(
            mir_entry=best_entry,
            tier=best_tier,
            match_score=best_score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
            uom_mismatch=uom_mismatch,
            field_coverage=coverage.quantize(Decimal("0.01")),
            severity=severity,
        ),
    )
    return match


def match_import_po_mir_line_item(config: _MatchConfig, import_line_item):
    """Same shape as match_po_mir_line_item() - the only real differences
    are the qty field (qty_as_per_boe, not qty_as_per_po), the currency
    conversion _import_rate_value_inr() does before scoring, and the match
    model class. Matches against the same MIR table domestic matching uses -
    MIR is a shared Drive file across domestic and import purchases."""
    po = import_line_item.purchase_order
    rate_inr, value_inr = _import_rate_value_inr(import_line_item)
    item = _Matchable(import_line_item.description, import_line_item.qty_as_per_boe, import_line_item.uom, rate_inr, value_inr)
    candidates = _candidate_mir_entries(config, po.vendor_name)
    pool, tier = _tier1_pool(candidates, po.po_number)
    best_entry, best_score, best_tier, coverage = _best_candidate(config, pool, tier, item)

    if best_entry is None or best_score < config.match_threshold:
        config.import_po_mir_match_model.objects.filter(po_line_item=import_line_item).delete()
        return None

    qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity = _diffs_and_flag(config, item, best_entry)
    match, _ = config.import_po_mir_match_model.objects.update_or_create(
        po_line_item=import_line_item,
        defaults=dict(
            mir_entry=best_entry,
            tier=best_tier,
            match_score=best_score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
            uom_mismatch=uom_mismatch,
            field_coverage=coverage.quantize(Decimal("0.01")),
            severity=severity,
        ),
    )
    return match


def match_mir_entry_stock(config: _MatchConfig, mir_entry):
    """Finds every Stock lot that (material[, vendor])-matches one MIR entry
    and upserts config.mir_stock_match_model for each - a many-to-many
    relationship on purpose (the same material/vendor is received into stock
    across multiple lots over time), unaffected by fix 2.B's PO<->MIR
    exclusivity. Vendor gating is skipped entirely when
    config.stock_vendor_field is None (Achhad's Stock sheet has no vendor
    column - see module docstring)."""
    mir_material = normalize_material(mir_entry.material_description)
    mir_vendor = normalize_vendor(mir_entry.party_name) if config.stock_vendor_field else None
    if not mir_material or (config.stock_vendor_field and not mir_vendor):
        return []

    lots = config.stock_lot_model.objects.filter(is_active=True).exclude(description="")
    if config.stock_vendor_field:
        lots = lots.exclude(**{config.stock_vendor_field: ""})
    candidates = []
    for lot in lots:
        if normalize_material(lot.description) != mir_material:
            continue
        if config.stock_vendor_field and not _vendor_matches(normalize_vendor(getattr(lot, config.stock_vendor_field)), mir_vendor):
            continue
        candidates.append(lot)

    matches = []
    matched_lot_ids = set()
    for lot in candidates:
        # A lot's own "received" running-total field reads 0 for nearly
        # every real lot (it evidently clears once allocated) - qty is
        # deliberately not compared here for any plant, only rate.
        rate_diff = _diff_pct(mir_entry.rate, getattr(lot, config.stock_rate_field))
        is_flagged = rate_diff is not None and rate_diff > config.flag_diff_pct
        match, _ = config.mir_stock_match_model.objects.update_or_create(
            mir_entry=mir_entry,
            stock_lot=lot,
            defaults=dict(qty_diff_pct=None, rate_diff_pct=rate_diff, is_flagged=is_flagged),
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

    Safe to call repeatedly (idempotent upserts) - intended to run after
    each sync, and synchronously after any "Edit Everywhere" field edit that
    feeds matching.
    """
    po_items = list(config.po_item_model.objects.select_related("purchase_order").all())
    import_items = list(config.import_item_model.objects.select_related("purchase_order").all())

    items_by_key: dict[tuple[str, int], _Matchable] = {}
    pairs = []  # (score, "po"|"import", item_key, mir_entry, tier, coverage)
    for item in po_items:
        po = item.purchase_order
        matchable = _Matchable(item.description, item.qty, item.uom, item.net_price, item.net_value)
        items_by_key[("po", item.id)] = matchable
        candidates = _candidate_mir_entries(config, po.vendor_name)
        pool, tier = _tier1_pool(candidates, po.po_number)
        for mir, score, mir_tier, coverage in _scored_pairs_above_threshold(config, pool, tier, matchable):
            pairs.append((score, "po", item.id, mir, mir_tier, coverage))

    for item in import_items:
        po = item.purchase_order
        rate_inr, value_inr = _import_rate_value_inr(item)
        matchable = _Matchable(item.description, item.qty_as_per_boe, item.uom, rate_inr, value_inr)
        items_by_key[("import", item.id)] = matchable
        candidates = _candidate_mir_entries(config, po.vendor_name)
        pool, tier = _tier1_pool(candidates, po.po_number)
        for mir, score, mir_tier, coverage in _scored_pairs_above_threshold(config, pool, tier, matchable):
            pairs.append((score, "import", item.id, mir, mir_tier, coverage))

    pairs.sort(key=lambda p: p[0], reverse=True)
    claimed_mir_ids: set[int] = set()
    assigned: dict[tuple[str, int], tuple] = {}  # (kind, item.id) -> (mir, score, tier, coverage)
    for score, kind, item_id, mir, tier, coverage in pairs:
        key = (kind, item_id)
        if key in assigned or mir.id in claimed_mir_ids:
            continue
        assigned[key] = (mir, score, tier, coverage)
        claimed_mir_ids.add(mir.id)

    po_matched = 0
    for item in po_items:
        result = assigned.get(("po", item.id))
        if result is None:
            config.po_mir_match_model.objects.filter(po_line_item=item).delete()
            continue
        mir, score, tier, coverage = result
        matchable = items_by_key[("po", item.id)]
        qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity = _diffs_and_flag(config, matchable, mir)
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
            ),
        )
        po_matched += 1

    import_po_matched = 0
    for item in import_items:
        result = assigned.get(("import", item.id))
        if result is None:
            config.import_po_mir_match_model.objects.filter(po_line_item=item).delete()
            continue
        mir, score, tier, coverage = result
        matchable = items_by_key[("import", item.id)]
        qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity = _diffs_and_flag(config, matchable, mir)
        config.import_po_mir_match_model.objects.update_or_create(
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
            ),
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
