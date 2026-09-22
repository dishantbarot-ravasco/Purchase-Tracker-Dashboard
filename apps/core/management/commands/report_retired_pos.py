"""
apps/core/management/commands/report_retired_pos.py - lists purchase orders
that are no longer in their plant's master CSV, so the deactivation the sync
now performs can be reviewed BEFORE it happens, and audited after.

Read-only by design, and that is the whole point of it existing separately
from the sync. Until 2026-09-18 the PO syncs never removed anything: upserting
on po_number as a natural key meant a RENAME upstream - adding or cleaning off
an annotation like "3000001104 (Changed Purchase Order)" - left the old
spelling behind forever as a second order carrying duplicate line items, still
competing for the same MIR rows. The domestic syncs detected this and wrote a
warning to stdout, which under the scheduled django-q2 run nobody ever reads;
the import syncs did not detect it at all.

The syncs now deactivate these automatically (see
sync_utils.deactivate_missing_orders()), which makes the pipeline
self-healing. This command is the human-readable half of that: run it to see
exactly what the next sync will retire, and to confirm afterwards that what
was retired is what you expected. It never writes, so it is always safe to
run, including against production.

    python manage.py report_retired_pos              # every plant
    python manage.py report_retired_pos --plant achhad
    python manage.py report_retired_pos --already-retired

Two different questions it answers:
  (default)          which stored orders are absent from the master CSV right
                     now - i.e. what the next sync would deactivate. Needs to
                     fetch each plant's CSV, so it is subject to the same
                     Drive credentials the sync uses.
  --already-retired  which orders are ALREADY deactivated, straight from the
                     database. No Drive access, no credentials, instant.
"""

from django.core.management.base import BaseCommand

from apps.core.models import (
    HRSDomesticPurchaseOrder,
    HRSImportPurchaseOrder,
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadImportPurchaseOrder,
    RTPVapiDomesticPurchaseOrder,
    RTPVapiImportPurchaseOrder,
)

# plant key -> (label, domestic model, import model)
PLANTS = {
    "hrs": ("HRS, Silvassa", HRSDomesticPurchaseOrder, HRSImportPurchaseOrder),
    "achhad": ("RTP-Achhad", RTPAchhadDomesticPurchaseOrder, RTPAchhadImportPurchaseOrder),
    "vapi": ("RTP-Vapi", RTPVapiDomesticPurchaseOrder, RTPVapiImportPurchaseOrder),
}


class Command(BaseCommand):
    help = "List purchase orders no longer in the master CSV (read-only; --already-retired reads the DB only)."

    def add_arguments(self, parser):
        parser.add_argument("--plant", choices=sorted(PLANTS), help="Limit to one plant.")
        parser.add_argument(
            "--already-retired", action="store_true",
            help="Report orders already deactivated in the DB instead of fetching each master CSV.",
        )

    def handle(self, *args, **options):
        keys = [options["plant"]] if options.get("plant") else sorted(PLANTS)
        total = 0
        for key in keys:
            label, domestic, imports = PLANTS[key]
            for kind, model in (("domestic", domestic), ("import", imports)):
                if options.get("already_retired"):
                    numbers = sorted(
                        model.objects.filter(is_active=False).values_list("po_number", flat=True))
                    caption = "already retired"
                else:
                    numbers = sorted(self._absent_from_csv(key, kind, model))
                    caption = "absent from the master CSV"
                total += len(numbers)
                if not numbers:
                    self.stdout.write(f"{label} {kind}: none {caption}.")
                    continue
                self.stdout.write(self.style.WARNING(
                    f"{label} {kind}: {len(numbers)} {caption}"))
                for number in numbers:
                    # An annotated number is the signature of a rename rather
                    # than a genuine withdrawal - worth calling out, since the
                    # two are otherwise indistinguishable from here and only
                    # the operator can tell them apart.
                    hint = "  (annotated - almost certainly a rename)" if "(" in number else ""
                    self.stdout.write(f"    {number}{hint}")
        self.stdout.write(f"\n{total} order(s) reported.")

    def _absent_from_csv(self, key: str, kind: str, model) -> list:
        """Stored ACTIVE orders whose number the current master CSV does not
        contain - what the next sync would deactivate."""
        from apps.services.sync_utils import orphaned_orders

        parsed = self._parse_master_csv(key, kind)
        live_numbers = {o.po_number for o in parsed}
        # orphaned_orders() considers every stored row; narrow to the active
        # ones, since already-retired orders are the other report's subject.
        return [
            number
            for number in orphaned_orders(model, parsed)
            if number not in live_numbers
            and model.objects.filter(po_number=number, is_active=True).exists()
        ]

    def _parse_master_csv(self, key: str, kind: str) -> list:
        """Fetches and parses one plant's master CSV, reusing the same Drive
        settings and parsers the sync commands themselves use, so this can
        never drift into reporting against a different file."""
        from django.conf import settings

        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        from apps.services.parsers.import_po_csv import parse_import_po_csv
        from apps.services.parsers.po_csv import parse_po_csv

        titles = {
            ("hrs", "domestic"): "HRS_PO_CSV_TITLE",
            ("hrs", "import"): "HRS_IMPORTS_PO_CSV_TITLE",
            ("achhad", "domestic"): "ACHHAD_PO_CSV_TITLE",
            ("achhad", "import"): "ACHHAD_IMPORTS_PO_CSV_TITLE",
            ("vapi", "domestic"): "VAPI_PO_CSV_TITLE",
            ("vapi", "import"): "VAPI_IMPORTS_PO_CSV_TITLE",
        }
        title = getattr(settings, titles[(key, kind)])
        file_id = find_file_id_by_title(title, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        text = download_file_bytes(file_id).decode("utf-8")
        return parse_po_csv(text) if kind == "domestic" else parse_import_po_csv(text)
