"""
apps/core/audit_log.py — Lightweight audit trail for authentication events.

Ported from the TDS Automation App's apps/core/audit_log.py, scoped down to
what this app actually has: Purchase Tracker is a read-only reporting/
reconciliation dashboard (no create/approve/decline/delete workflow the way
TDS has), so only ACTION_LOGIN and ACTION_LOGOUT exist here. Don't add
CREATE/UPDATE/APPROVE/DELETE-style actions unless a real mutating endpoint
is added first - see CLAUDE.md's note on not copying TDS's domain-specific
scope, only its architectural scaffolding.

WHAT IT LOGS
  Every successful login (trusted-device fast path, new-device email-OTP
  verify, and Google OAuth trusted-device path) and every logout. The log
  is append-only - rows are never updated or deleted.

SETUP
  1. Migration already created for this table (apps/core/migrations/) -
     run `manage.py migrate` if it hasn't been applied yet.
  2. Call log_pt_action() from the three login call sites and the logout
     view - already wired in apps/api/auth_views.py, apps/api/routers/
     device_views.py and apps/api/routers/google_oauth_views.py.
  3. Registered read-only in apps/core/admin.py.
"""

import logging

from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Model ────────────────────────────────────────────────────────────────

class PTAuditLog(models.Model):
    """Append-only audit trail - one row per login/logout event.

    Fields:
      timestamp    — UTC datetime of the action
      action       — one of the ACTION_* constants below
      actor_id     — PTUser.pk of the person who triggered the action
      actor_email  — denormalised for readability (survives account changes)
      ip_address   — from X-Forwarded-For or REMOTE_ADDR (see
                     apps/services/device_service.py's get_client_ip)
      detail       — free-text (e.g. which login path: trusted device,
                     new device OTP, Google OAuth)
    """

    ACTION_LOGIN = "login"
    ACTION_LOGOUT = "logout"

    ACTION_CHOICES = [
        (ACTION_LOGIN, "Login"),
        (ACTION_LOGOUT, "Logout"),
    ]

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_email = models.CharField(max_length=254, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        db_table = "pt_audit_log"
        managed = True
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["actor_id", "timestamp"]),
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
        request — DRF/Django request (provides IP always, and the actor
                  too when `actor` isn't passed explicitly)
        action  — one of PTAuditLog.ACTION_* constants
        detail  — optional free-text annotation (which login path, etc.)
        actor   — PTUser instance to credit. Required at every login call
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
