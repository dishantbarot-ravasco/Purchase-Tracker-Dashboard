"""
Import phase 2 (owner, 2026-10-03): shipments - one per Bill of Entry - from
the import CSV's repeated rows and from an approved BOE / CHA checklist
reading, and import MIRs received one shipment at a time against them.
"""

import copy
import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.api.tests.test_po_extraction import _fake_read, _read, configured  # noqa: F401
from apps.core.models import (
    Document,
    ImportShipment,
    ImportShipmentLine,
    Plant,
    PurchaseOrderLine,
    StockLot,
    Vendor,
)
from apps.services import mir_service, po_extraction
from apps.services.mir_service import MirValidationError
from apps.services.procurement_sync import project_plant_import_orders, project_plant_import_shipments
from apps.services.tests.test_import_procurement import _import_po

TODAY = datetime.date.today()


def _setup():
    _import_po()
    project_plant_import_orders("vapi")
    project_plant_import_shipments("vapi")
    return PurchaseOrderLine.objects.get(purchase_order__kind="import", line_no=1)


def _body(shipment, lines, **extra):
    body = {"plant": "vapi", "mir_date": TODAY.isoformat(), "invoice_no": shipment.boe_number,
            "invoice_date": TODAY.isoformat(), "tcs_amount": "0", "shipment_id": shipment.id, "lines": lines}
    body.update(extra)
    return body


def _ln(line, qty, rate="145.8000", **extra):
    return {"po_line_id": line.id, "qty_received": qty, "rate": rate, "gst_rate": "18", "material_category": "Synthetic Rubber",
            **extra}


@pytest.fixture
def clerk():
    return make_user(email="store@ravasco.com", role="editor", plants=["vapi"])


@pytest.mark.django_db
class TestShipmentsFromTheCsv:
    def test_each_repeated_row_is_one_shipment_of_the_po_line(self):
        line = _setup()
        shipments = list(ImportShipment.objects.order_by("boe_number"))
        assert [s.boe_number for s in shipments] == ["2477361", "2956922", "3448562"]
        assert all((s.currency, s.exchange_rate, s.source) == ("USD", Decimal("97.2000"), "csv") for s in shipments)
        assert [str(sl.qty_as_per_boe) for sl in line.shipment_lines.order_by("shipment__boe_number")] == ["100800.000"] * 3
        assert project_plant_import_shipments("vapi").lines_written == 0

    def test_a_row_the_csv_drops_is_retired_not_deleted(self):
        _setup()
        from apps.core.models import RTPVapiImportPOLineItem
        RTPVapiImportPOLineItem.objects.filter(boe_number="3448562").delete()
        r = project_plant_import_shipments("vapi")
        assert r.lines_retired == 1
        gone = ImportShipment.objects.get(boe_number="3448562")
        assert gone.is_active is False and gone.lines.get().is_active is False


@pytest.mark.django_db
class TestImportMir:
    def test_a_shipment_is_received_at_the_po_price_times_the_boe_rate(self, clerk):
        line = _setup()
        shipment = ImportShipment.objects.get(boe_number="2477361")
        assert mir_service.search_open_pos("1000001519", ["vapi"])[0].kind == "import"
        mir = mir_service.post_mir(_body(shipment, [_ln(line, "100800")]), clerk)
        ml = mir.lines.get()
        assert (mir.shipment_id, mir.vendor_state, mir.tax_type) == (shipment.id, "96", "IGST")
        assert (ml.shipment_line.shipment_id, ml.po_rate, ml.open_qty_before) == (shipment.id, Decimal("145.8000"), Decimal("100800.000"))
        assert mir.mismatches.count() == 0 and mir.invoice_total == ml.line_total
        lot = StockLot.objects.get(mir_line=ml)
        assert (lot.currency, lot.rate) == ("INR", Decimal("145.8000"))
        # That BOE line is now received in full; another BOE of the line is not.
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_body(shipment, [_ln(line, "1")], invoice_no=shipment.boe_number), clerk)
        assert "received in full" in str(exc.value)
        other = ImportShipment.objects.get(boe_number="2956922")
        assert mir_service.post_mir(_body(other, [_ln(line, "100800")]), clerk).shipment_id == other.id

    def test_a_short_receipt_and_a_different_rate_need_reasons(self, clerk):
        line = _setup()
        shipment = ImportShipment.objects.get(boe_number="2477361")
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_body(shipment, [_ln(line, "100000", rate="150")]), clerk)
        fields = {e["field"] for e in exc.value.errors}
        assert {"lines.0.qty_reason", "lines.0.rate_reason"} <= fields

    @pytest.mark.parametrize("change, field", [
        ({"invoice_no": "INV-77"}, "invoice_no"),
        ({"shipment_id": ""}, "shipment_id"),
    ])
    def test_the_boe_is_the_invoice(self, clerk, change, field):
        line = _setup()
        shipment = ImportShipment.objects.get(boe_number="2477361")
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_body(shipment, [_ln(line, "100800")], **change), clerk)
        assert field in {e["field"] for e in exc.value.errors}

    def test_a_line_not_on_that_boe_is_refused(self, clerk):
        _setup()
        zinc = PurchaseOrderLine.objects.get(purchase_order__kind="import", line_no=2)
        later = ImportShipment.objects.create(plant=Plant.objects.get(code="vapi"), boe_number="5000001", exchange_rate=Decimal("97.2"))
        ImportShipmentLine.objects.create(shipment=later, po_line=zinc, qty_as_per_boe=Decimal("1000"))
        shipment = ImportShipment.objects.get(boe_number="2477361")
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_body(shipment, [_ln(zinc, "1000", rate="194.4000")]), clerk)
        assert "not on Bill of Entry 2477361" in str(exc.value)

    def test_a_domestic_line_and_an_import_line_never_share_a_mir(self, clerk):
        from apps.services.tests.test_mir_service import _line, _po
        line = _setup()
        dom = _line(_po(plant="vapi", number="1000009001", vendor=Vendor.objects.get()))
        shipment = ImportShipment.objects.get(boe_number="2477361")
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_body(shipment, [_ln(line, "100800"), _ln(dom, "100", rate="50")]), clerk)
        assert "not both" in str(exc.value)

    def test_the_po_lookup_lists_the_shipments_with_what_is_open(self, clerk):
        line = _setup()
        shipment = ImportShipment.objects.get(boe_number="2477361")
        mir_service.post_mir(_body(shipment, [_ln(line, "100800")]), clerk)
        c = APIClient()
        c.force_authenticate(user=clerk)
        data = c.get(f"/api/mir/purchase-orders/{line.purchase_order_id}").json()
        assert data["kind"] == "import"
        by_boe = {s["boeNumber"]: s for s in data["shipments"]}
        assert by_boe["2477361"]["lines"][0]["openQty"] == "0.000"
        assert by_boe["2956922"]["lines"][0]["expectedRate"] == "145.8000"


BOE_READ = {
    "boe_number": "4026152", "boe_date": "2026-09-28", "bill_of_lading_number": "KMTCTAO9999999",
    "laden_on_board_date": "2026-09-10", "supplier_name": "Kumho Petrochemical", "po_numbers": "1000001519",
    "country_of_origin": "Korea", "currency": "USD", "exchange_rate": "97.20", "currency_after_taxes": "INR",
    "total_inclusive_value": "21000000.00",
    "lines": [{"po_number": "", "description": "SYNTHETIC RUBBER SBR 1502", "hsn": "40021990", "qty": "100800", "uom": "KGS",
               "unit_price": "1.5", "total_inclusive_value": "21000000.00", "license_type": "ADVANCE",
               "license_number": "0311051817"}],
    "notes": "",
}


def _boe_document(reference="4026152", po_number="1000001519"):
    n = Document.objects.count() + 1
    return Document.objects.create(kind="BOE", plant=Plant.objects.get(code="vapi"), po_number=po_number, reference=reference,
                                   revision=n, storage_key=f"vapi/{po_number}/boe-{reference}/r{n}.pdf", original_filename="boe.pdf",
                                   content_type="application/pdf", size_bytes=10, sha256=f"{n:064d}", uploaded_by_email="cha@ravasco.com")


@pytest.mark.django_db
class TestBoeReading:
    def test_a_boe_is_read_paired_to_its_po_line_and_approved_into_a_shipment(self, configured, monkeypatch,  # noqa: F811
                                                                               django_capture_on_commit_callbacks, clerk):
        line = _setup()
        _fake_read(monkeypatch, BOE_READ)
        ext = _read(django_capture_on_commit_callbacks, doc=_boe_document(), user=clerk)
        assert ext.kind == "BOE" and ext.status == "READY"
        assert ext.draft["lines"][0]["po_line_id"] == str(line.id)
        review = po_extraction.review(ext)
        assert review["problems"] == [] and review["sheet"] is None
        ext = po_extraction.approve(ext, None, clerk)
        sh = ext.shipment
        assert (sh.boe_number, sh.source, sh.exchange_rate, sh.boe_date) == ("4026152", "app", Decimal("97.2000"), datetime.date(2026, 9, 28))
        sl = sh.lines.get()
        assert (sl.po_line_id, sl.qty_as_per_boe, sl.license_type, sl.license_number) == (line.id, Decimal("100800.000"), "ADVANCE", "0311051817")
        # The new shipment is receivable at once.
        assert mir_service.post_mir(_body(sh, [_ln(line, "100800")]), clerk).shipment_id == sh.id

    def test_a_boe_the_csv_already_made_is_compared_then_taken_over(self, configured, monkeypatch,  # noqa: F811
                                                                     django_capture_on_commit_callbacks, clerk):
        _setup()
        read = copy.deepcopy(BOE_READ)
        read["boe_number"] = "2477361"
        read["lines"][0]["qty"] = "100000"
        _fake_read(monkeypatch, read)
        ext = _read(django_capture_on_commit_callbacks, doc=_boe_document(reference="2477361"), user=clerk)
        sheet = po_extraction.review(ext)["sheet"]
        assert sheet["source"] == "csv" and {"Item 1 quantity", "Bill of Lading number"} <= {d["field"] for d in sheet["differences"]}
        sh = po_extraction.approve(ext, None, clerk).shipment
        assert sh.id == ImportShipment.objects.get(boe_number="2477361").id and sh.source == "app"
        assert project_plant_import_shipments("vapi").held == ["2477361"]
        assert ImportShipment.objects.filter(boe_number="2477361").count() == 1
        assert ImportShipmentLine.objects.get(shipment=sh).qty_as_per_boe == Decimal("100000.000")

    @pytest.mark.parametrize("change, field", [
        ({"boe_number": "9999999"}, "boe_number"),
        ({"exchange_rate": ""}, "exchange_rate"),
        ({"bill_of_lading_number": ""}, "bill_of_lading_number"),
    ])
    def test_approval_is_refused_with_the_problem_named(self, configured, monkeypatch,  # noqa: F811
                                                        django_capture_on_commit_callbacks, clerk, change, field):
        _setup()
        _fake_read(monkeypatch, {**copy.deepcopy(BOE_READ), **change})
        ext = _read(django_capture_on_commit_callbacks, doc=_boe_document(), user=clerk)
        with pytest.raises(po_extraction.ExtractionError) as exc:
            po_extraction.approve(ext, None, clerk)
        assert field in {p["field"] for p in exc.value.problems}

    def test_an_unpaired_item_blocks_approval(self, configured, monkeypatch, django_capture_on_commit_callbacks, clerk):  # noqa: F811
        _setup()
        read = copy.deepcopy(BOE_READ)
        read["po_numbers"] = ""
        read["lines"][0].update(description="Titanium dioxide", hsn="32061110")
        _fake_read(monkeypatch, read)
        ext = _read(django_capture_on_commit_callbacks, doc=_boe_document(po_number="9999"), user=clerk)
        assert ext.draft["lines"][0]["po_line_id"] == ""
        assert "po_line_id" in {p["field"] for p in po_extraction.review(ext)["problems"]}

    def test_a_boe_reading_is_reviewed_with_import_docs_not_po_upload(self, configured, monkeypatch,  # noqa: F811
                                                                      django_capture_on_commit_callbacks, clerk):
        from apps.api.permissions import Perm
        _setup()
        _fake_read(monkeypatch, BOE_READ)
        ext = _read(django_capture_on_commit_callbacks, doc=_boe_document(), user=clerk)
        docs_only = make_user(email="imports@ravasco.com", role="user", plants=["vapi"], permissions=[Perm.IMPORT_DOCS])
        po_only = make_user(email="purchase@ravasco.com", role="user", plants=["vapi"], permissions=[Perm.PO_UPLOAD])
        a, b = APIClient(), APIClient()
        a.force_authenticate(user=docs_only)
        b.force_authenticate(user=po_only)
        assert a.get(f"/api/po-extractions/{ext.id}").status_code == 200
        assert b.get(f"/api/po-extractions/{ext.id}").status_code == 404
        assert a.get(f"/api/po-extractions/{ext.id}").json()["lineOptions"]["po_line_id"][0]["label"].startswith("PO 1000001519 line 1")
