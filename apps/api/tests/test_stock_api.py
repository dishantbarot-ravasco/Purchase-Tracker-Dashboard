"""
/api/stock/... (apps/api/routers/stock_views.py) - who may do what, and the
wire shape the RM store page reads. The rules themselves are covered in
apps/services/tests/test_stock_service.py.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Plant, PurchaseOrder, PurchaseOrderLine, StockLot, StockVoucher, Vendor
from apps.services import materials, mir_service

TODAY = timezone.localdate()


def _client(role="editor", plants=None, email="e@ravasco.com"):
    client = APIClient()
    user = make_user(email=email, role=role, plants=plants or [])
    client.force_authenticate(user=user)
    return client


def _stock(plant="hrs", qty="100"):
    """A posted MIR of SBR 1502 at `plant`: `qty` KG at Rs 50 into its store.
    Returns the MIR's receipt (StockLot)."""
    vendor = Vendor.objects.filter(gstin="27AAACP5506B1ZW").first() or Vendor.objects.create(
        gstin="27AAACP5506B1ZW", name="Prime Chemicals", name_key="primechemicals")
    n = PurchaseOrder.objects.count() + 1
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code=plant), po_number=f"90000{n:05d}", vendor=vendor,
                                      po_date=TODAY - datetime.timedelta(days=10), tax_type="IGST")
    line = PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description="SBR 1502", uom="KG", item_code="22001132",
                                            qty_ordered=Decimal(qty), rate=Decimal("50"), material=materials.material_for("SBR 1502"))
    body = {"plant": plant, "mir_date": TODAY.isoformat(), "invoice_no": f"INV-{n}", "invoice_date": TODAY.isoformat(), "tcs_amount": "0",
            "lines": [{"po_line_id": line.id, "qty_received": qty, "rate": "50", "gst_rate": "18", "material_category": "Synthetic Rubber"}]}
    body["invoice_total"] = str(mir_service.evaluate(body)["computed_total"])
    mir = mir_service.post_mir(body, make_user(email=f"clerk{n}@ravasco.com", role="editor"))
    return StockLot.objects.get(mir_line__mir=mir)


def _issue_body(lot, plant="hrs", qty="10"):
    return {"kind": "ISSUE", "plant": plant, "voucher_date": TODAY.isoformat(), "lines": [{"lot_id": lot.id, "qty": qty}]}


def _write_off(lot, qty="5"):
    return {"kind": "ADJUST", "plant": "hrs", "voucher_date": TODAY.isoformat(),
            "lines": [{"mode": "remove", "lot_id": lot.id, "qty": qty, "reason": "SAMPLE_TESTING"}]}


@pytest.mark.django_db
class TestWhoMayWrite:
    def test_without_rm_store_the_store_is_closed_and_with_it_open(self):
        """The RM store page is one permission (layered access, 2026-10-02):
        a dashboard viewer can neither read the register nor issue; an
        account granted RM store alone does both at its plant."""
        lot = _stock()
        client = _client(role="viewer")
        assert client.get("/api/stock/register").status_code == 403
        assert client.post("/api/stock/preview", _issue_body(lot), format="json").status_code == 403
        assert client.post("/api/stock/vouchers/new", _issue_body(lot), format="json").status_code == 403
        store = APIClient()
        store.force_authenticate(user=make_user(email="store@ravasco.com", role="user",
                                                permissions=["rm_store"], plants=["hrs"]))
        assert store.get("/api/stock/register").json()["rows"][0]["closing"] == "100.000"
        assert store.post("/api/stock/preview", _issue_body(lot), format="json").status_code == 200

    def test_an_editor_scoped_to_hrs_issues_at_hrs_only(self):
        hrs = _stock("hrs")
        vapi = _stock("vapi")
        client = _client(plants=["hrs"])
        assert client.post("/api/stock/vouchers/new", _issue_body(vapi, "vapi"), format="json").status_code == 403
        res = client.post("/api/stock/vouchers/new", _issue_body(hrs), format="json")
        assert res.status_code == 201, res.json()
        data = res.json()
        assert data["voucherNo"].startswith("HRS/ISS/") and data["status"] == "POSTED"
        # Exact strings, and the MIR it came out of.
        line = data["lines"][0]
        assert line["qty"] == "10.000" and line["value"] == "-500.00"
        assert line["receipt"]["mirNo"] == hrs.mir_line.mir.mir_no and line["receipt"]["itemCode"] == "22001132"

    def test_only_an_admin_approves_and_the_editor_sees_it_waiting(self):
        lot = _stock()
        editor = _client(plants=["hrs"])
        res = editor.post("/api/stock/vouchers/new", _write_off(lot), format="json")
        assert res.status_code == 201 and res.json()["status"] == "PENDING"
        vid = res.json()["id"]
        assert editor.get("/api/stock/meta").json()["pendingApprovals"] == 1
        assert editor.post(f"/api/stock/vouchers/{vid}/approve", {"note": "ok"}, format="json").status_code == 403
        admin = _client(role="admin", email="a@ravasco.com")
        res = admin.post(f"/api/stock/vouchers/{vid}/approve", {"note": "ok"}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "POSTED"
        assert admin.get(f"/api/stock/receipts/{lot.id}").json()["balance"] == "95.000"

    def test_a_refusal_comes_back_as_field_errors(self):
        lot = _stock(qty="5")
        res = _client().post("/api/stock/vouchers/new", _issue_body(lot, qty="6"), format="json")
        assert res.status_code == 400
        assert res.json()["errors"][0]["field"] == "lines.0.qty"


@pytest.mark.django_db
class TestPlantScope:
    def test_a_scoped_reader_sees_only_its_plants_receipts_register_and_vouchers(self):
        hrs = _stock("hrs")
        vapi = _stock("vapi")
        admin = _client(role="admin", email="a@ravasco.com")
        vapi_issue = admin.post("/api/stock/vouchers/new", _issue_body(vapi, "vapi"), format="json").json()
        viewer = _client(role="editor", plants=["hrs"], email="v@ravasco.com")
        assert {r["plant"]["code"] for r in viewer.get("/api/stock/register").json()["rows"]} == {"hrs"}
        assert viewer.get("/api/stock/register?plant=vapi").json()["rows"] == []
        assert viewer.get("/api/stock/vouchers").json()["vouchers"] == []
        assert viewer.get(f"/api/stock/vouchers/{vapi_issue['id']}").status_code == 404
        assert viewer.get(f"/api/stock/receipts/{vapi.id}").status_code == 404
        assert viewer.get("/api/stock/receipts?plant=vapi").status_code == 404
        assert viewer.get(f"/api/stock/receipts/{hrs.id}").json()["balance"] == "100.000"
        assert viewer.get("/api/stock/differences?plant=vapi&status=ALL").json()["differences"] == []


@pytest.mark.django_db
class TestPickingAMir:
    def test_the_picker_brings_everything_from_the_mir(self):
        lot = _stock()
        mir = lot.mir_line.mir
        found = _client().get(f"/api/stock/receipts?plant=hrs&q={mir.mir_no}").json()["receipts"]
        assert len(found) == 1
        r = found[0]
        assert (r["mirNo"], r["lineNo"], r["material"]["name"], r["uom"], r["vendor"], r["rate"], r["balance"]) == (
            mir.mir_no, 1, "SBR 1502", "KG", "Prime Chemicals", "50.0000", "100.000")
        assert (r["invoiceNo"], r["poNumber"], r["itemCode"], r["receivedDate"], r["days"]) == (
            mir.invoice_no, lot.mir_line.po_line.purchase_order.po_number, "22001132", TODAY.isoformat(), 0)

    def test_show_all_lists_every_open_mir_oldest_first(self):
        first, second = _stock(), _stock()
        found = _client().get("/api/stock/receipts?plant=hrs&all=1").json()["receipts"]
        assert [r["id"] for r in found] == [first.id, second.id]

    def test_a_receipt_with_nothing_left_is_not_offered(self):
        lot = _stock(qty="10")
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(lot, qty="10"), format="json")
        assert client.get("/api/stock/receipts?plant=hrs").json()["receipts"] == []


@pytest.mark.django_db
class TestTheMirConnection:
    def test_the_mir_detail_shows_what_each_line_put_into_stock(self):
        lot = _stock(qty="100")
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(lot, qty="30"), format="json")
        line = client.get(f"/api/mir/entries/{lot.mir_line.mir_id}").json()["lines"][0]
        assert line["stock"] == {"uom": "KG", "in": "100.000", "balance": "70.000", "stocked": True}

    def test_cancelling_a_mir_whose_stock_was_issued_is_a_400_naming_the_issue(self):
        lot = _stock()
        client = _client()
        issue = client.post("/api/stock/vouchers/new", _issue_body(lot), format="json").json()
        res = client.post(f"/api/mir/entries/{lot.mir_line.mir_id}/cancel", {"reason": "twice"}, format="json")
        assert res.status_code == 400 and issue["voucherNo"] in res.json()["error"]

    def test_the_issue_offers_its_lines_for_return(self):
        lot = _stock()
        client = _client()
        issue = client.post("/api/stock/vouchers/new", _issue_body(lot, qty="40"), format="json").json()
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
        category = _stock().material.category
        data = _client(plants=["hrs"]).get("/api/stock/meta").json()
        flags = {p["code"]: (p["canRead"], p["canWrite"], p["canApprove"]) for p in data["plants"]}
        assert flags == {"hrs": (True, True, False), "achhad": (False, False, False), "vapi": (False, False, False)}
        assert data["backdateDays"] == 7 and {r["kind"] for r in data["reasons"]} == {"RETURN", "ADJUST_IN", "ADJUST_OUT"}
        # Stock comes in only through a MIR: no opening-balance reason on offer.
        assert "OPENING_BALANCE" not in {r["code"] for r in data["reasons"]}
        # The register's category filter: what this plant holds.
        assert category and data["categories"] == [category]

    def test_preview_values_the_draw_and_saves_nothing(self):
        lot = _stock()
        data = _client().post("/api/stock/preview", _issue_body(lot, qty="20"), format="json").json()
        assert data["ok"] and data["lines"][0]["value"] == "1000.00" and data["lines"][0]["draws"][0]["qty"] == "20"
        assert not StockVoucher.objects.exists()

    def test_the_register_row_and_the_receipts_story(self):
        lot = _stock()
        client = _client()
        client.post("/api/stock/vouchers/new", _issue_body(lot, qty="30"), format="json")
        row = client.get("/api/stock/register?plant=hrs").json()["rows"][0]
        assert (row["opening"], row["received"], row["issued"], row["closing"], row["value"], row["itemCode"]) == (
            "0", "100.000", "30.000", "70.000", "3500.00", "22001132")
        data = client.get(f"/api/stock/receipts/{lot.id}").json()
        assert data["canWrite"] and data["setting"]["isStocked"] is True
        assert [(m["kind"], m["balance"]) for m in data["movements"]] == [("RECEIPT", "100.000"), ("ISSUE", "70.000")]

    def test_units_are_set_through_the_api_and_shown_on_the_receipt(self):
        lot = _stock()
        material = lot.material
        editor = _client()
        body = {"baseUom": "KG", "factors": {"BAG": "25"}, "reason": "25 kg bags"}
        assert _client(role="viewer", email="viewer@ravasco.com").post(
            f"/api/stock/materials/{material.id}/units", body, format="json").status_code == 403
        assert editor.post(f"/api/stock/materials/{material.id}/units", dict(body, reason=""), format="json").status_code == 400
        res = editor.post(f"/api/stock/materials/{material.id}/units", body, format="json")
        assert res.status_code == 200 and res.json()["factors"] == [{"uom": "BAG", "factor": "25", "by": "e@ravasco.com"}]
        data = editor.get(f"/api/stock/receipts/{lot.id}").json()
        assert data["units"]["baseUom"] == "KG" and data["units"]["history"][0]["reason"] == "25 kg bags"
        assert (data["mirUom"], data["factor"]) == ("KG", "1")
        meta = editor.get("/api/stock/meta").json()
        assert [b["code"] for b in meta["baseUnits"]] == ["KG", "L", "NOS", "M"]
        # Plain figures, never "1E+3".
        assert meta["exactUnits"]["MT"] == ["KG", "1000"] and meta["exactUnits"]["G"] == ["KG", "0.001"]
        assert "ROLL" in meta["packUnits"] and "MT" not in meta["packUnits"]

    def test_a_bad_register_period_is_a_400(self):
        res = _client().get(f"/api/stock/register?from={TODAY}&to={TODAY - datetime.timedelta(days=1)}")
        assert res.status_code == 400

    def test_settings_only_at_a_plant_the_editor_may_write(self):
        material = materials.material_for("SBR 1502")
        body = {"plant": "vapi", "materialId": material.id, "isStocked": False, "minLevel": "50", "minLevelUom": "KG"}
        assert _client(plants=["hrs"]).post("/api/stock/settings", body, format="json").status_code == 403
        client = _client(email="all@ravasco.com")
        assert client.post("/api/stock/settings", dict(body, minLevel="-1"), format="json").status_code == 400
        assert client.post("/api/stock/settings", body, format="json").status_code == 200
        lot = _stock("vapi")
        # Not kept in store: the receipt holds nothing, and is not offered.
        assert client.get(f"/api/stock/receipts/{lot.id}").json()["balance"] == "0"
        assert client.get("/api/stock/receipts?plant=vapi").json()["receipts"] == []

    def test_differences_list_and_resolve_through_the_api(self):
        lot = _stock()
        editor = _client(plants=["hrs"])
        vid = editor.post("/api/stock/vouchers/new", _write_off(lot), format="json").json()["id"]
        open_list = editor.get("/api/stock/differences").json()["differences"]
        assert [(d["voucherId"], d["kind"], d["qty"], d["value"], d["receipt"]["mirNo"]) for d in open_list] == [
            (vid, "WRITE_OFF", "5.000", "-250.00", lot.mir_line.mir.mir_no)]
        admin = _client(role="admin", email="a@ravasco.com")
        assert admin.post(f"/api/stock/vouchers/{vid}/reject", {}, format="json").status_code == 400
        res = admin.post(f"/api/stock/vouchers/{vid}/reject", {"note": "no sample was sent"}, format="json")
        assert res.json()["status"] == "REJECTED" and res.json()["decisionNote"] == "no sample was sent"
        assert editor.get("/api/stock/differences").json()["differences"] == []
        assert editor.get("/api/stock/differences?status=RESOLVED").json()["differences"][0]["status"] == "REJECTED"

    def test_cancelling_through_the_api(self):
        lot = _stock()
        editor = _client(plants=["hrs"])
        issue = editor.post("/api/stock/vouchers/new", _issue_body(lot), format="json").json()
        assert editor.post(f"/api/stock/vouchers/{issue['id']}/cancel", {}, format="json").status_code == 400
        other = _client(plants=["vapi"], email="v@ravasco.com")
        assert other.post(f"/api/stock/vouchers/{issue['id']}/cancel", {"reason": "x"}, format="json").status_code == 403
        res = editor.post(f"/api/stock/vouchers/{issue['id']}/cancel", {"reason": "wrong department"}, format="json")
        assert res.json()["status"] == "CANCELLED" and res.json()["canCancel"] is False

    def test_the_slip_register_filters(self):
        lot = _stock()
        client = _client()
        client.post("/api/stock/vouchers/new", dict(_issue_body(lot), department="Mixing"), format="json")
        assert len(client.get("/api/stock/vouchers?kind=ISSUE&status=POSTED&q=mixing").json()["vouchers"]) == 1
        assert len(client.get(f"/api/stock/vouchers?q={lot.mir_line.mir.mir_no}").json()["vouchers"]) == 1
        assert client.get("/api/stock/vouchers?kind=RETURN").json()["vouchers"] == []
        assert client.get(f"/api/stock/vouchers?from={TODAY + datetime.timedelta(days=1)}").json()["vouchers"] == []
