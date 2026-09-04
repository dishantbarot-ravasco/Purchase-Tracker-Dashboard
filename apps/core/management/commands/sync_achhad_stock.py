"""
apps/core/management/commands/sync_achhad_stock.py — syncs RAVASCO ACHHAD RM
STOCK FILE.xlsx from Drive into RTPAchhadStockLot, keyed by source_row_ref
(the sheet row number), and captures today's RTPAchhadStockSnapshot for
every lot synced.

Same shape as sync_stock.py for HRS - see that file for the general design
(row-shift reasoning, sync_utils.unchanged(), the separate per-day snapshot
upsert, --file/--no-snapshot). What's genuinely different for this plant:
the Drive folder is settings.ACHHAD_MIR_STOCK_FOLDER_ID; the file's single
tab is renamed every month (e.g. 'Aug 26-27'), so the parser reads
wb.sheetnames[0] instead of matching a literal tab name; and Achhad's Stock
sheet is one row per material full stop, with no vendor column at all
(_FIELDS below has no party_name, unlike HRS's HRSStockLot) - a materially
weaker match guarantee for MIR<->Stock than HRS's/Vapi's (material, vendor)
gate, documented in apps/services/matching_achhad.py's module docstring.

Usage:
    python manage.py sync_achhad_stock
    python manage.py sync_achhad_stock --file path.xlsx    # parse a local file instead (dev/testing)
    python manage.py sync_achhad_stock --no-snapshot        # skip snapshot capture
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import RTPAchhadStockLot, RTPAchhadStockSnapshot, SyncRun
from apps.services.parsers.achhad_stock import HeaderMismatch, parse_achhad_stock_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "overall_sr_no", "category_sr_no", "description", "category", "sap_code", "rate",
    "zone", "msl", "opening_stock", "received", "issued", "todays_stock", "value",
    "physical_stock", "received_date",
]
_SNAPSHOT_FIELDS = ["opening_stock", "received", "issued", "todays_stock", "rate", "value"]


class Command(BaseCommand):
    """Sync the RTP-Achhad Stock xlsx from Drive (or --file) into
    RTPAchhadStockLot, capturing today's snapshot per lot unless
    --no-snapshot. See sync_stock.py's Command docstring for the
    idempotency design (unchanged from HRS)."""

    help = "Sync the RTP-Achhad Stock xlsx from Drive into RTPAchhadStockLot and capture today's snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")
        parser.add_argument("--no-snapshot", action="store_true", help="Skip daily snapshot capture.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_bytes(options.get("file"))
            lots = parse_achhad_stock_xlsx(file_bytes)
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
                    RTPAchhadStockLot.objects.filter(is_active=True)
                    .exclude(source_row_ref__in=seen_refs)
                    .update(is_active=False)
                )

            self.stdout.write(self.style.SUCCESS(
                f"sync_achhad_stock: {rows_seen} stock rows seen, {rows_changed} created/updated, "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_stock: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_stock: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_ACHHAD,
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
        """--file, or fetched from Drive by settings.ACHHAD_STOCK_FILE_TITLE
        (from settings.ACHHAD_MIR_STOCK_FOLDER_ID)."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.ACHHAD_STOCK_FILE_TITLE, parent_id=settings.ACHHAD_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_lot(self, parsed) -> tuple[RTPAchhadStockLot, bool]:
        """See sync_stock.py's _upsert_lot - same unchanged()-and-skip logic."""
        existing = RTPAchhadStockLot.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(RTPAchhadStockLot, existing, parsed, _FIELDS):
            return existing, False

        lot, _ = RTPAchhadStockLot.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return lot, True

    def _upsert_snapshot(self, lot: RTPAchhadStockLot, snapshot_date) -> None:
        """Record/overwrite today's snapshot for this lot."""
        RTPAchhadStockSnapshot.objects.update_or_create(
            stock_lot=lot,
            snapshot_date=snapshot_date,
            defaults={f: getattr(lot, f) for f in _SNAPSHOT_FIELDS},
        )
