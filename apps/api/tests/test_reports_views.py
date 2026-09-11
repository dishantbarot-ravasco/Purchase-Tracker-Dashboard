"""
Tests for apps/api/routers/reports_views.py's trigger_daily_report/
trigger_monthly_report/trigger_mismatch_report/trigger_prune_revoked_tokens -
the shared-secret-protected endpoints an external free scheduler
(cron-job.org) hits (daily / on the 1st of each month / whatever cadence is
configured) to send the Raw Material Consumption and plant-head Data
Correction reports, and to sweep expired RevokedRefreshToken rows. Only the
auth scheme (and trigger_monthly_report's own year/month param validation) is
tested here (settings.override + APIClient) - the report content itself is
covered in apps/services/tests/test_consumption_report.py/
test_plant_mismatch_report.py, and prune_revoked_tokens' own deletion logic in
apps/api/tests/test_prune_revoked_tokens.py.
"""
import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
class TestTriggerDailyReport:
    def test_missing_secret_setting_returns_503(self, settings):
        settings.REPORT_CRON_SECRET = ""
        response = APIClient().get("/api/internal/send-daily-report", {"secret": "anything"})
        assert response.status_code == 503

    def test_wrong_secret_returns_403(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-daily-report", {"secret": "wrong"})
        assert response.status_code == 403

    def test_missing_secret_param_returns_403(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-daily-report")
        assert response.status_code == 403

    def test_correct_secret_via_query_param_runs_and_returns_ok(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-daily-report", {"secret": "the-real-secret"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        # No admins seeded in this test - send_daily_consumption_reports()
        # short-circuits to 0/0 rather than building/sending anything.
        assert body["plants_sent"] == 0
        assert body["admins_notified"] == 0

    def test_correct_secret_via_header(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get(
            "/api/internal/send-daily-report", HTTP_X_REPORT_SECRET="the-real-secret",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_no_login_session_required(self):
        """The whole point of this endpoint: an unauthenticated caller (no
        JWT cookie, no session) must be able to reach it - only the secret
        gates it, confirmed by not calling force_authenticate anywhere in
        this test class at all."""
        client = APIClient()
        assert "pt_access" not in client.cookies


@pytest.mark.django_db
class TestTriggerMonthlyReport:
    def test_missing_secret_setting_returns_503(self, settings):
        settings.REPORT_CRON_SECRET = ""
        response = APIClient().get("/api/internal/send-monthly-report", {"secret": "anything"})
        assert response.status_code == 503

    def test_wrong_secret_returns_403(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-monthly-report", {"secret": "wrong"})
        assert response.status_code == 403

    def test_correct_secret_runs_and_returns_ok(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-monthly-report", {"secret": "the-real-secret"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["plants_sent"] == 0
        assert body["admins_notified"] == 0
        assert "month" in body  # e.g. "2026-08" - defaults to last completed month

    def test_year_and_month_override_the_default_target_month(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get(
            "/api/internal/send-monthly-report",
            {"secret": "the-real-secret", "year": "2025", "month": "3"},
        )
        assert response.status_code == 200
        assert response.json()["month"] == "2025-03"

    def test_year_without_month_is_rejected(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get(
            "/api/internal/send-monthly-report", {"secret": "the-real-secret", "year": "2025"},
        )
        assert response.status_code == 400

    def test_non_integer_month_is_rejected(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get(
            "/api/internal/send-monthly-report",
            {"secret": "the-real-secret", "year": "2025", "month": "not-a-number"},
        )
        assert response.status_code == 400

    def test_out_of_range_month_is_rejected(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get(
            "/api/internal/send-monthly-report",
            {"secret": "the-real-secret", "year": "2025", "month": "13"},
        )
        assert response.status_code == 400

    def test_no_login_session_required(self):
        client = APIClient()
        assert "pt_access" not in client.cookies


@pytest.mark.django_db
class TestTriggerPruneRevokedTokens:
    def test_missing_secret_setting_returns_503(self, settings):
        settings.REPORT_CRON_SECRET = ""
        response = APIClient().get("/api/internal/prune-revoked-tokens", {"secret": "anything"})
        assert response.status_code == 503

    def test_wrong_secret_returns_403(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/prune-revoked-tokens", {"secret": "wrong"})
        assert response.status_code == 403

    def test_correct_secret_runs_and_returns_deleted_count(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/prune-revoked-tokens", {"secret": "the-real-secret"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["deleted"] == 0

    def test_actually_deletes_expired_rows(self, settings):
        import datetime

        from django.utils import timezone

        from apps.core.models import RevokedRefreshToken

        settings.REPORT_CRON_SECRET = "the-real-secret"
        RevokedRefreshToken.objects.create(jti="expired-1", expires_at=timezone.now() - datetime.timedelta(days=1))
        RevokedRefreshToken.objects.create(jti="still-valid", expires_at=timezone.now() + datetime.timedelta(days=1))

        response = APIClient().get("/api/internal/prune-revoked-tokens", {"secret": "the-real-secret"})
        assert response.status_code == 200
        assert response.json()["deleted"] == 1
        assert RevokedRefreshToken.objects.filter(jti="still-valid").exists()
        assert not RevokedRefreshToken.objects.filter(jti="expired-1").exists()

    def test_no_login_session_required(self):
        client = APIClient()
        assert "pt_access" not in client.cookies


@pytest.mark.django_db
class TestTriggerMismatchReport:
    def test_missing_secret_setting_returns_503(self, settings):
        settings.REPORT_CRON_SECRET = ""
        response = APIClient().get("/api/internal/send-mismatch-report", {"secret": "anything"})
        assert response.status_code == 503

    def test_wrong_secret_returns_403(self, settings):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        response = APIClient().get("/api/internal/send-mismatch-report", {"secret": "wrong"})
        assert response.status_code == 403

    def test_correct_secret_runs_and_returns_ok(self, settings):
        # MISMATCH_REPORT_PLANT_HEADS_ENABLED defaults to False as of
        # 2026-09-11 (see send_plant_mismatch_reports()'s own docstring) -
        # this test exercises the real-delivery path, so it needs the
        # killswitch explicitly on; test_disabled_by_default_returns_ok_
        # without_sending below covers the actual default.
        settings.REPORT_CRON_SECRET = "the-real-secret"
        settings.MISMATCH_REPORT_PLANT_HEADS_ENABLED = True
        response = APIClient().get("/api/internal/send-mismatch-report", {"secret": "the-real-secret"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        # No mismatches seeded in this test - every plant is skipped, not an error.
        assert body["plants_sent"] == 0
        assert body["plants_skipped_no_mismatches"] == 3

    def test_disabled_by_default_returns_ok_without_sending(self, settings):
        """MISMATCH_REPORT_PLANT_HEADS_ENABLED's actual default (False) -
        the endpoint still returns 200/ok, just with plant_heads_disabled
        instead of a real send count."""
        settings.REPORT_CRON_SECRET = "the-real-secret"
        assert settings.MISMATCH_REPORT_PLANT_HEADS_ENABLED is False
        response = APIClient().get("/api/internal/send-mismatch-report", {"secret": "the-real-secret"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["plants_sent"] == 0
        assert body["plant_heads_disabled"] is True

    def test_test_recipient_bypasses_the_disabled_default(self, settings, mailoutbox):
        settings.REPORT_CRON_SECRET = "the-real-secret"
        assert settings.MISMATCH_REPORT_PLANT_HEADS_ENABLED is False
        response = APIClient().get(
            "/api/internal/send-mismatch-report",
            {"secret": "the-real-secret", "test_recipient": "dishant.barot@ravasco.com"},
        )
        assert response.status_code == 200
        assert response.json().get("plant_heads_disabled") is not True

    def test_no_login_session_required(self):
        client = APIClient()
        assert "pt_access" not in client.cookies
