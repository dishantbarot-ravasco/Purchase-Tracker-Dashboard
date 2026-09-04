"""
Integration tests for the manual dismiss/reinstate-a-PO-level-flag endpoint
(apps/services/flag_dismiss.py, wired per-plant in hrs_views.py/
achhad_views.py/vapi_views.py, and cross-plant in imports_views.py) - added
2026-09-04 so every flag shown in a PO detail modal's Flags & Corrections
tab (not just PO<->MIR/MIR<->Stock match flags, which already had
dismiss_match()) can be marked reviewed-and-fine.

HRS only for the Domestic endpoint (matches this codebase's convention of
testing one plant's copy directly for logic shared byte-for-byte across all
three - see test_dismiss_match.py's own module docstring), plus Imports
since that's a genuinely different (cross-plant, plant-in-URL) router.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import FlagDismissal, HRSPurchaseOrder, RTPVapiImportPurchaseOrder


@pytest.mark.django_db
class TestDismissDomesticFlag:
    def setup_method(self):
        self.po = HRSPurchaseOrder.objects.create(
            po_drive_folder_name="1000009998", po_number="1000009998", vendor_name="Test Vendor Ltd",
        )
        self.url = f"/api/purchase-orders/{self.po.po_number}/flags/dismiss"

    def test_viewer_cannot_dismiss(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.patch(self.url, {"flagKey": "Quantity Discrepancy", "dismissed": True}, format="json")
        assert response.status_code == 403
        assert not FlagDismissal.objects.exists()

    def test_editor_can_dismiss_and_reinstate(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))

        response = client.patch(
            self.url, {"flagKey": "Quantity Discrepancy", "dismissed": True, "reason": "Verified against MIR"},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["dismissed"] is True
        assert body["dismissedReason"] == "Verified against MIR"
        assert body["dismissedBy"] == "e@ravasco.com"

        fd = FlagDismissal.objects.get()
        assert fd.plant == "HRS"
        assert fd.po_number == self.po.po_number
        assert fd.flag_key == "Quantity Discrepancy"
        assert fd.dismissed is True

        # Reinstate - clears the audit fields, same as dismiss_match().
        response = client.patch(self.url, {"flagKey": "Quantity Discrepancy", "dismissed": False}, format="json")
        assert response.status_code == 200
        fd.refresh_from_db()
        assert fd.dismissed is False
        assert fd.dismissed_reason == ""
        assert fd.dismissed_by is None
        assert fd.dismissed_at is None

    def test_missing_flag_key_returns_400(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))
        response = client.patch(self.url, {"dismissed": True}, format="json")
        assert response.status_code == 400

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor", plants=["achhad"]))
        response = client.patch(self.url, {"flagKey": "Quantity Discrepancy", "dismissed": True}, format="json")
        assert response.status_code == 403
        assert not FlagDismissal.objects.exists()


@pytest.mark.django_db
class TestDismissImportFlag:
    def setup_method(self):
        self.po = RTPVapiImportPurchaseOrder.objects.create(
            po_drive_folder_name="IMP-1", po_number="IMP-1", vendor_name="Test Vendor Ltd",
        )
        self.url = f"/api/imports/purchase-orders/vapi/{self.po.po_number}/flags/dismiss"

    def test_editor_can_dismiss(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
        response = client.patch(self.url, {"flagKey": "F7:ITEM1", "dismissed": True}, format="json")
        assert response.status_code == 200
        fd = FlagDismissal.objects.get()
        assert fd.plant == "RTP-VAPI"
        assert fd.flag_key == "F7:ITEM1"

    def test_unknown_plant_returns_404(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))
        response = client.patch(
            f"/api/imports/purchase-orders/nope/{self.po.po_number}/flags/dismiss",
            {"flagKey": "F7:ITEM1", "dismissed": True}, format="json",
        )
        assert response.status_code == 404
