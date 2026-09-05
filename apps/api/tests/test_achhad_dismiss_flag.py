"""
Characterization test for apps/api/routers/achhad_views.py's dismiss_flag -
written against today's UNMODIFIED code as a safety net before the planned
_domestic_base.py extraction. Mirrors test_dismiss_flag.py's
TestDismissDomesticFlag (HRS-only, per that file's own docstring on why only
one plant's copy of byte-for-byte-shared logic gets direct coverage) for
Achhad specifically.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import FlagDismissal, RTPAchhadPurchaseOrder


@pytest.mark.django_db
class TestAchhadDismissFlag:
    def setup_method(self):
        self.po = RTPAchhadPurchaseOrder.objects.create(
            po_drive_folder_name="3000009998", po_number="3000009998", vendor_name="Test Vendor Ltd",
        )
        self.url = f"/api/achhad/purchase-orders/{self.po.po_number}/flags/dismiss"

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
        assert fd.plant == "RTP-ACHHAD"
        assert fd.po_number == self.po.po_number
        assert fd.flag_key == "Quantity Discrepancy"
        assert fd.dismissed is True

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
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor", plants=["hrs"]))
        response = client.patch(self.url, {"flagKey": "Quantity Discrepancy", "dismissed": True}, format="json")
        assert response.status_code == 403
        assert not FlagDismissal.objects.exists()
