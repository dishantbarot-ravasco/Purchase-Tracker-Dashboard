"""
apps/services/consumption_report.py — Daily 20:00 IST "Raw Material
Consumption" report, one email per plant (HRS / RTP-Achhad / RTP-Vapi),
listing every material actually issued that day with its days-left estimate
and latest rate.

Triggered by an external free scheduler (cron-job.org) hitting
apps/api/routers/reports_views.py's trigger_daily_report once a day at
20:00 IST — same reasoning and pattern as the TDS Automation App's own
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
from django.utils import timezone

from apps.core.models import (
    HRSRMLot,
    HRSRMSnapshot,
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
    """Returns [(lot, issued_qty)] for every active lot with a nonzero,
    genuinely-today issued quantity - see module docstring for why the
    source differs by plant."""
    if cfg["daily_movement_model"] is not None:
        moves = (
            cfg["daily_movement_model"].objects
            .filter(movement_date=today, issued__gt=0, stock_lot__is_active=True)
            .select_related("stock_lot")
        )
        return [(m.stock_lot, m.issued) for m in moves]

    snapshots = (
        cfg["snapshot_model"].objects
        .filter(snapshot_date=today, issued__gt=0, stock_lot__is_active=True)
        .select_related("stock_lot")
    )
    return [(s.stock_lot, s.issued) for s in snapshots]


def build_plant_report(plant_key: str, today: datetime.date | None = None) -> dict:
    """Returns {'label', 'date', 'rows': [...]}, rows sorted by issuedToday
    descending (biggest movers first). Each row: material, issuedToday,
    rate, daysLeft, confidence."""
    cfg = _PLANTS[plant_key]
    today = today or timezone.localdate()
    window_start = today - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)

    consumption_by_lot = _consumption_stats_by_lot(cfg, window_start)
    issued_rows = _todays_issued_rows(cfg, today)

    rows = []
    for lot, issued_qty in issued_rows:
        stats = consumption_by_lot.get(lot.id, {})
        rate = getattr(lot, cfg["rate_field"], None)
        rows.append({
            "material": lot.description,
            "issuedToday": issued_qty,
            "rate": rate,
            "daysLeft": stats.get("daysLeft"),
            "confidence": stats.get("confidence", "none"),
        })
    rows.sort(key=lambda r: r["issuedToday"], reverse=True)

    return {"label": cfg["label"], "date": today.isoformat(), "rows": rows}


def _render_report_email(report: dict) -> tuple:
    """Builds (html_body, text_body) for one plant's report. A table, not
    apps/services/email_service.py's render_email() - that builder only
    knows plain paragraphs, with no concept of tabular rows, so this stays
    its own small builder rather than stretching that one's shape to fit.
    Every value is HTML-escaped/formatted defensively even though material
    descriptions come from Drive sync, not direct user input."""
    label = html.escape(report["label"])
    date = report["date"]
    rows = report["rows"]

    if not rows:
        body_html_rows = (
            '<tr><td colspan="4" style="padding:12px;color:#718096;">'
            "No material was issued today.</td></tr>"
        )
        text_rows = ["No material was issued today."]
    else:
        html_parts = []
        text_rows = []
        for r in rows:
            days_left = f'{r["daysLeft"]:.1f}' if r["daysLeft"] is not None else "—"
            rate = f'{r["rate"]:.2f}' if r["rate"] is not None else "—"
            material = html.escape(r["material"])
            html_parts.append(
                "<tr>"
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{material}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{r["issuedToday"]}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{rate}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{days_left} ({r["confidence"]})</td>'
                "</tr>"
            )
            text_rows.append(
                f'- {r["material"]}: issued {r["issuedToday"]}, rate {rate}, days left {days_left} ({r["confidence"]})'
            )
        body_html_rows = "".join(html_parts)

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 16px;font-size:13px;color:#1A202C;">
    Raw Material Consumption Report &mdash; {label} &mdash; {date}
  </p>
  <table style="border-collapse:collapse;width:100%;font-size:12px;color:#1A202C;">
    <thead>
      <tr style="background:#F7FAFC;">
        <th style="padding:8px;text-align:left;border-bottom:2px solid #CBD5E0;">Material</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">Issued Today</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">Latest Rate</th>
        <th style="padding:8px;text-align:right;border-bottom:2px solid #CBD5E0;">Days Left</th>
      </tr>
    </thead>
    <tbody>
      {body_html_rows}
    </tbody>
  </table>
  <p style="margin:20px 0 0;font-size:11px;color:#718096;">
    Days-left is an estimate based on the last {DEFAULT_WINDOW_DAYS} days of stock movement, not a
    guarantee - see the confidence level next to each figure (none/low/medium/high, based on how
    much real history is available). Figures reflect the most recent successful Drive sync for
    this plant (automatic or manually triggered).
  </p>
  <p style="margin:16px 0 0;font-size:11px;color:#718096;">
    This is a system generated email. Please do not reply.
  </p>
</body>
</html>"""

    text_body = (
        f"Raw Material Consumption Report - {report['label']} - {date}\n\n"
        + "\n".join(text_rows)
        + f"\n\nDays-left is an estimate based on the last {DEFAULT_WINDOW_DAYS} days of stock "
        "movement; see the confidence level (none/low/medium/high) next to each figure. Figures "
        "reflect the most recent successful Drive sync (automatic or manual).\n"
    )
    return html_body, text_body


def send_daily_consumption_reports() -> dict:
    """Builds and emails all 3 plants' consumption reports to every active
    admin (PTUser role=admin, apps/services/security_alerts.py's own
    _admin_emails() - reused rather than duplicated) - one email per plant,
    not one combined email, so each stays a manageable size and a plant with
    nothing issued today doesn't bury the other two in one thread.

    Called synchronously (not via django-q2/qcluster) by
    apps/api/routers/reports_views.py's trigger_daily_report, itself hit
    once a day (20:00 IST) by an external free scheduler - the caller is
    waiting on an HTTP response either way, and building+sending 3 small
    reports for a handful of plants is comfortably within gunicorn's default
    request handling, so no background dispatch is needed here (unlike
    apps/services/security_alerts.py's alerts, which fire from inside an
    unrelated request/task that must return immediately).

    Best-effort per plant: one plant's report failing to build/send must
    not block the other two."""
    admin_emails = _admin_emails()
    today = timezone.localdate()

    if not admin_emails:
        log.warning("send_daily_consumption_reports: no active admin recipients, nothing sent")
        return {"date": today.isoformat(), "plants_sent": 0, "admins_notified": 0}

    sent = 0
    for plant_key, cfg in _PLANTS.items():
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
            log.exception(
                "send_daily_consumption_reports: failed to build/send report for plant=%s", plant_key,
            )

    return {"date": today.isoformat(), "plants_sent": sent, "admins_notified": len(admin_emails)}
