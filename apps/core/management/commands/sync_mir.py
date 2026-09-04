"""
apps/core/management/commands/sync_mir.py — syncs HRS MIR FILE
2026-2027.xlsx ('RAW MATERIAL' sheet) from Drive into HRSMIREntry, keyed by
source_row_ref (the sheet row number).

source_row_ref is a sheet ROW NUMBER, not a stable business key - if a row is
inserted/deleted above another in the live sheet, every row below it shifts,
and the next sync will upsert the shifted row's new content onto the old
row's identity. This can't be fully solved without a real natural key (none
of this sheet's columns are reliably unique), but every row whose
source_row_ref no longer appears in the freshly parsed file is deactivated
(is_active=False) rather than left in the DB forever - see
HRSMIREntry.is_active's help_text. A row that reappears (e.g. a further edit
realigns row numbers) is reactivated automatically since every upsert below
sets is_active=True.

Change detection uses apps.services.sync_utils.unchanged() rather than a
whole-row hash (contrast with sync_po_csv.py's _po_hash) - unchanged()
quantizes Decimal fields with ROUND_HALF_UP before comparing, matching how
Postgres itself rounds a full-precision value on cast into a numeric(p,s)
column; see that function's own docstring and CLAUDE.md's "Change-detection
must compare quantized Decimal values" for why the more "obvious"
ROUND_HALF_EVEN/hash-based approaches were tried first and rejected after
real Vapi MIR rows landing on an exact X.XX5 boundary never converged to a
stable "unchanged" result.

Drive folder: settings.HRS_MIR_STOCK_FOLDER_ID - each plant's live MIR/Stock
files sit in their own separate folder, distinct from the shared PO-master
folder (see CLAUDE.md's "Two Drive folders per plant, not one").

--file lets this run against a local xlsx copy instead of hitting Drive -
useful for offline dev/testing without live service-account credentials.

Usage:
    python manage.py sync_mir
    python manage.py sync_mir --file path.xlsx  # parse a local file instead (dev/testing)
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import HRSMIREntry, SyncRun
from apps.services.parsers.mir import HeaderMismatch, parse_mir_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "month", "mir_no", "mir_date", "sap_grn_number", "po_number_raw", "party_name", "state",
    "invoice_no", "invoice_date", "material_description", "qty", "uom", "rate", "net",
    "discount_rate_pct", "discount_amt", "others", "taxable_value", "tax_rate_pct", "igst",
    "cgst_rate_pct", "cgst_amt", "sgst_rate_pct", "sgst_amt", "other_taxes_excl_gst",
    "total_amount", "tcs_rate_pct", "tcs_amt", "invoice_final_value", "plant_tag", "dept_use",
    "material_category", "remarks",
]


class Command(BaseCommand):
    """Sync the HRS MIR xlsx from Drive (or --file) into HRSMIREntry, and
    record the outcome as a SyncRun row.

    Idempotent: rows unchanged per sync_utils.unchanged() are skipped; rows
    no longer present in the sheet are deactivated, not deleted (see module
    docstring). Safe to re-run any time.
    """

    help = "Sync the HRS MIR xlsx from Drive into HRSMIREntry."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")

    def handle(self, *args, **options):
        """Parse the xlsx, upsert every row in one transaction, deactivate
        rows no longer seen, then always record a SyncRun regardless of
        outcome."""
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_bytes(options.get("file"))
            entries = parse_mir_xlsx(file_bytes)
            rows_seen = len(entries)

            with transaction.atomic():
                seen_refs = []
                for parsed in entries:
                    seen_refs.append(parsed.source_row_ref)
                    if self._upsert_entry(parsed):
                        rows_changed += 1
                deactivated = (
                    HRSMIREntry.objects.filter(is_active=True)
                    .exclude(source_row_ref__in=seen_refs)
                    .update(is_active=False)
                )

            self.stdout.write(self.style.SUCCESS(
                f"sync_mir: {rows_seen} MIR rows seen, {rows_changed} created/updated, "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_mir: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_mir: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.HRS,
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
        """--file, or fetched from Drive by settings.HRS_MIR_FILE_TITLE."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.HRS_MIR_FILE_TITLE, parent_id=settings.HRS_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_entry(self, parsed) -> bool:
        """Upsert one parsed MIR row by source_row_ref; returns False (no-op)
        when the row is unchanged and still active."""
        existing = HRSMIREntry.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(HRSMIREntry, existing, parsed, _FIELDS):
            return False

        HRSMIREntry.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return True
