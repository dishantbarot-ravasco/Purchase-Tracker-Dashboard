"""
apps/core/management/commands/ensure_schedules.py — Snapshot Pipeline
Rebuild, Phase B (see CLAUDE.md): idempotently creates/updates the one
django-q2 Schedule row this app needs, so the raw-material snapshot capture
actually runs on its own instead of only ever happening as a side effect of
a human clicking "Refresh Data".

Before this command existed, `qcluster` ran as a deployed Render service
with nothing telling it to do anything unprompted - no
django_q.models.Schedule row was ever created anywhere in this repo.
`run_daily_sync_all_plants()` (apps/services/sync_trigger.py) is the actual
work; this command only ensures django-q2 knows to call it on a schedule.

**Interval changed 2026-09-08 (project owner: "I want the first sync to
happen at 9:00 am daily and thereafter every hour and last sync at 8:00
pm") - now `Schedule.CRON` with `cron="0 9-20 * * *"`**, not the previous
every-3-hours-around-the-clock (`Schedule.MINUTES`, `minutes=180`, added
2026-09-07) or the original once-daily schedule before that. `"0 9-20 * * *"`
fires at minute 0 of every hour from 9 through 20 (9:00 AM through 8:00 PM),
12 times a day, evaluated in `settings.TIME_ZONE` ("Asia/Kolkata" - see
django_q2's own `Schedule.calculate_next_run()`, which calls Django's
`localtime()` before applying the cron expression) - no separate IST
conversion needed here. Requires the `croniter` package (added as a real
dependency this pass, pure-Python, no native build step - see this repo's
own Dockerfile note on why that matters) - django-q2 itself already handles
its absence gracefully (`Schedule.CRON` just raises a clear ImportError if
used without it), so nothing else in this app was silently depending on
`Schedule.CRON` working before now.

The function name (`run_daily_sync_all_plants`) and this row's own `name`
string (`daily-sync-all-plants`) are now doubly stale relative to what
actually happens - deliberately left unrenamed rather than "fixed", because
get_or_create() below is keyed on `name` alone: renaming it would make this
command create a *second* Schedule row on any environment where the old row
already exists (this repo's own local dev DB, for one) instead of migrating
that row in place, leaving two overlapping sync jobs active together.
Correcting `schedule_type`/`cron` (and `func`, as before) in place on the
existing row is the migration path instead - see the drift-correction block
below, which now also clears the old `minutes` value.

get_or_create() on `name` alone (not unique at the DB level, but this
command is the only code path that ever creates a Schedule row here) makes
re-running this safe - a container restart re-running render.yaml's
buildCommand/docker-entrypoint.sh must not create a second, duplicate job.
Deliberately get_or_create(), not update_or_create(): once the row exists,
this command only corrects `func`/`schedule_type`/`cron` if they've drifted
and never touches `next_run` again - django-q2 owns advancing that field
itself after each real run, and a deploy re-running this command must not be
able to stomp on a legitimately-overdue-but-not-yet-executed run (e.g.
qcluster was down over a deploy) by resetting next_run forward before the
scheduler daemon gets a chance to fire it. Q_CLUSTER["catch_up"] is already
False (config/settings.py) - a worker that was down for a while runs once on
restart, not once per missed interval.

Usage:
    python manage.py ensure_schedules
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule

_SCHEDULE_NAME = "daily-sync-all-plants"  # stale name, kept - see module docstring
_SCHEDULE_FUNC = "apps.services.sync_trigger.run_daily_sync_all_plants"
_SCHEDULE_TYPE = Schedule.CRON
_CRON = "0 9-20 * * *"  # 9:00 AM through 8:00 PM IST, once an hour (12 runs/day)


class Command(BaseCommand):
    help = "Idempotently create/update the daily-sync-all-plants django-q2 Schedule row (9 AM-8 PM IST, hourly)."

    def handle(self, *args, **options):
        schedule, created = Schedule.objects.get_or_create(
            name=_SCHEDULE_NAME,
            defaults={
                "func": _SCHEDULE_FUNC,
                "schedule_type": _SCHEDULE_TYPE,
                "cron": _CRON,
                "minutes": None,
                "next_run": timezone.now(),
            },
        )
        drifted = (
            schedule.func != _SCHEDULE_FUNC
            or schedule.schedule_type != _SCHEDULE_TYPE
            or schedule.cron != _CRON
        )
        if not created and drifted:
            # Correct drift (e.g. the every-3-hours->cron migration, or
            # func's dotted path changing in a refactor) without touching
            # next_run - see the module docstring for why. `minutes` is
            # cleared too - a stale value left over from the MINUTES-typed
            # row would otherwise sit there unused but confusing to read.
            schedule.func = _SCHEDULE_FUNC
            schedule.schedule_type = _SCHEDULE_TYPE
            schedule.cron = _CRON
            schedule.minutes = None
            schedule.save(update_fields=["func", "schedule_type", "cron", "minutes"])

        verb = "created" if created else "already exists"
        self.stdout.write(self.style.SUCCESS(
            f"ensure_schedules: {_SCHEDULE_NAME!r} {verb} (cron {_CRON!r}, "
            f"next_run={schedule.next_run.isoformat()})"
        ))
