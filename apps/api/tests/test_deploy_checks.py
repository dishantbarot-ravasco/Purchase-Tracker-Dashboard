"""
Tests for apps/core/checks.py's custom system checks (added 2026-09-05,
hardening pass) - both run as part of `manage.py check --deploy
--fail-level WARNING`, the same command CI already runs on every push.
Called directly rather than through `call_command("check", ...)` so each
scenario can control settings/DB state precisely.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.core.checks import check_jwt_signing_key_is_independent, check_no_unexpected_django_superuser


class TestJwtSigningKeyCheck:
    def test_warns_when_jwt_key_falls_back_to_secret_key_outside_debug(self):
        with override_settings(DEBUG=False, SECRET_KEY="shared-value", JWT_SIGNING_KEY="shared-value"):
            warnings = check_jwt_signing_key_is_independent(None)
        assert len(warnings) == 1
        assert warnings[0].id == "apps.core.W001"

    def test_no_warning_when_keys_differ(self):
        with override_settings(DEBUG=False, SECRET_KEY="a", JWT_SIGNING_KEY="b"):
            warnings = check_jwt_signing_key_is_independent(None)
        assert warnings == []

    def test_no_warning_under_debug_even_if_keys_match(self):
        with override_settings(DEBUG=True, SECRET_KEY="shared-value", JWT_SIGNING_KEY="shared-value"):
            warnings = check_jwt_signing_key_is_independent(None)
        assert warnings == []


@pytest.mark.django_db
class TestSuperuserExposureCheck:
    def test_warns_when_an_active_superuser_exists(self):
        get_user_model().objects.create_superuser(username="oops", email="oops@ravasco.com", password="whatever123")
        warnings = check_no_unexpected_django_superuser(None)
        assert len(warnings) == 1
        assert warnings[0].id == "apps.core.W002"

    def test_no_warning_when_no_superuser_exists(self):
        warnings = check_no_unexpected_django_superuser(None)
        assert warnings == []

    def test_no_warning_for_an_inactive_superuser(self):
        u = get_user_model().objects.create_superuser(username="oops2", email="oops2@ravasco.com", password="whatever123")
        u.is_active = False
        u.save()
        warnings = check_no_unexpected_django_superuser(None)
        assert warnings == []
