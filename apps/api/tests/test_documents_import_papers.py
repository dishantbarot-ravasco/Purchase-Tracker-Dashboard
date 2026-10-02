"""
Import paperwork filed under its PO (2026-10-01): a Bill of Entry, an
Advance License or a RoDTEP scrip file, each with its own number, through
the same upload endpoint as PO copies (apps/services/documents.py). R2 is
the recording fake from test_documents.py.
"""

import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from apps.api.tests.test_documents import PDF, PDF_REVISED, FakeR2, _client, _upload
from apps.core.models import Document, Plant, PurchaseOrder, RTPVapiImportPurchaseOrder

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    fake.install(monkeypatch)
    return fake


def _xlsx(extra=None, content_types_patch=None):
    """A real .xlsx from openpyxl, optionally with an extra zip part (a
    macro project, an embedded object) or a rewritten [Content_Types].xml."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Sr No", "Script No"])
    buf = io.BytesIO()
    wb.save(buf)
    if not extra and not content_types_patch:
        return buf.getvalue()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml" and content_types_patch:
                data = content_types_patch(data)
            dst.writestr(item, data)
        for name, data in (extra or {}).items():
            dst.writestr(name, data)
    return out.getvalue()


def _docx_like():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return out.getvalue()


def _paper(client, kind="BOE", ref="4026152", po="1000001576", plant="vapi", body=PDF, name="BOE.pdf"):
    return client.post("/api/documents/po/upload",
                       {"plant": plant, "kind": kind, "reference": ref, "poNumber": po, "note": "",
                        "file": SimpleUploadedFile(name, body)}, format="multipart")


@pytest.mark.django_db
class TestImportPapers:
    def test_a_boe_is_filed_under_its_po_with_its_number(self, r2):
        res = _paper(_client())
        assert res.status_code == 201, res.content
        body = res.json()
        assert (body["kind"], body["kindLabel"], body["reference"], body["poNumber"]) == (
            "BOE", "Bill of Entry", "4026152", "1000001576")
        [(bucket, key)] = r2.stored
        assert bucket == "po" and key.startswith("vapi/1000001576/boe-4026152/r1-") and key.endswith(".pdf")

    def test_revisions_count_per_boe_not_per_po(self, r2):
        """One PO clears on several BOEs; each is its own series, and the PO
        copy's own series is untouched."""
        client = _client()
        _upload(client, plant="vapi", po="1000001576")
        _paper(client, ref="4026152")
        _paper(client, ref="4026999", body=PDF_REVISED)
        again = _paper(client, ref="4026152", body=b"%PDF-1.7\n% amended\n")
        assert again.json()["revision"] == 2
        assert set(Document.objects.values_list("kind", "reference", "revision", "status")) == {
            ("PO", "", 1, "CURRENT"), ("BOE", "4026999", 1, "CURRENT"),
            ("BOE", "4026152", 1, "SUPERSEDED"), ("BOE", "4026152", 2, "CURRENT")}

    def test_withdrawing_a_boe_restores_its_own_earlier_copy_only(self, r2):
        client = _client()
        _paper(client, ref="4026152")
        _paper(client, ref="4026999", body=PDF_REVISED)
        second = _paper(client, ref="4026152", body=b"%PDF-1.7\n% amended\n").json()
        client.post(f"/api/documents/{second['id']}/withdraw", {"reason": "Wrong BOE"}, format="json")
        assert set(Document.objects.values_list("reference", "revision", "status")) == {
            ("4026152", 1, "CURRENT"), ("4026152", 2, "WITHDRAWN"), ("4026999", 1, "CURRENT")}

    @pytest.mark.parametrize("kind", ["BOE", "ADV_LIC", "RODTEP"])
    def test_the_number_is_required(self, r2, kind):
        res = _paper(_client(), kind=kind, ref="  ")
        assert res.status_code == 400 and "Enter the" in res.json()["error"]
        assert Document.objects.count() == 0 and r2.stored == {}

    def test_a_license_number_gets_back_the_leading_zero_the_sheets_drop(self, r2):
        client = _client()
        _paper(client, kind="ADV_LIC", ref="311051817", name="AA.pdf")
        res = _paper(client, kind="ADV_LIC", ref="0311051817", name="AA.pdf")
        assert res.status_code == 400 and "revision 1" in res.json()["error"], "one license, not two"
        assert Document.objects.get().reference == "0311051817"

    def test_an_unknown_kind_is_refused(self, r2):
        res = _paper(_client(), kind="INVOICE")
        assert res.status_code == 400 and Document.objects.count() == 0

    @pytest.mark.parametrize("kind", ["RODTEP", "ADV_LIC"])
    def test_a_license_may_be_an_excel_workbook(self, r2, kind):
        res = _paper(_client(), kind=kind, ref="2609004872", body=_xlsx(), name="RODTEP-JNPT-3.xlsx")
        assert res.status_code == 201, res.content
        doc = Document.objects.get()
        assert doc.content_type == XLSX_TYPE and doc.storage_key.endswith(".xlsx")

    @pytest.mark.parametrize("kind", ["BOE", "PO"])
    def test_a_boe_or_po_may_not_be_a_workbook(self, r2, kind):
        res = _paper(_client(), kind=kind, body=_xlsx(), name="x.xlsx")
        assert res.status_code == 400 and "PDF, JPEG or PNG" in res.json()["error"]

    @pytest.mark.parametrize("body", [
        _xlsx(extra={"xl/vbaProject.bin": b"\x00macro"}),
        _xlsx(extra={"xl/embeddings/oleObject1.bin": b"\x00ole"}),
        _xlsx(extra={"xl/externalLinks/externalLink1.xml": b"<externalLink/>"}),
        _xlsx(content_types_patch=lambda d: d.replace(b"sheet.main+xml", b"sheet.macroEnabled.main+xml")),
    ], ids=["macros", "embedded-object", "external-link", "xlsm"])
    def test_a_workbook_that_runs_or_carries_something_is_refused(self, r2, body):
        res = _paper(_client(), kind="RODTEP", ref="2609004872", body=body, name="s.xlsx")
        assert res.status_code == 400 and "macros, embedded objects or links" in res.json()["error"]
        assert Document.objects.count() == 0 and r2.stored == {}

    def test_a_zip_bomb_is_refused_before_anything_is_inflated(self, monkeypatch):
        """The unpacked size is judged on the declared sizes first: a part
        read before that check could inflate far past the worker's memory."""
        from apps.services import documents

        reads = []
        real_read = zipfile.ZipExtFile.read
        monkeypatch.setattr(zipfile.ZipExtFile, "read", lambda self, n=-1: reads.append(n) or real_read(self, n))
        monkeypatch.setattr(documents, "_XLSX_MAX_UNPACKED", 1000)  # a real workbook is bigger than this
        with pytest.raises(documents.DocumentError, match="too large once unpacked"):
            documents._check_xlsx(_xlsx())
        assert reads == []

    def test_the_content_types_part_is_read_capped(self, monkeypatch):
        from apps.services import documents

        padded = _xlsx(content_types_patch=lambda d: d + b" " * (2 * documents._XLSX_TYPES_MAX))
        returned = []
        real_read = zipfile.ZipExtFile.read

        def spy(self, n=-1):
            data = real_read(self, n)
            returned.append(len(data))
            return data

        monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
        documents._check_xlsx(padded)  # still a plain workbook, so accepted
        assert returned and max(returned) <= documents._XLSX_TYPES_MAX

    @pytest.mark.parametrize("body", [_docx_like(),b"PK\x03\x04 not really a zip"], ids=["docx", "broken-zip"])
    def test_a_zip_that_is_not_a_workbook_is_refused(self, r2, body):
        res = _paper(_client(), kind="RODTEP", ref="2609004872", body=body, name="s.xlsx")
        assert res.status_code == 400 and "not an Excel workbook" in res.json()["error"]

    def test_the_list_filters_by_kind_finds_by_number_and_knows_import_pos(self, r2):
        RTPVapiImportPurchaseOrder.objects.create(po_drive_folder_name="1000001576", po_number="1000001576",
                                                  vendor_name="Shandong Sunshine")
        client = _client()
        _upload(client, plant="vapi", po="1000001576")
        _paper(client, ref="4026152")
        _paper(client, kind="RODTEP", ref="2609004872", body=_xlsx(), name="r.xlsx")
        everything = client.get("/api/documents/po?plant=vapi").json()["documents"]
        assert {(d["kind"], d["poInSystem"]) for d in everything} == {("PO", True), ("BOE", True), ("RODTEP", True)}
        boes = client.get("/api/documents/po?kind=BOE").json()["documents"]
        assert [d["reference"] for d in boes] == ["4026152"]
        found = client.get("/api/documents/po?q=26090").json()["documents"]
        assert [d["kind"] for d in found] == ["RODTEP"]

    def test_an_import_po_at_another_plant_does_not_count_as_in_the_app(self, r2):
        RTPVapiImportPurchaseOrder.objects.create(po_drive_folder_name="1000001576", po_number="1000001576",
                                                  vendor_name="Shandong Sunshine")
        client = _client()
        _paper(client, plant="hrs")
        [doc] = client.get("/api/documents/po").json()["documents"]
        assert doc["poInSystem"] is False

    def test_the_mir_forms_po_view_still_shows_only_the_po_copy(self, r2):
        from apps.api.routers.document_views import po_files

        client = _client()
        _upload(client, plant="vapi", po="1000001576")
        _paper(client)
        po = PurchaseOrder(plant=Plant.objects.get(code="vapi"), po_number="1000001576")
        assert [f["kind"] for f in po_files(po)] == ["PO"]

    def test_a_paper_opens_from_the_po_bucket(self, r2):
        doc = _paper(_client()).json()
        res = _client(role="viewer", email="viewer@ravasco.com").get(f"/api/documents/{doc['id']}/open")
        assert res.status_code == 302
        assert res["Location"].startswith("https://r2.example/po/vapi/1000001576/boe-4026152/")

    def test_an_editor_of_another_plant_cannot_file_a_paper(self, r2):
        assert _paper(_client(plants=["hrs"])).status_code == 403
        assert Document.objects.count() == 0

    def test_the_database_refuses_a_paper_without_its_number(self):
        with transaction.atomic(), pytest.raises(IntegrityError):
            Document.objects.create(kind="BOE", plant=Plant.objects.get(code="vapi"), po_number="1000001576",
                                    revision=1, storage_key="k", original_filename="f", content_type="application/pdf",
                                    size_bytes=1, sha256="0" * 64, uploaded_by_email="e@ravasco.com")
