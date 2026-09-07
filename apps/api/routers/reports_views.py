"""
apps/api/routers/reports_views.py — Endpoint for a free external scheduler
to trigger the daily Raw Material Consumption report, since:
  - Render's free web service plan has no built-in cron scheduler, and
  - Render's own Cron Jobs feature has a $1/month minimum (no free tier).

Instead, a free external pinger (cron-job.org) hits this endpoint once a
day (20:00 IST). Because the caller has no login session or JWT, it's
protected by a shared-secret query param / header instead -
REPORT_CRON_SECRET, set as a Render environment variable and given only to
the scheduler config, never to a browser or the frontend.

Ported byte-for-byte (auth scheme only - the report itself is this app's
own) from the TDS Automation App's own apps/api/routers/reports_views.py -
same reasoning, same shared-secret scheme, don't diverge without a reason.
"""
import hmac
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.services.consumption_report import send_daily_consumption_reports

log = logging.getLogger(__name__)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def trigger_daily_report(request):
    """
    GET/POST /api/internal/send-daily-report?secret=<REPORT_CRON_SECRET>
    (or header 'X-Report-Secret: <REPORT_CRON_SECRET>')

    Runs send_daily_consumption_reports() and emails every active admin, one
    email per plant. Returns a small JSON summary. Wrong/missing secret ->
    403, nothing runs, nothing is revealed about why.
    """
    provided = (
        request.headers.get("X-Report-Secret")
        or request.query_params.get("secret")
        or (request.data.get("secret") if hasattr(request.data, "get") else None)
        or ""
    )
    expected = getattr(settings, "REPORT_CRON_SECRET", "")

    if not expected:
        log.error("trigger_daily_report: REPORT_CRON_SECRET is not set in the environment - refusing all requests")
        return Response({"detail": "Not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    # Constant-time comparison - avoids leaking the secret's length/prefix via timing.
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("trigger_daily_report: rejected request with invalid/missing secret")
        return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

    result = send_daily_consumption_reports()
    log.info(
        "trigger_daily_report: sent report for %s to %s admin(s) (%s plant reports)",
        result.get("date"), result.get("admins_notified"), result.get("plants_sent"),
    )
    return Response({"status": "ok", **result})
