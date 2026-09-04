"""
Unit tests for apps/services/import_flags.py - pure functions, no Django DB
(see that module's docstring). Uses a plain namespace as a stand-in for a
line-item model instance, since these functions only ever read attributes.
"""
import datetime
from decimal import Decimal
from types import SimpleNamespace

from apps.services import import_flags as f

TODAY = datetime.date(2026, 9, 4)


def item(**overrides):
    defaults = dict(
        item_id="1", description="Widget", hsn="12345678",
        qty_as_per_po=Decimal("100"), qty_as_per_boe=None, uom="KG",
        delivery_date=None, delivery_date_raw="", net_price=Decimal("1.5"), net_value=Decimal("150"),
        tax_type="", currency_after_taxes="", exchange_rate=None, total_inclusive_value=None,
        boe_number="", bill_of_lading_number="", laden_on_board_date=None,
        country_of_origin="China", license_type="", license_number="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestShipmentStage:
    def test_placed_when_nothing_filed(self):
        assert f.shipment_stage(item()) == f.STAGE_PLACED

    def test_shipped_when_bl_present_but_no_boe(self):
        assert f.shipment_stage(item(bill_of_lading_number="BL1")) == f.STAGE_SHIPPED

    def test_shipped_when_laden_date_present_but_no_boe(self):
        assert f.shipment_stage(item(laden_on_board_date=TODAY)) == f.STAGE_SHIPPED

    def test_cleared_when_boe_present(self):
        assert f.shipment_stage(item(boe_number="BOE1")) == f.STAGE_CLEARED

    def test_po_rollup_is_least_advanced_stage(self):
        items = [item(boe_number="BOE1"), item(bill_of_lading_number="BL1")]
        assert f.po_shipment_stage(items) == f.STAGE_SHIPPED


class TestQtyDiscrepancy:
    def test_no_discrepancy_when_boe_qty_missing(self):
        is_disc, pct = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=None))
        assert is_disc is False
        assert pct is None

    def test_discrepancy_when_qty_differs(self):
        is_disc, pct = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("90")))
        assert is_disc is True
        assert pct == -10.0

    def test_no_discrepancy_when_qty_matches(self):
        is_disc, _ = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100")))
        assert is_disc is False


class TestDeliveryDateStatus:
    def test_delivered_once_cleared_regardless_of_date(self):
        i = item(boe_number="BOE1", delivery_date=TODAY + datetime.timedelta(days=30))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_DELIVERED

    def test_unknown_when_no_delivery_date(self):
        assert f.delivery_date_status(item(delivery_date=None), TODAY) == f.STATUS_UNKNOWN

    def test_overdue_when_date_in_past_and_not_cleared(self):
        i = item(delivery_date=TODAY - datetime.timedelta(days=1))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_OVERDUE

    def test_on_order_when_date_today_or_future_and_not_cleared(self):
        i = item(delivery_date=TODAY)
        assert f.delivery_date_status(i, TODAY) == f.STATUS_ON_ORDER


class TestPartialDelivery:
    def test_true_when_boe_qty_short_of_po_qty(self):
        items = [item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("80"))]
        assert f.partial_delivery(items) is True

    def test_false_when_boe_qty_meets_or_exceeds_po_qty(self):
        items = [item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100"))]
        assert f.partial_delivery(items) is False

    def test_false_when_boe_qty_not_yet_present(self):
        items = [item(qty_as_per_po=Decimal("100"), qty_as_per_boe=None)]
        assert f.partial_delivery(items) is False


class TestDataQualityFlags:
    def test_f1_boe_filed_but_qty_missing(self):
        flags = f.item_flags(item(boe_number="BOE1", qty_as_per_boe=None))
        assert any(fl["code"] == "F1" for fl in flags)

    def test_f2_boe_filed_but_landed_cost_incomplete(self):
        flags = f.item_flags(item(boe_number="BOE1", tax_type="", exchange_rate=None))
        assert any(fl["code"] == "F2" for fl in flags)

    def test_f4_shipped_without_bl(self):
        flags = f.item_flags(item(laden_on_board_date=TODAY, bill_of_lading_number=""))
        assert any(fl["code"] == "F4" for fl in flags)

    def test_f5_hsn_malformed(self):
        assert any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="1234")))
        assert not any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="12345678")))
        assert not any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="123456")))

    def test_f6_currency_after_taxes_blank_despite_boe(self):
        flags = f.item_flags(item(boe_number="BOE1", currency_after_taxes=""))
        assert any(fl["code"] == "F6" for fl in flags)

    def test_f7_delivery_date_unparseable(self):
        flags = f.item_flags(item(delivery_date=None, delivery_date_raw="End Mar/Early Apr 2026"))
        assert any(fl["code"] == "F7" for fl in flags)

    def test_blank_payment_terms_is_not_a_flag(self):
        # Payment Terms isn't even a field import_flags looks at - this test
        # documents that omission is deliberate (spec: explicitly NOT a flag).
        flags = f.item_flags(item())
        assert not any("payment terms" in fl["message"].lower() for fl in flags)

    def test_f3_inconsistent_completion_across_items_same_po_boe(self):
        complete = item(item_id="1", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("1000"))
        incomplete = item(item_id="2", boe_number="BOE1", tax_type="", exchange_rate=None, total_inclusive_value=None)
        flags = f.po_flags("PO1", [complete, incomplete])
        assert any(fl["code"] == "F3" for fl in flags)

    def test_f3_not_flagged_when_all_items_consistent(self):
        a = item(item_id="1", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("1000"))
        b = item(item_id="2", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("2000"))
        flags = f.po_flags("PO1", [a, b])
        assert not any(fl["code"] == "F3" for fl in flags)
