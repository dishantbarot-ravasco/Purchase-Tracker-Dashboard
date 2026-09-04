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
    """5 login attempts per minute per IP - brute-force protection."""

    scope = "login"


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

            from apps.core.audit_log import PTAuditLog, log_pt_action
            from apps.core.models import PTUser

            user = PTUser.objects.filter(pk=response.data.get("user_id")).first()
            log_pt_action(request, PTAuditLog.ACTION_LOGIN, actor=user, detail="trusted device")
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
        from apps.services.device_service import REFRESH_COOKIE_NAME, set_access_cookie

        data = request.data
        if not data.get("refresh") and REFRESH_COOKIE_NAME in request.COOKIES:
            data = dict(data)
            data["refresh"] = request.COOKIES[REFRESH_COOKIE_NAME]

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        response = Response(serializer.validated_data, status=status.HTTP_200_OK)
        if response.data.get("access"):
            set_access_cookie(response, response.data["access"])
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
