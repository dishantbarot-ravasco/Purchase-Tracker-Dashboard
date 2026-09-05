"""
Tests for apps/services/security_alerts.py - added 2026-09-05 (hardening
pass) to close a real gap: this app previously had no alerting on any
security-relevant event (account lockouts, a burst of failed logins, a
sync pipeline failure), only passive log lines. Pure-function tests
against apps.core.models.PTUser + django.core.mail.outbox, no HTTP layer -
apps/api/tests/test_auth_flow.py's TestAccountLockout covers the
lockout-alert path end-to-end through the real login endpoint.
"""

import pytest
from django.core import mail
from django.core.cache import cache

from apps.api.tests.factories import make_user
from apps.services.security_alerts import notify_admins_sync_failure, record_failed_login_and_maybe_alert


@pytest.mark.django_db
class TestLoginBurstDetection:
    def setup_method(self):
        cache.clear()
        mail.outbox.clear()
        self.admin = make_user(email="admin-burst@ravasco.com", role="admin")

    def test_alerts_once_threshold_is_crossed(self):
        for _ in range(14):
            record_failed_login_and_maybe_alert()
        assert not mail.outbox  # below threshold (15) - no alert yet

        record_failed_login_and_maybe_alert()  # 15th - crosses the threshold
        alerts = [m for m in mail.outbox if "Unusual Login Activity" in m.subject]
        assert len(alerts) == 1
        assert self.admin.email in alerts[0].to

    def test_does_not_alert_twice_within_the_suppression_window(self):
        for _ in range(20):
            record_failed_login_and_maybe_alert()
        alerts = [m for m in mail.outbox if "Unusual Login Activity" in m.subject]
        assert len(alerts) == 1  # not one per attempt past the threshold


@pytest.mark.django_db
class TestSyncFailureAlert:
    def setup_method(self):
        mail.outbox.clear()
        self.admin = make_user(email="admin-sync@ravasco.com", role="admin")

    def test_sends_an_alert_with_plant_and_command_detail(self):
        notify_admins_sync_failure("hrs", "sync_mir", detail="Drive file not found")
        alerts = [m for m in mail.outbox if "Sync Failure" in m.subject]
        assert len(alerts) == 1
        assert "hrs" in alerts[0].subject
        assert "sync_mir" in alerts[0].subject
        assert "Drive file not found" in alerts[0].body
        assert self.admin.email in alerts[0].to

    def test_no_admins_means_no_error(self):
        """Confirms this is genuinely best-effort - an account roster with
        zero active admins (shouldn't normally happen, but not this
        function's job to enforce that) must not raise."""
        self.admin.is_active = False
        self.admin.save()
        notify_admins_sync_failure("vapi", "sync_stock")  # must not raise
