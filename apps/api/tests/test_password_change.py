"""
Integration tests for self-service "Change Password" (apps/api/routers/
password_views.py) - OTP-gated the same way new-device login is, added
2026-09-07 (project owner request).
"""

import bcrypt
import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import OTPCode


@pytest.mark.django_db
class TestChangePasswordRequest:
    def test_request_sends_otp_and_returns_202(self):
        user = make_user(email="changeme@ravasco.com", role="viewer")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post("/api/auth/change-password/request", {"currentPassword": "Str0ngPassw0rd!"}, format="json")
        assert response.status_code == 202
        assert OTPCode.objects.filter(email="changeme@ravasco.com", purpose="password_change").exists()

    def test_a_wrong_current_password_sends_nothing_and_counts_towards_the_lockout(self):
        """A session left open on someone's desk must not be enough to
        change the password (2026-10-02)."""
        user = make_user(email="desk@ravasco.com", role="viewer")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post("/api/auth/change-password/request", {"currentPassword": "not-it"}, format="json")
        assert response.status_code == 400
        assert not OTPCode.objects.filter(email="desk@ravasco.com").exists()
        user.refresh_from_db()
        assert user.failed_login_attempts == 1

    def test_request_requires_authentication(self):
        client = APIClient()
        response = client.post("/api/auth/change-password/request")
        assert response.status_code == 401


@pytest.mark.django_db
class TestChangePasswordConfirm:
    def setup_method(self):
        self.user = make_user(email="confirmchange@ravasco.com", role="editor")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _real_otp_code(self):
        """generate_otp() only stores a bcrypt hash - recover the plaintext
        the same way otp_service.py's own tests must: call generate_otp()
        directly rather than trying to reverse the hash."""
        from apps.services.otp_service import generate_otp
        return generate_otp(self.user.email, "password_change")

    def test_confirm_with_correct_otp_changes_password(self):
        code = self._real_otp_code()
        response = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"},
            format="json",
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert bcrypt.checkpw(b"BrandNewPassw0rd!", self.user.password_hash.encode())

    def test_confirm_with_wrong_otp_rejected_and_password_unchanged(self):
        self._real_otp_code()
        old_hash = self.user.password_hash
        response = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": "000000", "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"},
            format="json",
        )
        assert response.status_code == 400
        self.user.refresh_from_db()
        assert self.user.password_hash == old_hash

    def test_confirm_enforces_password_strength_policy(self):
        code = self._real_otp_code()
        response = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "1234567890", "confirmPassword": "1234567890"},
            format="json",
        )
        assert response.status_code == 400
        self.user.refresh_from_db()
        # A rejected-for-weakness attempt must not have consumed the OTP in
        # a way that changed anything - the original hash must still stand.
        assert not bcrypt.checkpw(b"1234567890", self.user.password_hash.encode())

    def test_a_weak_password_leaves_the_code_usable(self):
        """verify_otp() uses the code up, and it used to run before the
        strength check - so a rejected password cost the user their code."""
        code = self._real_otp_code()
        weak = self.client.post("/api/auth/change-password/confirm",
                                {"otp": code, "newPassword": "1234567890", "confirmPassword": "1234567890"}, format="json")
        assert weak.status_code == 400
        retry = self.client.post("/api/auth/change-password/confirm",
                                 {"otp": code, "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"}, format="json")
        assert retry.status_code == 200

    def test_confirm_otp_is_single_use(self):
        code = self._real_otp_code()
        first = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "FirstNewPassw0rd!", "confirmPassword": "FirstNewPassw0rd!"},
            format="json",
        )
        assert first.status_code == 200
        second = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "SecondNewPassw0rd!", "confirmPassword": "SecondNewPassw0rd!"},
            format="json",
        )
        assert second.status_code == 400

    def test_a_confirmation_that_does_not_match_is_refused_and_the_code_kept(self):
        code = self._real_otp_code()
        res = self.client.post("/api/auth/change-password/confirm",
                               {"otp": code, "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd?"},
                               format="json")
        assert res.status_code == 400
        retry = self.client.post("/api/auth/change-password/confirm",
                                 {"otp": code, "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"},
                                 format="json")
        assert retry.status_code == 200

    def test_the_current_password_is_refused_as_the_new_one(self):
        code = self._real_otp_code()
        res = self.client.post("/api/auth/change-password/confirm",
                               {"otp": code, "newPassword": "Str0ngPassw0rd!", "confirmPassword": "Str0ngPassw0rd!"},
                               format="json")
        assert res.status_code == 400 and "different from the current" in res.json()["detail"]

    def test_a_wrong_code_says_nothing_about_whether_a_guess_is_the_password(self):
        self._real_otp_code()
        answers = [self.client.post("/api/auth/change-password/confirm",
                                    {"otp": "000000", "newPassword": guess, "confirmPassword": guess}, format="json")
                   for guess in ("Str0ngPassw0rd!", "N0tTheirPassw0rd!")]
        assert [a.status_code for a in answers] == [400, 400]
        assert answers[0].json() == answers[1].json()

    def test_a_reset_code_cannot_change_a_password(self):
        from apps.services.otp_service import generate_otp
        code = generate_otp(self.user.email, "password_reset")
        res = self.client.post("/api/auth/change-password/confirm",
                               {"otp": code, "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"},
                               format="json")
        assert res.status_code == 400

    def test_confirm_requires_authentication(self):
        client = APIClient()
        response = client.post(
            "/api/auth/change-password/confirm",
            {"otp": "123456", "newPassword": "BrandNewPassw0rd!", "confirmPassword": "BrandNewPassw0rd!"},
            format="json",
        )
        assert response.status_code == 401
