"""
apps/services/token_revocation.py — custom refresh-token revocation.

See apps/core/models.py's RevokedRefreshToken for why this is a small
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

from apps.core.models import RevokedRefreshToken


def revoke_refresh_jti(jti: str, expires_at) -> None:
    """Record `jti` as revoked. Idempotent - rotating or logging out twice
    with the same token (e.g. a retried request) doesn't error."""
    RevokedRefreshToken.objects.get_or_create(jti=jti, defaults={"expires_at": expires_at})


def is_refresh_jti_revoked(jti: str) -> bool:
    return RevokedRefreshToken.objects.filter(jti=jti).exists()
