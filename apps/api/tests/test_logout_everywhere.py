"""
Integration tests for "log out everywhere" (added 2026-09-05, hardening
pass) - apps/services/token_revocation.py's revoke_all_sessions(), wired to
two endpoints: the self-service POST /api/auth/logout-everywhere
(device_views.py) and the admin-driven
POST /api/auth/users/<id>/logout-everywhere (users_views.py). Both bump
PTUser.token_version, which apps/api/auth_backend.py's
PTJWTAuthentication.get_user() and apps/api/auth_serializers.py's
PTTokenRefreshSerializer both check on every request/refresh - see
PTUser.token_version's own docstring for why this instantly invalidates
every previously issued access AND refresh token, not just ones this
session happens to know about.
"""

import re

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import TrustedDevice

LOGIN_URL = "/api/auth/login"
DEVICE_VERIFY_URL = "/api/auth/device-verify"
LOGOUT_EVERYWHERE_URL = "/api/auth/logout-everywhere"
PO_LIST_URL = "/api/purchase-orders"


def _extract_otp_from_outbox():
    body = mail.outbox[-1].body
    match = re.search(r"\b(\d{6})\b", body)
    assert match, f"No 6-digit OTP found in email body: {body!r}"
    return match.group(1)


@pytest.mark.django_db
class TestLogoutEverywhereSelfService:
    def setup_method(self):
        cache.clear()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def _full_login(self, client):
        client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        otp = _extract_otp_from_outbox()
        return client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")

    def test_revokes_every_other_session_and_clears_device_trust(self):
        """Two independent clients (e.g. laptop + phone) both fully log in
        and become trusted devices. Client A calls logout-everywhere - both
        A's own cookie AND B's already-issued access token must stop
        working immediately, and neither device stays trusted."""
        client_a = APIClient()
        client_b = APIClient()
        self._full_login(client_a)
        self._full_login(client_b)

        assert client_a.get(PO_LIST_URL).status_code == 200
        assert client_b.get(PO_LIST_URL).status_code == 200
        assert TrustedDevice.objects.filter(user_id=self.user.user_id).count() == 2

        response = client_a.post(LOGOUT_EVERYWHERE_URL)
        assert response.status_code == 200

        # A's own session is gone (cookie cleared server-side).
        assert client_a.get(PO_LIST_URL).status_code == 401
        # B's already-issued access token (never touched logout-everywhere
        # itself) must ALSO be rejected now - this is the whole point of
        # token_version over per-jti revocation, which wouldn't catch this.
        assert client_b.get(PO_LIST_URL).status_code == 401
        # Device trust cleared for both - a fresh login by either must
        # require the OTP challenge again, not skip straight to a JWT.
        assert not TrustedDevice.objects.filter(user_id=self.user.user_id).exists()

    def test_requires_authentication(self):
        client = APIClient()
        response = client.post(LOGOUT_EVERYWHERE_URL)
        assert response.status_code == 401


@pytest.mark.django_db
class TestLogoutEverywhereAdminDriven:
    def setup_method(self):
        cache.clear()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(email="target@ravasco.com", password=self.password)
        self.admin = make_user(email="admin@ravasco.com", role="admin")

    def test_admin_can_revoke_another_users_sessions(self):
        target_client = APIClient()
        target_client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        otp = _extract_otp_from_outbox()
        target_client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert target_client.get(PO_LIST_URL).status_code == 200

        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        response = admin_client.post(f"/api/auth/users/{self.user.user_id}/logout-everywhere")
        assert response.status_code == 200

        assert target_client.get(PO_LIST_URL).status_code == 401
        assert not TrustedDevice.objects.filter(user_id=self.user.user_id).exists()

    def test_non_admin_cannot_revoke_another_users_sessions(self):
        viewer = make_user(email="viewer2@ravasco.com", role="viewer")
        client = APIClient()
        client.force_authenticate(user=viewer)
        response = client.post(f"/api/auth/users/{self.user.user_id}/logout-everywhere")
        assert response.status_code == 403

    def test_unknown_user_returns_404(self):
        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        response = admin_client.post("/api/auth/users/999999/logout-everywhere")
        assert response.status_code == 404
