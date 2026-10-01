"""
/api/activity/... - the activity log (2026-10-01). The rules live in
apps/services/activity_log.py; this file only gates, parses and serializes.

Access:
  - POST activity/page-view: every signed-in account, for its own visits
    only (the row is always credited to request.user). auth.js calls it once
    per page load.
  - Everything else: ONLY the account named by
    settings.ACTIVITY_LOG_OWNER_EMAIL (owner, 2026-10-01: "I need the
    activity log only for me and private"). Every other account, other
    admins included, gets a 404 (permissions.IsActivityLogOwner). The log
    spans every plant and every account, so there is no plant scoping
    beyond that.
"""

import io

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import IsActivityLogOwner
from apps.api.routers._domestic_base import SafeCsvWriter
from apps.services import activity_log

PAGE_SIZE = 100
EXPORT_LIMIT = 50_000


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def page_view(request):
    """Body: {"page": "<auth.js nav key>"}. 202 whether or not a row was
    written (a repeat visit within five minutes is not)."""
    written = activity_log.record_page_view(request, str((request.data or {}).get("page") or ""))
    return Response({"recorded": written}, status=202)


@api_view(["GET"])
@permission_classes([IsActivityLogOwner])
def activity(request):
    """One page of the log, newest first. Filters: ?actor= ?group= ?q=
    ?since= ?until= (see activity_log.filtered()); ?page= from 1."""
    qs = activity_log.filtered(request.query_params)
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    total = qs.count()
    rows = list(qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])
    names = activity_log.user_names(r.actor_id for r in rows)
    return Response({
        "rows": [activity_log.serialize(r, names) for r in rows],
        "total": total, "page": page, "pageSize": PAGE_SIZE,
        "groups": [{"key": k, "label": v[0]} for k, v in activity_log.GROUPS.items()],
    })


@api_view(["GET"])
@permission_classes([IsActivityLogOwner])
def activity_people(request):
    """Every account: last active, last saved work, last full sign-in and
    30-day counts, plus `trackingSince` - the counts start there."""
    since = activity_log.tracking_since()
    return Response({"people": activity_log.people(days=30), "days": 30,
                     "trackingSince": since.isoformat() if since else None})


@api_view(["GET"])
@permission_classes([IsActivityLogOwner])
def activity_export(request):
    """The filtered log as CSV (same filters as `activity`), newest first,
    at most EXPORT_LIMIT rows."""
    qs = activity_log.filtered(request.query_params)[:EXPORT_LIMIT]
    rows = list(qs)
    names = activity_log.user_names(r.actor_id for r in rows)
    buffer = io.StringIO()
    writer = SafeCsvWriter(buffer)
    writer.writerow(["When (IST)", "Who", "Email", "Type", "What", "Plant", "Result", "Method", "Path", "IP",
                     "Duration ms"])
    tz = timezone.get_current_timezone()
    for r in rows:
        writer.writerow([
            timezone.localtime(r.timestamp, tz).strftime("%Y-%m-%d %H:%M:%S"), names.get(r.actor_id, ""),
            r.actor_email, r.get_action_display(), r.detail, r.plant, r.status_code or "", r.method, r.path,
            r.ip_address or "", r.duration_ms if r.duration_ms is not None else "",
        ])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="activity-log-{timezone.localdate():%Y-%m-%d}.csv"'
    return response
