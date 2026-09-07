"""
apps/services/arithmetic_checks.py — Match Accuracy Programme, fix 3.G:
arithmetic self-validation of the source sheets, independent of any
matching pass. Catches a class of error nothing else in this app can see -
a real typo in the spreadsheet itself (a transposed digit, a rate typed as
a fraction, a missing tax component, a broken stock formula), as opposed to
a matching artifact.

Dependency-free (no Django imports), same reasoning as parsers/common.py's
own module docstring - each check is a pure function returning None (no
discrepancy) or a small dict describing what didn't add up, so these are
unit-testable with nothing but plain Python and callable from any sync
command without a circular import. apps/services/data_quality.py is the
DB-touching layer that turns these results into DataQualityFlag rows.

All three checks use the same ₹1.00 absolute tolerance as the Match Accuracy
Programme's own value_diff_pct epsilon (fix 3.F, matching_core.py's
VALUE_FLAG_EPSILON) - rounding from multiplying/summing several
already-rounded currency fields is expected and not itself a data quality
problem. Confirmed empirically against real synced data (dev_smoke_test.sqlite3,
2026-09-05): all three checks reconcile 100% for HRS/Achhad, ~96-99.5% for
Vapi, with the remaining handful of real mismatches being genuine data
issues (e.g. two Vapi PO line items whose net_value figures appear swapped;
an HRS/Achhad stock lot showing opening=received=issued=0 but a nonzero
current stock figure) - not tolerance artifacts.
"""

from decimal import Decimal

_TOLERANCE = Decimal("1.00")


def _mismatch(check_name: str, expected: Decimal, actual: Decimal) -> dict:
    return {"check_name": check_name, "expected": expected, "actual": actual}


def check_po_line_item(qty, rate, net_value) -> dict | None:
    """qty x rate ~= net_value - catches a transposed digit, a rate typed as
    a fraction of the real value, or (confirmed in real Vapi data) two line
    items' values swapped. None if any input is missing (nothing to check)
    or the arithmetic holds within tolerance."""
    if qty is None or rate is None or net_value is None:
        return None
    expected = qty * rate
    if abs(expected - net_value) > _TOLERANCE:
        return _mismatch("po_qty_rate_value", expected, net_value)
    return None


def check_mir_entry(taxable_value, gst_amt, tcs_amt, discount_amt, final_value) -> dict | None:
    """taxable + gst + tcs - discount ~= final - catches a wrong tax rate or
    a missing tax component. Callers pass in a pre-summed `gst_amt` (the sum
    of whichever IGST/CGST/SGST[/other-taxable-charge] columns that plant's
    MIR sheet actually has - see each sync_*_mir.py command for the exact
    per-plant field composition, confirmed empirically against real data)
    and `discount_amt=Decimal("0")` for a plant with no discount column
    (RTP-Vapi's MIR sheet has none). None if any required input is missing."""
    if None in (taxable_value, gst_amt, tcs_amt, discount_amt, final_value):
        return None
    expected = taxable_value + gst_amt + tcs_amt - discount_amt
    if abs(expected - final_value) > _TOLERANCE:
        return _mismatch("mir_tax_arithmetic", expected, final_value)
    return None


def check_stock_lot(opening_stock, received, issued, closing_stock) -> dict | None:
    """opening + received - issued ~= closing - catches a broken formula in
    the Stock sheet, and is the only independent verification that the
    figures feeding the Days-Left Engine are internally consistent. None if
    any input is missing."""
    if None in (opening_stock, received, issued, closing_stock):
        return None
    expected = opening_stock + received - issued
    if abs(expected - closing_stock) > _TOLERANCE:
        return _mismatch("stock_balance", expected, closing_stock)
    return None
