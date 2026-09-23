"""
apps/api/views.py - Misc top-level API views not specific to auth or a plant.

The liveness check and the readiness probe. Everything plant-specific
(purchase orders, materials, sync status, matches) lives under
apps/api/routers/ instead - this module intentionally stays small.
"""

import datetime

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Max
from django.http import JsonResponse
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.core.models import SyncRun

# The steps of each plant's scheduled pipeline (sync_trigger._PLANT_COMMANDS),
# as the SyncRun sources they record. MATCH and CONSUMPTION are included on
# purpose: both derive from the DB with no Drive fetch, so a stale one looks
# perfectly healthy from the dashboard - yesterday's rows are still there.
_WATCHED_PLANTS = (SyncRun.Plant.HRS, SyncRun.Plant.RTP_ACHHAD, SyncRun.Plant.RTP_VAPI)
_WATCHED_SOURCES = (
    SyncRun.Source.PO_CSV, SyncRun.Source.MIR, SyncRun.Source.STOCK,
    SyncRun.Source.MATCH, SyncRun.Source.CONSUMPTION,
)
# PARTIAL counts as having run: some rows were skipped, the data is current.
_COMPLETED = (SyncRun.Status.SUCCESS, SyncRun.Status.PARTIAL)


def health(request):
    """Unauthenticated liveness check - deliberately not cached, mirrors
    the TDS app's /api/ health-check convention. Touches nothing: "is the
    process up". See readiness() for "is it doing its job"."""
    return JsonResponse({"status": "ok"})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def readiness(request):
    """GET /api/health/ready - for an external uptime monitor (added
    2026-09-23, audit pass).

    health() above says the web process is up; it cannot say the app is
    WORKING. The failure this app actually has is quieter: CLAUDE.md's
    Scheduling section records the qcluster worker not running for 13 days
    with every page still loading and nothing on screen saying so, and since
    the Stock sheet holds only today's position, each missed day of snapshots
    is unrecoverable. This probe turns that into a status code a monitor can
    alert on:

      200 {"status": "ok"}        - DB reachable, every watched pipeline step
                                    completed within HEALTH_SYNC_STALE_HOURS.
      503 {"status": "degraded"}  - DB reachable, but `stale` lists the
                                    plant/step pairs that have not completed
                                    in that window (or ever).
      503 {"status": "down"}      - the database is unreachable.

    Deliberately minimal and unauthenticated: plant/step names only, no
    timestamps, counts or business data. authentication_classes is empty so
    a monitor that happens to send a stale cookie or header cannot turn a
    health check into a 401; the anon throttle still applies. Not wired to
    Render's healthCheckPath (that is `/`, and should stay a liveness check -
    a stale sync is not a reason to restart the web service)."""
    stale_after = datetime.timedelta(hours=settings.HEALTH_SYNC_STALE_HOURS)
    try:
        latest = {
            (row["plant"], row["source"]): row["last"]
            for row in SyncRun.objects
            .filter(plant__in=_WATCHED_PLANTS, source__in=_WATCHED_SOURCES, status__in=_COMPLETED)
            .values("plant", "source")
            .annotate(last=Max("finished_at"))
        }
    except DatabaseError:
        return Response({"status": "down", "database": "unreachable"}, status=503)

    cutoff = timezone.now() - stale_after
    stale = [
        f"{plant}/{source}"
        for plant in _WATCHED_PLANTS
        for source in _WATCHED_SOURCES
        if latest.get((plant, source)) is None or latest[(plant, source)] < cutoff
    ]
    body = {
        "status": "degraded" if stale else "ok",
        "database": "ok",
        "staleAfterHours": settings.HEALTH_SYNC_STALE_HOURS,
        "stale": stale,
    }
    return Response(body, status=503 if stale else 200)
