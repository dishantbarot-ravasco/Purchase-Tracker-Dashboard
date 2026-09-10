"""
Pure, dependency-free consumption-rate / days-of-cover calculation for a
stock lot's snapshot history (Purchase Tracker Dashboard's "Days-Left
Engine"). Takes plain (date, todays_stock, received, issued) tuples so the
whole algorithm unit-tests without a database - see
apps/services/tests/test_stock_consumption.py, same convention as
apps/services/matching.py's own pure scoring helpers.

Why `issued` is never trusted for the primary rate: it means a different
thing at each of HRS/RTP-Achhad/RTP-Vapi's Stock sheets - RTP-Achhad's
`issued` is a period-to-date summary that resets each period, and even
where a plant's `issued` looks like a real per-day figure (HRS/RTP-Vapi),
confirmed against real synced data (2026-09-10) that it disagrees with the
lot's own closing-balance movement on more than half of HRS's days (and by
a wide margin in aggregate on all three plants) - some real HRS drawdowns
of thousands of units in a single day are recorded as `issued: 0`. The one
field that means the same thing everywhere and is never wrong by
construction is the closing balance, `todays_stock` - so the day-over-day
drawdown between consecutive snapshots of it is the primary signal.
`issued` is still read, but only for `_issued_cross_check()` below, a
data-quality signal that never overrides the primary number.

`received` gets one deliberate use beyond that (added 2026-09-10, project
owner: "if issue is having mistake then received can be used along with
todays stock"). The real relationship for any interval is simple and
always true regardless of which direction the balance moved:

    real consumption = (stock_yesterday - stock_today) + received_today

- **Balance fell/flat** (the ordinary case): the drop alone already proves
  that much was consumed - `received` (if any was logged the same day) can
  only mean MORE was consumed, never less, so it's added straight in. This
  closes a real, more common gap than the receipt-day case below: found
  real cases across all three plants (2026-09-10) where a receipt landed
  the SAME day as heavy consumption without changing the net direction at
  all - e.g. a lot's balance dropped by only 1,040 net, but 5,040 was also
  received that day, meaning 6,080 was actually consumed, not 1,040 - and
  every bit of that receipt-masked consumption used to be silently
  invisible, understating the rate on every such day.
- **Balance rose overall** (a receipt landed, net direction reversed): used
  to be skipped entirely, on the reasoning that "stock went up" can't be
  split into "how much was received" vs. "how much was consumed that same
  day" without a trustworthy log. That's still true in general - confirmed
  HRS's own `received` reads 0 on 98 of 111 real receipt days in this same
  data, so there's usually nothing to recover - but on the days `received`
  genuinely exceeds the net rise (confirmed real case: `todays_stock`
  4,350 -> 25,000 with `received` logged as 25,000 - the rise alone
  doesn't add up unless 4,350 units were ALSO consumed that same day), the
  same formula above still gives a real, positive answer and is trusted.
  A day where `received` is blank, zero, or doesn't fully explain the rise
  still has nothing reliable to recover and is skipped exactly as before -
  the formula naturally does this itself (a non-positive result is never
  counted), no separate special case needed.
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

    for (d0, stock0, r0, _i0), (d1, stock1, r1, _i1) in zip(windowed, windowed[1:]):
        gap = (d1 - d0).days
        if gap <= 0 or gap > _MAX_GAP_DAYS:
            continue
        delta = float(stock0 - stock1)  # positive = stock fell; negative = stock rose (a receipt landed)
        # `received` is only trusted as a NEW event for THIS interval when
        # it actually differs from the previous snapshot's own reading -
        # found against real data (2026-09-10) that it can stay frozen at
        # the same nonzero figure across several consecutive snapshot rows
        # (a real HRS lot showed the identical `received` value on three
        # snapshots in a row, spanning what should have been two separate
        # intervals) rather than resetting to 0 once "used". Without this
        # guard, that one real receipt would be added into consumption on
        # every interval it happens to still be visible in - counting the
        # same event two or three times over, not once.
        recv = float(r1) if (r1 and r1 != r0) else 0.0

        if delta < 0:
            # Stock rose overall - still counted as a receipt interval
            # regardless of what follows below.
            receipt_intervals += 1
            # `received` can recover REAL same-day consumption a receipt
            # would otherwise hide entirely - see this module's own
            # docstring for why this is narrow (only when `received`
            # genuinely exceeds the rise) and why it's still usually not
            # recoverable (received is frequently blank/zero even on a real
            # receipt day). Anything else (received missing, zero, or not
            # enough to explain the rise on its own) still has nothing
            # trustworthy to recover and is skipped exactly as before.
            implied_consumption = delta + recv  # delta is negative here; delta + recv = recv - rise
            if implied_consumption <= 0:
                continue
        else:
            # Stock fell (or stayed flat) - the ordinary case, always
            # counted, delta==0 included (a real "nothing moved" interval
            # correctly dilutes the average, not skipped). A receipt can
            # ALSO have landed the same day without changing the NET
            # direction at all (found alongside a full-codebase audit,
            # 2026-09-10, project owner's own question about `received`
            # prompted checking for exactly this) - e.g. stock dropped 100
            # net, but 500 was also received that day, meaning 600 was
            # actually consumed, not 100. `received` is simply added back
            # in here - unlike the delta<0 branch, there's no ambiguity to
            # guard against: the balance already tells us at least `delta`
            # was consumed, and anything logged as received on top of that
            # can only mean MORE was consumed, never less.
            implied_consumption = delta + recv

        consumed += Decimal(str(implied_consumption))
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
