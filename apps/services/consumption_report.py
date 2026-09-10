"""
apps/services/consumption_report.py — Daily "Raw Material
Consumption" report, one email per plant (HRS / RTP-Achhad / RTP-Vapi),
listing every material actually issued that day with its days-left estimate
and latest rate. Also builds the Monthly Raw Material Consumption report
(2026-09-08, project owner request) - same category-wise shape, summed over
a whole calendar month instead of one day (see build_plant_monthly_report()/
_render_monthly_report_email() below).

Triggered by an external free scheduler (cron-job.org) hitting
apps/api/routers/reports_views.py's trigger_daily_report once a day (e.g.
20:30 IST), and trigger_monthly_report on the 1st of each month — same
reasoning and pattern as the TDS Automation App's own
apps/api/routers/reports_views.py (Render's free web plan has no built-in
cron, and Render's own Cron Jobs feature isn't free either) — see that
view's own module docstring for the shared-secret auth scheme.

Deliberately NOT built on top of apps/api/routers/_domestic_base.py's
_consumption_by_lot()/_daily_movement_points() even though the underlying
query overlaps heavily: apps/services must not import from apps/api (the
dependency only ever flows the other way in this app — see CLAUDE.md's
"Architecture" section), so this module keeps its own small, report-scoped
copy of that query instead.

"Issued today" is read from a genuinely per-day source, not every plant's
same-named field — see CLAUDE.md's "Days-Left Engine" section:
RTPAchhadRMLot's own `issued` column is a period-to-date summary that resets
each period, not a daily value, so Achhad's per-day figure has to come from
RTPAchhadRMDailyMovement instead (parsed from the Stock file's own daily
Recp./Issue matrix). HRS's and RTP-Vapi's Stock sheets have no such
period-reset behavior — their RMSnapshot.issued for a given day is already
a real one-day figure, sourced from a separate Receipt/Issue tab.

"Latest rate" is read from the live *RMLot row's own rate field, not that
day's snapshot — every plant's *RMLot rate field is overwritten by every
sync (auto, every 3 hours, or manual), so it always reflects the latest
successful sync regardless of whether a snapshot happens to exist for today
for that specific lot.
"""
from __future__ import annotations

import datetime
import html
import itertools
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Sum
from django.utils import timezone

from apps.core.models import (
    HRSRMLot,
    HRSRMSnapshot,
    ReportSendLog,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
    RTPVapiRMLot,
    RTPVapiRMSnapshot,
)
from apps.services.security_alerts import _admin_emails
from apps.services.stock_consumption import DEFAULT_WINDOW_DAYS, consumption_stats

log = logging.getLogger(__name__)

# Plant report config - a lighter-weight, services-layer sibling of
# apps/api/routers/_domestic_base.py's _PlantConfig (see module docstring
# for why this isn't just imported from there).
_PLANTS = {
    "hrs": {
        "label": "HRS (Hindustan Rubbers, Silvassa)",
        "lot_model": HRSRMLot,
        "snapshot_model": HRSRMSnapshot,
        "rate_field": "basic_rate",
        "daily_movement_model": None,
    },
    "achhad": {
        "label": "RTP-Achhad (Ravasco Transmission and Packing, Achhad)",
        "lot_model": RTPAchhadRMLot,
        "snapshot_model": RTPAchhadRMSnapshot,
        "rate_field": "rate",
        "daily_movement_model": RTPAchhadRMDailyMovement,
    },
    "vapi": {
        "label": "RTP-Vapi (Ravasco Transmission and Packing, Vapi)",
        "lot_model": RTPVapiRMLot,
        "snapshot_model": RTPVapiRMSnapshot,
        "rate_field": "basic_rate",
        "daily_movement_model": None,
    },
}


def _consumption_stats_by_lot(cfg: dict, window_start: datetime.date) -> dict:
    """Same shape/logic as _domestic_base._consumption_by_lot() - kept as
    this module's own copy, see module docstring for why it isn't shared."""
    rows = (
        cfg["snapshot_model"].objects
        .filter(stock_lot__is_active=True, snapshot_date__gte=window_start)
        .values_list("stock_lot_id", "snapshot_date", "todays_stock", "received", "issued")
        .order_by("stock_lot_id", "snapshot_date")
    )
    points_by_lot: dict[int, list[tuple]] = {
        lot_id: [(date, stock, received, issued) for _, date, stock, received, issued in group]
        for lot_id, group in itertools.groupby(rows, key=lambda row: row[0])
    }

    if cfg["daily_movement_model"] is not None:
        move_rows = list(
            cfg["daily_movement_model"].objects
            .filter(stock_lot__is_active=True, movement_date__gte=window_start)
            .values_list("stock_lot_id", "movement_date", "received", "issued")
            .order_by("stock_lot_id", "movement_date")
        )
        if move_rows:
            lot_ids = {r[0] for r in move_rows}
            openings = dict(cfg["lot_model"].objects.filter(id__in=lot_ids).values_list("id", "opening_stock"))
            for lot_id, group in itertools.groupby(move_rows, key=lambda row: row[0]):
                running_stock = openings.get(lot_id) or 0
                points = []
                for _, date, received, issued in group:
                    running_stock = running_stock + received - issued
                    points.append((date, running_stock, received, issued))
                points_by_lot.setdefault(lot_id, []).extend(points)

    return {lot_id: consumption_stats(points) for lot_id, points in points_by_lot.items()}


def _todays_issued_rows(cfg: dict, today: datetime.date) -> list:
    """Returns [(lot, issued_qty, is_estimate)] for every active lot with a
    nonzero issued quantity - see module docstring for why the primary
    source differs by plant.

    Achhad fallback (2026-09-08, project owner request, after confirming
    directly against a real exported RM Stock file that its day-matrix can
    sit blank for the current day at export time - the plant hadn't filled
    it in yet): a lot with NO RTPAchhadRMDailyMovement row for `today` at
    all falls back to its own live RTPAchhadRMLot.issued column instead of
    being silently omitted. That column is a PERIOD-TO-DATE cumulative total
    that resets each period, not a true daily figure (see module docstring) -
    this is a deliberately-accepted rough estimate, not a fix for that
    unreliability, which is why it's flagged `is_estimate=True` and the
    report/email must say so next to the figure rather than presenting it
    as an equally-trustworthy same-day number. A lot that DOES have a real
    dated row for today keeps using that (never overridden by the
    estimate), so a genuine day-matrix entry is always preferred."""
    if cfg["daily_movement_model"] is not None:
        moves = (
            cfg["daily_movement_model"].objects
            .filter(movement_date=today, issued__gt=0, stock_lot__is_active=True)
            .select_related("stock_lot")
        )
        confirmed = {m.stock_lot_id: (m.stock_lot, m.issued) for m in moves}

        fallback_lots = (
            cfg["lot_model"].objects
            .filter(is_active=True, issued__gt=0)
            .exclude(id__in=confirmed.keys())
        )

        rows = [(lot, qty, False) for lot, qty in confirmed.values()]
        rows += [(lot, lot.issued, True) for lot in fallback_lots]
        return rows

    snapshots = (
        cfg["snapshot_model"].objects
        .filter(snapshot_date=today, issued__gt=0, stock_lot__is_active=True)
        .select_related("stock_lot")
    )
    return [(s.stock_lot, s.issued, False) for s in snapshots]


def _month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """Returns (first day, last day) of the given calendar month, inclusive."""
    start = datetime.date(year, month, 1)
    end = (datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)) - datetime.timedelta(days=1)
    return start, end


def _month_issued_rows(cfg: dict, month_start: datetime.date, month_end: datetime.date, *, allow_fallback: bool) -> list:
    """Monthly equivalent of _todays_issued_rows() - returns
    [(lot, issued_qty, is_estimate)], summing every dated entry within
    [month_start, month_end] per lot rather than reading a single day.

    For HRS/Vapi this sums *RMSnapshot.issued across the month - already a
    genuine per-day figure (see module docstring), so summing it is exactly
    as trustworthy as the daily report's own single-day read, just added up.
    A day the scheduler didn't capture a snapshot for (a known, documented
    gap - see CLAUDE.md's Snapshot Pipeline Rebuild notes) simply isn't
    counted, same accepted limitation as everywhere else this data is used.

    For Achhad this sums RTPAchhadRMDailyMovement.issued across the month -
    the same dated, reconciliation-confirmed source the daily report already
    trusts (see achhad_stock.py's parser docstring), not the live period-to-
    date `issued` column, which would double- or under-count depending on
    when in the (possibly already-reset) period this runs. `allow_fallback`
    (only ever True when the requested month IS the current, still-open
    period - see build_plant_monthly_report()) mirrors the daily report's own
    Achhad fallback for a lot with ZERO dated rows in the whole month so far:
    its live `issued` column is that same still-open period's running total,
    so it's a reasonable rough estimate for "this month so far" - flagged
    isEstimate=True, exactly like the daily fallback. This must NEVER apply
    to an already-closed prior month: by then the live column reflects a
    newer period entirely and would be flatly wrong, not just imprecise."""
    if cfg["daily_movement_model"] is not None:
        totals = (
            cfg["daily_movement_model"].objects
            .filter(movement_date__gte=month_start, movement_date__lte=month_end, stock_lot__is_active=True)
            .values("stock_lot_id")
            .annotate(total_issued=Sum("issued"))
            .filter(total_issued__gt=0)
        )
        lots_by_id = {l.id: l for l in cfg["lot_model"].objects.filter(id__in=[t["stock_lot_id"] for t in totals])}
        rows = [(lots_by_id[t["stock_lot_id"]], t["total_issued"], False) for t in totals if t["stock_lot_id"] in lots_by_id]

        if allow_fallback:
            confirmed_ids = {t["stock_lot_id"] for t in totals}
            fallback_lots = (
                cfg["lot_model"].objects
                .filter(is_active=True, issued__gt=0)
                .exclude(id__in=confirmed_ids)
            )
            rows += [(lot, lot.issued, True) for lot in fallback_lots]
        return rows

    totals = (
        cfg["snapshot_model"].objects
        .filter(snapshot_date__gte=month_start, snapshot_date__lte=month_end, stock_lot__is_active=True)
        .values("stock_lot_id")
        .annotate(total_issued=Sum("issued"))
        .filter(total_issued__gt=0)
    )
    lots_by_id = {l.id: l for l in cfg["lot_model"].objects.filter(id__in=[t["stock_lot_id"] for t in totals])}
    return [(lots_by_id[t["stock_lot_id"]], t["total_issued"], False) for t in totals if t["stock_lot_id"] in lots_by_id]


def build_plant_report(plant_key: str, today: datetime.date | None = None) -> dict:
    """Returns {'label', 'date', 'rows': [...]}, rows sorted by issuedToday
    descending (biggest movers first) - _render_report_email() re-groups
    them by category for display, so this order is really "within category"
    order once grouped. Each row: material, category, issuedToday, rate,
    daysLeft, confidence, isEstimate (True only for Achhad's period-to-date
    fallback - see _todays_issued_rows()'s own docstring)."""
    cfg = _PLANTS[plant_key]
    today = today or timezone.localdate()
    window_start = today - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)

    consumption_by_lot = _consumption_stats_by_lot(cfg, window_start)
    issued_rows = _todays_issued_rows(cfg, today)

    rows = []
    for lot, issued_qty, is_estimate in issued_rows:
        stats = consumption_by_lot.get(lot.id, {})
        rate = getattr(lot, cfg["rate_field"], None)
        rows.append({
            "material": lot.description,
            # Same field on all 3 *RMLot models (see apps/core/models.py) -
            # no per-plant branching needed. Blank on a real row (Achhad's
            # own category is itself a backfilled section-divider label, not
            # a guaranteed-present column - see RTPAchhadRMLot's docstring)
            # becomes "Uncategorized" rather than an empty group heading.
            "category": lot.category or "Uncategorized",
            "issuedToday": issued_qty,
            "rate": rate,
            "daysLeft": stats.get("daysLeft"),
            "confidence": stats.get("confidence", "none"),
            "isEstimate": is_estimate,
        })
    rows.sort(key=lambda r: r["issuedToday"], reverse=True)

    return {"label": cfg["label"], "date": today.isoformat(), "rows": rows}


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def build_plant_monthly_report(
    plant_key: str, year: int | None = None, month: int | None = None, today: datetime.date | None = None,
) -> dict:
    """Monthly equivalent of build_plant_report() - same shape, summed over
    a whole calendar month instead of one day. Returns {'label', 'month'
    (YYYY-MM), 'monthLabel' (e.g. "September 2026"), 'rows': [...]}. Each
    row: material, category, issuedThisMonth, rate, daysLeft, confidence,
    isEstimate (see _month_issued_rows()'s own docstring for what triggers
    it - Achhad only, and only for the current, still-open month).

    Defaults to the most recently COMPLETED month (today's month minus one) -
    the natural target for a report meant to run on the 1st of a new month,
    summarizing the month that just ended. Pass `year`/`month` explicitly to
    build a report for any other month, including the current (still-open)
    one - `daysLeft`/`confidence` always reflect right now regardless of
    which month is being summarized, since that figure is inherently a
    present-tense inventory runway estimate, not something tied to a
    specific past month. `today` is an explicit override for tests only
    (mirrors build_plant_report()'s own `today` param) - real callers never
    pass it, since "is this the current, still-open period" must be judged
    against the real calendar date, not a caller-supplied one."""
    cfg = _PLANTS[plant_key]
    today = today or timezone.localdate()
    if year is None or month is None:
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    month_start, month_end = _month_bounds(year, month)
    is_current_period = (year, month) == (today.year, today.month)

    window_start = today - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    consumption_by_lot = _consumption_stats_by_lot(cfg, window_start)
    issued_rows = _month_issued_rows(cfg, month_start, month_end, allow_fallback=is_current_period)

    rows = []
    for lot, issued_qty, is_estimate in issued_rows:
        stats = consumption_by_lot.get(lot.id, {})
        rate = getattr(lot, cfg["rate_field"], None)
        rows.append({
            "material": lot.description,
            "category": lot.category or "Uncategorized",
            "issuedThisMonth": issued_qty,
            "rate": rate,
            "daysLeft": stats.get("daysLeft"),
            "confidence": stats.get("confidence", "none"),
            "isEstimate": is_estimate,
        })
    rows.sort(key=lambda r: r["issuedThisMonth"], reverse=True)

    return {
        "label": cfg["label"],
        "month": f"{year:04d}-{month:02d}",
        "monthLabel": f"{_MONTH_NAMES[month - 1]} {year}",
        "rows": rows,
    }


def _render_consumption_rows(rows: list, qty_key: str, empty_message: str) -> tuple:
    """Shared category-grouped table/text builder for BOTH the daily and
    monthly consumption emails (_render_report_email()/
    _render_monthly_report_email() below) - added 2026-09-08 alongside the
    monthly report, factored out of what used to be the daily report's own
    inline loop so the two reports can't drift apart in how grouping/
    formatting works. `qty_key` picks which dict key holds the issued
    quantity ('issuedToday' vs 'issuedThisMonth' - kept as distinct keys per
    report rather than a shared name, so neither report's row shape is a
    breaking change for the other's existing consumers/tests).

    Grouped by category (2026-09-08, project owner request: "just divide the
    material by category, add a category pane in the table") - a blank
    category is bucketed as "Uncategorized" rather than dropped or given an
    empty-titled group (see build_plant_report()'s own comment on why that
    can legitimately happen, e.g. Achhad's category being a backfilled
    section-divider label). Rows arrive already sorted by the quantity
    column descending - grouping via plain dict insertion order (not a
    re-sort) preserves that within each category, and orders the categories
    themselves by whichever contains the single biggest mover first, without
    a separate category-level sort to reason about.

    Returns (body_html_rows, text_rows, any_estimate)."""
    if not rows:
        return (
            f'<tr><td colspan="4" style="padding:12px;color:#718096;">{empty_message}</td></tr>',
            [empty_message],
            False,
        )

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)

    any_estimate = False
    html_parts = []
    text_rows = []
    for category, cat_rows in groups.items():
        html_parts.append(
            '<tr><td colspan="4" style="padding:10px 8px 6px;font-weight:700;'
            'font-size:12.5px;color:#1A202C;background:#F7FAFC;border-top:2px solid #CBD5E0;">'
            f'{html.escape(category)}</td></tr>'
        )
        text_rows.append(f"\n{category}")
        for r in cat_rows:
            days_left = f'{r["daysLeft"]:.1f}' if r["daysLeft"] is not None else "N/A"
            rate = f'{r["rate"]:.2f}' if r["rate"] is not None else "N/A"
            material = html.escape(r["material"])
            # Achhad fallback rows (see _todays_issued_rows()'s/
            # _month_issued_rows()'s own docstrings) carry a period-to-date
            # total, not a true dated figure - marked "(est.)" right next to
            # the number so it's never read as an equally-confirmed figure,
            # in both the table and the plain-text line.
            if r["isEstimate"]:
                any_estimate = True
                qty_display = f'{r[qty_key]} (est.)'
            else:
                qty_display = f'{r[qty_key]}'
            html_parts.append(
                "<tr>"
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{material}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{qty_display}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{rate}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{days_left} ({r["confidence"]})</td>'
                "</tr>"
            )
            text_rows.append(
                f'- {r["material"]}: issued {qty_display}, rate {rate}, days left {days_left} ({r["confidence"]})'
            )
    return "".join(html_parts), text_rows, any_estimate


def _render_consumption_email(title: str, qty_column_label: str, rows: list, qty_key: str, empty_message: str, estimate_note: str) -> tuple:
    """Shared (html_body, text_body) builder wrapping _render_consumption_rows()
    above with the header line/column label/estimate footnote each of the
    daily/monthly reports supplies its own wording for - see
    _render_report_email()/_render_monthly_report_email(). A table, not
    apps/services/email_service.py's render_email() - that builder only
    knows plain paragraphs, with no concept of tabular rows, so this stays
    its own small builder rather than stretching that one's shape to fit.
    Every value is HTML-escaped/formatted defensively even though material
    descriptions come from Drive sync, not direct user input."""
    body_html_rows, text_rows, any_estimate = _render_consumption_rows(rows, qty_key, empty_message)

    # Only shown when at least one row actually used the fallback - most
    # reports (HRS/Vapi always, Achhad once its day-matrix is filled in, or
    # for any already-closed month) will have nothing to explain here.
    estimate_note_html = f'<p style="margin:12px 0 0;font-size:11px;color:#718096;">{estimate_note}</p>' if any_estimate else ""
    estimate_note_text = f"\n\n{estimate_note}" if any_estimate else ""

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 16px;font-size:13px;color:#1A202C;">
    {html.escape(title)}
  </p>
  <table style="border-collapse:collapse;width:100%;font-size:12px;color:#1A202C;">
    <thead>
      <tr style="background:#F7FAFC;">
        <th style="padding:8px;text-align:left;border-bottom:2px solid #CBD5E0;">Material</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">{html.escape(qty_column_label)}</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">Latest Rate</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">Days Left</th>
      </tr>
    </thead>
    <tbody>
      {body_html_rows}
    </tbody>
  </table>
  <p style="margin:20px 0 0;font-size:11px;color:#718096;">
    Days Left = that row's current stock &divide; its average daily consumption over the last
    {DEFAULT_WINDOW_DAYS} days - an estimate, not a guarantee. Each row is one specific vendor's
    stock lot, not the material's combined total across every vendor - a low days-left figure
    applies to that lot only. Figures reflect the most recent successful Drive sync for this plant
    (automatic or manually triggered).
  </p>
  <p style="margin:10px 0 0;font-size:11px;color:#718096;">
    <strong>The word in parentheses next to Days Left is NOT a stock-level warning</strong> - it's
    how much snapshot history backs that estimate, shown so a thin estimate is never mistaken for a
    confirmed one:
  </p>
  <ul style="margin:4px 0 0;padding-left:18px;font-size:11px;color:#718096;">
    <li><strong>none</strong> - not enough snapshot history yet to estimate at all</li>
    <li><strong>low</strong> - thin history (as little as 2 days / 1 usable data point)</li>
    <li><strong>medium</strong> - moderate history (7+ days / 3+ usable data points)</li>
    <li><strong>high</strong> - strong history (14+ days / 5+ usable data points)</li>
  </ul>
  <p style="margin:6px 0 0;font-size:11px;color:#718096;">
    A material tagged <strong>(low)</strong> can still have a perfectly healthy Days Left number -
    the tag means treat that particular figure cautiously until more history accumulates, not that
    the material itself is running low.
  </p>
  {estimate_note_html}
  <p style="margin:16px 0 0;font-size:11px;color:#718096;">
    This is a system generated email. Please do not reply.
  </p>
</body>
</html>"""

    text_body = (
        f"{title}\n\n"
        + "\n".join(text_rows)
        + f"\n\nDays Left = that row's current stock divided by its average daily consumption over "
        f"the last {DEFAULT_WINDOW_DAYS} days - an estimate, not a guarantee. Each row is one "
        "specific vendor's stock lot, not the material's combined total across every vendor - a low "
        "days-left figure applies to that lot only. Figures reflect the most recent successful Drive "
        "sync for this plant (automatic or manual).\n"
        "\nThe word in parentheses next to Days Left is NOT a stock-level warning - it's how much "
        "snapshot history backs that estimate, shown so a thin estimate is never mistaken for a "
        "confirmed one:\n"
        "  none   - not enough snapshot history yet to estimate at all\n"
        "  low    - thin history (as little as 2 days / 1 usable data point)\n"
        "  medium - moderate history (7+ days / 3+ usable data points)\n"
        "  high   - strong history (14+ days / 5+ usable data points)\n"
        "A material tagged (low) can still have a perfectly healthy Days Left number - the tag means "
        "treat that particular figure cautiously until more history accumulates, not that the "
        "material itself is running low.\n"
        + estimate_note_text
        + "\n\nThis is a system generated email. Please do not reply.\n"
    )
    return html_body, text_body


def _render_report_email(report: dict) -> tuple:
    """Builds (html_body, text_body) for one plant's DAILY report."""
    title = f"Raw Material Consumption Report - {report['label']} - {report['date']}"
    estimate_note = (
        "(est.) marks a material with no dated entry yet in today's Recp./Issue matrix - "
        "the figure shown is this plant's period-to-date running total instead, which can include "
        "earlier days in the same period, not only today. Treat it as a rough indicator, not a "
        "confirmed same-day amount."
    )
    return _render_consumption_email(title, "Issued Today", report["rows"], "issuedToday", "No material was issued today.", estimate_note)


def _render_monthly_report_email(report: dict) -> tuple:
    """Builds (html_body, text_body) for one plant's MONTHLY report -
    (added 2026-09-08). Same category-wise shape as the daily report, just
    summed over a whole month - see build_plant_monthly_report()."""
    title = f"Raw Material Consumption Report (Monthly) - {report['label']} - {report['monthLabel']}"
    estimate_note = (
        "(est.) marks a material with no dated entries at all in this month's Recp./Issue matrix "
        "so far - the figure shown is this plant's still-open period-to-date running total instead, "
        "which may change further before the month closes. Treat it as a rough indicator, not a "
        "final monthly amount."
    )
    return _render_consumption_email(
        title, "Issued This Month", report["rows"], "issuedThisMonth", "No material was issued this month.", estimate_note,
    )


def send_daily_consumption_reports() -> dict:
    """Builds and emails all 3 plants' consumption reports to every active
    admin (PTUser role=admin, apps/services/security_alerts.py's own
    _admin_emails() - reused rather than duplicated) - one email per plant,
    not one combined email, so each stays a manageable size and a plant with
    nothing issued today doesn't bury the other two in one thread.

    Called synchronously (not via django-q2/qcluster) by
    apps/api/routers/reports_views.py's trigger_daily_report, itself hit
    once a day (e.g. 20:30 IST) by an external free scheduler - the caller is
    waiting on an HTTP response either way, and building+sending 3 small
    reports for a handful of plants is comfortably within gunicorn's default
    request handling, so no background dispatch is needed here (unlike
    apps/services/security_alerts.py's alerts, which fire from inside an
    unrelated request/task that must return immediately).

    Best-effort per plant: one plant's report failing to build/send must
    not block the other two.

    Dedup guard (added 2026-09-10, full-codebase audit): before this,
    nothing stopped the external scheduler double-firing
    trigger_daily_report (a network retry, or a misconfigured overlapping
    schedule) from sending every admin a duplicate email for the same day.
    Each plant now claims a ReportSendLog row for
    (DAILY, plant_key, today's ISO date) BEFORE building/sending that
    plant's report - `get_or_create()`'s own unique-constraint-backed
    behavior makes the claim itself safe against two near-simultaneous
    calls, not just a plain "check then send". A plant whose row already
    existed is skipped (not an error, not counted in `plants_sent`) rather
    than re-sent. If building/sending then genuinely fails, the just-claimed
    row is deleted again before moving on - a real failure must still be
    retryable (by the next scheduled trigger, or a manual one) rather than
    permanently burning that day's slot the way an unconditional claim
    would."""
    admin_emails = _admin_emails()
    today = timezone.localdate()

    if not admin_emails:
        log.warning("send_daily_consumption_reports: no active admin recipients, nothing sent")
        return {"date": today.isoformat(), "plants_sent": 0, "admins_notified": 0}

    sent = 0
    for plant_key, cfg in _PLANTS.items():
        log_row, claimed = ReportSendLog.objects.get_or_create(
            report_type=ReportSendLog.ReportType.DAILY, plant=plant_key, period_key=today.isoformat(),
        )
        if not claimed:
            log.info(
                "send_daily_consumption_reports: already sent %s report for %s today - skipping duplicate",
                plant_key, today.isoformat(),
            )
            continue
        try:
            report = build_plant_report(plant_key, today=today)
            html_body, text_body = _render_report_email(report)
            subject = f"[Purchase Tracker] Raw Material Consumption - {cfg['label']} - {report['date']}"
            send_mail(
                subject=subject, message=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, html_message=html_body, fail_silently=True,
            )
            sent += 1
            log.info(
                "send_daily_consumption_reports: sent %s report to %s admin(s)",
                plant_key, len(admin_emails),
            )
        except Exception:
            log_row.delete()
            log.exception(
                "send_daily_consumption_reports: failed to build/send report for plant=%s", plant_key,
            )

    return {"date": today.isoformat(), "plants_sent": sent, "admins_notified": len(admin_emails)}


def send_monthly_consumption_reports(year: int | None = None, month: int | None = None) -> dict:
    """Monthly equivalent of send_daily_consumption_reports() (added
    2026-09-08, project owner request) - one email per plant, same
    category-wise shape, defaulting to the most recently completed month
    (see build_plant_monthly_report()'s own docstring). Meant to be triggered
    once, on the 1st of each month, by apps/api/routers/reports_views.py's
    trigger_monthly_report - same external-cron/shared-secret pattern as the
    daily report, a separate endpoint rather than a mode flag on the same
    one, since the two run on genuinely different schedules.

    Dedup guard: same reasoning/mechanism as send_daily_consumption_reports()'s
    own - see that function's docstring - keyed on (MONTHLY, plant_key,
    "YYYY-MM") instead of a date."""
    admin_emails = _admin_emails()
    today = timezone.localdate()
    if year is None or month is None:
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month
    month_str = f"{year:04d}-{month:02d}"

    if not admin_emails:
        log.warning("send_monthly_consumption_reports: no active admin recipients, nothing sent")
        return {"month": month_str, "plants_sent": 0, "admins_notified": 0}

    sent = 0
    for plant_key, cfg in _PLANTS.items():
        log_row, claimed = ReportSendLog.objects.get_or_create(
            report_type=ReportSendLog.ReportType.MONTHLY, plant=plant_key, period_key=month_str,
        )
        if not claimed:
            log.info(
                "send_monthly_consumption_reports: already sent %s report for %s - skipping duplicate",
                plant_key, month_str,
            )
            continue
        try:
            report = build_plant_monthly_report(plant_key, year=year, month=month)
            html_body, text_body = _render_monthly_report_email(report)
            subject = f"[Purchase Tracker] Raw Material Consumption (Monthly) - {cfg['label']} - {report['monthLabel']}"
            send_mail(
                subject=subject, message=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, html_message=html_body, fail_silently=True,
            )
            sent += 1
            log.info(
                "send_monthly_consumption_reports: sent %s report (%s) to %s admin(s)",
                plant_key, month_str, len(admin_emails),
            )
        except Exception:
            log_row.delete()
            log.exception(
                "send_monthly_consumption_reports: failed to build/send report for plant=%s month=%s", plant_key, month_str,
            )

    return {"month": month_str, "plants_sent": sent, "admins_notified": len(admin_emails)}
