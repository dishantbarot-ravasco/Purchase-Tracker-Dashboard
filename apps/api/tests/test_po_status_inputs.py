"""
Guards the four line-item fields frontend/js/flags.js's computeStatus()
depends on: `matched`, `dismissedByOverride`, `qtyDiffPct` and
`qtyOverDelivered`.

Why this file exists rather than a JS test: the status logic itself lives in
the frontend, which has no test runner in this project (CI runs `node --check`
and ESLint over frontend/js, nothing more). What CAN be pinned from here is
the API contract the logic reads, and that is where a silent regression would
actually come from - dropping `qtyOverDelivered` from _line_item_dict() would
not fail anything, it would just quietly revert "Material Inwarded" to
counting short-delivered orders as complete, which is the exact defect this
whole change set out to fix (2026-09-18: 23 of Achhad's 120 "received" POs
were short, one of them by 80%).

The status rule these fields serve, for reference:
  arrived        = matched AND NOT dismissedByOverride
  fully received = arrived AND NOT (qtyDiffPct > 0 AND qtyOverDelivered === false)
  overdue        = past the delivery date AND not fully received (an overlay
                   on the status buckets, not one of them)
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
)

LIST_URL = "/api/purchase-orders"


@pytest.mark.django_db
class TestPoStatusInputs:
    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))

    def _item(self, po_number, *, qty="500", **match_kwargs):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name=po_number, po_number=po_number, vendor_name="Vendor A")
        item = HRSDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="1", description="Widget", qty=qty, net_price="2")
        mir = HRSMIREntry.objects.create(
            mir_no="R1", party_name="Vendor A", material_description="Widget", source_row_ref="R1")
        if match_kwargs is not None:
            HRSPOMirMatch.objects.create(
                po_line_item=item, mir_entry=mir, tier="po_number", match_score="0.9",
                **match_kwargs)
        return po

    def _fetch(self, po_number):
        response = self.client.get(LIST_URL)
        assert response.status_code == 200
        po = next(p for p in response.json()["purchaseOrders"] if p["poNumber"] == po_number)
        return po["items"][0]

    def test_a_short_delivered_line_reports_the_fields_that_make_it_incomplete(self):
        """The case the old status logic got wrong: matched=True, so it
        counted as Material Inwarded, while only a fraction of the order had
        actually arrived."""
        self._item("2000000001", qty_diff_pct="80.00", qty_mismatched=True,
                   qty_over_delivered=False)
        item = self._fetch("2000000001")
        assert item["matched"] is True
        assert item["qtyDiffPct"] == 80.0
        assert item["qtyOverDelivered"] is False
        assert item["dismissedByOverride"] is False

    def test_an_over_delivered_line_is_distinguishable_from_a_short_one(self):
        """Over-delivered still counts as received - the material did arrive.
        Only the direction separates it from the case above, so the field has
        to be present and correct in both."""
        self._item("2000000002", qty_diff_pct="21.00", qty_mismatched=True,
                   qty_over_delivered=True)
        item = self._fetch("2000000002")
        assert item["qtyOverDelivered"] is True

    def test_an_exact_delivery_reports_no_qty_discrepancy(self):
        self._item("2000000003", qty_diff_pct="0.00", qty_mismatched=False,
                   qty_over_delivered=False)
        item = self._fetch("2000000003")
        assert item["qtyDiffPct"] == 0.0
        assert item["qtyOverDelivered"] is False

    def test_direction_survives_as_null_when_it_could_not_be_determined(self):
        """A UOM mismatch leaves qtyDiffPct None and the direction NULL. The
        frontend treats NULL as received rather than short, so the API must
        pass the null through rather than coercing it to False - those two
        answers drive opposite outcomes."""
        self._item("2000000004", qty_diff_pct=None, qty_mismatched=False,
                   qty_over_delivered=None)
        item = self._fetch("2000000004")
        assert item["qtyDiffPct"] is None
        assert item["qtyOverDelivered"] is None

    def test_a_dismissed_match_is_reported_as_dismissed(self):
        """A reviewer dismissing a match means the pairing is wrong, so the
        line item has not arrived. Status used to ignore this field while the
        flag counts honoured it, so dismissing a bad match cleared a PO's
        flags but left it counted as Material Inwarded."""
        self._item("2000000005", qty_mismatched=False, dismissed_by_override=True,
                   dismissed_reason="wrong vendor")
        item = self._fetch("2000000005")
        assert item["matched"] is True
        assert item["dismissedByOverride"] is True
