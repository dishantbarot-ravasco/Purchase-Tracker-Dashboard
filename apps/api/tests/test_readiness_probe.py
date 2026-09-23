"""
GET /api/health/ready - the readiness probe (apps/api/views.py, 2026-09-23).

/api/health only proves the process is up. The failure this app has actually
had is a silent one - the qcluster worker not running for 13 days while every
page still loaded (CLAUDE.md, Scheduling) - and missed stock snapshots cannot
be recovered afterwards. These tests pin what makes the probe useful to an
uptime monitor: the status CODE flips on a genuinely missed day, not on the
normal overnight gap, and it never needs credentials.
"""

import datetime

import pytest
from django.db import OperationalError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api import views
from apps.core.models import SyncRun

URL = "/api/health/ready"


def _completed(plant, source, hours_ago, status=SyncRun.Status.SUCCESS):
    finished = timezone.now() - datetime.timedelta(hours=hours_ago)
    SyncRun.objects.create(
        plant=plant, source=source, status=status, started_at=finished, finished_at=finished, error_detail="",
    )


def _all_fresh(hours_ago=1):
    for plant in views._WATCHED_PLANTS:
        for source in views._WATCHED_SOURCES:
            _completed(plant, source, hours_ago)


@pytest.mark.django_db
class TestReadinessProbe:
    def test_everything_fresh_is_ok(self):
        _all_fresh()

        response = APIClient().get(URL)

        assert response.status_code == 200
        assert response.data["status"] == "ok"
        assert response.data["stale"] == []

    def test_the_normal_overnight_gap_is_not_an_alarm(self):
        """Last sync 20:00, next 09:00 - 13 hours is the schedule working, and
        a probe that paged someone every night would be switched off."""
        _all_fresh(hours_ago=13)

        assert APIClient().get(URL).status_code == 200

    def test_one_missed_day_on_one_step_is_degraded_and_named(self):
        _all_fresh()
        _completed(SyncRun.Plant.RTP_VAPI, SyncRun.Source.STOCK, hours_ago=30)
        SyncRun.objects.filter(
            plant=SyncRun.Plant.RTP_VAPI, source=SyncRun.Source.STOCK, finished_at__gte=timezone.now() - datetime.timedelta(hours=2),
        ).delete()

        response = APIClient().get(URL)

        assert response.status_code == 503
        assert response.data["status"] == "degraded"
        assert response.data["stale"] == [f"{SyncRun.Plant.RTP_VAPI}/{SyncRun.Source.STOCK}"]

    def test_a_step_that_only_ever_failed_is_stale(self):
        """A FAILED run is not evidence the step works - a sync failing every
        hour is exactly as broken as one never running."""
        _all_fresh()
        SyncRun.objects.filter(plant=SyncRun.Plant.HRS, source=SyncRun.Source.MATCH).update(status=SyncRun.Status.FAILED)

        response = APIClient().get(URL)

        assert response.status_code == 503
        assert f"{SyncRun.Plant.HRS}/{SyncRun.Source.MATCH}" in response.data["stale"]

    def test_a_partial_run_counts_as_having_run(self):
        _all_fresh()
        SyncRun.objects.filter(plant=SyncRun.Plant.HRS, source=SyncRun.Source.STOCK).update(status=SyncRun.Status.PARTIAL)

        assert APIClient().get(URL).status_code == 200

    def test_an_empty_database_is_degraded_not_ok(self):
        """A fresh deploy that has never synced is not ready, and must not
        report that it is."""
        response = APIClient().get(URL)

        assert response.status_code == 503
        assert len(response.data["stale"]) == len(views._WATCHED_PLANTS) * len(views._WATCHED_SOURCES)

    def test_needs_no_credentials_and_ignores_a_bogus_one(self):
        """A monitor must never get a 401: authentication is switched off on
        this view, so even a garbage Bearer header is simply ignored."""
        _all_fresh()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

        assert client.get(URL).status_code == 200

    def test_an_unreachable_database_is_down(self, monkeypatch):
        def _unreachable(*args, **kwargs):
            raise OperationalError("could not connect to server")

        monkeypatch.setattr(views.SyncRun.objects, "filter", _unreachable)

        response = APIClient().get(URL)

        assert response.status_code == 503
        assert response.data == {"status": "down", "database": "unreachable"}

    def test_liveness_is_unchanged(self):
        """/api/health stays a pure liveness check - it must not start failing
        because a sync is stale (that is not a reason to restart the app)."""
        response = APIClient().get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
