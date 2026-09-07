"""
apps/core/management/commands/sync_vapi_stock.py — syncs RAVASCO VAPI RM
STOCK FILE.xlsx from Drive into RTPVapiStockLot, keyed by natural_key (a
stable business identity - see apps/services/stock_identity.py), and
captures today's RTPVapiStockSnapshot for every lot synced.

Same shape as sync_stock.py for HRS - see that file for the general design
(natural_key vs. the old source_row_ref, sync_utils.unchanged(), the
separate per-day snapshot upsert, --file/--no-snapshot). What's genuinely
different for this plant: the Drive folder is settings.VAPI_MIR_STOCK_FOLDER_ID;
the sheet keeps a fixed 'Stock' tab name (like HRS) but its header sits one
row lower, behind a 5-row document-control title block HRS's sheet doesn't
have (see apps/services/parsers/vapi_stock.py's docstring for the exact
layout); the file is a real shared multi-plant ledger (plant_tag captures
real PLANT column values like RTP-1/HRS/RTP-2, not filtered out); the
natural_key's code segment is hsn_code (Vapi's Stock sheet has no SAP item
code column); and unlike Achhad's Stock sheet, Vapi's does have a genuine
vendor column (supplier_name, confirmed not just an echo of PLANT), so
RTPVapiMirStockMatch uses the stronger (material, vendor) gate, same as HRS.

Usage:
    python manage.py sync_vapi_stock
    python manage.py sync_vapi_stock --file path.xlsx    # parse a local file instead (dev/testing)
    python manage.py sync_vapi_stock --no-snapshot        # skip snapshot capture
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import DataQualityFlag, RTPVapiStockLot, RTPVapiStockSnapshot, SyncRun
from apps.services.arithmetic_checks import check_stock_lot
from apps.services.data_quality import sync_data_quality_flags
from apps.services.parsers.vapi_stock import HeaderMismatch, parse_vapi_stock_xlsx
from apps.services.stock_identity import OccurrenceCounter
from apps.services.sync_utils import unchanged

_FIELDS = [
    "sr_no", "plant_tag", "description", "category", "sub_category", "uom",
    "opening_stock", "received", "issued", "todays_stock", "basic_rate", "value",
    "received_date", "supplier_name", "billing_on_plant", "material_location", "hsn_code",
]
_SNAPSHOT_FIELDS = ["opening_stock", "received", "issued", "todays_stock", "basic_rate", "value"]


class Command(BaseCommand):
    """Sync the RTP-Vapi Stock xlsx from Drive (or --file) into
    RTPVapiStockLot, capturing today's snapshot per lot unless
    --no-snapshot. See sync_stock.py's Command docstring for the
    idempotency design (unchanged from HRS)."""

    help = "Sync the RTP-Vapi Stock xlsx from Drive into RTPVapiStockLot and capture today's snapshot."

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
            lots = parse_vapi_stock_xlsx(file_bytes)
            rows_seen = len(lots)
            today = timezone.localdate()
            counter = OccurrenceCounter()

            with transaction.atomic():
                seen_keys = []
                for parsed in lots:
                    key = counter.key_for(code=parsed.hsn_code, description=parsed.description, vendor=parsed.supplier_name)
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
                    RTPVapiStockLot.objects.filter(is_active=True)
                    .exclude(natural_key__in=seen_keys)
                    .update(is_active=False)
                )

            self._sync_data_quality_flags()

            if rows_skipped:
                status = SyncRun.Status.PARTIAL
                error_detail = f"{rows_skipped} row(s) skipped: no material code or description to key on."

            self.stdout.write(self.style.SUCCESS(
                f"sync_vapi_stock: {rows_seen} stock rows seen, {rows_changed} created/updated, "
                f"{rows_skipped} skipped (no identity), "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_stock: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_stock: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_VAPI,
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
        """--file, or fetched from Drive by settings.VAPI_STOCK_FILE_TITLE
        (from settings.VAPI_MIR_STOCK_FOLDER_ID)."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.VAPI_STOCK_FILE_TITLE, parent_id=settings.VAPI_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_lot(self, parsed, natural_key: str) -> tuple[RTPVapiStockLot, bool]:
        """See sync_stock.py's _upsert_lot - same natural_key-lookup,
        unchanged()-and-skip logic."""
        existing = RTPVapiStockLot.objects.filter(natural_key=natural_key).first()
        if existing and existing.is_active and unchanged(RTPVapiStockLot, existing, parsed, _FIELDS):
            return existing, False

        lot, _ = RTPVapiStockLot.objects.update_or_create(
            natural_key=natural_key,
            defaults={f: getattr(parsed, f) for f in _FIELDS}
            | {"source_row_ref": parsed.source_row_ref, "last_synced_at": timezone.now(), "is_active": True},
        )
        return lot, True

    def _upsert_snapshot(self, lot: RTPVapiStockLot, snapshot_date) -> None:
        """Record/overwrite today's snapshot for this lot."""
        RTPVapiStockSnapshot.objects.update_or_create(
            stock_lot=lot,
            snapshot_date=snapshot_date,
            defaults={f: getattr(lot, f) for f in _SNAPSHOT_FIELDS},
        )

    def _sync_data_quality_flags(self) -> None:
        """See sync_stock.py's own _sync_data_quality_flags (Match Accuracy
        Programme fix 3.G) - same check, this plant's model. Confirmed
        empirically: reconciles 100% of the time on real Vapi data."""
        results = {
            lot.id: check_stock_lot(lot.opening_stock, lot.received, lot.issued, lot.todays_stock)
            for lot in RTPVapiStockLot.objects.filter(is_active=True)
        }
        sync_data_quality_flags(SyncRun.Plant.RTP_VAPI, DataQualityFlag.SourceType.STOCK_LOT, results)
