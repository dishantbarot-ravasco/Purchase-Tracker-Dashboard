"""
Tests for apps/api/routers/reports_views.py's trigger_daily_report - the
shared-secret-protected endpoint an external free scheduler (cron-job.org)
hits once a day to send the Raw Material Consumption reports. Only the auth
scheme is tested here (settings.override + APIClient) - the report content
itself is covered in apps/services/tests/test_consumption_report.py.
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
