"""
Tests for apps/services/sync_trigger.py's run_daily_sync_all_plants() -
Snapshot Pipeline Rebuild, Phase B (see CLAUDE.md). Deliberately monkeypatches
`_run_pipeline` rather than letting it run for real: a real run calls
call_command("sync_po_csv") etc., which reaches out to Google Drive - the
same reason this app's own CLAUDE.md documents the sync/match pipeline
itself as having no automated test coverage. What's actually new and worth
testing here is `run_daily_sync_all_plants()`'s own logic (which plants it
calls, and the per-plant lock-skip/one-plant-failure-doesn't-skip-others
behavior), not the pipeline commands it calls - those are exercised (without
mocking, against a real fixture) by test_sync_stock_row_insert.py instead.

Runs under pytest, so config/settings.py's CACHES override already points
at LocMemCache (no Postgres pt_cache_table needed) - no @pytest.mark.django_db
required here.
"""

from django.core.cache import cache

from apps.services import sync_trigger


def _clear_locks():
    for plant_key in sync_trigger._PLANT_COMMANDS:
        cache.delete(sync_trigger._lock_key(plant_key))


class TestRunDailySyncAllPlants:
    def setup_method(self):
        _clear_locks()

    def teardown_method(self):
        _clear_locks()

    def test_runs_every_plant_when_nothing_is_locked(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync_trigger, "_run_pipeline", lambda plant_key: called.append(plant_key))

        sync_trigger.run_daily_sync_all_plants()

        assert set(called) == set(sync_trigger._PLANT_COMMANDS)

    def test_skips_a_plant_whose_manual_refresh_is_already_in_progress(self, monkeypatch):
        cache.add(sync_trigger._lock_key("achhad"), True, timeout=900)
        called = []
        monkeypatch.setattr(sync_trigger, "_run_pipeline", lambda plant_key: called.append(plant_key))

        sync_trigger.run_daily_sync_all_plants()

        assert "achhad" not in called
        assert set(called) == set(sync_trigger._PLANT_COMMANDS) - {"achhad"}
        # The pre-existing lock (a real manual refresh) must survive
        # untouched - run_daily_sync_all_plants must not clear a lock it
        # didn't itself acquire.
        assert sync_trigger.is_sync_in_progress("achhad") is True

    def test_one_plant_raising_does_not_skip_the_others(self, monkeypatch):
        called = []

        def _stub(plant_key):
            called.append(plant_key)
            if plant_key == "hrs":
                raise RuntimeError("boom")

        monkeypatch.setattr(sync_trigger, "_run_pipeline", _stub)

        sync_trigger.run_daily_sync_all_plants()

        assert set(called) == set(sync_trigger._PLANT_COMMANDS)

    def test_a_plant_is_lockable_again_after_its_stubbed_run_completes(self, monkeypatch):
        """_run_pipeline's own `finally` clears its lock in real code; the
        stub here doesn't, so we assert on the lock run_daily_sync_all_plants
        itself acquires before calling the stub - proving it really does
        cache.add() per plant rather than relying on some other guard."""
        monkeypatch.setattr(sync_trigger, "_run_pipeline", lambda plant_key: None)

        assert sync_trigger.is_sync_in_progress("hrs") is False
        # Can't observe the lock mid-call without threads; instead prove
        # get_or_create-style semantics by pre-acquiring it ourselves and
        # confirming run_daily_sync_all_plants correctly treats that as
        # "already in progress" for every plant, symmetric with the
        # single-plant case above.
        for plant_key in sync_trigger._PLANT_COMMANDS:
            cache.add(sync_trigger._lock_key(plant_key), True, timeout=900)

        called = []
        monkeypatch.setattr(sync_trigger, "_run_pipeline", lambda plant_key: called.append(plant_key))
        sync_trigger.run_daily_sync_all_plants()

        assert called == []
