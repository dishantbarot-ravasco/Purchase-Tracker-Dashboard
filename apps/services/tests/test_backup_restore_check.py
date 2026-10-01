"""
apps/services/backup_restore_check.py - a backup is proven by restoring it.
The round trips run the real pg_dump and pg_restore against the test
database (skipped where the client tools are missing; CI has them) and a
real scratch database, which every test checks is gone afterwards.
"""

import datetime
import shutil

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.utils import timezone

from apps.api.tests.factories import make_user
from apps.core.models import DomesticPOCorrection, PTUser
from apps.services import backup_restore_check as brc
from apps.services import db_backup, object_storage
from apps.services.object_storage import StoredObject

needs_pg_tools = pytest.mark.skipif(not (shutil.which("pg_dump") and shutil.which("pg_restore")),
                                    reason="needs the PostgreSQL client tools")


def _scratch_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [brc.scratch_name()])
        return cursor.fetchone() is not None


@pytest.fixture
def dump(tmp_path):
    def _make():
        path = str(tmp_path / "db.dump")
        db_backup.dump_to(path)
        return path
    return _make


@needs_pg_tools
@pytest.mark.django_db(transaction=True)
class TestRoundTrip:
    def test_a_real_dump_restores_and_matches_the_live_counts(self, dump):
        make_user(email="a@ravasco.com")
        make_user(email="b@ravasco.com")
        report = brc.check(dump_file=dump())

        assert report.ok, report.problems
        assert report.dropped and not _scratch_exists()
        assert report.tables_restored > 50
        assert report.dump_migration == report.live_migration != ""
        assert dict((t, (live, got)) for t, live, got in report.counts)["pt_users"] == (2, 2)
        assert PTUser.objects.count() == 2  # the live database is untouched

    def test_rows_missing_from_the_backup_fail_the_check(self, dump):
        path = dump()  # taken while the table is empty
        user = make_user(email="a@ravasco.com")
        DomesticPOCorrection.objects.create(po_number="1", field_name="vendor_name", old_value="a", new_value="b",
                                            corrected_by=user)

        report = brc.check(dump_file=path)
        assert not report.ok
        assert any("core_domesticpocorrection has 1 rows live but none" in p for p in report.problems)
        assert not _scratch_exists()

    def test_a_corrupt_dump_fails_and_still_drops_the_scratch_database(self, tmp_path):
        bad = tmp_path / "bad.dump"
        bad.write_bytes(b"PGDMP this is not really a dump")
        report = brc.check(dump_file=str(bad))
        assert not report.ok and report.problems[0].startswith("pg_restore failed")
        assert report.dropped and not _scratch_exists()

    def test_keep_leaves_the_scratch_database_for_inspection(self, dump):
        report = brc.check(dump_file=dump(), keep=True)
        try:
            assert report.ok and not report.dropped and _scratch_exists()
        finally:
            brc._admin_exec("DROP DATABASE IF EXISTS {} WITH (FORCE)", brc.scratch_name())

    def test_the_command_reports_a_pass(self, dump, capsys):
        make_user(email="a@ravasco.com")
        call_command("verify_backup", file=dump())
        out = capsys.readouterr().out
        assert "Restore check passed" in out and "pt_users" in out

    def test_the_newest_r2_dump_is_the_one_checked(self, dump, monkeypatch):
        path = dump()
        downloaded = []
        now = timezone.now()
        older = StoredObject(key="postgres/2026/09/old.dump", size=1, last_modified=now - datetime.timedelta(days=2))
        newer = StoredObject(key="postgres/2026/10/new.dump", size=1, last_modified=now - datetime.timedelta(days=1))
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "list_objects", lambda kind, prefix="": [older, newer])

        def fake_download(kind, key, target):
            downloaded.append((kind, key))
            shutil.copyfile(path, target)

        monkeypatch.setattr(object_storage, "download_file", fake_download)
        report = brc.check()
        assert downloaded == [("backup", "postgres/2026/10/new.dump")]
        assert report.source == "r2://backup/postgres/2026/10/new.dump" and report.ok


class TestSafety:
    def test_create_and_drop_refuse_anything_but_the_scratch_database(self, settings):
        with pytest.raises(brc.RestoreCheckFailed):
            brc._admin_exec("DROP DATABASE IF EXISTS {}", settings.DATABASES["default"]["NAME"])
        with pytest.raises(brc.RestoreCheckFailed):
            brc._admin_exec("DROP DATABASE IF EXISTS {}", "postgres")

    def test_the_scratch_name_is_the_app_database_plus_a_suffix(self, settings):
        assert brc.scratch_name() == settings.DATABASES["default"]["NAME"] + "_restore_check"

    @pytest.mark.django_db
    def test_an_empty_bucket_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "list_objects", lambda kind, prefix="": [])
        with pytest.raises(CommandError, match="holds no dumps yet"):
            call_command("verify_backup")
