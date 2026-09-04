"""
apps/api/routers/device_views.py — Device trust endpoints.

Ported from the TDS Automation App's apps/api/routers/device_views.py.

Endpoints
---------
POST /api/auth/device-verify
    Verify the 6-digit email OTP sent when logging in from a new device.
    On success:
      - Creates a TrustedDevice row (permanent trust for this browser/device)
      - Sets the httpOnly `pt_device` cookie (365-day expiry)
      - Sends a 'new device signed in' notification email (informational)
      - Alerts all admin accounts of the new device login (informational)
      - Returns a full JWT (same shape as a trusted-device login)

POST /api/auth/logout
    Clears the pt_access/pt_refresh cookies and flushes the session.
    Does NOT clear the pt_device cookie - device stays trusted for next login.
"""

import logging

from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.api.auth_serializers import PTTokenObtainPairSerializer
from apps.core.models import PTUser
from apps.services.device_service import (
    notify_admins_new_device_login,
    register_device,
    send_new_device_notification,
)
from apps.services.otp_service import verify_otp

log = logging.getLogger(__name__)


class DeviceVerifyThrottle(AnonRateThrottle):
    """10 OTP attempts per minute per IP - prevents brute-force of 6-digit codes."""

    scope = "otp_verify"


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([DeviceVerifyThrottle])
def device_verify(request):
    """POST /api/auth/device-verify
    Body: { "code": "123456" }

    Requires a valid Django session containing `pending_user_id` (set by
    PTTokenObtainPairSerializer.validate() on a new-device login attempt).
    """
    code = request.data.get("code", "").strip()
    if not code:
        return Response({"detail": "Verification code is required."}, status=400)

    user_id = request.session.get("pending_user_id")
    if not user_id:
        log.warning("device_verify: no pending_user_id in session")
        return Response({"detail": "Session expired. Please sign in again."}, status=401)

    try:
        user = PTUser.objects.get(pk=user_id, is_active=True)
    except PTUser.DoesNotExist:
        return Response({"detail": "User not found or inactive."}, status=400)

    if not verify_otp(user.email, code):
        log.warning("device_verify: wrong or expired code for user_id=%s", user_id)
        return Response({"detail": "Invalid or expired code. Please try again."}, status=400)

    refresh = PTTokenObtainPairSerializer.get_token(user)
    jwt_data = {
        "status": "ok",
        "access_token": str(refresh.access_token),
        "refresh": str(refresh),
        "user_id": user.user_id,
        "role": user.role,
        "full_name": user.full_name or "",
        "email": user.email,
    }

    response = Response(jwt_data)

    register_device(response, user_id, request)

    from apps.services.device_service import set_access_cookie, set_refresh_cookie

    set_access_cookie(response, jwt_data["access_token"])
    set_refresh_cookie(response, jwt_data["refresh"])

    send_new_device_notification(user, request)
    notify_admins_new_device_login(user, request)

    request.session.pop("pending_user_id", None)

    from apps.core.audit_log import PTAuditLog, log_pt_action

    log_pt_action(request, PTAuditLog.ACTION_LOGIN, actor=user, detail="new device (email OTP verified)")

    log.info("device_verify: success user_id=%s role=%s", user_id, user.role)
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def logout_view(request):
    """POST /api/auth/logout

    Clears the pt_access/pt_refresh httpOnly cookies and flushes the
    session (a stateless JWT would otherwise keep authenticating every
    request right up to its natural expiry even after logout). Does NOT
    clear the pt_device cookie - device stays trusted for next login."""
    from django.conf import settings

    from apps.services.device_service import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH

    from apps.core.audit_log import PTAuditLog, log_pt_action

    log_pt_action(request, PTAuditLog.ACTION_LOGOUT, actor=getattr(request, "user", None))

    request.session.flush()
    response = Response({"detail": "Logged out successfully."})
    response.delete_cookie(key=settings.PT_COOKIE_NAME, path="/", samesite=settings.PT_COOKIE_SAMESITE)
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, samesite=settings.PT_COOKIE_SAMESITE)
    return response
