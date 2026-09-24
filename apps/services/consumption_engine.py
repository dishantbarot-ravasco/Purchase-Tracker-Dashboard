"""
apps/services/consumption_engine.py - pure, dependency-free derivation of
**what was actually consumed between two stock snapshots**, and the
classification of everything that looks like consumption but isn't.

This is the replacement core for apps/services/stock_consumption.py's
interval arithmetic. It is kept dependency-free (no Django imports) for the
same reason parsers/common.py, validation.py, stock_identity.py,
arithmetic_checks.py and stock_consumption.py are - it unit-tests as plain
Python and is safe to import from a migration's RunPython. The DB layer
over it is apps/services/consumption_ledger.py, the same way data_quality.py
is the DB layer over arithmetic_checks.py.

---------------------------------------------------------------------------
Why this exists: the premise the old engine was built on is wrong
---------------------------------------------------------------------------

stock_consumption.py's docstring states that RTP-Achhad's `issued` is a
period-to-date summary while HRS's and RTP-Vapi's are genuine one-day
figures, and concludes that `issued` can never be the primary signal.
Measured against real synced data (2026-09-21), **all three plants'
`received`/`issued` columns are period-to-date cumulative**:

- `opening + received - issued == closing` holds on **4,327 of 4,327** rows
  at all three plants - the sheets are internally exact, not approximate.
- `opening_stock` is frozen across consecutive snapshots on 100% (Achhad,
  Vapi) and 83% (HRS) of pairs. It is the *period* opening, not yesterday's
  closing.
- `issued` is monotonically non-decreasing per lot on 100% of Achhad's and
  Vapi's lots and 80% of HRS's.

The consequence is an identity that is true by construction whenever two
snapshots sit inside the same period (`opening` unchanged):

    (closing_0 - closing_1) + (received_1 - received_0)  ==  issued_1 - issued_0

Verified on **2,909 of 2,909** same-period intervals, zero failures.

So the old engine's "the books don't reconcile with the balance on 55% of
HRS's days, and are off 2x-10x in aggregate" finding was an artifact of
comparing a *cumulative* column against a *one-day* balance delta. The
books do reconcile - exactly. `issued_1 - issued_0` is therefore not a weak
secondary estimate; it is the plant's own record of what it issued, and it
is arithmetically equivalent to the balance method wherever both are valid.

**It is strictly better than the balance method where they are not both
valid**, which is the whole reason this module leads with it - see
`_RESTATEMENT` below.

---------------------------------------------------------------------------
The four things that are not consumption
---------------------------------------------------------------------------

`classify_interval()` separates these explicitly rather than letting them
average into a rate. Each is a real, measured category, not a hypothetical:

1. `_RESTATEMENT` - **the biggest single distortion.** The sheet rewrites
   `opening` AND `closing` together while `issued` never moves: the figure
   was corrected, nothing was issued. 148 of HRS's 192 opening-change
   intervals. The balance method counts **766,459 units** across them; the
   issue book counts **48,975**. Canonical real case, HRS lot 119 "SACK
   CARBON": 450,500 -> 17,850 on 2026-09-04 with `received` and `issued`
   both 0 on either side - 432,650 units of pure phantom consumption, about
   a third of that plant's entire measured total, from one corrected cell.
   Reading the issue book handles this for free (0 - 0 = 0), with no
   special case. **This is why a period/opening change must NOT fall back
   to the balance delta** - that is precisely the case the balance gets
   15x wrong.

2. `_PERIOD_ROLL` - `issued` actually resets downward, so the sheet really
   did roll into a new period between these two snapshots. The old period's
   closing issue total is unknowable from these two rows alone, so the
   interval is recorded and excluded rather than guessed at. 44 of HRS's
   192 opening-changes; none at Achhad or Vapi in the measured window.

3. `_CLOSEOUT` - a lot that has shown no movement at all in this window
   jumps straight to a closing balance of exactly 0. The books say
   "issued", but a dormant lot emptying in one step is a write-off,
   transfer or lot closure, not a burn rate. Deliberately narrow: 8 events
   at HRS, 8 at Achhad, 4 at Vapi (3-7% of each plant's total). A lot that
   was already being drawn down and simply finishes is NOT a closeout - it
   is genuine consumption of its last units.

4. `_BOOKS_DISAGREE` - the identity above fails. Never observed in the
   measured window (0 of 2,909), so this is a canary, not a workaround:
   it means the sheet's own arithmetic broke. The issue book is still used
   for the figure (it is the plant's direct assertion of what it issued),
   but the interval is marked so it can be surfaced rather than trusted
   silently.

A fifth flag, `_SPREAD`, is not an exclusion: the consumption is real, but
the snapshots either side are more than one day apart, so it cannot be
attributed to a single calendar day. `allocate_daily()` divides it evenly
across the span and marks every day it touches, so a monthly or quarterly
total stays correct while a single day's figure stays honest about being
an average. The old engine discarded any gap over 7 days outright - that
threw away 18% of HRS's and **63% of Achhad's** measured consumption.
"""
import datetime
from decimal import Decimal
from typing import NamedTuple, Optional, Sequence

# -- Interval classifications -----------------------------------------------
# Stored verbatim in MaterialConsumptionDaily.quality / ConsumptionEvent.kind,
# so these strings are persisted data - changing one needs a data migration.
COUNTED = "counted"                 # ordinary, trusted, counted toward the rate
_SPREAD = "spread"                  # real, but averaged across a multi-day span
_RESTATEMENT = "restatement"        # opening rewritten, issue book untouched
_PERIOD_ROLL = "period_roll"        # issue book reset - old period's total unknowable
_CLOSEOUT = "closeout"              # dormant lot zeroed in one step
_BOOKS_DISAGREE = "books_disagree"  # the sheet's own arithmetic failed
_NOT_AN_INTERVAL = "not_an_interval"  # same-day or out-of-order pair

SPREAD = _SPREAD
RESTATEMENT = _RESTATEMENT
PERIOD_ROLL = _PERIOD_ROLL
CLOSEOUT = _CLOSEOUT
BOOKS_DISAGREE = _BOOKS_DISAGREE

# Classifications whose quantity must never reach a consumption rate. Note
# _RESTATEMENT is NOT here: once read from the issue book a restatement's
# figure is correct (usually 0), so it is counted normally - the label is
# kept only so the balance-vs-books divergence stays visible.
EXCLUDED_FROM_RATE = frozenset({_PERIOD_ROLL, _CLOSEOUT, _NOT_AN_INTERVAL})

# Counted toward the rate normally, but still recorded as a ConsumptionEvent
# because the balance disagreed with the issue book - see daily_from_points().
_LOGGED_BUT_COUNTED = frozenset({_RESTATEMENT, _BOOKS_DISAGREE})

# The sheets are exact to 3 decimal places (every *RMSnapshot quantity field
# is decimal_places=3), so anything under half a thousandth is float noise
# from the comparison itself, not a real disagreement.
_IDENTITY_TOLERANCE = Decimal("0.0005")

# A dormant lot must empty by at least this fraction of its own balance in
# one step to read as a closeout rather than an ordinary final drawdown.
_CLOSEOUT_MIN_FRACTION = Decimal("0.9")


class ConsumptionPoint(NamedTuple):
    """One *RMSnapshot row, reduced to the five fields this module reads.
    All four quantities are Decimals straight from the DB - never floats;
    see CLAUDE.md's "Change detection must compare quantized Decimals"
    for why this codebase keeps quantities in Decimal end to end."""

    date: datetime.date
    opening: Decimal
    received: Decimal
    issued: Decimal
    closing: Decimal


class ConsumptionInterval(NamedTuple):
    """What happened between two consecutive snapshots of one lot.

    `quantity` is always the best available figure even when
    `classification` excludes it from the rate - an excluded event is
    reported with its real size (ConsumptionEvent rows), never silently
    dropped, the same convention sync_stock.py's `rows_skipped` uses.
    `balance_quantity` is what the old balance-drawdown method would have
    said, carried along so the two can be compared without recomputing.
    """

    start: datetime.date
    end: datetime.date
    span_days: int
    quantity: Decimal
    balance_quantity: Decimal
    classification: str

    @property
    def counts_toward_rate(self) -> bool:
        return self.classification not in EXCLUDED_FROM_RATE


def _balance_estimate(p0: ConsumptionPoint, p1: ConsumptionPoint) -> Decimal:
    """What the pre-2026-09-21 engine would have computed: the closing
    drawdown plus whatever came in. Kept only as a comparison figure -
    see this module's docstring for why it is no longer the primary."""
    return (p0.closing - p1.closing) + (p1.received - p0.received)


def classify_interval(
    p0: ConsumptionPoint,
    p1: ConsumptionPoint,
    *,
    lot_moved_before: bool,
    max_dated_gap_days: int,
) -> ConsumptionInterval:
    """Derives one interval's consumption and its classification.

    `lot_moved_before` - whether this lot showed any positive consumption in
    an earlier interval of the same window. Only used to tell a genuine
    final drawdown apart from a dormant lot being written off; see
    `_CLOSEOUT` in the module docstring.

    `max_dated_gap_days` - a span longer than this is still counted, but
    flagged `_SPREAD` because it cannot be attributed to one calendar day.
    """
    span = (p1.date - p0.date).days
    balance = _balance_estimate(p0, p1)
    if span <= 0:
        return ConsumptionInterval(p0.date, p1.date, span, Decimal(0), balance, _NOT_AN_INTERVAL)

    issued_delta = p1.issued - p0.issued

    # The issue book reset: p1.issued counts only the new period, so the
    # old period's final total is unknowable from these two rows. Recorded
    # at its balance-implied size purely so the event is visible, and
    # excluded from every rate. Deliberately checked BEFORE the opening
    # comparison - `opening` changing on its own is the restatement case
    # below, which is counted normally.
    if issued_delta < 0:
        return ConsumptionInterval(p0.date, p1.date, span, max(balance, Decimal(0)), balance, _PERIOD_ROLL)

    # A dormant lot emptying completely in one step. Checked before the
    # identity test because a closeout is excluded regardless of whether
    # the sheet's arithmetic happens to reconcile across it.
    #
    # `received` must be unchanged too. A lot that took a delivery in this
    # same interval was not dormant, whatever its prior history looks like -
    # it received and it issued, which is ordinary activity, and its issue
    # figure can legitimately exceed the balance it started from. Without
    # this term a first-interval lot that received 500, issued 550 and
    # ended at 0 reads as a write-off of its opening 100.
    if (
        not lot_moved_before
        and p1.closing == 0
        and p0.closing > 0
        and p1.received == p0.received
        and issued_delta >= _CLOSEOUT_MIN_FRACTION * p0.closing
    ):
        return ConsumptionInterval(p0.date, p1.date, span, issued_delta, balance, _CLOSEOUT)

    # The identity from the module docstring. A failure means the sheet
    # contradicts itself - never seen in real data, kept as a canary.
    if abs(balance - issued_delta) > _IDENTITY_TOLERANCE:
        # `opening` moving while the issue book keeps running is the
        # restatement case: the balance is the side that was rewritten, so
        # the issue book is right and this is not really a disagreement.
        classification = _RESTATEMENT if p0.opening != p1.opening else _BOOKS_DISAGREE
        return ConsumptionInterval(p0.date, p1.date, span, issued_delta, balance, classification)

    return ConsumptionInterval(
        p0.date, p1.date, span, issued_delta, balance,
        COUNTED if span <= max_dated_gap_days else _SPREAD,
    )


def intervals_for_lot(
    points: Sequence[ConsumptionPoint],
    *,
    max_dated_gap_days: int = 1,
) -> list[ConsumptionInterval]:
    """Every consecutive-snapshot interval for one lot, in date order.

    `points` may arrive unordered and may contain two rows for the same
    date (the API layer used to merge two differently-anchored series and
    produce exactly that) - sorting here and classifying a zero-or-negative
    span as `_NOT_AN_INTERVAL` makes both harmless by construction rather
    than by the caller remembering.

    `max_dated_gap_days` defaults to 1 because the snapshot job is meant to
    run every day: anything wider is already an interpolation, and the
    caller should know that. It is a parameter rather than a constant so a
    plant whose sheet genuinely only updates weekly can say so explicitly.
    """
    ordered = sorted(points, key=lambda p: p.date)
    out: list[ConsumptionInterval] = []
    moved = False
    # strict=False is deliberate: this is the pairwise idiom, so the two
    # sequences are intentionally of unequal length (strict=True raises).
    # Same convention as stock_consumption.py's own pairwise loops.
    for p0, p1 in zip(ordered, ordered[1:], strict=False):
        interval = classify_interval(
            p0, p1, lot_moved_before=moved, max_dated_gap_days=max_dated_gap_days,
        )
        out.append(interval)
        if interval.counts_toward_rate and interval.quantity > 0:
            moved = True
    return out


def allocate_daily(interval: ConsumptionInterval) -> list[tuple[datetime.date, Decimal, str]]:
    """Spreads one interval across the calendar days it covers, returning
    `[(date, quantity, quality)]`.

    A one-day interval yields exactly one day at its full quantity. A wider
    one divides evenly and marks every day `_SPREAD`, so that:

    - a month/quarter/year total is the plain sum of its days and stays
      correct even across a snapshot outage, and
    - a single day's figure never silently claims to be a real observation
      when it is one-tenth of a ten-day interval.

    The remainder from the division is added to the LAST day rather than
    dropped, so `sum(allocate_daily(i)) == i.quantity` exactly - a rollup
    must never lose units to rounding. Quantities are attributed to the days
    AFTER the opening snapshot, through the closing one: consumption
    recorded at `end` happened during the span leading up to it, and the
    `start` day's own consumption already belongs to the previous interval.
    """
    if interval.span_days <= 0 or interval.quantity <= 0:
        return []
    days = [interval.start + datetime.timedelta(days=n) for n in range(1, interval.span_days + 1)]
    if len(days) == 1:
        return [(days[0], interval.quantity, interval.classification)]
    # quantize() to the 3 decimal places every snapshot quantity field
    # already uses, so the per-day figures are storable without further
    # rounding and the remainder below is exact.
    share = (interval.quantity / len(days)).quantize(Decimal("0.001"))
    out = [(d, share, _SPREAD) for d in days[:-1]]
    out.append((days[-1], interval.quantity - share * (len(days) - 1), _SPREAD))
    return out


def daily_from_points(
    points: Sequence[ConsumptionPoint],
    *,
    max_dated_gap_days: int = 1,
) -> tuple[dict[datetime.date, tuple[Decimal, str]], list[ConsumptionInterval]]:
    """Convenience composition for one lot: returns
    `({date: (quantity, quality)}, event_intervals)`.

    Only intervals that count toward a rate are allocated to days. The
    second value is every interval the caller should record as a visible
    ConsumptionEvent:

    - the excluded ones (`period_roll`, `closeout`), which never reach a
      rate, and
    - `restatement` and `books_disagree`, which ARE counted (their issue-book
      figure is right) but where the balance disagreed with the books. They
      are returned even at zero quantity - a zero-issue restatement is the
      commonest kind, and without the event it leaves no trace anywhere.
    """
    daily: dict[datetime.date, tuple[Decimal, str]] = {}
    events: list[ConsumptionInterval] = []
    for interval in intervals_for_lot(points, max_dated_gap_days=max_dated_gap_days):
        if interval.classification in _LOGGED_BUT_COUNTED:
            events.append(interval)
        if not interval.counts_toward_rate:
            if interval.classification != _NOT_AN_INTERVAL:
                events.append(interval)
            continue
        for date, qty, quality in allocate_daily(interval):
            prior_qty, prior_quality = daily.get(date, (Decimal(0), COUNTED))
            # Two lots of the same material can both contribute to one day;
            # the weaker quality wins, same "worst contributing band"
            # convention the Days-Left engine's group aggregation uses.
            daily[date] = (prior_qty + qty, _SPREAD if _SPREAD in (prior_quality, quality) else quality)
    return daily, events


def rate_from_daily(
    daily: dict[datetime.date, Decimal],
    *,
    window_start: datetime.date,
    window_end: datetime.date,
) -> Optional[float]:
    """Average daily consumption across a closed window.

    Divides by the window's full calendar length, NOT by the number of days
    that happen to carry a figure. Days-of-cover is a calendar-time
    forecast - stock has to last through the quiet days too, and dividing
    by "days we have data for" would systematically overstate the burn rate
    on any material that only moves twice a week.

    Returns None for an empty window rather than 0.0: "no consumption
    recorded" and "consumes nothing" are different claims, and only the
    caller knows whether the window had any snapshot coverage at all.
    """
    span = (window_end - window_start).days + 1
    if span <= 0:
        return None
    total = sum((q for d, q in daily.items() if window_start <= d <= window_end), Decimal(0))
    if total <= 0:
        return None
    return float(total) / span
