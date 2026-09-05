"""
apps/core/management/commands/prune_revoked_tokens.py — deletes
RevokedRefreshToken rows past their own expires_at.

Added 2026-09-05 (hardening pass). apps/services/token_revocation.py writes
a row here every time a refresh token is rotated away or a user logs out -
nothing has ever deleted them, so the table grows forever. A row past its
own expires_at is safe to delete: the token it refers to would be rejected
by JWT expiry validation anyway (see PTTokenRefreshSerializer's own token
decode, which checks `exp` before this table is ever consulted), so keeping
a revocation record for an already-expired token serves no purpose.

Not wired to any scheduler yet (this app has no cron/scheduled-task
infrastructure at all - see CLAUDE.md's "No scheduling" known gap) - run
manually or via whatever job scheduler is set up alongside the other
`sync_*`/`match_*` commands once that gap is closed.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import RevokedRefreshToken


class Command(BaseCommand):
    help = "Delete RevokedRefreshToken rows whose underlying token has already expired."

    def handle(self, *args, **options):
        deleted, _ = RevokedRefreshToken.objects.filter(expires_at__lt=timezone.now()).delete()
        self.stdout.write(self.style.SUCCESS(f"Pruned {deleted} expired revoked-refresh-token row(s)."))
