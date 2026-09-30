"""
apps/core/management/commands/backup_database.py - one database backup to
the Cloudflare R2 backup bucket, now (2026-09-30).

The same work the `nightly-db-backup` schedule runs every night
(apps/services/db_backup.py). Run it by hand after setting up R2, to see a
first backup land, or before a risky migration.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.services import db_backup, object_storage


class Command(BaseCommand):
    help = "pg_dump the database and upload it to the R2 backup bucket, pruning old dumps."

    def handle(self, *args, **options):
        try:
            result = db_backup.run_backup()
        except (db_backup.BackupFailed, object_storage.StorageNotConfigured) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Backup uploaded: {result.key} ({result.size_bytes:,} bytes). Pruned {len(result.pruned)} old dump(s)."
        ))
