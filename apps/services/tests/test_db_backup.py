"""
apps/services/db_backup.py - the nightly backup. The dump itself runs the
real pg_dump and pg_restore against the test database; storage is a
recording fake, so what is uploaded and what is pruned are both asserted.
"""

import datetime
import shutil

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.services import db_backup, object_storage
from apps.services.object_storage import StoredObject

_NOW = datetime.datetime(2026, 9, 30, 20, 43, tzinfo=datetime.UTC)  # 02:13 IST on 1 Oct


def _dump(key, days_old):
    return StoredObject(key=key, size=1, last_modified=_NOW - datetime.timedelta(days=days_old))


class TestPruning:
    def test_deletes_only_dumps_past_the_retention_window(self):
        objects = [_dump(f"postgres/d{i}.dump", i) for i in range(40)]
        pruned = db_backup.keys_to_prune(objects, retention_days=30, now=_NOW)
        assert pruned == [f"postgres/d{i}.dump" for i in range(31, 40)]

    def test_never_deletes_the_newest_seven_however_old(self):
        """A schedule that stalled for months must not empty the bucket."""
        objects = [_dump(f"postgres/old{i}.dump", 200 + i) for i in range(10)]
        pruned = db_backup.keys_to_prune(objects, retention_days=30, now=_NOW)
        assert sorted(pruned) == sorted(f"postgres/old{i}.dump" for i in range(7, 10))

    def test_ignores_objects_outside_the_backup_prefix(self):
        objects = [_dump("manual/keep-me.dump", 400)] + [_dump(f"postgres/d{i}.dump", i) for i in range(8)]
        assert db_backup.keys_to_prune(objects, retention_days=1, now=_NOW) == ["postgres/d7.dump"]


def test_backup_key_is_dated_in_ist(settings):
    key = db_backup.backup_key(_NOW)
    assert key.startswith("postgres/2026/10/")
    assert key.endswith("-20261001-021300.dump")


class FakeStorage:
    def __init__(self, existing=()):
        self.uploaded = []
        self.deleted = []
        self.existing = list(existing)

    def install(self, monkeypatch):
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "upload_file", self.upload_file)
        monkeypatch.setattr(object_storage, "list_objects", lambda kind, prefix="": list(self.existing))
        monkeypatch.setattr(object_storage, "delete_objects", lambda kind, keys: self.deleted.extend(keys))

    def upload_file(self, kind, key, path, content_type="application/octet-stream"):
        with open(path, "rb") as fh:
            self.uploaded.append((kind, key, fh.read()))
        self.existing.append(StoredObject(key=key, size=0, last_modified=timezone.now()))


@pytest.mark.skipif(not shutil.which("pg_dump"), reason="needs the PostgreSQL client tools")
@pytest.mark.django_db
def test_run_backup_uploads_a_real_restorable_dump_then_prunes(monkeypatch, settings):
    settings.BACKUP_RETENTION_DAYS = 30
    old = [StoredObject(key=f"postgres/2026/01/old{i}.dump", size=1,
                        last_modified=timezone.now() - datetime.timedelta(days=100 + i)) for i in range(8)]
    storage = FakeStorage(existing=old)
    storage.install(monkeypatch)

    result = db_backup.run_backup()

    [(kind, key, body)] = storage.uploaded
    assert kind == "backup"
    assert key == result.key and key.startswith("postgres/")
    assert body.startswith(b"PGDMP")  # pg_dump custom-format magic
    assert result.size_bytes == len(body)
    # 9 dumps now (8 old + today's); the newest 7 are kept whatever their age.
    assert sorted(storage.deleted) == ["postgres/2026/01/old6.dump", "postgres/2026/01/old7.dump"]


@pytest.mark.django_db
def test_a_failed_dump_uploads_nothing_and_prunes_nothing(monkeypatch):
    storage = FakeStorage(existing=[_dump(f"postgres/d{i}.dump", 300) for i in range(10)])
    storage.install(monkeypatch)

    def failing(cmd, env):
        raise db_backup.BackupFailed("pg_dump failed (exit 1): connection refused")

    monkeypatch.setattr(db_backup, "_run", failing)
    with pytest.raises(db_backup.BackupFailed):
        db_backup.run_backup()
    assert storage.uploaded == [] and storage.deleted == []


@pytest.mark.django_db
def test_a_dump_that_lists_no_table_data_is_not_uploaded(monkeypatch):
    storage = FakeStorage()
    storage.install(monkeypatch)

    class Done:
        stdout = ";\n; Archive created at ...\n"

    monkeypatch.setattr(db_backup, "_run", lambda cmd, env: Done())
    with pytest.raises(db_backup.BackupFailed, match="no table data"):
        db_backup.run_backup()
    assert storage.uploaded == []


def test_a_missing_pg_dump_is_named(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(db_backup.BackupFailed, match="pg_dump is not installed"):
        db_backup._run(["pg_dump", "--version"], {"PATH": "/nonexistent"})


def test_the_password_goes_in_the_environment_not_the_command_line(settings, monkeypatch):
    monkeypatch.setitem(settings.DATABASES["default"], "PASSWORD", "s3cret")
    monkeypatch.setitem(settings.DATABASES["default"], "OPTIONS", {"sslmode": "require"})
    env, args = db_backup._pg_env_and_args()
    assert env["PGPASSWORD"] == "s3cret"
    assert env["PGSSLMODE"] == "require"
    assert "s3cret" not in " ".join(args)


@pytest.mark.django_db
def test_command_turns_a_missing_setup_into_a_clear_error(settings):
    settings.R2_ACCOUNT_ID = ""
    settings.R2_BUCKETS = {"po": "", "invoice": "", "backup": ""}
    with pytest.raises(CommandError, match="R2_BUCKET_BACKUPS"):
        call_command("backup_database")
