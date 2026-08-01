"""
Daily RM Stock snapshot - the fix for "the stock file has no date column and
gets overwritten daily". Run once a day (Render Cron Job or any scheduler),
matching the ~5pm EOD timing of the old Cowork scheduled task.

Idempotent by design: the (plant, material_code, snapshot_date) unique
constraint on StockSnapshot means rerunning this on the same day just
updates today's rows instead of duplicating them, so it's always safe to
run again if a run fails partway or gets triggered twice.
"""
from datetime import date

from django.core.management.base import BaseCommand

from core import mir_stock
from core.models import Plant, StockSnapshot


class Command(BaseCommand):
    help = "Reads each plant's current RM Stock file from Drive and upserts today's snapshot rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", dest="snapshot_date", default=None,
            help="Override the snapshot date (YYYY-MM-DD). Defaults to today.",
        )

    def handle(self, *args, **options):
        snapshot_date = options["snapshot_date"] or date.today().isoformat()

        for plant in [c[0] for c in Plant.choices]:
            self.stdout.write(f"Snapshotting {plant} RM Stock for {snapshot_date}...")
            try:
                rows = mir_stock.read_stock_rows_for_snapshot(plant)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  {plant}: failed to read - {e}"))
                continue

            created, updated = 0, 0
            for row in rows:
                if not row["material_code"] and not row["description"]:
                    continue
                # material_code can be blank on a handful of rows (known gap,
                # see project_mir_stock_standardization memory) - fall back to
                # description as the identity key so those rows still get a
                # history instead of being silently dropped.
                key = row["material_code"] or f"DESC:{row['description']}"
                obj, was_created = StockSnapshot.objects.update_or_create(
                    plant=plant,
                    material_code=key,
                    snapshot_date=snapshot_date,
                    defaults={
                        "description": row["description"],
                        "category": row["category"],
                        "qty": row["qty"],
                        "rate": row["rate"],
                        "value": row["value"],
                    },
                )
                created += was_created
                updated += not was_created

            self.stdout.write(self.style.SUCCESS(f"  {plant}: {created} new rows, {updated} updated."))
