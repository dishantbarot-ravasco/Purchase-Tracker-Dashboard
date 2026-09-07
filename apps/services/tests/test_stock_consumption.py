"""
Unit tests for the pure consumption-rate/days-of-cover calculation in
apps/services/stock_consumption.py (the Purchase Tracker Dashboard's
"Days-Left Engine") - same pure-function, no-DB convention as
test_matching.py (see that file's own module docstring): consumption_stats()
takes plain tuples, so nothing here touches Django or Postgres.
"""
import datetime
from decimal import Decimal

from apps.services.stock_consumption import consumption_stats

D = datetime.date


def _points(rows):
    """rows: list of (day_offset, stock, received, issued) - day_offset is
    days since 2026-01-01, purely to keep test fixtures terse."""
    base = D(2026, 1, 1)
    return [(base + datetime.timedelta(days=off), Decimal(str(stock)), r, i) for off, stock, r, i in rows]


class TestSteadyDrawdown:
    def test_returns_exact_hand_computed_rate(self):
        # 1000 -> 900 -> 800 -> 700, 1 day apart: 100/day exactly.
        points = _points([(0, 1000, None, None), (1, 900, None, None), (2, 800, None, None), (3, 700, None, None)])
        stats = consumption_stats(points)
        assert stats["avgDaily"] == 100.0
        assert stats["daysLeft"] == 7.0
        assert stats["intervalsUsed"] == 3
        assert stats["receiptIntervals"] == 0

    def test_a_receipt_near_the_end_of_the_window_is_excluded_not_zeroed(self):
        # Same shape as the build plan's own worked chart: a multi-day
        # steady drawdown followed by one receipt that jumps stock back up.
        base = D(2026, 8, 11)
        rows = [
            (base, Decimal("4200")),
            (base + datetime.timedelta(days=3), Decimal("3750")),
            (base + datetime.timedelta(days=6), Decimal("3300")),
            (base + datetime.timedelta(days=8), Decimal("2960")),
            (base + datetime.timedelta(days=12), Decimal("6300")),  # receipt
        ]
        points = [(d, s, None, None) for d, s in rows]
        stats = consumption_stats(points)
        # consumed = (4200-3750)+(3750-3300)+(3300-2960) = 1240 over 3+3+2=8 days -> 155/day
        assert stats["intervalsUsed"] == 3
        assert stats["receiptIntervals"] == 1
        assert stats["avgDaily"] == 155.0
        assert stats["daysLeft"] == 6300 / 155.0


class TestReceiptExclusion:
    def test_mid_window_receipt_is_excluded_not_zeroed(self):
        # Without exclusion, treating the receipt interval as 0 consumption
        # would pull the average down (more days, same total consumed).
        points = _points([
            (0, 1000, None, None),
            (1, 900, None, None),   # -100
            (2, 1200, None, None),  # receipt, excluded
            (3, 1100, None, None),  # -100
        ])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 2
        assert stats["avgDaily"] == 100.0  # not (100 / 3) ~= 33.3


class TestGapHandling:
    def test_a_20_day_gap_is_skipped_entirely(self):
        points = _points([(0, 1000, None, None), (20, 500, None, None)])
        stats = consumption_stats(points)
        assert stats["intervalsUsed"] == 0
        assert stats["avgDaily"] is None
        assert stats["daysLeft"] is None

    def test_a_7_day_gap_is_still_counted(self):
        points = _points([(0, 1000, None, None), (7, 300, None, None)])
        stats = consumption_stats(points)
        assert stats["intervalsUsed"] == 1
        assert stats["avgDaily"] == 100.0


class TestNoMovement:
    def test_zero_movement_yields_days_left_none(self):
        points = _points([(0, 1000, None, None), (1, 1000, None, None), (2, 1000, None, None)])
        stats = consumption_stats(points)
        assert stats["avgDaily"] is None
        assert stats["daysLeft"] is None


class TestConfidenceBands:
    def _stats_for(self, history_days, intervals_used):
        # Steady 1-unit-per-interval drawdown (never a receipt), with gaps
        # distributed as evenly as possible across `intervals_used`
        # intervals so they sum to exactly `history_days` - lets each band
        # boundary test pick history_days/intervals_used independently
        # instead of them being forced equal.
        base, rem = divmod(history_days, intervals_used)
        gaps = [base + 1] * rem + [base] * (intervals_used - rem)
        offsets = [0]
        for g in gaps:
            offsets.append(offsets[-1] + g)
        rows = [(off, 1000 - i, None, None) for i, off in enumerate(offsets)]
        return consumption_stats(_points(rows))

    def test_high_at_exact_boundary(self):
        assert self._stats_for(14, 5)["confidence"] == "high"

    def test_just_under_high_boundary_is_medium(self):
        assert self._stats_for(13, 5)["confidence"] == "medium"

    def test_medium_at_exact_boundary(self):
        assert self._stats_for(7, 3)["confidence"] == "medium"

    def test_just_under_medium_boundary_is_low(self):
        assert self._stats_for(6, 3)["confidence"] == "low"

    def test_low_at_exact_boundary(self):
        assert self._stats_for(2, 1)["confidence"] == "low"

    def test_just_under_low_boundary_is_none(self):
        assert self._stats_for(1, 1)["confidence"] == "none"

    def test_two_days_of_history_still_returns_a_number_banded_low(self):
        points = _points([(0, 1000, None, None), (2, 900, None, None)])
        stats = consumption_stats(points)
        assert stats["avgDaily"] == 50.0
        assert stats["daysLeft"] is not None
        assert stats["confidence"] == "low"

    def test_a_single_point_is_none_confidence_no_crash(self):
        points = _points([(0, 1000, None, None)])
        stats = consumption_stats(points)
        assert stats["confidence"] == "none"
        assert stats["daysLeft"] is None
        assert stats["avgDaily"] is None

    def test_no_points_is_none_confidence_no_crash(self):
        stats = consumption_stats([])
        assert stats["confidence"] == "none"
        assert stats["daysLeft"] is None


class TestIssuedCrossCheck:
    def test_agreeing_cumulative_issued_reports_true(self):
        # Stock drawdown: 100/day. Cumulative issued also climbs ~100/day.
        points = [
            (D(2026, 1, 1), Decimal("1000"), None, Decimal("0")),
            (D(2026, 1, 2), Decimal("900"), None, Decimal("100")),
            (D(2026, 1, 3), Decimal("800"), None, Decimal("205")),
        ]
        stats = consumption_stats(points)
        assert stats["estimatesAgree"] is True

    def test_disagreeing_cumulative_issued_reports_false(self):
        points = [
            (D(2026, 1, 1), Decimal("1000"), None, Decimal("0")),
            (D(2026, 1, 2), Decimal("900"), None, Decimal("10")),
            (D(2026, 1, 3), Decimal("800"), None, Decimal("20")),
        ]
        stats = consumption_stats(points)
        assert stats["estimatesAgree"] is False

    def test_non_cumulative_issued_reports_none(self):
        # issued drops mid-window (e.g. HRS's REC/ISSUE columns clearing
        # once allocated) - not a valid cross-check signal.
        points = [
            (D(2026, 1, 1), Decimal("1000"), None, Decimal("50")),
            (D(2026, 1, 2), Decimal("900"), None, Decimal("0")),
            (D(2026, 1, 3), Decimal("800"), None, Decimal("0")),
        ]
        stats = consumption_stats(points)
        assert stats["estimatesAgree"] is None

    def test_missing_issued_reports_none(self):
        points = _points([(0, 1000, None, None), (1, 900, None, None)])
        stats = consumption_stats(points)
        assert stats["estimatesAgree"] is None
