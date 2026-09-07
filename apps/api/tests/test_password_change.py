"""
Integration tests for self-service "Change Password" (apps/api/routers/
password_views.py) - OTP-gated the same way new-device login is, added
2026-09-07 (project owner request).
"""

import bcrypt
import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import OTPCode, PTUser


@pytest.mark.django_db
class TestChangePasswordRequest:
    def test_request_sends_otp_and_returns_202(self):
        user = make_user(email="changeme@ravasco.com", role="viewer")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post("/api/auth/change-password/request")
        assert response.status_code == 202
        assert OTPCode.objects.filter(email="changeme@ravasco.com").exists()

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

    def _request_otp(self):
        self.client.post("/api/auth/change-password/request")
        return OTPCode.objects.get(email=self.user.email)

    def _real_otp_code(self):
        """generate_otp() only stores a bcrypt hash - recover the plaintext
        the same way otp_service.py's own tests must: call generate_otp()
        directly rather than trying to reverse the hash."""
        from apps.services.otp_service import generate_otp
        return generate_otp(self.user.email)

    def test_confirm_with_correct_otp_changes_password(self):
        code = self._real_otp_code()
        response = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "BrandNewPassw0rd!"},
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
            {"otp": "000000", "newPassword": "BrandNewPassw0rd!"},
            format="json",
        )
        assert response.status_code == 400
        self.user.refresh_from_db()
        assert self.user.password_hash == old_hash

    def test_confirm_enforces_password_strength_policy(self):
        code = self._real_otp_code()
        response = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "1234567890"},
            format="json",
        )
        assert response.status_code == 400
        self.user.refresh_from_db()
        # A rejected-for-weakness attempt must not have consumed the OTP in
        # a way that changed anything - the original hash must still stand.
        assert not bcrypt.checkpw(b"1234567890", self.user.password_hash.encode())

    def test_confirm_otp_is_single_use(self):
        code = self._real_otp_code()
        first = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "FirstNewPassw0rd!"},
            format="json",
        )
        assert first.status_code == 200
        second = self.client.post(
            "/api/auth/change-password/confirm",
            {"otp": code, "newPassword": "SecondNewPassw0rd!"},
            format="json",
        )
        assert second.status_code == 400

    def test_confirm_requires_authentication(self):
        client = APIClient()
        response = client.post(
            "/api/auth/change-password/confirm",
            {"otp": "123456", "newPassword": "BrandNewPassw0rd!"},
            format="json",
        )
        assert response.status_code == 401
