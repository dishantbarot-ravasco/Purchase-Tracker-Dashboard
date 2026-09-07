"""
Integration tests for the password-strength policy added 2026-09-05
(hardening pass) to apps/api/routers/users_views.py's create_user/
update_user (_validate_password_strength) - replaces the previous
length-only (>= 8 chars) check with: minimum 10 chars, not purely numeric,
not identical to the account's own email local-part. Deliberately modest
(not a full breach-list/entropy policy) so it doesn't lock out reasonable
existing workflows - see that function's own docstring for the reasoning.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import PTUser


@pytest.mark.django_db
class TestCreateUserPasswordStrength:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)

    def test_rejects_password_under_10_chars(self):
        """A password shorter than 10 characters (even one that used to
        pass the old >= 8 check) must now be rejected."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "short@ravasco.com", "password": "Sh0rt1!", "role": "viewer"},
            format="json",
        )
        assert response.status_code == 400
        assert not PTUser.objects.filter(email="short@ravasco.com").exists()

    def test_rejects_purely_numeric_password(self):
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "numeric@ravasco.com", "password": "1234567890", "role": "viewer"},
            format="json",
        )
        assert response.status_code == 400
        assert not PTUser.objects.filter(email="numeric@ravasco.com").exists()

    def test_rejects_password_matching_email_local_part(self):
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "newuser123@ravasco.com", "password": "newuser123", "role": "viewer"},
            format="json",
        )
        assert response.status_code == 400
        assert not PTUser.objects.filter(email="newuser123@ravasco.com").exists()

    def test_accepts_a_reasonable_password(self):
        """A password that satisfies the new policy must still succeed -
        confirms the tightened rule isn't accidentally rejecting everything."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "reasonable@ravasco.com", "password": "Str0ngPassw0rd!", "role": "viewer", "fullName": "Reasonable Person"},
            format="json",
        )
        assert response.status_code == 201
        assert PTUser.objects.filter(email="reasonable@ravasco.com").exists()


@pytest.mark.django_db
class TestUpdateUserPasswordStrength:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin2@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)
        self.target = make_user(email="target@ravasco.com", role="viewer")

    def test_rejects_weak_password_reset(self):
        old_hash = self.target.password_hash
        response = self.client.patch(
            f"/api/auth/users/{self.target.user_id}", {"password": "weak1"}, format="json"
        )
        assert response.status_code == 400
        self.target.refresh_from_db()
        assert self.target.password_hash == old_hash

    def test_accepts_strong_password_reset(self):
        response = self.client.patch(
            f"/api/auth/users/{self.target.user_id}", {"password": "N3wStrongPassword!"}, format="json"
        )
        assert response.status_code == 200
        self.target.refresh_from_db()
        assert self.target.password_hash  # changed, don't assert exact hash
