"""
apps/core/management/commands/match_achhad.py — runs the PO<->MIR and
MIR<->Stock reconciliation passes over every synced RTP-Achhad record.
Intended to run after sync_achhad_po_csv/sync_achhad_mir/sync_achhad_stock.

Same shape as match_hrs.py - see that file for the general design. What's
different for this plant: matching logic lives in
apps.services.matching_achhad.run_full_match() (a separate module from
HRS's, deliberately duplicated rather than parameterized - see
CLAUDE.md/test_matching.py's docstring), and Achhad's MIR<->Stock pass gates
on material description alone (no vendor column exists on Achhad's Stock
sheet), a materially weaker guarantee than HRS's (material, vendor) gate.

Usage:
    python manage.py match_achhad
"""

from django.core.management.base import BaseCommand

from apps.services.matching_achhad import run_full_match


class Command(BaseCommand):
    """Run the RTP-Achhad PO<->MIR and MIR<->Stock matching passes and print
    a one-line summary. See match_hrs.py's Command docstring - identical
    shape, different matching module (matching_achhad.py)."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced RTP-Achhad records."

    def handle(self, *args, **options):
        result = run_full_match()
        self.stdout.write(self.style.SUCCESS(
            f"match_achhad: {result['po_line_items_matched']} PO line items matched to MIR, "
            f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
            f"{result['mir_entries_stock_matched']} MIR entries matched to stock"
        ))
