"""
apps/api/tests/factories.py — Minimal fixture builders shared by tests.

Much simpler than the TDS app's equivalent: PTUser has no reference-catalog
FK graph to build for an auth test (no Purpose/BeltType/Brand/... chain).
"""

import bcrypt

from apps.core.models import PTUser


def make_user(email="viewer@ravasco.com", password="Str0ngPassw0rd!", role="viewer", **extra):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return PTUser.objects.create(email=email, password_hash=hashed, role=role, is_active=True, **extra)
