"""
Pipeline tests for the MIR<->Stock three-tier identification added
2026-09-21 - the half of that change that only shows up against real rows:
which tier fires, best-evidence filtering, tier-1 exclusivity, and the
NO_RM_STOCK_VENDORS exclusion.

The string-level rules (cleaning, fuzzy tokens, grade-code contradiction) are
pinned dependency-free in test_mir_stock_identification.py; this file is the
other half, needing a real DB because run_full_match()/match_mir_entry_stock()
query the ORM directly.

Run against HRS, whose config has a vendor gate, an extended field set and a
date+rate path all enabled - the widest combination of the three plants. Row
builders follow test_run_full_match_pipeline.py's conventions (direct
Model.objects.create(), no factories).
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import HRSMIREntry, HRSMirStockMatch, HRSRMLot
from apps.services.matching import MATCH_CONFIG
from apps.services.matching_core import match_mir_entry_stock

VENDOR = "Rubamin Private Limited"
LOT_VENDOR = "Rubamin Private Limited - Vadodara"  # the Stock sheet's city suffix
MIR_DATE = datetime.date(2026, 5, 1)


def _mir(
    material_description, rate=Decimal("100.00"), qty=Decimal("1000"),
    party_name=VENDOR, mir_date=MIR_DATE, source_row_ref="7", uom="KG",
):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no="MIR001", mir_date=mir_date, party_name=party_name,
        po_number_raw="", material_description=material_description, qty=qty, uom=uom,
        rate=rate, net=qty * rate, taxable_value=qty * rate, invoice_final_value=qty * rate,
        source_row_ref=source_row_ref, is_active=True,
    )


def _lot(
    description, basic_rate=Decimal("100.00"), party_name=LOT_VENDOR,
    received_date=None, sap_item_code="H1", uom="KG",
):
    return HRSRMLot.objects.create(
        description=description, sap_item_code=sap_item_code, party_name=party_name,
        basic_rate=basic_rate, received=Decimal("0"), todays_stock=Decimal("5000"),
        received_date=received_date, natural_key=f"hrs:{sap_item_code}:{description}",
        is_active=True, uom=uom,
    )


def _match(entry):
    """Run the single-entry path and return the lots it matched."""
    matches = match_mir_entry_stock(MATCH_CONFIG, entry)
    return {m.stock_lot.description for m in matches}


@pytest.mark.django_db
class TestTiers:
    def test_tier3_identical_names_match(self):
        # The only rule that existed before 2026-09-21 - it must still work.
        _lot("SBR 1502")
        assert _match(_mir("SBR 1502")) == {"SBR 1502"}

    def test_tier2_word_order_differs(self):
        # Real shape: Vapi MIR '8MPA RECLAIM RUBBER' vs Stock 'RECLAIM
        # RUBBER 8MPA'. Exact equality found nothing here.
        _lot("RECLAIM RUBBER 8MPA")
        assert _match(_mir("8MPA RECLAIM RUBBER")) == {"RECLAIM RUBBER 8MPA"}

    def test_tier2_forgives_a_typo(self):
        # Real pair: HRS Stock's 'PRECIPITATD SILICA' against MIR's
        # 'Precipitated Silica'.
        _lot("PRECIPITATD SILICA")
        assert _match(_mir("Precipitated Silica")) == {"PRECIPITATD SILICA"}

    def test_tier1_date_and_rate_identify_when_names_do_not(self):
        # Real pair: Achhad's 'Kanatol-8A (DOA)' against stock's 'DOA Oil'.
        # Nothing in the text agrees; the receipt date and the rate do.
        _lot("DOA Oil", basic_rate=Decimal("172.00"), received_date=MIR_DATE)
        assert _match(_mir("Kanatol-8A (DOA)", rate=Decimal("172.00"))) == {"DOA Oil"}

    def test_tier1_needs_the_rate_too_not_just_the_date(self):
        # Date alone must NEVER identify - one vendor delivers several
        # different materials on one day (see match_mir_entry_stock()).
        _lot("China Clay Powder", basic_rate=Decimal("45.00"), received_date=MIR_DATE)
        assert _match(_mir("VULKACIT MBTS", rate=Decimal("172.00"))) == set()

    def test_tier1_needs_the_date_too_not_just_the_rate(self):
        _lot("DOA Oil", basic_rate=Decimal("172.00"), received_date=datetime.date(2026, 4, 1))
        assert _match(_mir("Kanatol-8A (DOA)", rate=Decimal("172.00"))) == set()


@pytest.mark.django_db
class TestGradeCodeGateInThePipeline:
    def test_a_grade_disagreement_blocks_the_date_rate_path(self):
        # THE case this gate exists for: same vendor, same day, same price,
        # different grade. Really matched before the gate existed.
        _lot("NBR 3345", basic_rate=Decimal("226.50"), received_date=MIR_DATE)
        assert _match(_mir("NBR 2675", rate=Decimal("226.50"))) == set()

    def test_the_same_grade_still_matches(self):
        _lot("NBR 2675", basic_rate=Decimal("226.50"), received_date=MIR_DATE)
        assert _match(_mir("NBR 2675", rate=Decimal("226.50"))) == {"NBR 2675"}


@pytest.mark.django_db
class TestBestEvidenceOnly:
    def test_an_exact_name_beats_a_merely_similar_one(self):
        _lot("SBR 1502", sap_item_code="H1")
        _lot("SBR 1502 GRADE A", sap_item_code="H2")
        # Both clear the similarity threshold; only the exact one is kept.
        assert _match(_mir("SBR 1502")) == {"SBR 1502"}

    def test_several_lots_of_the_same_material_all_survive(self):
        # This pairing is many-to-many BY DESIGN - the same material really
        # does arrive into several lots over time. Best-evidence filtering
        # must not collapse that: these tie, so they all stay.
        _lot("SBR 1502", sap_item_code="H1")
        _lot("SBR 1502", sap_item_code="H2")
        assert _match(_mir("SBR 1502")) == {"SBR 1502"}
        assert HRSMirStockMatch.objects.count() == 2

    def test_a_named_match_is_never_given_up_for_a_date_rate_one(self):
        _lot("SBR 1502", sap_item_code="H1", basic_rate=Decimal("999.00"))
        _lot("DOA Oil", sap_item_code="H2", basic_rate=Decimal("100.00"), received_date=MIR_DATE)
        assert _match(_mir("SBR 1502", rate=Decimal("100.00"))) == {"SBR 1502"}


@pytest.mark.django_db
class TestTierOneExclusivity:
    def test_a_lot_goes_to_the_row_that_actually_describes_it(self):
        # Real failure this prevents: 'Eva Bag 20"X20"' and 'Eva Bag 24"X36"'
        # arrived the same day at the same price and SWAPPED lots, each
        # "identified" on date+rate alone. The row naming the lot wins it.
        _lot("EVA BAG", basic_rate=Decimal("198.00"), received_date=MIR_DATE)
        describes_it = _mir("EVA BAG", rate=Decimal("198.00"), source_row_ref="7")
        other = _mir("WOODEN STOPPER", rate=Decimal("198.00"), source_row_ref="8")

        assert _match(describes_it) == {"EVA BAG"}
        assert _match(other) == set()


@pytest.mark.django_db
class TestNoRmStockVendorExclusion:
    def test_madura_is_excluded_even_from_a_perfect_match(self):
        # Madura's conveyor fabric reaches no plant's RM sheet - 830 rows,
        # zero lots. Registered, so it never matches, however well a row
        # would otherwise line up.
        _lot("EE 100/163 CM", party_name="Madura Industrial Textiles Ltd.")
        entry = _mir("EE 100/163 CM", party_name="Madura Industrial Textiles Ltd.")
        assert _match(entry) == set()

    def test_the_exclusion_deletes_a_match_written_before_registration(self):
        # Returning [] rather than skipping the call is what makes the
        # caller's stale-row cleanup fire - see match_mir_entry_stock().
        lot = _lot("EE 100/163 CM", party_name="Madura Industrial Textiles Ltd.")
        entry = _mir("EE 100/163 CM", party_name="Madura Industrial Textiles Ltd.")
        HRSMirStockMatch.objects.create(mir_entry=entry, stock_lot=lot)
        assert HRSMirStockMatch.objects.count() == 1

        match_mir_entry_stock(MATCH_CONFIG, entry)

        assert HRSMirStockMatch.objects.count() == 0

    def test_an_unregistered_vendor_is_unaffected(self):
        _lot("SBR 1502")
        assert _match(_mir("SBR 1502")) == {"SBR 1502"}


@pytest.mark.django_db
class TestVendorGateStillHolds:
    def test_a_different_vendor_never_matches_however_well_it_lines_up(self):
        _lot("SBR 1502", party_name="Kedar Metals Pvt Ltd - Daman",
             basic_rate=Decimal("100.00"), received_date=MIR_DATE)
        assert _match(_mir("SBR 1502", rate=Decimal("100.00"))) == set()

    def test_the_city_suffix_containment_arm_still_works(self):
        _lot("SBR 1502", party_name=LOT_VENDOR)
        assert _match(_mir("SBR 1502", party_name=VENDOR)) == {"SBR 1502"}
