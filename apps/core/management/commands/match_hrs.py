"""
apps/core/management/commands/match_hrs.py — runs the PO<->MIR and
MIR<->Stock reconciliation passes over every synced HRS record. Intended to
run after sync_po_csv/sync_mir/sync_stock (and sync_hrs_imports_po_csv for
the import-PO<->MIR pass).

Thin by design: all the actual matching logic (vendor-name hard-gating,
Tier-1 po_number_raw shortcut vs. Tier-2 weighted score, the
MATCH_THRESHOLD/FLAG_DIFF_PCT policy constants) lives in
apps.services.matching.run_full_match() - this command just wires it to
`manage.py match_hrs` and prints a one-line summary. See that module's
docstring, and CLAUDE.md's "PO<->MIR<->Stock matching" section, for the
actual scoring design. Safe to re-run any time: run_full_match() does
idempotent upserts via update_or_create() internally, it doesn't need any
change-detection logic of its own here.

Real bug, found and fixed 2026-09-04: this command used to have no error
handling and no SyncRun tracking at all - every sync_*.py command in this
package records a SyncRun row (success or failed, with error_detail) and
exits non-zero on failure, but match_hrs/match_achhad/match_vapi didn't,
even though they're triggered the exact same way (apps/services/
sync_trigger.py's _run_pipeline()/_run_imports_pipeline() run them right
after the sync_* commands). A real exception inside run_full_match() (a bug
in matching.py, a DB constraint violation, anything) was only ever visible
in logs/app.log - the sync badges on the dashboard/admin panel would show
every source green (PO Updated/MIR/RM all succeeded) even though matching -
the step that actually produces every discrepancy flag/confidence badge on
the dashboard - had silently failed. Now records a SyncRun.Source.MATCH row
the same way every sync_* command does, using the result dict's own counts
for rows_seen/rows_changed (total items considered vs. actually matched)
instead of leaving them at 0 for a step that isn't really row-count-shaped
the way a CSV import is - "0 seen, 0 changed" on every single run, even a
real success, would have been its own quiet, misleading signal.

Usage:
    python manage.py match_hrs
"""

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SyncRun
from apps.services.matching import run_full_match


class Command(BaseCommand):
    """Run the HRS PO<->MIR and MIR<->Stock matching passes, print a
    one-line summary, and record the outcome as a SyncRun row (see this
    file's own header comment for why that's new). All matching logic lives
    in apps.services.matching.run_full_match()."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced HRS records."

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
                f"match_hrs: {result['po_line_items_matched']} PO line items matched to MIR, "
                f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
                f"{result['mir_entries_stock_matched']} MIR entries matched to stock "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"match_hrs: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.HRS,
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
