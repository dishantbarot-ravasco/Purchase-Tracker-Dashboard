"""
Regression tests for the "password change does not evict live sessions" gap
found during the 2026-09-15 audit pass.

THE GAP
-------
Both password-setting paths saved a new bcrypt hash and stopped there:

  - POST /api/auth/change-password/confirm  (self-service, OTP-gated)
  - PATCH /api/auth/users/<id>              (admin reset)

Neither touched `PTUser.token_version`, which is the one thing that
invalidates an already-issued JWT (see PTJWTAuthentication.get_user()'s
`ver` check). So a token minted before the change kept authenticating for
its full 12h ACCESS_TOKEN_LIFETIME, and the sliding 30-day pt_refresh cookie
could renew it indefinitely. An account holder who changed their password
precisely BECAUSE they suspected compromise did not actually evict the
attacker - the remedy was ineffective against the case it exists for.

THE FIX
-------
Both paths now call `revoke_all_tokens()` (bump token_version only).
Deliberately NOT `revoke_all_sessions()`, which also deletes every
TrustedDevice row - a pt_device cookie only ever skips the email-OTP step
and never substitutes for the password, so it is useless to an attacker once
the password changes, and wiping it on every routine rotation would
re-challenge every colleague on every device for no security gain. The tests
below assert that distinction explicitly, so a future "simplification" to
revoke_all_sessions() fails rather than silently changing UX for everyone.

The self-service path additionally re-issues fresh cookies for the caller,
so changing your own password does not sign you out of the browser you did
it in.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.auth_serializers import PTTokenObtainPairSerializer
from apps.api.tests.factories import make_user
from apps.core.models import PTUser, TrustedDevice
from apps.services.otp_service import generate_otp

CONFIRM_URL = "/api/auth/change-password/confirm"
NEW_PASSWORD = "A-Str0ng-New-Passphrase"


def _bearer(client, user):
    """Authenticate `client` with a REAL signed JWT rather than
    force_authenticate() - these tests are specifically about whether an
    already-minted token still validates, which force_authenticate bypasses
    entirely by injecting the user object directly."""
    token = PTTokenObtainPairSerializer.get_token(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return str(token.access_token)


@pytest.mark.django_db
class TestSelfServicePasswordChange:
    def setup_method(self):
        self.user = make_user(email="self-change@ravasco.com", role="editor")
        self.client = APIClient()

    def test_old_token_stops_working_after_password_change(self):
        """The whole point: a token issued BEFORE the change must be dead
        after it."""
        stale_token = _bearer(self.client, self.user)
        assert self.client.get("/api/auth/me").status_code == 200

        otp = generate_otp(self.user.email)
        resp = self.client.post(CONFIRM_URL, {"otp": otp, "newPassword": NEW_PASSWORD}, format="json")
        assert resp.status_code == 200, resp.data
        assert resp.data["sessionsRevoked"] is True

        # Same token string, presented fresh on a clean client.
        other_device = APIClient()
        other_device.credentials(HTTP_AUTHORIZATION=f"Bearer {stale_token}")
        assert other_device.get("/api/auth/me").status_code == 401

    def test_token_version_is_bumped(self):
        before = PTUser.objects.get(pk=self.user.pk).token_version
        _bearer(self.client, self.user)
        otp = generate_otp(self.user.email)
        assert self.client.post(
            CONFIRM_URL, {"otp": otp, "newPassword": NEW_PASSWORD}, format="json"
        ).status_code == 200
        assert PTUser.objects.get(pk=self.user.pk).token_version == before + 1

    def test_caller_stays_signed_in_via_refreshed_cookies(self):
        """Changing your own password must not boot you out of the browser
        you changed it in - the response re-cookies a token minted against
        the NEW token_version."""
        _bearer(self.client, self.user)
        otp = generate_otp(self.user.email)
        resp = self.client.post(CONFIRM_URL, {"otp": otp, "newPassword": NEW_PASSWORD}, format="json")
        assert resp.status_code == 200

        from django.conf import settings
        assert settings.PT_COOKIE_NAME in resp.cookies, "no fresh access cookie was set"
        assert "pt_refresh" in resp.cookies, "no fresh refresh cookie was set"

        # The cookie must actually authenticate - i.e. it was minted against
        # the post-bump token_version, not the stale in-memory one.
        fresh = APIClient()
        fresh.cookies[settings.PT_COOKIE_NAME] = resp.cookies[settings.PT_COOKIE_NAME].value
        me = fresh.get("/api/auth/me")
        assert me.status_code == 200, "re-issued cookie does not authenticate"
        assert me.data["email"] == self.user.email

    def test_device_trust_is_deliberately_preserved(self):
        """A routine rotation must not re-OTP every device - see this
        module's docstring."""
        TrustedDevice.objects.create(
            user=self.user, device_token_hash="a" * 64, device_name="Laptop",
        )
        _bearer(self.client, self.user)
        otp = generate_otp(self.user.email)
        assert self.client.post(
            CONFIRM_URL, {"otp": otp, "newPassword": NEW_PASSWORD}, format="json"
        ).status_code == 200
        assert TrustedDevice.objects.filter(user=self.user).count() == 1, (
            "password change must NOT wipe device trust - use "
            "logout-everywhere for that"
        )

    def test_a_rejected_change_does_not_revoke_anything(self):
        """A wrong OTP or a too-weak password must leave sessions intact -
        otherwise a failed attempt becomes a denial-of-service on yourself."""
        before = PTUser.objects.get(pk=self.user.pk).token_version
        _bearer(self.client, self.user)

        assert self.client.post(
            CONFIRM_URL, {"otp": "000000", "newPassword": NEW_PASSWORD}, format="json"
        ).status_code == 400
        assert PTUser.objects.get(pk=self.user.pk).token_version == before

        otp = generate_otp(self.user.email)
        assert self.client.post(
            CONFIRM_URL, {"otp": otp, "newPassword": "short"}, format="json"
        ).status_code == 400
        assert PTUser.objects.get(pk=self.user.pk).token_version == before


@pytest.mark.django_db
class TestAdminPasswordReset:
    def setup_method(self):
        self.admin = make_user(email="admin-reset@ravasco.com", role="admin")
        self.target = make_user(email="target-reset@ravasco.com", role="viewer")
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _patch(self, **body):
        return self.client.patch(f"/api/auth/users/{self.target.user_id}", body, format="json")

    def test_admin_reset_revokes_the_targets_sessions(self):
        victim = APIClient()
        stale_token = _bearer(victim, self.target)
        assert victim.get("/api/auth/me").status_code == 200

        assert self._patch(password=NEW_PASSWORD).status_code == 200

        still_stale = APIClient()
        still_stale.credentials(HTTP_AUTHORIZATION=f"Bearer {stale_token}")
        assert still_stale.get("/api/auth/me").status_code == 401

    def test_admin_reset_preserves_target_device_trust(self):
        TrustedDevice.objects.create(
            user=self.target, device_token_hash="b" * 64, device_name="Target laptop",
        )
        assert self._patch(password=NEW_PASSWORD).status_code == 200
        assert TrustedDevice.objects.filter(user=self.target).count() == 1

    def test_a_non_password_edit_does_not_revoke_sessions(self):
        """Renaming someone or changing their plants must not sign them out -
        only a password reset carries that consequence."""
        before = PTUser.objects.get(pk=self.target.pk).token_version
        assert self._patch(fullName="Renamed Person").status_code == 200
        assert PTUser.objects.get(pk=self.target.pk).token_version == before

    def test_admin_own_session_survives_resetting_someone_else(self):
        """The admin doing the reset must not be logged out by it."""
        assert self._patch(password=NEW_PASSWORD).status_code == 200
        assert self.client.get("/api/auth/users").status_code == 200
