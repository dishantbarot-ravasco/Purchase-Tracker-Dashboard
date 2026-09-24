"""
Test-only email backends that fail the way a real SMTP server does.

Why these exist (2026-09-23, audit pass): every claim-before-send report in
this app (daily/monthly consumption, Advance License expiry) releases its
ReportSendLog claim in an `except` when delivery fails. That branch was
unreachable in production for as long as the senders passed
fail_silently=True, and no test caught it, because the obvious way to test a
send failure - monkeypatching `send_mail` itself to raise - bypasses
fail_silently entirely. A test written that way passes whether the sender
passes True or False, so it cannot fail when the bug is put back.

These backends fail at the BACKEND layer instead, and honour fail_silently
exactly as django.core.mail.backends.smtp.EmailBackend does facing a dead
server: raise SMTPException, unless the caller asked for fail_silently=True,
in which case swallow it and report 0 sent. A sender that regresses to
fail_silently=True therefore stops raising, its claim is never released, and
the test's "no ReportSendLog row survives" assertion fails - which is the
whole point. test_refusing_backend_honours_fail_silently() in
test_advance_license_report.py pins that the backend really does behave both
ways, so that chain of reasoning is itself under test.

Not collected by pytest (no test_ prefix). Point settings.EMAIL_BACKEND at a
class here via its dotted path, e.g.
    settings.EMAIL_BACKEND = REFUSE_ALL
"""
from smtplib import SMTPException

from django.core.mail.backends import locmem

_MODULE = "apps.services.tests.refusing_email_backends"
REFUSE_ALL = f"{_MODULE}.RefuseAllBackend"
REFUSE_IMPORT_VALIDITY = f"{_MODULE}.RefuseImportValidityBackend"
REFUSE_ALL_BUT_VAPI = f"{_MODULE}.RefuseAllButVapiBackend"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


class _RefusingBackend(locmem.EmailBackend):
    """Refuses any message whose subject contains `refuse_marker` (an empty
    marker refuses everything); delivers the rest to mail.outbox exactly like
    the normal test backend."""

    refuse_marker = ""

    def send_messages(self, messages):
        if any(self.refuse_marker in m.subject for m in messages):
            if self.fail_silently:
                return 0
            raise SMTPException("Connection unexpectedly closed")
        return super().send_messages(messages)


class RefuseAllBackend(_RefusingBackend):
    refuse_marker = ""


class RefuseImportValidityBackend(_RefusingBackend):
    """Fails only the Advance License Import Validity alert, so a test can
    prove one kind's failure does not take the other kind's claims with it."""

    refuse_marker = "Import Validity"


class RefuseAllButVapiBackend(locmem.EmailBackend):
    """Refuses every consumption report except RTP-Vapi's - the 2026-09-23
    run, where only Vapi's daily report went out. Same fail_silently
    contract as _RefusingBackend."""

    def send_messages(self, messages):
        if any("RTP-Vapi" not in m.subject for m in messages):
            if self.fail_silently:
                return 0
            raise SMTPException("Connection unexpectedly closed")
        return super().send_messages(messages)


STALL_ON_CONNECT = f"{_MODULE}.StallOnConnectBackend"


class StallOnConnectBackend(locmem.EmailBackend):
    """Fails in open() - the connect/login step - with the timeout an
    unreachable SMTP server produces, the shape the 2026-09-23 run's 24s
    suggests. Delivers nothing."""

    def open(self):
        if self.fail_silently:
            return False
        raise TimeoutError("timed out")
