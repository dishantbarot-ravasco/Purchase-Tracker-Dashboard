"""
apps/core/management/commands/sync_vapi_mir.py — syncs RTP VAPI MIR FILE
2026-27.xlsx (' MIR FILE 26-27 RM' sheet) from Drive into RTPVapiMIREntry,
keyed by source_row_ref (the sheet row number).

Same shape as sync_mir.py for HRS - see that file for the general design
(row-shift reasoning for source_row_ref, sync_utils.unchanged()'s quantized-
Decimal comparison, --file for offline testing). What's genuinely different
for this plant: the Drive folder is settings.VAPI_MIR_STOCK_FOLDER_ID, and
Vapi's MIR sheet is the most structurally different of the three plants -
no Net/discount columns at all, GST split into one overall rate column
(gst_rate_pct, stored as a whole percentage like 18.00, NOT a fraction like
HRS/Achhad's 0.18 - confirmed by cross-checking Taxable Value x GST% / 100 =
IGST against a real row) plus three amount-only IGST/CGST/SGST columns
(no per-component rate columns), and a single TCS amount instead of a
rate+amount pair. _FIELDS below reflects that different column set - don't
assume it should line up field-for-field with HRS's/Achhad's _FIELDS.

Usage:
    python manage.py sync_vapi_mir
    python manage.py sync_vapi_mir --file path.xlsx  # parse a local file instead (dev/testing)
"""

import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from decimal import Decimal

from apps.core.models import DataQualityFlag, RTPVapiMIREntry, SyncRun
from apps.services.arithmetic_checks import check_mir_entry
from apps.services.data_quality import sync_data_quality_flags
from apps.services.parsers.vapi_mir import HeaderMismatch, parse_vapi_mir_xlsx
from apps.services.sync_utils import unchanged

_FIELDS = [
    "month", "mir_no", "mir_date", "po_number_raw", "sap_grn_number", "park_invoice_no",
    "post", "post_no_correction", "party_name", "state", "invoice_no", "invoice_date",
    "material_description", "item_code", "qty", "uom", "rate", "taxable_value",
    "others_with_gst", "gst_rate_pct", "igst_amt", "cgst_amt", "sgst_amt",
    "other_taxes_excl_gst", "tcs_amt", "invoice_final_value", "material_category",
    "date_sent_to_office", "date_sent_to_ho",
]


class Command(BaseCommand):
    """Sync the RTP-Vapi MIR xlsx from Drive (or --file) into
    RTPVapiMIREntry. See sync_mir.py's Command docstring for the
    idempotency design (unchanged from HRS)."""

    help = "Sync the RTP-Vapi MIR xlsx from Drive into RTPVapiMIREntry."

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
            entries = parse_vapi_mir_xlsx(file_bytes)
            rows_seen = len(entries)

            with transaction.atomic():
                seen_refs = []
                for parsed in entries:
                    seen_refs.append(parsed.source_row_ref)
                    if self._upsert_entry(parsed):
                        rows_changed += 1
                deactivated = (
                    RTPVapiMIREntry.objects.filter(is_active=True)
                    .exclude(source_row_ref__in=seen_refs)
                    .update(is_active=False)
                )

            self._sync_data_quality_flags()

            self.stdout.write(self.style.SUCCESS(
                f"sync_vapi_mir: {rows_seen} MIR rows seen, {rows_changed} created/updated, "
                f"{deactivated} deactivated (no longer in sheet) "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_mir: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_vapi_mir: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_VAPI,
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
        """--file, or fetched from Drive by settings.VAPI_MIR_FILE_TITLE
        (from settings.VAPI_MIR_STOCK_FOLDER_ID, Vapi's own MIR/Stock
        folder)."""
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.VAPI_MIR_FILE_TITLE, parent_id=settings.VAPI_MIR_STOCK_FOLDER_ID)
        return download_file_bytes(file_id)

    def _upsert_entry(self, parsed) -> bool:
        """See sync_mir.py's _upsert_entry - same unchanged()-and-skip logic."""
        existing = RTPVapiMIREntry.objects.filter(source_row_ref=parsed.source_row_ref).first()
        if existing and existing.is_active and unchanged(RTPVapiMIREntry, existing, parsed, _FIELDS):
            return False

        RTPVapiMIREntry.objects.update_or_create(
            source_row_ref=parsed.source_row_ref,
            defaults={f: getattr(parsed, f) for f in _FIELDS} | {"last_synced_at": timezone.now(), "is_active": True},
        )
        return True

    def _sync_data_quality_flags(self) -> None:
        """See sync_mir.py's own _sync_data_quality_flags (Match Accuracy
        Programme fix 3.G). Vapi's own formula is genuinely different from
        HRS's/Achhad's, confirmed empirically against real data (100%
        reconciliation once others_with_gst is included, ~4% mismatch rate
        without it): Vapi has no discount column at all (discount_amt=0),
        its IGST field is named igst_amt (not igst), and others_with_gst -
        an additional taxable charge - must be added into the gst sum for
        the arithmetic to actually hold."""
        results = {}
        for entry in RTPVapiMIREntry.objects.filter(is_active=True):
            gst_amt = (entry.igst_amt or 0) + (entry.cgst_amt or 0) + (entry.sgst_amt or 0) + (entry.others_with_gst or 0)
            results[entry.id] = check_mir_entry(entry.taxable_value, gst_amt, entry.tcs_amt, Decimal("0"), entry.invoice_final_value)
        sync_data_quality_flags(SyncRun.Plant.RTP_VAPI, DataQualityFlag.SourceType.MIR_ENTRY, results)
