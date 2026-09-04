"""
apps/api/auth_serializers.py — Custom JWT token serializer.

Ported from the TDS Automation App's apps/api/auth_serializers.py.

Token payload: {"sub": "<user_id_as_string>", "role": "<role>", ...}.

validate() implements the device-aware 2FA gate:
  - Trusted device -> return {status: 'ok', access_token, refresh, ...}
  - New device     -> send OTP, store pending_user_id in session,
                       return {status: 'device_verify'}
"""

import logging

from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings

from apps.api.auth_backend import pt_user_authentication_rule
from apps.core.models import PTUser
from apps.services.device_service import is_trusted_device, send_device_otp

logger = logging.getLogger(__name__)


class PTTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Authenticate via email + bcrypt (PTUserBackend), embed sub=user_id
    and role in the JWT payload, and apply the device-aware 2FA gate before
    issuing a full JWT."""

    username_field = "email"

    email = serializers.EmailField(write_only=True)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["sub"] = str(user.user_id)
        token["role"] = user.role
        token["email"] = user.email
        token["full_name"] = user.full_name or ""
        return token

    def validate(self, attrs):
        email = attrs.get("email", "").strip().lower()
        password = attrs.get("password", "")

        request = self.context.get("request")

        user = authenticate(request=request, email=email, password=password)

        if user is None:
            raise serializers.ValidationError(
                {"detail": "Invalid email or password."},
                code="authentication_failed",
            )

        if is_trusted_device(request, user.user_id):
            refresh = self.get_token(user)
            logger.info("auth: trusted device login for user_id=%s", user.user_id)
            return {
                "status": "ok",
                "access_token": str(refresh.access_token),
                "refresh": str(refresh),
                "user_id": user.user_id,
                "role": user.role,
                "full_name": user.full_name or "",
                "email": user.email,
            }
        else:
            # New device - trigger OTP email, hold login until verified.
            # Session is set BEFORE the send attempt: generate_otp() already
            # commits the OTP's hash before send_device_otp() ever tries to
            # email it, so the code is checkable the instant this returns -
            # a transient SMTP failure shouldn't also throw away the pending
            # session needed to complete verification once the code is known
            # some other way (DEBUG console fallback, etc).
            if request is not None:
                request.session["pending_user_id"] = user.user_id
                request.session.modified = True

            try:
                send_device_otp(user)
            except Exception as exc:
                # Real bug, found and fixed 2026-09-04: this used to log the
                # failure and still return {"status": "device_verify"} below
                # regardless - telling the frontend "a code was sent, go
                # ahead and enter it" even though it wasn't. A transient
                # SMTP failure genuinely can't reach here (send_device_otp()
                # dispatches the actual send_mail() call on a background
                # thread that swallows its own delivery failures - see that
                # function's own docstring/comments), so an exception this
                # far up can only mean generate_otp() itself (the DB write
                # that creates the checkable code) or the email template
                # never ran at all - there is no code for the user to enter,
                # by any path, DEBUG console fallback included. Silently
                # sending them to a code-entry screen that can never accept
                # any code is worse than a clear error up front. Matches the
                # Google OAuth login path's own handling of this exact same
                # failure (google_callback() in google_oauth_views.py),
                # which already did this correctly.
                logger.error("auth: failed to send device OTP for user_id=%s: %s", user.user_id, exc)
                raise serializers.ValidationError(
                    {"detail": "Could not send the verification code. Please try again in a moment or contact your administrator."},
                    code="otp_send_failed",
                )
            else:
                logger.info("auth: new device - OTP sent for user_id=%s", user.user_id)
            return {"status": "device_verify"}


class PTTokenRefreshSerializer(TokenRefreshSerializer):
    """Resolve PTUser directly instead of via get_user_model() - see
    apps/api/auth_backend.py's module docstring for the production incident
    this avoids in the TDS app (and would reproduce here unmodified)."""

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])

        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM, None)
        if user_id is not None:
            user = PTUser.objects.filter(pk=user_id).first()
            if not pt_user_authentication_rule(user):
                raise AuthenticationFailed(
                    self.error_messages["no_active_account"],
                    "no_active_account",
                )

        data = {"access": str(refresh.access_token)}

        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass
            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()
            data["refresh"] = str(refresh)

        return data
