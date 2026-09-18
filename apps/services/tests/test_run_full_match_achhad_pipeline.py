"""
Integration tests for apps/services/matching_achhad.py's run_full_match() -
mirrors test_run_full_match_pipeline.py's HRS conventions (real DB rows via
Model.objects.create(), @pytest.mark.django_db, no mocking), adjusted for
what's genuinely different about Achhad's own matching config
(apps/services/matching_achhad.py's _MatchConfig): stock_vendor_field=None,
since RTPAchhadRMLot has no vendor/party_name column at all - MIR<->Stock
here gates on material description alone (a real, weaker-confidence
guarantee than HRS's (material, vendor) gate, documented in that module's
own docstring, not a test gap).
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    RTPAchhadDomesticPOLineItem,
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadPOMirMatch,
    RTPAchhadRMLot,
)
from apps.services.matching_achhad import run_full_match


def _make_po(po_number="4000000551", vendor_name="Ganesh Chemicals Ltd"):
    return RTPAchhadDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name=vendor_name,
        tax_type="IGST", total_value=Decimal("40000.00"), total_inclusive_value=Decimal("47200.00"),
    )


def _make_po_line_item(po, description="Stearic Acid", qty=Decimal("500"), net_price=Decimal("80.00")):
    return RTPAchhadDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="3823",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _make_mir_entry(
    party_name="Ganesh Chemicals Ltd", po_number_raw="4000000551",
    material_description="Stearic Acid", qty=Decimal("500"), rate=Decimal("80.00"),
    mir_date=datetime.date(2026, 5, 1), source_row_ref="3",
):
    return RTPAchhadMIREntry.objects.create(
        month="May-26", mir_no="AMR001", mir_date=mir_date, party_name=party_name,
        po_number_raw=po_number_raw, material_description=material_description,
        qty=qty, uom="KG", rate=rate, net=qty * rate, taxable_value=qty * rate,
        invoice_final_value=qty * rate, source_row_ref=source_row_ref, is_active=True,
    )


def _make_stock_lot(description="Stearic Acid", rate=Decimal("80.00"), received=Decimal("0"), received_date=None, sap_code="H1"):
    return RTPAchhadRMLot.objects.create(
        description=description, sap_code=sap_code, rate=rate, received=received,
        todays_stock=Decimal("5000"), received_date=received_date,
        natural_key=f"achhad:{sap_code}:{description}", is_active=True,
    )


@pytest.mark.django_db
class TestAchhadPoMirMatching:
    def test_exact_po_number_match_with_a_qty_mismatch_is_flagged(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("500"))
        _make_mir_entry(qty=Decimal("480"))

        run_full_match()

        match = RTPAchhadPOMirMatch.objects.get(po_line_item=line_item)
        assert match.tier == RTPAchhadPOMirMatch.Tier.PO_NUMBER
        assert match.qty_mismatched is True
        assert match.is_flagged is True

    # ── Identification 2-of-3 (2026-09-18, Achhad only) ────────────────────
    # Achhad is the one plant on
    # matching_core._MatchConfig.identification_two_of_three, so vendor is a
    # VOTE here, not a veto. The three tests below pin the rule at its two
    # edges - what a disagreeing vendor can now be outvoted by, and what it
    # still cannot - because those edges are the whole safety argument. See
    # that flag's own comment for the live Achhad rows each one stands for.

    def test_different_vendor_is_outvoted_by_po_number_plus_material(self):
        """MIR 96/05's shape: the party name is wrong, the PO number and the
        material are right, and the money agrees to the rupee. Two of three
        identify it, and vendor_matched records that the name disagreed so
        someone can fix it at source."""
        po = _make_po(vendor_name="Ganesh Chemicals Ltd")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="A Completely Different Vendor Ltd")

        run_full_match()

        match = RTPAchhadPOMirMatch.objects.get(po_line_item=line_item)
        assert match.po_number_matched is True
        assert match.material_matched is True
        assert match.vendor_matched is False

    def test_different_vendor_with_only_a_po_number_never_matches(self):
        """MIR 74/06's shape, and the case this rule exists to REFUSE: a
        mistyped PO number pointing at another supplier's order, with the
        material disagreeing and nothing else corroborating. One of three is
        not identification - leaving it unmatched is the honest outcome."""
        po = _make_po(vendor_name="Ganesh Chemicals Ltd")
        line_item = _make_po_line_item(po, description="Stearic Acid")
        _make_mir_entry(
            party_name="A Completely Different Vendor Ltd",
            material_description="Ammonium Polyphosphate",
        )

        run_full_match()

        assert not RTPAchhadPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_different_vendor_with_only_a_material_never_matches(self):
        """The mirror of the test above: the same common material bought
        from two suppliers is not evidence on its own, and never was."""
        po = _make_po(vendor_name="Ganesh Chemicals Ltd")
        line_item = _make_po_line_item(po, description="Stearic Acid")
        _make_mir_entry(
            party_name="A Completely Different Vendor Ltd",
            po_number_raw="4000000999",
        )

        run_full_match()

        assert not RTPAchhadPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_vendor_matched_is_true_on_an_ordinary_match(self):
        po = _make_po()
        line_item = _make_po_line_item(po)
        _make_mir_entry()

        run_full_match()

        assert RTPAchhadPOMirMatch.objects.get(po_line_item=line_item).vendor_matched is True

    def test_legacy_slashed_po_number_folds_across_the_two_file_formats(self):
        """The master CSV writes 'RTP2/HO/26-27/ENGG-0007' where the MIR
        sheet writes 'Eng/0007/2026-27' - the same Achhad order. Before
        parsers.common.legacy_po_matches() the token comparison read that as
        no PO evidence at all."""
        po = _make_po(po_number="RTP2/HO/26-27/ENGG-0007")
        line_item = _make_po_line_item(po)
        _make_mir_entry(po_number_raw="Eng/0007/2026-27")

        run_full_match()

        match = RTPAchhadPOMirMatch.objects.get(po_line_item=line_item)
        assert match.po_number_matched is True
        assert match.tier == RTPAchhadPOMirMatch.Tier.PO_NUMBER

    def test_no_po_vendor_row_is_reconciled_once_it_names_an_order_we_hold(self):
        """parsers.common's NO_PO_VENDORS registry drops a party's MIR rows
        from the PO<->MIR pool entirely. When that party starts being PO'd -
        as JMF Performance Materials and Eternia Trading now are at Achhad -
        the rows naming one of our orders have to come back on their own,
        or they stay silently excluded until someone remembers to edit that
        list. See _MirCandidateIndex's docstring."""
        po = _make_po(vendor_name="JMF Performance Materials Pvt. Ltd.")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="JMF Performance Materials Pvt. Ltd.")

        run_full_match()

        assert RTPAchhadPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_no_po_vendor_row_without_a_po_reference_stays_excluded(self):
        """The other half of the rule above - the ordinary internal-transfer
        case, which is what the registry is actually for, is untouched."""
        po = _make_po(vendor_name="JMF Performance Materials Pvt. Ltd.")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="JMF Performance Materials Pvt. Ltd.", po_number_raw="")

        run_full_match()

        assert not RTPAchhadPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_po()
        line_item = _make_po_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert RTPAchhadPOMirMatch.objects.filter(po_line_item=line_item).count() == 1


@pytest.mark.django_db
class TestAchhadMirStockMatching:
    def test_material_alone_is_enough_to_match_no_vendor_field_exists(self):
        """RTPAchhadRMLot has no vendor column at all - confirmed directly:
        stock_vendor_field is None in matching_achhad.py's _MatchConfig, so
        this pairing must succeed purely on normalized material description."""
        mir_entry = _make_mir_entry(material_description="Stearic Acid")
        _make_stock_lot(description="Stearic Acid")

        run_full_match()

        assert RTPAchhadMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_different_material_never_matches(self):
        mir_entry = _make_mir_entry(material_description="Stearic Acid")
        _make_stock_lot(description="Zinc Oxide")

        run_full_match()

        assert not RTPAchhadMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_rate_mismatch_is_flagged_only_when_dates_confirm_the_same_delivery(self):
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("80.00"))
        _make_stock_lot(rate=Decimal("90.00"), received_date=datetime.date(2026, 5, 1))

        run_full_match()

        match = RTPAchhadMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is True
        assert match.rate_mismatched is True
        assert match.is_flagged is True

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        mir_entry = _make_mir_entry()
        _make_stock_lot()

        run_full_match()
        run_full_match()

        assert RTPAchhadMirStockMatch.objects.filter(mir_entry=mir_entry).count() == 1
