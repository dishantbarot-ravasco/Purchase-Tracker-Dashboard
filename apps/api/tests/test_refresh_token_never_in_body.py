"""
Regression tests for the refresh token leaking into JS-readable response
bodies, found during the 2026-09-15 audit pass.

THE GAP
-------
`PTTokenRefreshView`'s own docstring states the policy plainly: the refresh
token "only ever travels as the httpOnly pt_refresh cookie, never in a
JS-readable JSON response body, same reasoning as the access token cookie."

Two endpoints broke that rule - and they were the two worst places to break
it, because both are the moment a session is created:

  POST /api/auth/login          returned {"refresh": "<30-day token>"}
  POST /api/auth/device-verify  returned {"refresh": "<30-day token>"}

pt_refresh is httpOnly precisely so page JS cannot read it. Handing the same
value back in the response body hands it to any script on the page anyway,
defeating the cookie flag at the one moment it matters most. The frontend
never read it (verified: login.js/auth.js store no tokens at all and rely
entirely on the cookies), so this was reachable-but-unused - defense in depth
that had quietly stopped being depth.

THE FIX
-------
Both bodies drop `refresh`. The cookie is still set on the same response, so
browsers and cookie-honouring API clients are unaffected. The serializer
hands the token to the view under a private `_refresh` key which the view
pops before responding.

WHAT THIS TEST LOCKS IN
-----------------------
A body-level assertion on EVERY auth response that mints a session, including
the private `_refresh` hand-off key - a rename that forgot the `.pop()` would
otherwise reintroduce the exact leak under a new name.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import TrustedDevice
from apps.services.device_service import _hash_device_token
from apps.services.otp_service import generate_otp

LOGIN_URL = "/api/auth/login"
DEVICE_VERIFY_URL = "/api/auth/device-verify"
PASSWORD = "A-Str0ng-Test-Passphrase"

# Any key whose value could be a refresh token. `_refresh` is included
# deliberately: it is the private hand-off key, and it must be popped by the
# view rather than merely conventionally ignored.
_FORBIDDEN_BODY_KEYS = ("refresh", "_refresh", "refresh_token", "refreshToken")


def _assert_no_refresh_in_body(response, label):
    data = response.data if hasattr(response, "data") else {}
    if not isinstance(data, dict):
        return
    for key in _FORBIDDEN_BODY_KEYS:
        assert key not in data, (
            f"{label} returned {key!r} in its JSON body - the refresh token must "
            f"only ever travel as the httpOnly pt_refresh cookie. Body keys: "
            f"{sorted(data)}"
        )


@pytest.mark.django_db
class TestRefreshTokenNeverInResponseBody:
    def setup_method(self):
        from django.core.cache import cache
        cache.clear()  # login throttle is cache-backed, not rolled back per test
        self.client = APIClient()
        self.user = make_user(email="body-leak@ravasco.com", role="editor", password=PASSWORD)

    def _trust_this_client(self):
        """Register a trusted device so login takes the 'ok' fast path rather
        than the OTP branch."""
        token = "d" * 64
        TrustedDevice.objects.create(
            user=self.user, device_token_hash=_hash_device_token(token), device_name="Test device",
        )
        self.client.cookies["pt_device"] = token

    def test_trusted_device_login_body_has_no_refresh_token(self):
        self._trust_this_client()
        resp = self.client.post(
            LOGIN_URL, {"email": self.user.email, "password": PASSWORD}, format="json",
        )
        assert resp.status_code == 200, resp.data
        assert resp.data["status"] == "ok"
        _assert_no_refresh_in_body(resp, "POST /api/auth/login")

    def test_trusted_device_login_still_sets_the_refresh_cookie(self):
        """Removing it from the body must not remove it entirely - otherwise
        'remember me' silently breaks and every user is forced to re-login
        after 12h."""
        self._trust_this_client()
        resp = self.client.post(
            LOGIN_URL, {"email": self.user.email, "password": PASSWORD}, format="json",
        )
        assert "pt_refresh" in resp.cookies, "the pt_refresh cookie was not set"
        assert resp.cookies["pt_refresh"].value, "the pt_refresh cookie is empty"
        assert resp.cookies["pt_refresh"]["httponly"], "pt_refresh must stay httpOnly"

    def test_device_verify_body_has_no_refresh_token(self):
        # New device -> login returns device_verify, then OTP completes it.
        login = self.client.post(
            LOGIN_URL, {"email": self.user.email, "password": PASSWORD}, format="json",
        )
        assert login.data["status"] == "device_verify"
        _assert_no_refresh_in_body(login, "POST /api/auth/login (device_verify branch)")

        otp = generate_otp(self.user.email)
        resp = self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert resp.status_code == 200, resp.data
        _assert_no_refresh_in_body(resp, "POST /api/auth/device-verify")

    def test_device_verify_still_sets_the_refresh_cookie(self):
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": PASSWORD}, format="json")
        otp = generate_otp(self.user.email)
        resp = self.client.post(DEVICE_VERIFY_URL, {"code": otp}, format="json")
        assert "pt_refresh" in resp.cookies, "the pt_refresh cookie was not set"
        assert resp.cookies["pt_refresh"]["httponly"]

    def test_the_cookie_from_login_actually_refreshes(self):
        """End-to-end proof the remaining carrier works: log in, then refresh
        using only the cookie the login set, with no body token anywhere."""
        self._trust_this_client()
        self.client.post(LOGIN_URL, {"email": self.user.email, "password": PASSWORD}, format="json")

        refreshed = self.client.post("/api/auth/token/refresh")
        assert refreshed.status_code == 200, refreshed.data
        assert "access" in refreshed.data
        _assert_no_refresh_in_body(refreshed, "POST /api/auth/token/refresh")
