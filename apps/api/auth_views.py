"""
apps/api/auth_views.py — Authentication endpoints.

Endpoints
---------
POST /api/auth/login          — credentials -> device-trust / email-OTP intermediate state
POST /api/auth/token/refresh  — refresh an expiring access token
POST /api/auth/token/verify   — verify a token is still valid

Logout (POST /api/auth/logout) and device-verify (POST /api/auth/device-verify)
live in apps/api/routers/device_views.py, not here.

Ported from the TDS Automation App's apps/api/auth_views.py.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from .auth_serializers import PTTokenObtainPairSerializer, PTTokenRefreshSerializer

logger = logging.getLogger(__name__)


class LoginRateThrottle(AnonRateThrottle):
    """5 login attempts per minute per email, not per IP - brute-force
    protection scoped to the account being attacked, not shared across every
    caller on the same source IP.

    A real bug, found and fixed 2026-09-04: this used to inherit
    AnonRateThrottle's default IP-based get_cache_key() unchanged, which
    means everyone behind the same office/VPN/NAT exit IP shared ONE 5-per-
    minute bucket. A handful of colleagues signing in within the same minute
    was enough to exhaust it, after which every subsequent login from that
    IP - correct credentials or not - got a generic "Request was throttled"
    error indistinguishable from a real failure. Keying on the submitted
    email instead means one account's attempts can no longer starve everyone
    else's. Falls back to the inherited IP-based key only when no email was
    submitted at all (a malformed/empty request body has no account to key
    on)."""

    scope = "login"

    def get_cache_key(self, request, view):
        email = str(request.data.get("email", "")).strip().lower()
        if not email:
            return super().get_cache_key(request, view)
        return self.cache_format % {"scope": self.scope, "ident": f"email:{email}"}


class PTLoginView(TokenObtainPairView):
    """POST /api/auth/login
    Body: { "email": "...", "password": "..." }

    Returns:
      { "status": "ok", "access_token": "...", ... }   - trusted device
      { "status": "device_verify" }                    - new device, OTP emailed

    On a trusted-device ("ok") login, also sets the httpOnly pt_access
    cookie (see apps/services/device_service.py#set_access_cookie) so the
    frontend never has to keep the token in JS-readable storage.
    access_token is still returned in the body too, for any API client that
    isn't a browser (Postman, scripts, etc.) and can't rely on cookies.
    """

    permission_classes = [AllowAny]
    serializer_class = PTTokenObtainPairSerializer
    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and response.data.get("status") == "ok":
            from apps.services.device_service import set_access_cookie, set_refresh_cookie

            set_access_cookie(response, response.data["access_token"])
            if response.data.get("refresh"):
                set_refresh_cookie(response, response.data["refresh"])

            from django.utils import timezone

            from apps.core.audit_log import PTAuditLog, log_pt_action
            from apps.core.models import PTUser

            user = PTUser.objects.filter(pk=response.data.get("user_id")).first()
            log_pt_action(request, PTAuditLog.ACTION_LOGIN, actor=user, detail="trusted device")
            # Real bug, found and fixed 2026-09-09: PTUser.last_login_at was
            # defined and displayed in admin.html's Users panel, but no login
            # path anywhere in this app ever wrote to it - every account
            # showed "Never" regardless of actual login history. .update(),
            # not user.save(), to avoid clobbering any other field a
            # concurrent request may have just changed on this same row.
            if user is not None:
                PTUser.objects.filter(pk=user.pk).update(last_login_at=timezone.now())
        return response


class PTTokenRefreshView(TokenRefreshView):
    """POST /api/auth/token/refresh

    The backbone of the 'remember me' flow: the pt_refresh cookie is
    httpOnly, so page JS can't read it to put it in the request body itself
    - instead, if the body doesn't already carry a `refresh` value (the
    normal case for a same-origin browser call), it's pulled from the
    cookie before handing off to simplejwt's serializer. Non-browser API
    clients can still pass `refresh` in the body directly.

    On success, re-sets the pt_access cookie to the new token.
    """

    serializer_class = PTTokenRefreshSerializer

    def post(self, request, *args, **kwargs):
        from apps.services.device_service import REFRESH_COOKIE_NAME, set_access_cookie, set_refresh_cookie

        data = request.data
        if not data.get("refresh") and REFRESH_COOKIE_NAME in request.COOKIES:
            data = dict(data)
            data["refresh"] = request.COOKIES[REFRESH_COOKIE_NAME]

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Body carries only `access` - the refresh token (rotated or not)
        # only ever travels as the httpOnly pt_refresh cookie, never in a
        # JS-readable JSON response body, same reasoning as the access token
        # cookie. ROTATE_REFRESH_TOKENS/BLACKLIST_AFTER_ROTATION (see
        # config/settings.py's SIMPLE_JWT) mean a successful refresh here
        # invalidates the refresh token that was just spent - the new one
        # (present in validated_data["refresh"] when rotation is on) must be
        # re-cookied or the client would keep sending an already-blacklisted
        # token on its next refresh.
        response = Response({"access": serializer.validated_data.get("access")}, status=status.HTTP_200_OK)
        if serializer.validated_data.get("access"):
            set_access_cookie(response, serializer.validated_data["access"])
        if serializer.validated_data.get("refresh"):
            set_refresh_cookie(response, serializer.validated_data["refresh"])
        return response


PTTokenVerifyView = TokenVerifyView


@api_view(["GET"])
def whoami(request):
    """GET /api/auth/me

    Requires the default IsAuthenticated - the only real signal the
    frontend has for "is my pt_access cookie still valid" (unlike a plain
    token string, a cookie can't be handed to /api/auth/token/verify
    directly). frontend/js/auth.js's requireAuth() calls this on every
    protected page load and redirects to login.html on a 401."""
    user = request.user
    return Response({
        "userId": user.user_id,
        "email": user.email,
        "fullName": user.full_name or "",
        "role": user.role,
        # Empty list = "all plants" - see PTUser.plants' own docstring. The
        # frontend uses this to decide which pencil icons to render for the
        # inline "Edit Everywhere" feature (shared.js's canEditField()).
        "plants": user.plants or [],
    })


# User management (list/create/update) lives in
# apps/api/routers/users_views.py, not here - see that file's header.
