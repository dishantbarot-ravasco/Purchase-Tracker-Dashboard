"""
Pipeline tests for pooling an order's identical lines
(matching_core._pool_duplicate_lines(), 2026-09-26).

Madura lists one fabric at one rate on several lines of a PO, and MIR books
the deliveries against the order, not a line - so which receipt "belongs"
to which line was a guess, and every line read wildly off while the order
as a whole could be exact. Real HRS model rows and the real
run_full_match(), as in test_manual_mir_match.py.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    ManualMirMatch,
    SyncRun,
)
from apps.services.matching import run_full_match

MADURA = "Madura Industrial Textiles Ltd"
FABRIC = "EE-350 fabric roll, width 142cm, GSM 1170, length 514m, {rolls} rolls, total weight {kg}"


def _po(po_number="3000009101", vendor_name=MADURA):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 1), vendor_name=vendor_name,
        tax_type="IGST", total_value=Decimal("0"), total_inclusive_value=Decimal("0"),
    )


def _line(po, qty, rolls, item_id, rate=Decimal("190.00"), description=None):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, hsn="5902",
        description=description or FABRIC.format(rolls=rolls, kg=qty),
        qty=Decimal(qty), uom="KG", net_price=rate, net_value=Decimal(qty) * rate,
    )


def _mir(mir_no, row, qty, *, po, desc="EE350 142CM", rate=Decimal("190.00"), party=MADURA + "."):
    qty = Decimal(qty)
    return HRSMIREntry.objects.create(
        month="May-26", mir_no=mir_no, mir_date=datetime.date(2026, 5, 1), party_name=party,
        po_number_raw=po.po_number, material_description=desc, qty=qty, uom="KGS", rate=rate,
        net=qty * rate, taxable_value=qty * rate, invoice_final_value=qty * rate,
        source_row_ref=row, is_active=True,
    )


def _match(line):
    return HRSPOMirMatch.objects.filter(po_line_item=line).first()


@pytest.mark.django_db
class TestPooling:
    def _order(self):
        """Vapi 1000001368's shape: one fabric on lines of 14, 6 and 8 rolls
        (7000, 3000, 4000 KG), and three receipts of 7100, 3050, 4060 KG -
        1.5% over as an order, but not one receipt per line."""
        po = _po()
        lines = [_line(po, "7000", 14, "1"), _line(po, "3000", 6, "2"), _line(po, "4000", 8, "3")]
        _mir("MIR-A", "10", "7100", po=po)
        _mir("MIR-B", "11", "3050", po=po)
        _mir("MIR-C", "12", "4060", po=po)
        return po, lines

    def test_every_line_reads_the_orders_own_percentage(self):
        _po_, lines = self._order()
        run_full_match()
        matches = [_match(line) for line in lines]
        assert all(m is not None for m in matches)
        # 14,210 received of 14,000 ordered = 1.5% over, on every line
        assert {m.qty_diff_pct for m in matches} == {Decimal("1.50")}
        assert all(m.qty_within_tolerance and not m.qty_mismatched for m in matches)

    def test_each_line_counts_its_ordered_share(self):
        _po_, lines = self._order()
        run_full_match()
        shares = [_match(line).receipt_share for line in lines]
        assert shares == [Decimal("0.500000"), Decimal("0.214286"), Decimal("0.285714")]
        assert {_match(line).pool_line_refs for line in lines} == {"1, 2, 3"}
        # every pooled receipt is recorded on every line
        assert all(_match(line).group_entries.count() == 3 for line in lines)

    def test_a_line_left_without_a_receipt_shares_in_the_pool(self):
        """Two identical lines, one receipt covering both: without pooling
        one line held it (100% over) and the other read Not Found."""
        po = _po()
        a, b = _line(po, "1000", 2, "1"), _line(po, "1000", 2, "2")
        _mir("MIR-ONE", "10", "2050", po=po, desc="EE350 142CM - 4 Rolls")
        run_full_match()
        ma, mb = _match(a), _match(b)
        assert ma is not None and mb is not None
        assert ma.qty_diff_pct == mb.qty_diff_pct == Decimal("2.50")
        assert (ma.rolls_ordered, ma.rolls_received) == (4, 4)
        assert not ma.qty_mismatched and not mb.qty_mismatched

    def test_every_roll_in_counts_the_order_as_arrived_whatever_the_weight(self):
        """4 of 4 rolls at 3% under the theoretical weight: arrived in full,
        so shared by quantity - both lines read the order's -3%, which the
        over-only allowance still reports as short."""
        po = _po()
        a, b = _line(po, "1000", 2, "1"), _line(po, "1000", 2, "2")
        _mir("MIR-ONE", "10", "1940", po=po, desc="EE350 142CM - 4 Rolls")
        run_full_match()
        ma, mb = _match(a), _match(b)
        assert (ma.rolls_ordered, ma.rolls_received) == (4, 4)
        assert ma.qty_diff_pct == mb.qty_diff_pct == Decimal("3.00")
        assert ma.receipt_share == mb.receipt_share == Decimal("0.500000")

    def test_a_missing_roll_fills_the_lines_in_order(self):
        """3 of 4 rolls: the first line has its 2, the second is a roll short."""
        po = _po()
        a, b = _line(po, "1000", 2, "1"), _line(po, "1000", 2, "2")
        _mir("MIR-ONE", "10", "2000", po=po, desc="EE350 142CM - 3 Rolls")
        run_full_match()
        ma, mb = _match(a), _match(b)
        assert (ma.rolls_ordered, ma.rolls_received, ma.qty_mismatched) == (2, 2, False)
        assert (mb.rolls_ordered, mb.rolls_received, mb.qty_mismatched) == (2, 1, True)
        assert mb.qty_over_delivered is False

    def test_a_part_delivered_order_fills_its_lines_in_order(self):
        """9,000 of 14,000 KG arrived: line 1 (7,000) is full, line 2 (3,000)
        has the other 2,000, line 3 has had nothing yet. Sharing it by
        quantity would have called all three 36% short."""
        po = _po()
        lines = [_line(po, "7000", 14, "1"), _line(po, "3000", 6, "2"), _line(po, "4000", 8, "3")]
        _mir("MIR-A", "10", "5000", po=po)
        _mir("MIR-B", "11", "4000", po=po)
        run_full_match()
        m1, m2, m3 = (_match(line) for line in lines)
        assert m1.qty_diff_pct == Decimal("0.00") and not m1.qty_mismatched
        assert m2.qty_diff_pct == Decimal("33.33") and m2.qty_over_delivered is False
        assert m3 is None

    def test_a_different_rate_is_not_the_same_line(self):
        po = _po()
        a = _line(po, "1000", 2, "1")
        b = _line(po, "1000", 2, "2", rate=Decimal("230.00"))
        _mir("MIR-ONE", "10", "1000", po=po)
        run_full_match()
        assert (_match(a).pool_line_refs if _match(a) else "") == ""
        assert (_match(b).pool_line_refs if _match(b) else "") == ""

    def test_a_different_width_is_not_the_same_line(self):
        po = _po()
        a = _line(po, "1000", 2, "1")
        b = _line(po, "1000", 2, "2", description="EE-350 fabric roll, width 102cm, GSM 1170, length 514m, 2 rolls")
        _mir("MIR-A", "10", "1000", po=po)
        _mir("MIR-B", "11", "1000", po=po, desc="EE350 102CM")
        run_full_match()
        assert _match(a).pool_line_refs == "" and _match(b).pool_line_refs == ""
        assert _match(a).receipt_share is None

    def test_a_pinned_line_stays_out_of_the_pool(self):
        po = _po()
        a, b = _line(po, "1000", 2, "1"), _line(po, "1000", 2, "2")
        _mir("MIR-A", "10", "1000", po=po)
        _mir("MIR-B", "11", "1100", po=po)
        ManualMirMatch.objects.create(
            plant=SyncRun.Plant.HRS, po_number=po.po_number, item_ref="0", mir_no="MIR-A",
            item_description=a.description, created_by_email="t@ravasco.com",
        )
        run_full_match()
        assert _match(a).mir_entry.mir_no == "MIR-A"
        assert _match(a).pool_line_refs == ""
        assert _match(b).pool_line_refs == ""
        assert _match(b).mir_entry.mir_no == "MIR-B"
