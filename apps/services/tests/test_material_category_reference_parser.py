"""
Unit tests for apps/services/parsers/material_category_reference.py - pure,
dependency-free parsing (no DB), same convention as test_parsers_common.py.
"""
from apps.services.parsers.material_category_reference import (
    EXPECTED_HEADER,
    HeaderMismatch,
    _split_subcategory,
    _strip_trailing_code_suffix,
    parse_material_category_reference_csv,
)

_HEADER_LINE = ",".join(EXPECTED_HEADER)


class TestStripTrailingCodeSuffix:
    def test_strips_trailing_parenthetical_item_code(self):
        assert _strip_trailing_code_suffix("SACK CARBON (22001065)") == "SACK CARBON"

    def test_description_with_no_suffix_is_unchanged(self):
        assert _strip_trailing_code_suffix("Natural Rubber") == "Natural Rubber"

    def test_only_strips_the_trailing_suffix_not_an_internal_parenthetical(self):
        # A real shape from the reference list: "Magnesium Hydroxide Precipitated (MDH) (22002241)"
        assert (
            _strip_trailing_code_suffix("Magnesium Hydroxide Precipitated (MDH) (22002241)")
            == "Magnesium Hydroxide Precipitated (MDH)"
        )


class TestSplitSubcategory:
    def test_splits_label_and_code(self):
        assert _split_subcategory("CARBON BLACK (RM-CB001)") == ("CARBON BLACK", "RM-CB001")

    def test_truncated_label_still_splits(self):
        # Real shape from the reference list - the label itself is truncated
        # by the source system, not something this parser should "fix".
        assert _split_subcategory("RECOVERED CARBON BLA (RM-CB002)") == ("RECOVERED CARBON BLA", "RM-CB002")

    def test_no_parenthetical_code_returns_label_only(self):
        assert _split_subcategory("Just A Label") == ("Just A Label", "")


class TestParseMaterialCategoryReferenceCsv:
    def test_header_mismatch_raises(self):
        try:
            parse_material_category_reference_csv("Wrong,Header\nfoo,bar\n")
            assert False, "expected HeaderMismatch"
        except HeaderMismatch:
            pass

    def test_parses_a_real_shaped_row(self):
        csv_text = (
            _HEADER_LINE + "\n"
            '22001109,KETJEN BLACK EC 300-JD (22001109),38159000,Carbon Black,'
            '"CARBON BLACK (RM-CB001)",Kilogram (KG)\n'
        )
        [row] = parse_material_category_reference_csv(csv_text)
        assert row.description == "KETJEN BLACK EC 300-JD"
        assert row.normalized_description == "ketjen black ec 300 jd"
        assert row.category == "Carbon Black"
        assert row.subcategory == "CARBON BLACK"
        assert row.subcategory_code == "RM-CB001"
        assert row.hsn_code == "38159000"
        assert row.sap_item_code == "22001109"

    def test_blank_description_row_is_skipped(self):
        csv_text = _HEADER_LINE + "\n,,,,,\n"
        assert parse_material_category_reference_csv(csv_text) == []

    def test_two_rows_with_different_descriptions_normalize_differently(self):
        # Guards against the normalized key accidentally collapsing distinct
        # materials together.
        csv_text = (
            _HEADER_LINE + "\n"
            "1,Natural Rubber ISNR 10 (1),,Natural Rubber,NATURAL RUBBER ISNR (RM-NR001),KG\n"
            "2,Natural Rubber ISNR 20 (2),,Natural Rubber,NATURAL RUBBER ISNR (RM-NR001),KG\n"
        )
        rows = parse_material_category_reference_csv(csv_text)
        assert {r.normalized_description for r in rows} == {"natural rubber isnr 10", "natural rubber isnr 20"}
