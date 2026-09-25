"""
Matcher fixes from the 2026-09-25 dashboard audit, each on the real shape
that exposed it:

- a multi-line fabric order handing a PO-cited receipt of one width/grade to
  a sibling line of another (Vapi 1000001462: an NN-100 102 cm line of 50 KG
  held an NN250 163 cm receipt of 1,709 KG);
- a Bill of Entry across two Bills of Lading refused even when its receipts
  meet every line exactly (1000001321);
- a foreign-currency line with no exchange rate compared as if its price
  were INR (1000001318, a 9,560% "rate mismatch").
"""

from decimal import Decimal

import pytest

from apps.core.models import HRSImportPOMirMatch, HRSPOMirMatch
from apps.services.matching import run_full_match
from apps.services.matching_core import _fabric_spec, _fabric_spec_contradicts
from apps.services.tests.test_manual_mir_match import _item, _mir, _po
from apps.services.tests.test_run_full_match_import_pipeline import _boe_line, _make_import_po, _receipt


class TestFabricSpec:
    def test_reads_series_grade_and_width(self):
        assert _fabric_spec("NN-200 fabric roll, width 67cm, GSM 500") == ("NN200", 67)
        assert _fabric_spec("EE250 122CM") == ("EE250", 122)
        assert _fabric_spec("Rubberised Textile Fabric-EE80,140cm") == ("EE80", 140)

    def test_leading_zeros_are_one_grade(self):
        assert _fabric_spec("EE-080 fabric roll, width 140cm")[0] == _fabric_spec("EE80 140CM")[0]

    def test_non_fabric_text_has_no_spec(self):
        assert _fabric_spec("SBR 1502") == (None, None)

    def test_silence_on_one_side_is_no_evidence(self):
        assert _fabric_spec_contradicts(("EE250", None), (None, 163)) is False
        assert _fabric_spec_contradicts(("EE250", 122), ("EE250", 123)) is False
        assert _fabric_spec_contradicts(("EE250", 122), ("EE250", 142)) is True
        assert _fabric_spec_contradicts(("NN200", 67), ("EE250", 67)) is True


@pytest.mark.django_db
class TestMultiLineFabricOrders:
    def test_a_receipt_of_another_width_is_not_handed_to_a_sibling_line(self):
        po = _po("1000001462", vendor_name="Madura Industrial Textiles Ltd")
        narrow = _item(po, description="NN-100 fabric roll, width 102cm, GSM 365", qty=Decimal("50"), item_id="1")
        wide = _item(po, description="NN-250 fabric roll, width 163cm, GSM 850", qty=Decimal("1709"), item_id="2")
        receipt = _mir("MIR118/04", "10", party_name="Madura Industrial Textiles Ltd", po_number_raw="1000001462",
                       material_description="NN250 163CM", qty=Decimal("1709"))

        run_full_match()

        assert HRSPOMirMatch.objects.get(po_line_item=wide).mir_entry_id == receipt.id
        assert not HRSPOMirMatch.objects.filter(po_line_item=narrow).exists()

    def test_a_single_line_order_is_unaffected(self):
        """The gate only separates siblings; with one line there is no
        sibling to protect, and the existing identification decides."""
        po = _po("1000001999", vendor_name="Madura Industrial Textiles Ltd")
        only = _item(po, description="EE-250 fabric roll, width 142cm", qty=Decimal("1000"), item_id="1")
        _mir("MIR1/05", "11", party_name="Madura Industrial Textiles Ltd", po_number_raw="1000001999",
             material_description="EE250 122CM", qty=Decimal("1000"))

        run_full_match()

        assert HRSPOMirMatch.objects.filter(po_line_item=only).exists()


@pytest.mark.django_db
class TestBoeAcrossBillsOfLading:
    def test_trusted_when_every_line_has_an_exact_receipt(self):
        po = _make_import_po("1000001321")
        a = _boe_line(po, boe="2164738", qty=Decimal("14680"), bl="008GX36432", item_id="1")
        b = _boe_line(po, boe="2164738", qty=Decimal("1320"), bl="008GX36433", item_id="2")
        _receipt(invoice_no="2164738", qty=Decimal("14680"), ref="7", mir_no="MIR23/07")
        _receipt(invoice_no="2164738", qty=Decimal("1320"), ref="8", mir_no="MIR23/07")

        run_full_match()

        assert HRSImportPOMirMatch.objects.get(po_line_item=a).tier == "boe_number"
        assert HRSImportPOMirMatch.objects.get(po_line_item=b).tier == "boe_number"
        assert HRSImportPOMirMatch.objects.get(po_line_item=a).mir_entry.qty == Decimal("14680")

    def test_still_refused_when_the_quantities_do_not_meet_the_lines(self):
        """1000001560's copy error: the second shipment carries the first's
        BOE, and its receipt does not match."""
        po = _make_import_po("1000001560")
        a = _boe_line(po, boe="3587956", qty=Decimal("151200"), bl="BL-ONE", item_id="1")
        b = _boe_line(po, boe="3587956", qty=Decimal("151200"), bl="BL-TWO", item_id="2")
        _receipt(invoice_no="3587956", qty=Decimal("151200"), ref="7", mir_no="MIR-A")
        _receipt(invoice_no="3729236", qty=Decimal("117480"), ref="8", mir_no="MIR-B")

        run_full_match()

        tiers = set(HRSImportPOMirMatch.objects.filter(po_line_item__in=[a, b]).values_list("tier", flat=True))
        assert "boe_number" not in tiers


class TestNoExchangeRate:
    """The pre-duty INR price of a foreign-currency line needs its exchange
    rate. Without one the bare USD price was set against MIR's INR price.
    (The landed-rate check still runs: landed value is already INR.)"""

    def _line(self, currency, fx):
        from types import SimpleNamespace
        return SimpleNamespace(net_price=Decimal("2.2"), net_value=Decimal("220"), qty_as_per_boe=Decimal("100"),
                               exchange_rate=fx, purchase_order=SimpleNamespace(currency=currency))

    def test_a_usd_line_with_no_rate_has_no_inr_figure(self):
        from apps.services.matching_core import _import_rate_value_inr
        assert _import_rate_value_inr(self._line("USD", None)) == (None, None)

    def test_an_inr_order_needs_no_rate(self):
        from apps.services.matching_core import _import_rate_value_inr
        assert _import_rate_value_inr(self._line("INR", None)) == (Decimal("2.2"), Decimal("220.0"))

    def test_value_is_what_cleared_not_the_whole_order(self):
        """net price x BOE qty x rate: net_value is the whole order line, so
        a part shipment read a gap the size of the unshipped part."""
        from apps.services.matching_core import _import_rate_value_inr
        line = self._line("USD", Decimal("90"))
        line.net_value = Decimal("440")  # the order is twice what cleared
        assert _import_rate_value_inr(line) == (Decimal("198.0"), Decimal("19800.00"))
