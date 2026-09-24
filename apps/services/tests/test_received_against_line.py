"""
matching_core.received_against_line() - what a PO line's matched MIR rows add
up to, in the PO line's own unit, for the PO modal's Ordered / Received /
Difference cards (2026-09-24). It must convert exactly as the matcher does
(_uom_adjust()), or the card and the flags beside it would disagree.
"""
from decimal import Decimal
from types import SimpleNamespace

from apps.services.matching import MATCH_CONFIG


def _mir(qty, uom, rate, net):
    return SimpleNamespace(qty=Decimal(qty), uom=uom, rate=Decimal(rate), net=Decimal(net), taxable_value=Decimal(net))


def test_receipts_are_summed_in_the_po_lines_unit():
    from apps.services.matching_core import received_against_line
    rows = [_mir("16100", "Kgs", "114", "1835400"), _mir("16100", "KG", "114", "1835400")]
    r = received_against_line(MATCH_CONFIG, "KG", rows)
    assert r["qty"] == Decimal("32200")
    assert r["value"] == Decimal("3670800")
    assert r["rate"] == Decimal("114")
    assert r["comparable"] is True


def test_kg_receipts_against_an_mt_order_convert_to_mt():
    from apps.services.matching_core import received_against_line
    r = received_against_line(MATCH_CONFIG, "MT", [_mir("31510", "KG", "9.55", "300920.5")])
    assert r["qty"] == Decimal("31.51")
    assert r["rows"][0]["qtyInPoUnit"] == Decimal("31.51")


def test_a_unit_that_cannot_convert_gives_no_total_rather_than_a_wrong_one():
    from apps.services.matching_core import received_against_line
    rows = [_mir("100", "KG", "10", "1000"), _mir("5", "NOS", "10", "50")]
    r = received_against_line(MATCH_CONFIG, "KG", rows)
    assert r["comparable"] is False
    assert r["qty"] is None and r["rate"] is None
    assert r["value"] == Decimal("1050"), "value is a currency sum and stays meaningful"
