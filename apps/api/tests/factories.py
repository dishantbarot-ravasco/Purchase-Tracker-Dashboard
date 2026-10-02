"""
apps/api/tests/factories.py - Minimal fixture builders shared by tests.

Much simpler than the TDS app's equivalent: PTUser has no reference-catalog
FK graph to build for an auth test (no Purpose/BeltType/Brand/... chain).
"""

import bcrypt

from apps.api.permissions import ALL_PERMISSIONS, PLANT_KEYS, Perm
from apps.core.models import PTUser


# Test shorthands for the two roles the app had before layered access
# (2026-10-02). PTUser.role is now "admin" or "user"; these build a "user"
# holding what that role used to mean, so a test reads as before:
#   "editor" - every permission, every plant (unless plants= is given)
#   "viewer" - every View permission, every plant (unless plants= is given)
#   "locked" - a user with no permissions and no plants, as migrated
# A test of the permissions themselves passes role="user" with explicit
# permissions= and plants=.
_SHORTHANDS = {
    "editor": list(ALL_PERMISSIONS),
    "viewer": [Perm.VIEW_DASHBOARD, Perm.VIEW_INVENTORY, Perm.VIEW_ON_ORDER, Perm.VIEW_STOCK_ORDERS],
}


def make_user(email="viewer@ravasco.com", password="Str0ngPassw0rd!", role="viewer", **extra):
    """Create and persist a PTUser with a real bcrypt password hash (matching
    how apps/api/auth_backend.py actually verifies logins), defaulting to an
    active viewer (see _SHORTHANDS). Extra kwargs (e.g. plants=[...]) pass
    straight through to PTUser.objects.create for per-test overrides."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    if role in _SHORTHANDS:
        extra.setdefault("permissions", list(_SHORTHANDS[role]))
        # The old roles' empty plants list meant every plant.
        if not extra.get("plants"):
            extra["plants"] = list(PLANT_KEYS)
        role = "user"
    elif role == "locked":
        role = "user"
    return PTUser.objects.create(email=email, password_hash=hashed, role=role, is_active=True, **extra)
