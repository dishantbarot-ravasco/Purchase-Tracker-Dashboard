"""
When a plant's dashboard data last changed for a reason other than a sync
(2026-09-25) - a correction, a pin, a dismissal, or the synchronous re-match
one of those runs.

The dashboard's freshness watcher (frontend/js/main.js's currentDataStamp())
decides whether to reload by comparing a stamp built from sync-status. That
stamp used to be SyncRun timestamps alone, and a correction or pin writes no
SyncRun row, so a colleague's change stayed invisible on every other open
dashboard until the next hourly sync. sync-status now also serves this
marker, and the stamp includes it.

Stored in the default cache (the shared DatabaseCache in production), keyed
per SyncRun plant value, with no expiry. Losing it is harmless: the watcher
just misses one change until the next sync, which is the behaviour before
this existed.
"""

from django.core.cache import cache
from django.utils import timezone

_KEY = "pt:data-changed:{}"


def touch(plant: str) -> None:
    """Record that `plant`'s dashboard data changed now."""
    if plant:
        cache.set(_KEY.format(plant), timezone.now().isoformat(), None)


def read(plant: str) -> str | None:
    """The ISO time `plant`'s data last changed outside a sync, or None."""
    return cache.get(_KEY.format(plant)) if plant else None
