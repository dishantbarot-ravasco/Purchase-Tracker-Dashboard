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

Wired to the same external-cron pattern as the report emails (added
2026-09-09) - apps/api/routers/reports_views.py's trigger_prune_revoked_tokens
calls the shared apps/services/token_revocation.prune_expired_revoked_tokens()
directly (not via call_command() - see that function's own docstring for
why), same shared-secret scheme, no separate infrastructure needed. Still
runnable directly (`manage.py prune_revoked_tokens`) for a manual one-off too.
"""

from django.core.management.base import BaseCommand

from apps.services.token_revocation import prune_expired_revoked_tokens


class Command(BaseCommand):
    help = "Delete RevokedRefreshToken rows whose underlying token has already expired."

    def handle(self, *args, **options):
        deleted = prune_expired_revoked_tokens()
        self.stdout.write(self.style.SUCCESS(f"Pruned {deleted} expired revoked-refresh-token row(s)."))
