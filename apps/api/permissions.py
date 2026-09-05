"""
apps/api/permissions.py — Custom DRF permission classes.

Ported from the TDS Automation App's apps/api/permissions.py (same design,
renamed for this app's role set):
  IsEditor  → role in ('admin', 'editor')
  IsAdmin   → role == 'admin'

'viewer' is intentionally excluded from both — a viewer can only read the
dashboard (search/view POs, materials, sync status), never anything that
writes (dismissing a flagged match once that endpoint exists, managing
users).
"""

from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.throttling import UserRateThrottle


def is_allowed_email_domain(email: str) -> bool:
    """True only for an email ending in "@<settings.ALLOWED_EMAIL_DOMAIN>"
    (case-insensitive). Gates account creation and login so no address
    outside the company domain can ever have or use a PTUser account."""
    return (email or "").strip().lower().endswith("@" + settings.ALLOWED_EMAIL_DOMAIN.lower())


class IsEditor(BasePermission):
    """Allows access only to users with role 'admin' or 'editor'."""

    message = "Editor (admin or editor) role required."

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, "is_active", False))
            and getattr(user, "role", None) in ("admin", "editor")
        )


class IsAdmin(BasePermission):
    """Allows access only to users with role 'admin'."""

    message = "Admin role required."

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, "is_active", False))
            and getattr(user, "role", None) == "admin"
        )


class SyncTriggerThrottle(UserRateThrottle):
    """Stricter than the generic 200/min "user" bucket - each request queues
    a real background Drive-sync job (apps/services/sync_trigger.py), so an
    IsAdmin-gated but buggy/compromised client hammering this endpoint is a
    materially worse resource-exhaustion vector than an ordinary field
    correction hitting the same generic limit. Same "distinct scope, keyed
    the same way UserRateThrottle already keys by user pk" pattern
    auth_views.py's LoginRateThrottle uses for its own reason (per-account,
    not per-IP) - see config/settings.py's DEFAULT_THROTTLE_RATES for the
    actual "sync_trigger" rate."""

    scope = "sync_trigger"


class AdminWriteThrottle(UserRateThrottle):
    """Stricter than the generic 200/min "user" bucket for admin-only writes
    that create/modify accounts (apps/api/routers/users_views.py) - see
    config/settings.py's DEFAULT_THROTTLE_RATES for the actual
    "admin_write" rate."""

    scope = "admin_write"


def user_can_edit_plant(user, plant_key: str) -> bool:
    """True if `user` may PATCH a field correction for `plant_key`
    ("hrs"/"achhad"/"vapi" - the lowercase frontend/js/shared.js keys, not
    SyncRun.Plant's uppercase enum).

    Layered on top of, not instead of, the IsEditor permission class already
    gating every correct_field view - this only adds the per-plant scoping
    from PTUser.plants. An empty `plants` list means "all plants" (see that
    field's docstring), so every user who predates this scoping keeps full
    access. Not a DRF BasePermission subclass because the plant key usually
    isn't known until inside the view (a URL kwarg for Import, an implicit
    per-router constant for Domestic) - call this directly from the view
    body and return a 403 on False."""
    plants = getattr(user, "plants", None) or []
    return not plants or plant_key in plants
