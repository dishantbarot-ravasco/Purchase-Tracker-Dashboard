"""
apps/core/audit_log.py - the activity log: who signed in, what they changed,
what they downloaded and which pages they opened.

One append-only table, PTAuditLog, written from two places:

  1. Explicit calls to log_pt_action() for authentication and account events
     - every successful login (trusted-device fast path, new-device email-OTP
     verify, Google OAuth trusted-device path), every logout, password
     change, "log out everywhere", and every admin user-management mutation
     (account created/updated/deleted, trusted device revoked). These rows
     are kept for good.

  2. apps/services/activity_log.py, fed by config.middleware.
     ActivityLogMiddleware and the page-visit beacon (2026-10-01, owner:
     "keep track of users, their activity, what they are changing or
     interacting with on the dashboard"):
       ACTION_CHANGE      every POST/PUT/PATCH/DELETE under /api/ - what was
                          sent (passwords, codes and tokens redacted), the
                          status that came back, the plant, how long it took;
       ACTION_DOWNLOAD    a CSV export or an opened PO/invoice file;
       ACTION_PAGE_VIEW   a page opened (one row per page per five minutes);
       ACTION_AUTH_FAILED a refused sign-in, verification code or password
                          change, with the email that was tried.
     These ROUTINE_ACTIONS are pruned after RETENTION_DAYS (owner's choice,
     2026-10-01) by the nightly activity-log-prune schedule.

The per-feature history tables (MirChange, DomesticPOCorrection,
MaterialCorrection, MatchDismissal, ManualReceiptEdit, ...) stay the record
of WHAT a field was before and after. This log is the record of WHO did WHAT
WHEN across the whole dashboard; it links the two by time and user, it does
not copy their old/new values.

Rows are never updated. Only activity_log.prune_routine() deletes, and only routine rows
past the retention window. Shown to admins in admin.html's Activity Log tab
(apps/api/routers/activity_views.py) and read-only in Django admin.
"""

import logging

from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Model ────────────────────────────────────────────────────────────────

class PTAuditLog(models.Model):
    """Append-only activity log - one row per sign-in, change, download or
    page visit.

    Fields:
      timestamp    - UTC datetime of the action
      action       - one of the ACTION_* constants below
      actor_id     - PTUser.pk of the person who triggered the action
      actor_email  - denormalised for readability (survives account changes);
                     for ACTION_AUTH_FAILED, the email that was tried
      ip_address   - from X-Forwarded-For or REMOTE_ADDR (see
                     apps/services/device_service.py's get_client_ip)
      detail       - one readable sentence ("Posted MIR HRS/MIR/26-27/0012",
                     "Login - trusted device")
    Request rows (ACTION_CHANGE / DOWNLOAD / PAGE_VIEW / AUTH_FAILED) also
    fill method, path, route (the URL name), status_code, plant, user_agent,
    duration_ms and payload (the redacted request body).
    """

    ACTION_LOGIN = "login"
    ACTION_LOGOUT = "logout"
    ACTION_USER_CREATED = "user_created"
    ACTION_USER_UPDATED = "user_updated"
    ACTION_USER_DELETED = "user_deleted"
    ACTION_DEVICE_REVOKED = "device_revoked"
    ACTION_SESSIONS_REVOKED = "sessions_revoked"
    ACTION_AUTH_FAILED = "auth_failed"
    ACTION_CHANGE = "change"
    ACTION_DOWNLOAD = "download"
    ACTION_PAGE_VIEW = "page_view"

    ACTION_CHOICES = [
        (ACTION_LOGIN, "Login"),
        (ACTION_LOGOUT, "Logout"),
        (ACTION_USER_CREATED, "User created"),
        (ACTION_USER_UPDATED, "User updated"),
        (ACTION_USER_DELETED, "User deleted"),
        (ACTION_DEVICE_REVOKED, "Trusted device revoked"),
        (ACTION_SESSIONS_REVOKED, "All sessions revoked (log out everywhere)"),
        (ACTION_AUTH_FAILED, "Sign-in refused"),
        (ACTION_CHANGE, "Change"),
        (ACTION_DOWNLOAD, "Download"),
        (ACTION_PAGE_VIEW, "Page visit"),
    ]

    # Pruned after RETENTION_DAYS; every other action is kept for good.
    ROUTINE_ACTIONS = (ACTION_CHANGE, ACTION_DOWNLOAD, ACTION_PAGE_VIEW)
    RETENTION_DAYS = 90

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_email = models.CharField(max_length=254, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    detail = models.TextField(blank=True)
    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=300, blank=True)
    route = models.CharField(max_length=80, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    plant = models.CharField(max_length=20, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "pt_audit_log"
        managed = True
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["actor_id", "timestamp"]),
            models.Index(fields=["action", "timestamp"]),
        ]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} by {self.actor_email or 'unknown'}"


# ── Helper ───────────────────────────────────────────────────────────────

def log_pt_action(request, action, detail="", actor=None):
    """Write one audit row. Never raises - failures are logged and
    swallowed so a broken audit system can't block a real login/logout.

    Usage:
        log_pt_action(request, PTAuditLog.ACTION_LOGIN, actor=user, detail='trusted device')
        log_pt_action(request, PTAuditLog.ACTION_LOGOUT, actor=request.user)

    Args:
        request - DRF/Django request (provides IP always, and the actor
                  too when `actor` isn't passed explicitly)
        action  - one of PTAuditLog.ACTION_* constants
        detail  - optional free-text annotation (which login path, etc.)
        actor   - PTUser instance to credit. Required at every login call
                  site (request.user is still anonymous - the JWT that
                  would authenticate it doesn't exist until *after* login
                  succeeds). Falls back to request.user for logout, which
                  runs while the access cookie is still valid.
    """
    from apps.services.device_service import get_client_ip

    try:
        user = actor if actor is not None else getattr(request, "user", None)
        PTAuditLog.objects.create(
            action=action,
            actor_id=user.pk if user and getattr(user, "is_authenticated", True) else None,
            actor_email=getattr(user, "email", "") or "",
            ip_address=get_client_ip(request),
            detail=detail,
        )
    except Exception:
        logger.exception("audit_log: failed to write audit row (action=%s)", action)
