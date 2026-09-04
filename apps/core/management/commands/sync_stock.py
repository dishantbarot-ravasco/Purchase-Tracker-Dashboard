"""
apps/core/management/commands/sync_stock.py — syncs HRS RAW MATERIAL
STOCK.xlsx ('Stock' sheet) from Drive into HRSStockLot, keyed by
source_row_ref (the sheet row number), and captures today's
HRSStockSnapshot for every lot synced - this is the daily-history mechanism
that replaces Drive's dated whole-file copies.

source_row_ref is a sheet ROW NUMBER, not a stable business key - see
sync_mir.py's module docstring for the full row-shift reasoning. A lot whose
source_row_ref no longer appears in the freshly parsed file is deactivated
(is_active=False), not deleted, so its HRSStockSnapshot history survives
(stock_lot's FK is on_delete=CASCADE) and it stops appearing as a matching
candidate - see HRSStockLot.is_active's help_text.

Change detection reuses sync_utils.unchanged() (same quantized-Decimal
reasoning as sync_mir.py). The snapshot itself is a separate update_or_create
keyed on (stock_lot, snapshot_date) - re-running --no-snapshot the same day
after a fix won't create a second, conflicting snapshot row.

Drive folder: settings.HRS_MIR_STOCK_FOLDER_ID - the same per-plant MIR/
Stock folder sync_mir.py reads from, not the shared PO-master folder.

Usage:
    python manage.py sync_stock
    python manage.py sync_stock --file path.xlsx    # parse a local file instead (dev/testing)
    python manage.py sync_stock --no-snapshot        # skip snapshot capture (e.g. re-running same day for a fix)
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import HRSStockLot, HRSStockSnapshot, SyncRun
from apps.services.parsers.stock import HeaderMismatch, parse_stock_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "sr_no", "description", "sap_item_code", "category", "sub_category", "uom",
    "opening_stock", "received", "issued", "todays_stock", "basic_rate", "value",
    "received_date", "no_of_days", "party_name", "location_tag",
]
_SNAPSHOT_FIELDS = ["opening_stock", "received", "issued", "todays_stock", "basic_rate", "value"]


class Command(BaseCommand):
    """Sync the HRS Stock xlsx from Drive (or --file) into HRSStockLot,
    capture today's HRSStockSnapshot for every synced lot (unless
    --no-snapshot), and record the outcome as a SyncRun row.

    Idempotent: lots unchanged per sync_utils.unchanged() are skipped for
    the lot upsert; the snapshot write is a separate update_or_create keyed
    on (stock_lot, date) so it's always safe to re-run, snapshot or not.
    """

    help = "Sync the HRS Stock xlsx from Drive into HRSStockLot and capture today's snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")
        parser.add_argument("--no-snapshot", action="store_true", help="Skip daily snapshot capture.")

    def handle(self, *args, **options):
        """Parse the xlsx, upsert every lot (and today's snapshot) in one
        transaction, deactivate lots no longer seen, then always record a
        SyncRun regardless of outcome."""
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_bytes(options.get("file"))
            lots = parse_stock_xlsx(file_bytes)
            rows_seen = len(lots)
            today = timezone.localdate()

            with transaction.atomic():
                seen_refs = []
                for parsed in lots:
                    seen_refs.append(parsed.source_row_ref)
                    lot, changed = self._upsert_lot(parsed)
                    if changed:
                        rows_changed += 1
                    if not options["no_snapshot"]:
                        self._upsert_snapshot(lot, today)
                deactivated = (
                    HRSStockLot.objects.filter(is_active=True)
                    .exclude(source_row_ref__in=seen_refs)
                    .update(is_active=False)
                )

            self.stdout.write(self.style.SUCCESS(
                f"sync_stock: {rows_seen} stock rows seen, {rows_changed} created/updated, "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_stock: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_stock: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.HRS,
                source=SyncRun.Source.STOCK,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)

    def _load_bytes(self, local_path: str | None) -> bytes:
        """--file, or fetched from Drive by settings.HRS_STOCK_FILE_TITLE."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.HRS_STOCK_FILE_TITLE, parent_id=settings.HRS_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_lot(self, parsed) -> tuple[HRSStockLot, bool]:
        """Upsert one parsed stock lot by source_row_ref; returns (lot,
        False) with a no-op when the lot is unchanged and still active."""
        existing = HRSStockLot.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(HRSStockLot, existing, parsed, _FIELDS):
            return existing, False

        lot, _ = HRSStockLot.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return lot, True

    def _upsert_snapshot(self, lot: HRSStockLot, snapshot_date) -> None:
        """Record/overwrite today's snapshot for this lot - keyed on
        (stock_lot, snapshot_date), so a same-day re-run just overwrites."""
        HRSStockSnapshot.objects.update_or_create(
            stock_lot=lot,
            snapshot_date=snapshot_date,
            defaults={f: getattr(lot, f) for f in _SNAPSHOT_FIELDS},
        )
