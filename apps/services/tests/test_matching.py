"""
Unit tests for the pure scoring helpers in apps/services/matching.py (HRS) -
_closeness(), _diff_pct(), _token_overlap(), _vendor_matches(),
_po_number_matches(). These are the actual PO<->MIR<->Stock reconciliation
math (see CLAUDE.md's "PO<->MIR<->Stock matching" section for the full
scoring rationale) - mirrors the TDS Automation App's
apps/services/tests/test_calculations.py convention: pure-function unit
tests, no Django DB, no mocking.

As of the Match Accuracy Programme's Phase 0 (see apps/services/matching_core.py's
module docstring), these five helpers live in exactly one place -
matching_core.py - and matching.py/matching_achhad.py/matching_vapi.py each
build a _MatchConfig around it rather than carrying their own copies. This
file imports them from matching_core directly, so it now genuinely exercises
the same code all three plants run, not just HRS's own copy of it.
FLAG_DIFF_PCT/MATCH_THRESHOLD stay imported from matching.py (HRS) - each
plant still owns its own module-level constants, per matching_core.py's own
docstring, even though all three currently use the same values.
"""
from decimal import Decimal

from apps.services.matching import FLAG_DIFF_PCT, MATCH_THRESHOLD
from apps.services.matching_core import (
    _MatchConfig,
    _Matchable,
    _closeness,
    _diff_pct,
    _diffs_and_flag,
    _identification_pool,
    _import_matchable,
    _import_total_value_inr,
    _material_matches,
    _po_number_matches,
    _tax_type_mismatch,
    _token_overlap,
    _uom_adjust,
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

    def test_prefix_collision_does_not_match(self):
        # Fix 2.E: matching.py:130 used to do a plain `in` substring test,
        # making 'HRS/HO/26-27/003' a false tier-1 hit against
        # 'HRS/HO/26-27/0031' - a genuinely different PO. Whole-token
        # comparison must reject this.
        """Regression test for fix 2.E: a PO number that is a textual prefix
        of a different, longer PO number must NOT match - the old plain
        substring test treated 'HRS/HO/26-27/003' as contained in
        'HRS/HO/26-27/0031', which is a different PO."""
        assert _po_number_matches("HRS/HO/26-27/003", "HRS/HO/26-27/0031") is False

    def test_trailing_float_artifact_still_matches(self):
        # Real data shape: openpyxl reads some PO-number cells as floats, so
        # po_number_raw can carry a literal '.0' suffix the PO's own
        # po_number field never has (e.g. '3000001081.0' vs '3000001081').
        """A '.0' float-parsing artifact on the MIR side (confirmed real
        data shape - openpyxl reads some PO-number cells as floats) must
        still match the PO's own clean integer-looking po_number."""
        assert _po_number_matches("3000001081", "3000001081.0") is True


# ── _material_matches()/_identification_pool(): the 2026-09-07 identification gate ──
# "Vendor mandatory plus one of {material, PO number}" - vendor is already
# satisfied by the time a candidate reaches _identification_pool() (the hard
# gate in _candidate_mir_entries()), so these tests exercise the remaining
# material-or-PO-number requirement directly.

class _FakeCandidate:
    def __init__(self, id, material_description, po_number_raw):
        self.id = id
        self.material_description = material_description
        self.po_number_raw = po_number_raw


class TestMaterialMatches:
    def test_above_threshold_matches(self):
        config = _test_config()
        assert _material_matches(config, "Natural Rubber ISNR 20", "Natural Rubber ISNR 20") is True

    def test_below_threshold_does_not_match(self):
        config = _test_config()
        assert _material_matches(config, "Sulphur Powder", "Zinc Oxide") is False

    def test_exactly_at_threshold_matches(self):
        # Jaccard >= threshold, not strictly > - a description scoring
        # exactly the configured threshold must still count as identified.
        config = _test_config(material_match_threshold=Decimal("0.5"))
        assert _material_matches(config, "Natural Rubber", "Natural Rubber ISNR 20") is True  # 2/4 = 0.5


class TestIdentificationPool:
    def test_material_only_match_is_included(self):
        """A candidate whose PO number doesn't match but whose material
        description does clears identification on material alone."""
        config = _test_config()
        item = _Matchable("Sulphur Powder", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        candidates = [_FakeCandidate(1, "Sulphur Powder", "UNRELATED-PO")]
        pool, id_flags = _identification_pool(config, candidates, item, "3000001075")
        assert pool == candidates
        assert id_flags[1] == (True, False)

    def test_po_number_only_match_is_included(self):
        """A candidate whose material description doesn't overlap at all but
        whose PO number matches clears identification on PO number alone -
        this is the real Vapi shape (material sometimes garbled, PO number
        exact) as well as the reverse HRS shape."""
        config = _test_config()
        item = _Matchable("Sulphur Powder", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        candidates = [_FakeCandidate(1, "Completely Different Material", "3000001075")]
        pool, id_flags = _identification_pool(config, candidates, item, "3000001075")
        assert pool == candidates
        assert id_flags[1] == (False, True)

    def test_neither_matching_is_excluded(self):
        """A candidate with no material overlap and no PO-number hit fails
        identification even though it already passed the vendor gate -
        this is what "PO Not Found" looks like when it's the only candidate:
        the pool ends up empty and no match is created."""
        config = _test_config()
        item = _Matchable("Sulphur Powder", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        candidates = [_FakeCandidate(1, "Completely Different Material", "UNRELATED-PO")]
        pool, id_flags = _identification_pool(config, candidates, item, "3000001075")
        assert pool == []
        assert id_flags == {}


class TestTaxTypeMismatchDirect:
    """Direct unit tests for _tax_type_mismatch() - see TestTaxTypeMismatch
    (below) for the same behavior exercised through _diffs_and_flag()."""

    def test_no_gst_recorded_on_mir_is_never_a_mismatch(self):
        config = _test_config()
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.igst, mir.cgst_amt, mir.sgst_amt = Decimal("0"), Decimal("0"), Decimal("0")
        assert _tax_type_mismatch("IGST", mir) is False


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


# ── _uom_adjust()/_diffs_and_flag(): fixes 2.C (UOM normalization) and 3.F ──
# (value epsilon + severity). No Django DB needed - a plain duck-typed
# object stands in for a MIR entry, since these functions only ever read
# plain attributes off it (never touch the ORM).

class _FakeMir:
    def __init__(self, description, qty, uom, rate, taxable_value, po_number_raw=""):
        self.material_description = description
        self.qty = qty
        self.uom = uom
        self.rate = rate
        self.taxable_value = taxable_value
        self.po_number_raw = po_number_raw


def _test_config(**overrides):
    defaults = dict(
        po_item_model=None, import_item_model=None, mir_model=None,
        po_mir_match_model=None, import_po_mir_match_model=None,
        mir_stock_match_model=None, stock_lot_model=None,
        match_threshold=Decimal("0.55"), flag_diff_pct=Decimal("0"),
        value_flag_epsilon=Decimal("1.00"),
        weight_qty=Decimal("0.29"), weight_rate=Decimal("0.29"), weight_value=Decimal("0.42"),
        material_match_threshold=Decimal("0.3"),
        mir_value=lambda mir: mir.taxable_value,
        stock_rate_field="basic_rate", stock_vendor_field="party_name",
    )
    defaults.update(overrides)
    return _MatchConfig(**defaults)


class TestUomAdjust:
    def test_same_family_converts_qty_and_inverts_rate(self):
        # 2 MT = 2000 KG; a rate of 40/KG converts to 40000/MT (inverse of
        # the qty factor - price-per-unit scales inversely to unit size).
        """Same-family units (KG vs MT) convert qty by the unit factor and
        rate by its inverse, so a true match's qty/rate closeness is scored
        on comparable base-unit values instead of raw 1000x-apart numbers."""
        qty_a, qty_b, rate_a, rate_b, mismatch = _uom_adjust(Decimal("2"), "MT", Decimal("2000"), "KG", Decimal("40000"), Decimal("40"))
        assert mismatch is False
        assert qty_a == Decimal("2000")
        assert qty_b == Decimal("2000")
        assert rate_a == Decimal("40")
        assert rate_b == Decimal("40")

    def test_different_families_is_a_mismatch(self):
        """Mass vs count is a genuine unit-family mismatch, not a rate/qty
        discrepancy - fix 2.C's whole point (the doc's own worked example:
        'A PO in MT matching a MIR in KG' should still score as a match;
        this is the deliberately-different case where the units genuinely
        aren't comparable at all)."""
        qty_a, qty_b, rate_a, rate_b, mismatch = _uom_adjust(Decimal("100"), "KG", Decimal("100"), "NOS", Decimal("5"), Decimal("5"))
        assert mismatch is True
        assert (qty_a, qty_b, rate_a, rate_b) == (None, None, None, None)

    def test_unrecognized_unit_passes_through_unconverted(self):
        """An unrecognized unit on either side (blank, or a real-but-
        ambiguous code like 'TO') passes both values through unconverted
        rather than guessing a wrong conversion - see normalize_uom()'s own
        docstring for why."""
        qty_a, qty_b, rate_a, rate_b, mismatch = _uom_adjust(Decimal("100"), "TO", Decimal("100"), "KG", Decimal("5"), Decimal("5"))
        assert mismatch is False
        assert (qty_a, qty_b, rate_a, rate_b) == (Decimal("100"), Decimal("100"), Decimal("5"), Decimal("5"))


class TestDiffsAndFlagValueEpsilon:
    """As of the 2026-09-07 identification/financial-check redesign,
    `is_flagged` means specifically "qty or rate mismatched" - every other
    discrepancy (value/UOM/tax-type/etc.) now folds into `data_mismatch`
    instead. See matching_core.py's module docstring and
    _diffs_and_flag()'s own docstring for the full rationale."""

    def test_value_diff_under_epsilon_is_not_flagged(self):
        # ₹0.50 absolute diff on a ₹5000 base - well under the ₹1.00
        # epsilon, even though the percentage (0.01%) is technically nonzero.
        """Fix 3.F: a value difference under the ₹1.00 absolute epsilon does
        not flag (neither is_flagged nor data_mismatch), even though qty and
        rate keep exact-zero tolerance - value is derived (qty x rate, plus
        tax-split rounding), so a rupee or two of rounding isn't a real
        discrepancy."""
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.50"))
        (qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
         qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
         taxable_value_diff, final_value_diff, *_new_fields) = _diffs_and_flag(config, item, mir)
        assert is_flagged is False
        assert data_mismatch is False
        assert uom_mismatch is False

    def test_value_diff_over_epsilon_is_a_data_mismatch_not_is_flagged(self):
        """A value difference exceeding the ₹1.00 absolute epsilon is a
        `data_mismatch` now, not `is_flagged` - only qty/rate raise
        is_flagged under the redesign, confirming the epsilon still has a
        real ceiling and doesn't swallow every value discrepancy."""
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("4995.00"))
        (qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
         qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
         taxable_value_diff, final_value_diff,
         net_value_mismatched, taxable_value_mismatched, final_value_mismatched) = _diffs_and_flag(config, item, mir)
        assert is_flagged is False
        assert data_mismatch is True
        # Added 2026-09-08: net_value_mismatched is the specific signal that
        # rolled into data_mismatch here - confirming it's actually True
        # (not just that the combined bucket fired) is the whole point of
        # having a separate field at all.
        assert net_value_mismatched is True
        assert taxable_value_mismatched is False
        assert final_value_mismatched is False

    def test_taxable_value_mismatch_is_flagged_individually(self):
        """taxable_value_mismatched (added 2026-09-08) is only set for a
        single-line-item PO (total_value is otherwise a whole-PO aggregate,
        see _po_matchable()'s docstring) and only when the gap exceeds the
        value epsilon - same rule as net value, just a different pair of
        figures (PO's own Total Value vs MIR's Taxable Value)."""
        config = _test_config()
        item = _Matchable(
            "Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"),
            total_value=Decimal("5100.00"),
        )
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        (*_rest, net_value_mismatched, taxable_value_mismatched, final_value_mismatched) = _diffs_and_flag(config, item, mir)
        assert taxable_value_mismatched is True
        assert net_value_mismatched is False
        assert final_value_mismatched is False

    def test_final_value_mismatch_is_flagged_individually(self):
        """final_value_mismatched (added 2026-09-08) - PO's Total Inclusive
        Value vs MIR's Final/Invoice Value, same single-line-item and
        epsilon rules as taxable_value_mismatched."""
        config = _test_config()
        item = _Matchable(
            "Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"),
            total_inclusive_value=Decimal("5900.00"),
        )
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.invoice_final_value = Decimal("5850.00")
        (*_rest, net_value_mismatched, taxable_value_mismatched, final_value_mismatched) = _diffs_and_flag(config, item, mir)
        assert final_value_mismatched is True
        assert net_value_mismatched is False
        assert taxable_value_mismatched is False

    def test_quantity_keeps_exact_zero_tolerance_regardless_of_value_epsilon(self):
        """Quantity is directly reported (not derived like value) and stays
        at exact-zero tolerance - the value epsilon must never leak into the
        qty/rate comparison. A qty mismatch is is_flagged=True (one of the
        two "real errors" under the redesign)."""
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("1000"), "KG", Decimal("50"), Decimal("50000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("999"), "KG", Decimal("50"), Decimal("50000.00"))  # 1kg out of 1000kg
        (qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
         qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
         taxable_value_diff, final_value_diff, *_new_fields) = _diffs_and_flag(config, item, mir)
        assert is_flagged is True
        assert qty_mismatched is True
        assert rate_mismatched is False

    def test_uom_mismatch_is_a_data_mismatch_with_material_severity_and_none_qty_rate_diff(self):
        """Fix 2.C + 3.F together, updated for the 2026-09-07 redesign: a
        genuine unit-family mismatch always flags "material" severity and
        drives `data_mismatch` (not `is_flagged` - the redesign scopes
        is_flagged down to qty/rate only), and qty_diff_pct/rate_diff_pct
        are None (not a nonsense 9999.99%-style percentage)."""
        config = _test_config()
        item = _Matchable("Reclaim Rubber", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir = _FakeMir("Reclaim Rubber", Decimal("100"), "NOS", Decimal("50"), Decimal("5000.00"))
        (qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
         qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
         taxable_value_diff, final_value_diff, *_new_fields) = _diffs_and_flag(config, item, mir)
        assert uom_mismatch is True
        assert qty_diff is None
        assert rate_diff is None
        assert is_flagged is False
        assert data_mismatch is True
        assert severity == "material"

    def test_exact_match_has_no_severity(self):
        """An exact match on every factor has no measurable discrepancy at
        all - severity is None, not "rounding" (there's nothing to round),
        and neither is_flagged nor data_mismatch fire."""
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        (qty_diff, rate_diff, value_diff, is_flagged, uom_mismatch, severity,
         qty_mismatched, rate_mismatched, data_mismatch, tax_type_mismatch,
         taxable_value_diff, final_value_diff, *_new_fields) = _diffs_and_flag(config, item, mir)
        assert is_flagged is False
        assert data_mismatch is False
        assert severity is None

    def test_large_discrepancy_is_material_severity(self):
        """A qty discrepancy well past the 20% cut point is "material"
        severity - reusing flags.js's rowTintClass() thresholds, not a new one."""
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("1000"), "KG", Decimal("50"), Decimal("50000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("700"), "KG", Decimal("50"), Decimal("35000.00"))  # 30% off
        _qty_diff, _rate_diff, _value_diff, _is_flagged, _uom_mismatch, severity, *_rest = _diffs_and_flag(config, item, mir)
        assert severity == "material"


class TestTaxTypeMismatch:
    """New for the 2026-09-07 identification/financial-check redesign:
    structural IGST-vs-CGST/SGST consistency, folded into `data_mismatch`."""

    def test_igst_po_with_igst_mir_is_not_a_mismatch(self):
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"), tax_type="IGST")
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.igst, mir.cgst_amt, mir.sgst_amt = Decimal("900"), Decimal("0"), Decimal("0")
        *_rest, tax_type_mismatch, _taxable, _final, _net_mm, _taxable_mm, _final_mm = _diffs_and_flag(config, item, mir)
        assert tax_type_mismatch is False

    def test_igst_po_with_cgst_sgst_mir_is_a_mismatch(self):
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"), tax_type="IGST")
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.igst, mir.cgst_amt, mir.sgst_amt = Decimal("0"), Decimal("450"), Decimal("450")
        *_rest, tax_type_mismatch, _taxable, _final, _net_mm, _taxable_mm, _final_mm = _diffs_and_flag(config, item, mir)
        assert tax_type_mismatch is True

    def test_cgst_sgst_po_with_igst_mir_is_a_mismatch(self):
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"), tax_type="CGST+SGST")
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.igst, mir.cgst_amt, mir.sgst_amt = Decimal("900"), Decimal("0"), Decimal("0")
        *_rest, tax_type_mismatch, _taxable, _final, _net_mm, _taxable_mm, _final_mm = _diffs_and_flag(config, item, mir)
        assert tax_type_mismatch is True

    def test_blank_tax_type_is_never_a_mismatch(self):
        config = _test_config()
        item = _Matchable("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir = _FakeMir("Zinc Oxide", Decimal("100"), "KG", Decimal("50"), Decimal("5000.00"))
        mir.igst, mir.cgst_amt, mir.sgst_amt = Decimal("0"), Decimal("450"), Decimal("450")
        *_rest, tax_type_mismatch, _taxable, _final, _net_mm, _taxable_mm, _final_mm = _diffs_and_flag(config, item, mir)
        assert tax_type_mismatch is False


# ── _import_total_value_inr()/_import_matchable(): Imports identification/ ──
# financial-check redesign (2026-09, HRS/Achhad only - project owner: "keep
# vapi out for now"). Two separate qty checks exist for imports and must not
# be confused: PO-vs-BOE (apps/services/import_flags.py, unaffected by this
# redesign) and BOE-vs-MIR (this module, via qty_as_per_boe - see
# _import_matchable()'s own docstring).

class _FakePo:
    def __init__(self, total_value, item_count=1):
        self.total_value = total_value
        self._item_count = item_count

    @property
    def items(self):
        return _FakeItemsManager(self._item_count)


class _FakeItemsManager:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _FakeImportLineItem:
    def __init__(self, purchase_order, description="Zinc Oxide", qty_as_per_boe=Decimal("100"), uom="KG",
                 net_price=Decimal("50"), net_value=Decimal("5000.00"), exchange_rate=None,
                 total_inclusive_value=None, tax_type=""):
        self.purchase_order = purchase_order
        self.description = description
        self.qty_as_per_boe = qty_as_per_boe
        self.uom = uom
        self.net_price = net_price
        self.net_value = net_value
        self.exchange_rate = exchange_rate
        self.total_inclusive_value = total_inclusive_value
        self.tax_type = tax_type


class TestImportTotalValueInr:
    def test_converts_using_exchange_rate(self):
        """Total Value (As per PO) is foreign-currency, PO-level - must be
        converted to INR via the specific line's own exchange rate before it
        can be compared against MIR's Taxable Value, same reasoning
        _import_rate_value_inr() already uses for rate/value."""
        assert _import_total_value_inr(Decimal("100"), Decimal("93.8")) == Decimal("9380.0")

    def test_falls_back_to_bare_figure_when_exchange_rate_missing(self):
        """Real data gap (2 of 37 real Vapi rows had no exchange rate) -
        falls back to the untouched figure rather than dropping it or
        crashing, same tolerance _import_rate_value_inr() already has."""
        assert _import_total_value_inr(Decimal("100"), None) == Decimal("100")

    def test_none_total_value_stays_none(self):
        """No Total Value recorded at all means nothing to convert or
        compare - must stay None, not become 0 or crash on the multiply."""
        assert _import_total_value_inr(None, Decimal("93.8")) is None


class TestImportMatchable:
    def test_extended_fields_off_keeps_original_four_field_shape(self):
        """Vapi's config (import_extended_fields=False, the dataclass
        default) must get back the original bare _Matchable - its match
        model has no columns to store tax_type/total_value/
        total_inclusive_value in, so these must stay unset rather than being
        silently computed and then discarded."""
        config = _test_config()
        po = _FakePo(total_value=Decimal("1000"), item_count=1)
        item = _FakeImportLineItem(po, exchange_rate=Decimal("90"), total_inclusive_value=Decimal("9500"), tax_type="IGST")
        matchable = _import_matchable(config, item, is_single_item_po=True)
        assert matchable.tax_type is None
        assert matchable.total_value is None
        assert matchable.total_inclusive_value is None

    def test_extended_fields_on_single_item_po_converts_total_value(self):
        """HRS/Achhad's config (import_extended_fields=True): a single-line-
        item PO's Total Value gets converted to INR via this line's own
        exchange rate, and total_inclusive_value/tax_type pass through
        directly (both already line-item-level, unlike domestic - see
        _import_matchable()'s own docstring)."""
        config = _test_config(import_extended_fields=True)
        po = _FakePo(total_value=Decimal("100"), item_count=1)
        item = _FakeImportLineItem(po, exchange_rate=Decimal("93.8"), total_inclusive_value=Decimal("9500"), tax_type="IGST")
        matchable = _import_matchable(config, item, is_single_item_po=True)
        assert matchable.tax_type == "IGST"
        assert matchable.total_value == Decimal("9380.0")
        assert matchable.total_inclusive_value == Decimal("9500")

    def test_extended_fields_on_multi_item_po_excludes_total_value_only(self):
        """Multi-item-PO caveat (same as domestic's _po_matchable()): Total
        Value is a whole-PO aggregate repeated on every row for a multi-item
        PO, so it's excluded to avoid comparing the wrong thing - but
        total_inclusive_value/tax_type are genuinely per-line for imports
        (unlike domestic) and stay populated regardless of item count."""
        config = _test_config(import_extended_fields=True)
        po = _FakePo(total_value=Decimal("100"), item_count=3)
        item = _FakeImportLineItem(po, exchange_rate=Decimal("93.8"), total_inclusive_value=Decimal("9500"), tax_type="IGST")
        matchable = _import_matchable(config, item, is_single_item_po=False)
        assert matchable.total_value is None
        assert matchable.total_inclusive_value == Decimal("9500")
        assert matchable.tax_type == "IGST"

    def test_blank_tax_type_becomes_none_not_empty_string(self):
        """Same normalization _po_matchable() applies (`po.tax_type or
        None`) - an empty-string tax_type must read as "nothing to check",
        not a real (blank) value _tax_type_mismatch() would try to parse."""
        config = _test_config(import_extended_fields=True)
        po = _FakePo(total_value=None, item_count=1)
        item = _FakeImportLineItem(po, tax_type="")
        matchable = _import_matchable(config, item, is_single_item_po=True)
        assert matchable.tax_type is None
