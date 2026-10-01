"""
apps/services/rematch.py - pins and corrections re-match on the background
worker instead of inside the request (2026-09-25: Vapi's in-request re-match
took 24.8 s on production against gunicorn's 30 s limit).

The queueing rules are tested with the task call stubbed; the inline pytest
path is exercised end to end by the API tests (a pin still reports
unfilledPins there).
"""

import pytest
from django.core.cache import cache

from apps.services import rematch


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _stub(monkeypatch, result=None):
    runs = []
    queued = []
    monkeypatch.setattr(rematch, "_match_fn", lambda plant: (lambda: runs.append(plant) or (result or {})))
    return runs, queued


class TestQueueing:
    def test_saves_while_a_run_is_queued_share_it(self, monkeypatch):
        """Only the first save queues; the task clears the flag when it
        starts, so until then every further save joins it."""
        monkeypatch.setattr(rematch, "_inline", lambda: False)
        tasks = []
        import django_q.tasks
        monkeypatch.setattr(django_q.tasks, "async_task", lambda *a, **k: tasks.append(a))
        rematch.request_rematch("vapi")
        rematch.request_rematch("vapi")
        rematch.request_rematch("vapi")
        assert len(tasks) == 1
        assert rematch.status("vapi")["queued"] is True

    def test_a_save_during_a_run_queues_the_next_one(self, monkeypatch):
        """run_rematch() clears the flag FIRST - a save arriving mid-run may
        postdate what the run read, so it must get a run of its own."""
        monkeypatch.setattr(rematch, "_inline", lambda: False)
        tasks = []
        import django_q.tasks
        monkeypatch.setattr(django_q.tasks, "async_task", lambda *a, **k: tasks.append(a))

        def mid_run_save():
            rematch.request_rematch("vapi")
            return {}
        monkeypatch.setattr(rematch, "_match_fn", lambda plant: mid_run_save)
        rematch.request_rematch("vapi")
        rematch.run_rematch("vapi")
        assert len(tasks) == 2

    def test_a_run_the_worker_never_starts_is_reported_stalled(self, monkeypatch):
        """qcluster down: the flag sits there. Fresh it is just queued; past
        _STALL_SECONDS the page is told to stop waiting."""
        import datetime

        from django.utils import timezone
        monkeypatch.setattr(rematch, "_inline", lambda: False)
        import django_q.tasks
        monkeypatch.setattr(django_q.tasks, "async_task", lambda *a, **k: None)
        assert rematch.request_rematch("vapi")["stalled"] is False
        old = (timezone.now() - datetime.timedelta(seconds=rematch._STALL_SECONDS + 5)).isoformat()
        cache.set(rematch._PENDING_KEY.format("vapi"), old, 60)
        status = rematch.status("vapi")
        assert status["queued"] is True and status["stalled"] is True

    def test_a_finished_run_reports_the_pins_it_could_not_apply(self, monkeypatch):
        unfilled = [{"poNumber": "1", "itemRef": "0", "mirNo": "MIR1", "poKind": "domestic"}]
        _stub(monkeypatch, {"manual_pins_unfilled": unfilled, "manual_pins_stale": []})
        status = rematch.request_rematch("hrs")
        assert status["state"] == "done" and status["queued"] is False
        assert status["unfilledPins"] == unfilled

    def test_a_run_whose_worker_died_is_reported_failed_not_running_forever(self):
        from datetime import timedelta

        from django.utils import timezone

        started = (timezone.now() - timedelta(minutes=30)).isoformat()
        cache.set(rematch._RESULT_KEY.format("vapi"), {"state": "running", "startedAt": started}, None)
        assert rematch.status("vapi")["state"] == "failed"
        cache.set(rematch._RESULT_KEY.format("vapi"), {"state": "running", "startedAt": timezone.now().isoformat()}, None)
        assert rematch.status("vapi")["state"] == "running"

    def test_a_failed_run_is_reported_not_raised(self, monkeypatch):
        def boom():
            raise RuntimeError("db went away")
        monkeypatch.setattr(rematch, "_match_fn", lambda plant: boom)
        status = rematch.request_rematch("hrs")
        assert status["state"] == "failed"
        assert "db went away" in status["error"]


@pytest.mark.django_db(transaction=True)
class TestOneMatchPerPlantAtATime:
    def test_run_full_match_takes_the_plant_advisory_lock(self):
        """Two overlapping runs of one plant would each delete and rewrite the
        other's rows. The lock is held for the run's transaction: while it
        is, another connection cannot take it."""
        import threading

        from django.db import connection, connections

        from apps.services import matching_core
        from apps.services.matching import MATCH_CONFIG

        seen = {}
        entered = threading.Event()
        release = threading.Event()
        real_lock = matching_core._lock_plant_match

        def hold(config):
            real_lock(config)
            entered.set()
            release.wait(10)

        key_sql = "SELECT pg_try_advisory_xact_lock(%s)"
        import zlib
        key = zlib.crc32(("run_full_match:" + MATCH_CONFIG.syncrun_plant).encode())

        def run():
            matching_core._lock_plant_match = hold
            try:
                matching_core.run_full_match(MATCH_CONFIG)
            finally:
                matching_core._lock_plant_match = real_lock
                connections.close_all()

        t = threading.Thread(target=run)
        t.start()
        assert entered.wait(10)
        with connection.cursor() as cur:
            cur.execute(key_sql, [key])
            seen["other_connection_got_it"] = cur.fetchone()[0]
        release.set()
        t.join(20)
        assert seen["other_connection_got_it"] is False
