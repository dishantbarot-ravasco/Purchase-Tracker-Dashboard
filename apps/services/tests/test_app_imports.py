"""
Import phases 4-5 (owner, 2026-10-03): the Import Purchases page per plant
from the import CSV or from the app's own import POs, shipments and import
MIRs - the same rows and status rules either way, never both.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import ImportShipment, Plant
from apps.services import app_stock_source, mir_service
from apps.services.tests.test_import_shipments import _body, _ln, _setup


@pytest.fixture
def clerk():
    return make_user(email="store@ravasco.com", role="editor", plants=["vapi"])


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestImportPurchasesFromTheApp:
    def test_the_switch_is_per_plant_and_admin_only(self, clerk):
        assert _client(clerk).post("/api/stock-source/set", {"plant": "vapi", "source": "app", "what": "imports"},
                                   format="json").status_code == 403
        admin = _client(make_user(email="a@ravasco.com", role="admin"))
        res = admin.post("/api/stock-source/set", {"plant": "vapi", "source": "app", "what": "imports"}, format="json")
        assert res.status_code == 200 and res.json()["what"] == "imports"
        data = _client(clerk).get("/api/stock-source").json()
        assert data["importSources"] == {"vapi": "app"} and data["sources"] == {"vapi": "drive"}
        assert admin.post("/api/stock-source/set", {"plant": "vapi", "source": "app", "what": "bank"}, format="json").status_code == 400

    def test_one_row_per_shipment_line_with_what_arrived_on_its_boe(self, clerk):
        line = _setup()
        shipment = ImportShipment.objects.get(boe_number="2477361")
        mir_service.post_mir(_body(shipment, [_ln(line, "100800")]), clerk)
        drive_rows = _client(clerk).get("/api/imports/purchase-orders").json()["purchaseOrders"]
        assert all("source" not in r for r in drive_rows)
        app_stock_source.set_source(Plant.objects.get(code="vapi"), "app", clerk, "imports")
        po = _client(clerk).get("/api/imports/purchase-orders").json()["purchaseOrders"][0]
        assert po["source"] == "app" and po["poNumber"] == "1000001519"
        by_boe = {i["boeNumber"]: i for i in po["items"]}
        assert set(by_boe) == {"2477361", "2956922", "3448562", ""}  # three shipments, zinc not shipped yet
        received = by_boe["2477361"]["mirMatch"]
        assert received["received"]["qty"] == 100800.0 and received["matchedMirs"][0]["invoiceNo"] == "2477361"
        assert by_boe["2956922"]["mirMatch"] is None and by_boe["2477361"]["exchangeRate"] == 97.2
        assert po["shipmentStage"] == "Placed"  # the zinc line has not shipped
        assert po["partialDelivery"] is True and po["materialInwarded"] is False
        detail = _client(clerk).get("/api/imports/purchase-orders/vapi/1000001519").json()
        assert detail["source"] == "app" and detail["corrections"] == [] and detail["vendorName"] == "Kumho Petrochemical"

    def test_another_plants_app_rows_never_show(self, clerk):
        _setup()
        app_stock_source.set_source(Plant.objects.get(code="vapi"), "app", clerk, "imports")
        hrs = _client(make_user(email="h@ravasco.com", role="editor", plants=["hrs"]))
        assert hrs.get("/api/imports/purchase-orders").json()["purchaseOrders"] == []
        assert hrs.get("/api/imports/purchase-orders/vapi/1000001519").status_code == 404

    def test_switching_back_reads_the_sheet_again(self, clerk):
        _setup()
        vapi = Plant.objects.get(code="vapi")
        app_stock_source.set_source(vapi, "app", clerk, "imports")
        app_stock_source.set_source(vapi, "drive", clerk, "imports")
        rows = _client(clerk).get("/api/imports/purchase-orders").json()["purchaseOrders"]
        assert rows and all("source" not in r for r in rows)
        assert Decimal(str(rows[0]["items"][0]["qtyAsPerBoe"])) == Decimal("100800")
