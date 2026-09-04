"""
apps/services/sync_trigger.py — background task runner for the dashboard's
"Refresh Data" button, wired 2026-09-04 so it triggers a real Google Drive
sync instead of only re-reading whatever Postgres already has.

Runs a plant's full sync+match pipeline (sync_po_csv -> sync_mir ->
sync_stock -> match_<plant>, the same order CLAUDE.md's own Commands section
documents) via manage.py's own management commands, using call_command()
rather than reimplementing anything.

Backgrounding uses django-q2's `async_task()` (ORM broker - queued tasks are
Postgres rows via this app's own `default` DB connection, no Redis/RabbitMQ
needed, config/settings.py's Q_CLUSTER), executed by a separate `manage.py
qcluster` worker process, not a thread inside the gunicorn request-handling
process. This replaced an earlier `threading.Thread(daemon=False)` version
same-day (2026-09-04) after real production logs showed exactly the failure
mode that approach couldn't survive: gunicorn's default worker timeout is
30s and render.yaml sets no `--timeout` override, and a worker recycle
(SIGKILL after a missed heartbeat) killed the sync thread mid-pipeline with
no automatic resume - only the lock's timeout (_LOCK_TIMEOUT_SECONDS)
self-healing so a *future* trigger wasn't blocked forever, not the run
itself. A qcluster worker is a wholly separate OS process from the web
workers gunicorn manages, so it isn't touched by a web worker recycling.
See render.yaml for the second `worker`-type service this requires running
alongside the web service - `qcluster` must actually be running for
`trigger_plant_sync()`/`trigger_plant_imports_sync()` to do anything; a
queued task with no worker consuming it just sits in the `django_q_ormq`
table forever (self-heals via the lock timeout the same as a crashed run
would, but nothing will have actually synced).

The `'pytest' in sys.modules` check (this app's test runner is pytest, not
`manage.py test` - see config/settings.py's CACHES block for the same check
applied to a different problem) still runs the pipeline function directly,
synchronously, in-process - there's no qcluster worker running under
pytest, and Q_CLUSTER's `sync` option is deliberately not used to get
django-q2's own "run inline" mode instead, so this app's own two pipeline
functions stay the single code path for "run this pipeline right now."

In-progress tracking (separate from django-q2's own task-status tracking)
still uses Django's DB-backed cache (`cache.add()`/`.get()`/`.delete()`,
config/settings.py's CACHES -> pt_cache_table) - this app runs multiple
gunicorn worker processes (render.yaml: `--workers 2`) *and* now a qcluster
worker process too, so an in-process flag would only be visible to whichever
process handled a given call; the cache is shared Postgres state, so every
process agrees. `add()` (not `get()` then `set()`) is used for the actual
lock acquisition specifically because it's atomic - a plain get-then-set has
a race window two near-simultaneous trigger requests could both slip
through. This lock is what actually prevents a duplicate sync from being
queued twice, not django-q2's own dedup (django-q2 has none).

Known gap, accepted for this pass - flag if it needs to change:
- match_<plant> commands write no SyncRun row of their own and have no
  internal error handling (unlike the sync_* commands, which always record
  a SyncRun even on failure). A match failure here is caught and logged via
  Python logging (logs/app.log), but there is nothing in `SyncRun` itself
  to show a match step failed - only that the syncs before it succeeded.
"""
from __future__ import annotations

import logging
import sys

from django.core.cache import cache
from django.core.management import call_command
from django_q.tasks import async_task

log = logging.getLogger(__name__)

# Safety expiry on the in-progress lock - self-heals if a worker dies
# mid-sync without ever reaching the `finally` that clears it. Comfortably
# above how long a real full sync+match pipeline should ever take; if one
# genuinely runs longer, a second trigger being allowed in in is an
# acceptable tradeoff against the lock wedging forever on a dead worker.
_LOCK_TIMEOUT_SECONDS = 900  # 15 min

# Per-plant command pipeline, in the order CLAUDE.md's "Commands" section
# documents (each plant's own 3 sync commands, then its match command).
_PLANT_COMMANDS = {
    "hrs": ["sync_po_csv", "sync_mir", "sync_stock", "match_hrs"],
    "achhad": ["sync_achhad_po_csv", "sync_achhad_mir", "sync_achhad_stock", "match_achhad"],
    "vapi": ["sync_vapi_po_csv", "sync_vapi_mir", "sync_vapi_stock", "match_vapi"],
}

# Import POs match against the SAME MIR/Stock data the domestic pipeline
# above already syncs (MIR/Stock are shared Drive files across domestic and
# import purchases for a plant - only the PO-source CSV differs, confirmed
# 2026-09-04; see matching.py's match_import_po_mir_line_item() docstring).
# So this pipeline is just the plant's own imports CSV sync, then that same
# plant's match_<plant> command again - run_full_match() covers both
# domestic AND import PO line items in one pass, idempotently, so re-running
# it here doesn't duplicate or disturb whatever the domestic pipeline above
# already matched.
_IMPORT_PLANT_COMMANDS = {
    "hrs": ["sync_hrs_imports_po_csv", "match_hrs"],
    "achhad": ["sync_achhad_imports_po_csv", "match_achhad"],
    "vapi": ["sync_vapi_imports_po_csv", "match_vapi"],
}


# ── Cache lock keys ──────────────────────────────────────────────────────────

def _lock_key(plant_key: str) -> str:
    return f"sync_trigger_in_progress:{plant_key}"


def _imports_lock_key(plant_key: str) -> str:
    # Separate namespace from _lock_key - an Import PO sync and a domestic
    # sync for the same plant are independent pipelines and shouldn't block
    # each other.
    return f"sync_trigger_imports_in_progress:{plant_key}"


def is_sync_in_progress(plant_key: str) -> bool:
    return bool(cache.get(_lock_key(plant_key)))


# ── Domestic sync+match pipeline ─────────────────────────────────────────────

def _run_pipeline(plant_key: str) -> None:
    try:
        for cmd_name in _PLANT_COMMANDS[plant_key]:
            try:
                call_command(cmd_name)
            except SystemExit:
                # sync_* commands raise SystemExit(1) on failure, but only
                # after already recording a failed SyncRun row themselves
                # (see each command's own `finally` block) - logged here
                # just so it's visible in this trigger's own trace too.
                log.error("sync_trigger: %s exited with failure for plant=%s", cmd_name, plant_key)
            except Exception:
                log.exception("sync_trigger: %s raised an unexpected error for plant=%s", cmd_name, plant_key)
    finally:
        cache.delete(_lock_key(plant_key))


def trigger_plant_sync(plant_key: str) -> bool:
    """Starts plant_key's sync+match pipeline on a background thread if one
    isn't already running for that plant. Returns True if a new run was
    started, False if one was already in progress for this plant (the
    caller should treat that as "already syncing", not an error - see the
    sync-trigger views' 409 handling)."""
    if not cache.add(_lock_key(plant_key), True, timeout=_LOCK_TIMEOUT_SECONDS):
        return False

    if "pytest" in sys.modules:
        _run_pipeline(plant_key)
    else:
        async_task(_run_pipeline, plant_key)
    return True


# ── Import PO sync+match pipeline ────────────────────────────────────────────

def is_imports_sync_in_progress(plant_key: str) -> bool:
    return bool(cache.get(_imports_lock_key(plant_key)))


def _run_imports_pipeline(plant_key: str) -> None:
    try:
        for cmd_name in _IMPORT_PLANT_COMMANDS[plant_key]:
            try:
                call_command(cmd_name)
            except SystemExit:
                log.error("sync_trigger: %s exited with failure for plant=%s (imports)", cmd_name, plant_key)
            except Exception:
                log.exception("sync_trigger: %s raised an unexpected error for plant=%s (imports)", cmd_name, plant_key)
    finally:
        cache.delete(_imports_lock_key(plant_key))


def trigger_plant_imports_sync(plant_key: str) -> bool:
    """Same shape as trigger_plant_sync(), for the Import PO pipeline."""
    if not cache.add(_imports_lock_key(plant_key), True, timeout=_LOCK_TIMEOUT_SECONDS):
        return False

    if "pytest" in sys.modules:
        _run_imports_pipeline(plant_key)
    else:
        async_task(_run_imports_pipeline, plant_key)
    return True
