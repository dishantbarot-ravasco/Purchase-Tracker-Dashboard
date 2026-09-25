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
    """Build a SimpleNamespace shaped like an Import PO line-item model
    instance, with sane defaults - import_flags.py's functions only ever
    read attributes off their argument, so a real Django model isn't needed."""
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


# ── Shipment-stage progression (Placed -> Shipped -> Cleared) ──────────────────

class TestShipmentStage:
    def test_placed_when_nothing_filed(self):
        """No BL, laden date, or BOE at all - the item hasn't left the vendor yet."""
        assert f.shipment_stage(item()) == f.STAGE_PLACED

    def test_shipped_when_bl_present_but_no_boe(self):
        """A bill of lading number, with no BOE yet, means it's in transit."""
        assert f.shipment_stage(item(bill_of_lading_number="BL1")) == f.STAGE_SHIPPED

    def test_shipped_when_laden_date_present_but_no_boe(self):
        """A laden-on-board date is an equally valid "shipped" signal, independent of the BL number."""
        assert f.shipment_stage(item(laden_on_board_date=TODAY)) == f.STAGE_SHIPPED

    def test_cleared_when_boe_present(self):
        """A BOE number means customs clearance has happened - the final stage."""
        assert f.shipment_stage(item(boe_number="BOE1")) == f.STAGE_CLEARED

    def test_po_rollup_is_least_advanced_stage(self):
        """A PO's overall stage is only as advanced as its least-advanced line
        item - one cleared item and one merely-shipped item rolls up to Shipped."""
        items = [item(boe_number="BOE1"), item(bill_of_lading_number="BL1")]
        assert f.po_shipment_stage(items) == f.STAGE_SHIPPED


# ── Qty discrepancy (PO qty vs. BOE-cleared qty) ────────────────────────────────

class TestQtyDiscrepancy:
    def test_no_discrepancy_when_boe_qty_missing(self):
        """Not yet cleared through customs - there's no BOE qty to compare
        against yet, so this must never read as a discrepancy."""
        is_disc, pct = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=None))
        assert is_disc is False
        assert pct is None

    def test_discrepancy_when_qty_differs(self):
        """A real gap between ordered and BOE-cleared qty is flagged, with
        the signed percentage difference returned alongside the flag."""
        is_disc, pct = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("90")))
        assert is_disc is True
        assert pct == -10.0

    def test_no_discrepancy_when_qty_matches(self):
        """An exact match between PO qty and BOE qty is never flagged."""
        is_disc, _ = f.qty_discrepancy(item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100")))
        assert is_disc is False


# ── Delivery-date status (On order / Overdue / Delivered / Unknown) ────────────

def mir_match(qty_diff_pct=Decimal("0"), qty_over_delivered=None, dismissed=False):
    """Stand-in for an Import PO<->MIR match row - only the attributes
    import_flags.py reads off `item.mir_match`."""
    return SimpleNamespace(qty_diff_pct=qty_diff_pct, qty_over_delivered=qty_over_delivered,
                           dismissed_by_override=dismissed)


class TestDeliveryDateStatus:
    def test_delivered_once_fully_received_in_mir_regardless_of_date(self):
        """A line fully received in MIR is Delivered even if its own delivery
        date is still in the future."""
        i = item(boe_number="BOE1", delivery_date=TODAY + datetime.timedelta(days=30), mir_match=mir_match())
        assert f.delivery_date_status(i, TODAY) == f.STATUS_DELIVERED

    def test_cleared_but_not_in_mir_is_overdue_once_past_due(self):
        """Customs clearance is not delivery. A cleared line with no MIR
        receipt, past its date, is Overdue - the old rule called it
        Delivered, so 15 of Vapi's 38 cleared POs could never read overdue."""
        i = item(boe_number="BOE1", delivery_date=TODAY - datetime.timedelta(days=1))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_OVERDUE

    def test_short_receipt_is_not_delivered(self):
        """A line received short in MIR is still outstanding."""
        i = item(delivery_date=TODAY - datetime.timedelta(days=1),
                 mir_match=mir_match(qty_diff_pct=Decimal("20"), qty_over_delivered=False))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_OVERDUE

    def test_over_receipt_is_delivered(self):
        """Over-delivery still means the material arrived."""
        i = item(delivery_date=TODAY - datetime.timedelta(days=1),
                 mir_match=mir_match(qty_diff_pct=Decimal("20"), qty_over_delivered=True))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_DELIVERED

    def test_dismissed_match_is_not_a_receipt(self):
        """A reviewer-dismissed pairing is wrong, not an arrival."""
        i = item(delivery_date=TODAY - datetime.timedelta(days=1), mir_match=mir_match(dismissed=True))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_OVERDUE

    def test_unknown_when_no_delivery_date(self):
        """No parseable delivery date at all yields Unknown, not a guess."""
        assert f.delivery_date_status(item(delivery_date=None), TODAY) == f.STATUS_UNKNOWN

    def test_overdue_when_date_in_past_and_not_received(self):
        """A delivery date already in the past, with no MIR receipt, is Overdue."""
        i = item(delivery_date=TODAY - datetime.timedelta(days=1))
        assert f.delivery_date_status(i, TODAY) == f.STATUS_OVERDUE

    def test_on_order_when_date_today_or_future_and_not_received(self):
        """A delivery date today or later, not yet received, is still On Order."""
        i = item(delivery_date=TODAY)
        assert f.delivery_date_status(i, TODAY) == f.STATUS_ON_ORDER


# ── Partial delivery and Material Inwarded across a whole PO (MIR-based) ───────

class TestPartialDelivery:
    def test_true_when_one_line_received_and_another_not(self):
        items = [item(mir_match=mir_match()), item()]
        assert f.partial_delivery(items) is True

    def test_true_when_the_only_line_arrived_short(self):
        items = [item(mir_match=mir_match(qty_diff_pct=Decimal("10"), qty_over_delivered=False))]
        assert f.partial_delivery(items) is True

    def test_false_when_every_line_fully_received(self):
        items = [item(mir_match=mir_match()), item(mir_match=mir_match(qty_diff_pct=Decimal("5"), qty_over_delivered=True))]
        assert f.partial_delivery(items) is False

    def test_false_when_boe_short_but_nothing_in_mir(self):
        """A BOE quantity below the PO quantity is a partial SHIPMENT, which
        the PO-vs-BOE qty card already counts - not a partial delivery. The
        old rule returned True here."""
        items = [item(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("80"))]
        assert f.partial_delivery(items) is False

    def test_false_when_only_receipt_is_dismissed(self):
        items = [item(mir_match=mir_match(dismissed=True)), item()]
        assert f.partial_delivery(items) is False


class TestMaterialInwarded:
    def test_true_only_when_every_line_fully_received(self):
        assert f.material_inwarded([item(mir_match=mir_match()), item(mir_match=mir_match())]) is True

    def test_false_when_any_line_has_no_receipt(self):
        """The old Import card counted a PO as soon as ANY line had a
        receipt, while its tooltip said every line."""
        assert f.material_inwarded([item(mir_match=mir_match()), item()]) is False

    def test_false_when_a_line_is_short(self):
        items = [item(mir_match=mir_match(qty_diff_pct=Decimal("10"), qty_over_delivered=False))]
        assert f.material_inwarded(items) is False

    def test_undetermined_direction_is_not_invented_as_short(self):
        items = [item(mir_match=mir_match(qty_diff_pct=Decimal("10"), qty_over_delivered=None))]
        assert f.material_inwarded(items) is True

    def test_false_for_a_po_with_no_lines(self):
        assert f.material_inwarded([]) is False


# ── Data quality flags (F1-F7 codes, per import_flags.py's own rule table) ─────

class TestDataQualityFlags:
    def test_f1_boe_filed_but_qty_missing(self):
        """F1: a BOE number was filed but qty_as_per_boe is still blank -
        customs clearance recorded without the cleared quantity."""
        flags = f.item_flags(item(boe_number="BOE1", qty_as_per_boe=None))
        assert any(fl["code"] == "F1" for fl in flags)

    def test_f2_boe_filed_but_landed_cost_incomplete(self):
        """F2: cleared (BOE present) but the landed-cost fields (tax type,
        exchange rate) that should come with clearance are still empty."""
        flags = f.item_flags(item(boe_number="BOE1", tax_type="", exchange_rate=None))
        assert any(fl["code"] == "F2" for fl in flags)

    def test_f4_shipped_without_bl(self):
        """F4: a laden-on-board date exists (shipped) but no bill-of-lading
        number was ever filed for it."""
        flags = f.item_flags(item(laden_on_board_date=TODAY, bill_of_lading_number=""))
        assert any(fl["code"] == "F4" for fl in flags)

    def test_f5_hsn_malformed(self):
        """F5: HSN codes must be exactly 8 digits - both a too-short (4) and
        a too-long (relative to 8) value should flag, while the correct
        length (8) and another valid shorter code (6) must not."""
        assert any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="1234")))
        assert not any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="12345678")))
        assert not any(fl["code"] == "F5" for fl in f.item_flags(item(hsn="123456")))

    def test_f6_currency_after_taxes_blank_despite_boe(self):
        """F6: cleared (BOE present) but currency_after_taxes was never filled in."""
        flags = f.item_flags(item(boe_number="BOE1", currency_after_taxes=""))
        assert any(fl["code"] == "F6" for fl in flags)

    def test_f7_delivery_date_unparseable(self):
        """F7: the source sheet had a delivery-date cell that couldn't be
        parsed into a real date (e.g. free-text like "End Mar/Early Apr
        2026") - delivery_date is None but delivery_date_raw still holds the
        original text, which is what should trigger this flag."""
        flags = f.item_flags(item(delivery_date=None, delivery_date_raw="End Mar/Early Apr 2026"))
        assert any(fl["code"] == "F7" for fl in flags)

    def test_blank_payment_terms_is_not_a_flag(self):
        """Payment Terms isn't even a field import_flags looks at - this test
        documents that omission is deliberate (spec: explicitly NOT a flag)."""
        flags = f.item_flags(item())
        assert not any("payment terms" in fl["message"].lower() for fl in flags)

    def test_f3_inconsistent_completion_across_items_same_po_boe(self):
        """F3: two line items sharing the same PO+BOE should have consistent
        landed-cost completeness - one fully filled in and one still blank
        under the same BOE is an inconsistency worth flagging at the PO level."""
        complete = item(item_id="1", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("1000"))
        incomplete = item(item_id="2", boe_number="BOE1", tax_type="", exchange_rate=None, total_inclusive_value=None)
        flags = f.po_flags("PO1", [complete, incomplete])
        assert any(fl["code"] == "F3" for fl in flags)

    def test_f3_not_flagged_when_all_items_consistent(self):
        """Two items under the same BOE that are both equally complete (values
        may differ, completeness must not) must not trigger F3."""
        a = item(item_id="1", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("1000"))
        b = item(item_id="2", boe_number="BOE1", tax_type="IGST", exchange_rate=Decimal("90"), total_inclusive_value=Decimal("2000"))
        flags = f.po_flags("PO1", [a, b])
        assert not any(fl["code"] == "F3" for fl in flags)
