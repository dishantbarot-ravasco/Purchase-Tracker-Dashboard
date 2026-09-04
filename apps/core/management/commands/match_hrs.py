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

Usage:
    python manage.py match_hrs
"""

from django.core.management.base import BaseCommand

from apps.services.matching import run_full_match


class Command(BaseCommand):
    """Run the HRS PO<->MIR and MIR<->Stock matching passes and print a
    one-line summary of how many records matched. All matching logic lives
    in apps.services.matching.run_full_match(); see this file's module
    docstring."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced HRS records."

    def handle(self, *args, **options):
        result = run_full_match()
        self.stdout.write(self.style.SUCCESS(
            f"match_hrs: {result['po_line_items_matched']} PO line items matched to MIR, "
            f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
            f"{result['mir_entries_stock_matched']} MIR entries matched to stock"
        ))
