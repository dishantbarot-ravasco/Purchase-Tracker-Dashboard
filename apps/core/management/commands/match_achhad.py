"""
apps/core/management/commands/match_achhad.py — runs the PO<->MIR and
MIR<->Stock reconciliation passes over every synced RTP-Achhad record.
Intended to run after sync_achhad_po_csv/sync_achhad_mir/sync_achhad_stock.

Same shape as match_hrs.py - see that file for the general design (matching
logic + SyncRun tracking + the 2026-09-04 fix for this command previously
having no error handling at all). What's different for this plant: matching
logic lives in apps.services.matching_achhad.run_full_match() (a separate
module from HRS's, deliberately duplicated rather than parameterized - see
CLAUDE.md/test_matching.py's docstring), and Achhad's MIR<->Stock pass gates
on material description alone (no vendor column exists on Achhad's Stock
sheet), a materially weaker guarantee than HRS's (material, vendor) gate.

Usage:
    python manage.py match_achhad
"""

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SyncRun
from apps.services.matching_achhad import run_full_match


class Command(BaseCommand):
    """Run the RTP-Achhad PO<->MIR and MIR<->Stock matching passes, print a
    one-line summary, and record the outcome as a SyncRun row. See
    match_hrs.py's Command docstring - identical shape, different matching
    module (matching_achhad.py)."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced RTP-Achhad records."

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
                f"match_achhad: {result['po_line_items_matched']} PO line items matched to MIR, "
                f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
                f"{result['mir_entries_stock_matched']} MIR entries matched to stock "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"match_achhad: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_ACHHAD,
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
