"""
apps/services/activity_log.py - turns a request into an activity-log row
(apps/core/audit_log.py's PTAuditLog), and reads the log back for the admin
Activity Log tab (2026-10-01).

WHAT IS RECORDED (config.middleware.ActivityLogMiddleware calls
record_request() after every /api/ view):
  - every POST/PUT/PATCH/DELETE: ACTION_CHANGE, whether it was accepted or
    refused - a refused MIR post is as much "what they tried" as a saved one;
  - a response that is a file (Content-Disposition: attachment) or an opened
    PO/invoice file: ACTION_DOWNLOAD;
  - a refused sign-in step (wrong password, wrong code, locked account):
    ACTION_AUTH_FAILED, credited to the email that was tried.
The page-visit beacon (record_page_view()) adds ACTION_PAGE_VIEW.

WHAT IS NOT:
  - plain reads (GET) other than downloads - the dashboard polls and filters
    constantly, and the page visit already says what was looked at;
  - previews (the MIR and stock forms preview through the server on every
    keystroke), token refresh/verify, health checks, the cron triggers,
    and saved sort presets;
  - successful /api/auth/ calls, because the auth views already write their
    own explicit row (log_pt_action: login, logout, user created/updated,
    device revoked) and a second row would double-count them.

REDACTION. The request body is stored so an admin can see what was changed,
but any key that names a password, code, OTP, token or secret is replaced
with "***" at every depth, long strings are cut to _MAX_STR characters, long
lists to _MAX_ITEMS, and an oversized body keeps only its field names.
Uploaded files are recorded by name and size, never content.

Writing never raises: a broken log must not break the request it describes.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

from django.db.models import Count, Max, Q, QuerySet
from django.utils import timezone

logger = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes never logged (URL names, apps/api/urls.py).
_SKIP_ROUTES = {
    "token-refresh", "token-verify", "health", "health-ready", "auth-me",
    "activity-page-view", "sort-presets", "sort-preset",
    "google-login", "google-callback",
}
# Any route whose name contains one of these is skipped too.
_SKIP_FRAGMENTS = ("preview", "trigger-")

# Sign-in steps: a refusal is ACTION_AUTH_FAILED; a success is already
# logged by the view itself (or is only "code sent", not a sign-in yet).
_AUTH_FLOW_ROUTES = {
    "auth-login", "device-verify", "google-session-token",
    "change-password-request", "change-password-confirm",
}

_PLANT_PREFIXES = {"hrs": "HRS", "achhad": "RTP-ACHHAD", "vapi": "RTP-VAPI"}
_PLANT_KEYS = {"hrs": "HRS", "achhad": "RTP-ACHHAD", "vapi": "RTP-VAPI", "rtp-achhad": "RTP-ACHHAD",
               "rtp-vapi": "RTP-VAPI"}

# Readable sentence per route, with the plant prefix ("hrs-", "achhad-",
# "vapi-", "imports-") taken off first. An unlisted route falls back to
# "<METHOD> <path>", so a new endpoint is logged before anyone labels it.
ROUTE_LABELS = {
    # MIR entry
    "mir-post": "Posted a MIR",
    "mir-cancel": "Cancelled a MIR",
    "mir-edit": "Edited a MIR",
    "mir-reject-line": "Recorded a rejection on a MIR",
    "mir-resolve": "Resolved a MIR mismatch",
    "mir-close-line": "Short-closed a PO line",
    "mir-reopen-line": "Reopened a PO line",
    "mir-review-line": "Confirmed a changed PO line",
    "mir-material-category": "Changed a material's category",
    # Files
    "po-document-upload": "Uploaded a PO file",
    "document-withdraw": "Withdrew a file",
    "mir-invoice": "Attached an invoice to a MIR",
    "document-open": "Opened a file",
    # RM store
    "stock-post": "Posted a stock voucher",
    "stock-cancel": "Cancelled a stock voucher",
    "stock-approve": "Approved a stock difference",
    "stock-reject": "Rejected a stock difference",
    "stock-settings": "Changed stock settings",
    "stock-material-units": "Changed a material's stock units",
    # Dashboard corrections and decisions
    "correct-field": "Corrected a PO field",
    "correct-material-field": "Corrected a material field",
    "set-mir-match": "Changed a PO line's MIR receipts",
    "dismiss-po-mir": "Dismissed a PO-MIR match",
    "dismiss-mir-stock": "Dismissed a MIR-stock match",
    "dismiss-flag": "Dismissed a flag",
    "sync-trigger": "Started a sync",
    "rodtep-sync-trigger": "Started a RoDTEP sync",
    "advance-license-sync-trigger": "Started an Advance Licence sync",
    "track-bl": "Tracked a bill of lading",
    "export-stock-snapshots": "Exported stock snapshots",
    "purchase-orders": "Exported purchase orders",
    # Account and sign-in (failures only - successes are logged by the views)
    "auth-login": "Sign-in refused",
    "device-verify": "Verification code refused",
    "google-session-token": "Google sign-in refused",
    "change-password-request": "Password change refused",
    "change-password-confirm": "Password change refused",
    "auth-logout": "Logout failed",
    "auth-logout-everywhere": "Log out everywhere failed",
    "users-create": "Create user refused",
    "users-update": "Update user refused",
    "users-devices-revoke": "Revoke device refused",
    "users-logout-everywhere": "Log user out everywhere refused",
    "activity-export": "Exported the activity log",
}

# Response fields that name what was acted on, in order of preference.
_REF_FIELDS = ("mirNo", "voucherNo", "poNumber", "fileName")

# The pages the beacon accepts (auth.js renderNavTabs() keys).
PAGES = {
    "home": "Home", "dashboard": "Dashboard", "search": "Search PO", "pofiles": "PO Files",
    "mir": "MIR Entry", "stock": "RM Store", "admin": "Admin Panel",
}
PAGE_VIEW_DEDUPE = datetime.timedelta(minutes=5)

_SECRET_KEY = re.compile(r"pass|otp|token|secret|credential|^code$|^key$", re.IGNORECASE)
_MAX_STR = 500
_MAX_ITEMS = 50
_MAX_PAYLOAD_CHARS = 8000

# Readable groups for the admin filter: {group: (label, actions)}.
GROUPS = {
    "signin": ("Sign-ins and sign-outs", ("login", "logout", "sessions_revoked")),
    "failed": ("Refused sign-ins", ("auth_failed",)),
    "account": ("User management", ("user_created", "user_updated", "user_deleted", "device_revoked")),
    "change": ("Changes", ("change",)),
    "download": ("Downloads", ("download",)),
    "page_view": ("Page visits", ("page_view",)),
}


# ── Redaction ─────────────────────────────────────────────────────────────


def redact(value, depth: int = 0):
    """A copy of a request body that is safe to keep: secrets masked, long
    values cut. Never raises on odd input - it is already-parsed data."""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:_MAX_ITEMS]:
            key = str(k)
            out[key] = "***" if _SECRET_KEY.search(key) else redact(v, depth + 1)
        if len(value) > _MAX_ITEMS:
            out["_more"] = len(value) - _MAX_ITEMS
        return out
    if isinstance(value, (list, tuple)):
        items = [redact(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            items.append(f"... {len(value) - _MAX_ITEMS} more")
        return items
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value), depth + 1)


def _fit(payload):
    """The redacted payload, or only its field names when it is too big."""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return {"_unreadable": True}
    if len(text) <= _MAX_PAYLOAD_CHARS:
        return payload
    if isinstance(payload, dict):
        return {"_truncated": True, "fields": sorted(payload)[:_MAX_ITEMS]}
    return {"_truncated": True}


def request_payload(request, body: bytes | None):
    """What the request sent, redacted. `body` is the raw JSON body the
    middleware read before the view (None for other content types); a
    multipart form is read back after the view from what DRF parsed."""
    content_type = (request.META.get("CONTENT_TYPE") or "").lower()
    if body and "json" in content_type:
        try:
            return _fit(redact(json.loads(body.decode("utf-8"))))
        except (ValueError, UnicodeDecodeError):
            return {"_unreadable": True}
    if "multipart" in content_type or "form-urlencoded" in content_type:
        fields = {}
        post = getattr(request, "_post", None)
        if post is not None:
            fields = {k: (post.getlist(k) if len(post.getlist(k)) > 1 else post.get(k)) for k in post}
        files = getattr(request, "_files", None)
        if files:
            fields["_files"] = [{"field": k, "name": f.name, "size": f.size} for k, f in files.items()]
        return _fit(redact(fields)) if fields else None
    return None


# ── Describing a request ──────────────────────────────────────────────────


def _bare_route(route: str) -> tuple[str, str]:
    """("vapi", "correct-field") for "vapi-correct-field"; ("", route) when
    the route has no plant prefix."""
    head, _, rest = route.partition("-")
    if rest and head in (*_PLANT_PREFIXES, "imports"):
        return head, rest
    return "", route


def plant_of(route: str, kwargs: dict, payload) -> str:
    prefix, _ = _bare_route(route)
    if prefix in _PLANT_PREFIXES:
        return _PLANT_PREFIXES[prefix]
    for source in (kwargs or {}, payload if isinstance(payload, dict) else {}):
        value = str(source.get("plant") or "").strip().lower()
        if value in _PLANT_KEYS:
            return _PLANT_KEYS[value]
    return ""


def _response_data(response):
    data = getattr(response, "data", None)
    return data if isinstance(data, dict) else {}


def describe(route: str, method: str, path: str, kwargs: dict, response) -> str:
    """One readable sentence: the route's label plus what it acted on."""
    _prefix, bare = _bare_route(route)
    label = ROUTE_LABELS.get(route) or ROUTE_LABELS.get(bare) or f"{method} {path}"
    data = _response_data(response)
    ref = next((str(data[k]) for k in _REF_FIELDS if data.get(k)), "")
    if not ref:
        kwargs = kwargs or {}
        if kwargs.get("po_number"):
            ref = f"PO {kwargs['po_number']}"
        elif kwargs.get("mir_id"):
            ref = f"MIR #{kwargs['mir_id']}"
        elif kwargs.get("voucher_id"):
            ref = f"voucher #{kwargs['voucher_id']}"
        elif kwargs.get("document_id"):
            ref = f"file #{kwargs['document_id']}"
    sentence = f"{label} - {ref}" if ref else label
    error = data.get("error") if getattr(response, "status_code", 200) >= 400 else None
    if isinstance(error, str) and error:
        sentence += f" (refused: {error[:200]})"
    return sentence


def _classify(route: str, method: str, response) -> str | None:
    """The action a request is logged as, or None to skip it."""
    from apps.core.audit_log import PTAuditLog

    if not route or route in _SKIP_ROUTES or any(f in route for f in _SKIP_FRAGMENTS):
        return None
    status = getattr(response, "status_code", 200)
    if route in _AUTH_FLOW_ROUTES:
        return PTAuditLog.ACTION_AUTH_FAILED if status >= 400 else None
    is_auth = route.startswith(("auth-", "users-", "device-"))
    if method in UNSAFE_METHODS:
        if is_auth and status < 400:
            return None
        return PTAuditLog.ACTION_CHANGE
    disposition = ""
    try:
        disposition = response.get("Content-Disposition", "") or ""
    except AttributeError:
        pass
    if route == "document-open" or "attachment" in disposition:
        return PTAuditLog.ACTION_DOWNLOAD if status < 400 else None
    return None


def _actor(request, payload):
    """(actor_id, actor_email). A refused sign-in has no user yet - it is
    credited to the email that was typed."""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False) and getattr(user, "pk", None):
        return user.pk, getattr(user, "email", "") or ""
    email = payload.get("email") if isinstance(payload, dict) else ""
    return None, str(email or "")[:254]


def record_request(request, response, *, body: bytes | None, started: float, now: float) -> None:
    """Write the row for one /api/ request if it is one worth keeping.
    Called by ActivityLogMiddleware after the view. Never raises."""
    try:
        match = getattr(request, "resolver_match", None)
        route = getattr(match, "url_name", "") or ""
        action = _classify(route, request.method, response)
        if action is None:
            return
        from apps.core.audit_log import PTAuditLog
        from apps.services.device_service import get_client_ip

        kwargs = dict(getattr(match, "kwargs", {}) or {})
        payload = request_payload(request, body) if request.method in UNSAFE_METHODS else None
        actor_id, actor_email = _actor(request, payload)
        PTAuditLog.objects.create(
            action=action, actor_id=actor_id, actor_email=actor_email, ip_address=get_client_ip(request) or None,
            detail=describe(route, request.method, request.path, kwargs, response)[:1000],
            method=request.method, path=request.get_full_path()[:300], route=route[:80],
            status_code=getattr(response, "status_code", None),
            plant=plant_of(route, kwargs, payload),
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:300],
            duration_ms=max(0, int((now - started) * 1000)), payload=payload,
        )
    except Exception:
        logger.exception("activity_log: failed to record %s %s", getattr(request, "method", "?"),
                         getattr(request, "path", "?"))


def record_page_view(request, page: str) -> bool:
    """One ACTION_PAGE_VIEW row for the signed-in user opening `page`, unless
    the same user opened the same page in the last PAGE_VIEW_DEDUPE (a
    reload or a back-button is not a new visit). Returns whether a row was
    written. Raises ValueError for a page the dashboard does not have."""
    from apps.core.audit_log import PTAuditLog
    from apps.services.device_service import get_client_ip

    if page not in PAGES:
        raise ValueError("Unknown page.")
    user = request.user
    since = timezone.now() - PAGE_VIEW_DEDUPE
    if PTAuditLog.objects.filter(action=PTAuditLog.ACTION_PAGE_VIEW, actor_id=user.pk, route=page,
                                 timestamp__gte=since).exists():
        return False
    PTAuditLog.objects.create(
        action=PTAuditLog.ACTION_PAGE_VIEW, actor_id=user.pk, actor_email=user.email or "",
        ip_address=get_client_ip(request) or None, detail=f"Opened {PAGES[page]}", method="GET",
        path=f"page:{page}", route=page, user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:300],
    )
    return True


# ── Retention ─────────────────────────────────────────────────────────────


def prune_routine(days: int | None = None) -> int:
    """Delete routine rows (changes, downloads, page visits) older than
    `days` (default PTAuditLog.RETENTION_DAYS). Sign-in and account rows are
    never pruned. Returns how many rows went."""
    from apps.core.audit_log import PTAuditLog

    days = PTAuditLog.RETENTION_DAYS if days is None else days
    cutoff = timezone.now() - datetime.timedelta(days=days)
    deleted, _ = PTAuditLog.objects.filter(action__in=PTAuditLog.ROUTINE_ACTIONS, timestamp__lt=cutoff).delete()
    return deleted


def scheduled_prune() -> int:
    """django-q2 entry point for the nightly activity-log-prune schedule."""
    deleted = prune_routine()
    logger.info("activity_log: pruned %d routine row(s) older than the retention window", deleted)
    return deleted


# ── Reading (admin Activity Log tab) ──────────────────────────────────────


def _parse_day(value):
    try:
        return datetime.date.fromisoformat(str(value)) if value else None
    except ValueError as exc:
        raise ValueError("Dates are YYYY-MM-DD.") from exc


def filtered(params) -> QuerySet:
    """The log filtered by the tab's controls: actor (user id), group (a
    GROUPS key), q (free text over who / what / path / IP), since / until
    (local dates, inclusive)."""
    from apps.core.audit_log import PTAuditLog

    qs = PTAuditLog.objects.all()
    actor = params.get("actor")
    if actor:
        try:
            qs = qs.filter(actor_id=int(actor))
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown user.") from exc
    group = params.get("group")
    if group:
        if group not in GROUPS:
            raise ValueError("Unknown activity type.")
        qs = qs.filter(action__in=GROUPS[group][1])
    since, until = _parse_day(params.get("since")), _parse_day(params.get("until"))
    tz = timezone.get_current_timezone()
    if since:
        qs = qs.filter(timestamp__gte=datetime.datetime.combine(since, datetime.time.min, tzinfo=tz))
    if until:
        qs = qs.filter(timestamp__lt=datetime.datetime.combine(until + datetime.timedelta(days=1), datetime.time.min,
                                                               tzinfo=tz))
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(actor_email__icontains=q) | Q(detail__icontains=q) | Q(path__icontains=q)
                       | Q(ip_address__icontains=q) | Q(plant__icontains=q))
    return qs.order_by("-timestamp", "-id")


def serialize(row, names: dict) -> dict:
    return {
        "id": row.id, "at": row.timestamp.isoformat(), "action": row.action, "actionLabel": row.get_action_display(),
        "actorId": row.actor_id, "actorEmail": row.actor_email, "actorName": names.get(row.actor_id, ""),
        "detail": row.detail, "method": row.method, "path": row.path, "route": row.route,
        "status": row.status_code, "plant": row.plant, "ip": row.ip_address or "", "userAgent": row.user_agent,
        "durationMs": row.duration_ms, "payload": row.payload,
    }


def user_names(ids) -> dict:
    from apps.core.models import PTUser

    ids = {i for i in ids if i}
    return {u.pk: (u.full_name or "") for u in PTUser.objects.filter(pk__in=ids).only("user_id", "full_name")}


def people(days: int = 30) -> list[dict]:
    """One row per account: last sign-in, last seen, and how many changes,
    downloads, page visits and refused sign-ins in the last `days`. Inactive
    accounts are included (an admin wants to see they stopped)."""
    from apps.core.audit_log import PTAuditLog
    from apps.core.models import PTUser

    since = timezone.now() - datetime.timedelta(days=days)
    stats = {
        r["actor_id"]: r for r in PTAuditLog.objects.filter(actor_id__isnull=False).values("actor_id").annotate(
            last_seen=Max("timestamp"),
            changes=Count("id", filter=Q(action=PTAuditLog.ACTION_CHANGE, timestamp__gte=since)),
            downloads=Count("id", filter=Q(action=PTAuditLog.ACTION_DOWNLOAD, timestamp__gte=since)),
            visits=Count("id", filter=Q(action=PTAuditLog.ACTION_PAGE_VIEW, timestamp__gte=since)),
            signins=Count("id", filter=Q(action=PTAuditLog.ACTION_LOGIN, timestamp__gte=since)),
        )
    }
    failed = dict(
        PTAuditLog.objects.filter(action=PTAuditLog.ACTION_AUTH_FAILED, timestamp__gte=since)
        .values_list("actor_email").annotate(n=Count("id")).values_list("actor_email", "n")
    )
    out = []
    for u in PTUser.objects.all().order_by("email"):
        s = stats.get(u.pk, {})
        last_seen = s.get("last_seen")
        out.append({
            "id": u.pk, "email": u.email, "name": u.full_name or "", "role": u.role, "active": u.is_active,
            "lastLogin": u.last_login_at.isoformat() if u.last_login_at else None,
            "lastSeen": last_seen.isoformat() if last_seen else None,
            "signins": s.get("signins", 0), "changes": s.get("changes", 0), "downloads": s.get("downloads", 0),
            "visits": s.get("visits", 0), "failed": failed.get(u.email, 0),
        })
    out.sort(key=lambda p: p["lastSeen"] or "", reverse=True)
    return out
