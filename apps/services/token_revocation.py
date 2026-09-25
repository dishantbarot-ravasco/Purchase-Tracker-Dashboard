"""
apps/services/token_revocation.py - custom refresh-token revocation.

See apps/core/models/auth.py's RevokedRefreshToken for why this is a small
custom table rather than rest_framework_simplejwt's built-in
`token_blacklist` app: that app's OutstandingToken model FKs to
AUTH_USER_MODEL (Django's default auth.User), which is incompatible with
this app's PTUser-is-not-auth.User architecture - confirmed by actually
hitting the crash (`OutstandingToken.user` must be a `User` instance`)
before reverting to this approach.

Used by apps/api/auth_serializers.py's PTTokenRefreshSerializer (revoke the
old refresh token's jti on rotation, reject an already-revoked jti) and
apps/api/routers/device_views.py's logout_view (revoke the current refresh
token's jti directly on logout).
"""

from django.db.models import F
from django.utils import timezone

from apps.core.models import PTUser, RevokedRefreshToken, TrustedDevice


def prune_expired_revoked_tokens() -> int:
    """Deletes RevokedRefreshToken rows past their own expires_at, returns
    the count deleted. A row past expiry is safe to remove - the token it
    refers to would already be rejected by JWT expiry validation before this
    table is ever consulted (see PTTokenRefreshSerializer), so keeping a
    revocation record for an already-expired token serves no purpose.

    Shared by manage.py prune_revoked_tokens (CLI) and
    apps/api/routers/reports_views.py's trigger_prune_revoked_tokens
    (external cron) - kept here rather than duplicated in both, and rather
    than having the view call the command via call_command(): Django's
    BaseCommand.execute() re-writes handle()'s return value to stdout
    whenever it's truthy, which crashes on a plain int (`'int' object has no
    attribute 'endswith'`) - confirmed hitting this directly. A shared plain
    function avoids the mismatch entirely."""
    deleted, _ = RevokedRefreshToken.objects.filter(expires_at__lt=timezone.now()).delete()
    return deleted


def revoke_refresh_jti(jti: str, expires_at) -> None:
    """Record `jti` as revoked. Idempotent - rotating or logging out twice
    with the same token (e.g. a retried request) doesn't error."""
    RevokedRefreshToken.objects.get_or_create(jti=jti, defaults={"expires_at": expires_at})


def claim_refresh_jti(jti: str, expires_at) -> bool:
    """Revoke `jti` and report whether THIS call did it - atomic on the
    table's unique jti, so of two requests rotating the same refresh token at
    once exactly one gets True (2026-09-25). The check-then-revoke it
    replaces let both mint new tokens. The loser is refused, not treated as
    theft: two tabs refreshing together look the same, and signing that user
    out everywhere was a bug once already (see CLAUDE.md's traps)."""
    _row, created = RevokedRefreshToken.objects.get_or_create(jti=jti, defaults={"expires_at": expires_at})
    return created


def is_refresh_jti_revoked(jti: str) -> bool:
    return RevokedRefreshToken.objects.filter(jti=jti).exists()


def revoke_all_tokens(user: PTUser) -> None:
    """Invalidate every access AND refresh token already issued to `user`,
    WITHOUT touching device trust - see PTUser.token_version's own docstring
    for why bumping this one counter is enough.

    Split out of revoke_all_sessions() (2026-09-15, audit pass) because a
    password change and a "log out everywhere" panic button want genuinely
    different blast radii, and conflating them was blocking the password fix:

      - A password change MUST kill live tokens. Before this, an attacker
        holding a stolen session kept working access for up to the access
        token's 12h lifetime and, via the sliding 30-day refresh cookie,
        effectively indefinitely - even after the victim changed their
        password specifically because they suspected compromise. That is the
        single behavior most users assume a password change already has.

      - A password change must NOT wipe device trust. A pt_device cookie only
        ever skips the email-OTP step; it never substitutes for the password
        itself, so an attacker who kept one still cannot sign in once the
        password changes. Deleting every trusted device on each routine
        rotation would re-challenge every colleague on every device and train
        people to click through OTP emails without reading them - a real
        security cost, paid for no real gain.

    revoke_all_sessions() below is still the right call for an actual
    compromise ("log out everywhere", admin-driven session kill), where
    re-challenging every device IS the point."""
    PTUser.objects.filter(pk=user.pk).update(token_version=F("token_version") + 1)


def revoke_all_sessions(user: PTUser) -> None:
    """"Log out everywhere" for `user` - see PTUser.token_version's own
    docstring for why bumping this one counter instantly invalidates every
    access AND refresh token already issued, without needing a registry of
    every token ever handed out. Also clears device trust (deletes every
    TrustedDevice row for this account) so a fresh sign-in on any device,
    including ones that were previously trusted, goes through the email-OTP
    challenge again rather than silently re-trusting a device that might be
    the very thing prompting this call (e.g. a lost laptop).

    Uses an F() expression (an in-DB `token_version = token_version + 1`,
    not Python computing `user.token_version + 1` from a possibly-stale
    in-memory value) - found and fixed during a full-codebase audit
    (2026-09-10): two concurrent calls (e.g. an admin's "logout everywhere"
    landing at the same moment as the user's own self-service one) could
    otherwise both read the same starting token_version and both write the
    same single increment, one bump getting silently lost. Milder than the
    similar races fixed in auth_backend.py/otp_service.py the same day - a
    lost bump here doesn't break the actual revocation (this call's own
    increment always applies, just possibly landing on a stale base), but
    an F() expression removes the race entirely rather than tolerating it."""
    revoke_all_tokens(user)
    TrustedDevice.objects.filter(user=user).delete()
