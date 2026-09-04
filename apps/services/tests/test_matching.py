"""
Unit tests for the pure scoring helpers in apps/services/matching.py (HRS) -
_closeness(), _diff_pct(), _token_overlap(), _vendor_matches(),
_po_number_matches(). These are the actual PO<->MIR<->Stock reconciliation
math (see CLAUDE.md's "PO<->MIR<->Stock matching" section for the full
scoring rationale) - mirrors the TDS Automation App's
apps/services/tests/test_calculations.py convention: pure-function unit
tests, no Django DB, no mocking.

matching_achhad.py and matching_vapi.py carry byte-for-byte identical
copies of these same five helpers (confirmed against the source - this is a
deliberate, documented duplication, not an oversight: each plant's matching
module is independently readable end-to-end rather than importing shared
internals from a "matching_common" module - see CLAUDE.md's "Per-plant
models, not a shared schema" section for the same reasoning applied one
layer up, at the model level). Testing HRS's copy exercises the identical
logic all three plants run; a change to any plant's copy without the same
change to the others would only be caught by this file if you also update
which module it imports from - keep that in mind if the three ever
genuinely diverge on purpose.
"""
from decimal import Decimal

from apps.services.matching import (
    FLAG_DIFF_PCT,
    MATCH_THRESHOLD,
    _closeness,
    _diff_pct,
    _po_number_matches,
    _token_overlap,
    _vendor_matches,
)


# ── _closeness(): the 0..1 score behind qty/rate/value weighted matching ───────

class TestCloseness:
    def test_exact_match_is_one(self):
        """Identical values always score a perfect 1."""
        assert _closeness(100, 100) == Decimal("1")

    def test_decays_linearly_to_zero_at_50pct_relative_difference(self):
        # denom=200 (the larger side), diff=100 -> diff_ratio=0.5 -> 1 - 0.5*2 = 0
        """A 50% relative difference (measured against the larger side) is
        the exact point where the score decays to zero."""
        assert _closeness(100, 200) == Decimal("0")

    def test_clamped_at_zero_beyond_50pct_difference_not_negative(self):
        # denom=300, diff=200 -> diff_ratio=0.667 -> 1 - 1.333 would be
        # negative without the max(0, ...) clamp.
        """Beyond 50% difference the raw linear formula would go negative -
        confirms the max(0, ...) clamp keeps the score a valid 0, not negative."""
        assert _closeness(100, 300) == Decimal("0")

    def test_both_sides_zero_is_a_perfect_match(self):
        """Two zero values (e.g. both sides genuinely have no rate) count as
        a perfect match, not a division-by-zero or a zero score."""
        assert _closeness(0, 0) == Decimal("1")

    def test_either_side_none_is_none_not_a_perfect_match(self):
        # A blank field must never look like a perfect match - see the
        # function's own docstring.
        """A missing (None) value on either side must return None, not 1 or
        0 - a blank field must never masquerade as a perfect match."""
        assert _closeness(None, 100) is None
        assert _closeness(100, None) is None
        assert _closeness(None, None) is None

    def test_quarter_off_gives_half_credit(self):
        # denom=100, diff=25 -> diff_ratio=0.25 -> 1 - 0.5 = 0.5
        """A 25% relative difference scores exactly 0.5 - confirms the linear
        decay slope (2x the diff ratio) is what's actually implemented."""
        assert _closeness(100, 75) == Decimal("0.5")


# ── _diff_pct(): the signed/clamped percentage-difference figure stored on matches ──

class TestDiffPct:
    def test_five_percent_over(self):
        """A simple 5% overage computes cleanly to 5.00."""
        assert _diff_pct(100, 105) == Decimal("5.00")

    def test_no_difference_is_zero(self):
        """Identical values yield exactly zero difference."""
        assert _diff_pct(100, 100) == Decimal("0")

    def test_both_sides_zero_is_none_not_a_flaggable_diff(self):
        """Two zero values have no meaningful percentage difference - must
        return None, not 0 (0 would still read as "no diff" downstream, but
        None makes explicit that this pair was never really compared)."""
        assert _diff_pct(0, 0) is None

    def test_reference_zero_actual_nonzero_clamps_to_max(self):
        # Can't compute a percentage against a zero denominator - clamp
        # rather than divide-by-zero or claim "no difference".
        """A zero reference value against a nonzero actual would divide by
        zero - must clamp to the max instead of crashing or claiming no diff."""
        assert _diff_pct(0, 5) == Decimal("9999.99")

    def test_either_side_none_is_none(self):
        """A missing value on either side yields None, same reasoning as _closeness()."""
        assert _diff_pct(None, 100) is None
        assert _diff_pct(100, None) is None

    def test_clamps_at_max_diff_pct_column_width(self):
        # *_diff_pct columns are DecimalField(max_digits=6, decimal_places=2)
        # - a pathological pair (tiny reference vs. huge actual) must clamp,
        # not overflow the column on insert.
        """Regression test tied to CLAUDE.md's "*_diff_pct columns need a
        clamp" note: *_diff_pct columns are DecimalField(max_digits=6,
        decimal_places=2), capped at 9999.99. A pathological pair (tiny
        reference vs. huge actual, e.g. a mistyped rate) must clamp to that
        ceiling instead of overflowing the column on insert."""
        assert _diff_pct(Decimal("0.01"), Decimal("1000000")) == Decimal("9999.99")

    def test_flag_threshold_boundary(self):
        # FLAG_DIFF_PCT = 0 (zero tolerance, 2026-09-04 - was 5.00) - confirm
        # the constant matching.py's matching logic actually flags against
        # hasn't silently drifted, and that an exact match (0.00 diff) is
        # still not itself flaggable (callers use `>`, not `>=` - identical
        # values must never read as "discrepant").
        """Guards the current zero-tolerance policy (FLAG_DIFF_PCT = 0, since
        2026-09-04, was 5.00): confirms the constant hasn't silently drifted,
        that an exact match (0.00 diff) still isn't itself flaggable (callers
        compare with `>`, not `>=`), and that even a tiny real difference
        (1kg out of 1000kg - the exact example the project owner gave) is
        enough to exceed the threshold under zero tolerance."""
        assert FLAG_DIFF_PCT == Decimal("0")
        diff = _diff_pct(100, 100)
        assert diff == Decimal("0")
        assert not (diff > FLAG_DIFF_PCT)
        diff2 = _diff_pct(1000, 999)  # 1kg out of 1000kg - the exact example the owner gave
        assert diff2 > FLAG_DIFF_PCT


# ── _token_overlap(): Jaccard similarity between two material descriptions ─────

class TestTokenOverlap:
    def test_identical_descriptions_is_one(self):
        """Two identical descriptions have full token overlap."""
        assert _token_overlap("Sulphur Powder", "Sulphur Powder") == Decimal("1")

    def test_completely_different_descriptions_is_zero(self):
        """No shared tokens at all scores zero."""
        assert _token_overlap("Sulphur Powder", "Zinc Oxide") == Decimal("0")

    def test_partial_overlap_is_jaccard_similarity(self):
        # {"natural","rubber"} vs {"natural","rubber","isnr","20"} -> 2/4
        """A description that's a superset of the other's tokens scores
        exactly |intersection|/|union| - here {natural,rubber} vs
        {natural,rubber,isnr,20} is 2/4 = 0.5, confirming Jaccard similarity
        (not, say, precision/recall against one side) is what's implemented."""
        assert _token_overlap("Natural Rubber", "Natural Rubber ISNR 20") == Decimal("0.5")

    def test_either_side_blank_is_zero_not_a_match(self):
        """A blank description on either side scores zero, not undefined/None
        - two blanks must never accidentally look like a match."""
        assert _token_overlap("", "Sulphur Powder") == Decimal("0")
        assert _token_overlap("Sulphur Powder", "") == Decimal("0")


# ── _vendor_matches(): the hard vendor gate (containment, not exact equality) ──

class TestVendorMatches:
    def test_exact_normalized_match(self):
        """Two identical normalized vendor names match."""
        assert _vendor_matches("kedarmetals", "kedarmetals") is True

    def test_containment_catches_the_real_hrs_city_suffix_case(self):
        # 'Rubamin Private Limited' (MIR/PO) vs 'Rubamin Private Limited -
        # Vadodara' (Stock) - both normalize to a value where one contains
        # the other. This is the exact case that forced containment over
        # exact-match in the first place (see CLAUDE.md).
        """Regression-shaped test for the real reason vendor matching uses
        containment rather than exact equality: HRS's Stock sheet appends a
        city suffix ("Rubamin Private Limited - Vadodara") that MIR/PO data
        doesn't carry ("Rubamin Private Limited"), which produced zero
        MIR<->Stock matches under exact matching. Both normalize to a value
        where one contains the other."""
        assert _vendor_matches("rubamin", "rubaminvadodara") is True

    def test_unrelated_vendors_do_not_match(self):
        """Two genuinely different vendor names never match."""
        assert _vendor_matches("kedarmetals", "jayamchemicals") is False

    def test_short_strings_never_match_even_if_technically_contained(self):
        # Length floor avoids a short/near-empty normalized name trivially
        # matching everything.
        """A length floor prevents a short/near-empty normalized name from
        trivially "containing" or being contained by everything - otherwise
        containment alone would be too permissive a gate."""
        assert _vendor_matches("ab", "abcdef") is False
        assert _vendor_matches("", "anything") is False


# ── _po_number_matches(): Tier-1 PO<->MIR shortcut (exact/substring, case-insensitive) ──

class TestPoNumberMatches:
    def test_exact_match(self):
        """Identical PO numbers match."""
        assert _po_number_matches("3000001075", "3000001075") is True

    def test_substring_match_for_combined_po_numbers(self):
        # Real data shape: some MIR rows are typed as
        # 'HRS/HO/26-27/003 & 004' - substring match catches this.
        """Real data shape: some MIR rows are typed as a combined PO number
        like "HRS/HO/26-27/003 & 004" covering multiple POs - substring
        match against one of the real PO numbers must still catch this."""
        assert _po_number_matches("HRS/HO/26-27/003", "HRS/HO/26-27/003 & 004") is True

    def test_case_insensitive(self):
        """PO number matching must be case-insensitive - source sheets aren't consistent about casing."""
        assert _po_number_matches("hrs/ho/26-27/003", "HRS/HO/26-27/003") is True

    def test_either_side_blank_never_matches(self):
        """A blank PO number on either side must never match - MIR's PO field
        is often blank on real data (see CLAUDE.md), and a blank must not
        accidentally satisfy substring containment against anything."""
        assert _po_number_matches("", "3000001075") is False
        assert _po_number_matches("3000001075", "") is False

    def test_unrelated_numbers_do_not_match(self):
        """Two genuinely different PO numbers, even similarly shaped, don't match."""
        assert _po_number_matches("3000001075", "3000001099") is False


def test_match_threshold_constant_unchanged():
    # A sentinel, not a real behavioral test - documents the current cutoff
    # so a silent edit to this constant shows up as a failing test instead
    # of an unreviewed diff. See CLAUDE.md's match-accuracy notes for the
    # context behind why this hasn't been raised/lowered without measuring
    # against real precision/recall.
    """A sentinel, not a real behavioral test - documents the current cutoff
    so a silent edit to MATCH_THRESHOLD shows up as a failing test instead of
    an unreviewed diff. Per CLAUDE.md's match-accuracy notes, 0.55 was picked,
    not measured against labeled ground truth - this test exists to make any
    future change to it a deliberate, visible decision."""
    assert MATCH_THRESHOLD == Decimal("0.55")
