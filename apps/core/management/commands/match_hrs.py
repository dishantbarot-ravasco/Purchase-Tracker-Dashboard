"""
Runs the PO<->MIR and MIR<->Stock reconciliation passes over every synced
HRS record. Intended to run after sync_po_csv/sync_mir/sync_stock.

Usage:
    python manage.py match_hrs
"""

from django.core.management.base import BaseCommand

from apps.services.matching import run_full_match


class Command(BaseCommand):
    help = "Run PO<->MIR and MIR<->Stock matching over all synced HRS records."

    def handle(self, *args, **options):
        result = run_full_match()
        self.stdout.write(self.style.SUCCESS(
            f"match_hrs: {result['po_line_items_matched']} PO line items matched to MIR, "
            f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
            f"{result['mir_entries_stock_matched']} MIR entries matched to stock"
        ))
