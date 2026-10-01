"""
Security pass, 2026-10-01: an oversized request is refused before it is read,
the Django admin sign-in locks after repeated failures, and a PDF carrying
active content (JavaScript, a launch action, an embedded file) is never stored.
"""

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.api.tests.test_documents import _upload
from apps.core.audit_log import PTAuditLog
from apps.core.models import Document
from apps.services import documents
from config.middleware import AdminLoginThrottleMiddleware, RequestSizeLimitMiddleware

PLAIN_PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n/Author (J Smith) /JSmith 1\n%%EOF"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestRequestSizeLimit:
    def test_an_oversized_body_is_refused_before_the_view(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
        too_big = str(RequestSizeLimitMiddleware.MAX_BYTES + 1)
        response = client.post("/api/documents/po/upload", {"plant": "hrs"}, format="multipart",
                               CONTENT_LENGTH=too_big)
        assert response.status_code == 413
        assert not Document.objects.exists()

    def test_a_normal_request_passes(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
        assert client.get("/api/mir/entries").status_code == 200

    def test_the_cap_is_above_the_largest_real_upload(self):
        assert RequestSizeLimitMiddleware.MAX_BYTES > documents.MAX_BYTES


@pytest.mark.django_db
class TestAdminLoginLockout:
    def _try(self, client, username, password):
        return client.post("/admin/login/", {"username": username, "password": password, "next": "/admin/"})

    def test_five_failures_lock_the_username_even_with_the_right_password(self):
        User.objects.create_superuser("boss", "boss@ravasco.com", "Right-Passw0rd!")
        client = Client()
        for _ in range(AdminLoginThrottleMiddleware.MAX_FAILURES):
            assert self._try(client, "boss", "wrong").status_code == 200
        assert self._try(client, "boss", "Right-Passw0rd!").status_code == 429
        assert PTAuditLog.objects.filter(action=PTAuditLog.ACTION_AUTH_FAILED).count() == 5

    def test_cycling_usernames_from_one_ip_is_locked_too(self):
        client = Client()
        for i in range(AdminLoginThrottleMiddleware.MAX_FAILURES):
            self._try(client, f"guess{i}", "wrong")
        assert self._try(client, "someone-new", "wrong").status_code == 429

    def test_a_success_signs_in_and_is_not_counted(self):
        User.objects.create_superuser("boss", "boss@ravasco.com", "Right-Passw0rd!")
        client = Client()
        self._try(client, "boss", "wrong")
        assert self._try(client, "boss", "Right-Passw0rd!").status_code == 302


class TestPdfActiveContent:
    @pytest.mark.parametrize("payload", [
        b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>",
        b"/AA << /O << /S /Launch /F (cmd.exe) >> >>",
        b"/Names << /EmbeddedFiles 5 0 R >>",
        b"/Type /EmbeddedFile",
        b"/RichMedia 3 0 R",
        b"/S /Java#53cript",  # hex-escaped name, a common way to hide it
    ])
    def test_active_content_is_refused(self, payload):
        with pytest.raises(documents.DocumentError, match="active content"):
            documents._check_pdf(b"%PDF-1.7\n" + payload + b"\n%%EOF")

    def test_a_plain_pdf_passes_and_look_alike_names_do_not_trip_it(self):
        documents._check_pdf(PLAIN_PDF)


@pytest.mark.django_db
def test_an_active_pdf_upload_is_refused_and_nothing_is_stored(monkeypatch):
    from apps.api.tests.test_documents import FakeR2

    fake = FakeR2()
    fake.install(monkeypatch)
    client = APIClient()
    client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
    response = _upload(client, body=b"%PDF-1.4\n/S /JavaScript /JS (x)\n%%EOF")
    assert response.status_code == 400
    assert "active content" in response.json()["error"]
    assert not Document.objects.exists() and fake.stored == {}

    assert _upload(client, body=PLAIN_PDF).status_code == 201
