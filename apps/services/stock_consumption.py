"""
Pure, dependency-free consumption-rate / days-of-cover calculation for a
stock lot's snapshot history (Purchase Tracker Dashboard's "Days-Left
Engine"). Takes plain (date, todays_stock, received, issued) tuples so the
whole algorithm unit-tests without a database - see
apps/services/tests/test_stock_consumption.py, same convention as
apps/services/matching.py's own pure scoring helpers.

Why this never reads `received`/`issued` for the primary rate: those columns
mean a different thing at each of HRS/RTP-Achhad/RTP-Vapi's Stock sheets -
RTP-Achhad's `issued` is a period-to-date summary that resets each period;
HRS's/RTP-Vapi's `issued`/`received` are formula cells pulled from a
separate Receipt/Issue tab, and HRS's own `received` reads 0 for nearly
every real lot (see CLAUDE.md's MIR<->Stock matching notes - it evidently
clears once allocated rather than holding a running total). The one field
that means the same thing everywhere is the closing balance, `todays_stock`
- so the only portable consumption signal is the day-over-day drawdown
between consecutive snapshots of it. `issued` is still read, but only for
`_issued_cross_check()` below, a data-quality signal that never overrides
the primary number.
"""
import datetime
from decimal import Decimal
from typing import NamedTuple, Optional, Sequence, Tuple

DEFAULT_WINDOW_DAYS = 30

# A gap this long between two snapshots means real data is missing, not that
# consumption was smooth across the hole - averaging across it would invent
# a drawdown that never happened, so the interval is skipped entirely rather
# than counted.
_MAX_GAP_DAYS = 7

_AGREEMENT_TOLERANCE = 0.20

# (band, min_history_days, min_intervals_used) - checked in order, first
# match wins from the top. Below LOW's threshold is "none".
_BANDS = (
    ("high", 14, 5),
    ("medium", 7, 3),
    ("low", 2, 1),
)


class ConsumptionPoint(NamedTuple):
    date: datetime.date
    todays_stock: Decimal
    received: Optional[Decimal]
    issued: Optional[Decimal]


def _confidence_band(history_days: int, intervals_used: int) -> str:
    for band, min_days, min_intervals in _BANDS:
        if history_days >= min_days and intervals_used >= min_intervals:
            return band
    return "none"


def _issued_cross_check(windowed, primary_avg_daily: Optional[float]) -> Optional[bool]:
    """Where `issued` behaves cumulatively (monotonically non-decreasing
    across the whole window), its total delta over the days spanned is a
    second, independent estimate of the daily rate. A disagreement means the
    sheet's own issue arithmetic doesn't reconcile with its own closing
    balances - worth surfacing on its own merits, but this second estimate
    is never used in place of the primary. Returns None when not computable
    (issued missing/not cumulative, or the primary itself isn't available),
    otherwise whether the two estimates agree within _AGREEMENT_TOLERANCE."""
    if not primary_avg_daily:
        return None
    for (_, _, _, i0), (_, _, _, i1) in zip(windowed, windowed[1:]):
        if i0 is None or i1 is None or i1 < i0:
            return None
    history_days = (windowed[-1][0] - windowed[0][0]).days
    if history_days <= 0:
        return None
    total_issued = windowed[-1][3] - windowed[0][3]
    if not total_issued or total_issued <= 0:
        return None
    issued_avg_daily = float(total_issued) / history_days
    diff = abs(issued_avg_daily - primary_avg_daily)
    return diff <= _AGREEMENT_TOLERANCE * max(issued_avg_daily, primary_avg_daily)


def consumption_stats(
    points: Sequence[Tuple],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: Optional[datetime.date] = None,
) -> dict:
    """points: an unordered list of (date, todays_stock, received, issued)
    tuples for one stock lot - `received`/`issued` may be None, only
    `date`/`todays_stock` are load-bearing for the primary number.

    Returns a dict shaped for direct inclusion in the materials API
    response's `consumption` block - see _domestic_base.py's `_lot_dict()`.
    """
    ordered = sorted(points, key=lambda p: p[0])
    if today is None:
        today = ordered[-1][0] if ordered else datetime.date.today()
    window_start = today - datetime.timedelta(days=window_days)
    windowed = [p for p in ordered if window_start <= p[0] <= today]

    if len(windowed) < 2:
        return {
            "avgDaily": None,
            "daysLeft": None,
            "confidence": "none",
            "historyDays": 0,
            "intervalsUsed": 0,
            "receiptIntervals": 0,
            "estimatesAgree": None,
            "windowStart": window_start.isoformat(),
            "windowEnd": today.isoformat(),
        }

    consumed = Decimal(0)
    days = 0
    intervals_used = 0
    receipt_intervals = 0

    for (d0, stock0, _r0, _i0), (d1, stock1, _r1, _i1) in zip(windowed, windowed[1:]):
        gap = (d1 - d0).days
        if gap <= 0 or gap > _MAX_GAP_DAYS:
            continue
        delta = stock0 - stock1
        if delta < 0:
            # Stock rose - a receipt landed. Excluded entirely rather than
            # counted as zero consumption, which would drag the average down
            # and inflate days-left - the exact direction of error that
            # hides a real reorder need.
            receipt_intervals += 1
            continue
        consumed += delta
        days += gap
        intervals_used += 1

    avg_daily = float(consumed) / days if days > 0 and consumed > 0 else None
    latest_stock = float(windowed[-1][1])
    days_left = latest_stock / avg_daily if avg_daily else None
    history_days = (windowed[-1][0] - windowed[0][0]).days

    return {
        "avgDaily": avg_daily,
        "daysLeft": days_left,
        "confidence": _confidence_band(history_days, intervals_used),
        "historyDays": history_days,
        "intervalsUsed": intervals_used,
        "receiptIntervals": receipt_intervals,
        "estimatesAgree": _issued_cross_check(windowed, avg_daily),
        "windowStart": window_start.isoformat(),
        "windowEnd": today.isoformat(),
    }
