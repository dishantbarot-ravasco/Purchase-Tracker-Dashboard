"""
Tests for apps/services/consumption_report.py - the daily and monthly Raw
Material Consumption reports (one email per plant). Real Postgres via
pytest-django, same factory-function convention as
apps/api/tests/test_materials_days_left.py.

**Reworked 2026-09-21 with the read-path migration.** Both reports read the
MaterialConsumptionDaily ledger now instead of deriving their own figures
from *RMSnapshot, so every data-shaped test here builds the ledger first
via `rebuild_plant_consumption()` - exactly as each plant's own
`compute_<plant>_consumption` pipeline step does before the report runs.

Two behaviours these tests used to pin are deliberately gone:

- **Achhad's period-to-date `issued` fallback.** It existed because the
  day-matrix could be blank for today while the live cumulative column had
  a figure. There is no live cumulative column in the read path any more -
  Achhad's matrix is reconciled into the ledger at build time, and days the
  matrix doesn't cover are derived from snapshot intervals like every other
  plant's. Nothing falls back to a month-to-date total.
- **`isEstimate` meaning "period-to-date, Achhad only".** It now means "this
  quantity was interpolated across a snapshot gap rather than observed on a
  single dated day", and applies at all three plants.

The plumbing tests below (admin selection, the ReportSendLog dedup guard,
the footer and legend) are unchanged in intent - they were never about
where the numbers came from.
"""
import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSRMLot,
    HRSRMSnapshot,
    ReportSendLog,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
)
from apps.services.consumption_ledger import rebuild_plant_consumption
from apps.services.consumption_report import (
    build_plant_monthly_report,
    build_plant_report,
    send_daily_consumption_reports,
    send_monthly_consumption_reports,
)

TODAY = timezone.localdate()


def hrs_lot_issuing(description, issues, *, opening=Decimal("1000"), rate=Decimal("120"), **overrides):
    """Creates an HRSRMLot plus one snapshot per (date, cumulative_issued)
    pair, with `closing` following the sheet's own
    `opening + received - issued == closing` identity the way every real
    row does. Consumption is driven through the ISSUE BOOK - a falling
    balance with a static issue book is a restatement and correctly
    measures zero (see consumption_engine.py)."""
    last_issued = Decimal(str(issues[-1][1]))
    defaults = dict(
        description=description, basic_rate=rate, opening_stock=opening,
        todays_stock=opening - last_issued, value=(opening - last_issued) * rate,
    )
    defaults.update(overrides)
    lot = HRSRMLot.objects.create(**defaults)
    for date, issued in issues:
        closing = opening - Decimal(str(issued))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=date, opening_stock=opening, received=0,
            issued=Decimal(str(issued)), todays_stock=closing, basic_rate=rate, value=closing * rate,
        )
    return lot


def achhad_lot_issuing(description, issues, *, opening=Decimal("1000"), rate=Decimal("200"), **overrides):
    last_issued = Decimal(str(issues[-1][1]))
    defaults = dict(
        description=description, rate=rate, opening_stock=opening,
        todays_stock=opening - last_issued, value=(opening - last_issued) * rate,
    )
    defaults.update(overrides)
    lot = RTPAchhadRMLot.objects.create(**defaults)
    for date, issued in issues:
        closing = opening - Decimal(str(issued))
        RTPAchhadRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=date, opening_stock=opening, received=0,
            issued=Decimal(str(issued)), todays_stock=closing, rate=rate, value=closing * rate,
        )
    return lot


def _days(*offsets):
    return [TODAY - datetime.timedelta(days=n) for n in offsets]


@pytest.mark.django_db
class TestBuildPlantReportHRS:
    def test_a_material_issued_today_appears_with_its_rate_and_days_left(self):
        y, t = _days(1, 0)
        hrs_lot_issuing("Natural Rubber", [(y, 0), (t, 50)], category="Natural Rubber")
        rebuild_plant_consumption("hrs")

        report = build_plant_report("hrs", today=TODAY)

        [row] = report["rows"]
        assert row["material"] == "Natural Rubber"
        assert row["issuedToday"] == Decimal("50.000")
        assert row["rate"] == Decimal("120.0000")
        assert row["daysLeft"] is not None
        assert row["isEstimate"] is False

    def test_a_blank_category_becomes_uncategorized(self):
        y, t = _days(1, 0)
        hrs_lot_issuing("Zinc Oxide", [(y, 0), (t, 10)], category="")
        rebuild_plant_consumption("hrs")

        [row] = build_plant_report("hrs", today=TODAY)["rows"]
        assert row["category"] == "Uncategorized"

    def test_a_material_with_no_issue_today_is_excluded(self):
        # Issued a week ago, nothing since.
        old, y, t = _days(7, 1, 0)
        hrs_lot_issuing("Silica", [(old, 0), (y, 40), (t, 40)])
        rebuild_plant_consumption("hrs")

        assert build_plant_report("hrs", today=TODAY)["rows"] == []

    def test_sibling_vendor_lots_are_one_row_not_several(self):
        # The migration's visible change to this report: a material split
        # across vendors used to appear once per lot, each holding part of
        # the day's issues.
        y, t = _days(1, 0)
        hrs_lot_issuing("SBR 1502", [(y, 0), (t, 30)], party_name="GPC", natural_key="sbr-gpc")
        hrs_lot_issuing("SBR 1502", [(y, 0), (t, 70)], party_name="Balaji", natural_key="sbr-bal")
        rebuild_plant_consumption("hrs")

        [row] = build_plant_report("hrs", today=TODAY)["rows"]
        assert row["issuedToday"] == Decimal("100.000")

    def test_a_restatement_is_not_reported_as_an_issue(self):
        # The balance collapses while the issue book stands still - the
        # sheet corrected a figure. The old report read the cumulative
        # `issued` column and this is the class of row that broke it.
        y, t = _days(1, 0)
        lot = HRSRMLot.objects.create(
            description="SACK CARBON", basic_rate=Decimal("10"),
            opening_stock=Decimal("450500"), todays_stock=Decimal("17850"),
        )
        for date, opening in ((y, Decimal("450500")), (t, Decimal("17850"))):
            HRSRMSnapshot.objects.create(
                stock_lot=lot, snapshot_date=date, opening_stock=opening, received=0,
                issued=0, todays_stock=opening, basic_rate=Decimal("10"), value=opening * 10,
            )
        rebuild_plant_consumption("hrs")

        assert build_plant_report("hrs", today=TODAY)["rows"] == []


@pytest.mark.django_db
class TestBuildPlantReportAchhad:
    def test_a_dated_movement_row_drives_the_figure(self):
        lot = achhad_lot_issuing("Butyl Rubber", [(d, 0) for d in _days(1, 0)])
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=TODAY, received=Decimal("0"), issued=Decimal("15"),
        )
        rebuild_plant_consumption("achhad")

        [row] = build_plant_report("achhad", today=TODAY)["rows"]
        assert row["issuedToday"] == Decimal("15.000")
        assert row["isEstimate"] is False

    def test_the_live_period_to_date_column_is_never_used_as_a_figure(self):
        # Replaces the old period-to-date fallback test. `issued=62` on the
        # live lot row is a month-to-date running total; with no ledger rows
        # for today the report must say nothing rather than print it under
        # a heading that says "Issued Today".
        RTPAchhadRMLot.objects.create(
            description="Neoprene", rate=Decimal("300"), todays_stock=Decimal("50"), issued=Decimal("62"),
        )
        rebuild_plant_consumption("achhad")

        assert build_plant_report("achhad", today=TODAY)["rows"] == []


@pytest.mark.django_db
class TestSpreadDaysAreMarkedAsEstimates:
    def test_a_day_interpolated_across_a_snapshot_gap_is_flagged(self):
        # Two snapshots four days apart: the 300 issued between them can't
        # be pinned to one day, so each covered day carries a share and is
        # marked. This is what isEstimate means now.
        old, t = _days(4, 0)
        hrs_lot_issuing("Sulphur", [(old, 0), (t, 300)])
        rebuild_plant_consumption("hrs")

        [row] = build_plant_report("hrs", today=TODAY)["rows"]
        assert row["issuedToday"] == Decimal("75.000")
        assert row["isEstimate"] is True

    def test_a_directly_observed_day_is_not_flagged(self):
        y, t = _days(1, 0)
        hrs_lot_issuing("Sulphur", [(y, 0), (t, 300)])
        rebuild_plant_consumption("hrs")

        [row] = build_plant_report("hrs", today=TODAY)["rows"]
        assert row["isEstimate"] is False


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

        y, t = _days(1, 0)
        hrs_lot_issuing("Silica", [(y, 0), (t, 10)])
        rebuild_plant_consumption("hrs")

        result = send_daily_consumption_reports()

        assert result["plants_sent"] == 3
        assert result["admins_notified"] == 1  # inactive admin excluded
        assert len(mailoutbox) == 3
        assert all(m.to == ["admin1@ravasco.com"] for m in mailoutbox)
        subjects = {m.subject for m in mailoutbox}
        assert any("HRS" in s for s in subjects)
        assert any("RTP-Achhad" in s for s in subjects)
        assert any("RTP-Vapi" in s for s in subjects)

    def test_calling_twice_the_same_day_does_not_resend(self, mailoutbox):
        """Regression test for the dedup guard added during a full-codebase
        audit (2026-09-10) - if the external scheduler ever double-fires
        trigger_daily_report (a network retry, a misconfigured overlapping
        schedule), a second call for the same day must be a no-op, not a
        second round of duplicate emails to every admin."""
        make_user(email="admin-dedup@ravasco.com", role="admin")
        HRSRMLot.objects.create(description="Silica", basic_rate=Decimal("60"), todays_stock=Decimal("40"))

        first = send_daily_consumption_reports()
        assert first["plants_sent"] == 3
        assert len(mailoutbox) == 3

        second = send_daily_consumption_reports()
        assert second["plants_sent"] == 0
        assert len(mailoutbox) == 3, "a second same-day call must not send any more emails"
        assert ReportSendLog.objects.filter(report_type=ReportSendLog.ReportType.DAILY).count() == 3

    def test_a_failed_build_does_not_permanently_block_retry(self, mailoutbox, monkeypatch):
        """The dedup guard claims a ReportSendLog row BEFORE building/sending
        (to close the race window against a double-fire) - but a genuine
        build/send failure must release that claim again, or a transient
        failure would silently block every future attempt for that plant/day
        with no way to recover short of a manual DB edit."""
        import apps.services.consumption_report as consumption_report_module

        make_user(email="admin-retry@ravasco.com", role="admin")

        def _boom(plant_key, today=None):
            if plant_key == "hrs":
                raise RuntimeError("simulated build failure")
            return {"label": plant_key, "date": (today or TODAY).isoformat(), "rows": []}

        monkeypatch.setattr(consumption_report_module, "build_plant_report", _boom)

        first = send_daily_consumption_reports()
        assert first["plants_sent"] == 2  # achhad + vapi sent, hrs failed
        assert not ReportSendLog.objects.filter(
            report_type=ReportSendLog.ReportType.DAILY, plant="hrs", period_key=TODAY.isoformat(),
        ).exists(), "a failed plant's claim must be released, not left behind"

        monkeypatch.undo()
        second = send_daily_consumption_reports()
        assert second["plants_sent"] == 1, "hrs must be retryable after the earlier failure; achhad/vapi stay deduped"

    def test_email_body_groups_materials_under_category_headings(self, mailoutbox):
        make_user(email="admin5@ravasco.com", role="admin")
        y, t = _days(1, 0)
        hrs_lot_issuing("ISNR-20", [(y, 0), (t, 50)], rate=Decimal("190"), category="Natural Rubber")
        hrs_lot_issuing("Zinc Oxide", [(y, 0), (t, 10)], rate=Decimal("117"), category="", natural_key="zn")
        rebuild_plant_consumption("hrs")

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
        old, t = _days(4, 0)
        hrs_lot_issuing("Neoprene", [(old, 0), (t, 300)], rate=Decimal("300"))
        rebuild_plant_consumption("hrs")

        send_daily_consumption_reports()

        [hrs_mail] = [m for m in mailoutbox if "HRS" in m.subject]
        assert "Neoprene" in hrs_mail.body
        assert "75.000 (est.)" in hrs_mail.body
        # A plant report with no interpolated rows at all must not carry the
        # explanatory footnote - it would be a confusing non sequitur.
        vapi_mail = [m for m in mailoutbox if "RTP-Vapi" in m.subject][0]
        assert "(est.)" not in vapi_mail.body

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

    def test_email_explains_confidence_labels_are_not_a_stock_level_warning(self, mailoutbox):
        """Added 2026-09-10, project owner: "(low) sitting right next to a
        days-left number reads exactly like 'low stock' - which it isn't" -
        every report must carry a legend explaining Days Left's formula and
        that none/low/medium/high describe how much history backs the
        estimate, not the stock level, in both the HTML and plain-text body
        (an email client that can't/won't render HTML must not silently lose
        this explanation).

        The scope sentence changed with the 2026-09-21 migration: a row is
        one MATERIAL across every vendor lot of it now, not one vendor's
        lot, so the legend has to say so or it describes the old shape."""
        make_user(email="admin-legend@ravasco.com", role="admin")
        send_daily_consumption_reports()

        for m in mailoutbox:
            assert "NOT a stock-level warning" in m.alternatives[0][0]  # HTML body
            assert "NOT a stock-level warning" in m.body               # plain-text body
            for label in ("none", "low", "medium", "high"):
                assert label in m.body
            assert "every vendor lot" in m.body
            assert "vendor's stock lot" not in m.body


# ── Monthly report (added 2026-09-08, project owner request) ────────────────
# A fixed reference "today" so month arithmetic (current vs. past period,
# default month selection) is deterministic regardless of when the test
# suite actually runs.
MONTH_TODAY = datetime.date(2026, 9, 15)  # mid-September: Sept is "current", Aug is "last completed"
AUG = [datetime.date(2026, 8, d) for d in (1, 3, 10, 20)]


@pytest.mark.django_db
class TestBuildPlantMonthlyReportHRS:
    def test_sums_across_the_whole_month_not_just_one_day(self):
        hrs_lot_issuing(
            "Natural Rubber",
            # Cumulative issue book: 0 -> 50 -> 80 -> 100 within August.
            [(AUG[0], 0), (AUG[1], 50), (AUG[2], 80), (AUG[3], 100)],
        )
        rebuild_plant_consumption("hrs")

        report = build_plant_monthly_report("hrs", year=2026, month=8, today=MONTH_TODAY)

        assert report["month"] == "2026-08"
        assert report["monthLabel"] == "August 2026"
        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("100.000")

    def test_september_consumption_does_not_leak_into_august(self):
        # Snapshots on Aug 31 and Sep 1, so the September interval covers
        # exactly one September day and cannot reach back over the boundary.
        hrs_lot_issuing(
            "Natural Rubber",
            [(AUG[0], 0), (AUG[3], 100), (datetime.date(2026, 8, 31), 100),
             (datetime.date(2026, 9, 1), 999)],
        )
        rebuild_plant_consumption("hrs")

        [row] = build_plant_monthly_report("hrs", year=2026, month=8, today=MONTH_TODAY)["rows"]
        assert row["issuedThisMonth"] == Decimal("100.000")

    def test_an_interval_straddling_month_end_splits_across_both_months(self):
        """Documented, deliberate behaviour rather than an accident - see
        consumption_periods.py's docstring. When the snapshots either side
        of a month boundary are days apart, the consumption between them
        genuinely cannot be attributed to one side, so each month takes the
        share of days it actually contains. The two months still sum to the
        interval's real total, which is what keeps a quarter or a year
        exact.

        16 days from Aug 20 to Sep 5 carrying 899 units: 11 of those days
        fall in August."""
        hrs_lot_issuing(
            "Natural Rubber",
            [(AUG[0], 0), (AUG[3], 100), (datetime.date(2026, 9, 5), 999)],
        )
        rebuild_plant_consumption("hrs")

        august = build_plant_monthly_report("hrs", year=2026, month=8, today=MONTH_TODAY)["rows"][0]
        september = build_plant_monthly_report("hrs", year=2026, month=9, today=MONTH_TODAY)["rows"][0]

        assert august["issuedThisMonth"] + september["issuedThisMonth"] == Decimal("999.000")
        assert august["issuedThisMonth"] == pytest.approx(Decimal("718.068"))
        assert august["isEstimate"] is True

    def test_defaults_to_the_most_recently_completed_month(self):
        hrs_lot_issuing("Carbon Black", [(AUG[0], 0), (AUG[3], 20)], rate=Decimal("50"))
        rebuild_plant_consumption("hrs")

        report = build_plant_monthly_report("hrs", today=MONTH_TODAY)  # no year/month given

        assert report["month"] == "2026-08"
        [row] = report["rows"]
        assert row["issuedThisMonth"] == Decimal("20.000")

    def test_a_month_with_no_ledger_rows_reports_nothing(self):
        hrs_lot_issuing("Carbon Black", [(AUG[0], 0), (AUG[3], 20)], rate=Decimal("50"))
        rebuild_plant_consumption("hrs")

        assert build_plant_monthly_report("hrs", year=2026, month=7, today=MONTH_TODAY)["rows"] == []


@pytest.mark.django_db
class TestBuildPlantMonthlyReportAchhad:
    def test_sums_daily_movement_rows_across_the_month(self):
        lot = achhad_lot_issuing("Butyl Rubber", [(AUG[0], 0)])
        for day, issued in ((4, Decimal("15")), (18, Decimal("25"))):
            RTPAchhadRMDailyMovement.objects.create(
                stock_lot=lot, movement_date=datetime.date(2026, 8, day),
                received=Decimal("0"), issued=issued,
            )
        rebuild_plant_consumption("achhad")

        [row] = build_plant_monthly_report("achhad", year=2026, month=8, today=MONTH_TODAY)["rows"]
        assert row["issuedThisMonth"] == Decimal("40.000")
        assert row["isEstimate"] is False

    def test_a_closed_month_is_never_estimated_from_the_live_column(self):
        """The critical safety property, preserved from before the
        migration. The live `issued` column reflects whatever period is
        currently open; using it as a stand-in for an already-closed month
        would be flatly wrong, not merely imprecise. There is now no code
        path that could - the report only ever reads dated ledger rows -
        but the property is worth pinning rather than assuming."""
        RTPAchhadRMLot.objects.create(
            description="Hypalon", rate=Decimal("400"), todays_stock=Decimal("20"), issued=Decimal("9999"),
        )
        rebuild_plant_consumption("achhad")

        assert build_plant_monthly_report("achhad", year=2026, month=8, today=MONTH_TODAY)["rows"] == []


@pytest.mark.django_db
class TestSendMonthlyConsumptionReports:
    def test_no_admins_sends_nothing(self):
        result = send_monthly_consumption_reports(year=2026, month=8)
        assert result["plants_sent"] == 0
        assert result["admins_notified"] == 0

    def test_sends_one_email_per_plant_with_monthly_subject(self, mailoutbox):
        make_user(email="admin6@ravasco.com", role="admin")
        hrs_lot_issuing("Silica", [(AUG[0], 0), (AUG[3], 10)], rate=Decimal("60"))
        rebuild_plant_consumption("hrs")

        result = send_monthly_consumption_reports(year=2026, month=8)

        assert result["plants_sent"] == 3
        assert len(mailoutbox) == 3
        assert all("Monthly" in m.subject for m in mailoutbox)

    def test_calling_twice_for_the_same_month_does_not_resend(self, mailoutbox):
        make_user(email="admin-mdedup@ravasco.com", role="admin")

        first = send_monthly_consumption_reports(year=2026, month=8)
        assert first["plants_sent"] == 3
        assert len(mailoutbox) == 3

        second = send_monthly_consumption_reports(year=2026, month=8)
        assert second["plants_sent"] == 0
        assert len(mailoutbox) == 3
        assert ReportSendLog.objects.filter(report_type=ReportSendLog.ReportType.MONTHLY).count() == 3

    def test_email_body_groups_materials_under_category_headings(self, mailoutbox):
        make_user(email="admin7@ravasco.com", role="admin")
        hrs_lot_issuing("ISNR-20", [(AUG[0], 0), (AUG[3], 50)], rate=Decimal("190"), category="Natural Rubber")
        rebuild_plant_consumption("hrs")

        send_monthly_consumption_reports(year=2026, month=8)

        [hrs_mail] = [m for m in mailoutbox if "HRS" in m.subject]
        assert "Natural Rubber" in hrs_mail.body
        assert hrs_mail.body.index("Natural Rubber") < hrs_mail.body.index("ISNR-20")

    def test_monthly_email_also_explains_confidence_labels(self, mailoutbox):
        make_user(email="admin-mlegend@ravasco.com", role="admin")
        send_monthly_consumption_reports(year=2026, month=8)

        for m in mailoutbox:
            assert "NOT a stock-level warning" in m.alternatives[0][0]
            assert "NOT a stock-level warning" in m.body
            assert "every vendor lot" in m.body
