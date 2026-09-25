"""
API fixes from the 2026-09-25 dashboard audit, each on the shape that
exposed it:

- an import edit on an order that repeats one item_id across its shipment
  lines landing on the first of them (Vapi 1000001508);
- exchange rate / BOE / Bill of Lading / landed value edits not re-matching;
- the MIR picker's default list burying the order's own receipts under the
  80 newest MIR rows;
- the RoDTEP ledger showing every plant's import POs to a plant-scoped user;
- a correction or pin never reaching other open dashboards (dataChangedAt);
- a queued sync with no worker reading "syncing..." for 15 minutes.
"""

import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    ImportPOCorrection,
    RTPVapiImportPOLineItem,
    RTPVapiImportPurchaseOrder,
    SyncRun,
)


def _client(role="editor", plants=None, email="e@ravasco.com"):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role=role, plants=plants or []))
    return client


def _import_po(po_number="1000001508"):
    return HRSImportPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name="Akros Trading Co Ltd",
        currency="USD", total_value=Decimal("1"),
    )


def _import_line(po, item_id="22001902", fx=Decimal("97.20")):
    return HRSImportPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, description="Chloroprene", hsn="40024900",
        qty_as_per_po=Decimal("127000"), qty_as_per_boe=Decimal("54080"), uom="KG",
        net_price=Decimal("2.2"), net_value=Decimal("279400"), exchange_rate=fx,
    )


@pytest.mark.django_db
class TestImportEditsAddressALine:
    def setup_method(self):
        self.po = _import_po()
        self.first = _import_line(self.po)
        self.second = _import_line(self.po, fx=None)  # same item_id, as on 12 Vapi orders
        self.url = f"/api/imports/purchase-orders/hrs/{self.po.po_number}/fields"

    def test_a_position_edits_that_line_and_no_other(self):
        res = _client().patch(self.url, {"itemId": "#1", "field": "exchange_rate", "value": "96.80"}, format="json")
        assert res.status_code == 200
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        assert self.first.exchange_rate == Decimal("97.20")
        assert self.second.exchange_rate == Decimal("96.80")
        correction = ImportPOCorrection.objects.get()
        assert (correction.item_id, correction.item_ref) == ("22001902", "1")

    def test_a_bare_item_id_naming_two_lines_is_refused_not_guessed(self):
        res = _client().patch(self.url, {"itemId": "22001902", "field": "exchange_rate", "value": "96.80"}, format="json")
        assert res.status_code == 400
        self.second.refresh_from_db()
        assert self.second.exchange_rate is None

    def test_an_exchange_rate_edit_re_matches(self, monkeypatch):
        from apps.api.routers import imports_views
        calls = []
        monkeypatch.setitem(imports_views._RUN_FULL_MATCH, "hrs", lambda: calls.append(1) or {})
        for field, value in (("exchange_rate", "96.8"), ("boe_number", "123456"),
                             ("bill_of_lading_number", "BL1"), ("total_inclusive_value", "1000")):
            _client(email=f"{field}@ravasco.com").patch(self.url, {"itemId": "#0", "field": field, "value": value}, format="json")
        assert len(calls) == 4


@pytest.mark.django_db
class TestPickerDefaultListLeadsWithTheOrdersOwnReceipts:
    def test_a_receipt_citing_the_po_is_listed_behind_ninety_newer_rows(self):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name="PO_X", po_number="3000009001", po_created_date=datetime.date(2026, 1, 1),
            vendor_name="Rubamin", tax_type="IGST", total_value=Decimal("1"), total_inclusive_value=Decimal("1"))
        HRSDomesticPOLineItem.objects.create(purchase_order=po, item_id="1", description="SBR", hsn="1",
                                             qty=Decimal("1"), uom="KG", net_price=Decimal("1"), net_value=Decimal("1"))

        def mir(no, ref, day, po_raw=""):
            HRSMIREntry.objects.create(month="x", mir_no=no, mir_date=datetime.date(2026, 1, 1) + datetime.timedelta(days=day),
                                       party_name="Other", po_number_raw=po_raw, material_description="Y",
                                       qty=Decimal("1"), uom="KG", rate=Decimal("1"), net=Decimal("1"),
                                       taxable_value=Decimal("1"), source_row_ref=ref, is_active=True)
        mir("MIR-OWN", "1", 0, po_raw="3000009001")
        for n in range(90):
            mir(f"MIR-NEW-{n}", str(n + 2), 30 + n)

        res = _client(role="viewer").get(f"/api/purchase-orders/{po.po_number}/mir-candidates")
        numbers = [c["mirNo"] for c in res.json()["candidates"]]
        assert numbers[0] == "MIR-OWN"


@pytest.mark.django_db
class TestLicenceLedgersArePlantScoped:
    def test_an_hrs_only_user_sees_no_vapi_citation(self):
        for model_po, model_line, number in (
            (HRSImportPurchaseOrder, HRSImportPOLineItem, "4500000001"),
            (RTPVapiImportPurchaseOrder, RTPVapiImportPOLineItem, "1000001598"),
        ):
            po = model_po.objects.create(po_drive_folder_name=f"PO_{number}", po_number=number,
                                         po_created_date=datetime.date(2026, 4, 1), vendor_name="V", currency="USD",
                                         total_value=Decimal("1"))
            model_line.objects.create(purchase_order=po, item_id="1", description="Neoprene", hsn="4002",
                                      qty_as_per_po=Decimal("1"), qty_as_per_boe=Decimal("1"), uom="KG",
                                      net_price=Decimal("1"), net_value=Decimal("1"),
                                      license_type="RoDTEP", license_number="1234567890")

        scoped = _client(role="viewer", plants=["hrs"], email="hrs@ravasco.com").get("/api/imports/rodtep")
        everyone = _client(role="viewer", email="all@ravasco.com").get("/api/imports/rodtep")

        assert "1000001598" not in scoped.content.decode()
        assert "4500000001" in scoped.content.decode()
        assert "1000001598" in everyone.content.decode()


@pytest.mark.django_db
class TestSyncStatusReportsDataChangesAndStalls:
    def setup_method(self):
        cache.clear()

    def test_a_correction_moves_data_changed_at(self):
        before = _client(role="viewer", email="v@ravasco.com").get("/api/sync-status").json()["dataChangedAt"]
        po = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name="PO_Y", po_number="3000009002", po_created_date=datetime.date(2026, 1, 1),
            vendor_name="Rubamin", tax_type="IGST", total_value=Decimal("1"), total_inclusive_value=Decimal("1"))
        _client().patch(f"/api/purchase-orders/{po.po_number}/fields", {"field": "remarks", "value": "checked"}, format="json")
        after = _client(role="viewer", email="v2@ravasco.com").get("/api/sync-status").json()["dataChangedAt"]
        assert before is None
        assert after is not None

    def test_a_sync_queued_minutes_ago_with_no_step_started_is_stalled(self):
        from apps.services.sync_trigger import _lock_key
        cache.set(_lock_key("hrs"), (timezone.now() - datetime.timedelta(minutes=5)).isoformat(), 900)
        data = _client(role="viewer").get("/api/sync-status").json()
        assert data["syncInProgress"] is True
        assert data["syncStalled"] is True

    def test_a_sync_whose_first_step_started_is_not_stalled(self):
        from apps.services.sync_trigger import _lock_key
        queued = timezone.now() - datetime.timedelta(minutes=5)
        cache.set(_lock_key("hrs"), queued.isoformat(), 900)
        SyncRun.objects.create(plant=SyncRun.Plant.HRS, source=SyncRun.Source.PO_CSV, status=SyncRun.Status.SUCCESS,
                               started_at=queued + datetime.timedelta(seconds=5))
        assert _client(role="viewer").get("/api/sync-status").json()["syncStalled"] is False


@pytest.mark.django_db
class TestMinorFixes:
    """The audit's minor findings (2026-09-25)."""

    def test_a_pin_clear_flag_sent_as_the_string_false_does_not_clear(self):
        from apps.core.models import ManualMirMatch
        po = _import_po("IMP9200")
        _import_line(po)
        HRSMIREntry.objects.create(month="x", mir_no="MIR-A", mir_date=datetime.date(2026, 5, 1), party_name="P",
                                   po_number_raw="", material_description="Chloroprene", qty=Decimal("1"), uom="KG",
                                   rate=Decimal("1"), net=Decimal("1"), taxable_value=Decimal("1"), source_row_ref="1",
                                   is_active=True)
        url = f"/api/imports/purchase-orders/hrs/{po.po_number}/mir-match"
        client = _client()
        client.patch(url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        client.patch(url, {"itemRef": "0", "mirNo": "MIR-A", "clear": "false"}, format="json")
        assert ManualMirMatch.objects.filter(po_number=po.po_number).exists()

    def test_dismissing_a_flag_on_a_po_that_does_not_exist_is_404(self):
        res = _client().patch("/api/imports/purchase-orders/hrs/NOPE/flags/dismiss", {"flagKey": "F2:1"}, format="json")
        assert res.status_code == 404

    def test_the_import_match_payload_names_who_dismissed_it(self):
        from apps.api.routers.imports_views import _mir_match_dict
        po = _import_po("IMP9300")
        line = _import_line(po)
        mir = HRSMIREntry.objects.create(month="x", mir_no="MIR-D", mir_date=datetime.date(2026, 5, 1), party_name="P",
                                         po_number_raw="", material_description="Chloroprene", qty=Decimal("1"),
                                         uom="KG", rate=Decimal("1"), net=Decimal("1"), taxable_value=Decimal("1"),
                                         source_row_ref="2", is_active=True)
        from apps.core.models import HRSImportPOMirMatch
        reviewer = make_user(email="d@ravasco.com", role="editor")
        HRSImportPOMirMatch.objects.create(po_line_item=line, mir_entry=mir, match_score=Decimal("1"),
                                           dismissed_by_override=True, dismissed_by=reviewer)
        line.refresh_from_db()
        assert _mir_match_dict(line)["dismissedBy"] == "d@ravasco.com"

    def test_the_kpi_summary_is_vendor_and_date_only(self):
        HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name="PO_S", po_number="3000009003", po_created_date=datetime.date(2026, 9, 1),
            vendor_name="Rubamin", tax_type="IGST", total_value=Decimal("1"), total_inclusive_value=Decimal("1"))
        data = _client(role="viewer").get("/api/purchase-orders/summary").json()
        assert data["purchaseOrders"] == [{"vendorName": "Rubamin", "createdDate": "2026-09-01"}]

    def test_the_kpi_summary_is_plant_scoped(self):
        res = _client(role="viewer", plants=["hrs"], email="h2@ravasco.com").get("/api/vapi/purchase-orders/summary")
        assert res.status_code == 403

    def test_the_advance_licence_ledger_does_not_query_per_citation(self, django_assert_max_num_queries):
        client = _client(role="viewer", email="al@ravasco.com")
        with django_assert_max_num_queries(20):
            assert client.get("/api/imports/advance-license").status_code == 200
