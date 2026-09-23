"""
Email delivery failures must be visible, and must never break the caller.

Added 2026-09-23 (audit pass). Every sender in this app wraps send_mail() in a
try/except that logs - and until this date most of them ALSO passed
fail_silently=True, which made the except unreachable for the failure it was
written for. An SMTP fault returned normally, the next line logged "sent", and
nothing reached logs/app.log as a failure or Sentry at all (Sentry's
LoggingIntegration only picks up ERROR and above). For the claim-based reports
it was worse - see refusing_email_backends.py - but for the admin security
alerts it meant the one channel that tells anyone an account was locked or a
sync died could itself be dead with no trace.

Pinned here:
  - no application module passes fail_silently=True (source guard);
  - a failed admin alert logs an ERROR naming the failure, and does NOT also
    log that it was sent;
  - the failure still never propagates to the caller - an alert must not be
    able to break the login or sync that triggered it.
"""

import ast
import logging
from pathlib import Path

import pytest

from apps.services.security_alerts import notify_admins_sync_failure
from apps.services.tests.refusing_email_backends import REFUSE_ALL

APPS_DIR = Path(__file__).resolve().parents[2]


def test_no_application_module_passes_fail_silently_true():
    offenders = []
    for path in APPS_DIR.rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "fail_silently":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    offenders.append(f"{path.relative_to(APPS_DIR.parent)}:{node.value.lineno}")
    assert not offenders, (
        "fail_silently=True makes a send failure return normally, so the surrounding except never runs, "
        "the failure is logged as a success, and a claim-based report keeps its claim. Pass False and "
        "handle the exception - every sender here already has an except for it:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.django_db
def test_a_failed_admin_alert_logs_an_error_and_never_claims_it_was_sent(settings, caplog):
    settings.EMAIL_BACKEND = REFUSE_ALL

    with caplog.at_level(logging.INFO, logger="apps.services.security_alerts"):
        notify_admins_sync_failure("hrs", "sync_mir", detail="simulated")  # must not raise

    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any(level == logging.ERROR and "failed" in msg for level, msg in messages), messages
    assert not any("sent for plant" in msg for _, msg in messages), \
        "logged a send that never happened - the exact misleading line fail_silently=True produced"
