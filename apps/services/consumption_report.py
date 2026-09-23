"""
apps/services/consumption_report.py - Daily "Raw Material
Consumption" report, one email per plant (HRS / RTP-Achhad / RTP-Vapi),
listing every material actually issued that day with its days-left estimate
and latest rate. Also builds the Monthly Raw Material Consumption report
(2026-09-08, project owner request) - same category-wise shape, summed over
a whole calendar month instead of one day (see build_plant_monthly_report()/
_render_monthly_report_email() below).

Triggered by an external free scheduler (cron-job.org) hitting
apps/api/routers/reports_views.py's trigger_daily_report once a day (e.g.
20:30 IST), and trigger_monthly_report on the 1st of each month - same
reasoning and pattern as the TDS Automation App's own
apps/api/routers/reports_views.py (Render's free web plan has no built-in
cron, and Render's own Cron Jobs feature isn't free either) - see that
view's own module docstring for the shared-secret auth scheme.

**Both reports read the MaterialConsumptionDaily ledger (2026-09-21).** They
used to derive their own figures from *RMSnapshot, keeping a private copy of
_domestic_base.py's query because apps/services must not import from
apps/api. Two implementations of one number is a bug waiting to happen, and
this one had already happened - the two copies disagreed about whether
`received` was per-day or cumulative. The ledger is written by each plant's
own `compute_<plant>_consumption` pipeline step, so the emails and the
dashboard now quote the same rows by construction.

**That migration fixed a real reporting error, not just a duplication.**
The old "Issued today" read `*RMSnapshot.issued` directly for HRS/Vapi,
believing only RTP-Achhad's column to be period-to-date. Measured on live
data: `opening + received - issued == closing` holds on 4,327 of 4,327 rows
and `opening_stock` is frozen across consecutive snapshots at every plant,
so **all three** columns are period-to-date cumulative. The daily report was
printing a running month-to-date total under a heading that said "Issued
Today", and the monthly report was summing those running totals across the
month. See CLAUDE.md's "Consumption ledger" section.

Rows are per MATERIAL now, not per stock lot: a material split across
several vendor lots was previously several rows each holding a fragment of
the day's issues.

"Latest rate" is still read from the live *RMLot row's own rate field, not
from history - every plant's *RMLot rate field is overwritten by every sync,
so it always reflects the latest successful one. Where several lots of a
material disagree, the highest-value lot's rate wins (see
_material_display()).
"""
from __future__ import annotations

import datetime
import html
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.models import (
    ConsumptionCoverage,
    HRSRMLot,
    MaterialConsumptionDaily,
    ReportSendLog,
    RTPAchhadRMLot,
    RTPVapiRMLot,
    SyncRun,
)
from apps.services.consumption_engine import SPREAD
from apps.services.consumption_periods import DEFAULT_WINDOW_DAYS, consumption_rates, coverage_in_window
from apps.services.parsers.common import normalize_material
from apps.services.security_alerts import _admin_emails

log = logging.getLogger(__name__)

# Fixed extra recipient on both consumption reports (project owner,
# 2026-09-22), alongside every active admin - same convention as
# advance_license_report.py's own _FIXED_RECIPIENT (imports@ravasco.com).
# Kept as a module constant rather than folded into _admin_emails(), which
# is shared with the security alerts: a purchasing mailbox has no business
# receiving account-lockout or login-burst alerts.
_FIXED_RECIPIENT = "purchase@ravasco.com"


def _report_recipients() -> list:
    """Every active admin plus _FIXED_RECIPIENT, de-duplicated (an admin
    PTUser whose own address IS the purchasing mailbox must not be mailed
    twice) while keeping a stable order for the log lines below."""
    out = list(_admin_emails())
    if _FIXED_RECIPIENT not in out:
        out.append(_FIXED_RECIPIENT)
    return out

# Plant report config - a lighter-weight, services-layer sibling of
# apps/api/routers/_domestic_base.py's _PlantConfig (see module docstring
# for why this isn't just imported from there).
# `snapshot_model`/`daily_movement_model` are gone as of the 2026-09-21
# read-path migration - this module no longer touches snapshot history at
# all. Both reports read MaterialConsumptionDaily, which the plant's own
# `compute_<plant>_consumption` step already reconciled (including Achhad's
# daily Recp./Issue matrix). `lot_model` remains, purely for the display
# fields and current stock that are NOT part of the ledger: description,
# category, the live rate, and today's balance.
_PLANTS = {
    "hrs": {
        "label": "HRS (Hindustan Rubbers, Silvassa)",
        "plant": SyncRun.Plant.HRS,
        "lot_model": HRSRMLot,
        "rate_field": "basic_rate",
    },
    "achhad": {
        "label": "RTP-Achhad (Ravasco Transmission and Packing, Achhad)",
        "plant": SyncRun.Plant.RTP_ACHHAD,
        "lot_model": RTPAchhadRMLot,
        "rate_field": "rate",
    },
    "vapi": {
        "label": "RTP-Vapi (Ravasco Transmission and Packing, Vapi)",
        "plant": SyncRun.Plant.RTP_VAPI,
        "lot_model": RTPVapiRMLot,
        "rate_field": "basic_rate",
    },
}


def _month_bounds(year: int, month: int) -> tuple:
    """Returns (first day, last day) of the given calendar month, inclusive.
    Kept as this module's own copy rather than imported from
    consumption_periods.month_bounds() only because the monthly report also
    names the month for display right beside it; the two are identical and
    either would do."""
    start = datetime.date(year, month, 1)
    end = (datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)) - datetime.timedelta(days=1)
    return start, end


def _material_display(cfg: dict) -> dict:
    """material_key -> {'material', 'category', 'rate'} for display.

    Rate comes from the live *RMLot row, not a snapshot - every plant's lot
    rate field is overwritten by every sync, so it always reflects the
    latest successful one regardless of whether a snapshot exists for today
    (unchanged by this migration). Where several lots of one material
    disagree on rate, the highest-value lot's rate wins: that is the lot
    dominating the material's stock value, and the alternative is picking
    whichever row happened to be iterated last.
    """
    rate_field = cfg["rate_field"]
    out = {}
    best_value = {}
    for lot in cfg["lot_model"].objects.all():
        key = normalize_material(lot.description or "")
        if not key:
            continue
        value = float(lot.value or 0)
        if key in out and value <= best_value.get(key, 0.0):
            continue
        best_value[key] = value
        out[key] = {
            "material": lot.description,
            # Blank on a real row (Achhad's own category is a backfilled
            # section-divider label, not a guaranteed column - see
            # RTPAchhadRMLot's docstring) becomes "Uncategorized" rather
            # than an empty group heading.
            "category": lot.category or "Uncategorized",
            "rate": getattr(lot, rate_field, None),
        }
    return out


def _current_stock(cfg: dict) -> dict:
    """material_key -> total current stock across that material's lots.

    Summed across lots for the same reason the ledger is keyed on the
    material: days-of-cover for "SBR 1502" means all the SBR 1502 on hand,
    not whichever vendor's lot was looked at first. Only ACTIVE lots count
    here, unlike the ledger's own history read - a sold-out lot consumed
    real material once but holds none now.
    """
    totals = {}
    for description, stock in cfg["lot_model"].objects.filter(is_active=True).values_list("description", "todays_stock"):
        key = normalize_material(description or "")
        if key:
            totals[key] = totals.get(key, 0.0) + float(stock or 0)
    return totals


def _ledger_rows(cfg: dict, start: datetime.date, end: datetime.date, qty_key: str) -> list:
    """The shared row builder for both reports, reading
    MaterialConsumptionDaily rather than re-deriving anything.

    **This is the read-path migration (2026-09-21) and it fixes a real
    reporting error, not just a duplicate implementation.** The previous
    version read `*RMSnapshot.issued` directly as "Issued Today" for
    HRS/Vapi - but that column is period-to-date cumulative at every plant,
    not a daily figure (measured: `opening + received - issued == closing`
    on 4,327 of 4,327 rows, `opening_stock` frozen across snapshots). So the
    daily report printed a running month-to-date total under a column headed
    "Issued Today", and the monthly report SUMMED those running totals
    across the month. See CLAUDE.md's "Consumption ledger" section.

    `isEstimate` keeps its meaning of "do not read this as a confirmed
    same-day figure", but now marks a genuinely different thing: a quantity
    interpolated across a snapshot gap (`quality == spread`) rather than
    observed on a single dated day. That subsumes the old Achhad-only
    period-to-date fallback - Achhad's daily Recp./Issue matrix is
    reconciled into the ledger at build time now, so there is no live
    cumulative column left to fall back to, and the flag applies uniformly
    at all three plants instead of only one.
    """
    totals = (
        MaterialConsumptionDaily.objects
        .filter(plant=cfg["plant"], consumption_date__gte=start, consumption_date__lte=end)
        .values("material_key")
        .annotate(qty=Sum("quantity"), spread=Count("id", filter=Q(quality=SPREAD)))
        .filter(qty__gt=0)
    )
    display = _material_display(cfg)
    rates = consumption_rates(cfg["plant"], today=timezone.localdate())
    stock_by_material = _current_stock(cfg)

    rows = []
    for t in totals:
        key = t["material_key"]
        meta = display.get(key, {})
        stats = rates.get(key, {})
        avg_daily = stats.get("avgDaily")
        stock = stock_by_material.get(key)
        rows.append({
            "material": meta.get("material") or key,
            "category": meta.get("category") or "Uncategorized",
            qty_key: t["qty"],
            "rate": meta.get("rate"),
            "daysLeft": (stock / avg_daily) if (avg_daily and stock is not None) else None,
            "confidence": stats.get("confidence", "none"),
            "isEstimate": t["spread"] > 0,
        })
    rows.sort(key=lambda r: r[qty_key], reverse=True)
    return rows


def _empty_message(cfg: dict, start: datetime.date, end: datetime.date, period_phrase: str) -> str:
    """What an empty report should actually say.

    **"No material was issued" and "we have no data" are different claims,
    and a report that conflates them is worse than useless on the one day
    it matters.** Before 2026-09-21 the empty body always read "No material
    was issued today." - so a plant whose sync had silently died for a week
    got a calm all-clear every morning, indistinguishable from a genuinely
    quiet day. That is the exact failure this whole ledger exists to stop,
    reproduced in the covering sentence.

    ConsumptionCoverage already knows which it is: a day inside a snapshot
    interval was observed, so zero rows there really does mean nothing was
    issued. A day with no coverage was never looked at, and the honest
    answer is "unknown", pointing at the last date we do have.
    """
    covered, _observed = coverage_in_window(cfg["plant"], start, end)
    if covered:
        return f"No material was issued {period_phrase}."

    # Point at the nearest real data in whichever direction it exists. A
    # report for a month BEFORE this plant's history starts is a different
    # situation from a sync that died yesterday, and saying "no history
    # yet" when September is full of it would be its own small lie.
    dates = ConsumptionCoverage.objects.filter(plant=cfg["plant"]).values_list("coverage_date", flat=True)
    before = dates.filter(coverage_date__lt=start).order_by("-coverage_date").first()
    after = dates.filter(coverage_date__gt=end).order_by("coverage_date").first()
    if before:
        tail = f" The most recent day with data is {before.isoformat()}."
    elif after:
        tail = f" This plant's history only begins on {after.isoformat()}, after the period asked for."
    else:
        tail = " There is no consumption history for this plant at all yet."
    return (
        f"NO STOCK SNAPSHOT WAS CAPTURED {period_phrase}, so there is nothing to report - "
        f"this is a gap in the data, NOT an absence of consumption.{tail} "
        "Material may well have been issued; it was simply never recorded. "
        "If this repeats, the Drive sync for this plant needs checking."
    )


def build_plant_report(plant_key: str, today: datetime.date | None = None) -> dict:
    """Returns {'label', 'date', 'rows': [...]}, rows sorted by issuedToday
    descending (biggest movers first) - _render_report_email() re-groups
    them by category for display, so this order is really "within category"
    order once grouped. Each row: material, category, issuedToday, rate,
    daysLeft, confidence, isEstimate.

    Reads the MaterialConsumptionDaily ledger, written by that plant's own
    `compute_<plant>_consumption` pipeline step - see _ledger_rows() for
    what that fixed. A day the ledger holds no rows for reports nothing,
    rather than falling back to a live cumulative column that would print a
    month-to-date total under a one-day heading.

    Rows are per MATERIAL now, not per stock lot. A material split across
    several vendor lots used to appear as several rows each carrying part
    of the day's issues; it is one row and one figure.
    """
    cfg = _PLANTS[plant_key]
    today = today or timezone.localdate()
    rows = _ledger_rows(cfg, today, today, "issuedToday")
    return {
        "label": cfg["label"],
        "date": today.isoformat(),
        "rows": rows,
        # Computed here, not in the renderer: only the builder knows which
        # plant and which dates were asked for. See _empty_message().
        "emptyMessage": _empty_message(cfg, today, today, "today") if not rows else "",
    }


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
    isEstimate (true when any of the month's days was interpolated across a
    snapshot gap - see _ledger_rows(); it no longer means "Achhad's
    period-to-date fallback", which the 2026-09-21 migration removed).

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
    rows = _ledger_rows(cfg, month_start, month_end, "issuedThisMonth")
    month_label = f"{_MONTH_NAMES[month - 1]} {year}"

    return {
        "label": cfg["label"],
        "month": f"{year:04d}-{month:02d}",
        "monthLabel": month_label,
        "rows": rows,
        "emptyMessage": (
            _empty_message(cfg, month_start, month_end, f"for {month_label}") if not rows else ""
        ),
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
            # A figure interpolated across a snapshot gap (see
            # _ledger_rows()) is one share of a multi-day interval, not a
            # confirmed single-day total - marked "(est.)" right next to
            # the number so it's never read as an equally-confirmed figure,
            # in both the table and the plain-text line. Before 2026-09-21
            # this flag meant Achhad's period-to-date fallback instead.
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
    Days Left = that material's total current stock &divide; its average daily consumption over the
    last {DEFAULT_WINDOW_DAYS} days - an estimate, not a guarantee. Each row is one material,
    combining every vendor lot of it this plant holds. Figures reflect the most recent successful
    Drive sync for this plant (automatic or manually triggered).
  </p>
  <p style="margin:10px 0 0;font-size:11px;color:#718096;">
    <strong>The word in parentheses next to Days Left is NOT a stock-level warning</strong> - it's
    how much snapshot history backs that estimate, shown so a thin estimate is never mistaken for a
    confirmed one:
  </p>
  <ul style="margin:4px 0 0;padding-left:18px;font-size:11px;color:#718096;">
    <li><strong>none</strong> - no usable history in the window at all</li>
    <li><strong>low</strong> - only a small part of the window has data</li>
    <li><strong>medium</strong> - at least half the window covered, some of it measured day by day</li>
    <li><strong>high</strong> - nearly the whole window covered, most of it measured day by day</li>
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
        + f"\n\nDays Left = that material's total current stock divided by its average daily "
        f"consumption over the last {DEFAULT_WINDOW_DAYS} days - an estimate, not a guarantee. Each "
        "row is one material, combining every vendor lot of it this plant holds. Figures reflect the "
        "most recent successful Drive sync for this plant (automatic or manual).\n"
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
    # Superseded 2026-09-21: this used to describe Achhad's period-to-date
    # fallback, which no longer exists. (est.) now means one thing at all
    # three plants - see _ledger_rows().
    estimate_note = (
        "(est.) marks a figure averaged across a gap between stock snapshots rather than observed "
        "on this day alone. The total across the gap is right; how it splits between those days is "
        "an even share, not a measurement. Treat it as an indicator, not a confirmed same-day amount."
    )
    return _render_consumption_email(
        title, "Issued Today", report["rows"], "issuedToday",
        report.get("emptyMessage") or "No material was issued today.", estimate_note,
    )


def _render_monthly_report_email(report: dict) -> tuple:
    """Builds (html_body, text_body) for one plant's MONTHLY report -
    (added 2026-09-08). Same category-wise shape as the daily report, just
    summed over a whole month - see build_plant_monthly_report()."""
    title = f"Raw Material Consumption Report (Monthly) - {report['label']} - {report['monthLabel']}"
    # See the daily report's own note - same change, same reason.
    estimate_note = (
        "(est.) marks a material whose month includes at least one figure averaged across a gap "
        "between stock snapshots rather than observed day by day. The month's total is right; the "
        "per-day split across those gaps is an even share, not a measurement."
    )
    return _render_consumption_email(
        title, "Issued This Month", report["rows"], "issuedThisMonth",
        report.get("emptyMessage") or "No material was issued this month.", estimate_note,
    )


def send_daily_consumption_reports() -> dict:
    """Builds and emails all 3 plants' consumption reports to every active
    admin (PTUser role=admin, apps/services/security_alerts.py's own
    _admin_emails() - reused rather than duplicated) plus the fixed
    purchasing mailbox - see _report_recipients() - one email per plant,
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
    admin_emails = _report_recipients()
    today = timezone.localdate()

    if not admin_emails:
        log.warning("send_daily_consumption_reports: no recipients at all, nothing sent")
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
            # fail_silently=False is what makes the claim release below work
            # for a DELIVERY failure, not only a build failure (2026-09-23,
            # audit pass). With True, an SMTP fault returned normally, the
            # `except` never ran, and the day's claim stayed - so a re-trigger
            # was silently deduped and the failure never reached the log or
            # Sentry. Same defect and same fix as advance_license_report.py's
            # _send_one(); see that comment for the long version.
            send_mail(
                subject=subject, message=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, html_message=html_body, fail_silently=False,
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
    admin_emails = _report_recipients()
    today = timezone.localdate()
    if year is None or month is None:
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month
    month_str = f"{year:04d}-{month:02d}"

    if not admin_emails:
        log.warning("send_monthly_consumption_reports: no recipients at all, nothing sent")
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
            # See the daily sender's comment. Matters more here: a kept claim
            # blocks the whole month, not one day.
            send_mail(
                subject=subject, message=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails, html_message=html_body, fail_silently=False,
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
