"""
apps/services/po_extraction.py and /api/po-extractions/... (owner,
2026-10-03): an uploaded PO file is read into a draft, reviewed, and only an
approved draft reaches the procurement tables - at the plant it is billed to.
The Claude call and the R2 read are stubbed; nothing leaves the machine.
"""

import copy
import datetime

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Document, MirLine, Plant, PoExtraction, PurchaseOrder, PurchaseOrderLine
from apps.services import mir_service, po_extraction

HRS_BILLING = "Hindustan Rubbers Silvassa, Kharadpada, Naroli, 375/4/2, 396230"
VAPI_BILLING = "Ravasco Transmission And Packing Pvt Ltd, 164,165/P&166/P, 2nd Phase, GIDC Ind. Estate, 396195 Vapi"

READ = {
    "po_number": "3000009001", "po_date": "2026-09-20", "vendor_name": "Prime Chemicals",
    "vendor_address": "Mumbai", "vendor_gstin": "27AAACP5506B1ZW", "vendor_email": "a@prime.example",
    "vendor_sap_code": "300001620", "billing_address": HRS_BILLING, "shipping_address": HRS_BILLING,
    "payment_terms": "60 days", "incoterms": "DDP", "currency": "INR", "tax_type": "IGST",
    "total_value": "6000.00", "tax_amount": "1080.00", "total_inclusive_value": "7080.00", "remarks": "",
    "lines": [
        {"item_code": "22001132", "description": "SBR 1502", "hsn": "4002", "qty": "100", "uom": "KG", "rate": "50",
         "net_value": "5000.00", "delivery_date": "2026-10-10", "remarks": ""},
        {"item_code": "", "description": "Zinc Oxide", "hsn": "2817", "qty": "10", "uom": "Kgs", "rate": "100",
         "net_value": "1000.00", "delivery_date": "2026-10-10", "remarks": ""},
    ],
    "notes": "",
}


@pytest.fixture
def configured(settings, monkeypatch):
    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr("apps.services.documents.read_bytes", lambda doc: b"%PDF-1.7 test")
    return settings


def _fake_read(monkeypatch, reply):
    calls = []

    def fake(data, content_type, *args):
        calls.append((data, content_type))
        if isinstance(reply, Exception):
            raise reply
        return {"json": copy.deepcopy(reply), "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 800}

    monkeypatch.setattr(po_extraction, "_call_claude", fake)
    return calls


def _document(plant="hrs", po_number="3000009001"):
    n = Document.objects.count() + 1
    return Document.objects.create(kind="PO", plant=Plant.objects.get(code=plant), po_number=po_number, revision=n,
                                   storage_key=f"{plant}/{po_number}/r{n}.pdf", original_filename="po.pdf",
                                   content_type="application/pdf", size_bytes=10, sha256=f"{n:064d}",
                                   uploaded_by_email="up@ravasco.com")


def _read(django_capture_on_commit_callbacks, doc=None, user=None):
    doc = doc or _document()
    with django_capture_on_commit_callbacks(execute=True):
        ext = po_extraction.request(doc, user or make_user(email="pm@ravasco.com", role="editor"))
    ext.refresh_from_db()
    return ext


def _client(plants=("hrs",), email="pm2@ravasco.com", role="editor"):
    c = APIClient()
    c.force_authenticate(user=make_user(email=email, role=role, plants=list(plants)))
    return c


@pytest.mark.django_db
class TestReading:
    def test_a_file_is_read_into_a_draft_on_the_worker(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        calls = _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        assert calls == [(b"%PDF-1.7 test", "application/pdf")]
        assert (ext.status, ext.model_name, ext.input_tokens) == ("READY", "claude-opus-5-5", 1200)
        assert ext.draft["lines"][1]["uom"] == "Kgs" and ext.extracted == READ
        # Nothing reaches the procurement tables before approval.
        assert not PurchaseOrder.objects.exists()

    def test_a_failure_is_recorded_not_raised(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, po_extraction.ExtractionError("The extraction service is busy - try again in a few minutes."))
        ext = _read(django_capture_on_commit_callbacks)
        assert ext.status == "FAILED" and "busy" in ext.error

    def test_without_a_key_nothing_is_queued(self, settings):
        settings.ANTHROPIC_API_KEY = ""
        with pytest.raises(po_extraction.ExtractionError):
            po_extraction.request(_document(), make_user(email="pm@ravasco.com", role="editor"))
        assert not PoExtraction.objects.exists()


@pytest.mark.django_db
class TestApproving:
    def test_an_approved_draft_becomes_the_plants_po_and_takes_receipts(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        ext = po_extraction.approve(ext, None, make_user(email="boss@ravasco.com", role="editor"))
        po = ext.purchase_order
        assert (po.plant.code, po.po_number, po.source, po.billing_plant.code) == ("hrs", "3000009001", "app", "hrs")
        assert (po.vendor.gstin, po.vendor.vendor_code, po.po_date) == ("27AAACP5506B1ZW", "300001620", datetime.date(2026, 9, 20))
        lines = list(po.lines.order_by("line_no"))
        assert [(ln.line_no, ln.item_code, ln.uom, str(ln.qty_ordered)) for ln in lines] == [
            (1, "22001132", "KG", "100.000"), (2, "", "KG", "10.000")]
        assert all(ln.material_id for ln in lines)
        assert [p.po_number for p in mir_service.search_open_pos("3000009001", ["hrs"])] == ["3000009001"]

    def test_the_reviewers_corrections_are_what_is_written(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        draft = copy.deepcopy(ext.draft)
        draft["lines"][0]["qty"] = "120"
        ext = po_extraction.approve(ext, draft, make_user(email="boss@ravasco.com", role="editor"))
        assert str(ext.purchase_order.lines.get(line_no=1).qty_ordered) == "120.000"

    @pytest.mark.parametrize("change, field", [
        ({"billing_address": VAPI_BILLING}, "billing_address"),
        ({"billing_address": "Some office, Mumbai"}, "billing_address"),
        ({"po_number": "3000009999"}, "po_number"),
        ({"vendor_email": ""}, "vendor_email"),
        ({"vendor_gstin": "27BADGSTIN"}, "vendor_gstin"),
        ({"lines": []}, "lines"),
    ])
    def test_approval_is_refused_with_every_problem_named(self, configured, monkeypatch, django_capture_on_commit_callbacks,
                                                          change, field):
        _fake_read(monkeypatch, {**READ, **change})
        ext = _read(django_capture_on_commit_callbacks)
        with pytest.raises(po_extraction.ExtractionError) as exc:
            po_extraction.approve(ext, None, make_user(email="boss@ravasco.com", role="editor"))
        assert field in {p["field"] for p in exc.value.problems}
        assert not PurchaseOrder.objects.exists()

    def test_the_po_sheets_order_is_compared_then_taken_over(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        hrs = Plant.objects.get(code="hrs")
        sheet = PurchaseOrder.objects.create(plant=hrs, po_number="3000009001", po_date=datetime.date(2026, 9, 20))
        PurchaseOrderLine.objects.create(purchase_order=sheet, line_no=1, description="SBR 1502", uom="KG", qty_ordered=90, rate=50)
        _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        diff = po_extraction.sheet_differences(ext.draft, hrs)
        assert diff["source"] == "csv"
        assert {"Number of lines", "Line 1 quantity", "Vendor name"} <= {d["field"] for d in diff["differences"]}
        po = po_extraction.approve(ext, None, make_user(email="boss@ravasco.com", role="editor")).purchase_order
        assert po.id == sheet.id and po.source == "app" and po.lines.count() == 2
        # The CSV projection now leaves it alone (compared, never added).
        assert PurchaseOrder.objects.get(pk=sheet.pk).source == PurchaseOrder.Source.APP

    def test_a_change_after_receipts_flags_the_line_for_review(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, READ)
        boss = make_user(email="boss@ravasco.com", role="editor")
        po = po_extraction.approve(_read(django_capture_on_commit_callbacks), None, boss).purchase_order
        line = po.lines.get(line_no=1)
        body = {"plant": "hrs", "mir_date": datetime.date.today().isoformat(), "invoice_no": "INV-1",
                "invoice_date": datetime.date.today().isoformat(), "tcs_amount": "0",
                "lines": [{"po_line_id": line.id, "qty_received": "100", "rate": "50", "gst_rate": "18", "material_category": "Rubber"}]}
        body["invoice_total"] = str(mir_service.evaluate(body)["computed_total"])
        mir_service.post_mir(body, boss)
        _fake_read(monkeypatch, {**READ, "lines": [{**READ["lines"][0], "description": "SBR 1712"}, READ["lines"][1]]})
        again = _read(django_capture_on_commit_callbacks, doc=_document(), user=boss)
        po_extraction.approve(again, None, boss)
        line.refresh_from_db()
        assert line.needs_review and line.review_note.startswith("The approved PO file changed description")
        assert MirLine.objects.get().po_line_id == line.id

    def test_a_rejection_needs_a_reason(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        user = make_user(email="boss@ravasco.com", role="editor")
        with pytest.raises(po_extraction.ExtractionError):
            po_extraction.reject(ext, "  ", user)
        assert po_extraction.reject(ext, "Wrong file", user).status == "REJECTED"
        with pytest.raises(po_extraction.ExtractionError):
            po_extraction.approve(ext, None, user)


@pytest.mark.django_db
class TestChecksAndApi:
    def test_figures_that_do_not_add_up_are_flagged_not_blocking(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, {**READ, "total_value": "6500.00"})
        ext = _read(django_capture_on_commit_callbacks)
        assert [c["check"] for c in po_extraction.checks(ext.draft)] == ["total_value", "tax_rate"]
        assert po_extraction.approve(ext, None, make_user(email="boss@ravasco.com", role="editor")).status == "APPROVED"

    def test_the_review_is_scoped_to_the_files_plant(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, READ)
        ext = _read(django_capture_on_commit_callbacks)
        assert _client(plants=("vapi",)).get(f"/api/po-extractions/{ext.id}").status_code == 404
        assert _client(plants=("vapi",), email="v@ravasco.com").post(
            f"/api/po-extractions/{ext.id}/approve", {}, format="json").status_code == 404
        assert _client(role="viewer", email="w@ravasco.com").get("/api/po-extractions").status_code == 403
        own = _client(email="h@ravasco.com")
        data = own.get(f"/api/po-extractions/{ext.id}").json()
        assert data["status"] == "READY" and data["problems"] == [] and data["draft"]["po_number"] == "3000009001"
        assert [e["id"] for e in own.get("/api/po-extractions").json()["extractions"]] == [ext.id]
        res = own.post(f"/api/po-extractions/{ext.id}/approve", {"draft": ext.draft}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "APPROVED"

    def test_a_refused_approval_lists_the_problems(self, configured, monkeypatch, django_capture_on_commit_callbacks):
        _fake_read(monkeypatch, {**READ, "billing_address": VAPI_BILLING})
        ext = _read(django_capture_on_commit_callbacks)
        res = _client(email="h@ravasco.com").post(f"/api/po-extractions/{ext.id}/approve", {}, format="json")
        assert res.status_code == 400 and "RTP - Vapi" in res.json()["error"]
