"""
The server-side contract frontend/js/auth.js's authFetch() relies on
(2026-09-23, audit pass).

Until this date nothing in the frontend ever called /api/auth/token/refresh:
every 401 went straight to /login.html, so the 30-day pt_refresh cookie never
extended a browser session and everyone was signed out 12 hours after signing
in. authFetch() now renews once on a 401 and replays the request. These tests
drive the exact sequence the browser performs - including the literal '{}'
JSON body authFetch sends - so a server-side change that would break silent
renewal fails here instead of quietly signing everyone out at hour 12 again.

The "refused" cases matter as much as the happy path: authFetch deliberately
falls through to the normal login redirect whenever renewal fails, so every
server-side revocation (log out everywhere, a spent/rotated token) must keep
refusing the refresh, or silent renewal would become a way around them.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import TrustedDevice
from apps.services.device_service import _hash_device_token

PASSWORD = "A-Str0ng-Test-Passphrase"
REFRESH_URL = "/api/auth/token/refresh"


def _renew(client):
    """Exactly what authFetch()'s refreshSession() sends."""
    return client.post(REFRESH_URL, data="{}", content_type="application/json")


@pytest.mark.django_db
class TestBrowserSessionRenewal:
    def setup_method(self):
        cache.clear()  # the login throttle is cache-backed, not rolled back per test
        self.client = APIClient()
        self.user = make_user(email="renewal@ravasco.com", role="editor", password=PASSWORD)
        token = "r" * 64
        TrustedDevice.objects.create(user=self.user, device_token_hash=_hash_device_token(token), device_name="Test")
        self.client.cookies["pt_device"] = token
        login = self.client.post("/api/auth/login", {"email": self.user.email, "password": PASSWORD}, format="json")
        assert login.status_code == 200, login.data

    def _expire_access_cookie(self):
        """A 12-hour-old pt_access: the browser has dropped it (its max-age
        elapsed) while pt_refresh, 30 days, is still held."""
        del self.client.cookies["pt_access"]

    def test_an_expired_session_renews_from_the_cookie_alone_and_the_replay_succeeds(self):
        self._expire_access_cookie()
        assert self.client.get("/api/auth/me").status_code == 401  # what triggers authFetch's renewal

        renewed = _renew(self.client)

        assert renewed.status_code == 200, renewed.data
        assert self.client.cookies["pt_access"].value, "renewal must re-set pt_access for the replay to carry"
        assert self.client.cookies["pt_refresh"].value, "the rotated refresh token must be re-cookied too"
        replay = self.client.get("/api/auth/me")
        assert replay.status_code == 200
        assert replay.data["email"] == self.user.email

    def test_renewal_is_refused_after_log_out_everywhere(self):
        """Silent renewal must not outlive a revocation - authFetch relies on
        this refusal to fall through to the login redirect.

        Two layers, both asserted. The browser that pressed the button has its
        refresh cookie deleted, so its renewal carries no token at all (400).
        The property that actually matters is the second: a COPY of the old
        refresh token - another device, or a stolen cookie - is refused by the
        token_version check (401), so silent renewal can never resurrect a
        session that "log out everywhere" killed."""
        stolen_copy = self.client.cookies["pt_refresh"].value
        self.client.post("/api/auth/logout-everywhere")
        self._expire_access_cookie()

        own_browser = _renew(self.client)
        assert not (200 <= own_browser.status_code < 300), own_browser.data

        elsewhere = APIClient()
        elsewhere.cookies["pt_refresh"] = stolen_copy
        assert _renew(elsewhere).status_code == 401

    def test_a_spent_refresh_token_cannot_renew_twice(self):
        """Why authFetch's refreshSession() is single-flight: a rotated token
        is revoked, so a second, concurrent renewal presenting the same token
        is refused. If the frontend ever fired two at once, the loser would
        sign the user out - this pins the server behaviour that makes the
        single-flight necessary."""
        spent = self.client.cookies["pt_refresh"].value
        assert _renew(self.client).status_code == 200

        second = APIClient()
        second.cookies["pt_refresh"] = spent
        assert _renew(second).status_code == 401
