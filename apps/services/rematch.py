"""
Re-matching after a pin or correction, on the background worker (2026-09-25).

A manual MIR pin, and a correction to a field matching reads, used to run the
plant's whole run_full_match() inside the request. That scales with the data
and runs on one CPU thread, and Render's gunicorn kills a request at 30 s:
Vapi took 24.8 s on production, and past the limit the reader got an HTML 502
while the pin had already committed and the re-match had rolled back. More
CPU would only have postponed it.

So the request saves the change and calls request_rematch(), which queues ONE
run_rematch() per plant on the qcluster (django-q2's ORM broker, the worker
the hourly sync already uses). Saves made while a run is queued share it; a
save made while a run is already under way queues the next one, because that
run may have read the data before the save. The page follows the run through
sync-status's `rematch` block (status()) and reloads when it finishes -
the freshness watcher then sees apps/services/data_stamp.py's stamp move.

run_full_match() itself takes a per-plant advisory lock, so a queued run and
the hourly sync's match of the same plant never overlap.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

# A queued run that has not started within this long is treated as lost (the
# worker was down or the task was dropped), so the next save queues afresh.
_PENDING_TTL_SECONDS = 900

# A queued run the worker has not started within this long means the
# qcluster is not running: a re-match normally starts within seconds.
_STALL_SECONDS = 120

# A run still "running" after this long died with its worker (Q_CLUSTER's
# timeout is 900 s, so no live run lasts longer): it is reported failed,
# rather than the page showing "re-matching" until the next save.
_RUNNING_TTL_SECONDS = 1200

_PENDING_KEY = "pt:rematch-pending:{}"
_RESULT_KEY = "pt:rematch-result:{}"


def _match_fn(plant_key: str):
    from apps.services import matching, matching_achhad, matching_vapi

    return {"hrs": matching, "achhad": matching_achhad, "vapi": matching_vapi}[plant_key].run_full_match


def _inline() -> bool:
    """Run the re-match in the calling process - under pytest only, the same
    rule trigger_plant_sync() uses. Separate so a test can switch it off."""
    return "pytest" in sys.modules


def request_rematch(plant_key: str) -> dict:
    """Queue a re-match of `plant_key`, or join the one already queued.
    Returns status() as it stands after queuing. Under pytest the run
    happens inline, as trigger_plant_sync() does, so tests see its result."""
    queued_now = cache.add(_PENDING_KEY.format(plant_key), timezone.now().isoformat(), _PENDING_TTL_SECONDS)
    if queued_now:
        if _inline():
            run_rematch(plant_key)
        else:
            from django_q.tasks import async_task

            async_task("apps.services.rematch.run_rematch", plant_key)
    return status(plant_key)


def run_rematch(plant_key: str) -> None:
    """The queued task. Clears the pending flag FIRST, so a save arriving
    while this runs queues another run rather than being folded into one
    that may already have read the old data."""
    cache.delete(_PENDING_KEY.format(plant_key))
    started = timezone.now().isoformat()
    cache.set(_RESULT_KEY.format(plant_key), {"state": "running", "startedAt": started}, None)
    try:
        result = _match_fn(plant_key)()
    except Exception as exc:
        log.exception("rematch: run_full_match failed for plant=%s", plant_key)
        cache.set(_RESULT_KEY.format(plant_key), {
            "state": "failed", "startedAt": started, "finishedAt": timezone.now().isoformat(),
            "error": str(exc)[:300],
        }, None)
        return
    cache.set(_RESULT_KEY.format(plant_key), {
        "state": "done", "startedAt": started, "finishedAt": timezone.now().isoformat(),
        "unfilledPins": result.get("manual_pins_unfilled", []),
        "stalePins": result.get("manual_pins_stale", []),
        "manualPinsApplied": result.get("manual_pins_applied"),
        "unfilledEdits": result.get("manual_edits_unfilled", []),
        "staleEdits": result.get("manual_edits_stale", []),
    }, None)


def status(plant_key: str) -> dict:
    """{"queued": bool, "state": "idle"|"running"|"done"|"failed",
    "queuedAt", "stalled", ...}: whether a re-match is waiting, whether the
    worker has left it waiting past _STALL_SECONDS (the qcluster is down, so
    the page stops waiting and says so), and the last run's outcome with the
    pins it could not apply - for sync-status and the save responses."""
    last = cache.get(_RESULT_KEY.format(plant_key)) or {"state": "idle"}
    if last.get("state") == "running":
        try:
            ran = (timezone.now() - datetime.fromisoformat(last["startedAt"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            ran = 0
        if ran > _RUNNING_TTL_SECONDS:
            last = {**last, "state": "failed",
                    "error": "The re-match stopped without finishing (the worker restarted). Save again to re-run it."}
    queued_at = cache.get(_PENDING_KEY.format(plant_key))
    stalled = False
    if queued_at:
        try:
            waited = (timezone.now() - datetime.fromisoformat(queued_at)).total_seconds()
            stalled = waited > _STALL_SECONDS
        except (TypeError, ValueError):
            stalled = False
    return {**last, "queued": bool(queued_at), "queuedAt": queued_at or None, "stalled": stalled}
