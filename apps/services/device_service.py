"""
apps/services/device_service.py — Device trust and email OTP for new-device login.

Ported from the TDS Automation App's apps/services/device_service.py (same
design; pt_* cookie names instead of tds_*). Implements Instagram/Google-
style device-aware 2FA:
  - Devices that have already verified their OTP are trusted permanently
    (until the user logs out from that device, or an admin revokes it).
  - New/unknown devices receive a 6-digit email OTP challenge before
    getting a JWT.
  - Google OAuth logins on new devices go through the same OTP challenge.

Public API
----------
is_trusted_device(request, user_id)          -> bool
register_device(response, user_id, request)  -> str  (device_token)
set_access_cookie(response, access_token)     -> None
set_refresh_cookie(response, refresh_token)   -> None
get_client_ip(request)                        -> str
send_device_otp(user)                         -> str  (plaintext OTP, already emailed)
send_new_device_notification(user, request)   -> None (informational only)
notify_admins_new_device_login(user, request) -> None (informational only)
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sys
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.models import TrustedDevice
from apps.services.email_service import render_email
from apps.services.otp_service import generate_otp

log = logging.getLogger(__name__)

DEVICE_COOKIE_NAME = "pt_device"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year in seconds

# 'Remember me' refresh-token cookie. Scoped to /api/auth/ only - it's never
# needed outside the login/refresh/logout endpoints.
REFRESH_COOKIE_NAME = "pt_refresh"
REFRESH_COOKIE_PATH = "/api/auth/"


# ── Request/device info helpers ─────────────────────────────────────────────

def get_client_ip(request) -> str:
    """Extract the client IP, honouring X-Forwarded-For for the app's
    reverse proxy (Render).

    Trusts the LAST entry in X-Forwarded-For, not the first - reverse
    proxies APPEND to this header rather than replacing it, so with exactly
    one trusted proxy in front of the app (Render's edge), the entry it
    appends is the real peer it saw. The first (leftmost) entry is
    client-controlled and trivially spoofable (see the same fix already
    applied in the TDS app, ported here directly)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.META.get("REMOTE_ADDR", "") or ""


def _hash_device_token(token: str) -> str:
    """SHA-256 hex digest of a plaintext device token - see TrustedDevice's
    own docstring (apps/core/models.py) for why plain SHA-256, not bcrypt,
    is the right hash here (the token already has 256 bits of entropy, and
    is_trusted_device() needs an indexed equality lookup, not a per-row
    bcrypt.checkpw() scan)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_device_name(request) -> str:
    """Extract a display-safe device name from the request's User-Agent.

    The header is attacker-controlled (any caller can send an arbitrary
    User-Agent) and gets embedded in device-login notification emails - strip
    control characters (e.g. embedded newlines) so it can't break out of its
    single-line/paragraph context in the rendered email, on top of
    email_service.render_email()'s own HTML-escaping of this value."""
    raw = request.META.get("HTTP_USER_AGENT", "Unknown device")[:512]
    return "".join(ch if ch.isprintable() else " " for ch in raw).strip() or "Unknown device"


# ── Email dispatch helper ────────────────────────────────────────────────────

def _dispatch_email(send_fn) -> None:
    """Run send_fn() on a background thread in production, inline under the
    test runner (mirrors config/settings.py's "pytest" in sys.modules checks
    elsewhere - avoids a real race between a backgrounded send and a test
    asserting against mail.outbox right after the request returns). Checks
    sys.modules rather than `'test' in sys.argv`, since this app's test
    runner is pytest, not `manage.py test` - see settings.py's CACHES block
    for the full reasoning."""
    if "pytest" in sys.modules:
        send_fn()
    else:
        threading.Thread(target=send_fn, daemon=False).start()


# ── JWT cookie helpers ───────────────────────────────────────────────────────

def set_access_cookie(response, access_token: str) -> None:
    """Set the httpOnly pt_access cookie carrying the JWT access token.
    max_age mirrors SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']."""
    max_age = int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds())
    response.set_cookie(
        key=settings.PT_COOKIE_NAME,
        value=access_token,
        max_age=max_age,
        httponly=True,
        secure=settings.PT_COOKIE_SECURE,
        samesite=settings.PT_COOKIE_SAMESITE,
        path="/",
    )


def set_refresh_cookie(response, refresh_token: str) -> None:
    """Set the httpOnly pt_refresh cookie carrying the JWT refresh token -
    lets a trusted device stay signed in past the access token's lifetime
    without re-entering a password. max_age mirrors
    SIMPLE_JWT['REFRESH_TOKEN_LIFETIME']."""
    max_age = int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds())
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.PT_COOKIE_SECURE,
        samesite=settings.PT_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


# ── Trusted-device management ────────────────────────────────────────────────

def is_trusted_device(request, user_id: int) -> bool:
    """True if the incoming request carries a valid pt_device cookie that
    matches a TrustedDevice row owned by user_id. Bumps last_used_at via a
    targeted .update() (not a full save()) so a trusted device's every
    request doesn't pay for a full row rewrite just to record a timestamp.

    Looks up by the cookie's own SHA-256 hash, never the plaintext value -
    see TrustedDevice's own docstring (apps/core/models.py) for why the
    stored column is device_token_hash, not the raw token."""
    device_token = request.COOKIES.get(DEVICE_COOKIE_NAME, "").strip()
    if not device_token:
        return False
    try:
        device = TrustedDevice.objects.only("pk").get(
            device_token_hash=_hash_device_token(device_token), user_id=user_id
        )
        TrustedDevice.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
        return True
    except TrustedDevice.DoesNotExist:
        return False


def register_device(response, user_id: int, request) -> str:
    """Create a new TrustedDevice row and set the pt_device httpOnly cookie
    on the given Response object. Returns the plaintext device token (only
    for logging - never expose it, and never store it anywhere but the
    cookie - only its SHA-256 hash is persisted, see _hash_device_token())."""
    device_token = secrets.token_hex(32)  # 64 hex chars, 256-bit entropy
    ip = get_client_ip(request) or None
    device_name = _get_device_name(request)

    TrustedDevice.objects.create(
        user_id=user_id,
        device_token_hash=_hash_device_token(device_token),
        device_name=device_name,
        ip_address=ip,
    )

    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=device_token,
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=getattr(settings, "PT_DEVICE_COOKIE_SECURE", False),
        samesite="Lax",
        path="/",
    )

    log.info("register_device: new device registered user_id=%s ip=%s", user_id, ip)
    return device_token


# ── OTP challenge & notification emails ─────────────────────────────────────

def send_device_otp(user) -> str:
    """Generate a 6-digit OTP, store its hash in pt_otp_codes, and email the
    plaintext code to the user. Returns the plaintext code (already queued
    for sending - never log this value).

    The OTP is generated and committed to the DB synchronously (must be
    checkable the instant this call returns), but the send_mail() call runs
    on a background thread - an SMTP round-trip has no correctness reason to
    block the login request itself; the frontend only needs
    {status: 'device_verify'} to show the code-entry screen, not delivery
    confirmation."""
    otp = generate_otp(user.email)
    name = user.full_name or user.email.split("@")[0]

    subject = "Your Purchase Tracker Login Verification Code"
    _html_body, body = render_email(
        greeting=f"Hi {name},",
        body_paragraphs=[
            "A sign-in attempt was made to your Ravasco Purchase Tracker account from a new device or browser.",
            "Your one-time verification code is provided below. It expires in 10 minutes.",
        ],
        highlight_value=otp,
        highlight_label="Verification Code",
        after_highlight_paragraphs=[
            "If this was you, enter the code and this device will be remembered for future logins.",
            "If you did not attempt to sign in, please contact your administrator immediately.",
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
            log.info("send_device_otp: verification email sent to %s", user.email)
        except Exception as exc:
            log.error("send_device_otp: email send failed for %s: %s", user.email, exc)
            # DEV CONVENIENCE: print the code to the console when SMTP isn't
            # configured/working in DEBUG, so local dev never dead-ends.
            if settings.DEBUG:
                print(f"\n{'=' * 50}\nLogin OTP for {user.email}: {otp}\n{'=' * 50}\n")

    # daemon=False so a worker restart mid-send doesn't silently kill the
    # OTP email instead of letting it finish (bounded by EMAIL_TIMEOUT=10s).
    _dispatch_email(_send)

    return otp


def send_new_device_notification(user, request) -> None:
    """Send a 'new device logged in' notification email after a successful
    device verification. Purely informational - errors are logged but not
    propagated."""
    ip = get_client_ip(request) or "unknown"
    device_name = _get_device_name(request)[:80]
    name = user.full_name or user.email.split("@")[0]
    now = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    subject = "New Device Signed In to Your Purchase Tracker Account"
    _html_body, body = render_email(
        greeting=f"Hi {name},",
        body_paragraphs=[
            "A new device was added to your Ravasco Purchase Tracker account.",
            f"Device: {device_name}",
            f"IP Address: {ip}",
            f"Time: {now}",
            "This device will be trusted automatically for future logins.",
            "If this was you, no action is needed. If you did not do this, please "
            "contact your administrator immediately.",
        ],
    )

    def _send():
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True,
            )
            log.info("send_new_device_notification: sent to %s", user.email)
        except Exception as exc:
            log.warning("send_new_device_notification: failed for %s: %s", user.email, exc)

    _dispatch_email(_send)


def notify_admins_new_device_login(user, request) -> None:
    """Instagram/Google-style admin alert: whenever ANY user signs in from a
    new device, email every active admin with who logged in, from what
    device, and from where. Purely informational."""
    from apps.core.models import PTUser  # local import avoids any import-cycle risk

    ip = get_client_ip(request) or "unknown"
    device_name = _get_device_name(request)[:80]
    name = user.full_name or user.email.split("@")[0]
    now = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    admin_emails = list(
        PTUser.objects
        .filter(role="admin", is_active=True)
        .exclude(email=user.email)
        .values_list("email", flat=True)
    )
    if not admin_emails:
        return

    subject = f"[Purchase Tracker Admin Alert] New Device Login: {name}"
    _html_body, body = render_email(
        greeting="Hi,",
        body_paragraphs=[
            "A user signed in to the Ravasco Purchase Tracker system from a device that was not previously trusted.",
            f"User: {name} ({user.email}, role: {user.role})",
            f"Device: {device_name}",
            f"IP Address: {ip}",
            f"Time: {now}",
            "This device has been trusted for future logins on that account. If this "
            "looks suspicious, contact the user directly or revoke the device from the admin panel.",
        ],
    )

    def _send():
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails,
                fail_silently=True,
            )
            log.info("notify_admins_new_device_login: sent to %s admin(s) re: %s", len(admin_emails), user.email)
        except Exception as exc:
            log.warning("notify_admins_new_device_login: failed re: %s: %s", user.email, exc)

    _dispatch_email(_send)
