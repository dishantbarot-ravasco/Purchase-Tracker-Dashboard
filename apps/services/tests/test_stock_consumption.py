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


class TestReceiptDayConsumptionRecovery:
    """A receipt day (stock rose overall) can still hide real same-day
    consumption if `received` is populated and genuinely exceeds the rise -
    added 2026-09-10, project owner: "if issue is having mistake then
    received can be used along with todays stock". Confirmed against real
    synced HRS data before building this: `received` reads 0 on the large
    majority of real receipt days (nothing recoverable then, correctly
    still skipped), but on the days it IS populated and doesn't match the
    rise alone, that gap is real consumption that used to be silently
    thrown away entirely."""

    def test_a_receipt_day_with_no_received_figure_is_still_skipped(self):
        """The exact scenario asked about: yesterday=100, today=200, no
        `received` value recorded at all - nothing to recover from, must
        behave exactly as before (skipped, not zeroed)."""
        points = _points([(0, 100, None, None), (1, 200, None, None), (2, 150, None, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 1  # only the second interval (200->150)
        assert stats["avgDaily"] == 50.0

    def test_received_exactly_explaining_the_rise_recovers_nothing(self):
        """received=100 fully accounts for a 100->200 rise on its own -
        zero implied hidden consumption, so nothing should be added."""
        points = _points([(0, 100, None, None), (1, 200, 100, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 0
        assert stats["avgDaily"] is None

    def test_received_exceeding_the_rise_recovers_the_real_hidden_consumption(self):
        """Real case confirmed against live HRS data: stock rose 4,350 ->
        25,000, but received was logged as 25,000 - meaning 4,350 units
        were ALSO consumed the same day, hidden behind the net rise."""
        points = _points([(0, 4350, None, None), (1, 25000, 25000, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 1
        assert stats["avgDaily"] == 4350.0
        assert stats["daysLeft"] == 25000 / 4350.0

    def test_received_less_than_the_rise_is_not_treated_as_negative_consumption(self):
        """received=50 on a 100->200 rise doesn't fully explain it (the
        other 50 is unexplained, not evidence of consumption) - must not
        produce a negative/nonsensical implied consumption figure."""
        points = _points([(0, 100, None, None), (1, 200, 50, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 0
        assert stats["avgDaily"] is None

    def test_recovered_consumption_combines_correctly_with_ordinary_drawdown_intervals(self):
        """A mix of an ordinary consumption interval and a recovered
        receipt-day interval must average together correctly, not just
        work in isolation."""
        points = _points([
            (0, 1000, None, None),
            (1, 900, None, None),        # ordinary: -100 over 1 day
            (2, 1400, 600, None),        # receipt: rise=500, received=600 -> 100 hidden consumption over 1 day
        ])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 1
        assert stats["intervalsUsed"] == 2
        # consumed = 100 + 100 = 200 over 1+1 = 2 days -> 100/day
        assert stats["avgDaily"] == 100.0


class TestReceiptMaskedConsumptionWithoutChangingNetDirection:
    """A bigger, more common gap than the receipt-day case above - found
    while answering the project owner's own follow-up question about
    `received`, 2026-09-10, then confirmed against real data before fixing:
    a receipt can land the SAME day as heavy consumption WITHOUT the net
    balance ever rising - the interval still looks like ordinary
    consumption (delta >= 0), so the old code trusted `delta` alone and
    never even looked at `received` for this branch. Real HRS example
    found this session: balance dropped by only 1,040 net, but 5,040 was
    also received that day - real consumption was 6,080, not 1,040."""

    def test_a_receipt_alongside_heavy_consumption_still_showing_a_net_drop_is_recovered(self):
        # Real case: stock0=8040, stock1=7000 (net drop of 1,040), but
        # received=5040 that day -> real consumption = 1040 + 5040 = 6080.
        points = _points([(0, 8040, None, None), (1, 7000, 5040, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 0  # net direction never reversed - not a "receipt interval"
        assert stats["intervalsUsed"] == 1
        assert stats["avgDaily"] == 6080.0
        assert stats["daysLeft"] == 7000 / 6080.0

    def test_a_receipt_alongside_consumption_that_exactly_offsets_it_is_recovered_even_though_stock_is_flat(self):
        """Real case confirmed this session: stock stayed EXACTLY flat
        (25000 -> 25000) with received=25000 logged - meaning the entire
        25000 received was also consumed the same day. Delta alone (0)
        would say "nothing happened"; that's wrong."""
        points = _points([(0, 25000, None, None), (1, 25000, 25000, None)])
        stats = consumption_stats(points)
        assert stats["receiptIntervals"] == 0
        assert stats["intervalsUsed"] == 1
        assert stats["avgDaily"] == 25000.0

    def test_an_ordinary_drop_with_no_received_figure_is_unaffected(self):
        """No `received` logged at all on a normal consumption day - must
        behave exactly as it always did (no regression from this fix)."""
        points = _points([(0, 1000, None, None), (1, 900, None, None)])
        stats = consumption_stats(points)
        assert stats["intervalsUsed"] == 1
        assert stats["avgDaily"] == 100.0

    def test_a_received_figure_that_stays_frozen_across_snapshots_is_not_double_counted(self):
        """Real bug found while verifying against live HRS data (2026-09-10):
        `received` can stay frozen at the same nonzero value across several
        consecutive snapshot rows instead of resetting to 0 once "used" -
        found a real lot where 7,487 appeared unchanged on three snapshots
        in a row. A naive `delta + received` on every interval would count
        that one receipt three times over. It must only be added on the
        ONE interval where it actually first appears (changed from the
        previous row's own reading) - every later interval where it's
        merely still visible, unchanged, must add 0."""
        points = _points([
            (0, 3100, 0, None),
            (1, 2950, 0, None),        # ordinary: -150... wait direction: 3100->2950 is -150 drop = 150 consumed
            (2, 10187, 7487, None),    # receipt: rise, received=7487 first appears -> counted once
            (5, 10187, 7487, None),    # flat, received UNCHANGED from previous row -> must NOT be recounted
            (6, 7000, 7487, None),     # ordinary drop, received STILL unchanged -> must NOT be recounted again
        ])
        stats = consumption_stats(points)
        # interval1 (3100->2950): ordinary drop of 150, received 0->0 (no new event) => 150
        # interval2 (2950->10187): rise of 7237, received 0->7487 (NEW) => implied = -7237+7487 = 250
        # interval3 (10187->10187): flat, received 7487->7487 (unchanged, NOT a new event) => 0
        # interval4 (10187->7000): ordinary drop of 3187, received 7487->7487 (unchanged) => 3187
        # total consumed = 150 + 250 + 0 + 3187 = 3587, over 1+1+3+1 = 6 days
        assert stats["intervalsUsed"] == 4
        assert stats["avgDaily"] == 3587 / 6

    def test_a_perfectly_flat_day_with_no_received_figure_still_dilutes_the_average(self):
        """A real "nothing happened" day (no drop, no logged receipt) must
        still count as a zero-consumption interval that dilutes the
        average - not be skipped, which would inflate avgDaily by
        pretending fewer days were observed than really were."""
        points = _points([(0, 1000, None, None), (1, 1000, None, None), (2, 900, None, None)])
        stats = consumption_stats(points)
        assert stats["intervalsUsed"] == 2
        # consumed = 0 + 100 = 100 over 1+1 = 2 days -> 50/day, not 100/day
        assert stats["avgDaily"] == 50.0


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
