"""
Integration tests for apps/services/matching.py's run_full_match() (HRS) -
previously zero coverage at the "runs against real DB rows and writes real
match rows" level. apps/services/tests/test_matching.py already covers the
pure, dependency-free scoring helpers (_closeness/_diff_pct/_vendor_matches/
etc, imported straight from matching_core with no DB) - this file is the
other half CLAUDE.md's "Known gaps" flags as missing: the actual pipeline
effect of calling run_full_match() against real HRSDomesticPOLineItem/
HRSMIREntry/HRSRMLot rows, which needs a real Postgres DB
(@pytest.mark.django_db) since matching_core.run_full_match() queries the
ORM directly rather than accepting in-memory objects.

No factories exist for these models (only apps/api/tests/factories.py's
make_user()) - built directly via Model.objects.create(), same convention
apps/api/tests/test_hrs_correct_field.py already uses for this app's other
HRS models.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSRMLot,
)
from apps.services.matching import run_full_match


def _make_po(po_number="3000001104", vendor_name="Rubamin Private Limited"):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}",
        po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25),
        vendor_name=vendor_name,
        tax_type="IGST",
        total_value=Decimal("100000.00"),
        total_inclusive_value=Decimal("118000.00"),
    )


def _make_po_line_item(po, description="SBR 1502", qty=Decimal("1000"), net_price=Decimal("100.00")):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="4002",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _make_mir_entry(
    party_name="Rubamin Private Limited", po_number_raw="3000001104",
    material_description="SBR 1502", qty=Decimal("1000"), rate=Decimal("100.00"),
    mir_date=datetime.date(2026, 5, 1), source_row_ref="7",
):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no="MIR001", mir_date=mir_date, party_name=party_name,
        po_number_raw=po_number_raw, material_description=material_description,
        qty=qty, uom="KG", rate=rate, net=qty * rate, taxable_value=qty * rate,
        invoice_final_value=qty * rate, source_row_ref=source_row_ref, is_active=True,
    )


def _make_stock_lot(
    description="SBR 1502", party_name="Rubamin Private Limited - Vadodara",
    basic_rate=Decimal("100.00"), received=Decimal("0"), received_date=None, sap_item_code="H1",
):
    return HRSRMLot.objects.create(
        description=description, sap_item_code=sap_item_code, party_name=party_name,
        basic_rate=basic_rate, received=received, todays_stock=Decimal("5000"),
        received_date=received_date, natural_key=f"hrs:{sap_item_code}:{description}",
        is_active=True,
    )


@pytest.mark.django_db
class TestPoMirMatching:
    def test_exact_po_number_match_with_a_qty_mismatch_is_flagged(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("1000"))
        _make_mir_entry(qty=Decimal("990"))  # 10 units short of the PO's 1000

        run_full_match()

        match = HRSPOMirMatch.objects.get(po_line_item=line_item)
        assert match.tier == HRSPOMirMatch.Tier.PO_NUMBER
        assert match.po_number_matched is True
        assert match.qty_mismatched is True
        assert match.is_flagged is True
        assert match.qty_diff_pct is not None and match.qty_diff_pct > 0

    def test_an_exact_match_on_every_field_is_never_flagged(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("1000"), net_price=Decimal("100.00"))
        _make_mir_entry(qty=Decimal("1000"), rate=Decimal("100.00"))

        run_full_match()

        match = HRSPOMirMatch.objects.get(po_line_item=line_item)
        assert match.qty_mismatched is False
        assert match.rate_mismatched is False
        assert match.is_flagged is False

    def test_different_vendor_never_matches_even_with_identical_po_number_and_material(self):
        po = _make_po(vendor_name="Rubamin Private Limited")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="Some Totally Different Vendor Ltd")

        run_full_match()

        assert not HRSPOMirMatch.objects.filter(po_line_item=line_item).exists(), (
            "vendor is a mandatory hard gate - a matching PO number and material must not be "
            "enough on their own (see matching_core.py's _identification_pool() docstring)"
        )

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_po()
        line_item = _make_po_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert HRSPOMirMatch.objects.filter(po_line_item=line_item).count() == 1


@pytest.mark.django_db
class TestMirStockMatching:
    def test_rate_mismatch_is_flagged_only_when_dates_confirm_the_same_delivery(self):
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("100.00"))
        _make_stock_lot(basic_rate=Decimal("105.00"), received_date=datetime.date(2026, 5, 1))

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is True
        assert match.rate_mismatched is True
        assert match.is_flagged is True
        assert match.rate_diff_pct is not None and match.rate_diff_pct > 0

    def test_rate_mismatch_is_not_flagged_when_dates_do_not_confirm_the_same_delivery(self):
        """A stock lot's own rate reflects its MOST RECENT receipt, which can
        be months apart from the specific delivery an MIR row recorded (see
        match_mir_entry_stock()'s own docstring, with a real HRS example of
        commodity price drift being misread as a data error) - rate is only
        compared when Rec. DT. actually confirms it's the same event."""
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("100.00"))
        _make_stock_lot(basic_rate=Decimal("105.00"), received_date=datetime.date(2026, 8, 13))

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is False
        assert match.rate_mismatched is False
        assert match.is_flagged is False
        assert match.rate_diff_pct is None

    def test_vendor_gate_uses_containment_not_equality(self):
        """HRS's Stock sheet appends a city suffix its MIR party_name never
        carries (e.g. '... - Vadodara') - confirmed live-data quirk per
        _vendor_matches()'s own docstring; the gate must still pass."""
        mir_entry = _make_mir_entry(party_name="Rubamin Private Limited")
        _make_stock_lot(party_name="Rubamin Private Limited - Vadodara")

        run_full_match()

        assert HRSMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        mir_entry = _make_mir_entry()
        _make_stock_lot()

        run_full_match()
        run_full_match()

        assert HRSMirStockMatch.objects.filter(mir_entry=mir_entry).count() == 1
