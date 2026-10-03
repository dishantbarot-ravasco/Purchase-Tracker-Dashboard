"""
Go-live security pass, 2026-10-03 - the open findings of the 2026-10-02 audit.

1. Reset-code failures count apart from sign-in codes: a stranger guessing
   on the signed-out reset form no longer blocks the account's new-device
   sign-in for a day.
2. Logout revokes the access token itself, not only the cookie.
3. A sign-in renews for at most PT_SESSION_MAX_AGE (auth_time claim).
4. A trusted device expires on the server (idle 90 days, or a year old).
5. Only the owner signs an admin out (one device, or everywhere).
6. The development SECRET_KEY is an Error with DEBUG off (apps.core.E001).
7. Django admin's PTUser page is view-only.
8. Common passwords are refused.

Every refusal is paired with the success it guards, so each test fails if
its fix is removed as well as if the check is wrongly tightened. (The cron
secret and CSP changes are tested in test_reports_views.py and
test_security_headers_and_csrf_scope.py.)
"""

import time
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.admin.sites import site as admin_site
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.api.auth_serializers import PTTokenObtainPairSerializer
from apps.api.routers.users_views import _validate_password_strength
from apps.api.tests.factories import make_user
from apps.core.checks import check_secret_key_is_set
from apps.core.models import PTUser, TrustedDevice
from apps.services.device_service import _hash_device_token, is_trusted_device
from apps.services.otp_service import _MAX_ATTEMPTS, _MAX_DAILY_FAILURES, generate_otp, verify_otp

PASSWORD = "A-Str0ng-Test-Passphrase"
LOGIN = "login"
RESET = "password_reset"


@pytest.fixture(autouse=True)
def _fresh_cache():
    # Throttles and the reset-failure counter live in the cache, outside the
    # test transaction.
    cache.clear()


def _wrong(code):
    return "000000" if code != "000000" else "111111"


def _burn(email, purpose, failures):
    """Spend `failures` wrong guesses on `purpose`, issuing a new code each
    time one is used up - what an attacker can do from the outside."""
    left = failures
    while left:
        code = generate_otp(email, purpose)
        for _ in range(min(left, _MAX_ATTEMPTS)):
            assert verify_otp(email, _wrong(code), purpose) is False
            left -= 1


# ── 1. Reset failures no longer lock the account's sign-in ──────────────────


@pytest.mark.django_db
class TestResetFailuresCountApart:
    def setup_method(self):
        self.user = make_user(email="victim@ravasco.com")

    def test_a_day_of_wrong_reset_codes_leaves_sign_in_working(self):
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES)
        code = generate_otp(self.user.email, LOGIN)
        assert verify_otp(self.user.email, code, LOGIN) is True

    def test_the_reset_flow_itself_is_still_capped(self):
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES)
        code = generate_otp(self.user.email, RESET)
        assert verify_otp(self.user.email, code, RESET) is False

    def test_one_short_of_the_cap_a_reset_code_still_works(self):
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES - 1)
        code = generate_otp(self.user.email, RESET)
        assert verify_otp(self.user.email, code, RESET) is True

    def test_the_reset_count_does_not_touch_the_account_counter(self):
        _burn(self.user.email, RESET, 7)
        assert PTUser.objects.get(pk=self.user.pk).otp_failed_attempts == 0

    def test_sign_in_codes_are_still_capped_on_the_account(self):
        """The control: the shared counter still guards sign-in codes."""
        _burn(self.user.email, LOGIN, _MAX_DAILY_FAILURES)
        code = generate_otp(self.user.email, LOGIN)
        assert verify_otp(self.user.email, code, LOGIN) is False

    def test_the_reset_window_ends_after_a_day(self):
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES)
        later = timezone.now() + timedelta(hours=24, minutes=1)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("apps.services.otp_service.timezone.now", lambda: later)
            code = generate_otp(self.user.email, RESET)
            assert verify_otp(self.user.email, code, RESET) is True

    def test_a_correct_reset_clears_its_count(self):
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES - 1)
        assert verify_otp(self.user.email, generate_otp(self.user.email, RESET), RESET) is True
        _burn(self.user.email, RESET, _MAX_DAILY_FAILURES - 1)
        assert verify_otp(self.user.email, generate_otp(self.user.email, RESET), RESET) is True


# ── 2. Logout revokes a copied access token ─────────────────────────────────


def _signed_in_client(user):
    """Signed in on a trusted device, the way the browser is."""
    client = APIClient()
    token = "d" * 64
    TrustedDevice.objects.create(user=user, device_token_hash=_hash_device_token(token), device_name="Test")
    client.cookies["pt_device"] = token
    login = client.post("/api/auth/login", {"email": user.email, "password": PASSWORD}, format="json")
    assert login.status_code == 200, login.data
    return client


@pytest.mark.django_db
class TestLogoutRevokesTheAccessToken:
    def test_a_copied_access_token_stops_working_at_logout(self):
        user = make_user(email="leaver@ravasco.com", password=PASSWORD)
        browser = _signed_in_client(user)
        stolen = APIClient()
        stolen.credentials(HTTP_AUTHORIZATION="Bearer " + browser.cookies["pt_access"].value)
        assert stolen.get("/api/auth/me").status_code == 200

        assert browser.post("/api/auth/logout").status_code == 200

        assert stolen.get("/api/auth/me").status_code == 401

    def test_logout_on_one_browser_leaves_another_signed_in(self):
        user = make_user(email="two-browsers@ravasco.com", password=PASSWORD)
        first = _signed_in_client(user)
        second = APIClient()
        second.cookies["pt_device"] = "d" * 64
        assert second.post("/api/auth/login", {"email": user.email, "password": PASSWORD}, format="json").status_code == 200

        first.post("/api/auth/logout")

        assert second.get("/api/auth/me").status_code == 200

    def test_access_tokens_last_an_hour(self):
        assert settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"] == timedelta(hours=1)


# ── 3. A session renews for PT_SESSION_MAX_AGE at most ──────────────────────


@pytest.mark.django_db
class TestAbsoluteSessionCap:
    def setup_method(self):
        self.user = make_user(email="long-session@ravasco.com")

    def _refresh_signed_in_ago(self, age):
        token = PTTokenObtainPairSerializer.get_token(self.user)
        if age is None:
            del token["auth_time"]
        else:
            token["auth_time"] = int(time.time() - age.total_seconds())
        return APIClient().post("/api/auth/token/refresh", {"refresh": str(token)}, format="json")

    def test_a_session_older_than_the_cap_cannot_renew(self):
        response = self._refresh_signed_in_ago(settings.PT_SESSION_MAX_AGE + timedelta(minutes=1))
        assert response.status_code == 401

    def test_a_session_inside_the_cap_renews(self):
        response = self._refresh_signed_in_ago(settings.PT_SESSION_MAX_AGE - timedelta(days=1))
        assert response.status_code == 200, response.data
        assert response.data["access"]

    def test_rotation_keeps_the_original_sign_in_time(self):
        signed_in = int(time.time() - timedelta(days=10).total_seconds())
        token = PTTokenObtainPairSerializer.get_token(self.user)
        token["auth_time"] = signed_in
        client = APIClient()
        client.post("/api/auth/token/refresh", {"refresh": str(token)}, format="json")
        from rest_framework_simplejwt.tokens import RefreshToken

        rotated = RefreshToken(client.cookies["pt_refresh"].value)
        assert rotated["auth_time"] == signed_in

    def test_a_token_from_before_the_cap_starts_its_clock_rather_than_failing(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        client = APIClient()
        token = PTTokenObtainPairSerializer.get_token(self.user)
        del token["auth_time"]
        response = client.post("/api/auth/token/refresh", {"refresh": str(token)}, format="json")
        assert response.status_code == 200
        assert abs(RefreshToken(client.cookies["pt_refresh"].value)["auth_time"] - time.time()) < 60


# ── 4. A trusted device expires on the server ───────────────────────────────


@pytest.mark.django_db
class TestTrustedDeviceExpiry:
    def setup_method(self):
        self.user = make_user(email="device-age@ravasco.com")
        self.token = "e" * 64
        self.device = TrustedDevice.objects.create(
            user=self.user, device_token_hash=_hash_device_token(self.token), device_name="Laptop",
        )

    def _trusted(self, **ages):
        now = timezone.now()
        TrustedDevice.objects.filter(pk=self.device.pk).update(**{k: now - v for k, v in ages.items()})
        request = RequestFactory().get("/")
        request.COOKIES["pt_device"] = self.token
        return is_trusted_device(request, self.user.user_id)

    def test_a_recently_used_device_is_trusted(self):
        assert self._trusted(last_used_at=timedelta(days=89), created_at=timedelta(days=364)) is True

    def test_a_device_idle_for_ninety_days_is_not(self):
        assert self._trusted(last_used_at=timedelta(days=91)) is False

    def test_a_device_older_than_its_cookie_is_not(self):
        assert self._trusted(last_used_at=timedelta(days=1), created_at=timedelta(days=366)) is False


# ── 5. Only the owner signs an admin out ────────────────────────────────────


@pytest.mark.django_db
class TestOnlyTheOwnerSignsAnAdminOut:
    def setup_method(self):
        self.owner = make_user(email=settings.OWNER_EMAIL, role="admin")
        self.admin = make_user(email="second-admin@ravasco.com", role="admin")
        self.other_admin = make_user(email="third-admin@ravasco.com", role="admin")
        self.user = make_user(email="plain-user@ravasco.com")

    def _device(self, user, token):
        return TrustedDevice.objects.create(user=user, device_token_hash=_hash_device_token(token), device_name="PC")

    def _as(self, actor):
        client = APIClient()
        client.force_authenticate(user=actor)
        return client

    def _logout_everywhere(self, actor, target):
        return self._as(actor).post(f"/api/auth/users/{target.user_id}/logout-everywhere")

    def _version(self, user):
        return PTUser.objects.get(pk=user.pk).token_version

    def test_an_admin_cannot_sign_the_owner_out_everywhere(self):
        before = self._version(self.owner)
        assert self._logout_everywhere(self.admin, self.owner).status_code == 403
        assert self._version(self.owner) == before

    def test_an_admin_cannot_sign_another_admin_out_everywhere(self):
        assert self._logout_everywhere(self.admin, self.other_admin).status_code == 403

    def test_an_admin_can_still_sign_a_user_out_everywhere(self):
        before = self._version(self.user)
        assert self._logout_everywhere(self.admin, self.user).status_code == 200
        assert self._version(self.user) == before + 1

    def test_the_owner_can_sign_an_admin_out_everywhere(self):
        assert self._logout_everywhere(self.owner, self.admin).status_code == 200

    def test_an_admin_cannot_revoke_the_owners_device(self):
        device = self._device(self.owner, "o" * 64)
        url = f"/api/auth/users/{self.owner.user_id}/devices/{device.pk}"
        assert self._as(self.admin).delete(url).status_code == 403
        assert TrustedDevice.objects.filter(pk=device.pk).exists()

    def test_an_admin_can_revoke_a_users_device_and_their_own(self):
        theirs = self._device(self.user, "u" * 64)
        mine = self._device(self.admin, "a" * 64)
        client = self._as(self.admin)
        assert client.delete(f"/api/auth/users/{self.user.user_id}/devices/{theirs.pk}").status_code == 204
        assert client.delete(f"/api/auth/users/{self.admin.user_id}/devices/{mine.pk}").status_code == 204

    def test_the_owner_can_revoke_an_admins_device(self):
        device = self._device(self.admin, "b" * 64)
        url = f"/api/auth/users/{self.admin.user_id}/devices/{device.pk}"
        assert self._as(self.owner).delete(url).status_code == 204


# ── 6. The development SECRET_KEY never runs with DEBUG off ─────────────────


class TestSecretKeyCheck:
    def test_the_development_key_is_an_error_with_debug_off(self):
        with override_settings(DEBUG=False, SECRET_KEY=settings.DEV_SECRET_KEY):
            assert [e.id for e in check_secret_key_is_set(None)] == ["apps.core.E001"]

    def test_a_real_key_passes(self):
        with override_settings(DEBUG=False, SECRET_KEY="a-real-production-key-0123456789"):
            assert check_secret_key_is_set(None) == []

    def test_local_development_may_use_it(self):
        with override_settings(DEBUG=True, SECRET_KEY=settings.DEV_SECRET_KEY):
            assert check_secret_key_is_set(None) == []


# ── 7. Django admin cannot edit accounts around the owner rule ──────────────


class TestPtUserAdminIsViewOnly:
    def test_no_add_change_or_delete(self):
        model_admin = admin_site._registry[PTUser]
        request = RequestFactory().get("/admin/")
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


# ── 8. Common passwords are refused ─────────────────────────────────────────


class TestCommonPasswords:
    @pytest.mark.parametrize("password", ["qwerty12345", "password123", "Password123"])
    def test_a_leaked_password_is_refused(self, password):
        with pytest.raises(ValidationError, match="too common"):
            _validate_password_strength(password, "someone@ravasco.com")

    def test_a_strong_password_passes(self):
        _validate_password_strength("Tea-kettle-orbit-42", "someone@ravasco.com")
