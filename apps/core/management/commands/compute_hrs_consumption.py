"""
apps/core/management/commands/compute_hrs_consumption.py — rebuilds HRS's
raw-material consumption ledger (MaterialConsumptionDaily +
ConsumptionEvent) from the *RMSnapshot history already in Postgres.

Thin by design, exactly like match_hrs.py beside it: all the arithmetic
lives in apps.services.consumption_engine (pure, dependency-free) and all
the DB work in apps.services.consumption_ledger.rebuild_plant_consumption().
This command only wires it to `manage.py`, prints a one-line summary, and
records a SyncRun row.

**Fetches nothing from Drive.** It runs after the plant's sync+match
pipeline (see apps/services/sync_trigger.py's _PLANT_COMMANDS) and reads
only what those commands already wrote, so it is fast, safe to re-run, and
cannot fail for a network reason.

Records a `SyncRun.Source.CONSUMPTION` row for the same reason match_hrs.py
records a `MATCH` row (see that file's header for the incident): a derive-
from-DB step with no Drive fetch has nothing else that would ever show it
ran. It is worse here than for matching, in fact - a stale consumption
ledger looks completely healthy from the outside, because yesterday's rows
are still sitting in the table returning plausible numbers.

Usage:
    python manage.py compute_hrs_consumption              # trailing lookback
    python manage.py compute_hrs_consumption --all        # whole history
    python manage.py compute_hrs_consumption --since 2026-09-01
"""

import datetime
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import SyncRun
from apps.services.consumption_ledger import rebuild_plant_consumption

# Wide enough that a snapshot arriving late still gets folded in (a late
# row changes the interval on BOTH sides of it, not just its own day) and
# that a month boundary is always fully inside the window when the monthly
# report runs on the 1st. Narrow enough that twelve runs a day isn't
# re-deriving all of history twelve times. --all exists for when it should.
_DEFAULT_LOOKBACK_DAYS = 45

_PLANT_KEY = "hrs"
_PLANT = SyncRun.Plant.HRS


class Command(BaseCommand):
    help = "Rebuild the HRS raw-material consumption ledger from synced stock snapshots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Rebuild the entire history instead of the default trailing lookback.",
        )
        parser.add_argument(
            "--since", help="Rebuild from this date (YYYY-MM-DD) forward.",
        )

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            if options["all"]:
                since = None
            elif options["since"]:
                since = datetime.date.fromisoformat(options["since"])
            else:
                since = timezone.localdate() - datetime.timedelta(days=_DEFAULT_LOOKBACK_DAYS)

            result = rebuild_plant_consumption(_PLANT_KEY, since=since)
            rows_seen = result["lots_read"]
            rows_changed = result["material_days"]
            self.stdout.write(self.style.SUCCESS(
                f"compute_{_PLANT_KEY}_consumption: {result['material_days']} material-days written "
                f"from {result['lots_read']} lots "
                f"({result['spread_days']} interpolated across snapshot gaps), "
                f"{result['events']} excluded/flagged events, "
                f"{result['total_quantity']:,.3f} units total "
                f"({time.monotonic() - t0:.1f}s)"
            ))
            if result["events"]:
                # Not a failure - an excluded event is a correct outcome.
                # Surfaced on stdout so it shows up in the pipeline log the
                # same way sync_stock's rows_skipped does.
                self.stdout.write(
                    f"compute_{_PLANT_KEY}_consumption: see ConsumptionEvent rows for what was excluded and why."
                )
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"compute_{_PLANT_KEY}_consumption: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=_PLANT,
                source=SyncRun.Source.CONSUMPTION,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)
