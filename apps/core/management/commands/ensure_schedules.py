"""
apps/core/management/commands/ensure_schedules.py — Snapshot Pipeline
Rebuild, Phase B (see CLAUDE.md): idempotently creates/updates the one
django-q2 Schedule row this app needs, so the daily raw-material snapshot
capture actually runs on its own instead of only ever happening as a side
effect of a human clicking "Refresh Data".

Before this command existed, `qcluster` ran as a deployed Render service
with nothing telling it to do anything unprompted - no
django_q.models.Schedule row was ever created anywhere in this repo.
`run_daily_sync_all_plants()` (apps/services/sync_trigger.py) is the actual
work; this command only ensures django-q2 knows to call it once a day.

get_or_create() on `name` alone (not unique at the DB level, but this
command is the only code path that ever creates a Schedule row here) makes
re-running this safe - a container restart re-running render.yaml's
buildCommand/docker-entrypoint.sh must not create a second, duplicate daily
job. Deliberately get_or_create(), not update_or_create(): once the row
exists, this command only corrects `func`/`schedule_type` if they've
drifted and never touches `next_run` again - django-q2 owns advancing that
field itself after each real run, and a deploy re-running this command
must not be able to stomp on a legitimately-overdue-but-not-yet-executed
run (e.g. qcluster was down over a deploy) by resetting next_run forward
before the scheduler daemon gets a chance to fire it. Q_CLUSTER["catch_up"]
is already False (config/settings.py) - a worker that was down for two
days runs once on restart, not twice, which is the right behaviour for a
job whose whole purpose is one snapshot per real day.

Usage:
    python manage.py ensure_schedules
"""

import datetime
import zoneinfo

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule

_SCHEDULE_NAME = "daily-sync-all-plants"
_SCHEDULE_FUNC = "apps.services.sync_trigger.run_daily_sync_all_plants"
_RUN_HOUR = 2
_RUN_MINUTE = 30
_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _next_run_at_0230_ist() -> datetime.datetime:
    """Today's 02:30 IST if that's still in the future, else tomorrow's -
    django-q2 advances next_run by one day itself after each run, this is
    only ever consulted for the very first schedule creation (or if
    next_run somehow drifted into the past)."""
    now_ist = timezone.now().astimezone(_IST)
    candidate = now_ist.replace(hour=_RUN_HOUR, minute=_RUN_MINUTE, second=0, microsecond=0)
    if candidate <= now_ist:
        candidate += datetime.timedelta(days=1)
    return candidate


class Command(BaseCommand):
    help = "Idempotently create/update the daily-sync-all-plants django-q2 Schedule row."

    def handle(self, *args, **options):
        schedule, created = Schedule.objects.get_or_create(
            name=_SCHEDULE_NAME,
            defaults={
                "func": _SCHEDULE_FUNC,
                "schedule_type": Schedule.DAILY,
                "next_run": _next_run_at_0230_ist(),
            },
        )
        if not created and (schedule.func != _SCHEDULE_FUNC or schedule.schedule_type != Schedule.DAILY):
            # Correct drift (e.g. func's dotted path changed in a refactor)
            # without touching next_run - see the module docstring for why.
            schedule.func = _SCHEDULE_FUNC
            schedule.schedule_type = Schedule.DAILY
            schedule.save(update_fields=["func", "schedule_type"])

        verb = "created" if created else "already exists"
        self.stdout.write(self.style.SUCCESS(
            f"ensure_schedules: {_SCHEDULE_NAME!r} {verb} (next_run={schedule.next_run.isoformat()})"
        ))
