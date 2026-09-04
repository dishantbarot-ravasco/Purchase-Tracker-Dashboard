"""
Syncs RTP ACHHAD MIR FILE 2026-27.xlsx ('R.M. ' sheet) from Drive into
RTPAchhadMIREntry, keyed by source_row_ref (the sheet row number).

source_row_ref is a sheet ROW NUMBER, not a stable business key - see
sync_mir.py's module docstring for the full row-shift reasoning. A row whose
source_row_ref no longer appears in the freshly parsed file is deactivated
(is_active=False) rather than deleted - see RTPAchhadMIREntry.is_active.

Usage:
    python manage.py sync_achhad_mir
    python manage.py sync_achhad_mir --file path.xlsx  # parse a local file instead (dev/testing)
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import RTPAchhadMIREntry, SyncRun
from apps.services.parsers.achhad_mir import HeaderMismatch, parse_achhad_mir_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "month", "mir_no", "mir_date", "po_number_raw", "party_name", "state",
    "invoice_no", "invoice_date", "material_description", "qty", "uom", "rate", "net",
    "discount_rate_pct", "discount_amt", "others", "taxable_value", "tax_rate_pct", "igst",
    "cgst_rate_pct", "cgst_amt", "sgst_rate_pct", "sgst_amt", "other_taxes_excl_gst",
    "total_amount", "tcs_rate_pct", "tcs_amt", "invoice_final_value", "plant_tag", "dept_use",
    "material_category", "remarks",
]


class Command(BaseCommand):
    help = "Sync the RTP-Achhad MIR xlsx from Drive into RTPAchhadMIREntry."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_bytes(options.get("file"))
            entries = parse_achhad_mir_xlsx(file_bytes)
            rows_seen = len(entries)

            with transaction.atomic():
                seen_refs = []
                for parsed in entries:
                    seen_refs.append(parsed.source_row_ref)
                    if self._upsert_entry(parsed):
                        rows_changed += 1
                deactivated = (
                    RTPAchhadMIREntry.objects.filter(is_active=True)
                    .exclude(source_row_ref__in=seen_refs)
                    .update(is_active=False)
                )

            self.stdout.write(self.style.SUCCESS(
                f"sync_achhad_mir: {rows_seen} MIR rows seen, {rows_changed} created/updated, "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_mir: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_mir: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_ACHHAD,
                source=SyncRun.Source.MIR,
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
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.ACHHAD_MIR_FILE_TITLE, parent_id=settings.ACHHAD_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_entry(self, parsed) -> bool:
        existing = RTPAchhadMIREntry.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(RTPAchhadMIREntry, existing, parsed, _FIELDS):
            return False

        RTPAchhadMIREntry.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return True
