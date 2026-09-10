"""
Integration test for GET /api/imports/sync-status now also exposing
rodtepInProgress/advanceLicenseInProgress (added 2026-09-10, alongside
wiring "Refresh Data" to trigger these two company-wide syncs - see
frontend/js/main.js's triggerRealSyncAndRefresh()). Before this, the two
is_rodtep_sync_in_progress()/is_advance_license_sync_in_progress() helpers
(apps/services/sync_trigger.py) existed but nothing ever read them.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user


@pytest.mark.django_db
class TestSyncStatusCompanyWideFlags:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(email="viewer@ravasco.com", role="viewer"))

    def test_flags_are_false_when_nothing_is_running(self):
        response = self.client.get("/api/imports/sync-status")
        assert response.status_code == 200
        assert response.json()["rodtepInProgress"] is False
        assert response.json()["advanceLicenseInProgress"] is False

    def test_rodtep_in_progress_flag_reflects_the_real_lock(self):
        cache.set("sync_trigger_rodtep_in_progress", True, timeout=60)
        response = self.client.get("/api/imports/sync-status")
        assert response.json()["rodtepInProgress"] is True
        assert response.json()["advanceLicenseInProgress"] is False

    def test_advance_license_in_progress_flag_reflects_the_real_lock(self):
        cache.set("sync_trigger_advance_license_in_progress", True, timeout=60)
        response = self.client.get("/api/imports/sync-status")
        assert response.json()["advanceLicenseInProgress"] is True
        assert response.json()["rodtepInProgress"] is False
