"""
Regression tests for the 2026-09-04 "no silent failures" pass:

1. A new-device login whose OTP could not even be generated used to still
   return {"status": "device_verify"} (a fake success - see
   PTTokenObtainPairSerializer.validate() in apps/api/auth_serializers.py)
   instead of a clear error. Now returns a real 400 with a readable
   message, matching how the Google OAuth login path already handled this
   exact same failure.

2. match_hrs/match_achhad/match_vapi previously had no SyncRun tracking at
   all - a real failure inside run_full_match() was invisible anywhere in
   the app (see each match_*.py command's own header comment). They now
   record a SyncRun.Source.MATCH row on both success and failure, the same
   way every sync_* command already did, and /sync-status now returns
   errorDetail for any failed source so an admin can see *why*, not just
   that something failed.
"""
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import SyncRun

LOGIN_URL = "/api/auth/login"


@pytest.mark.django_db
class TestLoginNeverFakesSuccess:
    def setup_method(self):
        cache.clear()
        self.client = APIClient()
        self.password = "Str0ngPassw0rd!"
        self.user = make_user(password=self.password)

    def test_otp_generation_failure_returns_a_real_error_not_device_verify(self):
        """If send_device_otp() itself raises (the OTP was never created,
        not just a background email-send hiccup), the response must be a
        clear error - not the misleading {"status": "device_verify"} this
        used to return, which sent the user to a code-entry screen that
        could never accept any code."""
        with patch("apps.api.auth_serializers.send_device_otp", side_effect=RuntimeError("db write failed")):
            response = self.client.post(LOGIN_URL, {"email": self.user.email, "password": self.password}, format="json")
        assert response.status_code == 400
        # DRF wraps a dict-shaped ValidationError's values in a list (one
        # entry per validation message on that "field") - login.js's own
        # postJson() already handles both a plain string and this list
        # shape when building the toast message, so asserting on the list
        # form here matches what actually reaches the wire.
        body = response.json()
        assert "detail" in body
        messages = body["detail"] if isinstance(body["detail"], list) else [body["detail"]]
        assert any("could not send" in m.lower() for m in messages)
        # Must not look like a success response at all.
        assert body != {"status": "device_verify"}


@pytest.mark.django_db
class TestMatchCommandsRecordSyncRun:
    def test_match_hrs_records_a_success_syncrun_row(self):
        """A clean run (no data to match, but no error either) must still
        leave a real, queryable trace - previously there was none at all."""
        assert not SyncRun.objects.filter(plant=SyncRun.Plant.HRS, source=SyncRun.Source.MATCH).exists()
        call_command("match_hrs")
        run = SyncRun.objects.filter(plant=SyncRun.Plant.HRS, source=SyncRun.Source.MATCH).latest("started_at")
        assert run.status == SyncRun.Status.SUCCESS
        assert run.error_detail == ""

    def test_match_hrs_failure_is_recorded_with_a_readable_reason_and_exits_nonzero(self):
        """A real exception inside run_full_match() must produce a FAILED
        SyncRun row carrying the actual reason (previously: nowhere at all,
        only logs/app.log) and a non-zero exit so sync_trigger.py's own
        SystemExit handling still applies."""
        with patch("apps.core.management.commands.match_hrs.run_full_match", side_effect=RuntimeError("matching blew up")):
            with pytest.raises(SystemExit):
                call_command("match_hrs")
        run = SyncRun.objects.filter(plant=SyncRun.Plant.HRS, source=SyncRun.Source.MATCH).latest("started_at")
        assert run.status == SyncRun.Status.FAILED
        assert "matching blew up" in run.error_detail

    def test_match_achhad_and_match_vapi_also_record_syncrun_rows(self):
        """Same fix, same shape, for the other two plants - guards against
        the fix being applied to HRS only and the other two copy-paste
        commands left as they were."""
        call_command("match_achhad")
        call_command("match_vapi")
        assert SyncRun.objects.filter(plant=SyncRun.Plant.RTP_ACHHAD, source=SyncRun.Source.MATCH, status=SyncRun.Status.SUCCESS).exists()
        assert SyncRun.objects.filter(plant=SyncRun.Plant.RTP_VAPI, source=SyncRun.Source.MATCH, status=SyncRun.Status.SUCCESS).exists()


@pytest.mark.django_db
class TestSyncStatusExposesErrorDetail:
    def setup_method(self):
        self.client = APIClient()
        self.admin = make_user(email="admin-syncstatus@ravasco.com", role="admin")
        self.client.force_authenticate(user=self.admin)

    def test_failed_sync_run_error_detail_is_returned_by_the_api(self):
        """SyncRun.error_detail was always recorded on a failure but never
        returned by /sync-status until now - an admin used to see only a
        red 'failed' badge with no way to find out why."""
        from django.utils import timezone

        SyncRun.objects.create(
            plant=SyncRun.Plant.HRS,
            source=SyncRun.Source.PO_CSV,
            status=SyncRun.Status.FAILED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_detail="Drive file not found: Master_HRS_SILVASSA_Domestic_Purchase_Data.csv",
        )
        response = self.client.get("/api/sync-status")
        assert response.status_code == 200
        body = response.json()
        assert body["sync"]["po_csv"]["errorDetail"] == "Drive file not found: Master_HRS_SILVASSA_Domestic_Purchase_Data.csv"

    def test_successful_sync_run_has_no_error_detail(self):
        """A success must report errorDetail as null/None, not an empty
        string that could render as a confusing blank tooltip."""
        from django.utils import timezone

        SyncRun.objects.create(
            plant=SyncRun.Plant.HRS,
            source=SyncRun.Source.PO_CSV,
            status=SyncRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_detail="",
        )
        response = self.client.get("/api/sync-status")
        assert response.json()["sync"]["po_csv"]["errorDetail"] is None
