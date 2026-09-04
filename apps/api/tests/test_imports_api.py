"""
Integration tests for the Import Purchase Dashboard API
(apps/api/routers/imports_views.py). Uses APIClient.force_authenticate()
with a plain PTUser instance rather than the full login/device-verify/cookie
flow (test_auth_flow.py) - permission classes here only read attributes off
request.user (role, is_active), so force_authenticate is enough to exercise
the actual behavior under test without re-testing login itself.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    ImportPOCorrection,
    RTPVapiImportPOLineItem,
    RTPVapiImportPurchaseOrder,
)

LIST_URL = "/api/imports/purchase-orders"


def _make_po(po_number="1000009001", **overrides):
    defaults = dict(
        po_drive_folder_name=po_number,
        po_number=po_number,
        vendor_name="Test Vendor Ltd",
        currency="USD",
        total_value="1000.00",
    )
    defaults.update(overrides)
    return RTPVapiImportPurchaseOrder.objects.create(**defaults)


def _make_item(po, item_id="1", **overrides):
    defaults = dict(
        purchase_order=po,
        item_id=item_id,
        description="Widget",
        hsn="12345678",
        qty_as_per_po="100",
        uom="KG",
        net_price="1.5",
        net_value="150",
        country_of_origin="China",
    )
    defaults.update(overrides)
    return RTPVapiImportPOLineItem.objects.create(**defaults)


@pytest.mark.django_db
class TestPurchaseOrdersList:
    def setup_method(self):
        self.client = APIClient()
        self.user = make_user(role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        anon = APIClient()
        assert anon.get(LIST_URL).status_code == 401

    def test_combines_plants_and_computes_derived_fields(self):
        po = _make_po()
        _make_item(po, boe_number="BOE1", bill_of_lading_number="BL1", country_of_origin="China")

        response = self.client.get(LIST_URL)
        assert response.status_code == 200
        rows = response.json()["purchaseOrders"]
        assert len(rows) == 1
        row = rows[0]
        assert row["plant"] == "vapi"
        assert row["shipmentStage"] == "Cleared (BOE)"
        assert row["countryOfOrigin"] == "China"

    def test_qty_discrepancy_flagged_only_when_boe_qty_present_and_differs(self):
        po = _make_po()
        _make_item(po, qty_as_per_po="100", qty_as_per_boe=None)  # not yet cleared - no discrepancy
        row = self.client.get(LIST_URL).json()["purchaseOrders"][0]
        assert row["qtyDiscrepancy"] is False

        po2 = _make_po(po_number="1000009002")
        _make_item(po2, qty_as_per_po="100", qty_as_per_boe="90")
        rows = self.client.get(LIST_URL).json()["purchaseOrders"]
        mismatched = next(r for r in rows if r["poNumber"] == "1000009002")
        assert mismatched["qtyDiscrepancy"] is True


@pytest.mark.django_db
class TestCorrectField:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        self.url = f"/api/imports/purchase-orders/vapi/{self.po.po_number}/fields"

    def test_viewer_cannot_correct(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.patch(self.url, {"field": "vendor_name", "value": "New Name"}, format="json")
        assert response.status_code == 403
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Test Vendor Ltd"

    def test_editor_can_correct_po_field_and_audit_row_is_written(self):
        client = APIClient()
        editor = make_user(email="e@ravasco.com", role="editor")
        client.force_authenticate(user=editor)

        response = client.patch(self.url, {"field": "vendor_name", "value": "Corrected Vendor"}, format="json")
        assert response.status_code == 200

        self.po.refresh_from_db()
        assert self.po.vendor_name == "Corrected Vendor"

        correction = ImportPOCorrection.objects.get()
        assert correction.field_name == "vendor_name"
        assert correction.old_value == "Test Vendor Ltd"
        assert correction.new_value == "Corrected Vendor"
        assert correction.corrected_by_email == "e@ravasco.com"

    def test_editor_can_correct_item_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))

        response = client.patch(
            self.url, {"itemId": self.item.item_id, "field": "qty_as_per_boe", "value": "95"}, format="json"
        )
        assert response.status_code == 200
        self.item.refresh_from_db()
        assert self.item.qty_as_per_boe == 95

    def test_rejects_non_editable_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "po_number", "value": "hacked"}, format="json")
        assert response.status_code == 400

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor", plants=["hrs"]))
        response = client.patch(self.url, {"field": "vendor_name", "value": "Nope"}, format="json")
        assert response.status_code == 403
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Test Vendor Ltd"

    def test_invalid_vendor_email_saves_with_a_warning(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "vendor_email", "value": "not-an-email"}, format="json")
        assert response.status_code == 200
        assert "warning" in response.json()

    def test_correction_history_appears_in_detail_payload(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e6@ravasco.com", role="editor"))
        client.patch(self.url, {"field": "vendor_name", "value": "Corrected Vendor"}, format="json")

        detail = client.get(f"/api/imports/purchase-orders/vapi/{self.po.po_number}")
        assert detail.status_code == 200
        corrections = detail.json()["corrections"]
        assert len(corrections) == 1
        assert corrections[0]["fieldName"] == "vendor_name"
        assert corrections[0]["newValue"] == "Corrected Vendor"
