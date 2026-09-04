"""
Integration tests for the in-app password reset added to
apps/api/routers/users_views.py's update_user() (2026-09-04) - closes the
"No in-app password reset" gap CLAUDE.md previously documented (admin.html's
Users panel used to only cover create/edit-role/activate/deactivate, an
existing user's forgotten password could only be reset via `manage.py
create_pt_user` CLI access).
"""

import bcrypt
import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import PTUser


@pytest.mark.django_db
class TestPasswordReset:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)
        self.user = make_user(email="target@ravasco.com", role="viewer")
        self._original_hash = self.user.password_hash

    def test_admin_can_reset_password(self):
        response = self.client.patch(
            f"/api/auth/users/{self.user.user_id}", {"password": "NewStr0ngPassw0rd!"}, format="json"
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.password_hash != self._original_hash
        assert bcrypt.checkpw(b"NewStr0ngPassw0rd!", self.user.password_hash.encode("utf-8"))

    def test_blank_password_leaves_existing_password_untouched(self):
        response = self.client.patch(
            f"/api/auth/users/{self.user.user_id}", {"password": "", "fullName": "Renamed"}, format="json"
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.password_hash == self._original_hash
        assert self.user.full_name == "Renamed"

    def test_omitted_password_leaves_existing_password_untouched(self):
        response = self.client.patch(
            f"/api/auth/users/{self.user.user_id}", {"designation": "Buyer"}, format="json"
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.password_hash == self._original_hash

    def test_short_password_rejected(self):
        response = self.client.patch(
            f"/api/auth/users/{self.user.user_id}", {"password": "short"}, format="json"
        )
        assert response.status_code == 400
        self.user.refresh_from_db()
        assert self.user.password_hash == self._original_hash

    def test_non_admin_cannot_reset_password(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="editor@ravasco.com", role="editor"))
        response = client.patch(
            f"/api/auth/users/{self.user.user_id}", {"password": "NewStr0ngPassw0rd!"}, format="json"
        )
        assert response.status_code == 403
        self.user.refresh_from_db()
        assert self.user.password_hash == self._original_hash

    def test_reset_password_actually_authenticates(self):
        self.client.patch(f"/api/auth/users/{self.user.user_id}", {"password": "FreshPassw0rd!"}, format="json")
        from django.contrib.auth import authenticate
        authed = authenticate(request=None, email="target@ravasco.com", password="FreshPassw0rd!")
        assert authed is not None
        assert authed.pk == self.user.pk
