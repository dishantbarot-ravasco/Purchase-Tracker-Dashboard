"""
apps/services/password_service.py — OTP email for self-service password
change (project owner, 2026-09-07: "add the change password for all users
and add the otp to it for verification like we do for 1st time devices").

Deliberately its own small module rather than added to device_service.py:
that file's own docstring scopes it to device trust and login - this is a
different concern (an already-authenticated user changing their own
password), even though it reuses the same underlying OTP store
(apps/services/otp_service.py) and render_email() template helper.

Public API
----------
send_password_change_otp(user) -> None  (OTP emailed on a background thread,
                                          same fire-and-forget shape as
                                          device_service.py's send_device_otp)
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.core.mail import send_mail

from apps.services.email_service import render_email
from apps.services.otp_service import generate_otp

log = logging.getLogger(__name__)


def send_password_change_otp(user) -> None:
    """Generates a 6-digit OTP (apps/services/otp_service.py - the same
    store new-device login uses; one active code per email at a time,
    regardless of which flow requested it) and emails it to `user`'s own
    address. The send itself runs on a background thread, same reasoning as
    send_device_otp(): the frontend only needs to know a code is on its way,
    not delivery confirmation, so an SMTP round-trip shouldn't block the
    request."""
    otp = generate_otp(user.email)
    name = user.full_name or user.email.split("@")[0]

    subject = "Confirm Your Purchase Tracker Password Change"
    _html_body, body = render_email(
        greeting=f"Hi {name},",
        body_paragraphs=[
            "You (or someone with access to your account) requested to change your Ravasco Purchase Tracker password.",
            "Your one-time verification code is provided below. It expires in 10 minutes.",
        ],
        highlight_value=otp,
        highlight_label="Verification Code",
        after_highlight_paragraphs=[
            "If this was you, enter the code to confirm the new password.",
            "If you did not request this, ignore this email - your password will not be changed without the code above, and you should contact your administrator.",
        ],
    )

    def _send():
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception:
            log.exception("send_password_change_otp: failed to send OTP email to %s", user.email)

    threading.Thread(target=_send, daemon=True).start()
