"""
Test for apps/core/management/commands/ensure_schedules.py - Snapshot
Pipeline Rebuild, Phase B.2 (see CLAUDE.md). Placed under apps/api/tests/
(real Postgres via pytest-django, same as test_prune_revoked_tokens.py)
since django_q.models.Schedule is a real DB-backed model, not
dependency-free pure logic.
"""

import io

import pytest
from django.core.management import call_command
from django_q.models import Schedule


@pytest.mark.django_db
class TestEnsureSchedules:
    def test_creates_exactly_one_schedule_row(self):
        out = io.StringIO()
        call_command("ensure_schedules", stdout=out)

        schedules = Schedule.objects.filter(name="daily-sync-all-plants")
        assert schedules.count() == 1
        schedule = schedules.get()
        assert schedule.func == "apps.services.sync_trigger.run_daily_sync_all_plants"
        assert schedule.schedule_type == Schedule.DAILY
        assert schedule.next_run is not None
        assert "created" in out.getvalue()

    def test_running_twice_still_creates_exactly_one_row(self):
        call_command("ensure_schedules")
        call_command("ensure_schedules")

        assert Schedule.objects.filter(name="daily-sync-all-plants").count() == 1

    def test_second_run_does_not_reset_next_run(self):
        """A deploy re-running this command must never push next_run
        forward past a legitimately-overdue-but-not-yet-executed run - see
        the command's own module docstring for the full reasoning."""
        call_command("ensure_schedules")
        schedule = Schedule.objects.get(name="daily-sync-all-plants")
        original_next_run = schedule.next_run

        call_command("ensure_schedules")

        schedule.refresh_from_db()
        assert schedule.next_run == original_next_run

    def test_corrects_a_drifted_func_without_touching_next_run(self):
        call_command("ensure_schedules")
        schedule = Schedule.objects.get(name="daily-sync-all-plants")
        original_next_run = schedule.next_run
        schedule.func = "some.stale.dotted.path"
        schedule.save(update_fields=["func"])

        call_command("ensure_schedules")

        schedule.refresh_from_db()
        assert schedule.func == "apps.services.sync_trigger.run_daily_sync_all_plants"
        assert schedule.next_run == original_next_run
