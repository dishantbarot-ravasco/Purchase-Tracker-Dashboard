"""
Tests for apps/services/consumption_report.py - the daily 20:00 IST
Raw Material Consumption report (one email per plant). Real Postgres via
pytest-django (RMLot/RMSnapshot/RMDailyMovement are real DB models), same
factory-function convention as apps/api/tests/test_materials_days_left.py.
"""
import datetime
from decimal import Decimal

import pytest

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSRMLot,
    HRSRMSnapshot,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
)
from apps.services.consumption_report import build_plant_report, send_daily_consumption_reports

TODAY = datetime.date.today()


@pytest.mark.django_db
class TestBuildPlantReportHRS:
    def test_lot_issued_today_appears_with_rate_and_days_left(self):
        lot = HRSRMLot.objects.create(
            description="Natural Rubber", basic_rate=Decimal("120.5000"), todays_stock=Decimal("800"),
        )
        # Steady 100/day drawdown over the last 2 days, then today's issue.
        for days_ago, stock in [(2, Decimal("1000")), (1, Decimal("900"))]:
            HRSRMSnapshot.objects.create(
                stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
                opening_stock=stock, received=0, issued=0, todays_stock=stock,
                basic_rate=Decimal("120.5000"), value=stock * Decimal("120.5"),
            )
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY,
            opening_stock=Decimal("900"), received=0, issued=Decimal("100"), todays_stock=Decimal("800"),
            basic_rate=Decimal("125.0000"), value=Decimal("100000.00"),
        )

        report = build_plant_report("hrs", today=TODAY)

        assert report["label"] == "HRS (Hindustan Rubbers, Silvassa)"
        assert report["date"] == TODAY.isoformat()
        [row] = report["rows"]
        assert row["material"] == "Natural Rubber"
        assert row["issuedToday"] == Decimal("100")
        # Latest rate comes from the live lot row, not the snapshot.
        assert row["rate"] == Decimal("120.5000")
        assert row["daysLeft"] == 8.0
        assert row["confidence"] == "low"

    def test_lot_with_no_issue_today_is_excluded(self):
        lot = HRSRMLot.objects.create(description="Carbon Black", basic_rate=Decimal("50"), todays_stock=Decimal("500"))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY, opening_stock=Decimal("500"), received=0, issued=0,
            todays_stock=Decimal("500"), basic_rate=Decimal("50"), value=Decimal("25000"),
        )

        report = build_plant_report("hrs", today=TODAY)

        assert report["rows"] == []

    def test_inactive_lot_is_excluded_even_if_issued_today(self):
        lot = HRSRMLot.objects.create(
            description="Old Lot", basic_rate=Decimal("10"), todays_stock=Decimal("0"), is_active=False,
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY, opening_stock=Decimal("10"), received=0, issued=Decimal("10"),
            todays_stock=Decimal("0"), basic_rate=Decimal("10"), value=Decimal("0"),
        )

        report = build_plant_report("hrs", today=TODAY)

        assert report["rows"] == []


@pytest.mark.django_db
class TestBuildPlantReportAchhad:
    def test_issued_today_comes_from_daily_movement_not_the_period_summary_field(self):
        """RTPAchhadRMLot.issued is a period-to-date summary that resets each
        period (see CLAUDE.md's Days-Left Engine section) - it must NOT be
        used as "today's issued". Only RTPAchhadRMDailyMovement carries a
        real per-day figure."""
        lot = RTPAchhadRMLot.objects.create(
            description="Butyl Rubber", rate=Decimal("200"), todays_stock=Decimal("300"),
            issued=Decimal("9999"),  # period-to-date noise - must be ignored
        )
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=TODAY, received=Decimal("0"), issued=Decimal("15"),
        )

        report = build_plant_report("achhad", today=TODAY)

        [row] = report["rows"]
        assert row["issuedToday"] == Decimal("15")
        assert row["rate"] == Decimal("200")

    def test_no_movement_today_is_excluded_even_with_snapshot_issued_set(self):
        lot = RTPAchhadRMLot.objects.create(description="EPDM", rate=Decimal("80"), todays_stock=Decimal("100"))
        RTPAchhadRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY, opening_stock=Decimal("110"), received=0, issued=Decimal("10"),
            todays_stock=Decimal("100"), rate=Decimal("80"), value=Decimal("8000"),
        )

        report = build_plant_report("achhad", today=TODAY)

        assert report["rows"] == []


@pytest.mark.django_db
class TestSendDailyConsumptionReports:
    def test_no_admins_sends_nothing(self):
        result = send_daily_consumption_reports()
        assert result["plants_sent"] == 0
        assert result["admins_notified"] == 0

    def test_sends_one_email_per_plant_to_every_active_admin(self, mailoutbox):
        make_user(email="admin1@ravasco.com", role="admin")
        inactive = make_user(email="admin2@ravasco.com", role="admin")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        lot = HRSRMLot.objects.create(description="Silica", basic_rate=Decimal("60"), todays_stock=Decimal("40"))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY, opening_stock=Decimal("50"), received=0, issued=Decimal("10"),
            todays_stock=Decimal("40"), basic_rate=Decimal("60"), value=Decimal("2400"),
        )

        result = send_daily_consumption_reports()

        assert result["plants_sent"] == 3
        assert result["admins_notified"] == 1  # inactive admin excluded
        assert len(mailoutbox) == 3
        assert all(m.to == ["admin1@ravasco.com"] for m in mailoutbox)
        subjects = {m.subject for m in mailoutbox}
        assert any("HRS" in s for s in subjects)
        assert any("RTP-Achhad" in s for s in subjects)
        assert any("RTP-Vapi" in s for s in subjects)
