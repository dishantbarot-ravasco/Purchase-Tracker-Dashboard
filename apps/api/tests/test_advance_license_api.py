"""
Integration tests for the Advance License ledger API
(apps/api/routers/imports_views.py's advance_license_* views). Same
convention as test_rodtep_api.py - APIClient.force_authenticate() with a
plain PTUser, real Postgres, no mocking.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import AdvanceLicense, AdvanceLicenseMaterial

LEDGER_URL = "/api/imports/advance-license"


def _make_license(license_number="0311047672", **overrides):
    defaults = dict(
        license_number=license_number,
        export_product_description="Conveyor and Elevator Textile Belting",
        cif_value_authorized=Decimal("47410000.00"),
        fob_value_export_target=Decimal("53514900.00"),
        export_validity_date="2027-03-26",
    )
    defaults.update(overrides)
    return AdvanceLicense.objects.create(**defaults)


@pytest.mark.django_db
class TestAdvanceLicenseLedger:
    def setup_method(self):
        self.client = APIClient()
        self.user = make_user(role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get(LEDGER_URL)
        assert resp.status_code == 401

    def test_any_role_can_read(self):
        _make_license()
        resp = self.client.get(LEDGER_URL)
        assert resp.status_code == 200

    def test_returns_the_requested_fields_in_order(self):
        lic = _make_license()
        AdvanceLicenseMaterial.objects.create(license=lic, material_description="Synthetic Fabric")
        AdvanceLicenseMaterial.objects.create(license=lic, material_description="Natural Rubber")

        resp = self.client.get(LEDGER_URL)
        assert resp.status_code == 200
        payload = resp.data["licenses"][0]
        assert list(payload.keys())[:6] == [
            "licenseNumber", "exportProductDescription", "cifValueAuthorized",
            "fobValueExportTarget", "exportValidityDate", "materials",
        ]
        assert payload["licenseNumber"] == "0311047672"
        assert payload["cifValueAuthorized"] == Decimal("47410000.00")
        assert payload["fobValueExportTarget"] == Decimal("53514900.00")
        assert payload["exportValidityDate"] == "2027-03-26"
        assert [m["materialDescription"] for m in payload["materials"]] == ["Synthetic Fabric", "Natural Rubber"]

    def test_multiple_licenses_all_returned(self):
        _make_license(license_number="0311047672")
        _make_license(license_number="0311051817")

        resp = self.client.get(LEDGER_URL)
        numbers = {lic["licenseNumber"] for lic in resp.data["licenses"]}
        assert numbers == {"0311047672", "0311051817"}

    def test_no_data_synced_yet_returns_empty_list_not_error(self):
        resp = self.client.get(LEDGER_URL)
        assert resp.status_code == 200
        assert resp.data["licenses"] == []
        assert resp.data["lastSync"] is None


@pytest.mark.django_db
class TestAdvanceLicenseSyncTrigger:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin@ravasco.com", role="admin")
        self.editor = make_user(email="editor@ravasco.com", role="editor")

    def test_non_admin_cannot_trigger_sync(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(f"{LEDGER_URL}/sync-trigger")
        assert resp.status_code == 403

    def test_admin_can_trigger_sync(self, monkeypatch):
        import apps.services.sync_trigger as sync_trigger
        monkeypatch.setattr(sync_trigger, "trigger_advance_license_sync", lambda: True)

        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f"{LEDGER_URL}/sync-trigger")
        assert resp.status_code == 200
        assert resp.data["status"] == "ok"

    def test_already_running_returns_409(self, monkeypatch):
        import apps.services.sync_trigger as sync_trigger
        monkeypatch.setattr(sync_trigger, "trigger_advance_license_sync", lambda: False)

        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f"{LEDGER_URL}/sync-trigger")
        assert resp.status_code == 409
        assert resp.data["status"] == "already_running"
