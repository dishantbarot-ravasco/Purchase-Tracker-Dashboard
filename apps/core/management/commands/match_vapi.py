"""
Runs the PO<->MIR and MIR<->Stock reconciliation passes over every synced
RTP-Vapi record. Intended to run after sync_vapi_po_csv/sync_vapi_mir/
sync_vapi_stock.

Usage:
    python manage.py match_vapi
"""

from django.core.management.base import BaseCommand

from apps.services.matching_vapi import run_full_match


class Command(BaseCommand):
    help = "Run PO<->MIR and MIR<->Stock matching over all synced RTP-Vapi records."

    def handle(self, *args, **options):
        result = run_full_match()
        self.stdout.write(self.style.SUCCESS(
            f"match_vapi: {result['po_line_items_matched']} PO line items matched to MIR, "
            f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
            f"{result['mir_entries_stock_matched']} MIR entries matched to stock"
        ))
