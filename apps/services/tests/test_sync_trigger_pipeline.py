"""
Tests for apps/services/sync_trigger.py's _run_pipeline() step gating: a
consumption rebuild is skipped, and recorded as a FAILED SyncRun saying why,
when that plant's stock sync failed. `call_command` is stubbed - the real
commands reach Google Drive (see test_sync_trigger_daily.py's docstring).
"""

import pytest

from apps.core.models import SyncRun
from apps.services import security_alerts, sync_trigger


def _run(monkeypatch, plant_key, failing):
    ran = []

    def _fake_call_command(name):
        ran.append(name)
        if name in failing:
            raise SystemExit(1)

    monkeypatch.setattr(sync_trigger, "call_command", _fake_call_command)
    monkeypatch.setattr(security_alerts, "notify_admins_sync_failure", lambda *a, **k: None)
    sync_trigger._run_pipeline(plant_key)
    return ran


@pytest.mark.django_db
class TestPipelineStepGating:
    @pytest.mark.parametrize("plant_key,stock_cmd,consumption_cmd,plant", [
        ("hrs", "sync_stock", "compute_hrs_consumption", SyncRun.Plant.HRS),
        ("achhad", "sync_achhad_stock", "compute_achhad_consumption", SyncRun.Plant.RTP_ACHHAD),
        ("vapi", "sync_vapi_stock", "compute_vapi_consumption", SyncRun.Plant.RTP_VAPI),
    ])
    def test_a_failed_stock_sync_skips_consumption_and_says_so(self, monkeypatch, plant_key, stock_cmd, consumption_cmd, plant):
        ran = _run(monkeypatch, plant_key, failing={stock_cmd})

        assert consumption_cmd not in ran
        # Matching still runs on whatever is in the DB.
        assert ran[-1] == f"match_{plant_key}"
        run = SyncRun.objects.get(plant=plant, source=SyncRun.Source.CONSUMPTION)
        assert run.status == SyncRun.Status.FAILED
        assert stock_cmd in run.error_detail

    def test_a_failed_po_sync_does_not_skip_anything(self, monkeypatch):
        ran = _run(monkeypatch, "hrs", failing={"sync_po_csv"})

        assert ran == sync_trigger._PLANT_COMMANDS["hrs"]
        assert not SyncRun.objects.filter(source=SyncRun.Source.CONSUMPTION).exists()

    def test_the_lock_is_released_after_a_skip(self, monkeypatch):
        _run(monkeypatch, "hrs", failing={"sync_stock"})

        assert sync_trigger.is_sync_in_progress("hrs") is False
