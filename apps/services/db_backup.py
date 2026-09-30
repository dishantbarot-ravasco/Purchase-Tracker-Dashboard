"""
apps/services/db_backup.py - nightly database backup to Cloudflare R2 (2026-09-30).

Why: the app now holds records nothing else has (MIRs posted in the app, RM
stock vouchers, and soon uploaded POs and invoices). The Drive mirrors can be
re-synced; those cannot. Render keeps its own point-in-time recovery, but
that copy lives on Render with the database. This one is off Render, in the
R2 backup bucket, in a format any Postgres can restore.

What run_backup() does, in order:
  1. pg_dump the database in custom format (-Fc: compressed, restorable
     table by table with pg_restore), without owner or grants, so it
     restores into any database, not only this Render instance.
  2. Check the file reads back (pg_restore --list). A dump that cannot be
     listed is not uploaded.
  3. Upload it as postgres/YYYY/MM/purchase_tracker-YYYYMMDD-HHMMSS.dump
     (IST).
  4. Only then, delete dumps older than BACKUP_RETENTION_DAYS - never the
     newest MIN_KEEP, whatever their age, so a stalled schedule cannot
     empty the bucket.

Any failure raises: the django-q task shows as failed and Sentry reports it.
pg_dump must be the same major version as the server or newer; the
Dockerfile installs a current client from the PostgreSQL apt repository.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.services import object_storage

logger = logging.getLogger(__name__)

PREFIX = "postgres/"
MIN_KEEP = 7
_TIMEOUT_SECONDS = 600


class BackupFailed(RuntimeError):
    pass


@dataclass
class BackupResult:
    key: str
    size_bytes: int
    pruned: list


def _pg_env_and_args() -> tuple[dict, list[str]]:
    """Connection details from Django's own settings, so the dump always
    reads the database the app uses. The password travels in PGPASSWORD,
    never on the command line."""
    db = settings.DATABASES["default"]
    env = dict(os.environ)
    if db.get("PASSWORD"):
        env["PGPASSWORD"] = str(db["PASSWORD"])
    sslmode = (db.get("OPTIONS") or {}).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode
    args = ["--host", str(db.get("HOST") or "localhost"), "--port", str(db.get("PORT") or 5432),
            "--username", str(db.get("USER") or ""), "--dbname", str(db["NAME"])]
    return env, args


def _run(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise BackupFailed(f"{cmd[0]} is not installed in this environment.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupFailed(f"{cmd[0]} did not finish within {_TIMEOUT_SECONDS} seconds.") from exc
    if done.returncode != 0:
        raise BackupFailed(f"{cmd[0]} failed (exit {done.returncode}): {done.stderr.strip()[:2000]}")
    return done


def dump_to(path: str) -> None:
    """Write a verified custom-format dump of the app's database to `path`."""
    env, conn_args = _pg_env_and_args()
    _run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", path, *conn_args], env)
    listing = _run(["pg_restore", "--list", path], env)
    if "TABLE DATA" not in listing.stdout:
        raise BackupFailed("The dump was written but lists no table data; not uploading it.")


def backup_key(now=None) -> str:
    local = timezone.localtime(now or timezone.now())
    return f"{PREFIX}{local:%Y/%m}/{connection.settings_dict['NAME']}-{local:%Y%m%d-%H%M%S}.dump"


def keys_to_prune(objects: list, retention_days: int, now=None) -> list[str]:
    """Dumps older than the retention window, sparing the newest MIN_KEEP."""
    cutoff = (now or timezone.now()) - timedelta(days=retention_days)
    newest_first = sorted((o for o in objects if o.key.startswith(PREFIX)),
                          key=lambda o: o.last_modified, reverse=True)
    return [o.key for o in newest_first[MIN_KEEP:] if o.last_modified < cutoff]


def run_backup() -> BackupResult:
    # Fails before pg_dump runs when R2 is not set up, naming what is missing.
    object_storage.require_configured("backup")
    key = backup_key()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "backup.dump")
        dump_to(path)
        size = os.path.getsize(path)
        object_storage.upload_file("backup", key, path)
    pruned = keys_to_prune(object_storage.list_objects("backup", PREFIX), settings.BACKUP_RETENTION_DAYS)
    if pruned:
        object_storage.delete_objects("backup", pruned)
    logger.info("Database backup uploaded: %s (%d bytes); pruned %d old dump(s)", key, size, len(pruned))
    return BackupResult(key=key, size_bytes=size, pruned=pruned)


def scheduled_backup() -> str:
    """Entry point for the django-q schedule (ensure_schedules)."""
    result = run_backup()
    return f"{result.key} ({result.size_bytes} bytes), pruned {len(result.pruned)}"
