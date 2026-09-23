"""
Tests for apps/services/advance_license_report.py - the two consolidated
Advance License validity-expiry alerts (Import / Export), each firing once
per license the first time it's seen inside the 30-day window (see that
module's own docstring for why, and for the ReportSendLog dedup reasoning).
"""
import datetime
from smtplib import SMTPException

import pytest
from django.core import mail
from django.core.mail import send_mail

from apps.api.tests.factories import make_user
from apps.core.models import AdvanceLicense, AdvanceLicenseMaterial, ReportSendLog
from apps.services.advance_license_report import send_advance_license_expiry_reports
from apps.services.tests.refusing_email_backends import LOCMEM, REFUSE_ALL, REFUSE_IMPORT_VALIDITY

TODAY = datetime.date.today()


def _make_license(license_number="0311051817", days_to_import=10, days_to_export=None, materials=None):
    lic = AdvanceLicense.objects.create(
        license_number=license_number,
        export_product_description="Rubber Conveyor Belting",
        cif_value_authorized="4500000.00",
        fob_value_export_target="6200000.00",
        import_validity_date=(TODAY + datetime.timedelta(days=days_to_import)) if days_to_import is not None else None,
        export_validity_date=(TODAY + datetime.timedelta(days=days_to_export)) if days_to_export is not None else None,
    )
    for m in (materials if materials is not None else ["Natural Rubber Sheet", "Carbon Black N330"]):
        AdvanceLicenseMaterial.objects.create(license=lic, material_description=m)
    return lic


@pytest.mark.django_db
class TestSendAdvanceLicenseExpiryReports:
    def test_license_inside_import_window_triggers_import_email_only(self):
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=15, days_to_export=None)

        result = send_advance_license_expiry_reports()

        assert result["importSent"] is True
        assert result["importLicenses"] == 1
        assert result["exportSent"] is False
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert "Import Validity" in msg.subject
        assert "admin@ravasco.com" in msg.to
        assert "imports@ravasco.com" in msg.to
        assert "0311051817" in msg.body
        # materials render deduped and alphabetically sorted for determinism
        assert "Carbon Black N330; Natural Rubber Sheet" in msg.body

    def test_license_outside_window_is_not_alerted(self):
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=45)  # past the 30-day window

        result = send_advance_license_expiry_reports()

        assert result["importSent"] is False
        assert len(mail.outbox) == 0

    def test_license_already_expired_is_not_alerted(self):
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=-5)  # already past its validity date

        result = send_advance_license_expiry_reports()

        assert result["importSent"] is False
        assert len(mail.outbox) == 0

    def test_second_run_does_not_repeat_an_already_alerted_license(self):
        """A license still inside the window on a later run must not be
        alerted a second time - see module docstring's dedup reasoning."""
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=20)

        first = send_advance_license_expiry_reports()
        second = send_advance_license_expiry_reports()

        assert first["importLicenses"] == 1
        assert second["importLicenses"] == 0
        assert len(mail.outbox) == 1
        expected_key = f"0311051817@{(TODAY + datetime.timedelta(days=20)).isoformat()}"
        assert ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.ADV_LICENSE_IMPORT, period_key=expected_key,
        ).count() == 1

    def test_extending_import_validity_re_alerts_once_on_the_new_date(self):
        """Project owner, 2026-09-22: an Advance License's validity gets
        extended, sometimes repeatedly, and the extended deadline must alert
        again - the original alert must not burn that license forever. The
        date is part of the dedup key, so a NEW date is a new claim."""
        make_user(email="admin-ext@ravasco.com", role="admin")
        lic = _make_license(days_to_import=20)

        first = send_advance_license_expiry_reports()
        assert first["importLicenses"] == 1

        # Extended by a month, still inside the 30-day window.
        lic.import_validity_date = TODAY + datetime.timedelta(days=25)
        lic.save(update_fields=["import_validity_date"])

        second = send_advance_license_expiry_reports()
        third = send_advance_license_expiry_reports()

        assert second["importLicenses"] == 1, "extended deadline must alert once"
        assert third["importLicenses"] == 0, "and then never repeat on the new date either"
        assert len(mail.outbox) == 2
        assert ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.ADV_LICENSE_IMPORT,
            period_key__startswith="0311051817@",
        ).count() == 2

    def test_extending_export_validity_re_alerts_independently_of_import(self):
        """Same guarantee on the export side, which is the one actually
        expected to be extended repeatedly."""
        make_user(email="admin-ext2@ravasco.com", role="admin")
        lic = _make_license(days_to_import=None, days_to_export=15)

        assert send_advance_license_expiry_reports()["exportLicenses"] == 1
        lic.export_validity_date = TODAY + datetime.timedelta(days=28)
        lic.save(update_fields=["export_validity_date"])
        after = send_advance_license_expiry_reports()

        assert after["exportLicenses"] == 1
        assert after["importLicenses"] == 0, "import side untouched by an export extension"
        assert len(mail.outbox) == 2

    def test_license_with_no_materials_shows_placeholder(self):
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=10, materials=[])

        result = send_advance_license_expiry_reports()

        assert result["importSent"] is True
        assert "Input Material Description" not in mail.outbox[0].body  # header only in HTML, not text
        assert " -  " not in mail.outbox[0].body  # sanity: no double placeholder artifact

    def test_a_license_can_be_alerted_on_both_import_and_export_independently(self):
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(license_number="0311055303", days_to_import=10, days_to_export=25)

        result = send_advance_license_expiry_reports()

        assert result["importSent"] is True
        assert result["exportSent"] is True
        assert len(mail.outbox) == 2

    def test_no_admins_and_no_licenses_in_window_sends_nothing(self):
        result = send_advance_license_expiry_reports()

        assert result == {
            "date": TODAY.isoformat(), "importSent": False, "importLicenses": 0,
            "exportSent": False, "exportLicenses": 0,
        }
        assert len(mail.outbox) == 0

    def test_refusing_backend_honours_fail_silently(self, settings):
        """Pins the premise the two failure tests below rest on: the fake
        backend swallows a refused send under fail_silently=True (returning 0,
        raising nothing) and raises under fail_silently=False - exactly what
        Django's SMTP backend does facing a dead server. So if
        _send_one() ever regresses to fail_silently=True, nothing raises, its
        `except` never runs, the claim is kept, and the next test fails on
        "no ReportSendLog row survives". That is how those tests satisfy
        CLAUDE.md's "a test must fail when the fix is reverted" without anyone
        having to revert the fix to find out."""
        settings.EMAIL_BACKEND = REFUSE_ALL

        assert send_mail("s", "b", "from@ravasco.com", ["to@ravasco.com"], fail_silently=True) == 0
        with pytest.raises(SMTPException):
            send_mail("s", "b", "from@ravasco.com", ["to@ravasco.com"], fail_silently=False)

    def test_send_failure_releases_claims_so_the_next_run_retries(self, settings):
        """A delivery failure must leave NO ReportSendLog row behind.

        This is the test the claim-release branch never had, and its absence
        is why that branch sat unreachable: send_mail() was called with
        fail_silently=True, so an SMTP fault returned normally instead of
        raising, the `except` never ran, and the claim stayed. Because this
        report's period_key embeds the validity date rather than the run
        date, a kept claim is not retried tomorrow the way a consumption
        report's is - it is never retried at all, and the license expires
        unannounced.

        The failure is injected at the BACKEND, not by replacing send_mail:
        a replaced send_mail raises regardless of fail_silently, so a test
        built that way passes with the bug present. See
        refusing_email_backends.py's module docstring.
        """
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=12)

        settings.EMAIL_BACKEND = REFUSE_ALL
        failed = send_advance_license_expiry_reports()

        assert failed["importSent"] is False
        assert failed["importLicenses"] == 0
        assert ReportSendLog.objects.count() == 0, "claim was not released - the license is now permanently suppressed"

        # The next run, with mail working again, must still alert it.
        settings.EMAIL_BACKEND = LOCMEM
        retried = send_advance_license_expiry_reports()

        assert retried["importSent"] is True
        assert retried["importLicenses"] == 1
        assert len(mail.outbox) == 1
        assert "0311051817" in mail.outbox[0].body

    def test_a_failing_import_send_does_not_suppress_the_export_alert(self, settings):
        """The two kinds are independent, and a claim released by one must
        not take the other's email with it - _send_one() is called twice and
        each owns only its own claims."""
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=8, days_to_export=8)

        settings.EMAIL_BACKEND = REFUSE_IMPORT_VALIDITY
        result = send_advance_license_expiry_reports()

        assert result["importSent"] is False
        assert result["exportSent"] is True
        assert len(mail.outbox) == 1
        assert "Export Validity" in mail.outbox[0].subject
        # Import's claim released, Export's kept - exactly one row survives.
        assert ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.ADV_LICENSE_IMPORT).count() == 0
        assert ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.ADV_LICENSE_EXPORT).count() == 1

    def test_subject_lines_carry_no_em_dash(self):
        """The em-dash sweep (2026-09-22) missed this module because it
        landed in the same batch of commits. A subject line is the one
        string here that reaches a real inbox, so it gets its own guard
        rather than relying on the sweep having been thorough."""
        make_user(email="admin@ravasco.com", role="admin")
        _make_license(days_to_import=8, days_to_export=8)

        send_advance_license_expiry_reports()

        assert len(mail.outbox) == 2
        for msg in mail.outbox:
            assert "\u2014" not in msg.subject
            assert "\u2014" not in msg.body
