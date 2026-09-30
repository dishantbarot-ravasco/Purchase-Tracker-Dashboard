"""
Uploaded PO and invoice files (apps/services/documents.py, the
/api/documents/... endpoints). R2 is replaced by a recording fake, so these
assert what would be stored and under which key, and the revision and
withdrawal rules that cover a revised or cancelled PO.
"""

import datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Document, Mir, Plant, Vendor
from apps.services import object_storage

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
PDF_REVISED = b"%PDF-1.7\n% revised\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


class FakeR2:
    def __init__(self):
        self.stored = {}

    def install(self, monkeypatch):
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "is_configured", lambda kind: True)
        monkeypatch.setattr(object_storage, "upload_file", self.upload_file)
        monkeypatch.setattr(object_storage, "presigned_url",
                            lambda kind, key, expires_seconds=300: f"https://r2.example/{kind}/{key}?exp={expires_seconds}")

    def upload_file(self, kind, key, path, content_type="application/octet-stream"):
        with open(path, "rb") as fh:
            self.stored[(kind, key)] = (fh.read(), content_type)


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    fake.install(monkeypatch)
    return fake


def _client(role="editor", plants=None, email="e@ravasco.com"):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role=role, plants=plants or []))
    return client


def _upload(client, po="3000001167", plant="hrs", body=PDF, name="po.pdf", note=""):
    return client.post("/api/documents/po/upload",
                       {"plant": plant, "poNumber": po, "note": note, "file": SimpleUploadedFile(name, body)},
                       format="multipart")


def _mir(plant="hrs", status=Mir.Status.POSTED):
    vendor = Vendor.objects.create(name="Prime", name_key="prime", gstin="27AAACP5506B1ZW")
    return Mir.objects.create(plant=Plant.objects.get(code=plant), fy="2026-27", seq=1, mir_no="HRS/26-27/0001",
                              mir_date=datetime.date(2026, 9, 1), vendor=vendor, invoice_no="INV-1", invoice_key="INV1",
                              invoice_date=datetime.date(2026, 9, 1), invoice_fy="2026-27", invoice_total=Decimal("1"),
                              tax_type="IGST", created_by_email="s@ravasco.com", status=status,
                              **({"cancelled_at": datetime.datetime(2026, 9, 2, tzinfo=datetime.UTC), "cancel_reason": "Wrong PO"}
                                 if status == Mir.Status.CANCELLED else {}))


@pytest.mark.django_db
class TestPoUpload:
    def test_a_po_file_is_stored_in_the_po_bucket_and_recorded(self, r2):
        res = _upload(_client(), note="first copy")
        assert res.status_code == 201, res.content
        doc = Document.objects.get()
        assert (doc.kind, doc.plant.code, doc.po_number, doc.revision, doc.status) == ("PO", "hrs", "3000001167", 1, "CURRENT")
        [(kind, key)] = r2.stored
        assert kind == "po" and key == doc.storage_key and key.startswith("hrs/3000001167/r1-") and key.endswith(".pdf")
        assert r2.stored[(kind, key)] == (PDF, "application/pdf")
        assert doc.uploaded_by_email == "e@ravasco.com" and doc.note == "first copy"

    def test_a_revised_po_becomes_the_next_revision_and_supersedes_the_last(self, r2):
        client = _client()
        _upload(client)
        res = _upload(client, body=PDF_REVISED, name="po-rev1.pdf")
        assert res.status_code == 201
        assert list(Document.objects.order_by("revision").values_list("revision", "status")) == [
            (1, "SUPERSEDED"), (2, "CURRENT")]
        assert len(r2.stored) == 2, "the older file is kept, never overwritten"

    def test_the_same_file_twice_is_refused_not_a_new_revision(self, r2):
        client = _client()
        _upload(client)
        res = _upload(client, name="same-again.pdf")
        assert res.status_code == 400 and "revision 1" in res.json()["error"]
        assert Document.objects.count() == 1 and len(r2.stored) == 1

    def test_revisions_count_per_plant_and_po(self, r2):
        client = _client()
        _upload(client, plant="hrs")
        _upload(client, plant="vapi", po="3000001167")
        assert set(Document.objects.values_list("plant__code", "revision", "status")) == {
            ("hrs", 1, "CURRENT"), ("vapi", 1, "CURRENT")}

    def test_spaces_in_the_po_number_are_ignored(self, r2):
        _upload(_client(), po=" 3000 001167 ")
        assert Document.objects.get().po_number == "3000001167"

    @pytest.mark.parametrize("body,name", [(b"MZ\x90\x00 not a pdf", "evil.pdf"), (b"", "empty.pdf")])
    def test_only_pdf_jpeg_or_png_content_is_accepted(self, r2, body, name):
        res = _upload(_client(), body=body, name=name)
        assert res.status_code == 400
        assert Document.objects.count() == 0 and r2.stored == {}

    def test_an_image_is_accepted(self, r2):
        assert _upload(_client(), body=PNG, name="photo.png").status_code == 201
        assert Document.objects.get().content_type == "image/png"

    def test_a_viewer_cannot_upload(self, r2):
        assert _upload(_client(role="viewer")).status_code == 403

    def test_an_editor_cannot_upload_for_another_plant(self, r2):
        assert _upload(_client(plants=["vapi"]), plant="hrs").status_code == 403
        assert Document.objects.count() == 0

    def test_without_storage_set_up_the_upload_says_so(self, settings):
        settings.R2_ACCOUNT_ID = ""
        res = _upload(_client())
        assert res.status_code == 503 and "Cloudflare" in res.json()["error"]


@pytest.mark.django_db
class TestWithdraw:
    def test_withdrawing_the_current_revision_restores_the_previous_one(self, r2):
        """A file uploaded by mistake is withdrawn and what was there before
        comes back."""
        client = _client()
        _upload(client)
        second = _upload(client, body=PDF_REVISED).json()
        res = client.post(f"/api/documents/{second['id']}/withdraw", {"reason": "Wrong PO"}, format="json")
        assert res.status_code == 200 and res.json()["status"] == "WITHDRAWN"
        assert list(Document.objects.order_by("revision").values_list("revision", "status")) == [
            (1, "CURRENT"), (2, "WITHDRAWN")]

    def test_a_cancelled_po_is_withdrawn_with_its_reason_and_kept(self, r2):
        client = _client()
        doc = _upload(client).json()
        client.post(f"/api/documents/{doc['id']}/withdraw", {"reason": "PO cancelled by purchase"}, format="json")
        stored = Document.objects.get()
        assert (stored.status, stored.withdraw_reason, stored.withdrawn_by_email) == (
            "WITHDRAWN", "PO cancelled by purchase", "e@ravasco.com")
        assert len(r2.stored) == 1, "the file itself is never deleted"

    def test_a_withdrawn_file_may_be_uploaded_again(self, r2):
        client = _client()
        doc = _upload(client).json()
        client.post(f"/api/documents/{doc['id']}/withdraw", {"reason": "Uploaded under the wrong plant"}, format="json")
        res = _upload(client)
        assert res.status_code == 201 and res.json()["revision"] == 2

    def test_a_reason_is_required(self, r2):
        client = _client()
        doc = _upload(client).json()
        res = client.post(f"/api/documents/{doc['id']}/withdraw", {"reason": "  "}, format="json")
        assert res.status_code == 400
        assert Document.objects.get().status == "CURRENT"

    def test_an_editor_of_another_plant_cannot_withdraw(self, r2):
        doc = _upload(_client()).json()
        res = _client(plants=["vapi"], email="v@ravasco.com").post(
            f"/api/documents/{doc['id']}/withdraw", {"reason": "x"}, format="json")
        assert res.status_code == 403


@pytest.mark.django_db
class TestListAndOpen:
    def test_the_list_is_plant_scoped_and_says_whether_the_po_is_in_the_system(self, r2):
        editor = _client()
        _upload(editor, plant="hrs", po="3000001167")
        _upload(editor, plant="vapi", po="1099990001")
        viewer = _client(role="viewer", plants=["hrs"], email="hv@ravasco.com")
        res = viewer.get("/api/documents/po")
        assert res.status_code == 200
        docs = res.json()["documents"]
        assert [(d["plant"], d["poNumber"], d["poInSystem"]) for d in docs] == [("hrs", "3000001167", False)]

    def test_a_file_opens_through_a_short_lived_link(self, r2):
        doc = _upload(_client()).json()
        res = _client(role="viewer", email="viewer@ravasco.com").get(f"/api/documents/{doc['id']}/open")
        assert res.status_code == 302
        assert res["Location"].startswith("https://r2.example/po/hrs/3000001167/r1-")
        assert res["Location"].endswith("?exp=300")
        assert "no-store" in res["Cache-Control"]

    def test_another_plants_file_does_not_open(self, r2):
        doc = _upload(_client(), plant="vapi").json()
        res = _client(role="viewer", plants=["hrs"], email="hv@ravasco.com").get(f"/api/documents/{doc['id']}/open")
        assert res.status_code == 404


@pytest.mark.django_db
class TestInvoiceUpload:
    def test_an_invoice_attaches_to_the_mir_and_shows_in_its_detail(self, r2):
        mir = _mir()
        client = _client()
        res = client.post(f"/api/mir/entries/{mir.id}/invoice", {"file": SimpleUploadedFile("inv.pdf", PDF)},
                          format="multipart")
        assert res.status_code == 201, res.content
        [(kind, key)] = r2.stored
        assert kind == "invoice" and key.startswith("hrs/HRS_26-27_0001/r1-")
        detail = client.get(f"/api/mir/entries/{mir.id}").json()
        assert [(f["revision"], f["status"], f["fileName"]) for f in detail["invoiceFiles"]] == [(1, "CURRENT", "inv.pdf")]

    def test_a_replacement_invoice_supersedes_the_first(self, r2):
        mir = _mir()
        client = _client()
        for body in (PDF, PDF_REVISED):
            client.post(f"/api/mir/entries/{mir.id}/invoice", {"file": SimpleUploadedFile("inv.pdf", body)}, format="multipart")
        assert list(mir.documents.order_by("revision").values_list("status", flat=True)) == ["SUPERSEDED", "CURRENT"]

    def test_a_cancelled_mir_takes_no_invoice(self, r2):
        mir = _mir(status=Mir.Status.CANCELLED)
        res = _client().post(f"/api/mir/entries/{mir.id}/invoice", {"file": SimpleUploadedFile("inv.pdf", PDF)},
                             format="multipart")
        assert res.status_code == 400 and Document.objects.count() == 0

    def test_only_an_editor_of_the_receiving_plant_may_attach(self, r2):
        mir = _mir()
        res = _client(plants=["vapi"]).post(f"/api/mir/entries/{mir.id}/invoice",
                                            {"file": SimpleUploadedFile("inv.pdf", PDF)}, format="multipart")
        assert res.status_code == 403
