"""
apps/core/management/commands/sync_achhad_stock.py — syncs RAVASCO ACHHAD RM
STOCK FILE.xlsx from Drive into RTPAchhadRMLot, keyed by natural_key (a
stable business identity - see apps/services/stock_identity.py), and
captures today's RTPAchhadRMSnapshot for every lot synced.

Same shape as sync_stock.py for HRS - see that file for the general design
(natural_key vs. the old source_row_ref, sync_utils.unchanged(), the
separate per-day snapshot upsert, --file/--no-snapshot). What's genuinely
different for this plant: the Drive folder is settings.ACHHAD_MIR_STOCK_FOLDER_ID;
the file's single tab is renamed every month (e.g. 'Aug 26-27'), so the
parser reads wb.sheetnames[0] instead of matching a literal tab name; and
Achhad's Stock sheet is one row per material full stop, with no vendor
column at all (_FIELDS below has no party_name, unlike HRS's HRSRMLot) -
so this plant's natural_key has no vendor segment (vendor="" is passed to
OccurrenceCounter.key_for below), a materially weaker identity guarantee by
necessity (two lots of the same material are separated only by the
occurrence counter), documented in stock_identity.py and
apps/services/matching_achhad.py's module docstrings - still strictly
better than a row number.

**Superseded (2026-09-09): this command no longer writes
RTPAchhadRMDailyMovement rows** - the parser's day-matrix scan
(achhad_stock.py's former ParsedDailyMovement/_day_columns()/
_sheet_month_year()) was removed per the project owner's own decision, so
this sync is header-only now, the same shape as sync_stock.py/
sync_vapi_stock.py. RTPAchhadRMDailyMovement's model/migration and its
consumers (consumption_report.py) are left as-is - they already degrade
gracefully to the existing monthly-summary `isEstimate` fallback when no new
rows land, so nothing crashes; that table just stops growing going forward.

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

from apps.core.models import DataQualityFlag, RTPAchhadRMLot, RTPAchhadRMSnapshot, SyncRun
from apps.services.arithmetic_checks import check_stock_lot
from apps.services.data_quality import sync_data_quality_flags
from apps.services.parsers.achhad_stock import HeaderMismatch, parse_achhad_stock_xlsx
from apps.services.stock_identity import OccurrenceCounter
from apps.services.sync_utils import unchanged

_FIELDS = [
    "overall_sr_no", "category_sr_no", "description", "category", "sap_code", "rate",
    "zone", "msl", "opening_stock", "received", "issued", "todays_stock", "value",
    "physical_stock", "received_date",
]
_SNAPSHOT_FIELDS = ["opening_stock", "received", "issued", "todays_stock", "rate", "value"]


class Command(BaseCommand):
    """Sync the RTP-Achhad Stock xlsx from Drive (or --file) into
    RTPAchhadRMLot, capturing today's snapshot per lot unless
    --no-snapshot. See sync_stock.py's Command docstring for the
    idempotency design (unchanged from HRS)."""

    help = "Sync the RTP-Achhad Stock xlsx from Drive into RTPAchhadRMLot and capture today's snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")
        parser.add_argument("--no-snapshot", action="store_true", help="Skip daily snapshot capture.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        rows_skipped = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_bytes(options.get("file"))
            lots = parse_achhad_stock_xlsx(file_bytes)
            rows_seen = len(lots)
            today = timezone.localdate()
            counter = OccurrenceCounter()

            with transaction.atomic():
                seen_keys = []
                for parsed in lots:
                    key = counter.key_for(code=parsed.sap_code, description=parsed.description, vendor="")
                    if not key:
                        rows_skipped += 1
                        continue
                    seen_keys.append(key)
                    lot, changed = self._upsert_lot(parsed, key)
                    if changed:
                        rows_changed += 1
                    if not options["no_snapshot"]:
                        self._upsert_snapshot(lot, today)
                deactivated = (
                    RTPAchhadRMLot.objects.filter(is_active=True)
                    .exclude(natural_key__in=seen_keys)
                    .update(is_active=False)
                )

            self._sync_data_quality_flags()

            if rows_skipped:
                status = SyncRun.Status.PARTIAL
                error_detail = f"{rows_skipped} row(s) skipped: no material code or description to key on."

            self.stdout.write(self.style.SUCCESS(
                f"sync_achhad_stock: {rows_seen} stock rows seen, {rows_changed} created/updated, "
                f"{rows_skipped} skipped (no identity), "
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

    def _upsert_lot(self, parsed, natural_key: str) -> tuple[RTPAchhadRMLot, bool]:
        """See sync_stock.py's _upsert_lot - same natural_key-lookup,
        unchanged()-and-skip logic."""
        existing = RTPAchhadRMLot.objects.filter(natural_key=natural_key).first()
        if existing and existing.is_active and unchanged(RTPAchhadRMLot, existing, parsed, _FIELDS):
            return existing, False

        lot, _ = RTPAchhadRMLot.objects.update_or_create(
            natural_key=natural_key,
            defaults={f: getattr(parsed, f) for f in _FIELDS}
            | {"source_row_ref": parsed.source_row_ref, "last_synced_at": timezone.now(), "is_active": True},
        )
        return lot, True

    def _upsert_snapshot(self, lot: RTPAchhadRMLot, snapshot_date) -> None:
        """Record/overwrite today's snapshot for this lot."""
        RTPAchhadRMSnapshot.objects.update_or_create(
            stock_lot=lot,
            snapshot_date=snapshot_date,
            defaults={f: getattr(lot, f) for f in _SNAPSHOT_FIELDS},
        )

    def _sync_data_quality_flags(self) -> None:
        """See sync_stock.py's own _sync_data_quality_flags (Match Accuracy
        Programme fix 3.G) - same check, this plant's model. Confirmed
        empirically: reconciles 99.7% of the time; the one real mismatch
        found is the same "stock materialized from nowhere" shape as HRS's."""
        results = {
            lot.id: check_stock_lot(lot.opening_stock, lot.received, lot.issued, lot.todays_stock)
            for lot in RTPAchhadRMLot.objects.filter(is_active=True)
        }
        sync_data_quality_flags(SyncRun.Plant.RTP_ACHHAD, DataQualityFlag.SourceType.STOCK_LOT, results)
