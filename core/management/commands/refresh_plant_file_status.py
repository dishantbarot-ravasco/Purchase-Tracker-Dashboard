"""
Background refresh of PlantFileStatus (see its model docstring for why this
exists) - run this on a schedule (Render Cron Job, e.g. every 15-30 min),
NOT as part of any web request. This is what actually fixes the dashboard
timing out on a slow/cold Drive connection: the dashboard now just reads
whatever this command last wrote to Postgres, so it's fast and reliable
regardless of how long a Drive call takes.
"""
import logging

from django.core.management.base import BaseCommand

from core import mir_stock
from core.models import Plant, PlantFileStatus

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Refreshes the cached MIR/RM Stock row counts shown on the dashboard, reading Drive in the background."

    def handle(self, *args, **options):
        for plant in [c[0] for c in Plant.choices]:
            mir_count, mir_error = self._safe_count(mir_stock.get_mir_rm_row_count, plant, "MIR file")
            stock_count, stock_error = self._safe_count(mir_stock.get_stock_row_count, plant, "RM Stock file")
            error = mir_error or stock_error or ""

            PlantFileStatus.objects.update_or_create(
                plant=plant,
                defaults={"mir_row_count": mir_count, "stock_row_count": stock_count, "last_error": error},
            )
            if error:
                self.stderr.write(self.style.WARNING(f"{plant}: {error}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"{plant}: MIR={mir_count} rows, Stock={stock_count} rows"))

    def _safe_count(self, fn, plant, label):
        try:
            return fn(plant), None
        except Exception:
            logger.exception("Failed to read %s for plant %s", label, plant)
            return 0, f"{label} isn't available right now."
