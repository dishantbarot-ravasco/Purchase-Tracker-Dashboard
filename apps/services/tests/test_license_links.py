"""
Tests for apps/services/license_links.py - the join between an import PO
line item's `License Type`/`License Number` columns and the two company-wide
licence ledgers.

Split the way this directory's own convention splits (see CLAUDE.md's
"Testing"): the normalisation/splitting/classification rules are pure
functions and get DB-free tests, while collect_citations() seeds real rows.

Every literal licence number here is one that appears in the live imports
data, not an invented shape - the two normalisation rules exist because of
these exact values.
"""

import pytest

from apps.core.models import HRSImportPOLineItem, HRSImportPurchaseOrder
from apps.services import license_links as ll


class TestClassifyScheme:
    def test_the_two_values_the_csv_actually_carries(self):
        assert ll.classify_scheme("RODTEP") == ll.SCHEME_RODTEP
        assert ll.classify_scheme("ADVANCE") == ll.SCHEME_ADVANCE

    def test_blank_is_unknown_not_an_error(self):
        assert ll.classify_scheme("") == ll.SCHEME_UNKNOWN
        assert ll.classify_scheme(None) == ll.SCHEME_UNKNOWN

    def test_matched_by_substring_so_a_longer_spelling_still_classifies(self):
        """The whole reason classify_scheme() isn't an equality test: these
        are hand-typed cells, and 'RODTEP SCRIP' must not fall into UNKNOWN
        and silently drop out of the ledger it belongs to."""
        assert ll.classify_scheme("RODTEP SCRIP") == ll.SCHEME_RODTEP
        assert ll.classify_scheme("  advance licence  ") == ll.SCHEME_ADVANCE
        assert ll.classify_scheme("Advance Authorisation") == ll.SCHEME_ADVANCE

    def test_an_unrecognised_scheme_is_unknown_rather_than_guessed(self):
        assert ll.classify_scheme("EPCG") == ll.SCHEME_UNKNOWN


class TestNormalizeLicenseNumber:
    def test_zero_pads_a_short_all_digit_number(self):
        """Rule 2. The live CSV writes the same authorisation both ways -
        '311051817' on one line and '0311051817' inside a slash-joined cell
        on another - and left alone they read as two licences."""
        assert ll.normalize_license_number("311051817") == "0311051817"
        assert ll.normalize_license_number("0311051817") == "0311051817"
        assert ll.normalize_license_number("311051817") == ll.normalize_license_number("0311051817")

    def test_a_rodtep_scrip_is_already_ten_digits_and_is_unchanged(self):
        assert ll.normalize_license_number("2603043916") == "2603043916"

    def test_trims_but_never_pads_a_non_numeric_value(self):
        assert ll.normalize_license_number("  aa/123  ") == "AA/123"

    def test_blank_stays_blank(self):
        assert ll.normalize_license_number("") == ""
        assert ll.normalize_license_number(None) == ""


class TestSplitLicenseNumbers:
    def test_single_number(self):
        assert ll.split_license_numbers("2603043916") == ["2603043916"]

    def test_splits_a_real_slash_joined_cell(self):
        assert ll.split_license_numbers("0311051817/0311055303") == ["0311051817", "0311055303"]

    def test_splits_the_widest_real_cell_and_normalises_each_token(self):
        raw = "0311047672/0311047922/0311048316/0311048705/0311049174/0311049210"
        assert ll.split_license_numbers(raw) == [
            "0311047672", "0311047922", "0311048316", "0311048705", "0311049174", "0311049210",
        ]

    def test_de_duplicates_after_normalising(self):
        """A cell naming the same authorisation twice, once padded and once
        not, is one licence - not two, and not one plus a phantom."""
        assert ll.split_license_numbers("311051817/0311051817") == ["0311051817"]

    def test_a_cell_that_does_not_split_cleanly_is_left_whole(self):
        """The guard. An unexpected shape stays intact and unmatched rather
        than being shredded into tokens that match nothing - the failure mode
        the multi-PO hyphen work hit before it was guarded."""
        assert ll.split_license_numbers("HO/26-27/003") == ["HO/26-27/003"]
        assert ll.split_license_numbers("0311051817/pending") == ["0311051817/PENDING"]

    def test_a_hyphen_never_splits(self):
        """Nothing has ever shown a hyphen joining licence numbers, and it
        does appear inside other identifier series in this data."""
        assert ll.split_license_numbers("1000001552-1000001630") == ["1000001552-1000001630"]

    def test_blank(self):
        assert ll.split_license_numbers("") == []
        assert ll.split_license_numbers("   ") == []


def _make_line(license_type="RODTEP", license_number="2603043916", po_number="1000001357", **overrides):
    po, _ = HRSImportPurchaseOrder.objects.get_or_create(
        po_number=po_number,
        defaults=dict(po_drive_folder_name=po_number, vendor_name="Test Vendor"),
    )
    defaults = dict(
        purchase_order=po, item_id="1", description="NEOPRENE RUBBER GNA",
        license_type=license_type, license_number=license_number,
        boe_number="8826527", qty_as_per_boe="100.000", uom="KG",
        total_inclusive_value="9215578.00",
    )
    defaults.update(overrides)
    return HRSImportPOLineItem.objects.create(**defaults)


@pytest.mark.django_db
class TestCollectCitations:
    def test_one_citation_per_line_item(self):
        _make_line()
        citations = ll.collect_citations(ll.SCHEME_RODTEP)
        assert len(citations) == 1
        assert citations[0].license_number == "2603043916"
        assert citations[0].plant_key == "hrs"
        assert citations[0].po_number == "1000001357"
        assert citations[0].boe_number == "8826527"
        assert citations[0].shared_with == ()

    def test_a_multi_licence_line_produces_one_citation_per_licence(self):
        _make_line(license_number="2603043916/2603044111")
        citations = ll.collect_citations(ll.SCHEME_RODTEP)
        assert {c.license_number for c in citations} == {"2603043916", "2603044111"}
        # Each one knows the other is on the same line - which is what stops
        # the landed value being read as either licence's own share.
        assert citations[0].shared_with == ("2603044111",)
        assert citations[1].shared_with == ("2603043916",)

    def test_the_verbatim_cell_is_preserved_on_every_citation(self):
        _make_line(license_number="311051817", license_type="ADVANCE")
        citation = ll.collect_citations(ll.SCHEME_ADVANCE)[0]
        assert citation.license_number == "0311051817"
        assert citation.license_number_raw == "311051817"

    def test_scheme_filter(self):
        _make_line(license_type="RODTEP", license_number="2603043916", po_number="PO-R")
        _make_line(license_type="ADVANCE", license_number="0311051817", po_number="PO-A")
        assert len(ll.collect_citations(ll.SCHEME_RODTEP)) == 1
        assert len(ll.collect_citations(ll.SCHEME_ADVANCE)) == 1
        assert len(ll.collect_citations()) == 2

    def test_a_line_naming_a_licence_under_no_scheme_is_still_collected(self):
        """It names a number, so somebody meant something by it - it must be
        surfaceable as "fill in the License Type column", not dropped."""
        _make_line(license_type="", license_number="0311051817")
        citations = ll.collect_citations(ll.SCHEME_UNKNOWN)
        assert len(citations) == 1
        assert citations[0].scheme == ll.SCHEME_UNKNOWN

    def test_a_line_with_no_licence_number_is_not_collected(self):
        _make_line(license_type="", license_number="")
        assert ll.collect_citations() == []

    def test_a_retired_po_is_excluded(self):
        """Same is_active filter everything downstream of the PO sync uses -
        a renamed order's ghost must not go on citing a licence."""
        line = _make_line()
        line.purchase_order.is_active = False
        line.purchase_order.save(update_fields=["is_active"])
        assert ll.collect_citations() == []


@pytest.mark.django_db
class TestCitationTotals:
    def test_counts_lines_pos_and_plants_and_sums_landed_value(self):
        _make_line(po_number="PO-1", total_inclusive_value="100.00")
        _make_line(po_number="PO-2", item_id="2", total_inclusive_value="250.00")
        totals = ll.citation_totals(ll.collect_citations(ll.SCHEME_RODTEP))
        assert totals["lineCount"] == 2
        assert totals["poCount"] == 2
        assert totals["plants"] == ["HRS-Silvassa"]
        assert totals["landedValue"] == 350
        assert totals["sharedLines"] == 0

    def test_shared_lines_are_counted_so_the_value_can_be_qualified(self):
        _make_line(license_number="2603043916/2603044111")
        grouped = ll.citations_by_license(ll.collect_citations(ll.SCHEME_RODTEP))
        totals = ll.citation_totals(grouped["2603043916"])
        assert totals["lineCount"] == 1
        assert totals["sharedLines"] == 1

    def test_a_null_landed_value_does_not_break_the_sum(self):
        _make_line(total_inclusive_value=None)
        assert ll.citation_totals(ll.collect_citations(ll.SCHEME_RODTEP))["landedValue"] == 0
