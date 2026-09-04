"""
Integration test for the `stockMatched` field on each PO line item
(hrs_views.py's _line_item_dict, added 2026-09-04) - backs the 3rd mini-
stepper step ("Received in Inventory") in frontend/js/main.js's
miniStepperHtml(). True only when the line item's matched MIR entry itself
has a real MIR<->Stock match, not just "the material shows up somewhere in
stock" - see that field's own comment for why.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSMIREntry, HRSMirStockMatch, HRSPOLineItem, HRSPOMirMatch, HRSPurchaseOrder, HRSStockLot

LIST_URL = "/api/purchase-orders"


@pytest.mark.django_db
class TestStockMatchedField:
    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))

    def _po_with_item(self, po_number, mir_no):
        """Build a PO + line item + a matching MIR entry, with a real
        HRSPOMirMatch linking the line item to the MIR entry - the common
        setup every test in this class starts from before deciding whether a
        Stock lot/match should also exist."""
        po = HRSPurchaseOrder.objects.create(po_drive_folder_name=po_number, po_number=po_number, vendor_name="Vendor A")
        item = HRSPOLineItem.objects.create(purchase_order=po, item_id="1", description="Widget", qty="10", net_price="2")
        mir = HRSMIREntry.objects.create(mir_no=mir_no, party_name="Vendor A", material_description="Widget", source_row_ref=mir_no)
        HRSPOMirMatch.objects.create(po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9")
        return po, item, mir

    def test_matched_but_not_stocked_is_false(self):
        """A line item matched to a MIR entry, but with no MIR<->Stock match
        for that entry, must report stockMatched=False even though
        matched=True - the two steps are independent."""
        po, item, mir = self._po_with_item("2000000001", "R1")
        response = self.client.get(LIST_URL)
        assert response.status_code == 200
        found = next(p for p in response.json()["purchaseOrders"] if p["poNumber"] == "2000000001")
        assert found["items"][0]["matched"] is True
        assert found["items"][0]["stockMatched"] is False

    def test_matched_and_stocked_is_true(self):
        """Once the matched MIR entry itself has a real HRSMirStockMatch, the
        line item's stockMatched flips to True - the full Ordered ->
        Inwarded -> Stocked chain the 3rd stepper step depends on."""
        po, item, mir = self._po_with_item("2000000002", "R2")
        lot = HRSStockLot.objects.create(description="Widget", party_name="Vendor A", source_row_ref="S2")
        HRSMirStockMatch.objects.create(mir_entry=mir, stock_lot=lot)

        response = self.client.get(LIST_URL)
        found = next(p for p in response.json()["purchaseOrders"] if p["poNumber"] == "2000000002")
        assert found["items"][0]["stockMatched"] is True

    def test_unmatched_item_is_not_stocked(self):
        """A line item with no PO<->MIR match at all can never be
        stock-matched - matched=False implies stockMatched=False, it isn't
        possible to skip straight to "Stocked"."""
        po = HRSPurchaseOrder.objects.create(po_drive_folder_name="2000000003", po_number="2000000003", vendor_name="Vendor A")
        HRSPOLineItem.objects.create(purchase_order=po, item_id="1", description="Unmatched Thing", qty="1", net_price="1")

        response = self.client.get(LIST_URL)
        found = next(p for p in response.json()["purchaseOrders"] if p["poNumber"] == "2000000003")
        assert found["items"][0]["matched"] is False
        assert found["items"][0]["stockMatched"] is False
