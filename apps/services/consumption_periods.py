"""
apps/services/consumption_periods.py - period rollups over the daily
consumption ledger: day, month, quarter, financial year, or any explicit
date range, for one plant or all three.

**The daily row is the only atom.** Every other period is a SUM over
MaterialConsumptionDaily rows, never a separate derivation. That is the
whole point of materialising a daily ledger (see consumption_ledger.py's
docstring): a monthly figure that is computed a second, different way from
the daily one will eventually disagree with it, and then nobody can say
which is right. apps/services/consumption_report.py's existing monthly
report is exactly that shape today - it re-reads *RMSnapshot and re-sums
`issued` independently of the daily report beside it.

Consequences worth knowing rather than rediscovering:

- A period total is exact even across a snapshot outage, because
  consumption_engine.allocate_daily() spreads a multi-day interval across
  the days it covers and adds the rounding remainder to the last day. Sum
  any set of whole intervals and you get the intervals' own total back.
- A period total is NOT exact if the range *cuts through* a spread
  interval - half of a ten-day interval lands in each month. That is
  unavoidable without knowing the real per-day split, and it is why
  `spread_quantity` travels alongside every total: a caller can see how
  much of a figure is interpolated rather than observed.
- Rates divide by the days the plant was actually OBSERVED
  (ConsumptionCoverage), not by the window's calendar length and not by
  the days a given material happens to carry a row. A quiet day inside
  coverage correctly dilutes the average; a day nobody looked at must not.
  See ConsumptionCoverage's docstring and consumption_rates() below.

Financial-year handling is India's April-March, matching the
`*_25-26` / `*_26-27` folder convention the PO data already uses
(Domestic_PO_Data/, see CLAUDE.md's "Drive layout"). A "2025-26" year runs
2025-04-01 to 2026-03-31.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.core.models import ConsumptionCoverage, ConsumptionEvent, MaterialConsumptionDaily, SyncRun
from apps.services.consumption_engine import SPREAD

# India's financial year starts in April - see the module docstring.
FINANCIAL_YEAR_START_MONTH = 4

GRAIN_DAY = "day"
GRAIN_MONTH = "month"
GRAIN_QUARTER = "quarter"
GRAIN_YEAR = "year"
GRAINS = (GRAIN_DAY, GRAIN_MONTH, GRAIN_QUARTER, GRAIN_YEAR)


def month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """(first day, last day) of a calendar month, inclusive."""
    start = datetime.date(year, month, 1)
    end = (datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)) - datetime.timedelta(days=1)
    return start, end


def quarter_bounds(year: int, quarter: int) -> tuple[datetime.date, datetime.date]:
    """(first day, last day) of a FINANCIAL quarter, inclusive. Q1 is
    Apr-Jun, matching the financial year this business actually reports on -
    not Jan-Mar. `year` is the year the financial year STARTS in, so
    quarter_bounds(2026, 4) is Jan-Mar 2027."""
    if not 1 <= quarter <= 4:
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    start_month = FINANCIAL_YEAR_START_MONTH + 3 * (quarter - 1)
    start_year = year + (start_month - 1) // 12
    start_month = (start_month - 1) % 12 + 1
    start = datetime.date(start_year, start_month, 1)
    end_month = start_month + 2
    end_year = start_year + (end_month - 1) // 12
    end_month = (end_month - 1) % 12 + 1
    return start, month_bounds(end_year, end_month)[1]


def financial_year_bounds(start_year: int) -> tuple[datetime.date, datetime.date]:
    """(first day, last day) of the financial year beginning in
    `start_year` - financial_year_bounds(2025) is 2025-04-01..2026-03-31,
    i.e. what the Drive folders call `25-26`."""
    return (
        datetime.date(start_year, FINANCIAL_YEAR_START_MONTH, 1),
        datetime.date(start_year + 1, FINANCIAL_YEAR_START_MONTH, 1) - datetime.timedelta(days=1),
    )


def financial_year_label(start_year: int) -> str:
    """"2025-26" - the same form the Domestic_PO_Data folders use."""
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def financial_year_of(date: datetime.date) -> int:
    """The year the financial year containing `date` starts in."""
    return date.year if date.month >= FINANCIAL_YEAR_START_MONTH else date.year - 1


def bounds_for(grain: str, *, year: int, month: int | None = None, quarter: int | None = None,
               day: datetime.date | None = None) -> tuple[datetime.date, datetime.date]:
    """One entry point from (grain, period identifiers) to a date range, so
    a caller - an API view, a report, a management command - never has to
    assemble its own bounds and get the financial-year offset subtly
    wrong."""
    if grain == GRAIN_DAY:
        if day is None:
            raise ValueError("grain 'day' needs `day`")
        return day, day
    if grain == GRAIN_MONTH:
        if month is None:
            raise ValueError("grain 'month' needs `month`")
        return month_bounds(year, month)
    if grain == GRAIN_QUARTER:
        if quarter is None:
            raise ValueError("grain 'quarter' needs `quarter`")
        return quarter_bounds(year, quarter)
    if grain == GRAIN_YEAR:
        return financial_year_bounds(year)
    raise ValueError(f"unknown grain {grain!r}, expected one of {GRAINS}")


def _base_queryset(start: datetime.date, end: datetime.date, plant: str | None):
    qs = MaterialConsumptionDaily.objects.filter(consumption_date__gte=start, consumption_date__lte=end)
    return qs.filter(plant=plant) if plant else qs


def material_totals(
    start: datetime.date,
    end: datetime.date,
    *,
    plant: str | None = None,
    min_quantity: Decimal | None = None,
) -> list[dict]:
    """Per-material consumption over a closed date range, biggest first.

    One aggregate query, not one per material. `plant=None` rolls the three
    plants together on `material_key` - which is exactly why `material_key`
    is a uniform normalized description across all three rather than each
    plant's own code (see MaterialConsumptionDaily's docstring); a
    per-plant key would make a company-wide total impossible to assemble.

    Each row carries `spreadQuantity` and `spreadDays` next to the total:
    how much of this figure was interpolated across a snapshot gap rather
    than observed on a single day. Rendered, not filtered on - the same
    convention the Days-Left confidence band follows.
    """
    span_days = (end - start).days + 1
    qs = _base_queryset(start, end, plant)
    rows = (
        qs.values("material_key")
        .annotate(
            total=Sum("quantity"),
            spread_total=Sum("quantity", filter=Q(quality=SPREAD)),
            days_with_data=Count("consumption_date", distinct=True),
            spread_days=Count("consumption_date", distinct=True, filter=Q(quality=SPREAD)),
        )
        .order_by("-total")
    )
    if min_quantity is not None:
        rows = rows.filter(total__gte=min_quantity)

    # Display fields come from the most recent row for each material rather
    # than being grouped on - a description reworded mid-period must not
    # split one material into two result rows (that is the same forking
    # problem material_key exists to prevent).
    latest = {}
    for r in qs.values("material_key", "material_description", "material_code", "category", "uom",
                       "consumption_date").order_by("material_key", "consumption_date"):
        latest[r["material_key"]] = r

    out = []
    for row in rows:
        meta = latest.get(row["material_key"], {})
        total = row["total"] or Decimal(0)
        out.append({
            "materialKey": row["material_key"],
            "material": meta.get("material_description") or row["material_key"],
            "materialCode": meta.get("material_code") or "",
            "category": meta.get("category") or "Uncategorized",
            "uom": meta.get("uom") or "",
            "quantity": total,
            "spreadQuantity": row["spread_total"] or Decimal(0),
            "daysWithData": row["days_with_data"],
            "spreadDays": row["spread_days"],
            "avgDaily": float(total) / span_days if span_days > 0 and total > 0 else None,
        })
    return out


def category_totals(start: datetime.date, end: datetime.date, *, plant: str | None = None) -> list[dict]:
    """Per-category totals over a range, biggest first - the shape the
    category-grouped consumption emails already render."""
    qs = _base_queryset(start, end, plant)
    rows = (
        qs.values("category")
        .annotate(total=Sum("quantity"), materials=Count("material_key", distinct=True))
        .order_by("-total")
    )
    return [
        {
            "category": r["category"] or "Uncategorized",
            "quantity": r["total"] or Decimal(0),
            "materials": r["materials"],
        }
        for r in rows
    ]


def period_series(
    grain: str,
    start: datetime.date,
    end: datetime.date,
    *,
    plant: str | None = None,
    material_key: str | None = None,
) -> list[dict]:
    """A time series of totals at `grain` across [start, end] - the trend
    behind any of the period reports.

    Bucketing is done in Python over one date-grouped query rather than
    with a database `Trunc`: financial quarters and financial years are
    offset by three and nine months from the calendar ones Trunc knows
    about, and getting that offset right in one place here is safer than
    having each caller remember it. The row count is one per day per
    material at most, so the volume is small either way.
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain {grain!r}, expected one of {GRAINS}")
    qs = _base_queryset(start, end, plant)
    if material_key:
        qs = qs.filter(material_key=material_key)
    rows = (
        qs.values("consumption_date")
        .annotate(total=Sum("quantity"), spread_total=Sum("quantity", filter=Q(quality=SPREAD)))
        .order_by("consumption_date")
    )

    buckets: dict[tuple, dict] = {}
    for r in rows:
        d = r["consumption_date"]
        if grain == GRAIN_DAY:
            key, label, b_start, b_end = (d,), d.isoformat(), d, d
        elif grain == GRAIN_MONTH:
            b_start, b_end = month_bounds(d.year, d.month)
            key, label = (d.year, d.month), f"{d.year}-{d.month:02d}"
        elif grain == GRAIN_QUARTER:
            fy = financial_year_of(d)
            q = (d.month - FINANCIAL_YEAR_START_MONTH) % 12 // 3 + 1
            b_start, b_end = quarter_bounds(fy, q)
            key, label = (fy, q), f"{financial_year_label(fy)} Q{q}"
        else:
            fy = financial_year_of(d)
            b_start, b_end = financial_year_bounds(fy)
            key, label = (fy,), financial_year_label(fy)

        bucket = buckets.setdefault(key, {
            "label": label,
            "periodStart": b_start,
            "periodEnd": b_end,
            "quantity": Decimal(0),
            "spreadQuantity": Decimal(0),
        })
        bucket["quantity"] += r["total"] or Decimal(0)
        bucket["spreadQuantity"] += r["spread_total"] or Decimal(0)

    return [buckets[k] for k in sorted(buckets)]


def period_summary(
    start: datetime.date,
    end: datetime.date,
    *,
    plant: str | None = None,
) -> dict:
    """Headline figures for one period, including the excluded events -
    so a report can say "42 closeouts worth 207,239 units were not counted"
    instead of leaving the reader to wonder why the ledger and the sheet
    disagree. See ConsumptionEvent's docstring for that convention."""
    qs = _base_queryset(start, end, plant)
    agg = qs.aggregate(
        total=Sum("quantity"),
        spread_total=Sum("quantity", filter=Q(quality=SPREAD)),
        materials=Count("material_key", distinct=True),
        days=Count("consumption_date", distinct=True),
    )
    events = ConsumptionEvent.objects.filter(interval_end__gte=start, interval_end__lte=end)
    if plant:
        events = events.filter(plant=plant)
    by_kind = [
        {"kind": r["kind"], "count": r["count"], "quantity": r["quantity"] or Decimal(0)}
        for r in events.values("kind").annotate(count=Count("id"), quantity=Sum("quantity")).order_by("-count")
    ]
    total = agg["total"] or Decimal(0)
    span_days = (end - start).days + 1
    return {
        "periodStart": start,
        "periodEnd": end,
        "spanDays": span_days,
        "plant": plant,
        "quantity": total,
        "spreadQuantity": agg["spread_total"] or Decimal(0),
        "materials": agg["materials"],
        "daysWithData": agg["days"],
        "avgDaily": float(total) / span_days if span_days > 0 and total > 0 else None,
        "excludedEvents": by_kind,
    }


def consumption_rate(
    material_key: str,
    *,
    plant: str,
    window_days: int,
    today: datetime.date,
) -> dict:
    """Average daily consumption for one material over a trailing window,
    plus how much of that window actually carries data.

    This is the ledger-backed replacement for stock_consumption.py's
    `consumption_stats()`. The difference that matters: `coverageDays`
    counts days the snapshot pipeline genuinely observed, and
    `observedRatio` is that over the window length. The old engine's
    confidence band counted the SPAN between the first and last snapshot,
    so a lot with 7 real days inside a 17-day span reported `high` - it was
    measuring the calendar, not the data. Here the caller gets both numbers
    and can say which it means.
    """
    start = today - datetime.timedelta(days=window_days - 1)
    rows = (
        MaterialConsumptionDaily.objects
        .filter(plant=plant, material_key=material_key,
                consumption_date__gte=start, consumption_date__lte=today)
        .values_list("quantity", "quality")
    )
    total = Decimal(0)
    observed = 0
    spread = 0
    for qty, quality in rows:
        total += qty
        if quality == SPREAD:
            spread += 1
        else:
            observed += 1
    return {
        "avgDaily": float(total) / window_days if window_days > 0 and total > 0 else None,
        "quantity": total,
        "windowStart": start,
        "windowEnd": today,
        "windowDays": window_days,
        "coverageDays": observed + spread,
        "observedDays": observed,
        "spreadDays": spread,
        "observedRatio": (observed + spread) / window_days if window_days > 0 else 0.0,
    }


# Trailing window for the dashboard's days-of-cover figure. Matches
# stock_consumption.DEFAULT_WINDOW_DAYS, the engine this replaces, so the
# migration changes how the number is derived and not what period it covers.
DEFAULT_WINDOW_DAYS = 30

# (band, min coverage ratio, min observed ratio) - checked top down, first
# match wins. Replaces stock_consumption._BANDS, which keyed on the SPAN
# between the first and last snapshot: a lot with 7 real days inside a
# 17-day span read as `high` there, because the span measures the calendar
# rather than the data. These key on how much of the window the plant
# actually has observation for (ConsumptionCoverage) and how much of that
# was seen on a single dated day rather than interpolated across a gap.
#
# Expect mostly `low` until the snapshot job runs every day - that is the
# bands working, not a regression. On 2026-09-21's data the plants have
# 6-7 distinct snapshot dates in an 18-day span, so almost every figure IS
# thin; the old bands said `high` anyway.
_BANDS = (
    ("high", 0.80, 0.50),
    ("medium", 0.50, 0.20),
    ("low", 0.05, 0.0),
)


def _confidence_band(coverage_ratio: float, observed_ratio: float) -> str:
    for band, min_coverage, min_observed in _BANDS:
        if coverage_ratio >= min_coverage and observed_ratio >= min_observed:
            return band
    return "none"


def coverage_in_window(plant: str, start: datetime.date, end: datetime.date) -> tuple[int, int]:
    """(covered days, directly-observed days) for one plant over a closed
    range - the denominator every rate here divides by.

    See ConsumptionCoverage's docstring for why this cannot be inferred
    from MaterialConsumptionDaily: that table holds only days something was
    consumed, so a quiet-but-watched day and an unwatched day look
    identical in it.
    """
    rows = ConsumptionCoverage.objects.filter(
        plant=plant, coverage_date__gte=start, coverage_date__lte=end,
    ).values_list("observed", flat=True)
    flags = list(rows)
    return len(flags), sum(1 for f in flags if f)


def consumption_rates(
    plant: str,
    *,
    today: datetime.date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, dict]:
    """Every material's trailing-window rate for one plant, keyed on
    `material_key`, in **one** query.

    This is the batched form `consumption_rate()` above answers for a single
    material, and it is what the materials API calls. One aggregate query
    for the whole plant, never one per material - the same N+1 reasoning
    (and the same `django_assert_num_queries` regression test) that
    `_domestic_base.py`'s `_consumption_by_lot()` was built around.

    The returned dict is shaped for direct inclusion in that endpoint's
    per-lot `consumption` block, keeping the field names the frontend
    already reads (`avgDaily`, `confidence`) and adding the coverage
    figures that replace `historyDays`/`intervalsUsed`.
    """
    start = today - datetime.timedelta(days=window_days - 1)
    rows = (
        MaterialConsumptionDaily.objects
        .filter(plant=plant, consumption_date__gte=start, consumption_date__lte=today)
        .values("material_key")
        .annotate(
            total=Sum("quantity"),
            days=Count("consumption_date", distinct=True),
            observed=Count("consumption_date", distinct=True, filter=~Q(quality=SPREAD)),
        )
    )
    covered_days, observed_days = coverage_in_window(plant, start, today)
    # **Divide by the days the plant was actually observed, not by the
    # window length.** MaterialConsumptionDaily has no row for a day a
    # material didn't move, so dividing by 30 asserts that every day
    # without a row was a real zero - including the days nobody looked.
    # Measured on 2026-09-21's data, HRS had 17 covered days in a 30-day
    # window, so that assertion put every rate 43% low and every
    # days-of-cover figure correspondingly high. A quiet day INSIDE
    # coverage still dilutes the average, which is correct: it was watched
    # and nothing moved.
    denominator = covered_days or window_days
    coverage_ratio = covered_days / window_days if window_days else 0.0
    observed_ratio = observed_days / window_days if window_days else 0.0
    band = _confidence_band(coverage_ratio, observed_ratio)

    out = {}
    for r in rows:
        total = r["total"] or Decimal(0)
        out[r["material_key"]] = {
            "avgDaily": float(total) / denominator if denominator > 0 and total > 0 else None,
            "quantity": float(total),
            "confidence": band,
            "coverageDays": covered_days,
            "observedDays": observed_days,
            "materialDays": r["days"],
            "materialObservedDays": r["observed"],
            "windowDays": window_days,
            "windowStart": start.isoformat(),
            "windowEnd": today.isoformat(),
        }
    return out


def plant_keys() -> list[str]:
    """The three plant values the ledger uses, for a caller that wants to
    loop them without importing SyncRun.Plant itself."""
    return [SyncRun.Plant.HRS, SyncRun.Plant.RTP_ACHHAD, SyncRun.Plant.RTP_VAPI]
