"""
apps/core/management/commands/sync_achhad_mir.py — syncs RTP ACHHAD MIR FILE
2026-27.xlsx ('R.M. ' sheet - note the trailing space) from Drive into
RTPAchhadMIREntry, keyed by source_row_ref (the sheet row number).

Same shape as sync_mir.py for HRS - see that file for the general design
(row-shift reasoning for source_row_ref, sync_utils.unchanged()'s quantized-
Decimal comparison, --file for offline testing). What's genuinely different
for this plant: the Drive folder is settings.ACHHAD_MIR_STOCK_FOLDER_ID (its
own separate MIR/Stock folder, not HRS's), the sheet tab is 'R.M. ' rather
than 'RAW MATERIAL', and Achhad's sheet has exactly one PO-number/PO-date
pair with no SAP GRN column at all - unlike HRS, which has 4 PO-related
columns (Purchase Order No./Date + SAP P.O. No./Date) plus a GRN column (see
CLAUDE.md's "Per-plant models, not a shared schema" for the full column
comparison). _FIELDS below reflects that narrower column set.

Usage:
    python manage.py sync_achhad_mir
    python manage.py sync_achhad_mir --file path.xlsx  # parse a local file instead (dev/testing)
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import DataQualityFlag, RTPAchhadMIREntry, SyncRun
from apps.services.arithmetic_checks import check_mir_entry
from apps.services.data_quality import sync_data_quality_flags
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
    """Sync the RTP-Achhad MIR xlsx from Drive (or --file) into
    RTPAchhadMIREntry. See sync_mir.py's Command docstring for the
    idempotency design (unchanged from HRS)."""

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

            self._sync_data_quality_flags()

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
        """--file, or fetched from Drive by settings.ACHHAD_MIR_FILE_TITLE
        (from settings.ACHHAD_MIR_STOCK_FOLDER_ID, Achhad's own MIR/Stock
        folder)."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.ACHHAD_MIR_FILE_TITLE, parent_id=settings.ACHHAD_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_entry(self, parsed) -> bool:
        """See sync_mir.py's _upsert_entry - same unchanged()-and-skip logic."""
        existing = RTPAchhadMIREntry.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(RTPAchhadMIREntry, existing, parsed, _FIELDS):
            return False

        RTPAchhadMIREntry.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return True

    def _sync_data_quality_flags(self) -> None:
        """See sync_mir.py's own _sync_data_quality_flags (Match Accuracy
        Programme fix 3.G) - same formula, Achhad's field names happen to
        match HRS's exactly here (igst/cgst_amt/sgst_amt/discount_amt)."""
        results = {}
        for entry in RTPAchhadMIREntry.objects.filter(is_active=True):
            gst_amt = (entry.igst or 0) + (entry.cgst_amt or 0) + (entry.sgst_amt or 0)
            results[entry.id] = check_mir_entry(entry.taxable_value, gst_amt, entry.tcs_amt, entry.discount_amt, entry.invoice_final_value)
        sync_data_quality_flags(SyncRun.Plant.RTP_ACHHAD, DataQualityFlag.SourceType.MIR_ENTRY, results)
