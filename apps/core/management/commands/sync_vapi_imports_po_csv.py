"""
apps/core/management/commands/sync_vapi_imports_po_csv.py — syncs
Master_RTP_VAPI_Imports_Purchase_Data.csv from Drive into
RTPVapiImportPurchaseOrder / RTPVapiImportPOLineItem.

Same shape as sync_hrs_imports_po_csv.py - see that file for the general
design (shared parser and shared apps.services.import_sync.sync_orders()
upsert helper across all three plants). What's different for this plant:
the Drive file title is settings.VAPI_IMPORTS_PO_CSV_TITLE (still the
shared settings.PURCHASE_TRACKER_DB_FOLDER_ID), and the target models are
RTPVapiImportPurchaseOrder/RTPVapiImportPOLineItem plus
SyncRun.Plant.RTP_VAPI. Unlike HRS/Achhad, this is the plant with real data
today (29 POs / 37 line items confirmed live 2026-09-04).

Usage:
    python manage.py sync_vapi_imports_po_csv
    python manage.py sync_vapi_imports_po_csv --file path.csv  # local file (dev/testing)
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import RTPVapiImportPOLineItem, RTPVapiImportPurchaseOrder, SyncRun
from apps.services.import_sync import sync_orders
from apps.services.parsers.import_po_csv import HeaderMismatch, parse_import_po_csv


class Command(BaseCommand):
    """Sync the RTP-Vapi Import PO master CSV from Drive (or --file) into
    RTPVapiImportPurchaseOrder/RTPVapiImportPOLineItem. See
    sync_hrs_imports_po_csv.py's Command docstring for the idempotency
    design (unchanged from HRS)."""

    help = "Sync the RTP-Vapi Import Purchase Order master CSV from Drive into RTPVapiImportPurchaseOrder/RTPVapiImportPOLineItem."

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
            rows_seen, rows_changed = sync_orders(RTPVapiImportPurchaseOrder, RTPVapiImportPOLineItem, orders)

            self.stdout.write(self.style.SUCCESS(
                f"sync_vapi_imports_po_csv: {rows_seen} POs seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_imports_po_csv: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_imports_po_csv: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_VAPI,
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
        """--file, or fetched from Drive by settings.VAPI_IMPORTS_PO_CSV_TITLE."""
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.VAPI_IMPORTS_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")
