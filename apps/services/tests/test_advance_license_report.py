"""
Tests for apps/services/advance_license_report.py - the two consolidated
Advance License validity-expiry alerts (Import / Export), each firing once
per license the first time it's seen inside the 30-day window (see that
module's own docstring for why, and for the ReportSendLog dedup reasoning).
"""
import datetime

import pytest
from django.core import mail

from apps.api.tests.factories import make_user
from apps.core.models import AdvanceLicense, AdvanceLicenseMaterial, ReportSendLog
from apps.services.advance_license_report import send_advance_license_expiry_reports

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
        assert "import@ravasco.com" in msg.to
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
        assert ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.ADV_LICENSE_IMPORT, period_key="0311051817",
        ).count() == 1

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
