"""
apps/services/backup_restore_check.py - prove a backup restores (2026-10-01).

A backup nobody has restored is a hope, not a backup. check() takes a dump -
the newest one in the R2 backup bucket, or a local file - restores it into a
SCRATCH database on the same Postgres server, and inspects what came back:

  - pg_restore runs with --exit-on-error, so any object that fails to
    restore fails the check;
  - the restored database must hold django_migrations, and every key table
    that has rows in the live database must have rows in the restored one
    (counts are reported side by side - the dump is from the night before,
    so small differences are expected);
  - the newest migration in the dump is reported beside the live one.

Safety, because this creates and drops a database on the production server:
  - the scratch database is always "<app database>_restore_check" - never a
    name the caller picks - and is refused if it would equal the app
    database;
  - nothing is written to the app database: pg_restore and the inspection
    connect to the scratch name only, and the live side is read with
    SELECT count(*) alone;
  - the scratch database is dropped at the end, pass or fail (keep=True
    leaves it for inspection), and a leftover one from an interrupted run is
    dropped before starting.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from django.apps import apps as django_apps
from django.conf import settings
from django.db import connection

from apps.services import db_backup, object_storage

SCRATCH_SUFFIX = "_restore_check"

# Tables whose emptiness after a restore would mean real data was lost. The
# records only the app holds (MIRs, stock, files, users) come first.
KEY_MODELS = (
    "PTUser", "Mir", "MirLine", "MirMismatch", "StockLot", "StockVoucher", "Document", "PurchaseOrder",
    "PurchaseOrderLine", "Material", "HRSDomesticPurchaseOrder", "HRSMIREntry", "RTPAchhadDomesticPurchaseOrder",
    "RTPVapiDomesticPurchaseOrder", "DomesticPOCorrection", "MatchDismissal",
)


class RestoreCheckFailed(RuntimeError):
    pass


@dataclass
class RestoreReport:
    source: str
    size_bytes: int
    scratch_db: str
    tables_restored: int = 0
    dump_migration: str = ""
    live_migration: str = ""
    counts: list = field(default_factory=list)  # (table, live rows, restored rows)
    problems: list = field(default_factory=list)
    dropped: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems


def scratch_name() -> str:
    live = str(settings.DATABASES["default"]["NAME"])
    name = f"{live}{SCRATCH_SUFFIX}"
    if not live or name == live:
        raise RestoreCheckFailed("Refusing: the scratch database name would be the app's own database.")
    return name


def _admin_exec(template: str, name: str) -> None:
    """CREATE / DROP DATABASE, which cannot run inside a transaction. Runs
    on the app's own connection (autocommit), naming only the scratch
    database - checked here again so no caller can point it elsewhere."""
    from psycopg import sql

    if not name.endswith(SCRATCH_SUFFIX) or name == settings.DATABASES["default"]["NAME"]:
        raise RestoreCheckFailed(f"Refusing to create or drop {name!r}: not the scratch database.")
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL(template).format(sql.Identifier(name)))


def _scratch_connect(name: str):
    import psycopg

    db = settings.DATABASES["default"]
    kwargs = {"host": db.get("HOST") or "localhost", "port": db.get("PORT") or 5432, "dbname": name,
              "autocommit": True}
    if db.get("USER"):
        kwargs["user"] = db["USER"]
    if db.get("PASSWORD"):
        kwargs["password"] = db["PASSWORD"]
    sslmode = (db.get("OPTIONS") or {}).get("sslmode")
    if sslmode:
        kwargs["sslmode"] = sslmode
    return psycopg.connect(**kwargs)


def _restore(dump_path: str, name: str) -> None:
    env, args = db_backup._pg_env_and_args()
    args = list(args)
    args[args.index("--dbname") + 1] = name
    db_backup._run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", *args, dump_path], env)


def _count(cursor, table: str):
    cursor.execute("SELECT to_regclass(%s)", [table])
    if cursor.fetchone()[0] is None:
        return None
    from psycopg import sql

    cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
    return cursor.fetchone()[0]


def _latest_core_migration(cursor) -> str:
    cursor.execute("SELECT to_regclass('django_migrations')")
    if cursor.fetchone()[0] is None:
        return ""
    cursor.execute("SELECT name FROM django_migrations WHERE app = 'core' ORDER BY name DESC LIMIT 1")
    row = cursor.fetchone()
    return row[0] if row else ""


def _inspect(report: RestoreReport) -> None:
    tables = [django_apps.get_model("core", m)._meta.db_table for m in KEY_MODELS]
    with connection.cursor() as live:
        live_counts = {t: _count(live, t) for t in tables}
        report.live_migration = _latest_core_migration(live)
    with _scratch_connect(report.scratch_db) as conn, conn.cursor() as restored:
        restored.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        report.tables_restored = restored.fetchone()[0]
        report.dump_migration = _latest_core_migration(restored)
        for table in tables:
            got = _count(restored, table)
            report.counts.append((table, live_counts[table], got))
            if got is None and live_counts[table] is not None:
                report.problems.append(f"{table} is missing from the restored database.")
            elif live_counts[table] and not got:
                report.problems.append(f"{table} has {live_counts[table]} rows live but none in the backup.")
    if not report.dump_migration:
        report.problems.append("The restored database has no django_migrations rows - not an app database.")


def check(*, dump_file: str | None = None, key: str | None = None, keep: bool = False) -> RestoreReport:
    """Restore a dump into the scratch database and inspect it. Raises
    RestoreCheckFailed only when it cannot even start (no dump found, no
    right to create a database); a restore or content failure comes back in
    report.problems, with the scratch database still dropped."""
    name = scratch_name()
    with tempfile.TemporaryDirectory() as tmp:
        if dump_file:
            path, source = dump_file, dump_file
        else:
            object_storage.require_configured("backup")
            if key is None:
                dumps = object_storage.list_objects("backup", db_backup.PREFIX)
                if not dumps:
                    raise RestoreCheckFailed("The backup bucket holds no dumps yet.")
                key = max(dumps, key=lambda o: o.last_modified).key
            path, source = os.path.join(tmp, "restore.dump"), f"r2://backup/{key}"
            object_storage.download_file("backup", key, path)
        report = RestoreReport(source=source, size_bytes=os.path.getsize(path), scratch_db=name)

        try:
            _admin_exec("DROP DATABASE IF EXISTS {} WITH (FORCE)", name)
            _admin_exec("CREATE DATABASE {}", name)
        except RestoreCheckFailed:
            raise
        except Exception as exc:
            raise RestoreCheckFailed(f"Could not create the scratch database {name}: {exc}") from exc
        try:
            try:
                _restore(path, name)
            except db_backup.BackupFailed as exc:
                report.problems.append(f"pg_restore failed: {exc}")
            else:
                _inspect(report)
        finally:
            if not keep:
                _admin_exec("DROP DATABASE IF EXISTS {} WITH (FORCE)", name)
                report.dropped = True
    return report
