"""
apps/core/management/commands/sync_hrs_imports_po_csv.py — syncs
Master_HRS_SILVASSA_Imports_Purchase_Data.csv from Drive into
HRSImportPurchaseOrder / HRSImportPOLineItem.

The Imports CSV format turned out to be shared across all three plants
(same BOE number/bill of lading/exchange rate/dual-quantity columns), unlike
MIR/Stock which genuinely differ per plant - so apps/services/parsers/
import_po_csv.py's parse_import_po_csv() is reused as-is, and the actual
upsert-with-change-detection logic lives in apps.services.import_sync.
sync_orders() (also shared across all three sync_*_imports_po_csv commands)
rather than being duplicated per plant the way sync_po_csv.py's _po_hash is
- this command is just the thin per-plant wiring (target models, Drive file
title, SyncRun.Plant tag). See sync_achhad_imports_po_csv.py /
sync_vapi_imports_po_csv.py for the other two plants' equally thin variants.

Header-only on Drive as of 2026-09-04 (no real HRS import POs yet) - this
command still runs cleanly against an empty file (0 rows seen, 0 changed),
so the dashboard's KPIs show 0 for HRS rather than erroring.

Drive folder: settings.PURCHASE_TRACKER_DB_FOLDER_ID (the same shared
folder as the domestic PO master CSVs, not a per-plant MIR/Stock folder).

Usage:
    python manage.py sync_hrs_imports_po_csv
    python manage.py sync_hrs_imports_po_csv --file path.csv  # local file (dev/testing)
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import HRSImportPOLineItem, HRSImportPurchaseOrder, SyncRun
from apps.services.import_sync import sync_orders
from apps.services.parsers.import_po_csv import HeaderMismatch, parse_import_po_csv


class Command(BaseCommand):
    """Sync the HRS Import PO master CSV from Drive (or --file) into
    HRSImportPurchaseOrder/HRSImportPOLineItem, and record the outcome as a
    SyncRun row.

    Idempotent: apps.services.import_sync.sync_orders() handles the actual
    upsert-with-change-detection, shared with the Achhad/Vapi variants.
    """

    help = "Sync the HRS Import Purchase Order master CSV from Drive into HRSImportPurchaseOrder/HRSImportPOLineItem."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local CSV file instead of fetching from Drive.")

    def handle(self, *args, **options):
        """Parse the CSV and hand it to the shared sync_orders() helper,
        then always record a SyncRun (SUCCESS or FAILED) regardless of
        outcome."""
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            csv_text = self._load_csv_text(options.get("file"))
            orders = parse_import_po_csv(csv_text)
            rows_seen, rows_changed = sync_orders(HRSImportPurchaseOrder, HRSImportPOLineItem, orders)

            self.stdout.write(self.style.SUCCESS(
                f"sync_hrs_imports_po_csv: {rows_seen} POs seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_hrs_imports_po_csv: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_hrs_imports_po_csv: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.HRS,
                source=SyncRun.Source.IMPORT_PO_CSV,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)

    def _load_csv_text(self, local_path: str | None) -> str:
        """--file, or fetched from Drive by settings.HRS_IMPORTS_PO_CSV_TITLE."""
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.HRS_IMPORTS_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")
