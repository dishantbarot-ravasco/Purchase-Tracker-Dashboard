"""
Two ways a credential change used to leave the old state in force:

- A present-but-blank JWT_SIGNING_KEY (the way .env.example ships it) made
  every JWT sign with an empty key, and checks.W001 never fired because it
  compares the key against SECRET_KEY. Settings are read once at import, so
  this is checked in a fresh interpreter with the variable set to blank.
- Re-running create_pt_user is the CLI password reset, but it did not bump
  token_version, so sessions issued under the old password stayed alive.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.api.tests.factories import make_user
from apps.core.models import PTUser

REPO_ROOT = Path(__file__).resolve().parents[3]

_PROBE = (
    "import django, os; django.setup(); from django.conf import settings; "
    "print(repr(settings.JWT_SIGNING_KEY), settings.JWT_SIGNING_KEY == settings.SECRET_KEY)"
)


def _signing_key_with(value):
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="config.settings",
               DJANGO_SECRET_KEY="probe-secret-key", JWT_SIGNING_KEY=value)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_a_blank_signing_key_falls_back_to_secret_key():
    assert _signing_key_with("") == "'probe-secret-key' True"


def test_a_whitespace_signing_key_falls_back_to_secret_key():
    assert _signing_key_with("   ") == "'probe-secret-key' True"


def test_a_real_signing_key_is_used_as_given():
    assert _signing_key_with("independent-key") == "'independent-key' False"


@pytest.mark.django_db
class TestCreatePtUserReset:
    def test_resetting_an_existing_account_revokes_its_tokens(self):
        user = make_user(email="reset@ravasco.com", role="viewer")
        before = PTUser.objects.get(pk=user.pk).token_version
        call_command("create_pt_user", email="reset@ravasco.com", password="A-new-long-pass1", role="viewer")
        assert PTUser.objects.get(pk=user.pk).token_version == before + 1

    def test_creating_a_new_account_starts_at_the_default_version(self):
        call_command("create_pt_user", email="fresh@ravasco.com", password="A-new-long-pass1", role="viewer")
        fresh = PTUser.objects.get(email="fresh@ravasco.com")
        assert fresh.token_version == PTUser._meta.get_field("token_version").default
