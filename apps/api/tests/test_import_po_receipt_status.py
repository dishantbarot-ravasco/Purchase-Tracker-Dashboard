"""
Import PO status fields read MIR receipts, not customs clearance (2026-09-25).

The Import KPI row's Material Inwarded / Partial Delivered / Overdue / On
Order cards read `materialInwarded`, `partialDelivery` and
`deliveryDateStatus` off /api/imports/purchase-orders. Until 2026-09-25 those
meant "any line matched", "BOE quantity below PO quantity" and "not yet
customs-cleared" - so a shipment that cleared the port but never reached the
plant read Delivered and could never be Overdue (15 of Vapi's 38 cleared POs
had no MIR receipt at all). These tests run the real router over real match
rows; each fails under the old rules.
"""

import datetime

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSImportPOLineItem, HRSImportPOMirMatch, HRSImportPurchaseOrder, HRSMIREntry


def _client():
    client = APIClient()
    client.force_authenticate(user=make_user(email="importstatus@ravasco.com", role="viewer"))
    return client


def _po(po_number, lines):
    """lines: list of (qty_as_per_po, qty_as_per_boe, boe_number) tuples."""
    po = HRSImportPurchaseOrder.objects.create(
        po_drive_folder_name=po_number, po_number=po_number, vendor_name="Kumho Petrochemical",
    )
    past = timezone.localdate() - datetime.timedelta(days=10)
    return [
        HRSImportPOLineItem.objects.create(
            purchase_order=po, item_id=str(n), description="SBR 1502", uom="KG", net_price="1.85",
            exchange_rate="88.50", qty_as_per_po=qty_po, qty_as_per_boe=qty_boe, boe_number=boe, delivery_date=past,
        )
        for n, (qty_po, qty_boe, boe) in enumerate(lines)
    ]


def _receive(item, qty_diff_pct="0", over=None, dismissed=False):
    mir = HRSMIREntry.objects.create(
        mir_no=f"MIR-{item.pk}", party_name="Kumho Petrochemical", material_description="SBR 1502",
        qty="100", uom="KG", rate="163.73", net="16373", source_row_ref=str(item.pk),
    )
    HRSImportPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9",
        qty_diff_pct=qty_diff_pct, qty_over_delivered=over, dismissed_by_override=dismissed,
    )


def _po_json(po_number):
    pos = _client().get("/api/imports/purchase-orders").json()["purchaseOrders"]
    return next(p for p in pos if p["poNumber"] == po_number)


@pytest.mark.django_db
class TestImportReceiptStatus:
    def test_cleared_but_not_in_mir_is_overdue_not_delivered(self):
        _po("3000009001", [("100", "100", "BOE1")])
        po = _po_json("3000009001")
        assert po["shipmentStage"] == "Cleared (BOE)"
        assert po["deliveryDateStatus"] == "Overdue"
        assert po["materialInwarded"] is False
        assert po["partialDelivery"] is False

    def test_every_line_received_is_inwarded_and_delivered(self):
        a, b = _po("3000009002", [("100", "100", "BOE1"), ("50", "50", "BOE1")])
        _receive(a)
        _receive(b, qty_diff_pct="4", over=True)  # over-delivered still counts
        po = _po_json("3000009002")
        assert po["materialInwarded"] is True
        assert po["partialDelivery"] is False
        assert po["deliveryDateStatus"] == "Delivered"

    def test_one_of_two_lines_received_is_partial_not_inwarded(self):
        a, _b = _po("3000009003", [("100", "100", "BOE1"), ("50", "50", "BOE1")])
        _receive(a)
        po = _po_json("3000009003")
        assert po["materialInwarded"] is False
        assert po["partialDelivery"] is True
        assert po["deliveryDateStatus"] == "Overdue"

    def test_short_receipt_is_partial(self):
        [a] = _po("3000009004", [("100", "100", "BOE1")])
        _receive(a, qty_diff_pct="20", over=False)
        po = _po_json("3000009004")
        assert po["materialInwarded"] is False
        assert po["partialDelivery"] is True

    def test_boe_short_of_po_is_a_qty_mismatch_not_a_partial_delivery(self):
        _po("3000009005", [("100", "60", "BOE1")])
        po = _po_json("3000009005")
        assert po["qtyDiscrepancy"] is True
        assert po["partialDelivery"] is False

    def test_dismissed_match_is_not_a_receipt(self):
        [a] = _po("3000009006", [("100", "100", "BOE1")])
        _receive(a, dismissed=True)
        po = _po_json("3000009006")
        assert po["materialInwarded"] is False
        assert po["deliveryDateStatus"] == "Overdue"


@pytest.mark.django_db
def test_a_shared_receipt_counts_only_this_lines_share():
    """One 100 KG MIR receipt covering a whole BOE of two lines: the 25%
    line counts 25 KG of it, not 100 (2026-09-25, receipt_share)."""
    [a] = _po("3000009007", [("25", "25", "BOE7")])
    mir = HRSMIREntry.objects.create(
        mir_no="MIR-SHARED", party_name="Kumho Petrochemical", material_description="SBR 1502",
        qty="100", uom="KG", rate="163.73", net="16373", source_row_ref="shared",
    )
    HRSImportPOMirMatch.objects.create(
        po_line_item=a, mir_entry=mir, tier="boe_number", match_score="1", receipt_share="0.25",
    )
    [line] = _po_json("3000009007")["items"]
    assert line["mirMatch"]["receiptShare"] == 0.25
    assert line["mirMatch"]["received"]["qty"] == 25.0
    assert line["mirMatch"]["matchedMirs"][0]["qty"] == 100.0  # the receipt still reads as the document it is
    assert line["mirMatch"]["tier"] == "boe_number"
