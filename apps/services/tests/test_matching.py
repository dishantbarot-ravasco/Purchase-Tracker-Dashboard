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


class TestCloseness:
    def test_exact_match_is_one(self):
        assert _closeness(100, 100) == Decimal("1")

    def test_decays_linearly_to_zero_at_50pct_relative_difference(self):
        # denom=200 (the larger side), diff=100 -> diff_ratio=0.5 -> 1 - 0.5*2 = 0
        assert _closeness(100, 200) == Decimal("0")

    def test_clamped_at_zero_beyond_50pct_difference_not_negative(self):
        # denom=300, diff=200 -> diff_ratio=0.667 -> 1 - 1.333 would be
        # negative without the max(0, ...) clamp.
        assert _closeness(100, 300) == Decimal("0")

    def test_both_sides_zero_is_a_perfect_match(self):
        assert _closeness(0, 0) == Decimal("1")

    def test_either_side_none_is_none_not_a_perfect_match(self):
        # A blank field must never look like a perfect match - see the
        # function's own docstring.
        assert _closeness(None, 100) is None
        assert _closeness(100, None) is None
        assert _closeness(None, None) is None

    def test_quarter_off_gives_half_credit(self):
        # denom=100, diff=25 -> diff_ratio=0.25 -> 1 - 0.5 = 0.5
        assert _closeness(100, 75) == Decimal("0.5")


class TestDiffPct:
    def test_five_percent_over(self):
        assert _diff_pct(100, 105) == Decimal("5.00")

    def test_no_difference_is_zero(self):
        assert _diff_pct(100, 100) == Decimal("0")

    def test_both_sides_zero_is_none_not_a_flaggable_diff(self):
        assert _diff_pct(0, 0) is None

    def test_reference_zero_actual_nonzero_clamps_to_max(self):
        # Can't compute a percentage against a zero denominator - clamp
        # rather than divide-by-zero or claim "no difference".
        assert _diff_pct(0, 5) == Decimal("9999.99")

    def test_either_side_none_is_none(self):
        assert _diff_pct(None, 100) is None
        assert _diff_pct(100, None) is None

    def test_clamps_at_max_diff_pct_column_width(self):
        # *_diff_pct columns are DecimalField(max_digits=6, decimal_places=2)
        # - a pathological pair (tiny reference vs. huge actual) must clamp,
        # not overflow the column on insert.
        assert _diff_pct(Decimal("0.01"), Decimal("1000000")) == Decimal("9999.99")

    def test_flag_threshold_boundary(self):
        # FLAG_DIFF_PCT = 0 (zero tolerance, 2026-09-04 - was 5.00) - confirm
        # the constant matching.py's matching logic actually flags against
        # hasn't silently drifted, and that an exact match (0.00 diff) is
        # still not itself flaggable (callers use `>`, not `>=` - identical
        # values must never read as "discrepant").
        assert FLAG_DIFF_PCT == Decimal("0")
        diff = _diff_pct(100, 100)
        assert diff == Decimal("0")
        assert not (diff > FLAG_DIFF_PCT)
        diff2 = _diff_pct(1000, 999)  # 1kg out of 1000kg - the exact example the owner gave
        assert diff2 > FLAG_DIFF_PCT


class TestTokenOverlap:
    def test_identical_descriptions_is_one(self):
        assert _token_overlap("Sulphur Powder", "Sulphur Powder") == Decimal("1")

    def test_completely_different_descriptions_is_zero(self):
        assert _token_overlap("Sulphur Powder", "Zinc Oxide") == Decimal("0")

    def test_partial_overlap_is_jaccard_similarity(self):
        # {"natural","rubber"} vs {"natural","rubber","isnr","20"} -> 2/4
        assert _token_overlap("Natural Rubber", "Natural Rubber ISNR 20") == Decimal("0.5")

    def test_either_side_blank_is_zero_not_a_match(self):
        assert _token_overlap("", "Sulphur Powder") == Decimal("0")
        assert _token_overlap("Sulphur Powder", "") == Decimal("0")


class TestVendorMatches:
    def test_exact_normalized_match(self):
        assert _vendor_matches("kedarmetals", "kedarmetals") is True

    def test_containment_catches_the_real_hrs_city_suffix_case(self):
        # 'Rubamin Private Limited' (MIR/PO) vs 'Rubamin Private Limited -
        # Vadodara' (Stock) - both normalize to a value where one contains
        # the other. This is the exact case that forced containment over
        # exact-match in the first place (see CLAUDE.md).
        assert _vendor_matches("rubamin", "rubaminvadodara") is True

    def test_unrelated_vendors_do_not_match(self):
        assert _vendor_matches("kedarmetals", "jayamchemicals") is False

    def test_short_strings_never_match_even_if_technically_contained(self):
        # Length floor avoids a short/near-empty normalized name trivially
        # matching everything.
        assert _vendor_matches("ab", "abcdef") is False
        assert _vendor_matches("", "anything") is False


class TestPoNumberMatches:
    def test_exact_match(self):
        assert _po_number_matches("3000001075", "3000001075") is True

    def test_substring_match_for_combined_po_numbers(self):
        # Real data shape: some MIR rows are typed as
        # 'HRS/HO/26-27/003 & 004' - substring match catches this.
        assert _po_number_matches("HRS/HO/26-27/003", "HRS/HO/26-27/003 & 004") is True

    def test_case_insensitive(self):
        assert _po_number_matches("hrs/ho/26-27/003", "HRS/HO/26-27/003") is True

    def test_either_side_blank_never_matches(self):
        assert _po_number_matches("", "3000001075") is False
        assert _po_number_matches("3000001075", "") is False

    def test_unrelated_numbers_do_not_match(self):
        assert _po_number_matches("3000001075", "3000001099") is False


def test_match_threshold_constant_unchanged():
    # A sentinel, not a real behavioral test - documents the current cutoff
    # so a silent edit to this constant shows up as a failing test instead
    # of an unreviewed diff. See CLAUDE.md's match-accuracy notes for the
    # context behind why this hasn't been raised/lowered without measuring
    # against real precision/recall.
    assert MATCH_THRESHOLD == Decimal("0.55")
