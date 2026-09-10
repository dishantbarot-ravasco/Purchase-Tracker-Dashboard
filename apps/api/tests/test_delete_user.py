"""
Integration tests for DELETE /api/auth/users/<id> (apps/api/routers/
users_views.py's update_user(), which now also handles DELETE) - the
hardcoded-to-one-account user-delete feature added to the Admin Panel.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.audit_log import PTAuditLog
from apps.core.models import PTUser


@pytest.mark.django_db
class TestDeleteUser:
    def test_allowed_admin_can_delete_a_user(self):
        """The one hardcoded account can delete another user; the row is
        actually gone afterwards, and an audit row is written."""
        client = APIClient()
        allowed_admin = make_user(email="dishant.barot@ravasco.com", role="admin")
        client.force_authenticate(user=allowed_admin)
        target = make_user(email="target@ravasco.com", role="editor")

        response = client.delete(f"/api/auth/users/{target.user_id}")

        assert response.status_code == 204
        assert not PTUser.objects.filter(pk=target.user_id).exists()
        assert PTAuditLog.objects.filter(
            action=PTAuditLog.ACTION_USER_DELETED, actor_email="dishant.barot@ravasco.com",
        ).exists()

    def test_other_admin_cannot_delete_a_user(self):
        """Any admin account other than the hardcoded one gets a 403, and
        the target row must survive untouched."""
        client = APIClient()
        other_admin = make_user(email="someone-else@ravasco.com", role="admin")
        client.force_authenticate(user=other_admin)
        target = make_user(email="target2@ravasco.com", role="editor")

        response = client.delete(f"/api/auth/users/{target.user_id}")

        assert response.status_code == 403
        assert PTUser.objects.filter(pk=target.user_id).exists()

    def test_case_insensitive_email_match(self):
        """The hardcoded check must not be defeated by a differently-cased
        stored email."""
        client = APIClient()
        allowed_admin = make_user(email="Dishant.Barot@Ravasco.com", role="admin")
        client.force_authenticate(user=allowed_admin)
        target = make_user(email="target3@ravasco.com", role="editor")

        response = client.delete(f"/api/auth/users/{target.user_id}")

        assert response.status_code == 204

    def test_non_admin_role_is_rejected_before_the_email_check(self):
        """IsAdmin gates this endpoint first - even the hardcoded email
        can't delete anything while holding a non-admin role."""
        client = APIClient()
        non_admin = make_user(email="dishant.barot@ravasco.com", role="editor")
        client.force_authenticate(user=non_admin)
        target = make_user(email="target4@ravasco.com", role="editor")

        response = client.delete(f"/api/auth/users/{target.user_id}")

        assert response.status_code == 403
        assert PTUser.objects.filter(pk=target.user_id).exists()

    def test_cannot_delete_the_last_active_admin(self):
        """Deleting the only remaining active admin must 400, not leave the
        system with zero admins able to sign in."""
        client = APIClient()
        allowed_admin = make_user(email="dishant.barot@ravasco.com", role="admin")
        client.force_authenticate(user=allowed_admin)

        response = client.delete(f"/api/auth/users/{allowed_admin.user_id}")

        assert response.status_code == 400
        assert PTUser.objects.filter(pk=allowed_admin.user_id).exists()

    def test_deleting_unknown_user_returns_404(self):
        client = APIClient()
        allowed_admin = make_user(email="dishant.barot@ravasco.com", role="admin")
        client.force_authenticate(user=allowed_admin)

        response = client.delete("/api/auth/users/999999")

        assert response.status_code == 404

    def test_patch_still_works_on_the_shared_view(self):
        """update_user() now branches on request.method - make sure adding
        the DELETE branch didn't disturb the existing PATCH behavior."""
        client = APIClient()
        admin = make_user(email="admin-plain@ravasco.com", role="admin")
        client.force_authenticate(user=admin)
        target = make_user(email="target5@ravasco.com", role="editor")

        response = client.patch(f"/api/auth/users/{target.user_id}", {"designation": "QA"}, format="json")

        assert response.status_code == 200
        assert response.json()["designation"] == "QA"
