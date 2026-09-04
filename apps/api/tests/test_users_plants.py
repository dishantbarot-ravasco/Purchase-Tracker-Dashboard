"""
Integration tests for the `plants` field added to PTUser create/update
(apps/api/routers/users_views.py) and surfaced on GET /api/auth/me
(apps/api/auth_views.py's whoami) - the frontend uses the latter to decide
which inline-edit pencils to render (shared.js's canEditField()).
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import PTUser


@pytest.mark.django_db
class TestUsersPlants:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)

    def test_create_user_with_plants(self):
        """Creating a user with an explicit plants list persists it on the
        PTUser row and echoes it back in the create response."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "new@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor", "plants": ["hrs", "vapi"]},
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["plants"] == ["hrs", "vapi"]
        user = PTUser.objects.get(email="new@ravasco.com")
        assert user.plants == ["hrs", "vapi"]

    def test_create_user_without_plants_defaults_to_empty_list(self):
        """Omitting `plants` entirely on create must default to an empty
        list, not null or a crash."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "new2@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor"},
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["plants"] == []

    def test_create_user_rejects_unknown_plant(self):
        """A plant key outside the known set (hrs/achhad/vapi) must be
        rejected with a 400 rather than silently accepted."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "new3@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor", "plants": ["mars"]},
            format="json",
        )
        assert response.status_code == 400

    def test_update_user_plants(self):
        """PATCHing `plants` on an existing user replaces the list."""
        user = make_user(email="existing@ravasco.com", role="editor")
        response = self.client.patch(f"/api/auth/users/{user.user_id}", {"plants": ["achhad"]}, format="json")
        assert response.status_code == 200
        assert response.json()["plants"] == ["achhad"]
        user.refresh_from_db()
        assert user.plants == ["achhad"]


@pytest.mark.django_db
class TestWhoamiExposesPlants:
    def test_whoami_includes_plants(self):
        """GET /api/auth/me must expose the caller's own plants list, since
        the frontend's canEditField() reads it from there to decide which
        inline-edit pencils to render."""
        client = APIClient()
        user = make_user(email="scoped@ravasco.com", role="editor", plants=["vapi"])
        client.force_authenticate(user=user)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["plants"] == ["vapi"]
