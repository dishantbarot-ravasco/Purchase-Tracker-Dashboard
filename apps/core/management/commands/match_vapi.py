"""
apps/core/management/commands/match_vapi.py — runs the PO<->MIR and
MIR<->Stock reconciliation passes over every synced RTP-Vapi record.
Intended to run after sync_vapi_po_csv/sync_vapi_mir/sync_vapi_stock.

Same shape as match_hrs.py - see that file for the general design (matching
logic + SyncRun tracking + the 2026-09-04 fix for this command previously
having no error handling at all). What's different for this plant: matching
logic lives in apps.services.matching_vapi.run_full_match() (a separate
module, same deliberate-duplication reasoning as Achhad's). Vapi's
po_number_raw field is effectively always blank on real data (100% blank
across every row checked), so every Vapi PO<->MIR match runs on the Tier-2
weighted score alone - there is currently no usable Tier-1 shortcut for
this plant.

Usage:
    python manage.py match_vapi
"""

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SyncRun
from apps.services.matching_vapi import run_full_match


class Command(BaseCommand):
    """Run the RTP-Vapi PO<->MIR and MIR<->Stock matching passes, print a
    one-line summary, and record the outcome as a SyncRun row. See
    match_hrs.py's Command docstring - identical shape, different matching
    module (matching_vapi.py)."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced RTP-Vapi records."

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            result = run_full_match()
            rows_seen = result["po_line_items_matched"] + result["import_po_line_items_matched"] + result["mir_entries_stock_matched"]
            rows_changed = rows_seen
            self.stdout.write(self.style.SUCCESS(
                f"match_vapi: {result['po_line_items_matched']} PO line items matched to MIR, "
                f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
                f"{result['mir_entries_stock_matched']} MIR entries matched to stock "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"match_vapi: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_VAPI,
                source=SyncRun.Source.MATCH,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)
