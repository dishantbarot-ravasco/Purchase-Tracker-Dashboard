"""
Sign-in hardening from the 2026-09-25 security review:

- a per-account daily cap on wrong device codes that a new code does not
  reset (each sign-in issued a fresh code with 5 fresh tries);
- one refresh token rotated by two requests at once minting only ONE new
  chain (the check-then-revoke let both through);
- DRF's IP throttles no longer keyed on a client-written X-Forwarded-For.
"""

import datetime

import pytest
from django.conf import settings
from django.test import RequestFactory
from django.utils import timezone

from apps.api.tests.factories import make_user
from apps.services import otp_service, token_revocation


@pytest.mark.django_db
class TestDailyCodeCap:
    def test_fresh_codes_do_not_reset_the_daily_failures(self):
        user = make_user(email="cap@ravasco.com")
        for _ in range(otp_service._MAX_DAILY_FAILURES // 5):
            otp_service.generate_otp(user.email)
            for _ in range(5):
                assert otp_service.verify_otp(user.email, "000000") is False
        # 20 wrong codes across four fresh codes: now even the RIGHT code of a
        # brand-new one is refused.
        code = otp_service.generate_otp(user.email)
        assert otp_service.verify_otp(user.email, code) is False

    def test_the_window_ends_after_a_day(self):
        user = make_user(email="win@ravasco.com")
        type(user).objects.filter(pk=user.pk).update(
            otp_failed_attempts=otp_service._MAX_DAILY_FAILURES,
            otp_failures_since=timezone.now() - datetime.timedelta(hours=25))
        code = otp_service.generate_otp(user.email)
        assert otp_service.verify_otp(user.email, code) is True

    def test_a_right_code_clears_the_count(self):
        user = make_user(email="ok@ravasco.com")
        otp_service.generate_otp(user.email)
        otp_service.verify_otp(user.email, "000000")
        code = otp_service.generate_otp(user.email)
        assert otp_service.verify_otp(user.email, code) is True
        user.refresh_from_db()
        assert user.otp_failed_attempts == 0


@pytest.mark.django_db
class TestRefreshRotationIsAtomic:
    def test_only_the_first_claim_of_a_jti_wins(self):
        exp = timezone.now() + datetime.timedelta(days=1)
        assert token_revocation.claim_refresh_jti("jti-abc", exp) is True
        assert token_revocation.claim_refresh_jti("jti-abc", exp) is False


class TestThrottleIdentity:
    def test_a_spoofed_forwarded_for_does_not_change_the_throttle_ident(self):
        """With one trusted proxy, DRF keys on the entry Render appended (the
        last), not the whole client-writable header."""
        from rest_framework.throttling import AnonRateThrottle

        assert settings.REST_FRAMEWORK["NUM_PROXIES"] == 1
        factory = RequestFactory()
        idents = set()
        for spoof in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            req = factory.get("/", HTTP_X_FORWARDED_FOR=f"{spoof}, 10.0.0.9", REMOTE_ADDR="10.1.1.1")
            idents.add(AnonRateThrottle().get_ident(req))
        assert idents == {"10.0.0.9"}
