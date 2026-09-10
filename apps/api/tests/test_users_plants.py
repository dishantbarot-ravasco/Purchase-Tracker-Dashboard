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
            {"email": "new@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor", "plants": ["hrs", "vapi"], "fullName": "New Editor"},
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
            {"email": "new2@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor", "fullName": "New Editor Two"},
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["plants"] == []

    def test_create_user_rejects_unknown_plant(self):
        """A plant key outside the known set (hrs/achhad/vapi) must be
        rejected with a 400 rather than silently accepted."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "new3@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor", "plants": ["mars"], "fullName": "New Editor Three"},
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

    def test_create_user_requires_full_name(self):
        """Full name became a required field 2026-09-07 (project owner) -
        omitting it must 400, not silently create a nameless account."""
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "noname@ravasco.com", "password": "Str0ngPassw0rd!", "role": "editor"},
            format="json",
        )
        assert response.status_code == 400
        assert not PTUser.objects.filter(email="noname@ravasco.com").exists()

    def test_update_user_rejects_blank_full_name(self):
        """A PATCH that explicitly blanks out fullName must 400 too - full
        name is required going forward for existing accounts, not just new
        ones."""
        user = make_user(email="named@ravasco.com", role="editor")
        response = self.client.patch(f"/api/auth/users/{user.user_id}", {"fullName": "  "}, format="json")
        assert response.status_code == 400

    def test_create_user_forces_empty_plants_for_admin_role(self):
        """An admin account's own access is never plant-scoped (every
        admin-only endpoint ignores PTUser.plants entirely) - a `plants`
        list submitted alongside role=admin must be silently dropped to []
        rather than persisted, so it can never look like it's restricting
        an admin when it actually isn't."""
        response = self.client.post(
            "/api/auth/users/create",
            {
                "email": "newadmin@ravasco.com", "password": "Str0ngPassw0rd!",
                "role": "admin", "plants": ["hrs"], "fullName": "New Admin",
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["plants"] == []
        user = PTUser.objects.get(email="newadmin@ravasco.com")
        assert user.plants == []

    def test_cannot_deactivate_the_last_active_admin(self):
        """PATCH /api/auth/users/<id> {"isActive": false} on the only active
        admin must 400, same protection DELETE already has (see
        test_delete_user.py) - this path had no test coverage at all before
        this (found during a full-codebase audit, 2026-09-10)."""
        response = self.client.patch(f"/api/auth/users/{self.admin.user_id}", {"isActive": False}, format="json")
        assert response.status_code == 400
        self.admin.refresh_from_db()
        assert self.admin.is_active is True

    def test_cannot_demote_the_last_active_admin_to_a_non_admin_role(self):
        """Same protection, the other way it can be triggered: changing role
        away from admin (not just isActive) on the only active admin."""
        response = self.client.patch(f"/api/auth/users/{self.admin.user_id}", {"role": "editor"}, format="json")
        assert response.status_code == 400
        self.admin.refresh_from_db()
        assert self.admin.role == "admin"

    def test_can_deactivate_an_admin_when_another_active_admin_remains(self):
        other_admin = make_user(email="other-admin@ravasco.com", role="admin")
        response = self.client.patch(f"/api/auth/users/{other_admin.user_id}", {"isActive": False}, format="json")
        assert response.status_code == 200
        other_admin.refresh_from_db()
        assert other_admin.is_active is False

    def test_update_user_forces_empty_plants_when_role_changed_to_admin(self):
        """Promoting an existing plant-scoped editor to admin must clear
        `plants` too, not leave a stale, now-meaningless restriction on the
        row."""
        user = make_user(email="promoted@ravasco.com", role="editor", plants=["hrs"])
        response = self.client.patch(f"/api/auth/users/{user.user_id}", {"role": "admin"}, format="json")
        assert response.status_code == 200
        assert response.json()["plants"] == []
        user.refresh_from_db()
        assert user.plants == []


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
