"""
apps/services/app_stock_source.py and its endpoints (stock_source_views.py):
which records a plant's Inventory / On Order / Stock & Orders tabs read
(owner, 2026-10-03), and the app-record payloads in the Drive rows' shape.
"""

import datetime
from decimal import Decimal

import pytest

from apps.api.tests.factories import make_user
from apps.api.tests.test_stock_api import TODAY, _client, _issue_body, _stock
from apps.core.models import Plant, PurchaseOrder, PurchaseOrderLine, StockSetting, Vendor
from apps.services import materials, mir_service, stock_service


@pytest.mark.django_db
class TestTheSwitch:
    def test_every_plant_reads_drive_until_an_admin_switches_it(self):
        viewer = _client(role="viewer", plants=["hrs"])
        assert viewer.get("/api/stock-source").json() == {"sources": {"hrs": "drive"}, "importSources": {"hrs": "drive"}, "canChange": False}
        admin = _client(role="admin", email="a@ravasco.com")
        res = admin.post("/api/stock-source/set", {"plant": "hrs", "source": "app"}, format="json")
        assert res.status_code == 200 and res.json()["source"] == "app"
        assert viewer.get("/api/stock-source").json()["sources"] == {"hrs": "app"}
        assert admin.get("/api/stock-source").json()["sources"] == {"hrs": "app", "achhad": "drive", "vapi": "drive"}
        # Back at once - nothing was copied or deleted.
        admin.post("/api/stock-source/set", {"plant": "hrs", "source": "drive"}, format="json")
        assert viewer.get("/api/stock-source").json()["sources"] == {"hrs": "drive"}

    def test_only_an_admin_switches_and_only_to_a_known_source(self):
        editor = _client(plants=["hrs"])
        assert editor.post("/api/stock-source/set", {"plant": "hrs", "source": "app"}, format="json").status_code == 403
        admin = _client(role="admin", email="a@ravasco.com")
        assert admin.post("/api/stock-source/set", {"plant": "hrs", "source": "sheet"}, format="json").status_code == 400


@pytest.mark.django_db
class TestAppMaterials:
    def test_each_receipt_with_what_it_holds_and_the_issue_rate(self):
        lot = _stock(qty="100")
        user = make_user(email="store@ravasco.com", role="editor", plants=["hrs"])
        stock_service.post_voucher(_issue_body(lot, qty="30"), user)
        stock_service.set_location(lot, "RTP-1", user)
        rows = _client(plants=["hrs"]).get("/api/app-stock/hrs/materials").json()["materials"]
        assert len(rows) == 1
        row = rows[0]
        assert (row["lotId"], row["description"], row["qty"], row["uom"], row["rate"]) == (lot.id, "SBR 1502", 70.0, "KG", 50.0)
        assert (row["value"], row["locationTag"], row["materialCode"]) == (3500.0, "RTP-1", "22001132")
        # One day of history: 30 KG issued over the one day observed.
        assert row["consumption"]["avgDaily"] == 30.0 and row["consumption"]["confidence"] == "low"

    def test_a_minimum_level_reached_reads_as_reorder_now(self):
        lot = _stock(qty="100")
        StockSetting.objects.create(plant=lot.plant, material=lot.material, min_level=Decimal("100"), min_level_uom="KG")
        row = _client(plants=["hrs"]).get("/api/app-stock/hrs/materials").json()["materials"][0]
        assert row["daysToMsl"] == 0.0

    def test_another_plants_stock_is_refused(self):
        _stock()
        assert _client(plants=["vapi"]).get("/api/app-stock/hrs/materials").status_code == 403
        assert _client(role="viewer", plants=["hrs"], email="v@ravasco.com").get("/api/app-stock/hrs/materials").status_code == 200


def _po_with_lines(*qtys):
    vendor = Vendor.objects.create(gstin="27AAACP5506B1ZW", name="Prime Chemicals", name_key="primechemicals")
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code="hrs"), po_number="3000009001", vendor=vendor,
                                      po_date=TODAY - datetime.timedelta(days=10), tax_type="IGST")
    for n, qty in enumerate(qtys, start=1):
        PurchaseOrderLine.objects.create(purchase_order=po, line_no=n, description=f"Grade {n}", uom="KG", qty_ordered=Decimal(qty),
                                         rate=Decimal("50"), material=materials.material_for(f"Grade {n}"),
                                         delivery_date=TODAY + datetime.timedelta(days=5))
    return po


def _receive(line, qty, reason=None):
    ln = {"po_line_id": line.id, "qty_received": qty, "rate": "50", "gst_rate": "18", "material_category": "Fillers"}
    if reason:
        ln["qty_reason"] = reason
    body = {"plant": "hrs", "mir_date": TODAY.isoformat(), "invoice_no": f"INV-{line.id}", "invoice_date": TODAY.isoformat(),
            "tcs_amount": "0", "lines": [ln]}
    body["invoice_total"] = str(mir_service.evaluate(body)["computed_total"])
    return mir_service.post_mir(body, make_user(email=f"clerk{line.id}@ravasco.com", role="editor"))


@pytest.mark.django_db
class TestAppPurchaseOrders:
    def test_received_is_what_posted_mirs_accepted_exactly(self):
        po = _po_with_lines("100", "40", "10")
        lines = list(po.lines.order_by("line_no"))
        _receive(lines[0], "60", "PARTIAL_BALANCE_DUE")
        _receive(lines[1], "40")
        mir = _receive(lines[2], "10")
        mir_service.cancel_mir(mir, make_user(email="x@ravasco.com", role="editor"), "Wrong invoice")
        data = _client(plants=["hrs"]).get("/api/app-stock/hrs/purchase-orders").json()["purchaseOrders"]
        items = {it["description"]: it for it in data[0]["items"]}
        assert items["Grade 1"]["received"] == {"qty": 60.0, "comparable": True}
        assert (items["Grade 1"]["matched"], items["Grade 1"]["qtyDiffPct"], items["Grade 1"]["qtyOverDelivered"]) == (True, 40.0, False)
        assert (items["Grade 2"]["qtyDiffPct"], items["Grade 2"]["qtyOverDelivered"]) == (0.0, True)
        # A cancelled MIR stops counting at once.
        assert (items["Grade 3"]["matched"], items["Grade 3"]["received"]["qty"]) == (False, 0.0)

    def test_a_short_closed_line_is_not_on_order(self):
        po = _po_with_lines("100", "40")
        lines = list(po.lines.order_by("line_no"))
        mir_service.close_po_line(lines[1], make_user(email="pm@ravasco.com", role="editor"), "VENDOR_SHORT_CLOSE", "")
        data = _client(plants=["hrs"]).get("/api/app-stock/hrs/purchase-orders").json()["purchaseOrders"]
        assert [it["description"] for it in data[0]["items"]] == ["Grade 1"]

    def test_another_plants_orders_are_refused(self):
        _po_with_lines("100")
        assert _client(plants=["vapi"]).get("/api/app-stock/hrs/purchase-orders").status_code == 403
