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

    # ── Identification 2-of-3 (2026-09-18) ─────────────────────────────────
    # Achhad is on matching_core._MatchConfig.identification_two_of_three
    # (HRS joined the same day, Vapi has not), so vendor is a
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


@pytest.mark.django_db
class TestWithinTierMaterialTieBreak:
    """Several lines of ONE purchase order, all confirmed by the same PO
    number against that order's own MIR rows (2026-09-18).

    Every pair lands in the same evidence tier, so the tier separates
    nothing; and when the lines share a rate the money separates nothing
    either. Material similarity is the only field left that can tell them
    apart, and until _pair_weight() included it the pairing was arbitrary.

    Real case this reproduces: Achhad PO 1000001471 (Madura Industrial
    Textiles), three rolls of EE-080 fabric at 140/158/168 cm, each matched
    to the wrong width in a clean one-place shift down the list, at a
    material similarity of 0.05.
    """

    def test_lines_of_one_po_pair_by_material_not_arbitrarily(self):
        """Every quantity and rate here is IDENTICAL on purpose, so the
        financial score cannot separate any pair from any other. Material is
        then the only signal left, which is precisely the situation the real
        defect arose in - and before _pair_weight() read it, the pairing was
        whatever the assignment happened to settle on."""
        po = _make_po(po_number="1000001471", vendor_name="Madura Industrial Textiles Ltd")
        qty, rate = Decimal("1000"), Decimal("220")
        narrow = RTPAchhadDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="1",
            description="EE-080 fabric roll, width 140cm, GSM 320", hsn="5911",
            qty=qty, uom="KG", net_price=rate, net_value=qty * rate)
        wide = RTPAchhadDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="2",
            description="EE-080 fabric roll, width 168cm, GSM 320", hsn="5911",
            qty=qty, uom="KG", net_price=rate, net_value=qty * rate)

        for mir_no, desc, ref in (
            ("M-140", "Rubberised Textile Fabric-EE80,140cm", "3"),
            ("M-168", "Rubberised Textile Fabric-EE80,168cm", "4"),
        ):
            RTPAchhadMIREntry.objects.create(
                month="May-26", mir_no=mir_no, mir_date=datetime.date(2026, 5, 1),
                party_name="Madura Industrial Textiles Ltd", po_number_raw="1000001471",
                material_description=desc, qty=qty, uom="KG", rate=rate,
                net=qty * rate, taxable_value=qty * rate, invoice_final_value=qty * rate,
                source_row_ref=ref, is_active=True)

        run_full_match()

        assert RTPAchhadPOMirMatch.objects.get(po_line_item=narrow).mir_entry.mir_no == "M-140"
        assert RTPAchhadPOMirMatch.objects.get(po_line_item=wide).mir_entry.mir_no == "M-168"

    def test_material_never_outranks_a_whole_evidence_tier(self):
        """The guarantee the 2026-09-12 redesign exists to give, re-checked
        now that material is back in the within-tier weight: a PERFECT
        material match with no PO number must still lose to a PO-number
        confirmed candidate, however weak that one's other evidence."""
        from apps.services.matching_core import (
            TIER_RANK_MATERIAL_ONLY, TIER_RANK_PO_NUMBER, _Candidate, _pair_weight)

        material_only = _Candidate(
            mir=None, tier_rank=TIER_RANK_MATERIAL_ONLY, score=Decimal("1"),
            coverage=Decimal("1"), date_weight=Decimal("1"),
            material_matched=True, po_number_matched=False, material_score=Decimal("1"))
        po_confirmed = _Candidate(
            mir=None, tier_rank=TIER_RANK_PO_NUMBER, score=Decimal("0"),
            coverage=Decimal("0"), date_weight=Decimal("0"),
            material_matched=False, po_number_matched=True, material_score=Decimal("0"))

        assert _pair_weight(po_confirmed) > _pair_weight(material_only)
