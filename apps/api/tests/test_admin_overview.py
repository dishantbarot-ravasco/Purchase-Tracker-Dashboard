"""
Integration tests for GET /api/auth/admin-overview (apps/api/routers/
admin_overview_views.py) - Admin Panel Overview tab data, added 2026-09-07.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import DomesticPOCorrection, HRSDomesticPurchaseOrder, MaterialCorrection, SyncRun


@pytest.mark.django_db
class TestAdminOverview:
    def setup_method(self):
        self.admin = make_user(email="overview-admin@ravasco.com", role="admin", full_name="Overview Admin")
        self.editor = make_user(email="overview-editor@ravasco.com", role="editor", full_name="Overview Editor")
        self.client = APIClient()

    def test_requires_admin_role(self):
        client = APIClient()
        client.force_authenticate(user=self.editor)
        response = client.get("/api/auth/admin-overview")
        assert response.status_code == 403

    def test_returns_empty_shape_with_no_data(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/auth/admin-overview")
        assert response.status_code == 200
        data = response.json()
        assert data == {"topCorrectors": [], "topVendors": [], "recentActivity": []}

    def test_aggregates_correctors_and_recent_activity_across_tables(self):
        DomesticPOCorrection.objects.create(
            plant=SyncRun.Plant.HRS, po_number="PO1", field_name="vendor_name",
            old_value="A", new_value="B", corrected_by=self.editor, corrected_by_email=self.editor.email,
        )
        DomesticPOCorrection.objects.create(
            plant=SyncRun.Plant.HRS, po_number="PO2", field_name="vendor_name",
            old_value="C", new_value="D", corrected_by=self.editor, corrected_by_email=self.editor.email,
        )
        MaterialCorrection.objects.create(
            plant=SyncRun.Plant.RTP_VAPI, lot_id=7, field_name="category",
            old_value="X", new_value="Y", corrected_by=self.admin, corrected_by_email=self.admin.email,
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/auth/admin-overview")
        assert response.status_code == 200
        data = response.json()

        correctors_by_email = {c["email"]: c["count"] for c in data["topCorrectors"]}
        assert correctors_by_email[self.editor.email] == 2
        assert correctors_by_email[self.admin.email] == 1
        assert next(c["fullName"] for c in data["topCorrectors"] if c["email"] == self.editor.email) == "Overview Editor"

        assert len(data["recentActivity"]) == 3
        types = {row["type"] for row in data["recentActivity"]}
        assert types == {"domestic", "material"}

    def test_top_vendors_counts_pos_across_domestic_plant(self):
        HRSDomesticPurchaseOrder.objects.create(po_number="PO100", vendor_name="Acme Rubber Co")
        HRSDomesticPurchaseOrder.objects.create(po_number="PO101", vendor_name="Acme Rubber Co")
        HRSDomesticPurchaseOrder.objects.create(po_number="PO102", vendor_name="Other Vendor")
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/auth/admin-overview")
        assert response.status_code == 200
        vendors_by_name = {v["vendor"]: v["count"] for v in response.json()["topVendors"]}
        assert vendors_by_name["Acme Rubber Co"] == 2
        assert vendors_by_name["Other Vendor"] == 1
