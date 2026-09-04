"""
apps/api/tests/factories.py — Minimal fixture builders shared by tests.

Much simpler than the TDS app's equivalent: PTUser has no reference-catalog
FK graph to build for an auth test (no Purpose/BeltType/Brand/... chain).
"""

import bcrypt

from apps.core.models import PTUser


def make_user(email="viewer@ravasco.com", password="Str0ngPassw0rd!", role="viewer", **extra):
    """Create and persist a PTUser with a real bcrypt password hash (matching
    how apps/api/auth_backend.py actually verifies logins), defaulting to an
    active viewer. Extra kwargs (e.g. plants=[...]) pass straight through to
    PTUser.objects.create for per-test overrides."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return PTUser.objects.create(email=email, password_hash=hashed, role=role, is_active=True, **extra)
