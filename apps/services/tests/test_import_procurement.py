"""
Import orders in the app's own PO tables (owner, 2026-10-03), phase 1:
the import CSV mirror projected into PurchaseOrder (kind IMPORT) with one
line per ordered item, and an import PO read from its PDF. Import receipts
wait for the shipment's Bill of Entry (phase 2), so MIR entry does not offer
them yet.
"""

import copy
import datetime
from decimal import Decimal

import pytest

from apps.api.tests.factories import make_user
from apps.api.tests.test_po_extraction import READ, _document, _fake_read, _read, configured  # noqa: F401
from apps.core.models import Plant, PurchaseOrder, PurchaseOrderLine, RTPVapiImportPOLineItem, RTPVapiImportPurchaseOrder
from apps.services import app_stock_source, mir_service, po_extraction
from apps.services.procurement_sync import project_plant_import_orders

VAPI_BILLING = "Ravasco Transmission And Packing Pvt Ltd, 164,165/P&166/P, 2nd Phase, GIDC Ind. Estate, 396195 Vapi"


def _import_po(number="1000001519", shipments=(("100800", "2477361"), ("100800", "2956922"), ("100800", "3448562"))):
    po = RTPVapiImportPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{number}", po_number=number, po_created_date=datetime.date(2026, 5, 1),
        vendor_name="Kumho Petrochemical", vendor_address="Seoul, Korea", currency="USD",
        billing_address=VAPI_BILLING, ship_to=VAPI_BILLING, total_value=Decimal("453600.00"), synced_from_row_hash="h1")
    for qty_boe, boe in shipments:
        RTPVapiImportPOLineItem.objects.create(
            purchase_order=po, item_id="22001132", description="Synthetic Rubber SBR-1502", hsn="40021990",
            qty_as_per_po=Decimal("302400"), qty_as_per_boe=Decimal(qty_boe), uom="KG", net_price=Decimal("1.5000"),
            net_value=Decimal("453600.00"), boe_number=boe, exchange_rate=Decimal("97.2"))
    RTPVapiImportPOLineItem.objects.create(
        purchase_order=po, item_id="22001500", description="Zinc Oxide", qty_as_per_po=Decimal("1000"), uom="KG",
        net_price=Decimal("2.0000"), net_value=Decimal("2000.00"))
    return po


@pytest.mark.django_db
class TestImportProjection:
    def test_one_po_line_per_ordered_item_not_per_shipment(self):
        _import_po()
        r = project_plant_import_orders("vapi")
        po = PurchaseOrder.objects.get(plant__code="vapi", po_number="1000001519")
        assert (po.kind, po.currency, po.billing_plant.code, po.tax_type, po.total_inclusive_value) == ("import", "USD", "vapi", "", None)
        lines = list(po.lines.order_by("line_no"))
        assert [(ln.line_no, ln.item_code, str(ln.qty_ordered), str(ln.rate)) for ln in lines] == [
            (1, "22001132", "302400.000", "1.5000"), (2, "22001500", "1000.000", "2.0000")]
        assert r.lines_created == 2
        # A second run writes nothing.
        assert project_plant_import_orders("vapi").orders_written == 0

    def test_a_domestic_po_of_the_same_number_stays_its_own_order(self):
        vapi = Plant.objects.get(code="vapi")
        PurchaseOrder.objects.create(plant=vapi, po_number="1000001519")
        _import_po()
        project_plant_import_orders("vapi")
        assert sorted(PurchaseOrder.objects.filter(po_number="1000001519").values_list("kind", flat=True)) == ["domestic", "import"]

    def test_an_order_the_app_owns_is_left_alone(self):
        vapi = Plant.objects.get(code="vapi")
        PurchaseOrder.objects.create(plant=vapi, kind="import", po_number="1000001519", source="app")
        _import_po()
        r = project_plant_import_orders("vapi")
        assert r.orders_held == ["1000001519"] and not PurchaseOrderLine.objects.exists()

    def test_mir_entry_and_the_app_stock_tabs_leave_imports_out_for_now(self):
        _import_po()
        project_plant_import_orders("vapi")
        assert mir_service.search_open_pos("1000001519", ["vapi"]) == []
        line = PurchaseOrderLine.objects.get(line_no=1)
        state = mir_service.line_state(line, Decimal("0"))
        assert state["receivable"] is False and "Bill of Entry" in state["blocked_reason"]
        assert app_stock_source.purchase_orders_payload(Plant.objects.get(code="vapi")) == []


IMPORT_READ = {
    **READ, "order_type": "import", "po_number": "1000009001", "vendor_name": "Kumho Petrochemical",
    "vendor_address": "Seoul, Korea", "vendor_gstin": "", "vendor_sap_code": "400000123", "currency": "USD",
    "incoterms": "CIF Nhava Sheva", "billing_address": VAPI_BILLING, "shipping_address": VAPI_BILLING,
    "tax_type": "", "tax_amount": "", "total_value": "4536.00", "total_inclusive_value": "",
    "lines": [{"item_code": "22001132", "description": "Synthetic Rubber SBR-1502", "hsn": "", "qty": "3024", "uom": "KG",
               "rate": "1.5", "net_value": "4536.00", "delivery_date": "2026-11-30", "remarks": ""}],
}


@pytest.mark.django_db
class TestImportPoReading:
    def test_an_import_po_needs_no_gstin_tax_or_hsn_and_is_kept_in_its_currency(self, configured, monkeypatch,  # noqa: F811
                                                                                django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, IMPORT_READ)
        ext = _read(django_capture_on_commit_callbacks, doc=_document(plant="vapi", po_number="1000009001"))
        assert po_extraction.problems(ext.draft, ext.plant, "1000009001") == []
        po = po_extraction.approve(ext, None, make_user(email="boss@ravasco.com", role="editor")).purchase_order
        assert (po.kind, po.currency, po.vendor.gstin, po.tax_type, po.source) == ("import", "USD", "", "", "app")
        assert str(po.lines.get().rate) == "1.5000"

    def test_the_same_draft_as_domestic_needs_the_gst_fields(self, configured, monkeypatch,  # noqa: F811
                                                             django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, {**copy.deepcopy(IMPORT_READ), "order_type": "domestic"})
        ext = _read(django_capture_on_commit_callbacks, doc=_document(plant="vapi", po_number="1000009001"))
        fields = {p["field"] for p in po_extraction.problems(ext.draft, ext.plant, "1000009001")}
        assert {"vendor_gstin", "tax_type", "total_inclusive_value", "hsn"} <= fields

    def test_an_unknown_order_type_reads_as_domestic(self):
        assert po_extraction.normalize({"order_type": "Overseas"})["order_type"] == "domestic"
