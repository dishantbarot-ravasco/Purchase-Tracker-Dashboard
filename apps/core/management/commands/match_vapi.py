"""
apps/core/management/commands/match_vapi.py — runs the PO<->MIR and
MIR<->Stock reconciliation passes over every synced RTP-Vapi record.
Intended to run after sync_vapi_po_csv/sync_vapi_mir/sync_vapi_stock.

Same shape as match_hrs.py - see that file for the general design. What's
different for this plant: matching logic lives in
apps.services.matching_vapi.run_full_match() (a separate module, same
deliberate-duplication reasoning as Achhad's). Vapi's po_number_raw field is
effectively always blank on real data (100% blank across every row checked),
so every Vapi PO<->MIR match runs on the Tier-2 weighted score alone - there
is currently no usable Tier-1 shortcut for this plant.

Usage:
    python manage.py match_vapi
"""

from django.core.management.base import BaseCommand

from apps.services.matching_vapi import run_full_match


class Command(BaseCommand):
    """Run the RTP-Vapi PO<->MIR and MIR<->Stock matching passes and print
    a one-line summary. See match_hrs.py's Command docstring - identical
    shape, different matching module (matching_vapi.py)."""

    help = "Run PO<->MIR and MIR<->Stock matching over all synced RTP-Vapi records."

    def handle(self, *args, **options):
        result = run_full_match()
        self.stdout.write(self.style.SUCCESS(
            f"match_vapi: {result['po_line_items_matched']} PO line items matched to MIR, "
            f"{result['import_po_line_items_matched']} import PO line items matched to MIR, "
            f"{result['mir_entries_stock_matched']} MIR entries matched to stock"
        ))
