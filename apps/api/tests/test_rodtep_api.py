"""
Integration tests for the RoDTEP scrip ledger API
(apps/api/routers/imports_views.py's rodtep_* views). Same convention as
test_imports_api.py - APIClient.force_authenticate() with a plain PTUser,
real Postgres, no mocking.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSImportPOLineItem, HRSImportPurchaseOrder, RodtepScrollEntry, RodtepUsage

LEDGER_URL = "/api/imports/rodtep"
USAGE_URL = "/api/imports/rodtep/usage"


def _make_entry(script_no="2603043916", sb_number="9551651", sanctioned_amount="22889.00", **overrides):
    defaults = dict(
        script_no=script_no, sb_number=sb_number, sanctioned_amount=sanctioned_amount,
        location="INNSA1",
    )
    defaults.update(overrides)
    return RodtepScrollEntry.objects.create(**defaults)


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

    def test_balance_reflects_usage(self):
        _make_entry(script_no="SCRIPT-A", sb_number="SB1", sanctioned_amount="1000.00")
        RodtepUsage.objects.create(script_no="SCRIPT-A", used_amount="400.00")

        resp = self.client.get(LEDGER_URL)
        script = next(s for s in resp.data["scripts"] if s["scriptNo"] == "SCRIPT-A")
        assert script["totalUsed"] == Decimal("400.00")
        assert script["balance"] == Decimal("600.00")

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
class TestRodtepScriptDetail:
    def setup_method(self):
        self.client = APIClient()
        self.user = make_user(role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_returns_entries_and_usages(self):
        _make_entry(script_no="SCRIPT-A", sb_number="SB1")
        RodtepUsage.objects.create(script_no="SCRIPT-A", used_amount="10.00", boe_number="BOE1")

        resp = self.client.get(f"{LEDGER_URL}/SCRIPT-A")
        assert resp.status_code == 200
        assert len(resp.data["entries"]) == 1
        assert len(resp.data["usages"]) == 1

    def test_unknown_script_returns_404(self):
        resp = self.client.get(f"{LEDGER_URL}/NO-SUCH-SCRIPT")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestRodtepUsageCreate:
    def setup_method(self):
        self.client = APIClient()
        self.editor = make_user(email="editor@ravasco.com", role="editor")
        self.viewer = make_user(email="viewer@ravasco.com", role="viewer")

    def test_viewer_cannot_create_usage(self):
        self.client.force_authenticate(user=self.viewer)
        resp = self.client.post(USAGE_URL, {"scriptNo": "SCRIPT-A", "usedAmount": "10.00"})
        assert resp.status_code == 403

    def test_editor_can_create_usage(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(USAGE_URL, {
            "scriptNo": "SCRIPT-A", "usedAmount": "10.00", "boeNumber": "BOE123",
            "usedDate": "2026-09-09",
        })
        assert resp.status_code == 201
        usage = RodtepUsage.objects.get()
        assert usage.entered_by == self.editor
        assert usage.used_amount == 10

    def test_invalid_amount_returns_400_not_500(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(USAGE_URL, {"scriptNo": "SCRIPT-A", "usedAmount": "not-a-number"})
        assert resp.status_code == 400

    def test_boe_verified_reflects_real_import_line_item(self):
        po = HRSImportPurchaseOrder.objects.create(
            po_drive_folder_name="1000001357", po_number="1000001357", vendor_name="Test Vendor",
        )
        HRSImportPOLineItem.objects.create(purchase_order=po, item_id="1", description="Rubber", boe_number="REALBOE1")

        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(USAGE_URL, {"scriptNo": "SCRIPT-A", "usedAmount": "10.00", "boeNumber": "REALBOE1"})
        assert resp.data["boeVerified"] is True

        resp2 = self.client.post(USAGE_URL, {"scriptNo": "SCRIPT-A", "usedAmount": "10.00", "boeNumber": "MADE-UP-BOE"})
        assert resp2.data["boeVerified"] is False
