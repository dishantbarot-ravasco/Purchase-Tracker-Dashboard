"""
Tests for two security gaps found in the 2026-09-15 audit pass, both in
modules that had ZERO test coverage beforehand (`google_oauth_views.py` - an
authentication entry point - and `exceptions.py`).

1. GOOGLE OAUTH IGNORED THE ACCOUNT LOCKOUT
   `PTUserBackend.authenticate()` refuses a locked account outright (5 failed
   passwords -> 15 minute lock). `google_callback()` only ever checked
   `is_active`, so the lockout closed the password door and left the Google
   door open. The inconsistency is the real problem: an admin told an account
   is locked reasonably believes it cannot be signed into, and a lockout that
   one of two front doors silently ignores is worse than no lockout, because
   it is trusted.

2. KeyError LEAKED INTERNAL KEY NAMES AS A 400
   `custom_exception_handler`'s `_DESCRIBABLE_EXCEPTIONS` included KeyError,
   whose `str()` is just the missing key's name. A genuine server-side bug
   (an internal dict lookup missing a key) was therefore reported to the
   caller as `400 {"detail": "'some_internal_key'"}` - leaking a fragment of
   internal structure AND mis-classifying a server fault as client error, so
   it would never show up in 5xx alerting.

The Google tests drive `google_callback` directly with the OAuth round-trip
stubbed (`_make_flow` and the userinfo `requests.get`). That is deliberate:
the behavior under test is what happens AFTER Google has vouched for the
address, and standing up a real OAuth dance would test Google, not this code.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.views import APIView

from apps.api.exceptions import custom_exception_handler
from apps.api.tests.factories import make_user

CALLBACK_URL = "/api/auth/google/callback/"


class _FakeCredentials:
    token = "fake-google-access-token"


def _stub_google(email, email_verified=True):
    """Context managers that make google_callback believe Google has just
    verified `email`, without any network call."""
    flow = MagicMock()
    flow.credentials = _FakeCredentials()
    flow.fetch_token.return_value = None

    userinfo = MagicMock()
    userinfo.raise_for_status.return_value = None
    userinfo.json.return_value = {"email": email, "email_verified": email_verified}

    return (
        patch("apps.api.routers.google_oauth_views._make_flow", return_value=flow),
        patch("apps.api.routers.google_oauth_views.http_requests.get", return_value=userinfo),
    )


def _callback(client, email, email_verified=True):
    """Drive the callback with a valid `state` already seeded in the session -
    state verification itself is a separate concern, tested below."""
    session = client.session
    session["google_oauth_state"] = "test-state"
    session.save()

    flow_patch, get_patch = _stub_google(email, email_verified)
    with flow_patch, get_patch:
        return client.get(CALLBACK_URL, {"code": "test-code", "state": "test-state"})


@pytest.mark.django_db
class TestGoogleOAuthRespectsAccountLockout:
    def setup_method(self):
        self.client = APIClient()

    def test_a_locked_account_cannot_sign_in_with_google(self):
        user = make_user(email="locked-oauth@ravasco.com", role="editor")
        user.locked_until = timezone.now() + datetime.timedelta(minutes=15)
        user.save(update_fields=["locked_until"])

        resp = _callback(self.client, user.email)

        assert resp.status_code == 302
        assert "oauth_error=account_locked" in resp.url
        # And crucially: no token was handed out by any route.
        assert "oauth_delivery" not in self.client.session
        assert "pending_user_id" not in self.client.session

    def test_an_expired_lock_does_not_block_sign_in(self):
        """A lock that has already elapsed must not linger - otherwise the
        fix turns a 15-minute lockout into a permanent one."""
        user = make_user(email="expired-lock@ravasco.com", role="editor")
        user.locked_until = timezone.now() - datetime.timedelta(minutes=1)
        user.save(update_fields=["locked_until"])

        resp = _callback(self.client, user.email)

        assert resp.status_code == 302
        assert "oauth_error=account_locked" not in resp.url

    def test_an_unlocked_account_is_unaffected(self):
        user = make_user(email="normal-oauth@ravasco.com", role="editor")
        assert user.locked_until is None

        resp = _callback(self.client, user.email)

        assert resp.status_code == 302
        assert "oauth_error" not in resp.url, f"unexpected error redirect: {resp.url}"

    def test_state_mismatch_is_still_rejected(self):
        """Guard on the guard: the CSRF `state` check must keep working - the
        lockout change sits just below it in the same function."""
        make_user(email="state-test@ravasco.com", role="editor")
        session = self.client.session
        session["google_oauth_state"] = "the-real-state"
        session.save()

        resp = self.client.get(CALLBACK_URL, {"code": "c", "state": "an-attacker-state"})
        assert resp.status_code == 302
        assert "oauth_error=state_mismatch" in resp.url

    def test_an_unverified_google_email_is_still_rejected(self):
        make_user(email="unverified@ravasco.com", role="editor")
        resp = _callback(self.client, "unverified@ravasco.com", email_verified=False)
        assert "oauth_error=unverified_email" in resp.url


class TestExceptionHandlerDoesNotLeakInternals:
    """Pure-function tests against the DRF exception handler."""

    def _handle(self, exc):
        return custom_exception_handler(exc, {"view": APIView()})

    def test_keyerror_becomes_a_generic_500_not_a_400_naming_the_key(self):
        resp = self._handle(KeyError("_internal_plant_config"))
        assert resp.status_code == 500, "a server-side KeyError is not a client error"
        assert "_internal_plant_config" not in str(resp.data), (
            "the internal key name leaked to the client"
        )
        assert resp.data["detail"] == "An unexpected server error occurred."

    def test_valueerror_still_describes_itself(self):
        """ValueError stays describable - the service layer raises it
        deliberately with user-facing wording, and breaking that would turn
        every real validation message into an opaque 500."""
        resp = self._handle(ValueError("Quantity must be a positive number."))
        assert resp.status_code == 400
        assert resp.data["detail"] == "Quantity must be a positive number."

    def test_an_unknown_exception_stays_generic(self):
        resp = self._handle(RuntimeError("connection string: postgres://user:pw@host/db"))
        assert resp.status_code == 500
        assert "postgres://" not in str(resp.data)
