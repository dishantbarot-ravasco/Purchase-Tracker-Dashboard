"""
Integration tests for the RoDTEP scrip ledger API
(apps/api/routers/imports_views.py's rodtep_* views). Same convention as
test_imports_api.py - APIClient.force_authenticate() with a plain PTUser,
real Postgres, no mocking.

`POST /api/imports/rodtep/usage` and its "Log Usage" form were removed
2026-09-22 - the import side of a scrip comes from the imports master CSV's
own License Type/Number columns now (see apps/services/license_links.py).
The legacy RodtepUsage READ path is still exercised below, because existing
hand-entered rows are still displayed.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    RodtepScrollEntry,
    RodtepUsage,
    RTPVapiImportPOLineItem,
    RTPVapiImportPurchaseOrder,
)

LEDGER_URL = "/api/imports/rodtep"


def _make_entry(script_no="2603043916", sb_number="9551651", sanctioned_amount="22889.00", **overrides):
    defaults = dict(
        script_no=script_no, sb_number=sb_number, sanctioned_amount=sanctioned_amount,
        location="INNSA1",
    )
    defaults.update(overrides)
    return RodtepScrollEntry.objects.create(**defaults)


def _make_import_line(
    po_model=HRSImportPurchaseOrder, item_model=HRSImportPOLineItem,
    po_number="1000001357", license_type="RODTEP", license_number="2603043916", **overrides,
):
    po, _ = po_model.objects.get_or_create(
        po_number=po_number,
        defaults=dict(po_drive_folder_name=po_number, vendor_name="Test Vendor"),
    )
    defaults = dict(
        purchase_order=po, item_id="1", description="NEOPRENE RUBBER GNA",
        license_type=license_type, license_number=license_number,
        boe_number="8826527", qty_as_per_boe="100.000", uom="KG",
        total_inclusive_value="9215578.00",
    )
    defaults.update(overrides)
    return item_model.objects.create(**defaults)


@pytest.mark.django_db
class TestRodtepLedger:
    def setup_method(self):
        self.client = APIClient()
        self.user = make_user(role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get(LEDGER_URL)
        assert resp.status_code == 401

    def test_any_role_can_read(self):
        _make_entry()
        resp = self.client.get(LEDGER_URL)
        assert resp.status_code == 200

    def test_groups_by_script_and_sums_sanctioned_amount(self):
        _make_entry(script_no="SCRIPT-A", sb_number="SB1", sanctioned_amount="100.00")
        _make_entry(script_no="SCRIPT-A", sb_number="SB2", sanctioned_amount="200.00")
        _make_entry(script_no="SCRIPT-B", sb_number="SB3", sanctioned_amount="50.00")

        resp = self.client.get(LEDGER_URL)
        assert resp.status_code == 200
        scripts = {s["scriptNo"]: s for s in resp.data["scripts"]}
        assert scripts["SCRIPT-A"]["totalSanctioned"] == Decimal("300.00")
        assert scripts["SCRIPT-A"]["entryCount"] == 2
        assert scripts["SCRIPT-A"]["totalUsed"] == 0
        assert scripts["SCRIPT-A"]["balance"] == Decimal("300.00")
        assert scripts["SCRIPT-B"]["totalSanctioned"] == Decimal("50.00")

    def test_balance_reflects_legacy_logged_usage(self):
        _make_entry(script_no="SCRIPT-A", sb_number="SB1", sanctioned_amount="1000.00")
        RodtepUsage.objects.create(script_no="SCRIPT-A", used_amount="400.00")

        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "SCRIPT-A")
        assert script["totalUsed"] == Decimal("400.00")
        assert script["balance"] == Decimal("600.00")
        assert resp.data["summary"]["hasLoggedUsage"] is True

    def test_has_logged_usage_is_false_when_that_table_is_empty(self):
        """The flag the frontend uses to decide whether to show the Total
        Used / Balance columns at all. With no hand-entered rows those two
        would render Balance == Sanctioned on every row, reading as "none of
        this scrip has been spent" while the CSV says otherwise."""
        _make_entry(script_no="SCRIPT-A", sb_number="SB1", sanctioned_amount="1000.00")
        resp = self.client.get(LEDGER_URL)
        assert resp.data["summary"]["hasLoggedUsage"] is False

    def test_script_with_usage_but_no_synced_ledger_still_appears(self):
        """A usage entry can be logged before its script's own ledger file
        has been synced yet (see RodtepUsage's own docstring) - the ledger
        view must still surface it, with 0 sanctioned rather than dropping
        it silently."""
        RodtepUsage.objects.create(script_no="NOT-YET-SYNCED", used_amount="500.00")

        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "NOT-YET-SYNCED")
        assert script["totalSanctioned"] == 0
        assert script["totalUsed"] == Decimal("500.00")
        assert script["balance"] == Decimal("-500.00")


@pytest.mark.django_db
class TestRodtepImportSideFromTheCsv:
    """The join added 2026-09-22 - which imports were cleared under a scrip,
    read from each plant's own Imports Purchase Data master CSV."""

    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(role="viewer"))

    def test_a_scrip_cited_by_an_import_reports_that_line(self):
        _make_entry(script_no="2603043916")
        _make_import_line()

        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "2603043916")
        assert script["imports"]["lineCount"] == 1
        assert script["imports"]["poCount"] == 1
        assert script["imports"]["plants"] == ["HRS-Silvassa"]
        assert script["imports"]["landedValue"] == Decimal("9215578.00")
        assert resp.data["summary"]["scripsCited"] == 1
        assert resp.data["summary"]["scripsNeverCited"] == 0

    def test_a_scrip_no_import_names_is_reported_as_idle_not_as_missing(self):
        _make_entry(script_no="2603044111")
        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "2603044111")
        assert script["imports"]["lineCount"] == 0
        assert resp.data["summary"]["scripsNeverCited"] == 1

    def test_a_multi_scrip_cell_credits_both_scrips_and_flags_the_line_shared(self):
        _make_entry(script_no="2603043916", sb_number="SB1")
        _make_entry(script_no="2603044111", sb_number="SB2")
        _make_import_line(license_number="2603043916/2603044111")

        resp = self.client.get(LEDGER_URL)
        scripts = {s["scriptNo"]: s for s in resp.data["scripts"]}
        assert scripts["2603043916"]["imports"]["lineCount"] == 1
        assert scripts["2603044111"]["imports"]["lineCount"] == 1
        # The same one line, so its landed value is NOT this scrip's own
        # share - the payload has to say so or the column reads as additive.
        assert scripts["2603043916"]["imports"]["sharedLines"] == 1

    def test_a_cited_scrip_we_hold_no_file_for_is_its_own_bucket(self):
        """Not folded in among real ledger rows with 0 sanctioned - the fix
        is upstream (add the file, or correct the CSV), which is a different
        action from anything a ledger row implies."""
        _make_import_line(license_number="2603099999")

        resp = self.client.get(LEDGER_URL)
        assert resp.data["scripts"] == []
        assert len(resp.data["unknownScrips"]) == 1
        unknown = resp.data["unknownScrips"][0]
        assert unknown["licenseNumber"] == "2603099999"
        assert unknown["lineCount"] == 1
        assert unknown["boeNumbers"] == ["8826527"]

    def test_a_line_naming_a_licence_with_no_scheme_is_reported_separately(self):
        _make_import_line(license_type="", license_number="2603043916")
        resp = self.client.get(LEDGER_URL)
        assert resp.data["scripts"] == []
        assert resp.data["unknownScrips"] == []
        assert len(resp.data["unclassifiedCitations"]) == 1
        assert resp.data["unclassifiedCitations"][0]["licenseTypeRaw"] == ""

    def test_the_join_is_cross_plant(self):
        _make_entry(script_no="2603043916")
        _make_import_line(po_number="1000001357")
        _make_import_line(
            po_model=RTPVapiImportPurchaseOrder, item_model=RTPVapiImportPOLineItem,
            po_number="1000001598", total_inclusive_value="1.00",
        )

        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "2603043916")
        assert script["imports"]["plants"] == ["HRS-Silvassa", "RTP-Vapi"]


@pytest.mark.django_db
class TestRodtepScriptDetail:
    def setup_method(self):
        self.client = APIClient()
        self.user = make_user(role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_returns_entries_usages_and_imports(self):
        _make_entry(script_no="2603043916", sb_number="SB1")
        RodtepUsage.objects.create(script_no="2603043916", used_amount="10.00", boe_number="BOE1")
        _make_import_line(license_number="2603043916")

        resp = self.client.get(f"{LEDGER_URL}/2603043916")
        assert resp.status_code == 200
        assert len(resp.data["entries"]) == 1
        assert len(resp.data["usages"]) == 1
        assert len(resp.data["imports"]) == 1
        assert resp.data["imports"][0]["boeNumber"] == "8826527"
        assert resp.data["importTotals"]["lineCount"] == 1

    def test_a_scrip_only_the_csv_knows_about_still_resolves(self):
        """It is reachable from the ledger's own unknownScrips list, so
        404ing would be the wrong answer to a link the panel just drew."""
        _make_import_line(license_number="2603099999")
        resp = self.client.get(f"{LEDGER_URL}/2603099999")
        assert resp.status_code == 200
        assert resp.data["entries"] == []
        assert len(resp.data["imports"]) == 1

    def test_unknown_script_returns_404(self):
        resp = self.client.get(f"{LEDGER_URL}/NO-SUCH-SCRIPT")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestRodtepUsageEndpointIsGone:
    def test_posting_usage_is_not_a_route_any_more(self):
        client = APIClient()
        client.force_authenticate(user=make_user(role="editor"))
        resp = client.post(f"{LEDGER_URL}/usage", {"scriptNo": "SCRIPT-A", "usedAmount": "10.00"})
        # Falls through to rodtep_script_detail's <str:script_no>, which
        # accepts GET only - either shape proves the write path is gone.
        assert resp.status_code in (404, 405)
