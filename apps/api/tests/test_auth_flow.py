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
    """Pull the 6-digit OTP out of the most recently sent email's body, so
    tests can drive device-verify without a real inbox."""
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
        """A correct email with the wrong password must be rejected with a plain 400."""
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json")
        assert response.status_code == 400

    def test_unknown_email_does_not_leak_which_field_was_wrong(self):
        """An email with no matching account gets the same generic 400 as a
        wrong password - the response must not reveal whether the account exists."""
        response = self.client.post(LOGIN_URL, {"email": "nobody@ravasco.com", "password": "whatever"}, format="json")
        assert response.status_code == 400

    def test_inactive_user_cannot_login(self):
        """A deactivated PTUser (admin toggled isActive off) must be refused
        login even with the correct password."""
        self.user.is_active = False
        self.user.save()
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 400

    def test_new_device_triggers_otp_challenge_not_a_jwt(self):
        """A first-time login from an unrecognized device must return the
        device_verify challenge (and email an OTP) instead of granting a JWT
        outright - the whole point of the device-aware 2FA gate."""
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
class TestAccountLockout:
    """Regression tests for the 2026-09-05 hardening pass:
    PTUser.failed_login_attempts/locked_until (apps/api/auth_backend.py's
    PTUserBackend.authenticate()) add a real account lockout on top of the
    existing LoginRateThrottle - a rate limit alone slows down guessing but
    never actually stops it. cache.clear() between attempts isolates this
    from LoginRateThrottle's own 5/minute bucket (already covered by
    TestLogin::test_login_throttle_is_scoped_per_email_not_shared_across_ip)
    so these tests exercise the lockout mechanism specifically, not the
    rate throttle."""

    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)
        # An admin recipient - see apps/services/security_alerts.py's
        # _admin_emails(); with none, the alert functions are still called
        # but no-op (nothing to assert on mail.outbox in that case).
        self.admin = make_user(email="admin-lockout-test@ravasco.com", role="admin")

    def test_locks_account_after_5_failed_attempts_and_rejects_even_correct_password(self):
        for _ in range(5):
            cache.clear()
            response = self.client.post(
                LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json"
            )
            assert response.status_code == 400

        self.user.refresh_from_db()
        assert self.user.locked_until is not None
        assert self.user.failed_login_attempts == 0  # reset once locked, not left at 5

        cache.clear()
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 400

    def test_lockout_sends_an_admin_alert_email(self):
        """Regression test for the 2026-09-05 hardening pass's alerting
        gap: an account locking out must actually notify an admin, not
        just silently write a log line nobody's watching."""
        for _ in range(5):
            cache.clear()
            self.client.post(LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json")

        alert_emails = [m for m in mail.outbox if "Account Locked" in m.subject]
        assert len(alert_emails) == 1
        assert self.admin.email in alert_emails[0].to
        assert self.user.email in alert_emails[0].body

    def test_successful_login_resets_the_failed_attempt_counter(self):
        cache.clear()
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": "wrong-password"}, format="json")
        self.user.refresh_from_db()
        assert self.user.failed_login_attempts == 1

        cache.clear()
        response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.failed_login_attempts == 0


@pytest.mark.django_db
class TestDeviceVerifyAndTrustedLogin:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def _login_new_device(self):
        """Drive the password step only, leaving the OTP challenge unanswered
        - shared setup for every test in this class that needs a pending
        device-verify session to exist."""
        return self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")

    def test_full_new_device_flow_sets_cookies_and_grants_access(self):
        """End-to-end: password login -> OTP verify -> httpOnly JWT + device-
        trust cookies are both set -> the cookie alone (no Authorization
        header) authenticates a protected endpoint."""
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
        """An incorrect 6-digit code must be rejected and must not create a
        TrustedDevice row - a wrong guess should never grant device trust."""
        self._login_new_device()
        response = self.client.post(DEVICE_VERIFY_URL, {"code": "000000"}, format="json")
        assert response.status_code == 400
        assert not TrustedDevice.objects.filter(user_id=self.user.user_id).exists()

    def test_device_verify_without_prior_login_session_is_rejected(self):
        """Hitting device-verify directly, with no pending login session
        (no prior password step), must 401 rather than accept a guessed code."""
        response = self.client.post(DEVICE_VERIFY_URL, {"code": "123456"}, format="json")
        assert response.status_code == 401

    def test_otp_is_single_use(self):
        """A correct OTP is consumed on first use (verify_otp deletes the row)
        - replaying the same code from a second, otherwise-independent login
        attempt must fail."""
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
        """Once a device has verified once (pt_device cookie set), a second
        login from the same client must skip straight to a JWT with no new
        OTP challenge and no new email - the entire point of device trust."""
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
        """A completely anonymous request (no cookie, no Authorization
        header) must be refused, not silently treated as some default user."""
        response = self.client.get(PO_LIST_URL)
        assert response.status_code == 401

    def test_logout_clears_access_cookie_and_revokes_access(self):
        """After logout, the previously-authenticated client must lose access
        to a protected endpoint - the access cookie itself is cleared server-side."""
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        otp = _extract_otp_from_outbox()
        self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert self.client.get(PO_LIST_URL).status_code == 200

        logout_response = self.client.post(LOGOUT_URL)
        assert logout_response.status_code == 200
        assert self.client.get(PO_LIST_URL).status_code == 401


@pytest.mark.django_db
class TestTokenRefreshRotationAndBlacklist:
    """Regression tests for the 2026-09-05 hardening pass: SIMPLE_JWT's
    ROTATE_REFRESH_TOKENS/BLACKLIST_AFTER_ROTATION were enabled (previously
    False, making PTTokenRefreshSerializer's existing rotate/blacklist
    branch dead code) so a stolen refresh token can't be replayed forever -
    each successful /api/auth/token/refresh call issues a new refresh token
    and blacklists the one just spent, and POST /api/auth/logout blacklists
    the caller's current refresh token directly."""

    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def _login_and_verify(self):
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        otp = _extract_otp_from_outbox()
        return self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")

    def test_refresh_rotates_and_the_old_refresh_token_is_rejected(self):
        """A successful refresh must issue a NEW pt_refresh cookie value and
        blacklist the old one - replaying the old refresh token afterward
        must fail, proving rotation actually took effect end-to-end (not
        just that the serializer's dead-code branch exists)."""
        self._login_and_verify()
        old_refresh_token = self.client.cookies["pt_refresh"].value

        refresh_response = self.client.post(TOKEN_REFRESH_URL)
        assert refresh_response.status_code == 200
        assert "access" in refresh_response.data
        # Body must never carry the raw refresh token - only the cookie does.
        assert "refresh" not in refresh_response.data

        new_refresh_token = refresh_response.cookies["pt_refresh"].value
        assert new_refresh_token != old_refresh_token

        # Replaying the OLD refresh token (as a non-browser client would, by
        # sending it directly in the body) must now be rejected - it was
        # blacklisted on rotation above.
        replay = self.client.post(TOKEN_REFRESH_URL, {"refresh": old_refresh_token}, format="json")
        assert replay.status_code == 401

        # The NEW refresh token must still work - rotation didn't just
        # break refreshing outright.
        second_refresh = self.client.post(TOKEN_REFRESH_URL, {"refresh": new_refresh_token}, format="json")
        assert second_refresh.status_code == 200

    def test_logout_blacklists_the_current_refresh_token(self):
        """After POST /api/auth/logout, the refresh token that was active at
        logout time must be rejected even if presented directly in the body
        (not relying on the cookie having been cleared client-side) - a
        copy of the token made before logout must not still work."""
        self._login_and_verify()
        refresh_token = self.client.cookies["pt_refresh"].value

        logout_response = self.client.post(LOGOUT_URL)
        assert logout_response.status_code == 200

        replay = self.client.post(TOKEN_REFRESH_URL, {"refresh": refresh_token}, format="json")
        assert replay.status_code == 401
