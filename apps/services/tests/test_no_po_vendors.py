"""
Unit tests for the NO_PO_VENDORS registry in apps/services/parsers/common.py -
the vendors no plant raises a purchase order against, whose MIR rows
matching_core._MirCandidateIndex drops from the PO<->MIR candidate pool.

No Django DB and no mocking for the registry half, same as
test_parsers_common.py: the registry and its two lookup functions live in the
dependency-free parsers/common.py on purpose, and this file is that promise
being kept. The matcher-wiring half does seed rows, since the bug worth
catching there is the wiring rather than the lookup.

The tests below are weighted towards STRUCTURAL properties of the registry
rather than spot-checks of individual entries. An entry is a business fact the
project owner supplies and can change; the properties - exact matching only,
no accidental collision with a real supplier, group companies never filed as
third-party suppliers - are what must hold no matter who edits the list next.
"""

import pytest

from apps.services.parsers.common import (
    INTERNAL_TRANSFER,
    NO_PO_SUPPLIER,
    NO_PO_VENDORS,
    _normalize_vendor_for_matching_base,
    is_no_po_vendor,
    no_po_vendor_entry,
)


class TestNoPoVendorLookup:
    def test_a_registered_vendor_is_found(self):
        category, reason = no_po_vendor_entry("Harsha Impex")
        assert category == NO_PO_SUPPLIER
        assert reason

    def test_lookup_is_case_and_punctuation_insensitive(self):
        """The registry is keyed on the normalized name, so one entry covers
        every casing/punctuation spelling of it - which is what makes the
        exact-matching rule affordable. Real spellings: Vapi's MIR shouts
        'HINDUSTAN RUBBERS (SILVASSA)', Achhad's writes it in title case."""
        assert is_no_po_vendor("HINDUSTAN RUBBERS (SILVASSA)")
        assert is_no_po_vendor("Hindustan Rubbers (Silvassa)")

    def test_one_list_covers_all_three_plants(self):
        """The registry is deliberately NOT scoped per plant (see its header):
        these vendors are no-PO everywhere, so the same name must resolve the
        same way regardless of which plant's MIR row it came from. This test
        exists to make a future re-introduction of per-plant scoping a
        deliberate decision rather than a quiet one."""
        for name in ("Harsha Impex", "Gangamani Enterprise Pvt Ltd", "Hindustan Rubbers (Silvassa)"):
            assert is_no_po_vendor(name), name

    def test_blank_name_is_not_registered(self):
        """Returns None rather than raising - a MIR row with no party name is
        a data problem for the sync layer to surface, not a crash here."""
        assert no_po_vendor_entry("") is None
        assert no_po_vendor_entry(None) is None

    def test_an_ordinary_supplier_is_not_registered(self):
        for real_supplier in (
            "Atul Limited",
            "Anupam Colours Private Limited",
            "Balkrishna Industries Limited",
            "Rubamin Private Limited",
            "SRF Limited",
            "Madura Industrial Textiles Ltd",
        ):
            assert not is_no_po_vendor(real_supplier), real_supplier


class TestNormalizationCollapsesRealVariants:
    """Exact matching only pays off because normalization already folds the
    ordinary spelling drift. These pin the real variants seen in the three
    plants' MIR tables, so a change to the normalization rules that quietly
    splits one of them fails here rather than silently re-admitting those rows
    to the candidate pool."""

    @pytest.mark.parametrize("variant", [
        "STAR POLYMER",
        "STAR POLYMERS INC.",
        "Star Polymers Inc.",
    ])
    def test_star_polymers_spellings_collapse_to_one_entry(self, variant):
        assert is_no_po_vendor(variant)

    @pytest.mark.parametrize("variant", ["K-Flex", "Kflex", "K FLEX"])
    def test_kflex_spellings_collapse_to_one_entry(self, variant):
        assert is_no_po_vendor(variant)

    @pytest.mark.parametrize("variant", [
        "Tinna Rubber and Infrastructure Ltd",
        "Tinna Rubber And Infrastructure Limited",
    ])
    def test_tinna_spellings_collapse_to_one_entry(self, variant):
        assert is_no_po_vendor(variant)

    @pytest.mark.parametrize("variant", [
        "2M Elastomers Private Limited",
        "2M Elastomers Pvt ltd",
    ])
    def test_2m_elastomers_spellings_collapse_to_one_entry(self, variant):
        assert is_no_po_vendor(variant)

    @pytest.mark.parametrize("variant", [
        "Ravasco Transmission & Packing Private Limited",
        "Ravasco Transmission & Packing Pvt Ltd",
        "Ravasco Transmission And Packing Private Limited",
        # "Packaging" is a different word, not a fold-able variant - it earns
        # its own registry entry, and this asserts that entry exists.
        "Ravasco Transmission and Packaging Pvt Ltd",
        # Vapi's MIR appends the originating plant.
        "Ravasco Transmission And Packing Pvt Ltd ACHHAD",
    ])
    def test_every_real_ravasco_spelling_is_covered(self, variant):
        assert is_no_po_vendor(variant)

    @pytest.mark.parametrize("variant", [
        "JMF Performance Materials Pvt. Ltd.",
        # Achhad's MIR misspells it on every real row. The similarity arm that
        # would normally absorb this is deliberately not used here, so the
        # typo needs its own entry.
        "JMF Perfomance Materials Pvt Ltd",
    ])
    def test_both_jmf_spellings_are_covered(self, variant):
        assert is_no_po_vendor(variant)


class TestExactMatchingIsLoadBearing:
    """The registry deliberately does NOT use matching_core's _vendor_matches()
    gate - no containment, no 0.90-similarity arm. These pin that, because the
    failure it prevents is silent: a false positive here removes a real
    supplier's receipts from reconciliation entirely."""

    def test_containment_does_not_catch_an_unrelated_supplier(self):
        """'Harsha Impex' is registered; a different company whose normalized
        name merely CONTAINS a registered one must not be caught."""
        assert is_no_po_vendor("Harsha Impex")
        assert not is_no_po_vendor("Harsha Impex Global Trading Company")
        assert not is_no_po_vendor("Sumitra Enterprise Chemicals Pvt Ltd")

    def test_a_near_miss_spelling_is_not_registered(self):
        """No similarity arm either. 'Ravasco Transmission And Packing Pvt Ltd
        ACHHAD' scores 0.897 against 'Ravasco Transmission & Packing Pvt Ltd' -
        close enough that the ordinary vendor gate nearly accepts it. Under
        exact matching each real spelling needs its own entry, which is why
        both are listed; an unrelated near-miss must still be rejected."""
        assert not is_no_po_vendor("Ravasco Transmissions Limited Bangalore")
        assert not is_no_po_vendor("Star Polymer Additives Private Limited")


class TestRegistryStructure:
    def test_keys_are_stored_pre_normalized(self):
        """A key that isn't already normalized can never be hit by the lookup.
        Cheap structural guard against a malformed addition, same shape as
        test_parsers_common.py's equivalent check on VENDOR_ALIASES."""
        for key in NO_PO_VENDORS:
            assert key == _normalize_vendor_for_matching_base(key), f"{key!r} not normalized"

    def test_every_entry_declares_a_known_category_and_a_reason(self):
        """The category is not decoration - INTERNAL_TRANSFER is permanent,
        NO_PO_SUPPLIER is a process gap that has to be revisited when that
        supplier starts being PO'd. An entry with neither is unmaintainable."""
        for key, (category, reason) in NO_PO_VENDORS.items():
            assert category in (INTERNAL_TRANSFER, NO_PO_SUPPLIER), f"{key}: {category!r}"
            assert reason.strip(), f"{key} has no reason"

    def test_both_categories_are_actually_used(self):
        """If one category ever empties out, the split has stopped earning its
        keep and the next reader should know to re-decide rather than
        cargo-cult it."""
        assert {c for c, _ in NO_PO_VENDORS.values()} == {INTERNAL_TRANSFER, NO_PO_SUPPLIER}

    def test_group_companies_are_internal_transfers_never_suppliers(self):
        """The inverse of test_parsers_common.py's
        test_alias_map_never_targets_a_group_company, and the same hard rule
        underneath: the company's own plants are not third parties. Recording
        one as NO_PO_SUPPLIER would mean someone later 'fixes the process' by
        raising POs for an inter-unit stock movement."""
        group = ("ravasco", "hindustanrubber")
        for key, (category, _reason) in NO_PO_VENDORS.items():
            if any(g in key for g in group):
                assert category == INTERNAL_TRANSFER, f"{key} is a group company but marked {category}"

    def test_registry_stays_small(self):
        """A soft ceiling, not a rule about the business. Exact matching means
        this list grows one line per spelling, and the honest failure mode is
        someone pasting a whole vendor master in here - at which point the
        registry stops being "the handful of vendors with no PO" and quietly
        becomes a way to hide unmatched rows. Raise this deliberately if the
        real list genuinely grows."""
        assert len(NO_PO_VENDORS) <= 40


class TestMatcherExclusion:
    """_MirCandidateIndex is where the registry actually changes behaviour.
    These use the real index against seeded rows rather than asserting on the
    registry alone, because the bug worth catching is the wiring."""

    @pytest.mark.django_db
    def test_index_drops_no_po_vendor_rows_and_keeps_the_rest(self):
        from apps.core.models import HRSMIREntry
        from apps.services.matching import MATCH_CONFIG
        from apps.services.matching_core import _MirCandidateIndex

        HRSMIREntry.objects.create(
            mir_no="M-REAL", party_name="Atul Limited",
            material_description="Sulphur", is_active=True, source_row_ref="r1",
        )
        HRSMIREntry.objects.create(
            mir_no="M-INTERNAL", party_name="Ravasco Transmission & Packing Pvt Ltd",
            material_description="Sulphur", is_active=True, source_row_ref="r2",
        )
        HRSMIREntry.objects.create(
            mir_no="M-NOPO", party_name="Harsha Impex",
            material_description="Sulphur", is_active=True, source_row_ref="r3",
        )

        assert {r.mir_no for r in _MirCandidateIndex(MATCH_CONFIG)._rows} == {"M-REAL"}

    @pytest.mark.django_db
    def test_exclusion_applies_to_every_plants_index(self):
        """One shared registry means all three matchers must behave the same
        way - a per-plant config that somehow bypassed it would be invisible
        at runtime, since matching would just quietly keep the rows."""
        from apps.core.models import HRSMIREntry, RTPAchhadMIREntry, RTPVapiMIREntry
        from apps.services.matching import MATCH_CONFIG as HRS
        from apps.services.matching_achhad import MATCH_CONFIG as ACHHAD
        from apps.services.matching_core import _MirCandidateIndex
        from apps.services.matching_vapi import MATCH_CONFIG as VAPI

        for model, config in ((HRSMIREntry, HRS), (RTPAchhadMIREntry, ACHHAD), (RTPVapiMIREntry, VAPI)):
            model.objects.create(
                mir_no="M-REAL", party_name="Atul Limited",
                material_description="Sulphur", is_active=True, source_row_ref="r1",
            )
            model.objects.create(
                mir_no="M-NOPO", party_name="Hindustan Rubbers (Silvassa)",
                material_description="Sulphur", is_active=True, source_row_ref="r2",
            )
            pooled = {r.mir_no for r in _MirCandidateIndex(config)._rows}
            assert pooled == {"M-REAL"}, config.mir_model.__name__
