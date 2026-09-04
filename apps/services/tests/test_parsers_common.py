"""
Unit tests for apps/services/parsers/common.py - the pure, dependency-free
parsing helpers every plant's parser is built on. Mirrors the TDS Automation
App's apps/services/tests/test_calculations.py convention: pure-function
unit tests for the calculation/normalization layer, no Django DB, no
mocking.

These functions were deliberately written with zero Django imports so they
could be tested with nothing but plain Python (see common.py's own module
docstring) - this file is that promise being kept.
"""
import datetime
from decimal import Decimal

from apps.services.parsers.common import (
    normalize_material,
    normalize_vendor,
    to_code_str,
    to_date,
    to_decimal,
    to_str,
    tokenize,
)


class TestToDecimal:
    def test_plain_number_string(self):
        assert to_decimal("950") == Decimal("950")

    def test_strips_comma_thousands_separators(self):
        # Real source data shape - MIR sheets store amounts as ' 109,250.00 '.
        assert to_decimal(" 109,250.00 ") == Decimal("109250.00")

    def test_literal_null_string_becomes_none(self):
        # 'NULL' is the literal placeholder used throughout these source files.
        assert to_decimal("NULL") is None
        assert to_decimal("null") is None

    def test_blank_and_none_become_none(self):
        assert to_decimal("") is None
        assert to_decimal("   ") is None
        assert to_decimal(None) is None

    def test_unparseable_value_becomes_none_not_a_crash(self):
        assert to_decimal("not a number") is None

    def test_int_and_float_coerce_through_str(self):
        assert to_decimal(950) == Decimal("950")
        assert to_decimal(75.5) == Decimal("75.5")


class TestToStr:
    def test_strips_whitespace(self):
        assert to_str("  Sulphur Powder  ") == "Sulphur Powder"

    def test_null_and_none_become_empty_string(self):
        assert to_str("NULL") == ""
        assert to_str(None) == ""

    def test_case_insensitive_null_check(self):
        assert to_str("null") == ""
        assert to_str("Null") == ""


class TestToDate:
    def test_iso_format(self):
        assert to_date("2026-04-01") == datetime.date(2026, 4, 1)

    def test_slash_dmy_format(self):
        assert to_date("01/04/2026") == datetime.date(2026, 4, 1)

    def test_dot_dmy_format(self):
        assert to_date("01.04.2026") == datetime.date(2026, 4, 1)

    def test_real_datetime_object_returns_date_part(self):
        assert to_date(datetime.datetime(2026, 4, 1, 13, 30)) == datetime.date(2026, 4, 1)

    def test_real_date_object_passes_through(self):
        assert to_date(datetime.date(2026, 4, 1)) == datetime.date(2026, 4, 1)

    def test_unparseable_value_becomes_none_not_a_crash(self):
        # A real MIR typo (delivery date printed earlier than the PO's own
        # created date) must not crash a whole sync - see common.py's docstring.
        assert to_date("not a date") is None

    def test_null_and_blank_become_none(self):
        assert to_date("NULL") is None
        assert to_date("") is None
        assert to_date(None) is None


class TestNormalizeVendor:
    def test_strips_legal_suffixes_case_insensitively(self):
        assert normalize_vendor("Kedar Metals Pvt Ltd") == normalize_vendor("KEDAR METALS PVT. LTD.")

    def test_produces_same_normalized_form_for_both(self):
        assert normalize_vendor("Kedar Metals Pvt Ltd") == "kedarmetals"

    def test_the_real_hrs_stock_city_suffix_case(self):
        # Confirmed real-data case from CLAUDE.md: HRS's Stock sheet appends
        # a city suffix that MIR/PO data doesn't carry. Both must still
        # normalize to a value where one contains the other (that containment
        # check lives in matching.py's _vendor_matches, not here - this just
        # confirms the normalized forms line up the way that check relies on).
        mir_side = normalize_vendor("Rubamin Private Limited")
        stock_side = normalize_vendor("Rubamin Private Limited - Vadodara")
        assert mir_side in stock_side

    def test_blank_and_none_become_empty_string(self):
        assert normalize_vendor("") == ""
        assert normalize_vendor(None) == ""


class TestNormalizeMaterial:
    def test_lowercases_and_collapses_punctuation_to_single_spaces(self):
        assert normalize_material("SBR 1502 - SILVASSA") == "sbr 1502 silvassa"

    def test_blank_and_none_become_empty_string(self):
        assert normalize_material("") == ""
        assert normalize_material(None) == ""


class TestTokenize:
    def test_splits_normalized_text_into_words(self):
        assert tokenize("Sulphur Powder") == ["sulphur", "powder"]

    def test_empty_string_returns_empty_list(self):
        assert tokenize("") == []


class TestToCodeStr:
    def test_whole_number_float_drops_trailing_zero(self):
        # openpyxl hands back a float for a numeric-looking cell with no text
        # formatting - confirmed on Achhad's Stock 'SAP Code' column, e.g.
        # 22001002.0 - a plain to_str() would keep the '.0'.
        assert to_code_str(22001002.0) == "22001002"

    def test_non_integer_float_falls_back_to_plain_str(self):
        assert to_code_str(22001002.5) == "22001002.5"

    def test_string_value_passes_through_to_str(self):
        assert to_code_str("3000001075") == "3000001075"
