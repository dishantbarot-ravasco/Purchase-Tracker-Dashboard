"""
Integration tests for the Advance License ledger API
(apps/api/routers/imports_views.py's advance_license_* views). Same
convention as test_rodtep_api.py - APIClient.force_authenticate() with a
plain PTUser, real Postgres, no mocking.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    AdvanceLicense,
    AdvanceLicenseMaterial,
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
)

LEDGER_URL = "/api/imports/advance-license"


def _make_import_line(po_number="1000001519", license_number="0311051817", **overrides):
    po, _ = HRSImportPurchaseOrder.objects.get_or_create(
        po_number=po_number,
        defaults=dict(po_drive_folder_name=po_number, vendor_name="Test Vendor"),
    )
    defaults = dict(
        purchase_order=po, item_id="1", description="Synthetic Rubber SBR-1502",
        license_type="ADVANCE", license_number=license_number,
        boe_number="3448562", qty_as_per_boe="100.000", uom="KG",
        total_inclusive_value="20135304.00",
    )
    defaults.update(overrides)
    return HRSImportPOLineItem.objects.create(**defaults)


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


@pytest.mark.django_db
class TestAdvanceLicenseDerivedInsights:
    """Everything added 2026-09-22: utilisation from the workbook's own usage
    columns, validity countdowns, the import-side join, and the cross-check
    between the two sources."""

    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(role="viewer"))

    def _license_row(self):
        return self.client.get(LEDGER_URL).data["licenses"][0]

    def test_cif_utilisation_sums_the_workbooks_value_imported_column(self):
        lic = _make_license(cif_value_authorized=Decimal("1000.00"))
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", value_imported=Decimal("250.00"),
        )
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", value_imported=Decimal("150.00"),
        )
        usage = self._license_row()["usage"]
        assert usage["valueImported"] == Decimal("400.00")
        assert usage["cifRemaining"] == Decimal("600.00")
        assert usage["cifUtilisedPct"] == Decimal("40")

    def test_utilisation_pct_is_null_not_zero_when_nothing_was_authorised(self):
        """A percentage of zero is a question about the source row, not
        '0% used' - rendering it as 0% would state the opposite."""
        _make_license(cif_value_authorized=Decimal("0"))
        assert self._license_row()["usage"]["cifUtilisedPct"] is None

    def test_the_authorised_qty_is_taken_once_per_material_never_summed(self):
        """The workbook repeats the licence-level and material-level
        authorisation on every usage row of the same material (see
        AdvanceLicenseMaterial's docstring). Summing it would multiply the
        authorisation by the number of times the material was imported."""
        lic = _make_license()
        for imported in ("10.000", "15.000"):
            AdvanceLicenseMaterial.objects.create(
                license=lic, material_description="SBR 1502",
                qty_authorized=Decimal("100.000"), qty_imported=Decimal(imported),
                boe_number="BOE" + imported,
            )
        rollup = self._license_row()["materialRollup"]
        assert len(rollup) == 1
        assert rollup[0]["qtyAuthorized"] == Decimal("100.000")
        assert rollup[0]["qtyImported"] == Decimal("25.000")
        assert rollup[0]["qtyRemaining"] == Decimal("75.000")
        assert rollup[0]["usageRows"] == 2

    def test_a_blank_first_usage_row_does_not_report_the_material_unauthorised(self):
        lic = _make_license()
        AdvanceLicenseMaterial.objects.create(license=lic, material_description="SBR 1502")
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", qty_authorized=Decimal("100.000"),
        )
        assert self._license_row()["materialRollup"][0]["qtyAuthorized"] == Decimal("100.000")

    def test_validity_countdown_is_in_plant_time_and_flags_expiry(self):
        today = timezone.localdate()
        _make_license(export_validity_date=today + datetime.timedelta(days=30))
        validity = self._license_row()["validity"]
        assert validity["exportDaysLeft"] == 30
        assert validity["exportExpired"] is False
        assert validity["exportExpiringSoon"] is True

    def test_an_expired_export_obligation_is_reported_as_expired(self):
        today = timezone.localdate()
        _make_license(export_validity_date=today - datetime.timedelta(days=5))
        validity = self._license_row()["validity"]
        assert validity["exportDaysLeft"] == -5
        assert validity["exportExpired"] is True
        assert validity["exportExpiringSoon"] is False
        assert self.client.get(LEDGER_URL).data["summary"]["exportExpired"] == 1

    def test_a_licence_with_no_validity_date_reports_null_not_a_countdown(self):
        _make_license(export_validity_date=None)
        validity = self._license_row()["validity"]
        assert validity["exportDaysLeft"] is None
        assert validity["exportExpired"] is False

    def test_imports_citing_a_licence_are_joined_from_the_master_csv(self):
        _make_license(license_number="0311051817")
        _make_import_line(license_number="0311051817")
        row = self._license_row()
        assert row["imports"]["lineCount"] == 1
        assert row["importCitations"][0]["poNumber"] == "1000001519"
        assert self.client.get(LEDGER_URL).data["summary"]["neverCited"] == 0

    def test_a_short_licence_number_in_the_csv_still_joins(self):
        """The leading-zero rule. The CSV writes this authorisation both as
        '311051817' and '0311051817'; unnormalised it reads as a licence we
        do not hold."""
        _make_license(license_number="0311051817")
        _make_import_line(license_number="311051817")
        assert self._license_row()["imports"]["lineCount"] == 1
        assert self.client.get(LEDGER_URL).data["unknownLicenses"] == []

    def test_a_cited_licence_we_do_not_hold_goes_to_its_own_bucket(self):
        _make_license(license_number="0311051817")
        _make_import_line(license_number="0311099999")
        data = self.client.get(LEDGER_URL).data
        assert data["licenses"][0]["imports"]["lineCount"] == 0
        assert [u["licenseNumber"] for u in data["unknownLicenses"]] == ["0311099999"]

    def test_boe_cross_check_reports_each_side_the_other_is_missing(self):
        lic = _make_license(license_number="0311051817")
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", boe_number="WORKBOOK-ONLY",
        )
        _make_import_line(license_number="0311051817", boe_number="CSV-ONLY")
        row = self._license_row()
        assert row["boeCrossCheck"]["workbookOnly"] == ["WORKBOOK-ONLY"]
        assert row["boeCrossCheck"]["csvOnly"] == ["CSV-ONLY"]
        assert self.client.get(LEDGER_URL).data["summary"]["boeGaps"] == 2

    def test_a_boe_both_sources_agree_on_is_not_a_gap(self):
        lic = _make_license(license_number="0311051817")
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", boe_number="3448562",
        )
        _make_import_line(license_number="0311051817", boe_number="3448562")
        row = self._license_row()
        assert row["boeCrossCheck"] == {"workbookOnly": [], "csvOnly": []}
        # And that BOE really is one the Import dashboard holds.
        assert row["materials"][0]["boeVerified"] is True

    def test_summary_rolls_up_across_licences(self):
        _make_license(license_number="0311047672", cif_value_authorized=Decimal("1000.00"))
        lic = _make_license(license_number="0311051817", cif_value_authorized=Decimal("3000.00"))
        AdvanceLicenseMaterial.objects.create(
            license=lic, material_description="SBR 1502", value_imported=Decimal("1000.00"),
        )
        summary = self.client.get(LEDGER_URL).data["summary"]
        assert summary["licenseCount"] == 2
        assert summary["cifAuthorized"] == Decimal("4000.00")
        assert summary["cifImported"] == Decimal("1000.00")
        assert summary["cifRemaining"] == Decimal("3000.00")
        assert summary["cifUtilisedPct"] == Decimal("25")
        assert summary["neverCited"] == 2

    def test_no_licences_synced_yet_still_returns_a_usable_summary(self):
        data = self.client.get(LEDGER_URL).data
        assert data["licenses"] == []
        assert data["summary"]["licenseCount"] == 0
        assert data["summary"]["cifUtilisedPct"] is None
