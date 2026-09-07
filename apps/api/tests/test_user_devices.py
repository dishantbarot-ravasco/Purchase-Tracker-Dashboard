"""
Integration tests for the Trusted Devices admin endpoints
(apps/api/routers/users_views.py's list_user_devices/revoke_user_device,
added 2026-09-04) and the audit-log rows create_user/update_user/
revoke_user_device now write (apps/core/audit_log.py). Closes the gap
notify_admins_new_device_login() (apps/services/device_service.py) already
promised in its own email body ("...or revoke the device from the admin
panel") before any such revoke endpoint existed.
"""
import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.audit_log import PTAuditLog
from apps.core.models import TrustedDevice
from apps.services.device_service import register_device


def _make_device(user, name="Test Device", ip="127.0.0.1"):
    """Register a real TrustedDevice row (through the real hashing path,
    not a hand-built row) for `user`, returning it."""
    rf = RequestFactory()
    request = rf.get("/")
    request.META["HTTP_USER_AGENT"] = name
    request.META["REMOTE_ADDR"] = ip
    register_device(HttpResponse(), user.user_id, request)
    return TrustedDevice.objects.filter(user_id=user.user_id).latest("created_at")


@pytest.mark.django_db
class TestListUserDevices:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin-devices@ravasco.com", role="admin")
        self.target = make_user(email="target-devices@ravasco.com")

    def test_admin_can_list_a_users_devices(self):
        device = _make_device(self.target)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/auth/users/{self.target.user_id}/devices")
        assert response.status_code == 200
        devices = response.json()["devices"]
        assert len(devices) == 1
        assert devices[0]["id"] == device.pk
        assert devices[0]["deviceName"] == device.device_name

    def test_device_hash_is_never_exposed_in_the_response(self):
        """The whole point of hashing is defeated if the hash (or anything
        derived from it) leaks back out over the API."""
        _make_device(self.target)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/auth/users/{self.target.user_id}/devices")
        body = response.json()
        assert "deviceTokenHash" not in body["devices"][0]
        assert "device_token_hash" not in body["devices"][0]
        assert "deviceToken" not in body["devices"][0]

    def test_non_admin_cannot_list_devices(self):
        viewer = make_user(email="viewer-devices@ravasco.com", role="viewer")
        _make_device(self.target)
        self.client.force_authenticate(user=viewer)
        response = self.client.get(f"/api/auth/users/{self.target.user_id}/devices")
        assert response.status_code == 403

    def test_unknown_user_id_404s(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/auth/users/999999/devices")
        assert response.status_code == 404


@pytest.mark.django_db
class TestRevokeUserDevice:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin-revoke@ravasco.com", role="admin")
        self.target = make_user(email="target-revoke@ravasco.com")

    def test_admin_can_revoke_a_device(self):
        device = _make_device(self.target)
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(f"/api/auth/users/{self.target.user_id}/devices/{device.pk}")
        assert response.status_code == 204
        assert not TrustedDevice.objects.filter(pk=device.pk).exists()

    def test_revoking_writes_an_audit_row(self):
        device = _make_device(self.target)
        self.client.force_authenticate(user=self.admin)
        self.client.delete(f"/api/auth/users/{self.target.user_id}/devices/{device.pk}")

        row = PTAuditLog.objects.filter(action=PTAuditLog.ACTION_DEVICE_REVOKED).latest("timestamp")
        assert row.actor_email == self.admin.email
        assert self.target.email in row.detail

    def test_revoking_unknown_device_404s(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(f"/api/auth/users/{self.target.user_id}/devices/999999")
        assert response.status_code == 404

    def test_cannot_revoke_another_users_device_via_mismatched_ids(self):
        """A device row belongs to exactly one user - passing a real
        device_id under a DIFFERENT user_id in the URL must 404, not revoke
        the device anyway. Guards the query filtering on both pk and
        user_id together, not pk alone."""
        device = _make_device(self.target)
        other_user = make_user(email="other-revoke@ravasco.com")
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(f"/api/auth/users/{other_user.user_id}/devices/{device.pk}")
        assert response.status_code == 404
        assert TrustedDevice.objects.filter(pk=device.pk).exists()

    def test_non_admin_cannot_revoke(self):
        editor = make_user(email="editor-revoke@ravasco.com", role="editor")
        device = _make_device(self.target)
        self.client.force_authenticate(user=editor)
        response = self.client.delete(f"/api/auth/users/{self.target.user_id}/devices/{device.pk}")
        assert response.status_code == 403
        assert TrustedDevice.objects.filter(pk=device.pk).exists()

    def test_revoked_device_no_longer_trusted(self):
        """The actual security property this endpoint exists for: once
        revoked, that device's own token must stop passing
        is_trusted_device() - not just disappear from the list."""
        from django.http import HttpResponse
        from django.test import RequestFactory

        from apps.services.device_service import DEVICE_COOKIE_NAME, is_trusted_device, register_device

        rf = RequestFactory()
        token = register_device(HttpResponse(), self.target.user_id, rf.get("/"))
        device = TrustedDevice.objects.get(user_id=self.target.user_id)

        self.client.force_authenticate(user=self.admin)
        self.client.delete(f"/api/auth/users/{self.target.user_id}/devices/{device.pk}")

        check_request = rf.get("/")
        check_request.COOKIES[DEVICE_COOKIE_NAME] = token
        assert is_trusted_device(check_request, self.target.user_id) is False


@pytest.mark.django_db
class TestUserManagementAuditLog:
    """create_user()/update_user() (apps/api/routers/users_views.py) used to
    write no permanent audit trail at all - only a rotating logs/app.log
    line. Both now write a PTAuditLog row (2026-09-04)."""

    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin-audit@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)

    def test_create_user_writes_an_audit_row(self):
        response = self.client.post(
            "/api/auth/users/create",
            {"email": "new-audit@ravasco.com", "password": "Str0ngPassw0rd!", "role": "viewer", "fullName": "Audit Test User"},
            format="json",
        )
        assert response.status_code == 201
        row = PTAuditLog.objects.filter(action=PTAuditLog.ACTION_USER_CREATED).latest("timestamp")
        assert row.actor_email == self.admin.email
        assert "new-audit@ravasco.com" in row.detail

    def test_role_change_is_called_out_in_the_audit_detail(self):
        """A privilege escalation (viewer -> admin) is exactly the kind of
        change that must be visible in the audit detail text, not folded
        into a generic 'user updated' row."""
        target = make_user(email="promote-audit@ravasco.com", role="viewer")
        response = self.client.patch(f"/api/auth/users/{target.user_id}", {"role": "admin"}, format="json")
        assert response.status_code == 200
        row = PTAuditLog.objects.filter(action=PTAuditLog.ACTION_USER_UPDATED).latest("timestamp")
        assert "viewer -> admin" in row.detail

    def test_password_reset_is_called_out_in_the_audit_detail_without_leaking_it(self):
        target = make_user(email="reset-audit@ravasco.com")
        response = self.client.patch(f"/api/auth/users/{target.user_id}", {"password": "AnotherStr0ngPass!"}, format="json")
        assert response.status_code == 200
        row = PTAuditLog.objects.filter(action=PTAuditLog.ACTION_USER_UPDATED).latest("timestamp")
        assert "password reset" in row.detail
        assert "AnotherStr0ngPass!" not in row.detail
