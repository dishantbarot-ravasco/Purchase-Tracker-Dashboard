"""
apps/api/routers/password_views.py - the only ways a password changes,
outside the `create_pt_user` CLI (2026-10-02: admins no longer set or reset
anyone's password - the account holder does, proving they hold the
mailbox).

Signed in - "Change password" in the account menu (project owner,
2026-09-07, extended 2026-10-02):
  POST /api/auth/change-password/request  IsAuthenticated. Body:
    {"currentPassword"}. Checks the current password (a wrong one counts
    towards the same 5-try lockout as the sign-in page), then emails a code
    for OTPCode.Purpose.PASSWORD_CHANGE to the CALLER's own address - never
    an address from the body. A session left open on someone's desk cannot
    change the password without knowing it.
  POST /api/auth/change-password/confirm  IsAuthenticated. Body:
    {"otp", "newPassword", "confirmPassword"}. Re-issues the caller's own
    session cookies after revoking every other token.

Signed out - "Forgot password" on the sign-in page (2026-10-02):
  POST /api/auth/password-reset/request  AllowAny. Body: {"email"}. Always
    202 with the same body, and about the same time, whether or not the
    address has an account - the page never confirms who has one. A code
    for OTPCode.Purpose.PASSWORD_RESET goes only to an active account on an
    allowed domain.
  POST /api/auth/password-reset/confirm  AllowAny. Body: {"email", "otp",
    "newPassword", "confirmPassword"}. Every failure - unknown address,
    wrong or expired code - answers the same "Invalid or expired code.", at
    the same cost: one password-cost bcrypt check and one code-cost check on
    every path (_new_password(), otp_service.burn_code_check()).
    On success the password is set, the lockout cleared, every session AND
    trusted device revoked (a reset is what someone does when they think
    the account may be in other hands), and no one is signed in: the
    holder signs in with the new password, verifying the device again.

Every successful change also emails the holder a notice
(password_service.notify_password_changed()), so a change they did not
make is seen at once. Each path checks the new password before spending the
code (verify_otp() uses it up), refuses a confirmation that does not match,
and refuses the current password as the new one.

Throttles key on the account, not the IP (CLAUDE.md): the signed-in pair on
the user, the reset pair on the submitted email.
"""

import logging

import bcrypt
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from apps.api.auth_backend import _dummy_verify, _register_failed_attempt, _verify_password
from apps.api.auth_serializers import PTTokenObtainPairSerializer
from apps.api.permissions import is_allowed_email_domain
from apps.api.routers.users_views import _hash_password, _validate_password_strength
from apps.core.audit_log import PTAuditLog, log_pt_action
from apps.core.models import OTPCode, PTUser
from apps.services.device_service import set_access_cookie, set_refresh_cookie
from apps.services.otp_service import burn_code_check, verify_otp
from apps.services.password_service import (
    notify_password_changed,
    send_password_change_otp,
    send_password_reset_otp,
)
from apps.services.token_revocation import revoke_all_sessions, revoke_all_tokens

logger = logging.getLogger(__name__)

_BAD_CODE = "Invalid or expired code."
# What every reset request answers, account or not.
_RESET_SENT = {"status": "sent", "detail": "If that address has an account, a reset code is on its way. It expires in 10 minutes."}


class PasswordChangeRequestThrottle(UserRateThrottle):
    scope = "password_change_request"


class PasswordChangeConfirmThrottle(UserRateThrottle):
    # Same rate as device-login OTP guessing (DEFAULT_THROTTLE_RATES
    # ["otp_verify"]) - the same problem: bound 6-digit guesses per minute.
    scope = "otp_verify"


class _EmailKeyedThrottle(AnonRateThrottle):
    """Keyed on the submitted email, like auth_views.LoginRateThrottle, so
    colleagues behind one office IP never share a bucket; the IP only when
    no email was sent."""

    def get_cache_key(self, request, view):
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email", "")).strip().lower()
        if not email:
            return super().get_cache_key(request, view)
        return self.cache_format % {"scope": self.scope, "ident": f"email:{email}"}


class PasswordResetRequestThrottle(_EmailKeyedThrottle):
    scope = "password_reset_request"


class PasswordResetConfirmThrottle(_EmailKeyedThrottle):
    scope = "otp_verify"


_SAME_AS_CURRENT = "Choose a password different from the current one. Request a new code to try again."


def _new_password(data, email, current_hash=None) -> tuple[str, bool]:
    """The new password from the body, checked before any code is spent:
    present, matching its confirmation and strong enough (ValidationError,
    400). Also returns whether it is the current password - which the caller
    may say only AFTER the emailed code checks out. Saying it earlier made
    the reset form a password oracle: anyone could post a guess with a junk
    code and read "different from the current one" back, with no lockout."""
    new_password = data.get("newPassword") or ""
    if not new_password:
        raise ValidationError({"detail": "Enter a new password."})
    if new_password != (data.get("confirmPassword") or ""):
        raise ValidationError({"detail": "The new password and its confirmation do not match."})
    _validate_password_strength(new_password, email)
    if current_hash is None:
        _dummy_verify()  # no account: the same bcrypt cost as the comparison below
        return new_password, False
    return new_password, _verify_password(new_password, current_hash)


# ── Signed in: Change password ───────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordChangeRequestThrottle])
def request_password_change(request):
    """POST /api/auth/change-password/request - body {"currentPassword"}.
    202 once a code is on its way; 400 for a wrong current password."""
    data = request.data if isinstance(request.data, dict) else {}
    user = PTUser.objects.get(pk=request.user.pk)
    if not _verify_password(str(data.get("currentPassword") or ""), user.password_hash):
        _register_failed_attempt(user)
        return Response({"detail": "Your current password is not correct."}, status=400)
    send_password_change_otp(user)
    return Response({"status": "sent"}, status=202)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordChangeConfirmThrottle])
def confirm_password_change(request):
    """POST /api/auth/change-password/confirm
    Body: {"otp": "123456", "newPassword": "...", "confirmPassword": "..."}."""
    data = request.data if isinstance(request.data, dict) else {}
    otp = str(data.get("otp") or "").strip()
    if not otp:
        return Response({"detail": "Enter the code we emailed you."}, status=400)
    user = PTUser.objects.get(pk=request.user.pk)
    new_password, is_current = _new_password(data, user.email, user.password_hash)
    if not verify_otp(user.email, otp, OTPCode.Purpose.PASSWORD_CHANGE):
        return Response({"detail": _BAD_CODE}, status=400)
    if is_current:
        return Response({"detail": _SAME_AS_CURRENT}, status=400)

    user.password_hash = _hash_password(new_password)
    user.save(update_fields=["password_hash"])

    # Kill every token issued before this moment, so a session stolen
    # before the change stops working. revoke_all_tokens (not
    # revoke_all_sessions) leaves trusted devices alone - a routine change
    # should not re-challenge every device (see that function's docstring).
    revoke_all_tokens(user)

    # ...including the caller's own, so re-issue a fresh pair against the
    # NEW token_version, re-read from the DB (revoke_all_tokens() bumps it
    # with an F() expression, so the in-memory value is stale).
    refreshed_user = PTUser.objects.get(pk=user.pk)
    new_tokens = PTTokenObtainPairSerializer.get_token(refreshed_user)

    logger.info("password_views: user %s changed their own password", user.email)
    log_pt_action(
        request, PTAuditLog.ACTION_USER_UPDATED, actor=user,
        detail="self-service password change (current password + emailed code); all other sessions revoked",
    )
    notify_password_changed(user, "from your account menu")
    response = Response({"status": "ok", "sessionsRevoked": True})
    set_access_cookie(response, str(new_tokens.access_token))
    set_refresh_cookie(response, str(new_tokens))
    return response


# ── Signed out: Forgot password ──────────────────────────────────────────


def _reset_account(email: str):
    """The active account a reset may act on, or None. Never says why."""
    if not email or not is_allowed_email_domain(email):
        return None
    return PTUser.objects.filter(email__iexact=email, is_active=True).first()


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetRequestThrottle])
def request_password_reset(request):
    """POST /api/auth/password-reset/request - body {"email"}. Always 202
    with _RESET_SENT. With no account behind the address, one bcrypt hash
    at the code's cost stands in for generating one, so the reply takes
    about as long either way."""
    data = request.data if isinstance(request.data, dict) else {}
    email = str(data.get("email") or "").strip().lower()
    user = _reset_account(email)
    if user is None:
        bcrypt.hashpw(b"no-account", bcrypt.gensalt(rounds=10))
        return Response(_RESET_SENT, status=202)
    send_password_reset_otp(user)
    return Response(_RESET_SENT, status=202)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetConfirmThrottle])
def confirm_password_reset(request):
    """POST /api/auth/password-reset/confirm
    Body: {"email", "otp", "newPassword", "confirmPassword"}. 200 and no
    session on success; 400 with one message for every code failure."""
    data = request.data if isinstance(request.data, dict) else {}
    email = str(data.get("email") or "").strip().lower()
    otp = str(data.get("otp") or "").strip()
    if not otp:
        return Response({"detail": "Enter the code we emailed you."}, status=400)
    user = _reset_account(email)
    # Checked whatever the address, so a weak password reads the same for
    # an unknown one; the current-password comparison needs the account.
    new_password, is_current = _new_password(data, email, user.password_hash if user else None)
    if user is None:
        burn_code_check(otp)  # what verify_otp() costs, so timing says nothing
        return Response({"detail": _BAD_CODE}, status=400)
    if not verify_otp(user.email, otp, OTPCode.Purpose.PASSWORD_RESET):
        return Response({"detail": _BAD_CODE}, status=400)
    if is_current:
        return Response({"detail": _SAME_AS_CURRENT}, status=400)

    with transaction.atomic():
        PTUser.objects.filter(pk=user.pk).update(
            password_hash=_hash_password(new_password), failed_login_attempts=0, locked_until=None,
        )
        # Every token and every trusted device: whoever reset the password
        # signs in fresh, and so must anyone else.
        revoke_all_sessions(user)

    logger.info("password_views: password reset by emailed code for %s", user.email)
    log_pt_action(
        request, PTAuditLog.ACTION_USER_UPDATED, actor=user,
        detail="password reset from the sign-in page (emailed code); all sessions and trusted devices revoked",
    )
    notify_password_changed(user, "from the sign-in page (Forgot password)")
    return Response({"status": "ok", "detail": "Your password has been reset. Sign in with your new password."})
