"""
Integration tests for apps/services/matching_vapi.py's run_full_match() -
mirrors test_run_full_match_pipeline.py's HRS conventions (real DB rows via
Model.objects.create(), @pytest.mark.django_db, no mocking), adjusted for
what's genuinely different about Vapi's own matching config
(apps/services/matching_vapi.py's _MatchConfig): mir_value reads
mir.taxable_value (RTPVapiMIREntry has no `net` field to fall back to -
confirmed by reading the model directly, no fields named `net` exist),
stock_vendor_field="supplier_name" (a real vendor gate, unlike Achhad),
and material_match_threshold=0.2 (lower than HRS/Achhad's 0.3, a deliberate
real-data-tuned difference per that file's own comment). po_number_raw is
~100% blank on real Vapi data (per CLAUDE.md's Match Accuracy section), so
these tests deliberately exercise the TIER_MATERIAL path, not PO_NUMBER -
that's the realistic path for this plant, not an oversight.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    RTPVapiDomesticPOLineItem,
    RTPVapiDomesticPurchaseOrder,
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiPOMirMatch,
    RTPVapiRMLot,
)
from apps.services.matching_vapi import run_full_match


def _make_po(po_number="5000000221", vendor_name="Vapi Polymers Pvt Ltd"):
    return RTPVapiDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name=vendor_name,
        tax_type="IGST", total_value=Decimal("120000.00"), total_inclusive_value=Decimal("141600.00"),
    )


def _make_po_line_item(po, description="NBR 3345", qty=Decimal("800"), net_price=Decimal("150.00")):
    return RTPVapiDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="4002",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _make_mir_entry(
    party_name="Vapi Polymers Pvt Ltd", po_number_raw="",  # blank on real Vapi data - never a reliable join key
    material_description="NBR 3345", qty=Decimal("800"), rate=Decimal("150.00"),
    mir_date=datetime.date(2026, 5, 1), source_row_ref="2",
):
    taxable_value = qty * rate
    return RTPVapiMIREntry.objects.create(
        month="May-26", mir_no="MIR01/01", mir_date=mir_date, party_name=party_name,
        po_number_raw=po_number_raw, material_description=material_description,
        qty=qty, uom="KG", rate=rate, taxable_value=taxable_value,
        gst_rate_pct=Decimal("18.00"), invoice_final_value=taxable_value * Decimal("1.18"),
        source_row_ref=source_row_ref, is_active=True,
    )


def _make_stock_lot(description="NBR 3345", supplier_name="Vapi Polymers Pvt Ltd - Vapi", basic_rate=Decimal("150.00"), received=Decimal("0"), received_date=None, hsn_code="H1"):
    return RTPVapiRMLot.objects.create(
        description=description, hsn_code=hsn_code, supplier_name=supplier_name,
        basic_rate=basic_rate, received=received, todays_stock=Decimal("5000"),
        received_date=received_date, natural_key=f"vapi:{hsn_code}:{description}", is_active=True,
    )


@pytest.mark.django_db
class TestVapiPoMirMatching:
    def test_material_match_with_a_qty_mismatch_is_flagged(self):
        """po_number_raw is ~100% blank on real Vapi data - matching runs
        entirely on the material-description identification path, not the
        PO-number shortcut (see CLAUDE.md's Match Accuracy section)."""
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("800"))
        _make_mir_entry(qty=Decimal("750"), po_number_raw="")

        run_full_match()

        match = RTPVapiPOMirMatch.objects.get(po_line_item=line_item)
        assert match.tier == RTPVapiPOMirMatch.Tier.MATERIAL
        assert match.po_number_matched is False
        assert match.material_matched is True
        assert match.qty_mismatched is True
        assert match.is_flagged is True

    def test_different_vendor_never_matches(self):
        po = _make_po(vendor_name="Vapi Polymers Pvt Ltd")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="A Totally Different Vendor Ltd")

        run_full_match()

        assert not RTPVapiPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_value_compares_against_taxable_value_not_a_net_field(self):
        """RTPVapiMIREntry has no `net` column at all (unlike HRS/Achhad) -
        mir_value in matching_vapi.py's _MatchConfig reads mir.taxable_value
        instead. Confirmed both ways: an exact PO-net-value-vs-MIR-taxable-
        value match doesn't flag a data mismatch, and a genuine mismatch
        between them does - proving the comparison is actually wired to
        taxable_value (a test that only checked the non-flagged case
        wouldn't tell a broken/absent comparator apart from a correct one)."""
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("800"), net_price=Decimal("150.00"))  # net_value = 120000
        _make_mir_entry(qty=Decimal("800"), rate=Decimal("150.00"))  # taxable_value = 120000, matches exactly

        run_full_match()

        match = RTPVapiPOMirMatch.objects.get(po_line_item=line_item)
        assert match.qty_mismatched is False
        assert match.rate_mismatched is False
        assert match.data_mismatch is False
        assert match.value_diff_pct == 0

    def test_a_genuine_taxable_value_mismatch_sets_data_mismatch(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("800"), net_price=Decimal("150.00"))  # net_value = 120000
        _make_mir_entry(qty=Decimal("800"), rate=Decimal("100.00"))  # taxable_value = 80000 - real gap, not rounding

        run_full_match()

        match = RTPVapiPOMirMatch.objects.get(po_line_item=line_item)
        assert match.data_mismatch is True
        assert match.value_diff_pct is not None and match.value_diff_pct > 0

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_po()
        line_item = _make_po_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert RTPVapiPOMirMatch.objects.filter(po_line_item=line_item).count() == 1


@pytest.mark.django_db
class TestVapiMirStockMatching:
    def test_vendor_gate_uses_containment_not_equality(self):
        """Vapi's Stock sheet has a real Supplier Name vendor column
        (supplier_name) - confirmed genuine per CLAUDE.md, unlike Achhad's
        vendor-less Stock sheet. Gate uses the same containment rule as
        HRS's _vendor_matches(), not strict equality."""
        mir_entry = _make_mir_entry(party_name="Vapi Polymers Pvt Ltd")
        _make_stock_lot(supplier_name="Vapi Polymers Pvt Ltd - Vapi")

        run_full_match()

        assert RTPVapiMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_different_vendor_never_matches(self):
        mir_entry = _make_mir_entry(party_name="Vapi Polymers Pvt Ltd")
        _make_stock_lot(supplier_name="A Totally Different Vendor Ltd")

        run_full_match()

        assert not RTPVapiMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_rate_mismatch_is_flagged_only_when_dates_confirm_the_same_delivery(self):
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("150.00"))
        _make_stock_lot(basic_rate=Decimal("160.00"), received_date=datetime.date(2026, 5, 1))

        run_full_match()

        match = RTPVapiMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is True
        assert match.rate_mismatched is True
        assert match.is_flagged is True

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        mir_entry = _make_mir_entry()
        _make_stock_lot()

        run_full_match()
        run_full_match()

        assert RTPVapiMirStockMatch.objects.filter(mir_entry=mir_entry).count() == 1
