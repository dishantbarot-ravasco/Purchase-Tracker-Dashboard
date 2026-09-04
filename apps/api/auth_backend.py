"""
apps/api/auth_backend.py — Custom Django authentication backend + JWT authentication.

Ported from the TDS Automation App's apps/api/auth_backend.py (same design,
PTUser instead of TDSUser). Three classes:

1. PTUserBackend
   Django authentication backend: authenticate(request, email=..., password=...).
   Verifies bcrypt passwords against the `pt_users` table (PTUser model).

2. PTJWTAuthentication
   Subclass of simplejwt's JWTAuthentication. Looks up PTUser by the `sub`
   claim (user_id) instead of Django's auth.User.

3. PTCookieJWTAuthentication
   Extends PTJWTAuthentication. Tries the httpOnly cookie first, falls back
   to the Authorization: Bearer header (for non-browser API clients).

4. pt_user_authentication_rule
   SIMPLE_JWT['USER_AUTHENTICATION_RULE'] — checks PTUser.is_active rather
   than auth.User.is_active.

**AUTH_USER_MODEL is deliberately left at Django's default (auth.User) —
this app's real user model, PTUser, is a plain, unrelated model, not a
Django auth user.** Every real authentication path below resolves PTUser
directly instead of touching get_user_model()/AUTH_USER_MODEL at all. Any
new code — or any djangorestframework-simplejwt upgrade — that calls
django.contrib.auth.get_user_model() will silently resolve to auth.User,
not PTUser, and almost certainly do the wrong thing or crash outright. This
bit the TDS Automation App in production: simplejwt's stock
TokenRefreshSerializer.validate() calls
get_user_model().objects.get(**{api_settings.USER_ID_FIELD: user_id}) to
re-check the user is still active before issuing a refreshed access token —
since SIMPLE_JWT['USER_ID_FIELD'] is 'user_id' (correct for PTUser's PK,
wrong for auth.User's 'id'), every POST /api/auth/token/refresh crashed
with FieldError: Cannot resolve keyword 'user_id' into field. Fixed here the
same way TDS fixed it: PTTokenRefreshSerializer (apps/api/auth_serializers.py)
overrides validate() to resolve PTUser directly via
pt_user_authentication_rule instead of get_user_model(), wired in via
PTTokenRefreshView.serializer_class (apps/api/auth_views.py). Before
adopting any simplejwt/DRF upgrade, grep it for new get_user_model() call
sites — that is the recurring failure mode this whole class of bug comes
from.
"""

import logging

import bcrypt
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken

from apps.api.permissions import is_allowed_email_domain
from apps.core.models import PTUser

logger = logging.getLogger(__name__)


def _verify_password(plain: str, hashed: str) -> bool:
    """Verify a bcrypt password hash using the bcrypt library directly."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception as exc:
        logger.warning("bcrypt.checkpw error: %s", exc)
        return False


def _dummy_verify() -> None:
    """Constant-time no-op to prevent user-enumeration timing attacks.

    The bare except is deliberate, not an oversight: this call's only job is
    to burn the same amount of time a real bcrypt.checkpw() would, for an
    unknown-email login attempt. dummy_hash is a fixed, valid-format hash,
    so an exception here is not expected in practice - but if bcrypt ever
    did raise, there is nothing useful to do with that failure (there is no
    real check in flight to abort, and logging it would itself leak a
    timing/behavioral difference between the dummy and real paths, defeating
    the point of this function)."""
    dummy_hash = b"$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    try:
        bcrypt.checkpw(b"dummy", dummy_hash)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# 1. Django authentication backend (used at login time)
# ─────────────────────────────────────────────────────────────────────────

class PTUserBackend:
    """authenticate(request, email=..., password=...) -> PTUser | None.

    Django calls each backend in AUTHENTICATION_BACKENDS order and stops at
    the first non-None result."""

    def authenticate(self, request, email: str = None, password: str = None):
        if not email or not password:
            return None

        try:
            user = PTUser.objects.get(email=email)
        except PTUser.DoesNotExist:
            _dummy_verify()
            return None

        # Always do the bcrypt work first, then check is_active - doing the
        # active check before verifying the password would make an inactive
        # account's login attempt return faster than a wrong-password
        # attempt on an active one, a timing side-channel that partially
        # defeats the account-enumeration protection _dummy_verify() exists
        # to provide (this exact ordering bug bit the TDS app in the past).
        password_ok = _verify_password(password, user.password_hash)

        if not user.is_active:
            return None
        if not password_ok:
            return None
        # Defense-in-depth: account creation already rejects non-domain
        # emails (manage.py create_pt_user), so this should never trip on a
        # normally-created account.
        if not is_allowed_email_domain(user.email):
            return None

        return user

    def get_user(self, user_id: int):
        """Required by Django session machinery."""
        try:
            return PTUser.objects.get(pk=user_id)
        except PTUser.DoesNotExist:
            return None


# ─────────────────────────────────────────────────────────────────────────
# 2. JWT authentication (Bearer header)
# ─────────────────────────────────────────────────────────────────────────

class PTJWTAuthentication(JWTAuthentication):
    """Resolve PTUser from the `sub` claim instead of Django's auth.User."""

    def get_user(self, validated_token):
        try:
            user_id = int(validated_token["sub"])
        except (KeyError, ValueError, TypeError):
            raise InvalidToken("Token contains no valid `sub` claim.")

        try:
            user = PTUser.objects.get(pk=user_id)
        except PTUser.DoesNotExist:
            raise AuthenticationFailed("User not found.", code="user_not_found")

        if not user.is_active:
            raise AuthenticationFailed("User account is inactive.", code="user_inactive")

        return user


# ─────────────────────────────────────────────────────────────────────────
# 3. Cookie-first JWT authentication
# ─────────────────────────────────────────────────────────────────────────

class PTCookieJWTAuthentication(PTJWTAuthentication):
    """Check the httpOnly cookie before falling back to the Authorization:
    Bearer header.

    SameSite=Lax on the cookie means the browser will not send it on
    cross-site POST requests, which is sufficient CSRF protection for an
    internal app without a separate CSRF token flow on the API - the JWT API
    never touches Django's session-based CSRF check (see
    config/middleware.py's AdminOnlyCsrfMiddleware, scoped to /admin/ only)."""

    def authenticate(self, request):
        cookie_val = request.COOKIES.get(settings.PT_COOKIE_NAME, "")
        if cookie_val:
            try:
                validated = self.get_validated_token(cookie_val.encode("utf-8"))
                return self.get_user(validated), validated
            except Exception:
                # Invalid/expired cookie - fall through to Bearer header,
                # don't raise (a bad cookie shouldn't block Bearer-based calls).
                logger.debug("Cookie JWT invalid or expired - trying Bearer header")

        return super().authenticate(request)


# ─────────────────────────────────────────────────────────────────────────
# 4. SimpleJWT USER_AUTHENTICATION_RULE
# ─────────────────────────────────────────────────────────────────────────

def pt_user_authentication_rule(user) -> bool:
    """Called by simplejwt after get_user() to decide whether the user may
    use the token. Checks PTUser.is_active."""
    return user is not None and bool(user.is_active)
