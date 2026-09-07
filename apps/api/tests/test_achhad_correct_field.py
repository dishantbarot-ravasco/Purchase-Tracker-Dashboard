"""
Characterization tests for apps/api/routers/achhad_views.py's correct_field -
written against today's UNMODIFIED code as a safety net before the planned
_domestic_base.py extraction (see CLAUDE.md's "Domestic router
de-duplication"). Mirrors test_hrs_correct_field.py's TestHrsCorrectField
exactly, pointed at Achhad's own models/URL prefix, since achhad_views.py
had no direct test coverage of its own before this file.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import DomesticPOCorrection, RTPAchhadDomesticPOLineItem, RTPAchhadDomesticPurchaseOrder


def _make_po(po_number="3000001511", **overrides):
    defaults = dict(
        po_drive_folder_name=po_number,
        po_number=po_number,
        vendor_name="Test Vendor Ltd",
        vendor_gstin="27AAPFU0939F1ZV",
        currency="INR",
    )
    defaults.update(overrides)
    return RTPAchhadDomesticPurchaseOrder.objects.create(**defaults)


def _make_item(po, item_id="1", **overrides):
    defaults = dict(purchase_order=po, item_id=item_id, description="Widget", qty="100", net_price="1.5")
    defaults.update(overrides)
    return RTPAchhadDomesticPOLineItem.objects.create(**defaults)


@pytest.mark.django_db
class TestAchhadCorrectField:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        self.url = f"/api/achhad/purchase-orders/{self.po.po_number}/fields"

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

        correction = DomesticPOCorrection.objects.get()
        assert correction.plant == "RTP-ACHHAD"
        assert correction.field_name == "vendor_name"
        assert correction.old_value == "Test Vendor Ltd"
        assert correction.new_value == "Corrected Vendor"
        assert correction.corrected_by_email == "e@ravasco.com"

    def test_editor_can_correct_item_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))

        response = client.patch(self.url, {"itemId": self.item.item_id, "field": "qty", "value": "95"}, format="json")
        assert response.status_code == 200
        self.item.refresh_from_db()
        assert self.item.qty == 95

    def test_invalid_decimal_value_returns_400_not_500(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2b@ravasco.com", role="editor"))
        response = client.patch(self.url, {"itemId": self.item.item_id, "field": "qty", "value": "not-a-number"}, format="json")
        assert response.status_code == 400
        self.item.refresh_from_db()
        assert self.item.qty == 100

    def test_rejects_non_editable_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "po_number", "value": "hacked"}, format="json")
        assert response.status_code == 400

    def test_invalid_gstin_saves_with_a_warning(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "vendor_gstin", "value": "NOT-A-GSTIN"}, format="json")
        assert response.status_code == 200
        assert "warning" in response.json()
        self.po.refresh_from_db()
        assert self.po.vendor_gstin == "NOT-A-GSTIN"

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor", plants=["hrs"]))
        response = client.patch(self.url, {"field": "vendor_name", "value": "Nope"}, format="json")
        assert response.status_code == 403
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Test Vendor Ltd"

    def test_editor_scoped_to_this_plant_can_correct(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e6@ravasco.com", role="editor", plants=["achhad"]))
        response = client.patch(self.url, {"field": "vendor_name", "value": "Scoped OK"}, format="json")
        assert response.status_code == 200
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Scoped OK"

    def test_rematch_trigger_field_does_not_error_with_no_mir_data(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e7@ravasco.com", role="editor"))
        response = client.patch(
            self.url, {"itemId": self.item.item_id, "field": "qty", "value": "42"}, format="json"
        )
        assert response.status_code == 200
