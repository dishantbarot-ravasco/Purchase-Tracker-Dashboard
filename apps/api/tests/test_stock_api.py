"""
/api/stock/... (apps/api/routers/stock_views.py) - who may do what, and the
wire shape the stock page reads. The rules themselves are covered in
apps/services/tests/test_stock_service.py.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Plant, PurchaseOrder, PurchaseOrderLine, StockVoucher, Vendor
from apps.services import materials, mir_service

TODAY = timezone.localdate()


def _client(role="editor", plants=None, email="e@ravasco.com"):
    client = APIClient()
    user = make_user(email=email, role=role, plants=plants or [])
    client.force_authenticate(user=user)
    return client


def _stock(plant="hrs", qty="100"):
    """A posted MIR of SBR 1502 at `plant`: `qty` KG at Rs 50 into its store."""
    vendor = Vendor.objects.filter(gstin="27AAACP5506B1ZW").first() or Vendor.objects.create(
        gstin="27AAACP5506B1ZW", name="Prime Chemicals", name_key="primechemicals")
    n = PurchaseOrder.objects.count() + 1
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code=plant), po_number=f"90000{n:05d}", vendor=vendor,
                                      po_date=TODAY - datetime.timedelta(days=10), tax_type="IGST")
    line = PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description="SBR 1502", uom="KG",
                                            qty_ordered=Decimal(qty), rate=Decimal("50"), material=materials.material_for("SBR 1502"))
    body = {"plant": plant, "mir_date": TODAY.isoformat(), "invoice_no": f"INV-{n}", "invoice_date": TODAY.isoformat(),
            "lines": [{"po_line_id": line.id, "qty_received": qty, "rate": "50", "gst_rate": "18", "material_category": "Synthetic Rubber"}]}
    body["invoice_total"] = str(mir_service.evaluate(body)["computed_total"])
    return mir_service.post_mir(body, make_user(email=f"clerk{n}@ravasco.com", role="editor"))


def _issue_body(plant="hrs", qty="10"):
    return {"kind": "ISSUE", "plant": plant, "voucher_date": TODAY.isoformat(), "department": "Mixing", "issued_to": "Ramesh",
            "lines": [{"material_id": materials.material_for("SBR 1502").id, "uom": "KG", "qty": qty}]}


@pytest.mark.django_db
class TestWhoMayWrite:
    def test_a_viewer_reads_stock_but_cannot_preview_or_issue(self):
        _stock()
        client = _client(role="viewer")
        assert client.get("/api/stock/balances").json()["rows"][0]["qty"] == "100.000"
        assert client.post("/api/stock/preview", _issue_body(), format="json").status_code == 403
        assert client.post("/api/stock/vouchers/new", _issue_body(), format="json").status_code == 403

    def test_an_editor_scoped_to_hrs_issues_at_hrs_only(self):
        _stock("hrs")
        _stock("vapi")
        client = _client(plants=["hrs"])
        assert client.post("/api/stock/vouchers/new", _issue_body("vapi"), format="json").status_code == 403
        res = client.post("/api/stock/vouchers/new", _issue_body("hrs"), format="json")
        assert res.status_code == 201, res.json()
        data = res.json()
        assert data["voucherNo"].startswith("HRS/ISS/") and data["status"] == "POSTED"
        # Exact strings, and which lot it came from.
        assert data["lines"][0]["qty"] == "10.000" and data["lines"][0]["value"] == "-500.00"
        assert data["lines"][0]["draws"][0]["doc"].startswith("HRS/")

    def test_only_an_admin_approves_and_the_editor_sees_it_waiting(self):
        _stock()
        editor = _client(plants=["hrs"])
        body = {"kind": "ADJUST", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                "lines": [{"mode": "remove", "material_id": materials.material_for("SBR 1502").id, "uom": "KG", "qty": "5",
                           "reason": "SAMPLE_TESTING"}]}
        res = editor.post("/api/stock/vouchers/new", body, format="json")
        assert res.status_code == 201 and res.json()["status"] == "PENDING"
        vid = res.json()["id"]
        assert editor.get("/api/stock/meta").json()["pendingApprovals"] == 1
        assert editor.post(f"/api/stock/vouchers/{vid}/approve", {"note": "ok"}, format="json").status_code == 403
        admin = _client(role="admin", email="a@ravasco.com")
        res = admin.post(f"/api/stock/vouchers/{vid}/approve", {"note": "ok"}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "POSTED"
        assert admin.get("/api/stock/balances?plant=hrs").json()["rows"][0]["qty"] == "95.000"

    def test_a_refusal_comes_back_as_field_errors(self):
        _stock(qty="5")
        res = _client().post("/api/stock/vouchers/new", _issue_body(qty="6"), format="json")
        assert res.status_code == 400
        assert res.json()["errors"][0]["field"] == "lines.0.qty"


@pytest.mark.django_db
class TestPlantScope:
    def test_a_scoped_reader_sees_only_its_plants_stock_and_vouchers(self):
        _stock("hrs")
        _stock("vapi")
        admin = _client(role="admin", email="a@ravasco.com")
        vapi_issue = admin.post("/api/stock/vouchers/new", _issue_body("vapi"), format="json").json()
        viewer = _client(role="viewer", plants=["hrs"], email="v@ravasco.com")
        assert {r["plant"]["code"] for r in viewer.get("/api/stock/balances").json()["rows"]} == {"hrs"}
        assert viewer.get("/api/stock/vouchers").json()["vouchers"] == []
        assert viewer.get(f"/api/stock/vouchers/{vapi_issue['id']}").status_code == 404
        material = materials.material_for("SBR 1502")
        assert viewer.get(f"/api/stock/materials/{material.id}?plant=vapi&uom=KG").status_code == 404
        assert viewer.get(f"/api/stock/materials/{material.id}?plant=hrs&uom=KG").json()["lots"][0]["balance"] == "100.000"


@pytest.mark.django_db
class TestTheMirConnection:
    def test_the_mir_detail_shows_what_each_line_put_into_stock(self):
        mir = _stock(qty="100")
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(qty="30"), format="json")
        line = client.get(f"/api/mir/entries/{mir.id}").json()["lines"][0]
        assert line["stock"] == {"uom": "KG", "in": "100.000", "balance": "70.000", "stocked": True}

    def test_cancelling_a_mir_whose_stock_was_issued_is_a_400_naming_the_issue(self):
        mir = _stock()
        client = _client()
        issue = client.post("/api/stock/vouchers/new", _issue_body(), format="json").json()
        res = client.post(f"/api/mir/entries/{mir.id}/cancel", {"reason": "twice"}, format="json")
        assert res.status_code == 400 and issue["voucherNo"] in res.json()["error"]

    def test_the_issue_offers_its_lines_for_return(self):
        _stock()
        client = _client()
        issue = client.post("/api/stock/vouchers/new", _issue_body(qty="40"), format="json").json()
        assert issue["returnable"][0]["stillOut"] == "40.000"
        body = {"kind": "RETURN", "plant": "hrs", "voucher_date": TODAY.isoformat(), "return_of": issue["id"],
                "lines": [{"issue_line_id": issue["returnable"][0]["lineId"], "qty": "15", "reason": "RETURN_UNUSED"}]}
        assert client.post("/api/stock/vouchers/new", body, format="json").status_code == 201
        detail = client.get(f"/api/stock/vouchers/{issue['id']}").json()
        assert detail["returnable"][0]["stillOut"] == "25.000" and detail["returns"][0]["voucherNo"].startswith("HRS/RET/")
        assert StockVoucher.objects.filter(kind="RETURN").count() == 1


@pytest.mark.django_db
class TestTheRestOfThePage:
    def test_meta_says_what_the_caller_may_do_at_each_plant(self):
        data = _client(plants=["hrs"]).get("/api/stock/meta").json()
        flags = {p["code"]: (p["canRead"], p["canWrite"], p["canApprove"]) for p in data["plants"]}
        assert flags == {"hrs": (True, True, False), "achhad": (False, False, False), "vapi": (False, False, False)}
        assert data["backdateDays"] == 7 and {r["kind"] for r in data["reasons"]} == {"RETURN", "ADJUST_IN", "ADJUST_OUT"}

    def test_preview_values_the_draw_and_saves_nothing(self):
        _stock()
        data = _client().post("/api/stock/preview", _issue_body(qty="20"), format="json").json()
        assert data["ok"] and data["lines"][0]["value"] == "1000.00" and data["lines"][0]["draws"][0]["qty"] == "20"
        assert not StockVoucher.objects.exists()

    def test_the_material_page_shows_lots_and_a_running_ledger(self):
        _stock()
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(qty="30"), format="json")
        material = materials.material_for("SBR 1502")
        data = client.get(f"/api/stock/materials/{material.id}?plant=hrs&uom=KG").json()
        assert data["canWrite"] and data["setting"]["isStocked"] is True
        assert [(e["kind"], e["balance"]) for e in data["ledger"]] == [("RECEIPT", "100.000"), ("ISSUE", "70.000")]

    def test_settings_only_at_a_plant_the_editor_may_write(self):
        material = materials.material_for("SBR 1502")
        body = {"plant": "vapi", "materialId": material.id, "isStocked": False, "minLevel": "50", "minLevelUom": "KG"}
        assert _client(plants=["hrs"]).post("/api/stock/settings", body, format="json").status_code == 403
        client = _client(email="all@ravasco.com")
        assert client.post("/api/stock/settings", dict(body, minLevel="-1"), format="json").status_code == 400
        assert client.post("/api/stock/settings", body, format="json").status_code == 200
        _stock("vapi")
        row = client.get("/api/stock/balances?plant=vapi").json()["rows"][0]
        # Not kept in store: the receipt holds nothing.
        assert (row["isStocked"], row["qty"], row["minLevel"]) == (False, "0", "50.000")

    def test_material_search_is_the_master_for_editors_only(self):
        materials.material_for("Carbon Black N330")
        assert _client(role="viewer", email="viewer@ravasco.com").get("/api/stock/material-search?q=carbon").status_code == 403
        found = _client().get("/api/stock/material-search?q=carbon").json()["materials"]
        assert [m["name"] for m in found] == ["Carbon Black N330"]
        assert _client(email="x@ravasco.com").get("/api/stock/material-search?q=c").json()["materials"] == []

    def test_turning_down_and_cancelling_through_the_api(self):
        _stock()
        editor = _client(plants=["hrs"])
        body = {"kind": "ADJUST", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                "lines": [{"mode": "remove", "material_id": materials.material_for("SBR 1502").id, "uom": "KG", "qty": "5",
                           "reason": "SAMPLE_TESTING"}]}
        vid = editor.post("/api/stock/vouchers/new", body, format="json").json()["id"]
        admin = _client(role="admin", email="a@ravasco.com")
        assert admin.post(f"/api/stock/vouchers/{vid}/reject", {}, format="json").status_code == 400
        res = admin.post(f"/api/stock/vouchers/{vid}/reject", {"note": "no sample was sent"}, format="json")
        assert res.json()["status"] == "REJECTED" and res.json()["decisionNote"] == "no sample was sent"
        issue = editor.post("/api/stock/vouchers/new", _issue_body(), format="json").json()
        assert editor.post(f"/api/stock/vouchers/{issue['id']}/cancel", {}, format="json").status_code == 400
        other = _client(plants=["vapi"], email="v@ravasco.com")
        assert other.post(f"/api/stock/vouchers/{issue['id']}/cancel", {"reason": "x"}, format="json").status_code == 403
        res = editor.post(f"/api/stock/vouchers/{issue['id']}/cancel", {"reason": "wrong department"}, format="json")
        assert res.json()["status"] == "CANCELLED" and res.json()["canCancel"] is False

    def test_the_register_filters(self):
        _stock()
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(), format="json")
        assert len(client.get("/api/stock/vouchers?kind=ISSUE&status=POSTED&q=mixing").json()["vouchers"]) == 1
        assert client.get("/api/stock/vouchers?kind=RETURN").json()["vouchers"] == []
        assert client.get(f"/api/stock/vouchers?from={TODAY + datetime.timedelta(days=1)}").json()["vouchers"] == []
