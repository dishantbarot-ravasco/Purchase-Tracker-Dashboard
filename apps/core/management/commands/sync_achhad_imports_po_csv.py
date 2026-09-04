"""
Syncs Master_RTP_Achhad_Imports_Purchase_Data.csv from Drive into
RTPAchhadImportPurchaseOrder / RTPAchhadImportPOLineItem. Header-only on
Drive as of 2026-09-04 (no real Achhad import POs yet) - this command still
runs cleanly against an empty file (0 rows seen, 0 changed).

Usage:
    python manage.py sync_achhad_imports_po_csv
    python manage.py sync_achhad_imports_po_csv --file path.csv  # local file (dev/testing)
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import RTPAchhadImportPOLineItem, RTPAchhadImportPurchaseOrder, SyncRun
from apps.services.import_sync import sync_orders
from apps.services.parsers.import_po_csv import HeaderMismatch, parse_import_po_csv


class Command(BaseCommand):
    help = "Sync the RTP-Achhad Import Purchase Order master CSV from Drive into RTPAchhadImportPurchaseOrder/RTPAchhadImportPOLineItem."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local CSV file instead of fetching from Drive.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            csv_text = self._load_csv_text(options.get("file"))
            orders = parse_import_po_csv(csv_text)
            rows_seen, rows_changed = sync_orders(RTPAchhadImportPurchaseOrder, RTPAchhadImportPOLineItem, orders)

            self.stdout.write(self.style.SUCCESS(
                f"sync_achhad_imports_po_csv: {rows_seen} POs seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_imports_po_csv: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_imports_po_csv: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_ACHHAD,
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
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.ACHHAD_IMPORTS_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")
