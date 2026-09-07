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

**Interval changed 2026-09-07: every 3 hours (`Schedule.MINUTES`,
`minutes=180`), not once a day.** The function name
(`run_daily_sync_all_plants`) and this row's own `name` string
(`daily-sync-all-plants`) are now stale relative to what actually happens -
deliberately left unrenamed rather than "fixed", because get_or_create()
below is keyed on `name` alone: renaming it would make this command create a
*second* Schedule row on any environment where the old daily row already
exists (this repo's own local dev DB, for one) instead of migrating that row
in place, leaving both a stale daily job and a new 3-hourly job active
together - duplicate, silently overlapping syncs. Correcting `schedule_type`/
`minutes` (and `func`, as before) in place on the existing row is the
migration path instead - see the drift-correction block below.

get_or_create() on `name` alone (not unique at the DB level, but this
command is the only code path that ever creates a Schedule row here) makes
re-running this safe - a container restart re-running render.yaml's
buildCommand/docker-entrypoint.sh must not create a second, duplicate job.
Deliberately get_or_create(), not update_or_create(): once the row exists,
this command only corrects `func`/`schedule_type`/`minutes` if they've
drifted and never touches `next_run` again - django-q2 owns advancing that
field itself after each real run, and a deploy re-running this command must
not be able to stomp on a legitimately-overdue-but-not-yet-executed run
(e.g. qcluster was down over a deploy) by resetting next_run forward before
the scheduler daemon gets a chance to fire it. Q_CLUSTER["catch_up"] is
already False (config/settings.py) - a worker that was down for a while
runs once on restart, not once per missed interval.

`Schedule.MINUTES` needs no DST/timezone correction the way DAILY did
(django_q2's own `calculate_next_run()` skips that adjustment for MINUTES/
HOURLY/YEARLY types) - `next_run` is only ever set to "now" on first
creation below; every run after that is next_run + 180 minutes, computed by
django-q2 itself.

Usage:
    python manage.py ensure_schedules
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule

_SCHEDULE_NAME = "daily-sync-all-plants"  # stale name, kept - see module docstring
_SCHEDULE_FUNC = "apps.services.sync_trigger.run_daily_sync_all_plants"
_SCHEDULE_TYPE = Schedule.MINUTES
_INTERVAL_MINUTES = 180  # every 3 hours


class Command(BaseCommand):
    help = "Idempotently create/update the daily-sync-all-plants django-q2 Schedule row (runs every 3 hours)."

    def handle(self, *args, **options):
        schedule, created = Schedule.objects.get_or_create(
            name=_SCHEDULE_NAME,
            defaults={
                "func": _SCHEDULE_FUNC,
                "schedule_type": _SCHEDULE_TYPE,
                "minutes": _INTERVAL_MINUTES,
                "next_run": timezone.now(),
            },
        )
        drifted = (
            schedule.func != _SCHEDULE_FUNC
            or schedule.schedule_type != _SCHEDULE_TYPE
            or schedule.minutes != _INTERVAL_MINUTES
        )
        if not created and drifted:
            # Correct drift (e.g. the daily->3-hourly migration, or func's
            # dotted path changing in a refactor) without touching next_run
            # - see the module docstring for why.
            schedule.func = _SCHEDULE_FUNC
            schedule.schedule_type = _SCHEDULE_TYPE
            schedule.minutes = _INTERVAL_MINUTES
            schedule.save(update_fields=["func", "schedule_type", "minutes"])

        verb = "created" if created else "already exists"
        self.stdout.write(self.style.SUCCESS(
            f"ensure_schedules: {_SCHEDULE_NAME!r} {verb} (every {_INTERVAL_MINUTES}m, "
            f"next_run={schedule.next_run.isoformat()})"
        ))
