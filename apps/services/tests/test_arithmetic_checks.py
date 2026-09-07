"""
Unit tests for apps/services/arithmetic_checks.py - Match Accuracy Programme
fix 3.G. Pure-function tests, no Django DB, same convention as
test_parsers_common.py/test_matching.py.
"""
from decimal import Decimal

from apps.services.arithmetic_checks import check_mir_entry, check_po_line_item, check_stock_lot


class TestCheckPoLineItem:
    def test_arithmetic_holds_returns_none(self):
        """qty x rate == net_value exactly - no discrepancy."""
        assert check_po_line_item(Decimal("100"), Decimal("50"), Decimal("5000.00")) is None

    def test_within_tolerance_returns_none(self):
        """A sub-rupee rounding difference is within tolerance, not a real discrepancy."""
        assert check_po_line_item(Decimal("100"), Decimal("50.005"), Decimal("5000.00")) is None

    def test_real_mismatch_is_caught(self):
        # Real data shape: two RTP-Vapi PO line items whose net_value figures
        # appeared swapped, confirmed against dev_smoke_test.sqlite3.
        """A genuine arithmetic mismatch (net_value doesn't match qty x rate)
        is caught and reports the expected value."""
        result = check_po_line_item(Decimal("1011.200"), Decimal("225.0000"), Decimal("131760.00"))
        assert result is not None
        assert result["check_name"] == "po_qty_rate_value"
        assert result["expected"] == Decimal("227520.0000")

    def test_missing_field_returns_none(self):
        """A missing qty/rate/value means there's nothing to check yet -
        never a false discrepancy."""
        assert check_po_line_item(None, Decimal("50"), Decimal("5000")) is None
        assert check_po_line_item(Decimal("100"), None, Decimal("5000")) is None
        assert check_po_line_item(Decimal("100"), Decimal("50"), None) is None


class TestCheckMirEntry:
    def test_arithmetic_holds_returns_none(self):
        """taxable + gst + tcs - discount == final exactly - no discrepancy."""
        assert check_mir_entry(Decimal("109250.00"), Decimal("19665.00"), Decimal("0"), Decimal("0"), Decimal("128915.00")) is None

    def test_real_mismatch_is_caught(self):
        """A wrong tax rate or missing component throws off the final value
        beyond tolerance."""
        result = check_mir_entry(Decimal("100000.00"), Decimal("5000.00"), Decimal("0"), Decimal("0"), Decimal("110000.00"))
        assert result is not None
        assert result["check_name"] == "mir_tax_arithmetic"

    def test_no_discount_column_plant_passes_zero(self):
        """A plant with no discount column (RTP-Vapi) passes discount_amt=0
        explicitly - the check still runs normally."""
        assert check_mir_entry(Decimal("2020578.56"), Decimal("364244.14") , Decimal("0"), Decimal("0"), Decimal("2384822.70")) is None

    def test_missing_field_returns_none(self):
        """Any missing input means there's nothing to check yet."""
        assert check_mir_entry(None, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("100")) is None


class TestCheckStockLot:
    def test_arithmetic_holds_returns_none(self):
        """opening + received - issued == closing exactly - no discrepancy."""
        assert check_stock_lot(Decimal("1000"), Decimal("200"), Decimal("300"), Decimal("900")) is None

    def test_real_mismatch_is_caught(self):
        # Real data shape: an HRS stock lot with opening=received=issued=0
        # but a nonzero current stock figure - confirmed against
        # dev_smoke_test.sqlite3, a genuine broken-formula case.
        """A broken stock formula (stock appearing from nowhere) is caught -
        the exact real anomaly this check found in live HRS/Achhad data."""
        result = check_stock_lot(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("500"))
        assert result is not None
        assert result["check_name"] == "stock_balance"
        assert result["expected"] == Decimal("0")

    def test_missing_field_returns_none(self):
        """Any missing input means there's nothing to check yet."""
        assert check_stock_lot(Decimal("100"), None, Decimal("10"), Decimal("90")) is None
