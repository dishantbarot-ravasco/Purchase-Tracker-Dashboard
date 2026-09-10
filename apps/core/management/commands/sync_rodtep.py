"""
apps/core/management/commands/sync_rodtep.py — syncs every RoDTEP scrip
ledger file from the "Purchase Orders HO/RODTEP SCRIPT LICENSE" Drive folder
into RodtepScrollEntry.

Genuinely different shape from every other sync command in this app: there
is no single fixed file to fetch (settings.RODTEP_FOLDER_ID names a FOLDER,
not a file title) - the folder holds one file per Script Number
("RODTEP-JNPT-<N>.xlsx"), a real, growing count with no fixed final name.
This command lists the whole folder (apps/services/google_client.py's
list_files_in_folder(), added for this) and parses every file found, rather
than find_file_id_by_title()'s single-file lookup every other sync_*
command uses.

Change detection reuses apps.services.sync_utils.unchanged() (same
quantized-Decimal reasoning as every other sync command - see CLAUDE.md's
"Change-detection must compare quantized Decimal values"), keyed on
(script_no, sb_number) - see RodtepScrollEntry's own docstring for why that
pair, not sr_no, is the real identity.

No deactivation step (unlike sync_stock.py/sync_mir.py's is_active
handling) - this is a financial ledger, not a current-state snapshot; a
script/SB row that stops appearing in a re-download (e.g. a file briefly
unavailable) should not be treated as "no longer real" the way a vanished
stock lot is. If a file is genuinely deleted from Drive, its rows simply
stop being touched by future syncs - they are not deleted here either.

Usage:
    python manage.py sync_rodtep
    python manage.py sync_rodtep --file path.xlsx   # parse ONE local file instead of listing Drive (dev/testing)
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import RodtepScrollEntry, SyncRun
from apps.services.parsers.rodtep import HeaderMismatch, parse_rodtep_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "script_date", "sb_date", "scroll_number", "scroll_date", "scroll_type",
    "location", "sanctioned_amount",
]


class Command(BaseCommand):
    """Sync every RoDTEP scrip ledger file on Drive (or one local --file)
    into RodtepScrollEntry, and record the outcome as a SyncRun row.

    Idempotent: rows unchanged per sync_utils.unchanged() are skipped. Safe
    to re-run any time.
    """

    help = "Sync every RoDTEP scrip ledger xlsx from Drive into RodtepScrollEntry."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a single local xlsx file instead of listing the Drive folder.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        files_processed = 0
        files_failed = []  # [(file_label, error_message), ...]
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            files = list(self._iter_files(options.get("file")))
            for file_label, file_bytes in files:
                # Real bug, found and fixed 2026-09-10 (project owner
                # reported "the rodtep script sync is not working" right
                # after adding a new file to the RoDTEP Drive folder): one
                # file failing HeaderMismatch (e.g. a header row shifted
                # further down than _MAX_HEADER_SCAN_ROW tolerates, or a
                # genuinely different layout) used to raise straight out of
                # this loop, aborting the ENTIRE sync - every other,
                # already-working script file silently stopped updating
                # too, with no indication which file was actually the
                # problem. Each file is now isolated: a bad file is
                # skipped (with its own name and error recorded) and every
                # other file still syncs normally, same "one bad row/file
                # shouldn't sink the whole run" principle sync_stock.py's
                # own rows_skipped/PARTIAL handling already follows.
                try:
                    entries = parse_rodtep_xlsx(file_bytes)
                except HeaderMismatch as exc:
                    files_failed.append((file_label, str(exc)))
                    self.stderr.write(self.style.ERROR(f"sync_rodtep: {file_label}: header mismatch - {exc}"))
                    continue
                rows_seen += len(entries)
                files_processed += 1
                for parsed in entries:
                    if self._upsert_entry(parsed, file_label):
                        rows_changed += 1

            if files_failed:
                failed_names = ", ".join(name for name, _ in files_failed)
                status = SyncRun.Status.PARTIAL if files_processed else SyncRun.Status.FAILED
                error_detail = (
                    f"{len(files_failed)} file(s) skipped (header mismatch): {failed_names}. "
                    + "; ".join(f"{name}: {msg}" for name, msg in files_failed)
                )

            self.stdout.write(self.style.SUCCESS(
                f"sync_rodtep: {files_processed}/{len(files)} file(s) synced, {rows_seen} rows seen, "
                f"{rows_changed} created/updated"
                + (f", {len(files_failed)} file(s) skipped" if files_failed else "")
                + f" ({time.monotonic() - t0:.1f}s)"
            ))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_rodtep: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.COMPANY,
                source=SyncRun.Source.RODTEP,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)

    def _iter_files(self, local_path: str | None):
        """Yields (label, file_bytes) pairs - one local file if --file was
        given, otherwise every file currently in settings.RODTEP_FOLDER_ID."""
        if local_path:
            with open(local_path, "rb") as f:
                yield local_path, f.read()
            return

        from apps.services.google_client import download_file_bytes, list_files_in_folder
        files = list_files_in_folder(settings.RODTEP_FOLDER_ID, name_prefix="RODTEP")
        for f in files:
            yield f["name"], download_file_bytes(f["id"])

    def _upsert_entry(self, parsed, source_file_name: str) -> bool:
        """Upsert one parsed RoDTEP row by (script_no, sb_number); returns
        False (no-op) when the row is unchanged."""
        existing = RodtepScrollEntry.objects.filter(
            script_no=parsed.script_no, sb_number=parsed.sb_number,
        ).first()
        if existing and unchanged(RodtepScrollEntry, existing, parsed, _FIELDS):
            return False

        RodtepScrollEntry.objects.update_or_create(
            script_no=parsed.script_no,
            sb_number=parsed.sb_number,
            defaults={f: getattr(parsed, f) for f in _FIELDS}
            | {"source_file_name": source_file_name, "source_row_ref": parsed.source_row_ref, "last_synced_at": timezone.now()},
        )
        return True
