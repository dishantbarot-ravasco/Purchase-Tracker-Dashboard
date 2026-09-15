"""
apps/api/routers/password_views.py — Self-service password change, OTP-
gated the same way a new-device login is (project owner, 2026-09-07: "add
the change password for all users and add the otp to it for verification
like we do for 1st time devices").

Two-step flow, mirroring device_verify's own shape (apps/api/routers/
device_views.py):
  POST /api/auth/change-password/request  IsAuthenticated, no body. Emails a
    fresh 6-digit OTP to the CALLER's own address - never an email supplied
    in the request body, there is no "change someone else's password" self-
    service path. That remains admin-only, via PATCH /api/auth/users/<id>
    (users_views.py's update_user, unaffected by this file).
  POST /api/auth/change-password/confirm  IsAuthenticated. Body:
    {"otp", "newPassword"}. Verifies the OTP against the caller's own email
    (apps/services/otp_service.py - the same store/verify new-device login
    uses), then applies the same password-strength policy every other
    password-setting path in this app enforces
    (users_views.py's _validate_password_strength), and saves.

Every role can use this (any authenticated, active account) - unlike
users_views.py's admin-only reset, this only ever touches request.user's own
row, so there's no privilege-escalation surface to gate against.
"""

import logging

from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from apps.api.auth_serializers import PTTokenObtainPairSerializer
from apps.api.routers.users_views import _hash_password, _validate_password_strength
from apps.core.audit_log import PTAuditLog, log_pt_action
from apps.core.models import PTUser
from apps.services.device_service import set_access_cookie, set_refresh_cookie
from apps.services.otp_service import verify_otp
from apps.services.password_service import send_password_change_otp
from apps.services.token_revocation import revoke_all_tokens

logger = logging.getLogger(__name__)


class PasswordChangeRequestThrottle(UserRateThrottle):
    scope = "password_change_request"


class PasswordChangeConfirmThrottle(UserRateThrottle):
    # Reuses the same rate as device-login OTP guessing (config/settings.py's
    # DEFAULT_THROTTLE_RATES["otp_verify"]) - same shape of problem (bound
    # how many 6-digit guesses an authenticated session can throw at
    # verify_otp() per minute), no reason for a separate configured rate.
    scope = "otp_verify"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordChangeRequestThrottle])
def request_password_change(request):
    """POST /api/auth/change-password/request - always 202, regardless of
    whether the send itself succeeds (matches send_device_otp's own fire-
    and-forget shape) - the caller only ever changes their own already-
    verified account's password, so there's nothing here worth revealing
    delivery failure over that a retry wouldn't also cover."""
    send_password_change_otp(request.user)
    return Response({"status": "sent"}, status=202)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordChangeConfirmThrottle])
def confirm_password_change(request):
    """POST /api/auth/change-password/confirm
    Body: {"otp": "123456", "newPassword": "..."}."""
    otp = (request.data.get("otp") or "").strip()
    new_password = request.data.get("newPassword") or ""

    if not otp:
        return Response({"detail": "OTP code is required."}, status=400)
    if not verify_otp(request.user.email, otp):
        return Response({"detail": "Invalid or expired code."}, status=400)

    _validate_password_strength(new_password, request.user.email)

    request.user.password_hash = _hash_password(new_password)
    request.user.save(update_fields=["password_hash"])

    # Kill every token issued before this moment (audit pass, 2026-09-15).
    # Without this, changing a password did nothing to sessions already in
    # flight: a stolen access token stayed good for its full 12h lifetime and
    # the sliding 30-day pt_refresh cookie could renew indefinitely, so a user
    # who changed their password BECAUSE they suspected compromise did not
    # actually evict the attacker. revoke_all_tokens (not revoke_all_sessions)
    # deliberately leaves TrustedDevice rows alone - see that function's own
    # docstring for why a routine rotation should not re-OTP every device.
    revoke_all_tokens(request.user)

    # ...including the caller's own, which is why this endpoint immediately
    # re-issues a fresh pair against the NEW token_version and re-cookies it.
    # Skipping this would log the user out of the very browser they just used
    # to change their password - correct on paper, but it reads as "the change
    # failed" and pushes people back to the login screen mid-flow. Re-reading
    # the row from the DB is required: revoke_all_tokens() bumps the counter
    # with an in-DB F() expression, so the in-memory request.user still holds
    # the stale pre-bump value and would mint a token that fails on the very
    # next request.
    refreshed_user = PTUser.objects.get(pk=request.user.pk)
    new_tokens = PTTokenObtainPairSerializer.get_token(refreshed_user)

    logger.info("password_views: user %s changed their own password", request.user.email)
    log_pt_action(
        request, PTAuditLog.ACTION_USER_UPDATED, actor=request.user,
        detail="self-service password change (OTP-verified); all other sessions revoked",
    )
    response = Response({"status": "ok", "sessionsRevoked": True})
    set_access_cookie(response, str(new_tokens.access_token))
    set_refresh_cookie(response, str(new_tokens))
    return response
