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
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from apps.api.permissions import IsAdmin, is_allowed_email_domain
from apps.core.models import PTUser

logger = logging.getLogger(__name__)

_VALID_ROLES = {choice[0] for choice in PTUser.Role.choices}
# The lowercase plant keys from frontend/js/shared.js's PLANTS map - not
# SyncRun.Plant's uppercase enum, see PTUser.plants' own docstring.
_VALID_PLANTS = {"hrs", "achhad", "vapi"}


# ── Internal helpers ──────────────────────────────────────────────────────────

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
    just the create direction."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _user_out(u: PTUser) -> dict:
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
    }


# ── User list/create/update endpoints ─────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAdmin])
def list_users(request):
    """GET /api/auth/users - the Admin Panel's user list. Admin only."""
    users = PTUser.objects.order_by("email")
    return Response({"users": [_user_out(u) for u in users]})


@api_view(["POST"])
@permission_classes([IsAdmin])
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

    email = (data.get("email") or "").strip().lower()
    if not is_allowed_email_domain(email):
        raise ValidationError({"detail": f"Only @{settings.ALLOWED_EMAIL_DOMAIN} email addresses are allowed."})

    if PTUser.objects.filter(email=email).exists():
        return Response({"detail": "That email is already registered."}, status=409)

    password = data.get("password") or ""
    if len(password) < 8:
        raise ValidationError({"detail": "Password must be at least 8 characters."})

    plants = _clean_plants(data.get("plants")) if data.get("plants") is not None else []

    user = PTUser.objects.create(
        email=email,
        password_hash=_hash_password(password),
        full_name=(data.get("fullName") or "").strip() or None,
        designation=(data.get("designation") or "").strip() or None,
        role=role,
        plants=plants,
        is_active=True,
    )
    logger.info("users_views: admin %s created PTUser %s (role=%s)", request.user.email, user.email, user.role)
    return Response(_user_out(user), status=201)


@api_view(["PATCH"])
@permission_classes([IsAdmin])
def update_user(request, user_id):
    """PATCH /api/auth/users/<id>
    Body: any of { "role", "isActive", "fullName", "designation", "plants",
    "password" }

    Admin only. No email field (identity, not editable) - see this file's
    header comment for the password field's own history."""
    user = PTUser.objects.filter(pk=user_id).first()
    if not user:
        raise NotFound(f"User {user_id} not found.")

    data = request.data
    if "password" in data and data["password"]:
        password = data["password"]
        if len(password) < 8:
            raise ValidationError({"detail": "Password must be at least 8 characters."})
        user.password_hash = _hash_password(password)

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
        user.full_name = data["fullName"].strip() or None
    if "designation" in data and data["designation"] is not None:
        user.designation = data["designation"].strip() or None
    if "plants" in data and data["plants"] is not None:
        user.plants = _clean_plants(data["plants"])
    user.save()

    logger.info("users_views: admin %s updated PTUser %s", request.user.email, user.email)
    return Response(_user_out(user))
