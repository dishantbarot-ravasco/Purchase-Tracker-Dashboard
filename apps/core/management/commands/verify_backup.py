"""
apps/core/management/commands/verify_backup.py - restore a backup into a
scratch database and check it (2026-10-01). See
apps/services/backup_restore_check.py for what is checked and why it is safe
to run against production.

    python manage.py verify_backup                 # the newest dump in R2
    python manage.py verify_backup --key postgres/2026/10/x.dump
    python manage.py verify_backup --file /tmp/x.dump
    python manage.py verify_backup --keep          # leave the scratch database
"""

from django.core.management.base import BaseCommand, CommandError

from apps.services import backup_restore_check as brc
from apps.services import object_storage


class Command(BaseCommand):
    help = "Restore a database backup into a scratch database, check its contents, then drop it."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="A local dump instead of the newest one in R2.")
        parser.add_argument("--key", help="A specific R2 object key under postgres/.")
        parser.add_argument("--keep", action="store_true", help="Do not drop the scratch database afterwards.")

    def handle(self, *args, **options):
        try:
            report = brc.check(dump_file=options["file"], key=options["key"], keep=options["keep"])
        except (brc.RestoreCheckFailed, object_storage.StorageNotConfigured) as exc:
            raise CommandError(str(exc)) from exc
        out = self.stdout
        out.write(f"Backup:   {report.source} ({report.size_bytes:,} bytes)")
        out.write(f"Restored: {report.tables_restored} tables into {report.scratch_db}"
                  + (" (dropped afterwards)" if report.dropped else " (kept)"))
        out.write(f"Newest migration - backup: {report.dump_migration or '-'}   live: {report.live_migration or '-'}")
        out.write(f"{'table':40} {'live rows':>10} {'backup rows':>12}")
        for table, live, got in report.counts:
            live_text = "-" if live is None else str(live)
            got_text = "missing" if got is None else str(got)
            out.write(f"{table:40} {live_text:>10} {got_text:>12}")
        if not report.ok:
            raise CommandError("RESTORE CHECK FAILED:\n  " + "\n  ".join(report.problems))
        out.write(self.style.SUCCESS("Restore check passed: the backup restores and holds the app's data."))
