"""
apps/api/permissions.py - Custom DRF permission classes.

Ported from the TDS Automation App's apps/api/permissions.py (same design,
renamed for this app's role set):
  IsEditor  → role in ('admin', 'editor')
  IsAdmin   → role == 'admin'

'viewer' is intentionally excluded from both - a viewer can only read the
dashboard (search/view POs, materials, sync status), never anything that
writes (dismissing a flagged match once that endpoint exists, managing
users).
"""

from django.conf import settings
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission
from rest_framework.throttling import UserRateThrottle


def is_allowed_email_domain(email: str) -> bool:
    """True only for an email whose domain is exactly one of
    settings.ALLOWED_EMAIL_DOMAINS (case-insensitive) - "x@ravasco.com" yes,
    "x@evilravasco.com" and "x@ravasco.com.evil.io" no. Gates account
    creation and login so no address outside the company domains can ever
    have or use a PTUser account."""
    local, sep, domain = (email or "").strip().lower().rpartition("@")
    return bool(sep and local) and domain in settings.ALLOWED_EMAIL_DOMAINS


def allowed_domains_text() -> str:
    """"@ravasco.com or @hindustanrubbers.com" - for refusal messages."""
    return " or ".join("@" + d for d in settings.ALLOWED_EMAIL_DOMAINS)


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


def is_activity_log_owner(user) -> bool:
    """True only for the active account named by
    settings.ACTIVITY_LOG_OWNER_EMAIL - the activity log is private to it."""
    return (
        user is not None
        and bool(getattr(user, "is_authenticated", False))
        and bool(getattr(user, "is_active", False))
        and (getattr(user, "email", "") or "").strip().lower() == settings.ACTIVITY_LOG_OWNER_EMAIL
    )


class IsActivityLogOwner(BasePermission):
    """The activity log's reads. Anyone else gets a 404, not a 403, so the
    log's existence is not confirmed to other admins either."""

    def has_permission(self, request, view):
        if not is_activity_log_owner(request.user):
            raise NotFound()
        return True


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


class BlTrackThrottle(UserRateThrottle):
    """The BL "Track" lookup (imports_views.track_bl) makes a synchronous
    SafeCube call that can hold a gunicorn worker for its whole timeout, and
    production runs two sync workers - so a few repeated clicks could leave
    the app unresponsive. See DEFAULT_THROTTLE_RATES' "bl_track"."""

    scope = "bl_track"


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
    return user_can_access_plant(user, plant_key)


def user_can_access_plant(user, plant_key: str) -> bool:
    """True if `user` may READ `plant_key`'s data at all (added 2026-09-05,
    hardening pass, closing a gap flagged by a security review: every read
    endpoint used to be plain IsAuthenticated regardless of role/plant, so
    an editor explicitly scoped to e.g. ["hrs"] could still read every
    other plant's dashboard - inconsistent with what PTUser.plants'
    docstring already promises ("scopes which plants a user may edit"),
    which read as narrower than what was actually enforced).

    Same underlying check as user_can_edit_plant (which now delegates here)
    - there was never a real distinction in the logic, only in which call
    sites used it. Deliberately LOW blast-radius: an empty `plants` list
    still means "all plants" (see that field's docstring), so this only
    changes behavior for accounts an admin has already explicitly scoped to
    specific plants - the vast majority of accounts (empty plants list) see
    no change at all. Call this directly from a read view's body and return
    403 (single-plant endpoints) or filter results (cross-plant endpoints
    like imports_views.py's purchase_orders) on False - do not make this a
    DRF BasePermission subclass, for the same URL-kwarg-vs-router-constant
    reason user_can_edit_plant's own docstring already explains."""
    plants = getattr(user, "plants", None) or []
    return not plants or plant_key in plants
