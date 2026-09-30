"""
apps/core/management/commands/sync_procurement_pos.py - projects the plants'
PO master CSV mirrors into the normalized procurement tables the MIR form
reads (apps/services/procurement_sync.py).

Every sync_*_po_csv run already does this for its own plant; this command is
for the first backfill after deploying, and for re-running by hand. It reads
only the database (no Drive call) and is idempotent.

Usage:
    python manage.py sync_procurement_pos             # all three plants
    python manage.py sync_procurement_pos --plant vapi
"""

from django.core.management.base import BaseCommand

from apps.services.procurement_sync import LEGACY_PO_MODELS, project_plant_orders


class Command(BaseCommand):
    help = "Project the plants' PO master CSV mirrors into the normalized procurement tables."

    def add_arguments(self, parser):
        parser.add_argument("--plant", choices=sorted(LEGACY_PO_MODELS), help="One plant only.")

    def handle(self, *args, **options):
        plants = [options["plant"]] if options.get("plant") else list(LEGACY_PO_MODELS)
        for code in plants:
            r = project_plant_orders(code)
            self.stdout.write(self.style.SUCCESS(
                f"sync_procurement_pos {code}: {r.orders_seen} orders seen, {r.orders_written} written, "
                f"lines {r.lines_created} created / {r.lines_updated} updated / {r.lines_deactivated} deactivated, "
                f"{len(r.orders_held)} left as the app has them"
            ))
            if r.lines_flagged:
                self.stdout.write(self.style.WARNING(
                    f"sync_procurement_pos {code}: need review - " + ", ".join(r.lines_flagged)
                ))
