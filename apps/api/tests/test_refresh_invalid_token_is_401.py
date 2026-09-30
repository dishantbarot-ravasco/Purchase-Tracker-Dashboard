"""
An unusable refresh token must be refused with a 401, not a 500
(2026-09-30).

THE BUG
-------
`PTTokenRefreshView.post()` overrides simplejwt's `TokenRefreshView.post()` to
read the pt_refresh cookie, and in doing so dropped simplejwt's own

    except TokenError as e:
        raise InvalidToken(e.args[0])

around `serializer.is_valid()`. A garbage, expired or tampered refresh token
therefore escaped as a bare `TokenError` - not an APIException - which
apps/api/exceptions.py reports as "An unexpected server error occurred." with
a 500, and logs as an unhandled exception. Reproduced with:

    curl -X POST -H "Content-Type: application/json" \
         --cookie "pt_refresh=not-a-real-token" -d "{}" \
         http://127.0.0.1:8000/api/auth/token/refresh

auth.js's refreshSession() treats any non-ok response as a failed renewal, so
the browser behaved the same either way; the harm was a 500 in the logs for
every stale cookie, and a wrong status for API clients.

Every case below goes through the real view with the literal '{}' body that
refreshSession() sends, and would fail (500) with the try/except removed.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.api.tests.factories import make_user

REFRESH_URL = "/api/auth/token/refresh"


def _renew(client, body="{}"):
    return client.post(REFRESH_URL, data=body, content_type="application/json")


def _assert_refused_401(resp):
    assert resp.status_code == 401, (resp.status_code, getattr(resp, "data", resp.content))
    assert resp.data.get("code") == "token_not_valid", resp.data
    # A refused renewal must not hand out a fresh access token.
    assert "access" not in resp.data
    assert not (resp.cookies.get("pt_access") and resp.cookies["pt_access"].value)


@pytest.mark.django_db
class TestInvalidRefreshTokenIs401:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()

    def test_garbage_refresh_cookie(self):
        self.client.cookies["pt_refresh"] = "not-a-real-token"
        _assert_refused_401(_renew(self.client))

    def test_garbage_refresh_in_body(self):
        _assert_refused_401(_renew(self.client, '{"refresh": "not-a-real-token"}'))

    def test_expired_refresh_cookie(self):
        user = make_user(email="expired-refresh@ravasco.com", role="editor")
        token = RefreshToken()
        token["user_id"] = user.pk
        token["ver"] = user.token_version
        token.set_exp(lifetime=timedelta(seconds=-60))
        self.client.cookies["pt_refresh"] = str(token)
        _assert_refused_401(_renew(self.client))

    def test_tampered_signature(self):
        user = make_user(email="tampered-refresh@ravasco.com", role="editor")
        token = RefreshToken()
        token["user_id"] = user.pk
        token["ver"] = user.token_version
        raw = str(token)
        # Flip the last signature character to a different base64url char.
        tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")
        self.client.cookies["pt_refresh"] = tampered
        _assert_refused_401(_renew(self.client))

    def test_a_valid_token_built_the_same_way_still_renews(self):
        """Control: the expired/tampered cases fail because of the defect in
        the token, not because hand-built tokens are unusable here."""
        user = make_user(email="control-refresh@ravasco.com", role="editor")
        token = RefreshToken()
        token["user_id"] = user.pk
        token["ver"] = user.token_version
        self.client.cookies["pt_refresh"] = str(token)
        resp = _renew(self.client)
        assert resp.status_code == 200, resp.data
        assert resp.data["access"]
