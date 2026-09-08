"""
Tests for apps/services/consumption_report.py - the daily
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
from apps.services.consumption_report import (
    build_plant_monthly_report,
    build_plant_report,
    send_daily_consumption_reports,
    send_monthly_consumption_reports,
)

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

    def test_row_carries_category_blank_becomes_uncategorized(self):
        """Category grouping (2026-09-08, project owner: "divide the
        material by category, add a category pane in the table") - a blank
        category (real on some rows, e.g. never set during sync) buckets as
        "Uncategorized" rather than an empty-titled group."""
        lot = HRSRMLot.objects.create(
            description="Mystery Chemical", basic_rate=Decimal("10"), todays_stock=Decimal("100"), category="",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY, opening_stock=Decimal("110"), received=0, issued=Decimal("10"),
            todays_stock=Decimal("100"), basic_rate=Decimal("10"), value=Decimal("1000"),
        )

        report = build_plant_report("hrs", today=TODAY)

        [row] = report["rows"]
        assert row["category"] == "Uncategorized"

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

    def test_falls_back_to_period_to_date_issued_when_no_daily_movement_row_exists_at_all(self):
        """Real bug found and fixed 2026-09-08, confirmed directly against a
        real exported RM Stock file: the day-matrix can sit completely blank
        for the current day (the plant hadn't filled it in yet at export
        time), which previously meant the report silently showed "no
        material issued today" even though real activity was visible via the
        lot's own period-to-date Issued column. Project owner's explicit
        choice: fall back to that period-to-date figure as a rough estimate
        rather than showing nothing, flagged isEstimate so it's never
        confused with a confirmed same-day figure."""
        lot = RTPAchhadRMLot.objects.create(
            description="Neoprene", rate=Decimal("300"), todays_stock=Decimal("50"), issued=Decimal("62"),
        )
        # No RTPAchhadRMDailyMovement row for TODAY at all - day-matrix blank.

        report = build_plant_report("achhad", today=TODAY)

        [row] = report["rows"]
        assert row["issuedToday"] == Decimal("62")
        assert row["isEstimate"] is True

    def test_a_real_dated_movement_row_is_never_overridden_by_the_fallback(self):
        lot = RTPAchhadRMLot.objects.create(
            description="Hypalon", rate=Decimal("400"), todays_stock=Decimal("20"), issued=Decimal("9999"),
        )
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=TODAY, received=Decimal("0"), issued=Decimal("5"),
        )

        report = build_plant_report("achhad", today=TODAY)

        [row] = report["rows"]
        assert row["issuedToday"] == Decimal("5")
        assert row["isEstimate"] is False

    def test_inactive_lot_is_excluded_from_the_fallback_too(self):
        RTPAchhadRMLot.objects.create(
            description="Old Neoprene", rate=Decimal("300"), todays_stock=Decimal("0"),
            issued=Decimal("40"), is_active=False,
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

    def test_email_body_groups_materials_under_category_headings(self, mailoutbox):
        make_user(email="admin5@ravasco.com", role="admin")
        rubber_lot = HRSRMLot.objects.create(
            description="ISNR-20", basic_rate=Decimal("190"), todays_stock=Decimal("500"), category="Natural Rubber",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=rubber_lot, snapshot_date=TODAY, opening_stock=Decimal("550"), received=0, issued=Decimal("50"),
            todays_stock=Decimal("500"), basic_rate=Decimal("190"), value=Decimal("95000"),
        )
        chem_lot = HRSRMLot.objects.create(
            description="Zinc Oxide", basic_rate=Decimal("117"), todays_stock=Decimal("300"), category="",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=chem_lot, snapshot_date=TODAY, opening_stock=Decimal("310"), received=0, issued=Decimal("10"),
            todays_stock=Decimal("300"), basic_rate=Decimal("117"), value=Decimal("35100"),
        )

        send_daily_consumption_reports()

        [hrs_mail] = [m for m in mailoutbox if "HRS" in m.subject]
        assert "Natural Rubber" in hrs_mail.body
        assert "Uncategorized" in hrs_mail.body
        # The category heading must appear before its own material in the
        # plain-text body - not just present anywhere in the email.
        assert hrs_mail.body.index("Natural Rubber") < hrs_mail.body.index("ISNR-20")
        assert hrs_mail.body.index("Uncategorized") < hrs_mail.body.index("Zinc Oxide")
        # Category pane in the HTML table - a distinct heading row, not just
        # the plain category string appearing somewhere in a material's cell.
        html_alt = hrs_mail.alternatives[0][0]
        assert 'font-weight:700' in html_alt and 'Natural Rubber' in html_alt

    def test_estimate_rows_are_marked_and_explained_in_the_email_body(self, mailoutbox):
        make_user(email="admin4@ravasco.com", role="admin")
        RTPAchhadRMLot.objects.create(
            description="Neoprene", rate=Decimal("300"), todays_stock=Decimal("50"), issued=Decimal("62"),
        )
        # No RTPAchhadRMDailyMovement row for TODAY at all - day-matrix blank,
        # so this row can only appear via the period-to-date fallback.

        send_daily_consumption_reports()

        [achhad_mail] = [m for m in mailoutbox if "RTP-Achhad" in m.subject]
        assert "Neoprene" in achhad_mail.body
        # DecimalField(decimal_places=3) quantizes 62 -> 62.000 on save/reload.
        assert "62.000 (est.)" in achhad_mail.body
        assert "period-to-date running total" in achhad_mail.body
        # A plant report with no fallback rows at all must not carry the
        # explanatory footnote - it would be a confusing non sequitur.
        hrs_mail = [m for m in mailoutbox if "HRS" in m.subject][0]
        assert "period-to-date running total" not in hrs_mail.body

    def test_plain_text_body_carries_the_system_generated_footer(self, mailoutbox):
        """Real bug, found and fixed 2026-09-08: _render_report_email()'s
        html_body already had the "system generated / do not reply" footer
        every other email in this app carries (via email_service.py's
        render_email()), but the plain-text body built alongside it - the
        one an email client actually shows when it can't/won't render HTML -
        was missing it entirely."""
        make_user(email="admin3@ravasco.com", role="admin")
        result = send_daily_consumption_reports()
        assert result["plants_sent"] == 3
        assert all("This is a system generated email. Please do not reply." in m.body for m in mailoutbox)


# ── Monthly report (added 2026-09-08, project owner request) ────────────────
# A fixed reference "today" so month arithmetic (current vs. past period,
# default month selection) is deterministic regardless of when the test
# suite actually runs - unlike the daily tests above, which can safely use
# the real TODAY since they never reason about month boundaries.
MONTH_TODAY = datetime.date(2026, 9, 15)  # mid-September: Sept is "current", Aug is "last completed"


@pytest.mark.django_db
class TestBuildPlantMonthlyReportHRS:
    def test_sums_issued_across_the_whole_month_not_just_one_day(self):
        lot = HRSRMLot.objects.create(description="Natural Rubber", basic_rate=Decimal("120"), todays_stock=Decimal("700"))
        for day, issued in [(3, Decimal("50")), (10, Decimal("30")), (20, Decimal("20"))]:
            HRSRMSnapshot.objects.create(
                stock_lot=lot, snapshot_date=datetime.date(2026, 8, day),
                opening_stock=Decimal("1000"), received=0, issued=issued, todays_stock=Decimal("900"),
                basic_rate=Decimal("120"), value=Decimal("108000"),
            )
        # A day in September (outside the target month) must not be counted.
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=datetime.date(2026, 9, 5),
            opening_stock=Decimal("900"), received=0, issued=Decimal("999"), todays_stock=Decimal("800"),
            basic_rate=Decimal("120"), value=Decimal("96000"),
        )

        report = build_plant_monthly_report("hrs", year=2026, month=8, today=MONTH_TODAY)

        assert report["month"] == "2026-08"
        assert report["monthLabel"] == "August 2026"
        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("100")  # 50 + 30 + 20, not +999
        assert row["isEstimate"] is False

    def test_defaults_to_the_most_recently_completed_month(self):
        lot = HRSRMLot.objects.create(description="Carbon Black", basic_rate=Decimal("50"), todays_stock=Decimal("500"))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=datetime.date(2026, 8, 20),
            opening_stock=Decimal("520"), received=0, issued=Decimal("20"), todays_stock=Decimal("500"),
            basic_rate=Decimal("50"), value=Decimal("25000"),
        )

        report = build_plant_monthly_report("hrs", today=MONTH_TODAY)  # no year/month given

        assert report["month"] == "2026-08"
        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("20")

    def test_inactive_lot_is_excluded(self):
        lot = HRSRMLot.objects.create(
            description="Old Lot", basic_rate=Decimal("10"), todays_stock=Decimal("0"), is_active=False,
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=datetime.date(2026, 8, 10),
            opening_stock=Decimal("10"), received=0, issued=Decimal("10"), todays_stock=Decimal("0"),
            basic_rate=Decimal("10"), value=Decimal("0"),
        )

        report = build_plant_monthly_report("hrs", year=2026, month=8, today=MONTH_TODAY)

        assert report["rows"] == []


@pytest.mark.django_db
class TestBuildPlantMonthlyReportAchhad:
    def test_sums_daily_movement_rows_across_the_month(self):
        lot = RTPAchhadRMLot.objects.create(description="Butyl Rubber", rate=Decimal("200"), todays_stock=Decimal("300"))
        for day, issued in [(4, Decimal("15")), (18, Decimal("25"))]:
            RTPAchhadRMDailyMovement.objects.create(
                stock_lot=lot, movement_date=datetime.date(2026, 8, day), received=Decimal("0"), issued=issued,
            )

        report = build_plant_monthly_report("achhad", year=2026, month=8, today=MONTH_TODAY)

        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("40")
        assert row["isEstimate"] is False

    def test_fallback_applies_for_the_current_still_open_month(self):
        """Same reasoning as the daily report's own fallback
        (_month_issued_rows()'s docstring) - a lot with zero dated rows for
        THIS month so far can reasonably fall back to the live period-to-
        date `issued` column, since that column genuinely still reflects
        this same still-open period."""
        RTPAchhadRMLot.objects.create(
            description="Neoprene", rate=Decimal("300"), todays_stock=Decimal("50"), issued=Decimal("62"),
        )
        # No RTPAchhadRMDailyMovement rows in September at all.

        report = build_plant_monthly_report("achhad", year=2026, month=9, today=MONTH_TODAY)

        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("62")
        assert row["isEstimate"] is True

    def test_fallback_never_applies_for_an_already_closed_past_month(self):
        """The critical safety property: the live `issued` column reflects
        THIS month (September, per MONTH_TODAY) by the time this test's
        "today" has arrived - using it as a stand-in for August (already
        closed) would be flatly wrong, not just imprecise. A lot with no
        August RTPAchhadRMDailyMovement rows must be silently excluded from
        the August report, never estimated from the current live column."""
        RTPAchhadRMLot.objects.create(
            description="Hypalon", rate=Decimal("400"), todays_stock=Decimal("20"), issued=Decimal("9999"),
        )
        # No RTPAchhadRMDailyMovement rows in August at all either.

        report = build_plant_monthly_report("achhad", year=2026, month=8, today=MONTH_TODAY)

        assert report["rows"] == []


@pytest.mark.django_db
class TestSendMonthlyConsumptionReports:
    def test_no_admins_sends_nothing(self):
        result = send_monthly_consumption_reports(year=2026, month=8)
        assert result["plants_sent"] == 0
        assert result["admins_notified"] == 0

    def test_sends_one_email_per_plant_with_monthly_subject(self, mailoutbox):
        make_user(email="admin6@ravasco.com", role="admin")
        lot = HRSRMLot.objects.create(description="Silica", basic_rate=Decimal("60"), todays_stock=Decimal("40"))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=datetime.date(2026, 8, 15),
            opening_stock=Decimal("50"), received=0, issued=Decimal("10"), todays_stock=Decimal("40"),
            basic_rate=Decimal("60"), value=Decimal("2400"),
        )

        result = send_monthly_consumption_reports(year=2026, month=8)

        assert result["month"] == "2026-08"
        assert result["plants_sent"] == 3
        assert len(mailoutbox) == 3
        assert all("(Monthly)" in m.subject and "August 2026" in m.subject for m in mailoutbox)

    def test_email_body_groups_materials_under_category_headings(self, mailoutbox):
        make_user(email="admin7@ravasco.com", role="admin")
        lot = HRSRMLot.objects.create(
            description="ISNR-20", basic_rate=Decimal("190"), todays_stock=Decimal("500"), category="Natural Rubber",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=datetime.date(2026, 8, 15),
            opening_stock=Decimal("550"), received=0, issued=Decimal("50"), todays_stock=Decimal("500"),
            basic_rate=Decimal("190"), value=Decimal("95000"),
        )

        send_monthly_consumption_reports(year=2026, month=8)

        [hrs_mail] = [m for m in mailoutbox if "HRS" in m.subject]
        assert "Natural Rubber" in hrs_mail.body
        assert hrs_mail.body.index("Natural Rubber") < hrs_mail.body.index("ISNR-20")
        assert "Issued This Month" in hrs_mail.alternatives[0][0]
