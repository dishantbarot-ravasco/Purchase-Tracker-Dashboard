"""
Unit tests for apps/services/consumption_engine.py - the pure, DB-free core
of the raw-material consumption ledger. Same pure-function convention as
test_stock_consumption.py and test_matching.py: everything here takes plain
Decimals and dates, so nothing touches Django or Postgres.

Several fixtures below are real rows copied out of the live database on
2026-09-21 rather than invented numbers - they are named `*_real_*` and
carry the lot they came from, because the whole point of this engine is
that the invented cases and the real ones disagree about what consumption
means.
"""
import datetime
from decimal import Decimal

import pytest

from apps.services.consumption_engine import (
    BOOKS_DISAGREE,
    CLOSEOUT,
    COUNTED,
    PERIOD_ROLL,
    RESTATEMENT,
    SPREAD,
    ConsumptionPoint,
    allocate_daily,
    daily_from_points,
    intervals_for_lot,
    rate_from_daily,
)

D = datetime.date


def _p(offset, opening, received, issued, closing):
    """A snapshot `offset` days after 2026-09-01, all quantities as Decimals."""
    return ConsumptionPoint(
        D(2026, 9, 1) + datetime.timedelta(days=offset),
        Decimal(str(opening)), Decimal(str(received)), Decimal(str(issued)), Decimal(str(closing)),
    )


class TestOrdinaryConsumption:
    def test_issue_book_delta_is_the_quantity(self):
        # Period opening 1000, issued running 0 -> 100 -> 250.
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 900), _p(2, 1000, 0, 250, 750)]
        intervals = intervals_for_lot(points)
        assert [i.quantity for i in intervals] == [Decimal(100), Decimal(150)]
        assert all(i.classification == COUNTED for i in intervals)

    def test_balance_and_books_agree_so_nothing_is_flagged(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 900)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == interval.balance_quantity == Decimal(100)

    def test_a_receipt_landing_the_same_day_does_not_hide_consumption(self):
        # Received 500 and issued 100 on one day: closing only falls 1000 ->
        # 1400? No - it RISES. The balance alone would read as negative
        # consumption; the issue book reads 100, which is the truth.
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 500, 100, 1400)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(100)
        assert interval.classification == COUNTED

    def test_a_flat_day_contributes_nothing_but_is_not_an_error(self):
        points = [_p(0, 1000, 0, 50, 950), _p(1, 1000, 0, 50, 950)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(0)
        assert interval.classification == COUNTED


class TestCumulativeReceivedIsNotDoubleCounted:
    """The defect this engine replaces: stock_consumption.py added the
    cumulative `received` LEVEL rather than its delta, inflating totals
    1.31x-1.85x across the three plants."""

    def test_real_hrs_silsheet_rubber_second_delivery_is_not_consumption(self):
        # HRS lot 52, 2026-09-07 -> 09-08, verbatim. A second 25,000
        # delivery landed; `issued` did not move, so nothing was consumed.
        # The old engine computed -25,000 + 50,000 = 25,000.
        points = [_p(6, 0, 25000, 11100, 13900), _p(7, 0, 50000, 11100, 38900)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(0)
        assert interval.balance_quantity == Decimal(0)

    def test_two_successive_receipts_each_count_only_their_own_increment(self):
        points = [_p(0, 0, 1000, 0, 1000), _p(1, 0, 3000, 500, 2500), _p(2, 0, 6000, 900, 5100)]
        assert [i.quantity for i in intervals_for_lot(points)] == [Decimal(500), Decimal(400)]

    def test_a_frozen_received_figure_adds_nothing(self):
        # `received` sitting unchanged across snapshots was the case the old
        # engine's `r1 != r0` guard handled; a delta handles it for free.
        points = [_p(0, 0, 7487, 100, 7387), _p(1, 0, 7487, 300, 7187)]
        assert intervals_for_lot(points)[0].quantity == Decimal(200)


class TestRestatement:
    def test_real_hrs_sack_carbon_phantom_is_zero(self):
        # HRS lot 119, 2026-09-03 -> 09-04, verbatim: 450,500 -> 17,850 with
        # `received` and `issued` both 0 either side. The balance method
        # counted 432,650 units - about a third of that plant's entire
        # measured total - from one corrected cell.
        points = [_p(2, 450500, 0, 0, 450500), _p(3, 17850, 0, 0, 17850)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(0)
        assert interval.balance_quantity == Decimal(432650)
        assert interval.classification == RESTATEMENT

    def test_a_restatement_still_counts_toward_the_rate(self):
        # It is not excluded - the issue book's figure is correct, the label
        # exists only so the balance disagreement stays visible.
        points = [_p(2, 450500, 0, 0, 450500), _p(3, 17850, 0, 0, 17850)]
        assert intervals_for_lot(points)[0].counts_toward_rate

    def test_real_hrs_6ppd_restatement_counts_its_genuine_issues(self):
        # HRS lot 53, 2026-09-03 -> 09-04: opening restated 9000 -> 4850
        # while the issue book kept running 350 -> 700. 350 was really
        # issued; the balance would have said 4,500.
        points = [_p(2, 9000, 0, 350, 8650), _p(3, 4850, 0, 700, 4150)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(350)
        assert interval.balance_quantity == Decimal(4500)
        assert interval.classification == RESTATEMENT


class TestPeriodRoll:
    def test_issue_book_resetting_downward_is_excluded(self):
        points = [_p(0, 1000, 0, 800, 200), _p(1, 200, 0, 50, 150)]
        interval = intervals_for_lot(points)[0]
        assert interval.classification == PERIOD_ROLL
        assert not interval.counts_toward_rate

    def test_a_period_roll_still_reports_its_size(self):
        # Excluded, but never silently: the event row must carry a figure.
        points = [_p(0, 1000, 0, 800, 200), _p(1, 200, 0, 50, 150)]
        assert intervals_for_lot(points)[0].quantity == Decimal(50)

    def test_an_opening_change_alone_is_not_a_period_roll(self):
        # This is the trap: "opening changed, so fall back to the balance"
        # gets the restatement case 15x wrong. Only the issue book resetting
        # proves a real roll.
        points = [_p(0, 1000, 0, 100, 900), _p(1, 500, 0, 200, 300)]
        assert intervals_for_lot(points)[0].classification == RESTATEMENT


class TestCloseout:
    def test_a_dormant_lot_zeroed_in_one_step_is_excluded(self):
        # HRS lot 4 (SBR 1502) shape: 75,600 untouched for days, then
        # straight to 0 with the full amount posted as issued.
        points = [_p(0, 75600, 0, 0, 75600), _p(1, 75600, 0, 0, 75600), _p(2, 75600, 0, 75600, 0)]
        intervals = intervals_for_lot(points)
        assert intervals[-1].classification == CLOSEOUT
        assert not intervals[-1].counts_toward_rate

    def test_a_lot_already_being_drawn_down_that_finishes_is_real_consumption(self):
        # The discriminator. This lot was moving, so its last units are a
        # genuine burn, not a write-off.
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 400, 600), _p(2, 1000, 0, 1000, 0)]
        intervals = intervals_for_lot(points)
        assert intervals[-1].classification == COUNTED
        assert intervals[-1].quantity == Decimal(600)

    def test_a_partial_drop_to_zero_below_the_threshold_is_not_a_closeout(self):
        # Closing hits 0, but most of the balance had already gone in an
        # earlier interval - this last step is not the all-at-once
        # signature, and the lot had moved besides.
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 950, 50), _p(2, 1000, 0, 1000, 0)]
        assert intervals_for_lot(points)[-1].classification != CLOSEOUT

    def test_a_lot_that_received_in_the_same_interval_is_not_dormant(self):
        # Received 500 and issued 550, ending at 0, on its very first
        # interval. That is ordinary activity, not a write-off of the
        # opening 100 - and `issued` legitimately exceeds the balance it
        # started from, which is why the received term is needed.
        points = [_p(0, 1000, 0, 0, 100), _p(1, 1000, 500, 550, 0)]
        interval = intervals_for_lot(points)[0]
        assert interval.classification != CLOSEOUT
        assert interval.quantity == Decimal(550)


class TestBooksDisagree:
    def test_broken_sheet_arithmetic_is_flagged_but_still_uses_the_issue_book(self):
        # opening unchanged, yet closing does not follow received/issued.
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 500)]
        interval = intervals_for_lot(points)[0]
        assert interval.classification == BOOKS_DISAGREE
        assert interval.quantity == Decimal(100)
        assert interval.balance_quantity == Decimal(500)

    def test_it_still_counts_toward_the_rate(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 500)]
        assert intervals_for_lot(points)[0].counts_toward_rate


class TestSpreadAcrossGaps:
    def test_a_multi_day_gap_is_kept_not_discarded(self):
        # The old engine dropped any gap over 7 days outright, throwing away
        # 63% of Achhad's measured consumption.
        points = [_p(0, 1000, 0, 0, 1000), _p(10, 1000, 0, 500, 500)]
        interval = intervals_for_lot(points)[0]
        assert interval.quantity == Decimal(500)
        assert interval.classification == SPREAD
        assert interval.counts_toward_rate

    def test_allocation_covers_the_days_after_the_opening_snapshot(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(3, 1000, 0, 300, 700)]
        days = allocate_daily(intervals_for_lot(points)[0])
        assert [d for d, _, _ in days] == [D(2026, 9, 2), D(2026, 9, 3), D(2026, 9, 4)]

    def test_allocation_never_loses_units_to_rounding(self):
        # 100 over 3 days is 33.333 each; the remainder must land on the
        # last day so a monthly rollup stays exact.
        points = [_p(0, 1000, 0, 0, 1000), _p(3, 1000, 0, 100, 900)]
        days = allocate_daily(intervals_for_lot(points)[0])
        assert sum(q for _, q, _ in days) == Decimal(100)

    def test_a_single_day_interval_is_not_marked_spread(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 900)]
        days = allocate_daily(intervals_for_lot(points)[0])
        assert days == [(D(2026, 9, 2), Decimal(100), COUNTED)]

    def test_an_explicit_wider_gap_tolerance_marks_it_counted(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(3, 1000, 0, 300, 700)]
        assert intervals_for_lot(points, max_dated_gap_days=7)[0].classification == COUNTED


class TestMalformedInput:
    def test_unordered_points_are_sorted_not_trusted(self):
        points = [_p(2, 1000, 0, 250, 750), _p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 900)]
        assert [i.quantity for i in intervals_for_lot(points)] == [Decimal(100), Decimal(150)]

    def test_two_points_on_the_same_date_cannot_invent_consumption(self):
        # The old API layer merged two differently-anchored series and
        # produced exactly this.
        points = [_p(0, 1000, 0, 0, 1000), _p(0, 0, 0, 0, 200), _p(1, 1000, 0, 100, 900)]
        assert sum(i.quantity for i in intervals_for_lot(points) if i.counts_toward_rate) == Decimal(100)

    def test_a_single_point_yields_no_intervals(self):
        assert intervals_for_lot([_p(0, 1000, 0, 0, 1000)]) == []

    def test_no_points_is_not_a_crash(self):
        assert intervals_for_lot([]) == []


class TestDailyFromPoints:
    def test_excluded_intervals_come_back_separately(self):
        points = [_p(0, 75600, 0, 0, 75600), _p(1, 75600, 0, 0, 75600), _p(2, 75600, 0, 75600, 0)]
        daily, excluded = daily_from_points(points)
        assert daily == {}
        assert [e.classification for e in excluded] == [CLOSEOUT]

    def test_a_spread_day_overlapping_an_observed_day_takes_the_weaker_quality(self):
        points = [_p(0, 1000, 0, 0, 1000), _p(1, 1000, 0, 100, 900), _p(4, 1000, 0, 400, 600)]
        daily, _ = daily_from_points(points)
        assert daily[D(2026, 9, 2)][1] == COUNTED
        assert daily[D(2026, 9, 3)][1] == SPREAD


class TestRateFromDaily:
    def test_divides_by_the_whole_window_not_by_days_with_data(self):
        # 100 units on one day inside a 10-day window is 10/day, not 100/day.
        daily = {D(2026, 9, 5): Decimal(100)}
        rate = rate_from_daily(daily, window_start=D(2026, 9, 1), window_end=D(2026, 9, 10))
        assert rate == pytest.approx(10.0)

    def test_an_empty_window_is_none_not_zero(self):
        assert rate_from_daily({}, window_start=D(2026, 9, 1), window_end=D(2026, 9, 10)) is None

    def test_days_outside_the_window_are_excluded(self):
        daily = {D(2026, 8, 1): Decimal(1000), D(2026, 9, 5): Decimal(100)}
        rate = rate_from_daily(daily, window_start=D(2026, 9, 1), window_end=D(2026, 9, 10))
        assert rate == pytest.approx(10.0)
