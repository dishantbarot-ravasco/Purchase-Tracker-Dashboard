"""
Integration tests for the IMPORT manual MIR-match endpoints
(apps/api/routers/imports_views.py's mir_candidates / set_mir_match),
2026-09-21.

The Domestic equivalents are covered in test_manual_mir_match_api.py. This
file covers what is different on this router: the plant path segment, the
404-not-403 convention for an out-of-scope plant (see this module's own
`purchase_order_detail`), `po_kind=import` on the row it writes, and the
picker reporting DOMESTIC claims as well as import ones - both kinds compete
for the same MIR table, so showing only half the holders would let a reader
take a row believing nothing was using it.
"""

import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    ManualMirMatch,
    SyncRun,
)


def _import_po(po_number="IMP9100"):
    return HRSImportPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name="Global Polymers Inc",
        currency="USD", total_value=Decimal("234.00"),
    )


def _import_item(po, description="PTFE Coated Fabric", item_id="1"):
    return HRSImportPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, description=description, hsn="5903",
        qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100"), uom="KG",
        net_price=Decimal("1.95"), net_value=Decimal("195.00"),
        tax_type="IGST", currency_after_taxes="INR", exchange_rate=Decimal("93.80"),
        total_inclusive_value=Decimal("18291.00"),
    )


def _mir(mir_no="MIR-A", source_row_ref="1", material_description="PTFE Coated Fabric",
         party_name="Global Polymers Inc"):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no=mir_no, mir_date=datetime.date(2026, 5, 1),
        party_name=party_name, po_number_raw="", material_description=material_description,
        qty=Decimal("100"), uom="KG", rate=Decimal("182.91"), net=Decimal("18291.00"),
        taxable_value=Decimal("18291.00"), source_row_ref=source_row_ref, is_active=True,
    )


def _editor(email="e@ravasco.com", plants=None):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role="editor", plants=plants or []))
    return client


@pytest.mark.django_db
class TestImportSetMirMatch:
    def setup_method(self):
        self.po = _import_po()
        self.item = _import_item(self.po)
        self.mir = _mir()
        self.client = _editor()
        self.url = f"/api/imports/purchase-orders/hrs/{self.po.po_number}/mir-match"

    def test_editor_pins_an_import_line(self):
        res = self.client.patch(
            self.url, {"itemRef": "0", "mirNo": "MIR-A", "reason": "checked the BOE"}, format="json")
        assert res.status_code == 200
        pin = ManualMirMatch.objects.get()
        assert pin.po_kind == ManualMirMatch.POKind.IMPORT
        assert (pin.plant, pin.po_number, pin.item_ref, pin.mir_no) == (
            SyncRun.Plant.HRS, self.po.po_number, "0", "MIR-A")
        assert pin.item_description == "PTFE Coated Fabric"
        self.item.refresh_from_db()
        assert self.item.mir_match.mir_entry_id == self.mir.id
        assert self.item.mir_match.manually_pinned is True

    def test_viewer_cannot_pin(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        res = client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 403
        assert ManualMirMatch.objects.count() == 0

    def test_editor_scoped_to_another_plant_cannot_pin(self):
        res = _editor(email="scoped@ravasco.com", plants=["vapi"]).patch(
            self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 403
        assert ManualMirMatch.objects.count() == 0

    def test_unknown_plant_is_404(self):
        res = self.client.patch(
            f"/api/imports/purchase-orders/nosuchplant/{self.po.po_number}/mir-match",
            {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 404

    def test_unknown_mir_number_is_rejected(self):
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": "NOPE"}, format="json")
        assert res.status_code == 400
        assert ManualMirMatch.objects.count() == 0

    def test_unknown_item_ref_is_rejected(self):
        res = self.client.patch(self.url, {"itemRef": "9", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 404
        assert ManualMirMatch.objects.count() == 0

    def test_clear_removes_only_the_import_pin(self):
        """po_kind is part of the delete filter too - clearing an import pin
        must not take a domestic one for the same number with it."""
        dom = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name=self.po.po_number, po_number=self.po.po_number,
            vendor_name="Rubamin Private Limited")
        HRSDomesticPOLineItem.objects.create(
            purchase_order=dom, item_id="1", description="SBR 1502",
            qty=Decimal("10"), uom="KG", net_price=Decimal("1"), net_value=Decimal("10"))
        ManualMirMatch.objects.create(
            plant=SyncRun.Plant.HRS, po_kind=ManualMirMatch.POKind.DOMESTIC,
            po_number=self.po.po_number, item_ref="0", mir_no="MIR-A",
            item_description="SBR 1502")
        self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert ManualMirMatch.objects.count() == 2

        self.client.patch(self.url, {"itemRef": "0", "clear": True}, format="json")
        remaining = ManualMirMatch.objects.get()
        assert remaining.po_kind == ManualMirMatch.POKind.DOMESTIC

    def test_empty_mir_no_records_a_deliberate_no_match(self):
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": ""}, format="json")
        assert res.status_code == 200
        assert ManualMirMatch.objects.get().mir_no == ""
        self.item.refresh_from_db()
        assert getattr(self.item, "mir_match", None) is None


@pytest.mark.django_db
class TestImportMirCandidates:
    def setup_method(self):
        self.po = _import_po()
        self.item = _import_item(self.po)
        self.client = _editor()
        self.url = f"/api/imports/purchase-orders/hrs/{self.po.po_number}/mir-candidates"

    def test_search_returns_one_entry_per_mir_number(self):
        _mir("MIR-MULTI", "1", material_description="PTFE Coated Fabric")
        _mir("MIR-MULTI", "2", material_description="Nylon Fabric")
        candidates = self.client.get(self.url, {"q": "MIR-MULTI"}).json()["candidates"]
        assert len(candidates) == 1
        assert candidates[0]["rowCount"] == 2

    def test_claimed_by_reports_an_import_holder(self):
        _mir("MIR-A", "1")
        other_po = _import_po("IMP9101")
        _import_item(other_po)
        _editor(email="e2@ravasco.com").patch(
            f"/api/imports/purchase-orders/hrs/{other_po.po_number}/mir-match",
            {"itemRef": "0", "mirNo": "MIR-A"}, format="json")

        claimed = self.client.get(self.url, {"q": "MIR-A"}).json()["candidates"][0]["claimedBy"]
        assert len(claimed) == 1
        assert claimed[0]["poNumber"] == other_po.po_number
        assert claimed[0]["isImport"] is True
        assert claimed[0]["manuallyPinned"] is True

    def test_claimed_by_also_reports_a_DOMESTIC_holder(self):
        """Both kinds compete for the same MIR table, so the import picker
        must warn about a domestic line holding the document too."""
        mir = _mir("MIR-A", "1", material_description="SBR 1502",
                   party_name="Rubamin Private Limited")
        dom = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name="3000009600", po_number="3000009600",
            po_created_date=datetime.date(2026, 4, 25), vendor_name="Rubamin Private Limited")
        HRSDomesticPOLineItem.objects.create(
            purchase_order=dom, item_id="1", description="SBR 1502",
            qty=Decimal("100"), uom="KG", net_price=Decimal("182.91"),
            net_value=Decimal("18291.00"))
        _editor(email="e3@ravasco.com").patch(
            f"/api/purchase-orders/{dom.po_number}/mir-match",
            {"itemRef": "0", "mirNo": "MIR-A"}, format="json")

        claimed = self.client.get(self.url, {"q": "MIR-A"}).json()["candidates"][0]["claimedBy"]
        assert [c["poNumber"] for c in claimed] == [dom.po_number]
        assert claimed[0].get("isImport") is None  # domestic rows carry no flag
        assert mir.mir_no == "MIR-A"

    def test_inactive_mir_rows_are_not_offered(self):
        stale = _mir("MIR-OLD", "1")
        stale.is_active = False
        stale.save(update_fields=["is_active"])
        assert self.client.get(self.url, {"q": "MIR-OLD"}).json()["candidates"] == []

    def test_out_of_scope_plant_is_404_not_403(self):
        """Matches this router's existing convention - it does not confirm a
        PO exists for a plant the caller is not scoped to."""
        _mir("MIR-A", "1")
        client = APIClient()
        client.force_authenticate(
            user=make_user(email="v9@ravasco.com", role="viewer", plants=["vapi"]))
        assert client.get(self.url, {"q": "MIR-A"}).status_code == 404

    def test_viewer_may_read_candidates(self):
        _mir("MIR-A", "1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="v8@ravasco.com", role="viewer"))
        res = client.get(self.url, {"q": "MIR-A"})
        assert res.status_code == 200
        assert [c["mirNo"] for c in res.json()["candidates"]] == ["MIR-A"]


@pytest.mark.django_db
class TestImportItemRefIsExposed:
    def test_import_payload_carries_item_ref_and_pin_state(self):
        po = _import_po()
        first = _import_item(po, description="PTFE Coated Fabric", item_id="1")
        second = _import_item(po, description="Nylon Fabric", item_id="2")
        client = _editor()
        client.patch(f"/api/imports/purchase-orders/hrs/{po.po_number}/mir-match",
                     {"itemRef": "0", "mirNo": ""}, format="json")

        payload = client.get("/api/imports/purchase-orders").json()
        row = next(p for p in payload["purchaseOrders"] if p["poNumber"] == po.po_number)
        refs = {i["description"]: i["itemRef"] for i in row["items"]}
        assert refs[first.description] == "0"
        assert refs[second.description] == "1"
        assert all("manuallyPinned" in i for i in row["items"])
