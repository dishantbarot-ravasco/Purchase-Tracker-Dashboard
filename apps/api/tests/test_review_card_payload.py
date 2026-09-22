"""
Integration tests for what the match-review screen actually puts on a card
(apps/api/routers/review_views.py, rendered by frontend/js/review-page.js).

The screen's whole purpose is to collect verdicts precise enough to become
this app's first measured accuracy figure (CLAUDE.md, Known gaps), so the
card has to be judgeable: these tests pin the two things that decide whether
a reviewer CAN judge it, both of which the first version got wrong.

  1. Identifiers. The card showed description/qty/rate/value/vendor and
     nothing else - no PO number, no MIR number, no dates, no UOM - so a
     Tier-1 match could not be checked against the very PO number it was
     made on, and 500 KG against 500 MTR read as agreement.
  2. Import currency. An import line item is priced in the PO's own currency
     while MIR is always INR; the matcher scores it through
     _import_rate_value_inr(), but the card showed the raw foreign net_price
     against MIR's INR rate. At a ~90x USD/INR rate that makes a CORRECT
     match look obviously wrong, i.e. it actively invited a wrong verdict.

HRS only, matching this codebase's convention of testing one plant's copy of
byte-for-byte shared logic (see test_dismiss_match.py's module docstring).
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSRMLot,
)


def _client():
    client = APIClient()
    client.force_authenticate(user=make_user(email="reviewer@ravasco.com", role="viewer"))
    return client


def _refs(side):
    """The card's identifying fields as a plain {label: value} dict."""
    return {ref["label"]: ref["value"] for ref in side["refs"]}


def _card_for(client, match_type):
    """/api/review/next returns a mixed batch across plants and types (it is
    randomised by design), so pull out the one card this test cares about."""
    response = client.get("/api/review/next")
    assert response.status_code == 200, response.data
    cards = [c for c in response.data["matches"] if c["matchType"] == match_type]
    assert cards, f"no {match_type} card in the batch: {response.data}"
    return cards[0]


@pytest.mark.django_db
class TestDomesticCardIsJudgeable:
    def setup_method(self):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name="1000001234",
            po_number="1000001234",
            po_created_date="2026-04-01",
            vendor_name="Test Vendor Ltd",
        )
        item = HRSDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="2", description="Widget", hsn="4010",
            qty="100", uom="KG", delivery_date="2026-04-20",
            net_price="1.5", net_value="150",
        )
        mir = HRSMIREntry.objects.create(
            mir_no="MIR-77", mir_date="2026-04-18", po_number_raw="1000001234",
            invoice_no="INV-9", invoice_date="2026-04-17",
            party_name="Test Vendor Ltd", material_description="Widget",
            qty="100", uom="MTR", rate="1.5", net="150", source_row_ref="10",
        )
        HRSPOMirMatch.objects.create(
            po_line_item=item, mir_entry=mir, tier="po_number", match_score="0.95",
            po_number_matched=True, vendor_matched=True, material_matched=True,
        )

    def test_card_carries_both_rows_identifiers(self):
        """Without these a reviewer cannot tell which delivery they are
        looking at, nor look it up in the source sheet."""
        card = _card_for(_client(), "po_mir")
        left, right = _refs(card["left"]), _refs(card["right"])
        assert left["PO no."] == "1000001234"
        assert left["PO date"] == "2026-04-01"
        assert left["Line"] == "2"
        assert right["MIR no."] == "MIR-77"
        assert right["MIR date"] == "2026-04-18"
        assert right["PO cited on MIR"] == "1000001234"
        assert right["Invoice no."] == "INV-9"

    def test_both_sides_carry_uom(self):
        """500 KG against 500 MTR is not a match, and reads as a perfect one
        when neither unit is on the card. review-page.js highlights the
        disagreement; it can only do that if both values are sent."""
        card = _card_for(_client(), "po_mir")
        assert _refs(card["left"])["UOM"] == "KG"
        assert _refs(card["right"])["UOM"] == "MTR"

    def test_signals_report_the_matchers_own_evidence(self):
        """The identification booleans the matcher recorded, in words - so a
        reviewer judges the evidence the pair was built on rather than
        re-running the description comparison by eye."""
        card = _card_for(_client(), "po_mir")
        signals = {s["label"]: s["state"] for s in card["signals"]}
        assert signals["PO number cited on MIR"] == "yes"
        assert signals["Vendor agrees"] == "yes"
        assert signals["Material description agrees"] == "yes"


@pytest.mark.django_db
class TestImportCardShowsInrNotForeignCurrency:
    def setup_method(self):
        po = HRSImportPurchaseOrder.objects.create(
            po_drive_folder_name="IMP-1", po_number="IMP-1", vendor_name="Overseas Supplier",
            currency="USD",
        )
        item = HRSImportPOLineItem.objects.create(
            purchase_order=po, item_id="1", description="Imported Widget",
            qty_as_per_po="10", qty_as_per_boe="10", uom="NOS",
            net_price="12.50", net_value="125", exchange_rate="83.50",
        )
        mir = HRSMIREntry.objects.create(
            mir_no="MIR-88", party_name="Overseas Supplier", material_description="Imported Widget",
            qty="10", rate="1043.75", net="10437.50", source_row_ref="11",
        )
        HRSImportPOMirMatch.objects.create(
            po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.88",
            vendor_matched=True, material_matched=True,
        )

    def test_rate_is_converted_the_same_way_the_matcher_converts_it(self):
        """12.50 USD x 83.50 = 1043.75, which is exactly MIR's INR rate. Shown
        raw, the same correct match reads as a ~98% rate discrepancy."""
        card = _card_for(_client(), "import_po_mir")
        assert card["left"]["rate"] == pytest.approx(1043.75)
        assert card["right"]["rate"] == pytest.approx(1043.75)

    def test_original_currency_amount_is_still_on_the_card(self):
        """Converting must not hide the PO's own figures - the reviewer needs
        both to reconcile against the import paperwork."""
        card = _card_for(_client(), "import_po_mir")
        left = _refs(card["left"])
        assert left["Rate in USD"] == pytest.approx(12.50)
        assert left["Exchange rate"] == pytest.approx(83.50)


@pytest.mark.django_db
class TestStockCardUsesPerPlantLotColumns:
    def setup_method(self):
        mir = HRSMIREntry.objects.create(
            mir_no="MIR-99", mir_date="2026-05-02", party_name="Vendor B",
            material_description="Gadget", uom="KG", qty="5", rate="20", net="100",
            source_row_ref="12",
        )
        lot = HRSRMLot.objects.create(
            description="Gadget", sap_item_code="SAP-123", uom="KG",
            received_date="2026-05-02", basic_rate="20", party_name="Vendor B",
            source_row_ref="20",
        )
        HRSMirStockMatch.objects.create(
            mir_entry=mir, stock_lot=lot, material_matched=True, date_matched=True,
        )

    def test_lot_identifiers_come_from_this_plants_configured_columns(self):
        """HRS's lot item code is `sap_item_code` (Achhad's is `sap_code`,
        Vapi has none) - read through MATCH_CONFIG.stock_code_field rather
        than a second per-plant mapping in the view."""
        card = _card_for(_client(), "mir_stock")
        right = _refs(card["right"])
        assert right["Item code"] == "SAP-123"
        assert right["Received date"] == "2026-05-02"
        assert card["right"]["vendor"] == "Vendor B"

    def test_date_signal_reflects_the_stored_flag(self):
        card = _card_for(_client(), "mir_stock")
        signals = {s["label"]: s["state"] for s in card["signals"]}
        assert signals["Material agrees"] == "yes"
        assert signals["Receipt date agrees"] == "yes"
