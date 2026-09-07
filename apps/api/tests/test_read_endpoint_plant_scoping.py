"""
Integration tests for read-endpoint plant scoping (added 2026-09-05,
hardening pass) - apps/api/permissions.py's user_can_access_plant(), now
also gating the domestic routers' purchase_orders/materials/stock_trend/
sync_status (apps/api/routers/_domestic_base.py) and imports_views.py's
purchase_orders/purchase_order_detail/sync_status, not just the write
endpoints user_can_edit_plant already gated.

Deliberately low blast-radius, verified directly here: an account with an
EMPTY plants list (the default, and every account that predates this
change) must see zero behavior difference - only an account an admin has
already explicitly scoped to specific plants is newly restricted on reads
too.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSDomesticPurchaseOrder, RTPAchhadDomesticPurchaseOrder, RTPVapiImportPurchaseOrder


@pytest.mark.django_db
class TestDomesticReadEndpointsRespectPlantScoping:
    def setup_method(self):
        HRSDomesticPurchaseOrder.objects.create(po_drive_folder_name="p1", po_number="p1", vendor_name="V")

    def test_unscoped_viewer_can_still_read_every_plant(self):
        """The default (empty plants list) - the vast majority of accounts,
        including every account that existed before this change - must see
        NO behavior difference at all."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        assert client.get("/api/purchase-orders").status_code == 200
        assert client.get("/api/materials").status_code == 200
        assert client.get("/api/sync-status").status_code == 200
        assert client.get("/api/vapi/purchase-orders").status_code == 200

    def test_viewer_scoped_to_a_different_plant_cannot_read_hrs(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v2@ravasco.com", role="viewer", plants=["achhad"]))
        assert client.get("/api/purchase-orders").status_code == 403
        assert client.get("/api/materials").status_code == 403
        assert client.get("/api/sync-status").status_code == 403

    def test_viewer_scoped_to_hrs_can_still_read_hrs(self):
        """Mirror case - scoping isn't accidentally denying everyone, only
        the wrong plant."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="v3@ravasco.com", role="viewer", plants=["hrs"]))
        assert client.get("/api/purchase-orders").status_code == 200

    def test_editor_scoped_to_one_plant_cannot_read_a_different_plants_stock_trend(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor", plants=["vapi"]))
        response = client.get("/api/materials/1/stock-trend")
        assert response.status_code == 403


@pytest.mark.django_db
class TestImportsReadEndpointsRespectPlantScoping:
    def setup_method(self):
        self.po = RTPVapiImportPurchaseOrder.objects.create(
            po_drive_folder_name="IMP-1", po_number="IMP-1", vendor_name="Test Vendor Ltd",
        )
        RTPAchhadDomesticPurchaseOrder.objects.create(po_drive_folder_name="a1", po_number="a1", vendor_name="V")

    def test_unscoped_viewer_sees_every_plant_in_the_combined_list(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.get("/api/imports/purchase-orders")
        assert response.status_code == 200
        assert any(po["poNumber"] == "IMP-1" for po in response.json()["purchaseOrders"])

    def test_scoped_viewer_sees_only_their_plants_in_the_combined_list(self):
        """The combined cross-plant list narrows silently (no error) rather
        than blocking the whole endpoint - a plant with genuinely zero rows
        already behaves this way, this is the same shape."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="v2@ravasco.com", role="viewer", plants=["achhad"]))
        response = client.get("/api/imports/purchase-orders")
        assert response.status_code == 200
        po_numbers = [po["poNumber"] for po in response.json()["purchaseOrders"]]
        assert "IMP-1" not in po_numbers  # vapi - not in this viewer's scope

    def test_scoped_viewer_gets_404_not_403_for_a_po_detail_in_an_out_of_scope_plant(self):
        """404, not 403 - doesn't confirm the PO exists to a caller who
        isn't scoped to see it, same reasoning as the existing
        unknown-plant-segment 404."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="v3@ravasco.com", role="viewer", plants=["achhad"]))
        response = client.get(f"/api/imports/purchase-orders/vapi/{self.po.po_number}")
        assert response.status_code == 404

    def test_scoped_viewer_still_sees_their_own_plant_detail(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v4@ravasco.com", role="viewer", plants=["vapi"]))
        response = client.get(f"/api/imports/purchase-orders/vapi/{self.po.po_number}")
        assert response.status_code == 200

    def test_scoped_viewer_sees_only_their_plant_in_sync_status(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v5@ravasco.com", role="viewer", plants=["hrs"]))
        response = client.get("/api/imports/sync-status")
        assert response.status_code == 200
        assert set(response.json()["sync"].keys()) == {"hrs"}
