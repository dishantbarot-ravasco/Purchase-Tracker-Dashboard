"""
apps/api/routers/users_views.py — In-app user management (Admin Panel).

Ported from the TDS Automation App's apps/api/routers/users_views.py, same
design: a real create/edit/activate/deactivate UI backed by these endpoints,
instead of routing every user-management action through Django Admin or
`manage.py create_pt_user`. `create_pt_user` still exists and still works
(it's the only way to create the very first account, before any admin
exists to use this panel) - this doesn't replace it, it adds a normal path
for every account after that first one.

Endpoints
---------
GET   /api/auth/users             List all users. Admin only.
POST  /api/auth/users/create      Create a user (email + password + role +
                                   plants). Admin only. Bcrypt-hashes the
                                   password server-side - the plaintext never
                                   touches the database.
PATCH /api/auth/users/<id>        Update role / is_active / full_name /
                                   designation / plants / password. Admin
                                   only.
DELETE /api/auth/users/<id>       Permanently delete the account (same view
                                   as PATCH, branches on request.method).
                                   Admin only, AND only when the caller is
                                   dishant.barot@ravasco.com (hardcoded, per
                                   project owner's explicit request - every
                                   other admin only sees Deactivate).

URL naming (see CLAUDE.md's "In-app user management"): GET and POST are
split by path segment (`/auth/users` vs. `/auth/users/create`), not by
trailing slash alone the way TDS's own users_urls.py disambiguates them -
deliberately clearer/less fragile, not a functional deviation from the
pattern being ported.

Deliberately NOT ported from TDS, and why
------------------------------------------
- Signature upload/delete: TDS-document-specific (a TDS PDF is signed by its
  creator), meaningless for a read-only reconciliation dashboard. Not
  applicable here at all, not a gap.

Diverges from TDS (added 2026-09-04): password reset from this panel
----------------------------------------------------------------------
TDS's own update_user() has no password field - this app's did not either,
until now. An existing user's password could previously only be reset via
`manage.py create_pt_user` (CLI access required), which the project owner
asked to fix so an admin never needs shell/terminal access just to help a
locked-out colleague. update_user() below now accepts an optional
`password` field (min 8 chars, bcrypt-hashed the same way create_user()
hashes a new account's password) - omitted or blank means "leave the
current password alone", never accidentally cleared. `manage.py
create_pt_user` still exists and still works (it remains the only way to
create/reset the very first account, before any admin exists to use this
panel at all) - this doesn't replace it, it adds a normal in-app path for
every reset after that first account exists.
"""

import logging

import bcrypt
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.api.permissions import AdminWriteThrottle, IsAdmin, is_allowed_email_domain
from apps.core.audit_log import PTAuditLog, log_pt_action
from apps.core.models import PTUser, TrustedDevice

logger = logging.getLogger(__name__)

_VALID_ROLES = {choice[0] for choice in PTUser.Role.choices}
# The lowercase plant keys from frontend/js/shared.js's PLANTS map - not
# SyncRun.Plant's uppercase enum, see PTUser.plants' own docstring.
_VALID_PLANTS = {"hrs", "achhad", "vapi"}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate_password_strength(password: str, email: str) -> None:
    """Modest strength check beyond plain length - deliberately not a full
    breach-list/entropy policy (this is an internal tool bootstrapped by an
    admin, not a public signup form), just closing the most obviously weak
    cases a length-only check lets through: an 8-character password made
    entirely of the user's own email local-part, or a purely numeric string
    (e.g. "12345678"), or anything under 10 characters. Raises
    ValidationError (400) the same way the existing length check does."""
    if len(password) < 10:
        raise ValidationError({"detail": "Password must be at least 10 characters."})
    if password.isdigit():
        raise ValidationError({"detail": "Password must not be entirely numeric."})
    local_part = (email or "").split("@")[0].lower()
    if local_part and password.lower() == local_part:
        raise ValidationError({"detail": "Password must not be the same as your email address."})


def _clean_plants(raw) -> list:
    """Validates an incoming `plants` array against _VALID_PLANTS. `None`/
    omitted is left to the caller (means "don't change this field" on
    update, "default to []" on create) - this only runs when a value was
    actually provided."""
    if not isinstance(raw, list):
        raise ValidationError({"detail": "plants must be a list of plant keys."})
    invalid = [p for p in raw if p not in _VALID_PLANTS]
    if invalid:
        raise ValidationError({"detail": f"Unknown plant(s) {invalid}; must be one of {sorted(_VALID_PLANTS)}."})
    return raw


def _hash_password(plain: str) -> str:
    """Bcrypt-hash a plaintext password - same call shape as
    `manage.py create_pt_user` and apps/api/auth_backend.py's verify side,
    just the create direction. rounds=12 pinned explicitly (bcrypt's own
    library default today, confirmed by apps/api/auth_backend.py's
    dummy-hash constant) so a future bcrypt version changing its default
    can't silently weaken/strengthen this without a deliberate decision."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _user_out(u: PTUser, corrections_by_email=None) -> dict:
    return {
        "userId": u.user_id,
        "email": u.email,
        "fullName": u.full_name or "",
        "role": u.role,
        "designation": u.designation or "",
        "plants": u.plants or [],
        "isActive": u.is_active,
        "createdAt": u.created_at.isoformat() if u.created_at else None,
        "lastLoginAt": u.last_login_at.isoformat() if u.last_login_at else None,
        # The Admin Panel's redesigned Users cards (2026-09-07) show this
        # alongside Last Login - the closest real analogue this app has to
        # TDS's own per-user "TDS Made" card stat. 0 for a brand-new/never-
        # corrected account, not omitted, so the card always has a number.
        "correctionsCount": (corrections_by_email or {}).get(u.email, 0),
    }


# ── User list/create/update endpoints ─────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAdmin])
def list_users(request):
    """GET /api/auth/users - the Admin Panel's user list. Admin only."""
    from apps.api.routers.admin_overview_views import correction_counts_by_email

    users = PTUser.objects.order_by("email")
    corrections_by_email = correction_counts_by_email()
    return Response({"users": [_user_out(u, corrections_by_email) for u in users]})


@api_view(["POST"])
@permission_classes([IsAdmin])
@throttle_classes([AdminWriteThrottle])
def create_user(request):
    """POST /api/auth/users
    Body: { "email", "password", "fullName"?, "designation"?, "role"? }

    Admin only. New account starts active. Mirrors the same validation
    `manage.py create_pt_user` already enforces (domain check, role choice)
    plus what only makes sense at API-creation time (password length,
    duplicate-email check with a real 409 instead of a stack trace)."""
    data = request.data

    role = data.get("role") or PTUser.Role.VIEWER
    if role not in _VALID_ROLES:
        raise ValidationError({"detail": f"role must be one of {sorted(_VALID_ROLES)}"})

    full_name = (data.get("fullName") or "").strip()
    if not full_name:
        raise ValidationError({"detail": "Full name is required."})

    email = (data.get("email") or "").strip().lower()
    if not is_allowed_email_domain(email):
        raise ValidationError({"detail": f"Only @{settings.ALLOWED_EMAIL_DOMAIN} email addresses are allowed."})

    if PTUser.objects.filter(email=email).exists():
        return Response({"detail": "That email is already registered."}, status=409)

    password = data.get("password") or ""
    _validate_password_strength(password, email)

    # An admin's own access is never plant-scoped in practice (every
    # admin-only endpoint - sync_trigger, this file's own views - ignores
    # PTUser.plants entirely), but user_can_access_plant()/
    # user_can_edit_plant() don't special-case role at all - they'd still
    # honor a non-empty `plants` list against an admin account, which would
    # silently lock that admin out of correcting fields on an unscoped
    # plant. Forcing plants=[] for role=admin here closes that off at
    # creation time rather than relying on the frontend never sending one
    # (see admin-page.js's own role-change handler, which hides the Plants
    # section for this exact reason).
    plants = [] if role == PTUser.Role.ADMIN else (_clean_plants(data.get("plants")) if data.get("plants") is not None else [])

    user = PTUser.objects.create(
        email=email,
        password_hash=_hash_password(password),
        full_name=full_name,
        designation=(data.get("designation") or "").strip() or None,
        role=role,
        plants=plants,
        is_active=True,
    )
    logger.info("users_views: admin %s created PTUser %s (role=%s)", request.user.email, user.email, user.role)
    log_pt_action(
        request, PTAuditLog.ACTION_USER_CREATED, actor=request.user,
        detail=f"created {user.email} (role={user.role})",
    )
    return Response(_user_out(user), status=201)


# Hardcoded on purpose, per project owner's explicit request ("only for
# dishant.barot@ravasco.com email only hard code it") - deleting a PTUser is
# irreversible (unlike deactivate, which just blocks sign-in) and every other
# admin action in this file is available to any admin account, so this one
# extra gate is deliberately narrower than IsAdmin alone, not a bug.
_DELETE_USER_ALLOWED_EMAIL = "dishant.barot@ravasco.com"


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAdmin])
@throttle_classes([AdminWriteThrottle])
def update_user(request, user_id):
    """PATCH /api/auth/users/<id>
    Body: any of { "role", "isActive", "fullName", "designation", "plants",
    "password" }

    Admin only. No email field (identity, not editable) - see this file's
    header comment for the password field's own history.

    DELETE /api/auth/users/<id> - permanently delete the account. Admin only,
    AND only when the caller's own account is dishant.barot@ravasco.com
    (hardcoded, see comment above) - every other admin still sees
    Deactivate/Activate only, no Delete control at all. Shares this view
    (rather than a separate one) because both must live at the same URL -
    `path()` matches on URL alone, not HTTP method."""
    user = PTUser.objects.filter(pk=user_id).first()
    if not user:
        raise NotFound(f"User {user_id} not found.")

    if request.method == "DELETE":
        if request.user.email.lower() != _DELETE_USER_ALLOWED_EMAIL:
            raise PermissionDenied("Only dishant.barot@ravasco.com can delete a user account.")
        if user.role == PTUser.Role.ADMIN and user.is_active:
            other_active_admins = (
                PTUser.objects.filter(role=PTUser.Role.ADMIN, is_active=True).exclude(pk=user.pk).exists()
            )
            if not other_active_admins:
                raise ValidationError({"detail": "Cannot delete the last active admin account."})
        email = user.email
        user.delete()
        logger.info("users_views: admin %s deleted PTUser %s", request.user.email, email)
        log_pt_action(
            request, PTAuditLog.ACTION_USER_DELETED, actor=request.user,
            detail=f"deleted {email}",
        )
        return Response(status=204)

    # Captured before any field is mutated below, purely to build an audit
    # `detail` string afterwards - role changes and password resets are the
    # two security-sensitive cases worth calling out explicitly rather than
    # a generic "user updated" row (see log_pt_action() call at the bottom).
    prev_role, prev_is_active = user.role, user.is_active
    password_was_reset = False

    data = request.data
    if "password" in data and data["password"]:
        password = data["password"]
        _validate_password_strength(password, user.email)
        user.password_hash = _hash_password(password)
        password_was_reset = True

    new_role = user.role
    if "role" in data and data["role"] is not None:
        if data["role"] not in _VALID_ROLES:
            raise ValidationError({"detail": f"role must be one of {sorted(_VALID_ROLES)}"})
        new_role = data["role"]
    new_is_active = user.is_active
    if "isActive" in data and data["isActive"] is not None:
        new_is_active = bool(data["isActive"])

    was_active_admin = user.role == PTUser.Role.ADMIN and user.is_active
    stays_active_admin = new_role == PTUser.Role.ADMIN and new_is_active
    if was_active_admin and not stays_active_admin:
        other_active_admins = (
            PTUser.objects.filter(role=PTUser.Role.ADMIN, is_active=True).exclude(pk=user.pk).exists()
        )
        if not other_active_admins:
            raise ValidationError({"detail": "Cannot remove or deactivate the last active admin account."})

    user.role = new_role
    user.is_active = new_is_active
    if "fullName" in data and data["fullName"] is not None:
        new_full_name = data["fullName"].strip()
        if not new_full_name:
            raise ValidationError({"detail": "Full name is required."})
        user.full_name = new_full_name
    if "designation" in data and data["designation"] is not None:
        user.designation = data["designation"].strip() or None
    # See create_user()'s own comment on why an admin's `plants` is always
    # forced empty rather than trusted from the request body.
    if new_role == PTUser.Role.ADMIN:
        user.plants = []
    elif "plants" in data and data["plants"] is not None:
        user.plants = _clean_plants(data["plants"])
    user.save()

    logger.info("users_views: admin %s updated PTUser %s", request.user.email, user.email)

    detail_parts = []
    if new_role != prev_role:
        detail_parts.append(f"role: {prev_role} -> {new_role}")
    if new_is_active != prev_is_active:
        detail_parts.append(f"isActive: {prev_is_active} -> {new_is_active}")
    if password_was_reset:
        detail_parts.append("password reset")
    log_pt_action(
        request, PTAuditLog.ACTION_USER_UPDATED, actor=request.user,
        detail=f"updated {user.email}" + (f" ({'; '.join(detail_parts)})" if detail_parts else ""),
    )
    return Response(_user_out(user))


# ── Trusted devices (Admin Panel > Edit User > Trusted Devices) ───────────────
# Closes a real gap: notify_admins_new_device_login() (apps/services/
# device_service.py) already promises "...or revoke the device from the
# admin panel" in its own email body, but until now no such revoke existed
# anywhere - the only way to remove a TrustedDevice row was a manual DB
# delete. Admin-only, same as every other user-management endpoint in this
# file - a device is a credential for signing in AS a given account, so
# viewing/revoking one is exactly as sensitive as editing that account.

def _device_out(d: TrustedDevice) -> dict:
    return {
        "id": d.pk,
        "deviceName": d.device_name,
        "ipAddress": d.ip_address,
        "createdAt": d.created_at.isoformat() if d.created_at else None,
        "lastUsedAt": d.last_used_at.isoformat() if d.last_used_at else None,
    }
    # Deliberately never includes device_token_hash - even a hash of the
    # real credential has no reason to reach the frontend/an API response;
    # it exists purely for the server's own equality lookup.


@api_view(["GET"])
@permission_classes([IsAdmin])
def list_user_devices(request, user_id):
    """GET /api/auth/users/<id>/devices - every trusted device currently
    registered for this account. Admin only."""
    if not PTUser.objects.filter(pk=user_id).exists():
        raise NotFound(f"User {user_id} not found.")
    devices = TrustedDevice.objects.filter(user_id=user_id).order_by("-last_used_at")
    return Response({"devices": [_device_out(d) for d in devices]})


@api_view(["DELETE"])
@permission_classes([IsAdmin])
def revoke_user_device(request, user_id, device_id):
    """DELETE /api/auth/users/<id>/devices/<device_id> - revoke one trusted
    device. Admin only. That device's browser loses trust immediately (its
    pt_device cookie no longer matches any row) - its next login attempt
    (if the access/refresh cookies have also expired or are cleared) goes
    through the email-OTP challenge again, same as a genuinely new device."""
    device = TrustedDevice.objects.filter(pk=device_id, user_id=user_id).first()
    if not device:
        raise NotFound(f"Device {device_id} not found for user {user_id}.")

    user = device.user
    device_name = device.device_name
    device.delete()

    logger.info(
        "users_views: admin %s revoked device %r for PTUser %s",
        request.user.email, device_name, user.email,
    )
    log_pt_action(
        request, PTAuditLog.ACTION_DEVICE_REVOKED, actor=request.user,
        detail=f"revoked device {device_name!r} for {user.email}",
    )
    return Response(status=204)


@api_view(["POST"])
@permission_classes([IsAdmin])
@throttle_classes([AdminWriteThrottle])
def admin_logout_everywhere(request, user_id):
    """POST /api/auth/users/<id>/logout-everywhere - admin-driven equivalent
    of device_views.logout_everywhere_view's self-service panic button, for
    when an admin needs to kill a DIFFERENT account's sessions remotely
    (e.g. a reported compromise, an employee's device was lost/stolen).
    Admin only. Does not deactivate the account or change its password -
    those remain separate, deliberate actions via update_user()."""
    from apps.services.token_revocation import revoke_all_sessions

    user = PTUser.objects.filter(pk=user_id).first()
    if not user:
        raise NotFound(f"User {user_id} not found.")

    revoke_all_sessions(user)
    logger.info("users_views: admin %s revoked all sessions for PTUser %s", request.user.email, user.email)
    log_pt_action(
        request, PTAuditLog.ACTION_SESSIONS_REVOKED, actor=request.user,
        detail=f"revoked all sessions for {user.email}",
    )
    return Response({"status": "ok"})
