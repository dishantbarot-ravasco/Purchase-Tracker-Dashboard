"""
The activity log (2026-10-01): apps/services/activity_log.py, fed by
config.middleware.ActivityLogMiddleware and the page-visit beacon, read by
apps/api/routers/activity_views.py.

Each test drives a real request through the middleware stack and reads back
the PTAuditLog rows it left, so the tests fail if the middleware is taken out
of MIDDLEWARE, if a secret reaches the stored payload, if a rule about what
is (not) logged changes, or if anyone but the owner can read the log.
"""

import datetime

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.audit_log import PTAuditLog
from apps.core.models import PTUser, SortPreset
from apps.services import activity_log, object_storage


OWNER = "dishant.barot@ravasco.com"


def _owner(**extra):
    """The account settings.ACTIVITY_LOG_OWNER_EMAIL names - the only reader."""
    return make_user(email=OWNER, role="admin", **extra)


def _client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def _clear_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestWhatIsRecorded:
    def test_a_refused_mir_post_is_a_change_row_naming_who_what_and_the_refusal(self):
        editor = make_user(email="store@ravasco.com", role="editor")
        response = _client(editor).post("/api/mir/entries/new", {"plant": "hrs", "lines": []}, format="json")
        assert response.status_code == 400

        row = PTAuditLog.objects.get(action=PTAuditLog.ACTION_CHANGE)
        assert (row.actor_id, row.actor_email) == (editor.pk, "store@ravasco.com")
        assert (row.method, row.route, row.status_code) == ("POST", "mir-post", 400)
        assert row.detail.startswith("Posted a MIR")
        assert row.plant == "HRS"
        assert row.payload == {"plant": "hrs", "lines": []}
        assert row.duration_ms is not None

    def test_reads_and_previews_are_not_recorded(self):
        editor = make_user(email="store@ravasco.com", role="editor")
        client = _client(editor)
        assert client.get("/api/mir/entries").status_code == 200
        client.post("/api/mir/preview", {"plant": "HRS"}, format="json")
        assert not PTAuditLog.objects.exists()

    def test_a_refused_sign_in_is_credited_to_the_email_tried_with_the_password_masked(self):
        user = make_user(email="clerk@ravasco.com")
        response = _client().post("/api/auth/login", {"email": "clerk@ravasco.com", "password": "wrong-password"},
                                  format="json")
        assert response.status_code == 400

        row = PTAuditLog.objects.get()
        assert row.action == PTAuditLog.ACTION_AUTH_FAILED
        assert row.actor_id is None
        assert row.actor_email == "clerk@ravasco.com"
        assert row.payload == {"email": "clerk@ravasco.com", "password": "***"}
        assert "wrong-password" not in str(row.payload)
        assert user.pk is not None

    def test_a_successful_auth_action_keeps_its_own_row_and_is_not_logged_twice(self):
        admin = make_user(email="admin@ravasco.com", role="admin")
        response = _client(admin).post("/api/auth/users/create", {
            "email": "new@ravasco.com", "fullName": "New Person", "role": "viewer", "password": "An0therStr0ngPass!",
        }, format="json")
        assert response.status_code == 201

        assert list(PTAuditLog.objects.values_list("action", flat=True)) == [PTAuditLog.ACTION_USER_CREATED]

    def test_an_upload_records_the_form_fields_and_the_file_name_never_its_bytes(self, monkeypatch):
        # Never reach the real R2 bucket, even with it configured in .env.
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "upload_file", lambda *args, **kwargs: None)
        editor = make_user(email="buyer@ravasco.com", role="editor")
        upload = SimpleUploadedFile("po-123.pdf", b"%PDF-1.4 secret contents", content_type="application/pdf")
        response = _client(editor).post("/api/documents/po/upload",
                                         {"plant": "hrs", "poNumber": "TEST-0001", "file": upload}, format="multipart")
        assert response.status_code == 201

        row = PTAuditLog.objects.get(action=PTAuditLog.ACTION_CHANGE)
        assert row.route == "po-document-upload"
        assert row.detail == "Uploaded a PO file - TEST-0001"
        assert (row.payload["plant"], row.plant) == ("hrs", "HRS")
        assert row.payload["poNumber"] == "TEST-0001"
        assert row.payload["_files"] == [{"field": "file", "name": "po-123.pdf", "size": 24}]
        assert "secret contents" not in str(row.payload)

    def test_a_csv_export_is_a_download(self):
        admin = _owner()
        response = _client(admin).get("/api/activity/export")
        assert response.status_code == 200
        assert "attachment" in response["Content-Disposition"]

        row = PTAuditLog.objects.get(action=PTAuditLog.ACTION_DOWNLOAD)
        assert row.detail == "Exported the activity log"
        assert row.payload is None

    def test_a_failure_to_write_the_log_never_breaks_the_request(self, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("database is having a bad day")

        monkeypatch.setattr(PTAuditLog.objects, "create", broken)
        editor = make_user(email="store@ravasco.com", role="editor")
        response = _client(editor).post("/api/mir/entries/new", {"plant": "HRS"}, format="json")
        assert response.status_code == 400


class TestRedaction:
    def test_secrets_are_masked_at_every_depth_and_long_values_cut(self):
        out = activity_log.redact({
            "email": "a@b.com", "newPassword": "x", "otp": "123456", "code": "999999", "refresh_token": "t",
            "material_code": "SBR-1502", "lines": [{"po_line_id": 1, "remarks": "y" * 600, "api_secret": "s"}],
        })
        assert out["newPassword"] == out["otp"] == out["code"] == out["refresh_token"] == "***"
        assert out["material_code"] == "SBR-1502"
        assert out["lines"][0]["api_secret"] == "***"
        assert out["lines"][0]["po_line_id"] == 1
        assert len(out["lines"][0]["remarks"]) == 503

    def test_an_oversized_body_keeps_only_its_field_names(self):
        fitted = activity_log._fit({"lines": ["x" * 400] * 50, "plant": "HRS"})
        assert fitted == {"_truncated": True, "fields": ["lines", "plant"]}


@pytest.mark.django_db
class TestPageVisits:
    def test_a_visit_is_recorded_once_per_five_minutes(self):
        viewer = make_user(email="viewer@ravasco.com")
        client = _client(viewer)
        assert client.post("/api/activity/page-view", {"page": "mir"}, format="json").json() == {"recorded": True}
        assert client.post("/api/activity/page-view", {"page": "mir"}, format="json").json() == {"recorded": False}
        assert client.post("/api/activity/page-view", {"page": "stock"}, format="json").json() == {"recorded": True}

        rows = PTAuditLog.objects.filter(action=PTAuditLog.ACTION_PAGE_VIEW).order_by("id")
        assert [r.detail for r in rows] == ["Opened MIR Entry", "Opened RM Store"]
        assert all(r.actor_id == viewer.pk for r in rows)
        # The beacon itself is not logged a second time as a change.
        assert not PTAuditLog.objects.filter(action=PTAuditLog.ACTION_CHANGE).exists()

    def test_an_old_visit_does_not_hide_a_new_one(self):
        viewer = make_user(email="viewer@ravasco.com")
        client = _client(viewer)
        client.post("/api/activity/page-view", {"page": "home"}, format="json")
        PTAuditLog.objects.update(timestamp=timezone.now() - datetime.timedelta(minutes=6))
        assert client.post("/api/activity/page-view", {"page": "home"}, format="json").json() == {"recorded": True}

    def test_an_unknown_page_is_refused(self):
        response = _client(make_user()).post("/api/activity/page-view", {"page": "../etc"}, format="json")
        assert response.status_code == 400
        assert not PTAuditLog.objects.filter(action=PTAuditLog.ACTION_PAGE_VIEW).exists()

    def test_signed_out_visits_are_refused(self):
        assert _client().post("/api/activity/page-view", {"page": "home"}, format="json").status_code == 401


@pytest.mark.django_db
class TestReadingTheLog:
    def _seed(self):
        a = make_user(email="a@ravasco.com", role="editor", full_name="Asha")
        b = make_user(email="b@ravasco.com", role="editor")
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_LOGIN, actor_id=a.pk, actor_email=a.email, detail="trusted device")
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_CHANGE, actor_id=a.pk, actor_email=a.email,
                                  detail="Posted a MIR - HRS/MIR/26-27/0001", status_code=201, plant="HRS")
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_PAGE_VIEW, actor_id=b.pk, actor_email=b.email,
                                  detail="Opened RM Store")
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_AUTH_FAILED, actor_email=b.email, detail="Sign-in refused")
        return a, b

    @pytest.mark.parametrize("path", ["/api/activity", "/api/activity/people", "/api/activity/export"])
    def test_only_the_owner_may_read_it_everyone_else_gets_a_404(self, path):
        # Other admins included: the log is private to one account.
        for role in ("viewer", "editor", "admin"):
            assert _client(make_user(email=f"{role}@ravasco.com", role=role)).get(path).status_code == 404
        assert _client(_owner()).get(path).status_code == 200

    def test_an_inactive_owner_account_cannot_read_it(self):
        owner = _owner()
        owner.is_active = False
        owner.save(update_fields=["is_active"])
        assert _client(owner).get("/api/activity").status_code == 404

    def test_the_owner_can_be_changed_by_setting(self, settings):
        settings.ACTIVITY_LOG_OWNER_EMAIL = "auditor@ravasco.com"
        assert _client(_owner()).get("/api/activity").status_code == 404
        assert _client(make_user(email="auditor@ravasco.com")).get("/api/activity").status_code == 200

    def test_whoami_shows_the_tab_only_to_the_owner(self):
        assert _client(_owner()).get("/api/auth/me").json()["canViewActivityLog"] is True
        other = make_user(email="admin@ravasco.com", role="admin")
        assert _client(other).get("/api/auth/me").json()["canViewActivityLog"] is False

    def test_django_admin_shows_it_only_to_the_owner(self):
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        model_admin = django_admin.site._registry[PTAuditLog]
        request = RequestFactory().get("/admin/")
        for email, allowed in ((OWNER, True), ("someone.else@ravasco.com", False)):
            request.user = type("U", (), {"email": email, "is_active": True, "is_staff": True})()
            assert model_admin.has_module_permission(request) is allowed
            assert model_admin.has_view_permission(request) is allowed

    def test_filters_by_person_type_and_text(self):
        a, b = self._seed()
        client = _client(_owner())

        rows = client.get(f"/api/activity?actor={a.pk}").json()["rows"]
        assert {r["action"] for r in rows} == {"login", "change"}
        assert rows[0]["actorName"] == "Asha"

        rows = client.get("/api/activity?group=change").json()["rows"]
        assert [r["detail"] for r in rows] == ["Posted a MIR - HRS/MIR/26-27/0001"]

        rows = client.get("/api/activity?q=0001").json()["rows"]
        assert len(rows) == 1

        assert client.get("/api/activity?group=nonsense").status_code == 400
        assert client.get("/api/activity?since=yesterday").status_code == 400

    def test_people_counts_the_last_thirty_days(self):
        a, b = self._seed()
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_CHANGE, actor_id=a.pk, actor_email=a.email, detail="old")
        PTAuditLog.objects.filter(detail="old").update(timestamp=timezone.now() - datetime.timedelta(days=40))

        people = {p["email"]: p for p in _client(_owner())
                  .get("/api/activity/people").json()["people"]}
        assert (people["a@ravasco.com"]["signins"], people["a@ravasco.com"]["changes"]) == (1, 1)
        assert (people["b@ravasco.com"]["visits"], people["b@ravasco.com"]["failed"]) == (1, 1)
        assert people[OWNER]["lastSeen"] is None

    def test_the_export_is_formula_safe(self):
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_CHANGE, actor_email="x@ravasco.com", detail="=HYPERLINK(1)")
        body = _client(_owner()).get("/api/activity/export").content.decode()
        assert "'=HYPERLINK(1)" in body


@pytest.mark.django_db
class TestRetention:
    def test_routine_rows_go_after_ninety_days_sign_ins_stay(self):
        old = timezone.now() - datetime.timedelta(days=PTAuditLog.RETENTION_DAYS + 1)
        recent = timezone.now() - datetime.timedelta(days=PTAuditLog.RETENTION_DAYS - 1)
        for action in (PTAuditLog.ACTION_CHANGE, PTAuditLog.ACTION_DOWNLOAD, PTAuditLog.ACTION_PAGE_VIEW,
                       PTAuditLog.ACTION_LOGIN, PTAuditLog.ACTION_AUTH_FAILED, PTAuditLog.ACTION_USER_CREATED):
            PTAuditLog.objects.create(action=action, timestamp=old, detail="old")
            PTAuditLog.objects.create(action=action, timestamp=recent, detail="recent")

        assert activity_log.scheduled_prune() == 3

        assert not PTAuditLog.objects.filter(detail="old", action__in=PTAuditLog.ROUTINE_ACTIONS).exists()
        assert PTAuditLog.objects.filter(detail="recent").count() == 6
        assert set(PTAuditLog.objects.filter(detail="old").values_list("action", flat=True)) == {
            PTAuditLog.ACTION_LOGIN, PTAuditLog.ACTION_AUTH_FAILED, PTAuditLog.ACTION_USER_CREATED}


@pytest.mark.django_db
class TestLastActive:
    """A sign-in lasts up to 30 days, so last_login_at alone said a person
    who works every day was last seen weeks ago (owner, 2026-10-01)."""

    def test_any_signed_in_request_stamps_last_seen_at_most_every_five_minutes(self):
        viewer = make_user(email="viewer@ravasco.com")
        client = _client(viewer)
        assert client.get("/api/mir/entries").status_code == 200
        viewer.refresh_from_db()
        first = viewer.last_seen_at
        assert first is not None

        client.get("/api/mir/entries")
        viewer.refresh_from_db()
        assert viewer.last_seen_at == first  # throttled: no write inside five minutes

        PTUser.objects.filter(pk=viewer.pk).update(last_seen_at=first - datetime.timedelta(minutes=6))
        client.get("/api/mir/entries")
        viewer.refresh_from_db()
        assert viewer.last_seen_at > first - datetime.timedelta(minutes=6)

    def test_last_active_is_not_the_last_sign_in(self):
        worker = make_user(email="worker@ravasco.com", role="editor")
        PTUser.objects.filter(pk=worker.pk).update(
            last_login_at=timezone.now() - datetime.timedelta(days=21), last_seen_at=timezone.now())

        people = {p["email"]: p for p in _client(_owner()).get("/api/activity/people").json()["people"]}
        row = people["worker@ravasco.com"]
        assert row["lastActive"] == row["lastSeen"]
        assert row["lastActive"] > row["lastLogin"]

    def test_saved_work_from_before_the_log_counts_as_activity(self):
        worker = make_user(email="worker@ravasco.com", role="editor")
        SortPreset.objects.create(user=worker, view="po_list", name="Mine", levels=[])
        SortPreset.objects.filter(user=worker).update(updated_at=timezone.now() - datetime.timedelta(days=1))

        row = {p["email"]: p for p in _client(_owner()).get("/api/activity/people").json()["people"]}["worker@ravasco.com"]
        assert row["lastWorkWhat"] == "Sort preset saved"
        assert row["lastActive"] == row["lastWork"]

    def test_people_says_when_tracking_started(self):
        PTAuditLog.objects.create(action=PTAuditLog.ACTION_PAGE_VIEW, detail="Opened Home")
        data = _client(_owner()).get("/api/activity/people").json()
        assert data["trackingSince"] is not None
