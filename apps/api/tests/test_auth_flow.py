"""
Integration tests for the login -> device-verify -> cookie -> protected
endpoint flow (apps/api/auth_views.py, device_views.py, auth_backend.py).

Ported from the TDS Automation App's apps/api/tests/test_auth_flow.py.
Covers the device-aware 2FA gate end to end: a brand-new device must pass
through email-OTP verification before it gets a JWT/httpOnly cookie, and a
device that has already verified once skips straight to a trusted login.
"""
import re

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import OTPCode, TrustedDevice

LOGIN_URL = "/api/auth/login"
DEVICE_VERIFY_URL = "/api/auth/device-verify"
LOGOUT_URL = "/api/auth/logout"
PO_LIST_URL = "/api/purchase-orders"
TOKEN_REFRESH_URL = "/api/auth/token/refresh"


def _extract_otp_from_outbox():
    body = mail.outbox[-1].body
    match = re.search(r"\b(\d{6})\b", body)
    assert match, f"No 6-digit OTP found in email body: {body!r}"
    return match.group(1)


@pytest.mark.django_db
class TestLogin:
    def setup_method(self):
        # DRF's login throttle is backed by Django's cache, which is NOT
        # part of the per-test DB transaction rollback - without this,
        # tests in this class trip each other's throttle counter.
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def test_wrong_password_returns_error(self):
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json")
        assert response.status_code == 400

    def test_unknown_email_does_not_leak_which_field_was_wrong(self):
        response = self.client.post(LOGIN_URL, {"email": "nobody@ravasco.com", "password": "whatever"}, format="json")
        assert response.status_code == 400

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 400

    def test_new_device_triggers_otp_challenge_not_a_jwt(self):
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 200
        assert response.data["status"] == "device_verify"
        assert "access_token" not in response.data
        assert len(mail.outbox) == 1
        assert OTPCode.objects.filter(email=self.user.email).exists()

    def test_login_throttle_is_scoped_per_email_not_shared_across_ip(self):
        """Regression test for a real bug (found and fixed 2026-09-04):
        LoginRateThrottle used to key on client IP, so one email's login
        attempts could exhaust the whole IP's 5/minute bucket and lock out
        every other account behind the same office/NAT IP. All requests in
        this test share one APIClient (one source IP) but two different
        emails - the second account's first attempt must not be throttled by
        the first account's attempts."""
        other_password = "An0therStr0ngPassw0rd!"
        other_user = make_user(email="other@ravasco.com", password=other_password)

        # Exhaust the first account's own 5/minute bucket with wrong passwords.
        for _ in range(5):
            response = self.client.post(
                LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json"
            )
            assert response.status_code == 400
        throttled = self.client.post(
            LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json"
        )
        assert throttled.status_code == 429

        # A different account, same client/IP, must still be able to log in.
        response = self.client.post(LOGIN_URL, {"email": other_user.email, "password": other_password}, format="json")
        assert response.status_code == 200
        assert response.data["status"] == "device_verify"


@pytest.mark.django_db
class TestDeviceVerifyAndTrustedLogin:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def _login_new_device(self):
        return self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")

    def test_full_new_device_flow_sets_cookies_and_grants_access(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()

        verify_response = self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert verify_response.status_code == 200
        assert verify_response.data["status"] == "ok"
        assert "access_token" in verify_response.data

        # httpOnly JWT + device-trust cookies must both be set.
        assert "pt_access" in verify_response.cookies
        assert "pt_device" in verify_response.cookies
        assert TrustedDevice.objects.filter(user_id=self.user.user_id).exists()

        # Cookie alone (no Authorization header) must now authenticate.
        protected = self.client.get(PO_LIST_URL)
        assert protected.status_code == 200

    def test_wrong_otp_code_is_rejected(self):
        self._login_new_device()
        response = self.client.post(DEVICE_VERIFY_URL, {"code": "000000"}, format="json")
        assert response.status_code == 400
        assert not TrustedDevice.objects.filter(user_id=self.user.user_id).exists()

    def test_device_verify_without_prior_login_session_is_rejected(self):
        response = self.client.post(DEVICE_VERIFY_URL, {"code": "123456"}, format="json")
        assert response.status_code == 401

    def test_otp_is_single_use(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()
        first = self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert first.status_code == 200

        # A fresh, still-unverified client (no tds_device cookie carried
        # over) trying the same code a second time must fail - the OTP row
        # was deleted on first success.
        second_client = APIClient()
        second_client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        replay = second_client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert replay.status_code == 400

    def test_trusted_device_skips_otp_on_next_login(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()
        self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        # First login's OTP + device_verify's "new device signed in"
        # notification (no admin-alert email - this fixture has no admin
        # user, so notify_admins_new_device_login's recipient list is empty).
        assert len(mail.outbox) == 2

        # Same client (carries the pt_device cookie now) logs in again.
        second_login = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert second_login.status_code == 200
        assert second_login.data["status"] == "ok"
        assert "access_token" in second_login.data
        # No new email of any kind for a trusted-device login.
        assert len(mail.outbox) == 2


@pytest.mark.django_db
class TestLogoutAndProtectedAccess:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def test_protected_endpoint_401s_with_no_cookie_or_header(self):
        response = self.client.get(PO_LIST_URL)
        assert response.status_code == 401

    def test_logout_clears_access_cookie_and_revokes_access(self):
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        otp = _extract_otp_from_outbox()
        self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert self.client.get(PO_LIST_URL).status_code == 200

        logout_response = self.client.post(LOGOUT_URL)
        assert logout_response.status_code == 200
        assert self.client.get(PO_LIST_URL).status_code == 401
