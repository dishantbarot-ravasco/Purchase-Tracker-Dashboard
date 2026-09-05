"""
Test for apps/core/management/commands/prune_revoked_tokens.py - added
2026-09-05 (hardening pass) since apps/services/token_revocation.py's
RevokedRefreshToken table has nothing else that ever deletes a row.
"""

import io

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.core.models import RevokedRefreshToken


@pytest.mark.django_db
class TestPruneRevokedTokens:
    def test_deletes_only_expired_rows(self):
        expired = RevokedRefreshToken.objects.create(
            jti="expired-jti", expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        still_valid = RevokedRefreshToken.objects.create(
            jti="valid-jti", expires_at=timezone.now() + timezone.timedelta(days=1),
        )

        out = io.StringIO()
        call_command("prune_revoked_tokens", stdout=out)

        assert not RevokedRefreshToken.objects.filter(pk=expired.pk).exists()
        assert RevokedRefreshToken.objects.filter(pk=still_valid.pk).exists()
        assert "Pruned 1" in out.getvalue()
