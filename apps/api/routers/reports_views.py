"""
apps/api/routers/reports_views.py — Endpoints for a free external scheduler
to trigger this app's time-based jobs (report emails + the one housekeeping
job that needs a periodic sweep), since:
  - Render's free web service plan has no built-in cron scheduler, and
  - Render's own Cron Jobs feature has a $1/month minimum (no free tier).

Instead, a free external pinger (cron-job.org) hits trigger_daily_report once
a day (e.g. 20:30 IST), trigger_monthly_report once a month (the 1st, any time
after 00:00 IST - see send_monthly_consumption_reports()'s own default month
selection), trigger_mismatch_report on whatever interval is chosen (e.g.
10:30 IST daily), and trigger_prune_revoked_tokens once a day. Because the
caller has no login session or JWT, all four are protected by a shared-secret
query param / header instead - REPORT_CRON_SECRET, set as a Render
environment variable and given only to the scheduler config, never to a
browser or the frontend. Four separate endpoints (not one with a mode flag)
since each runs on its own independent schedule with a real external
scheduler - one cron job per endpoint is simpler to configure than one job
with a parameter that must vary by call.

The daily report endpoint's auth scheme was ported byte-for-byte (auth
scheme only - the report itself is this app's own) from the TDS Automation
App's own apps/api/routers/reports_views.py - same reasoning, same
shared-secret scheme, don't diverge without a reason. The monthly report
endpoint (added 2026-09-08), the mismatch report endpoint, and
trigger_prune_revoked_tokens (both added 2026-09-09) all reuse the exact
same scheme, not a new one per endpoint.
"""
import hmac
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.services.consumption_report import send_daily_consumption_reports, send_monthly_consumption_reports
from apps.services.plant_mismatch_report import send_plant_mismatch_reports
from apps.services.token_revocation import prune_expired_revoked_tokens

log = logging.getLogger(__name__)


def _check_report_secret(request):
    """Returns None if the shared secret checks out, else the Response to
    return immediately (503 if REPORT_CRON_SECRET isn't configured at all,
    403 for a wrong/missing one - nothing is revealed about which). Shared
    by both trigger_daily_report/trigger_monthly_report below - same check,
    same secret, don't diverge per endpoint."""
    provided = (
        request.headers.get("X-Report-Secret")
        or request.query_params.get("secret")
        or (request.data.get("secret") if hasattr(request.data, "get") else None)
        or ""
    )
    expected = getattr(settings, "REPORT_CRON_SECRET", "")

    if not expected:
        log.error("reports_views: REPORT_CRON_SECRET is not set in the environment - refusing all requests")
        return Response({"detail": "Not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    # Constant-time comparison - avoids leaking the secret's length/prefix via timing.
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("reports_views: rejected request with invalid/missing secret")
        return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    return None


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
    denied = _check_report_secret(request)
    if denied is not None:
        return denied

    result = send_daily_consumption_reports()
    log.info(
        "trigger_daily_report: sent report for %s to %s admin(s) (%s plant reports)",
        result.get("date"), result.get("admins_notified"), result.get("plants_sent"),
    )
    return Response({"status": "ok", **result})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def trigger_monthly_report(request):
    """
    GET/POST /api/internal/send-monthly-report?secret=<REPORT_CRON_SECRET>
    (or header 'X-Report-Secret: <REPORT_CRON_SECRET>')

    Runs send_monthly_consumption_reports() and emails every active admin,
    one email per plant, for the most recently completed calendar month
    (see that function's own docstring - meant to be hit once, on the 1st of
    each month). Optional `year`/`month` query params override the target
    month (e.g. for a manual re-send) - both must be provided together, as
    integers, or the request is rejected with 400 rather than silently
    guessing which one was meant.
    """
    denied = _check_report_secret(request)
    if denied is not None:
        return denied

    year_param = request.query_params.get("year")
    month_param = request.query_params.get("month")
    if bool(year_param) != bool(month_param):
        return Response({"detail": "Provide both `year` and `month`, or neither."}, status=status.HTTP_400_BAD_REQUEST)
    year = month = None
    if year_param and month_param:
        try:
            year, month = int(year_param), int(month_param)
        except ValueError:
            return Response({"detail": "`year`/`month` must be integers."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12):
            return Response({"detail": "`month` must be between 1 and 12."}, status=status.HTTP_400_BAD_REQUEST)

    result = send_monthly_consumption_reports(year=year, month=month)
    log.info(
        "trigger_monthly_report: sent report for %s to %s admin(s) (%s plant reports)",
        result.get("month"), result.get("admins_notified"), result.get("plants_sent"),
    )
    return Response({"status": "ok", **result})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def trigger_mismatch_report(request):
    """
    GET/POST /api/internal/send-mismatch-report?secret=<REPORT_CRON_SECRET>
    (or header 'X-Report-Secret: <REPORT_CRON_SECRET>')

    Runs send_plant_mismatch_reports() - emails each plant's own plant head
    (not the internal admin list) its currently-flagged PO<->MIR and
    MIR<->Stock quantity/rate mismatches, CC'ing every active admin (see
    apps/services/plant_mismatch_report.py's own module docstring). A plant
    with nothing currently flagged is skipped entirely. No built-in cadence
    here - set up whatever interval you want on the external scheduler
    (daily/weekly), same shared-secret scheme as the other two endpoints.

    Optional `test_recipient` query param redirects all 3 emails to that one
    address instead of the real plant heads, with no admin CC - for testing
    without ever reaching a real plant head's inbox (see
    send_plant_mismatch_reports()'s own docstring).
    """
    denied = _check_report_secret(request)
    if denied is not None:
        return denied

    result = send_plant_mismatch_reports(test_recipient=request.query_params.get("test_recipient") or None)
    log.info(
        "trigger_mismatch_report: sent %s plant report(s), skipped %s with nothing flagged",
        result.get("plants_sent"), result.get("plants_skipped_no_mismatches"),
    )
    return Response({"status": "ok", **result})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def trigger_prune_revoked_tokens(request):
    """
    GET/POST /api/internal/prune-revoked-tokens?secret=<REPORT_CRON_SECRET>
    (or header 'X-Report-Secret: <REPORT_CRON_SECRET>')

    Deletes RevokedRefreshToken rows past their own expiry (same logic as
    manage.py prune_revoked_tokens - both call the shared
    apps/services/token_revocation.prune_expired_revoked_tokens()). Added
    2026-09-09 to close the one real scheduling gap left after the
    daily/monthly/mismatch reports above: this table had nothing that ever
    deleted a row, and no scheduler (internal or external) ever called this
    command. Same shared-secret scheme as the other three endpoints in this
    file - reuses REPORT_CRON_SECRET rather than introducing a second secret
    for one more low-stakes internal job. Cheap and idempotent - safe on any
    interval (daily is more than enough for a table that only grows from
    token rotation/logout events).
    """
    denied = _check_report_secret(request)
    if denied is not None:
        return denied

    deleted = prune_expired_revoked_tokens()
    log.info("trigger_prune_revoked_tokens: pruned %s expired row(s)", deleted)
    return Response({"status": "ok", "deleted": deleted})
