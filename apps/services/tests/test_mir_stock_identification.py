"""
Unit tests for the MIR<->Stock identification rules added 2026-09-21: the
description cleaning, the fuzzy-token scorer mode, the grade-code
contradiction gate, and the NO_RM_STOCK_VENDORS registry.

Dependency-free by design, like the rest of this directory - every function
under test here is plain Python over strings and sets. The pipeline-level
behaviour (three tiers, best-evidence filtering, tier-1 exclusivity) is
exercised against real rows in test_mir_stock_matching.py.

Each case below is a REAL pair taken from the live files, named in the
assertion, not an invented example - the point of these tests is to pin the
specific data that motivated each rule, so a future tweak that re-breaks one
fails by name rather than by a percentage moving somewhere.
"""

from decimal import Decimal

from apps.services.matching_core import _MaterialScorer, _grade_codes, _grade_codes_contradict
from apps.services.parsers.common import (
    clean_mir_material_for_stock,
    clean_stock_material,
    is_no_rm_stock_vendor,
    not_stocked_material_entry,
    normalize_material,
    tokenize,
)


def _codes(text):
    return _grade_codes(tokenize(text))


class TestDescriptionCleaning:
    """Vapi's two kinds of bookkeeping noise - see parsers/common.py."""

    def test_strips_sap_material_code_prefix_from_mir(self):
        assert clean_mir_material_for_stock("RM00011014 ZINC OXIDE") == "ZINC OXIDE"
        assert clean_mir_material_for_stock("RMP001105002 EVA BAG") == "EVA BAG"
        assert clean_mir_material_for_stock("RM000130056 PRECIPITATED SILICA") == "PRECIPITATED SILICA"

    def test_keeps_a_description_that_is_only_a_code(self):
        # A row with nothing left after stripping still has to compare as
        # something rather than becoming the empty string, which would make
        # it identical to every other such row.
        assert clean_mir_material_for_stock("RM00015073") == "RM00015073"

    def test_does_not_touch_a_real_description(self):
        for text in ("ZINC OXIDE", "EE250 102CM FABRIC POLYESTER", "NBR 2675", ""):
            assert clean_mir_material_for_stock(text) == text

    def test_does_not_strip_a_code_shaped_word_that_is_not_one(self):
        # The guard is a word boundary plus RM + 0 + at least five digits.
        assert clean_mir_material_for_stock("RM 500 GRADE") == "RM 500 GRADE"
        assert clean_mir_material_for_stock("DRUM00011014X") == "DRUM00011014X"

    def test_strips_trailing_plant_tag_from_stock(self):
        assert clean_stock_material("RECLAIM RUBBER 6MPA HRS") == "RECLAIM RUBBER 6MPA"
        assert clean_stock_material("BAYPRENE RUBBER RTP2") == "BAYPRENE RUBBER"
        assert clean_stock_material("SILSHEET  (G) SILDEC HRS") == "SILSHEET (G) SILDEC"

    def test_strips_only_from_the_end_and_only_whole_tokens(self):
        # "HRS" inside a name is part of the name, not a location tag.
        assert clean_stock_material("HRS BLEND 400") == "HRS BLEND 400"
        # A lot whose description IS the tag keeps it rather than emptying.
        assert clean_stock_material("HRS") == "HRS"


class TestFuzzyTokenScorer:
    """_MaterialScorer(fuzzy_tokens=True) - the MIR<->Stock-only mode."""

    CORPUS = [
        "PRECIPITATD SILICA",
        "Precipitated Silica",
        "RECLAIM RUBBER 8MPA",
        "8MPA RECLAIM RUBBER",
        "ZINC OXIDE",
        "NBR 2675",
        "NBR 3345",
    ]

    def test_word_order_does_not_matter(self):
        # Real pair: Vapi MIR writes '8MPA RECLAIM RUBBER', its Stock sheet
        # writes 'RECLAIM RUBBER 8MPA'. Token sets, so this is exact.
        scorer = _MaterialScorer(self.CORPUS, fuzzy_tokens=True)
        assert scorer.similarity("8MPA RECLAIM RUBBER", "RECLAIM RUBBER 8MPA") == Decimal("1")

    def test_a_misspelled_token_still_counts_as_shared(self):
        # Real pair: HRS Stock writes 'PRECIPITATD SILICA' (missing the
        # second e) against MIR's 'Precipitated Silica'. Exact-token Jaccard
        # scores this 1/3; it has to read as the same material.
        fuzzy = _MaterialScorer(self.CORPUS, fuzzy_tokens=True)
        exact = _MaterialScorer(self.CORPUS)
        assert fuzzy.similarity("Precipitated Silica", "PRECIPITATD SILICA") == Decimal("1")
        assert exact.similarity("Precipitated Silica", "PRECIPITATD SILICA") < Decimal("0.45")

    def test_fuzzy_tokens_is_off_by_default(self):
        # PO<->MIR must be byte-identical to before this parameter existed.
        default = _MaterialScorer(self.CORPUS)
        explicit = _MaterialScorer(self.CORPUS, fuzzy_tokens=False)
        for a, b in [("Precipitated Silica", "PRECIPITATD SILICA"), ("NBR 2675", "NBR 3345")]:
            assert default.similarity(a, b) == explicit.similarity(a, b)

    def test_short_tokens_never_fuzzy_match(self):
        # Below four characters an edit-ratio test is noise, not spelling.
        scorer = _MaterialScorer(["OIL 501", "OIL 601"], fuzzy_tokens=True)
        assert scorer.similarity("OIL 501", "OIL 601") < Decimal("1")

    def test_one_correct_word_cannot_be_claimed_twice(self):
        # Two misspellings of the same word must not both collect credit for
        # it - the greedy pass consumes each right-hand token once.
        scorer = _MaterialScorer(["silica silica", "silaca silca"], fuzzy_tokens=True)
        assert scorer.similarity("silica", "silaca silca") <= Decimal("1")

    def test_score_never_exceeds_one(self):
        scorer = _MaterialScorer(self.CORPUS, fuzzy_tokens=True)
        for a in self.CORPUS:
            for b in self.CORPUS:
                assert Decimal("0") <= scorer.similarity(a, b) <= Decimal("1")

    def test_genuinely_different_materials_still_score_low(self):
        scorer = _MaterialScorer(self.CORPUS, fuzzy_tokens=True)
        assert scorer.similarity("ZINC OXIDE", "RECLAIM RUBBER 8MPA") < Decimal("0.45")


class TestGradeCodeContradiction:
    """The gate that makes the date+rate identification path safe.

    Every pair here was WRONGLY MATCHED on date+rate alone against real
    data before this gate existed - see _grade_codes_contradict()."""

    def test_rejects_the_real_false_positives(self):
        for a, b in [
            ("NBR 2675", "NBR 3345"),
            ("AUROBOND 825", "AUROAID AR 262"),
            ("Nordel 4770", "Hydrocarbon Rubber-Nordel 4570"),
            ("Auroaid AR 260", "AUROAID AR262"),
        ]:
            assert _grade_codes_contradict(_codes(a), _codes(b)), f"{a!r} vs {b!r} must contradict"

    def test_allows_the_real_true_positives(self):
        # Pairs the date+rate path correctly found: no codes on one side, or
        # codes that agree.
        for a, b in [
            ("Kanatol-8A (DOA)", "DOA Oil"),
            ("JC Magnesium Hydroxide", "JH Magnesium Hydroxide MDH"),
            ("MELAMINE CYANURATE", "MELAMINE CYANURATE (FIRECEZ M25)"),
            ("RECLAIM RUBBER 8MPA", "8MPA RECLAIM RUBBER"),
        ]:
            assert not _grade_codes_contradict(_codes(a), _codes(b)), f"{a!r} vs {b!r} must not contradict"

    def test_is_silent_when_either_side_has_no_code(self):
        assert not _grade_codes_contradict(set(), {"2675"})
        assert not _grade_codes_contradict({"2675"}, set())
        assert not _grade_codes_contradict(set(), set())


class TestNoRmStockVendorRegistry:
    """Madura - see parsers/common.py's NO_RM_STOCK_VENDORS."""

    def test_recognises_every_spelling_the_three_mir_files_use(self):
        for name in [
            "MADURA INDL TEXTILES LTD",          # Vapi's MIR
            "MADURA TECHNICAL FABRICS LTD.",     # Vapi's MIR, second spelling
            "Madura Industrial Textiles Ltd.",   # HRS's MIR
            "Madura Industrial Textile Ltd.",    # Achhad's MIR (singular)
            "Madura Technical Textiles Ltd",     # Vapi's PO master
        ]:
            assert is_no_rm_stock_vendor(name), f"{name!r} must be registered"

    def test_does_not_catch_anyone_else(self):
        # Including a NO_PO_VENDORS member - the two registries are separate
        # questions and Tinna's reclaim rubber really does land in stock.
        for name in [
            "Tinna Rubber and Infrastructure Ltd",
            "Ravasco Transmission & Packing Pvt Ltd",
            "Jayam Industries",
            "Madhu Silica Pvt. Ltd.",
            "",
        ]:
            assert not is_no_rm_stock_vendor(name), f"{name!r} must not be registered"


class TestNormalizeMaterialIsUnchanged:
    """normalize_material()'s output is a PERSISTED join key
    (MaterialCategoryReference.normalized_description, stock_identity's
    lot_natural_key()). The cleaning helpers above were added deliberately
    OUTSIDE it; this pins that they stayed outside."""

    def test_cleaning_did_not_leak_into_normalize_material(self):
        assert normalize_material("RM00011014 ZINC OXIDE") == "rm00011014 zinc oxide"
        assert normalize_material("RECLAIM RUBBER 6MPA HRS") == "reclaim rubber 6mpa hrs"


class TestNotStockedMaterialRegistry:
    """NOT_STOCKED_MATERIALS - see parsers/common.py.

    Every ACCEPTED class had to pass two tests against live data: no stock lot
    at any plant matches the pattern, and no currently-matched MIR row does.
    The REJECTED cases below are the ones that failed, and they are pinned
    here because re-adding them is the obvious "improvement" somebody will
    reach for."""

    PLANTS = ("hrs", "achhad", "vapi")

    def test_the_shared_classes_are_caught_at_every_plant(self):
        cases = [
            ("EE250 102CM FABRIC POLYESTER", "Conveyor fabric"),
            ("NN315 163CM FABRICS", "Conveyor fabric"),
            ("Rubberised Textile Fabric EE800", "Conveyor fabric"),
            ("CONVEYOR BELT (MTRS)", "Conveyor belting"),
            ("Conveyor or Transmission Belting", "Conveyor belting"),
            ("RUBBER COMPOUND (KGS)", "Rubber compound"),
            ("COMPOUNDED RUBBER UNVULCANISED", "Rubber compound"),
            ("M S CRATE", "Crates"),
            ("Machine Spare Parts", "Spares and services"),
        ]
        for plant in self.PLANTS:
            for description, expected in cases:
                entry = not_stocked_material_entry(description, plant)
                assert entry is not None, f"{description!r} should be excluded at {plant}"
                assert entry[0] == expected, f"{plant}: {description!r} -> {entry[0]}, expected {expected}"

    # ── The two genuinely per-plant classes ──────────────────────────────
    #
    # Measured 2026-09-21 by sweeping every class against every plant's own
    # Stock sheet:
    #     grease : stocked at HRS and Vapi ('GREASE EP 1'), absent at Achhad
    #     logo   : stocked at Achhad ('Lamor Logo 160mm X 70Mic'), absent at
    #              HRS and Vapi

    def test_grease_is_excluded_only_at_achhad(self):
        for description in ["GREASE EP 1", "Grease EP2", "GREASE"]:
            assert not_stocked_material_entry(description, "achhad") is not None, description
            assert not_stocked_material_entry(description, "hrs") is None, description
            assert not_stocked_material_entry(description, "vapi") is None, description

    def test_logo_work_is_excluded_everywhere_except_achhad(self):
        # Achhad's nine 'Lamor Logo Print' rows are a SCORER miss against a
        # lot that exists - excluding them there would bury the one genuinely
        # fixable thing this registry touches.
        for description in ["Lamor Logo Print", "Lamor Logo 160mm X 70Mic", "Printed Label"]:
            assert not_stocked_material_entry(description, "achhad") is None, description
            assert not_stocked_material_entry(description, "hrs") is not None, description
            assert not_stocked_material_entry(description, "vapi") is not None, description

    def test_an_unknown_plant_excludes_nothing(self):
        # A new plant must state its own scope; until it does, every row stays
        # in the pool exactly as it would have before this registry existed.
        for description in ["CONVEYOR BELT (MTRS)", "M S CRATE", "GREASE EP 1"]:
            assert not_stocked_material_entry(description, "newplant") is None, description
            assert not_stocked_material_entry(description, "") is None, description

    # ── Narrowings that each cost real matches before they were made ─────

    def test_named_rubber_compounds_are_NOT_excluded(self):
        # All three plants stock these - a blanket /rubber comp|silsheet/
        # destroyed 43 real matches (HRS 15, Achhad 27, Vapi 1).
        for plant in self.PLANTS:
            for description in [
                "Rubber Compound-EAR 11560",
                "Rubber Compound-SHRC T23",
                "Rubber Compound-40mm3",
                "Rubber Compound Cushion Roll",
                "Silsheet Rubber",
                "SILSHEET  (G) SILDEC",
            ]:
                assert not_stocked_material_entry(description, plant) is None, f"{plant}: {description}"

    def test_a_compound_that_merely_names_a_belt_is_NOT_excluded(self):
        # Achhad stocks 'Rubber Compound-Cushion'; its MIR writes the receipt
        # as 'Rubber Compound Cushion Belts', naming the belt only as the
        # application. A bare /\bbelts?\b/ in the belting pattern destroyed
        # that match - found by re-running the matcher with the registry
        # disabled and diffing, NOT by the two static checks.
        for plant in self.PLANTS:
            assert not_stocked_material_entry("Rubber Compound Cushion Belts", plant) is None, plant

    def test_real_belting_rows_are_still_excluded_after_that_narrowing(self):
        for plant in self.PLANTS:
            for description in [
                "CONVEYOR BELT (MTRS)",
                "Conveyor Belting",
                "CONVEYOR OR TRANSMISSION BELTING",
                "Conveyor Belt 800mm Wide x EP400 4x5mm Top",
                "Transmission Belt",
            ]:
                assert not_stocked_material_entry(description, plant) is not None, f"{plant}: {description}"

    def test_bags_and_wooden_items_are_NOT_excluded(self):
        # All three plants stock EVA/LD/BATA bags; HRS stocks wooden
        # stoppers and circles. Packing narrowed to crates only.
        for plant in self.PLANTS:
            for description in [
                'Eva Bag 20" X 20"',
                "LD Plastic Bag 20X26X180G",
                'BATA BAG/L.D. BAG 13"X20"X180G',
                'WOODEN STOPPER 12"',
                'WOODEN CIRCLE 12"X12"X4"',
            ]:
                assert not_stocked_material_entry(description, plant) is None, f"{plant}: {description}"

    def test_pallets_are_NOT_excluded(self):
        # 'Carbon Black Pallets (Majestique)' is a real Achhad lot, where
        # "pallets" is the FORM the carbon black arrives in. No plant has a
        # MIR row naming a pallet as the goods, so the word earns nothing.
        for plant in self.PLANTS:
            for description in ["Carbon Black Pallets (Majestique)", "Wooden Pallet"]:
                assert not_stocked_material_entry(description, plant) is None, f"{plant}: {description}"

    def test_ordinary_raw_materials_are_never_excluded(self):
        for plant in self.PLANTS:
            for description in [
                "ZINC OXIDE", "Precipitated Silica", "NBR 2675", "SBR 1502",
                "Carbon Black N-330", "RECLAIM RUBBER 8MPA", "Natural Rubber ISNR-20", "",
            ]:
                assert not_stocked_material_entry(description, plant) is None, f"{plant}: {description}"

    def test_a_grade_code_alone_does_not_read_as_conveyor_fabric(self):
        # The fabric pattern is EE/NN/EP plus 2-3 digits. A chemical grade
        # that happens to carry digits must not trip it.
        for plant in self.PLANTS:
            for description in ["Aksil 180 G", "TECHNIC TR-100", "ULTRA LUB-250", "PVC FR 102"]:
                assert not_stocked_material_entry(description, plant) is None, f"{plant}: {description}"
