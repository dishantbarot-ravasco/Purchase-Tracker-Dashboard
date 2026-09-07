"""
apps/services/security_alerts.py — admin-facing security event emails.

Added 2026-09-05 (hardening pass), closing the "logging/monitoring/
alerting" gap a security review flagged as this app's weakest area:
PTAuditLog and logs/app.log both record security-relevant events, but
nothing ever notified anyone - an attacker could brute-force an account for
days and the only sign would be a log line nobody's watching. Follows the
same pattern already established in apps/services/device_service.py's
notify_admins_new_device_login() (render_email() for a consistently
branded, plain-text-first body; _dispatch_email() to send on a background
thread in production, inline under pytest) - reusing that module's private
_dispatch_email() directly rather than duplicating it, since these are
tightly-coupled internal helpers within apps/services, not a public API.

Every function here is purely informational/best-effort: a failed alert
email must never block or fail the real action it's reporting on (a login
attempt, a sync run) - same "never propagate" convention as
send_new_device_notification()/notify_admins_new_device_login().
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone

from apps.services.device_service import _dispatch_email
from apps.services.email_service import render_email

log = logging.getLogger(__name__)

# Login-burst detection: a lightweight, cache-backed counter, not a
# database table - this only needs to catch "something is hammering logins
# right now," not build a permanent forensic record (PTAuditLog/logs/app.log
# already do that). Deliberately coarse: one counter per fixed-size window,
# not a sliding window - simple to reason about, and "N failed logins in
# roughly this many minutes" is precise enough for an operational alert.
_BURST_WINDOW_SECONDS = 300  # 5 minutes
_BURST_THRESHOLD = 15  # failed logins across ANY accounts within the window
_BURST_ALERT_SUPPRESS_SECONDS = 1800  # don't re-alert more than once per 30 min


def _admin_emails(exclude_email: str = "") -> list:
    from apps.core.models import PTUser  # local import avoids any import-cycle risk

    qs = PTUser.objects.filter(role="admin", is_active=True)
    if exclude_email:
        qs = qs.exclude(email=exclude_email)
    return list(qs.values_list("email", flat=True))


def notify_admins_account_locked(user) -> None:
    """Sent once, at the moment an account actually locks (not on every
    failed attempt - see apps/api/auth_backend.py's _register_failed_attempt(),
    the only call site) - 5 failed passwords in a row is a real signal an
    admin should see, not routine noise."""
    admin_emails = _admin_emails(exclude_email=user.email)
    if not admin_emails:
        return

    name = user.full_name or user.email.split("@")[0]
    now = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[Purchase Tracker Admin Alert] Account Locked: {name}"
    _html_body, body = render_email(
        greeting="Hi,",
        body_paragraphs=[
            f"The account for {name} ({user.email}) was locked after 5 consecutive failed "
            "login attempts.",
            f"Time: {now}",
            "The account will unlock automatically after the lockout period, or you can "
            "review this account directly from the Admin Panel. If this doesn't look like "
            "the account holder simply mistyping their password, consider resetting their "
            "password or revoking their sessions.",
        ],
    )

    def _send():
        try:
            send_mail(
                subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, fail_silently=True,
            )
            log.info("notify_admins_account_locked: sent for %s", user.email)
        except Exception as exc:
            log.warning("notify_admins_account_locked: failed for %s: %s", user.email, exc)

    _dispatch_email(_send)


def record_failed_login_and_maybe_alert() -> None:
    """Increment the login-burst counter and alert admins once per
    suppression window if it crosses _BURST_THRESHOLD. Called on every
    failed password attempt (apps/api/auth_backend.py), regardless of which
    account - a credential-stuffing run typically spreads across many
    different accounts, so per-account counters (LoginRateThrottle,
    failed_login_attempts) wouldn't individually cross their own thresholds
    even though the aggregate clearly indicates an attack in progress."""
    window_key = f"login_burst:{int(timezone.now().timestamp()) // _BURST_WINDOW_SECONDS}"
    count = cache.get(window_key, 0) + 1
    cache.set(window_key, count, timeout=_BURST_WINDOW_SECONDS)

    if count < _BURST_THRESHOLD:
        return

    suppress_key = f"login_burst_alerted:{window_key}"
    if cache.get(suppress_key):
        return
    cache.set(suppress_key, True, timeout=_BURST_ALERT_SUPPRESS_SECONDS)

    admin_emails = _admin_emails()
    if not admin_emails:
        return

    now = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    subject = "[Purchase Tracker Admin Alert] Unusual Login Activity Detected"
    _html_body, body = render_email(
        greeting="Hi,",
        body_paragraphs=[
            f"{count} failed login attempts were recorded across the system in a short "
            f"window (roughly {_BURST_WINDOW_SECONDS // 60} minutes), as of {now}.",
            "This may indicate a credential-stuffing or brute-force attempt against "
            "multiple accounts. Review recent login activity and locked accounts from the "
            "Admin Panel, and consider whether any affected accounts need a password reset.",
        ],
    )

    def _send():
        try:
            send_mail(
                subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, fail_silently=True,
            )
            log.info("record_failed_login_and_maybe_alert: burst alert sent (count=%s)", count)
        except Exception as exc:
            log.warning("record_failed_login_and_maybe_alert: alert send failed: %s", exc)

    _dispatch_email(_send)


def notify_admins_sync_failure(plant_key: str, cmd_name: str, detail: str = "") -> None:
    """Sent when a sync_*/match_* command fails inside
    apps/services/sync_trigger.py's pipeline - previously only visible via
    logs/app.log or a SyncRun row's own error_detail, both of which require
    someone to go looking. Deliberately lightweight/best-effort like every
    other function here - if this alert itself fails to send, the sync
    failure it's reporting on has already been recorded elsewhere
    regardless (SyncRun + logs), so nothing is lost.

    Deliberately NOT sent to every admin (unlike this module's other
    alerts) - restricted to a single fixed recipient per an explicit
    request, 2026-09-07."""
    admin_emails = ["dishant.barot@ravasco.com"]

    now = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[Purchase Tracker Admin Alert] Sync Failure: {plant_key} / {cmd_name}"
    paragraphs = [
        f"The {cmd_name!r} step of the {plant_key!r} sync pipeline failed.",
        f"Time: {now}",
    ]
    if detail:
        paragraphs.append(f"Detail: {detail}")
    paragraphs.append(
        "Check the Admin Panel's sync status cards and logs/app.log for the full trace. "
        "Data for this plant may be stale until the next successful sync."
    )
    _html_body, body = render_email(greeting="Hi,", body_paragraphs=paragraphs)

    def _send():
        try:
            send_mail(
                subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, fail_silently=True,
            )
            log.info("notify_admins_sync_failure: sent for plant=%s cmd=%s", plant_key, cmd_name)
        except Exception as exc:
            log.warning("notify_admins_sync_failure: failed for plant=%s cmd=%s: %s", plant_key, cmd_name, exc)

    _dispatch_email(_send)
