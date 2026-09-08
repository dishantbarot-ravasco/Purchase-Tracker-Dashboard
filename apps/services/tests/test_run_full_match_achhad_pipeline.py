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

    def test_different_vendor_never_matches(self):
        po = _make_po(vendor_name="Ganesh Chemicals Ltd")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="A Completely Different Vendor Ltd")

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
