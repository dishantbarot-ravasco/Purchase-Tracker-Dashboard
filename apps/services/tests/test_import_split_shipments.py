"""
Split shipments and "delivered" on import orders (2026-09-25) - see
apps/services/import_flags.py's boe_totals().

The Imports CSV writes one order line shipped in several parts as several
rows, each repeating the WHOLE ordered quantity (Vapi 1000001528: three rows
each "64,800 ordered", each clearing 21,600). Row by row, each read 67% short
of its order; and a line whose BOE covered only half the order read
"Delivered" once that half arrived (1000001450), so it could never be
Overdue. Pure functions - plain objects, no database.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace

from apps.services import import_flags as f


def line(**overrides):
    base = dict(
        item_id="22001902", qty_as_per_po=Decimal("64800"), qty_as_per_boe=Decimal("21600"),
        delivery_date=datetime.date(2026, 5, 1), boe_number="B1",
        mir_match=SimpleNamespace(dismissed_by_override=False, qty_diff_pct=Decimal("0"), qty_over_delivered=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


TODAY = datetime.date(2026, 9, 25)


class TestSplitShipments:
    def test_three_shipment_rows_of_one_order_are_not_each_short(self):
        rows = [line(), line(), line()]
        assert f.po_has_qty_discrepancy(rows) is False
        assert f.boe_totals(rows)[id(rows[0])] == Decimal("64800")

    def test_a_split_order_still_short_overall_is_flagged(self):
        """1000001508 cleared 126,153 of 127,000 across two rows - a real
        847 KG shortfall, which the sum must still show."""
        rows = [line(qty_as_per_po=Decimal("127000"), qty_as_per_boe=Decimal("72073")),
                line(qty_as_per_po=Decimal("127000"), qty_as_per_boe=Decimal("54080"))]
        assert f.po_has_qty_discrepancy(rows) is True

    def test_two_lines_with_different_item_ids_are_judged_alone(self):
        rows = [line(item_id="A"), line(item_id="B")]
        assert f.po_has_qty_discrepancy(rows) is True

    def test_a_fully_received_split_order_is_delivered(self):
        rows = [line(), line(), line()]
        assert f.po_delivery_date_status(rows, TODAY) == f.STATUS_DELIVERED
        assert f.material_inwarded(rows) is True


class TestDeliveredMeansTheOrderIsCovered:
    def test_half_shipped_and_received_is_overdue_not_delivered(self):
        """1000001450: 252,000 of 504,000 KG cleared, all of it received."""
        row = line(qty_as_per_po=Decimal("504000"), qty_as_per_boe=Decimal("252000"))
        assert f.po_delivery_date_status([row], TODAY) == f.STATUS_OVERDUE
        assert f.material_inwarded([row]) is False
        assert f.partial_delivery([row]) is True

    def test_a_line_shipped_in_full_and_received_is_delivered(self):
        row = line(qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100"))
        assert f.po_delivery_date_status([row], TODAY) == f.STATUS_DELIVERED


class TestFlagKeys:
    def test_a_repeated_item_id_gets_a_position_in_its_key(self):
        """Dismissing one shipment line's flag used to dismiss it on every
        line sharing the item_id."""
        rows = [line(id=10, hsn="12", delivery_date_raw="", laden_on_board_date=None, bill_of_lading_number="",
                     tax_type="", exchange_rate=None, total_inclusive_value=None, currency_after_taxes=""),
                line(id=11, hsn="12", delivery_date_raw="", laden_on_board_date=None, bill_of_lading_number="",
                     tax_type="", exchange_rate=None, total_inclusive_value=None, currency_after_taxes="")]
        keys = {fl["flag_key"] for fl in f.po_flags("P", rows) if fl["code"] == "F5"}
        assert keys == {"F5:22001902#0", "F5:22001902#1"}

    def test_a_unique_item_id_keeps_the_old_key(self):
        """So every dismissal already stored keeps applying."""
        row = line(id=10, hsn="12", delivery_date_raw="", laden_on_board_date=None, bill_of_lading_number="",
                   tax_type="", exchange_rate=None, total_inclusive_value=None, currency_after_taxes="")
        keys = {fl["flag_key"] for fl in f.po_flags("P", [row]) if fl["code"] == "F5"}
        assert keys == {"F5:22001902"}
