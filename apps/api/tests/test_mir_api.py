"""
/api/mir/... (apps/api/routers/mir_views.py) - who may do what, and the wire
shape the MIR page reads. The rules themselves are covered in
apps/services/tests/test_mir_service.py.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Mir, Plant, PurchaseOrder, PurchaseOrderLine, Vendor

TODAY = timezone.localdate()


def _client(role="editor", plants=None, email="e@ravasco.com"):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role=role, plants=plants or []))
    return client


def _po(plant="vapi", number="1000009001"):
    vendor = Vendor.objects.filter(gstin="27AAACP5506B1ZW").first() or Vendor.objects.create(
        gstin="27AAACP5506B1ZW", name="Prime Chemicals", name_key="primechemicals")
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code=plant), po_number=number, vendor=vendor,
                                      po_date=TODAY - datetime.timedelta(days=10), tax_type="IGST",
                                      total_value=Decimal("5000"), total_inclusive_value=Decimal("5900"))
    PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description="SBR 1502", uom="KG",
                                     qty_ordered=Decimal("100"), rate=Decimal("50"))
    return po


def _body(po, plant="hrs", qty="100", **extra):
    body = {"plant": plant, "mir_date": TODAY.isoformat(), "invoice_no": "INV-1", "invoice_date": TODAY.isoformat(),
            "invoice_total": "5900.00", "lines": [{"po_line_id": po.lines.get().id, "qty_received": qty,
                                                    "rate": "50", "gst_rate": "18"}]}
    body.update(extra)
    return body


@pytest.mark.django_db
class TestPostingPermissions:
    def test_a_viewer_can_neither_preview_nor_post(self):
        client, po = _client(role="viewer"), _po()
        assert client.post("/api/mir/preview", _body(po), format="json").status_code == 403
        assert client.post("/api/mir/entries/new", _body(po), format="json").status_code == 403
        assert Mir.objects.count() == 0

    def test_an_editor_scoped_to_hrs_posts_at_hrs_even_against_a_vapi_po(self):
        client, po = _client(plants=["hrs"]), _po(plant="vapi")
        res = client.post("/api/mir/entries/new", _body(po, plant="hrs"), format="json")
        assert res.status_code == 201, res.json()
        data = res.json()
        assert data["mirNo"].startswith("HRS/") and data["createdBy"] == "e@ravasco.com"
        assert data["lines"][0]["poPlant"] == "vapi"
        # money travels as exact strings
        assert data["lines"][0]["lineTotal"] == "5900.00"

    def test_an_editor_scoped_to_hrs_cannot_post_at_vapi(self):
        client, po = _client(plants=["hrs"]), _po()
        assert client.post("/api/mir/entries/new", _body(po, plant="vapi"), format="json").status_code == 403

    def test_a_validation_failure_is_a_400_naming_the_field(self):
        client, po = _client(), _po()
        res = client.post("/api/mir/entries/new", _body(po, qty="60"), format="json")
        assert res.status_code == 400
        assert {"field": "lines.0.qty_reason", "message": "Choose a reason."} in res.json()["errors"]

    def test_preview_reports_mismatches_and_computed_figures_without_saving(self):
        client, po = _client(), _po()
        data = client.post("/api/mir/preview", _body(po, qty="60"), format="json").json()
        assert data["ok"] is False
        assert data["mismatches"][0]["kind"] == "QTY_SHORT" and data["mismatches"][0]["expected"] == "100.000"
        assert data["lines"][0]["total"] == "3540.00" and data["taxType"] == "IGST"
        assert Mir.objects.count() == 0


@pytest.mark.django_db
class TestFindingPOs:
    def test_open_pos_span_every_plant_for_an_editor(self):
        _po(plant="vapi", number="1000009001")
        _po(plant="achhad", number="1100009001")
        client = _client(plants=["hrs"])
        found = client.get("/api/mir/open-pos?q=Prime").json()["purchaseOrders"]
        assert {p["plant"]["code"] for p in found} == {"vapi", "achhad"}
        assert found[0]["gstRate"] == "18" and found[0]["openLines"] == 1

    def test_a_received_po_is_no_longer_open(self):
        po = _po()
        client = _client()
        client.post("/api/mir/entries/new", _body(po), format="json")
        assert client.get("/api/mir/open-pos?q=1000009001").json()["purchaseOrders"] == []

    def test_a_viewer_cannot_search_pos(self):
        assert _client(role="viewer").get("/api/mir/open-pos?q=Prime").status_code == 403

    def test_po_detail_carries_each_lines_open_quantity(self):
        po = _po()
        line = _client().get(f"/api/mir/purchase-orders/{po.id}").json()["lines"][0]
        assert (line["openQty"], line["accepted"], line["status"], line["receivable"]) == ("100.000", "0", "open", True)


@pytest.mark.django_db
class TestRegisterScoping:
    def _post(self, plant):
        po = _po(number=f"10000090{plant[:2]}")
        client = _client(email=f"{plant}@ravasco.com")
        return client.post("/api/mir/entries/new", _body(po, plant=plant, invoice_no=f"INV-{plant}"), format="json").json()

    def test_the_register_shows_only_plants_the_caller_may_read(self):
        hrs, vapi = self._post("hrs"), self._post("vapi")
        scoped = _client(role="viewer", plants=["hrs"], email="v@ravasco.com")
        listed = {e["mirNo"] for e in scoped.get("/api/mir/entries").json()["entries"]}
        assert listed == {hrs["mirNo"]}
        assert scoped.get(f"/api/mir/entries/{vapi['id']}").status_code == 404
        assert scoped.get(f"/api/mir/entries/{hrs['id']}").status_code == 200

    def test_the_register_is_newest_first(self):
        """Django drops Meta.ordering on the register's aggregate query; the
        view must order explicitly (found in the browser, 2026-09-28)."""
        first, second = self._post("hrs"), self._post("vapi")
        listed = [e["mirNo"] for e in _client(email="all@ravasco.com").get("/api/mir/entries").json()["entries"]]
        assert listed == [second["mirNo"], first["mirNo"]]

    def test_cancel_is_editor_only_and_plant_scoped(self):
        hrs = self._post("hrs")
        assert _client(role="viewer", email="v1@ravasco.com").post(
            f"/api/mir/entries/{hrs['id']}/cancel", {"reason": "x"}, format="json").status_code == 403
        assert _client(plants=["vapi"], email="v2@ravasco.com").post(
            f"/api/mir/entries/{hrs['id']}/cancel", {"reason": "x"}, format="json").status_code == 403
        res = _client(plants=["hrs"], email="v3@ravasco.com").post(
            f"/api/mir/entries/{hrs['id']}/cancel", {"reason": "Duplicate"}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "CANCELLED"


@pytest.mark.django_db
class TestMismatchesAndLines:
    def test_open_mismatches_are_listed_and_resolved(self):
        client, po = _client(), _po()
        client.post("/api/mir/entries/new", _body(po, qty="60", lines=[{
            "po_line_id": po.lines.get().id, "qty_received": "60", "rate": "50", "gst_rate": "18",
            "qty_reason": "PARTIAL_BALANCE_DUE"}], invoice_total="3540.00"), format="json")
        listed = client.get("/api/mir/mismatches").json()["mismatches"]
        assert len(listed) == 1 and listed[0]["poNumber"] == "1000009001"
        mm_id = listed[0]["id"]
        assert client.post(f"/api/mir/mismatches/{mm_id}/resolve", {"note": ""}, format="json").status_code == 400
        assert client.post(f"/api/mir/mismatches/{mm_id}/resolve", {"note": "Balance came"}, format="json").status_code == 200
        assert client.get("/api/mir/mismatches").json()["mismatches"] == []

    def test_closing_a_line_is_scoped_to_the_pos_plant(self):
        po = _po(plant="vapi")
        line_id = po.lines.get().id
        assert _client(plants=["hrs"], email="h@ravasco.com").post(
            f"/api/mir/po-lines/{line_id}/close", {"reason": "VENDOR_SHORT_CLOSE"}, format="json").status_code == 403
        res = _client(plants=["vapi"], email="v@ravasco.com").post(
            f"/api/mir/po-lines/{line_id}/close", {"reason": "VENDOR_SHORT_CLOSE"}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "closed"


@pytest.mark.django_db
def test_meta_says_where_the_caller_may_receive():
    plants = {p["code"]: p for p in _client(plants=["hrs"]).get("/api/mir/meta").json()["plants"]}
    assert plants["hrs"]["canReceive"] and not plants["vapi"]["canReceive"]
    viewer = {p["code"]: p for p in _client(role="viewer", email="v@ravasco.com").get("/api/mir/meta").json()["plants"]}
    assert not any(p["canReceive"] for p in viewer.values()) and all(p["canRead"] for p in viewer.values())
