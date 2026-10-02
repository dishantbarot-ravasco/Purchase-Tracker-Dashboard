"""
"Forgot password" on the sign-in page (2026-10-02) - apps/api/routers/
password_views.py's request_password_reset / confirm_password_reset - and
the rules around it: admins can no longer set a password, and every emailed
code is bound to the flow it was issued for.

Every refusal is paired with the success it guards, so a test fails if the
check is removed as well as if it is wrongly tightened.
"""

import bcrypt
import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import OTPCode, PTUser, TrustedDevice
from apps.services.otp_service import generate_otp

REQUEST_URL = "/api/auth/password-reset/request"
CONFIRM_URL = "/api/auth/password-reset/confirm"
NEW = "BrandNewPassw0rd!"


@pytest.fixture(autouse=True)
def _fresh_throttles():
    # DRF throttles live in Django's cache, outside the test transaction.
    cache.clear()


def _confirm(client, email, otp, new=NEW, confirm=None):
    return client.post(CONFIRM_URL, {"email": email, "otp": otp, "newPassword": new,
                                     "confirmPassword": new if confirm is None else confirm}, format="json")


@pytest.mark.django_db
class TestRequest:
    def test_the_reply_is_the_same_whether_or_not_the_address_has_an_account(self):
        make_user(email="known@ravasco.com")
        client = APIClient()
        known = client.post(REQUEST_URL, {"email": "known@ravasco.com"}, format="json")
        unknown = client.post(REQUEST_URL, {"email": "nobody@ravasco.com"}, format="json")
        outside = client.post(REQUEST_URL, {"email": "known@gmail.com"}, format="json")
        assert known.status_code == unknown.status_code == outside.status_code == 202
        assert known.json() == unknown.json() == outside.json()
        assert OTPCode.objects.filter(email="known@ravasco.com", purpose="password_reset").exists()
        assert not OTPCode.objects.exclude(email="known@ravasco.com").exists()

    def test_codes_per_address_are_rate_limited(self):
        """Each request emails a real mailbox: five an hour per address."""
        make_user(email="flood@ravasco.com")
        client = APIClient()
        codes = [client.post(REQUEST_URL, {"email": "flood@ravasco.com"}, format="json").status_code for _ in range(6)]
        assert codes == [202] * 5 + [429]
        assert client.post(REQUEST_URL, {"email": "other@ravasco.com"}, format="json").status_code == 202

    def test_a_code_goes_only_to_an_active_account(self):
        user = make_user(email="gone@ravasco.com")
        PTUser.objects.filter(pk=user.pk).update(is_active=False)
        APIClient().post(REQUEST_URL, {"email": "gone@ravasco.com"}, format="json")
        assert not OTPCode.objects.filter(email="gone@ravasco.com").exists()
        assert not mail.outbox


@pytest.mark.django_db
class TestConfirm:
    def setup_method(self):
        self.user = make_user(email="forgot@ravasco.com")
        self.client = APIClient()

    def test_the_right_code_sets_the_password_and_signs_everything_out(self):
        TrustedDevice.objects.create(user=self.user, device_token_hash="c" * 64, device_name="Old laptop")
        PTUser.objects.filter(pk=self.user.pk).update(
            failed_login_attempts=3, locked_until=timezone.now() + timezone.timedelta(minutes=10))
        before = PTUser.objects.get(pk=self.user.pk).token_version
        code = generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)

        res = _confirm(self.client, self.user.email, code)

        assert res.status_code == 200
        after = PTUser.objects.get(pk=self.user.pk)
        assert bcrypt.checkpw(NEW.encode(), after.password_hash.encode())
        assert (after.failed_login_attempts, after.locked_until) == (0, None)
        assert after.token_version == before + 1
        assert not TrustedDevice.objects.filter(user=self.user).exists()
        assert "pt_access" not in res.cookies  # nobody is signed in by a reset
        assert any("Password Was Changed" in m.subject for m in mail.outbox)

    def test_a_wrong_code_and_an_unknown_address_answer_alike(self):
        generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
        wrong = _confirm(self.client, self.user.email, "000000")
        unknown = _confirm(self.client, "nobody@ravasco.com", "000000")
        assert wrong.status_code == unknown.status_code == 400
        assert wrong.json() == unknown.json()
        assert PTUser.objects.get(pk=self.user.pk).password_hash == self.user.password_hash

    def test_every_failing_path_costs_one_password_check_and_one_code_check(self, monkeypatch):
        """Timing must not say which addresses have an account or a code
        pending: an unknown address, a known one with no code, and a wrong
        code each run exactly one bcrypt password check and one code check."""
        from apps.api import auth_backend
        from apps.api.routers import password_views
        from apps.services import otp_service

        counts = {}
        real_check, real_verify, real_dummy = otp_service._check_code, auth_backend._verify_password, auth_backend._dummy_verify

        def spy(name, fn):
            def wrapped(*a, **k):
                counts[name] = counts.get(name, 0) + 1
                return fn(*a, **k)
            return wrapped

        monkeypatch.setattr(otp_service, "_check_code", spy("code", real_check))
        monkeypatch.setattr(password_views, "_verify_password", spy("password", real_verify))
        monkeypatch.setattr(password_views, "_dummy_verify", spy("password", real_dummy))

        seen = []
        for email, issue in (("nobody@ravasco.com", False), (self.user.email, False), (self.user.email, True)):
            if issue:
                generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
            counts.clear()
            assert _confirm(self.client, email, "000000").status_code == 400
            seen.append(dict(counts))
        assert seen == [{"code": 1, "password": 1}] * 3

    def test_a_code_works_once(self):
        code = generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
        assert _confirm(self.client, self.user.email, code).status_code == 200
        assert _confirm(self.client, self.user.email, code, new="AnotherNewPassw0rd!").status_code == 400

    def test_a_mismatched_confirmation_is_refused_without_spending_the_code(self):
        code = generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
        assert _confirm(self.client, self.user.email, code, confirm=NEW + "x").status_code == 400
        assert _confirm(self.client, self.user.email, code, new="1234567890").status_code == 400  # too weak
        assert _confirm(self.client, self.user.email, code).status_code == 200

    def test_the_current_password_is_refused_as_the_new_one(self):
        code = generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
        res = _confirm(self.client, self.user.email, code, new="Str0ngPassw0rd!")
        assert res.status_code == 400 and "different from the current" in res.json()["detail"]
        # Said only once the code checked out, so that code is spent.
        assert _confirm(self.client, self.user.email, code).status_code == 400
        code = generate_otp(self.user.email, OTPCode.Purpose.PASSWORD_RESET)
        assert _confirm(self.client, self.user.email, code).status_code == 200

    def test_a_wrong_code_says_nothing_about_whether_a_guess_is_the_password(self):
        """No code needed to ask: if a wrong code with the real password
        answered differently from a wrong code with any other password, the
        form would confirm password guesses with no lockout."""
        right_guess = _confirm(self.client, self.user.email, "000000", new="Str0ngPassw0rd!")
        wrong_guess = _confirm(self.client, self.user.email, "000000", new="N0tTheirPassw0rd!")
        assert right_guess.status_code == wrong_guess.status_code == 400
        assert right_guess.json() == wrong_guess.json()


@pytest.mark.django_db
class TestCodesAreBoundToTheirPurpose:
    def test_a_sign_in_code_cannot_reset_a_password_and_a_reset_code_cannot_sign_in(self):
        user = make_user(email="purpose@ravasco.com")
        login_code = generate_otp(user.email, OTPCode.Purpose.LOGIN)
        assert _confirm(APIClient(), user.email, login_code).status_code == 400

        from apps.services.otp_service import verify_otp
        reset_code = generate_otp(user.email, OTPCode.Purpose.PASSWORD_RESET)
        assert verify_otp(user.email, reset_code, OTPCode.Purpose.LOGIN) is False
        # Both codes are still live for their own flow.
        assert verify_otp(user.email, login_code, OTPCode.Purpose.LOGIN) is True
        assert verify_otp(user.email, reset_code, OTPCode.Purpose.PASSWORD_RESET) is True


@pytest.mark.django_db
class TestAdminsNoLongerSetPasswords:
    def test_a_password_in_an_admin_edit_is_refused_and_nothing_changes(self):
        admin = make_user(email="admin@ravasco.com", role="admin")
        target = make_user(email="target@ravasco.com")
        client = APIClient()
        client.force_authenticate(user=admin)
        res = client.patch(f"/api/auth/users/{target.user_id}", {"password": NEW, "fullName": "Renamed"}, format="json")
        assert res.status_code == 400
        after = PTUser.objects.get(pk=target.pk)
        assert (after.password_hash, after.full_name) == (target.password_hash, target.full_name)
        # The same edit without a password goes through.
        assert client.patch(f"/api/auth/users/{target.user_id}", {"fullName": "Renamed"}, format="json").status_code == 200
