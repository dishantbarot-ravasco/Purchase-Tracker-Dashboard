"""
apps/services/advance_license_report.py - Advance License validity-expiry
alerts (added 2026-09-22, project owner request), two consolidated emails:

  - Import Validity expiring within 30 days (AdvanceLicense.import_validity_date)
  - Export Validity expiring within 30 days (AdvanceLicense.export_validity_date)

One email per run per kind (not one email per license) - same batch shape as
consumption_report.py/plant_mismatch_report.py, listing every license
currently crossing the 30-day window in one table rather than spamming a
message per record.

**Fires once per license, the first time it's seen inside the 30-day
window - not on the exact day it crosses 30 days out.** The latter would
silently miss a license entirely if that one day's scheduler run didn't
fire (a network hiccup, a deploy window) - the license would then age past
its validity date having never been alerted at all. Dedup is the same
ReportSendLog table the daily/monthly consumption reports already use
(report_type=ADV_LICENSE_IMPORT/ADV_LICENSE_EXPORT, plant="all" since this
isn't a per-plant concept, period_key="<license_number>@<validity date>") -
a license claims its row before it's included in an email, so a license
already alerted once (even if it's still inside the window on the next run)
is never repeated for that deadline. **Extending a license's validity
re-arms its alert** (2026-09-22): the new date makes a new key, so the
extended deadline is alerted once on its own merits rather than being
silently suppressed by the original alert - see ReportSendLog's docstring.
If sending then fails, every row claimed in that run is released so the
next run retries them - same "claim before send, release on failure"
pattern as send_daily_consumption_reports().

**Materials are joined into one cell, not repeated as one row per
material.** A license can carry many AdvanceLicenseMaterial rows (one per
BOE usage, same material_description repeated); a flat one-row-per-material
table would repeat License Number/CIF/FOB/dates identically down a long
block of rows for a license with several materials. Joining distinct
material descriptions into one semicolon-separated cell matches this app's
own existing convention for a multi-value cell - see
apps/services/license_links.py's module docstring on why a multi-licence
CSV cell is slash-joined rather than exploded into rows there.

Recipients: every active admin (security_alerts._admin_emails(), reused
rather than duplicated) plus the fixed address imports@ravasco.com - unlike
plant_mismatch_report.py's plant-head emails, there is no per-plant
individual here since AdvanceLicense isn't plant-scoped at all.
"""
from __future__ import annotations

import datetime
import html
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.models import AdvanceLicense, ReportSendLog
from apps.services.security_alerts import _admin_emails

log = logging.getLogger(__name__)

_EXPIRY_WINDOW_DAYS = 30
_FIXED_RECIPIENT = "imports@ravasco.com"

_REPORT_CONFIG = {
    "import": {
        "report_type": ReportSendLog.ReportType.ADV_LICENSE_IMPORT,
        "date_field": "import_validity_date",
        "label": "Import Validity",
        "instruction": (
            "Kindly ensure remaining imports against these licenses are completed, or arrange an "
            "extension, before the validity date."
        ),
    },
    "export": {
        "report_type": ReportSendLog.ReportType.ADV_LICENSE_EXPORT,
        "date_field": "export_validity_date",
        "label": "Export Validity",
        "instruction": (
            "Kindly ensure the export obligation (FOB value target) is met, or arrange an extension, "
            "before the validity date."
        ),
    },
}


def _claim_expiring_licenses(kind: str, today: datetime.date) -> tuple:
    """Returns (rows, claimed_log_rows) for the given kind ('import'/'export').

    Only licenses whose relevant validity date falls in [today, today+30]
    AND have not already claimed a ReportSendLog row for this report_type
    AND THIS VALIDITY DATE are included - see module docstring for why this
    is "first time seen in-window", not "exactly 30 days out", and why the
    date belongs in the key."""
    cfg = _REPORT_CONFIG[kind]
    window_end = today + datetime.timedelta(days=_EXPIRY_WINDOW_DAYS)
    date_field = cfg["date_field"]
    qs = (
        AdvanceLicense.objects
        .filter(**{f"{date_field}__gte": today, f"{date_field}__lte": window_end})
        .prefetch_related("materials")
        .order_by(date_field, "license_number")
    )

    rows = []
    claimed_logs = []
    for lic in qs:
        validity_date = getattr(lic, date_field)
        # '<number>@<validity date>', not a bare number - see ReportSendLog's
        # own docstring: a license whose validity is EXTENDED must be able to
        # alert again on its new date, while an unchanged date keeps re-
        # claiming this same row and so never repeats.
        log_row, created = ReportSendLog.objects.get_or_create(
            report_type=cfg["report_type"], plant="all",
            period_key=f"{lic.license_number}@{validity_date.isoformat()}",
        )
        if not created:
            continue
        claimed_logs.append(log_row)

        materials = sorted({
            m.material_description.strip()
            for m in lic.materials.all()
            if m.material_description and m.material_description.strip()
        })
        rows.append({
            "licenseNumber": lic.license_number,
            "exportProductDescription": lic.export_product_description or "-",
            "inputMaterialDescription": "; ".join(materials) if materials else "-",
            "cifValueAuthorized": float(lic.cif_value_authorized),
            "fobValueExportTarget": float(lic.fob_value_export_target),
            "validityDate": validity_date.isoformat(),
            "daysRemaining": (validity_date - today).days,
        })
    return rows, claimed_logs


def _amount_cell(value: float) -> str:
    return f"{value:,.2f}"


def _render_expiry_table_html(rows: list) -> str:
    headers = [
        "License Number", "Export Product Description", "Input Material Description",
        "CIF Value Authorized (INR)", "FOB Value Export Target (INR)", "Validity Date", "Days Remaining",
    ]
    head_html = "".join(
        f'<th style="padding:8px;text-align:left;border-bottom:2px solid #CBD5E0;">{html.escape(h)}</th>'
        for h in headers
    )
    body_html = "".join(
        "<tr>"
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{html.escape(r["licenseNumber"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{html.escape(r["exportProductDescription"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{html.escape(r["inputMaterialDescription"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{_amount_cell(r["cifValueAuthorized"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{_amount_cell(r["fobValueExportTarget"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{html.escape(r["validityDate"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;text-align:right;">{r["daysRemaining"]}</td>'
        "</tr>"
        for r in rows
    )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:12px;color:#1A202C;">'
        f'<thead><tr style="background:#F7FAFC;">{head_html}</tr></thead><tbody>{body_html}</tbody></table>'
    )


def _render_expiry_email(kind: str, rows: list, today: datetime.date) -> tuple:
    cfg = _REPORT_CONFIG[kind]
    label = cfg["label"]
    table_html = _render_expiry_table_html(rows)

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 16px;font-size:13px;color:#1A202C;">Hi,</p>
  <p style="margin:0 0 16px;font-size:13px;color:#4A5568;line-height:1.6;">
    The following Advance Licenses have their <strong>{html.escape(label)}</strong> ending within the
    next {_EXPIRY_WINDOW_DAYS} days, as of {today.isoformat()}. {html.escape(cfg["instruction"])}
  </p>
  {table_html}
  <p style="margin:20px 0 0;font-size:13px;color:#1A202C;line-height:1.6;">
    Regards,<br />Ravasco Transmission and Packing Pvt Ltd.
  </p>
  <p style="margin:24px 0 0;font-size:11px;color:#718096;">
    This is a system generated email. Please do not reply.
  </p>
</body>
</html>"""

    text_rows = "\n".join(
        f'- License {r["licenseNumber"]} | {r["exportProductDescription"]} | '
        f'Materials: {r["inputMaterialDescription"]} | CIF {_amount_cell(r["cifValueAuthorized"])} | '
        f'FOB {_amount_cell(r["fobValueExportTarget"])} | Valid till {r["validityDate"]} | '
        f'{r["daysRemaining"]} day(s) remaining'
        for r in rows
    )
    text_body = (
        "Hi,\n\n"
        f"The following Advance Licenses have their {label} ending within the next "
        f"{_EXPIRY_WINDOW_DAYS} days, as of {today.isoformat()}. {cfg['instruction']}\n\n"
        f"{text_rows}\n\n"
        "Regards,\nRavasco Transmission and Packing Pvt Ltd.\n\n"
        "This is a system generated email. Please do not reply.\n"
    )
    return html_body, text_body


def _send_one(kind: str, today: datetime.date) -> dict:
    cfg = _REPORT_CONFIG[kind]
    rows, claimed_logs = _claim_expiring_licenses(kind, today)
    if not rows:
        return {"sent": False, "licenses": 0}

    recipients = _admin_emails() + [_FIXED_RECIPIENT]
    try:
        html_body, text_body = _render_expiry_email(kind, rows, today)
        subject = (
            f"[Purchase Tracker Admin Alert] Advance License — {cfg['label']} "
            f"Expiring in {_EXPIRY_WINDOW_DAYS} Days"
        )
        send_mail(
            subject=subject, message=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients, html_message=html_body, fail_silently=True,
        )
        log.info(
            "advance_license_report: sent %s expiry alert for %s license(s) to %s recipient(s)",
            kind, len(rows), len(recipients),
        )
        return {"sent": True, "licenses": len(rows)}
    except Exception:
        for log_row in claimed_logs:
            log_row.delete()
        log.exception("advance_license_report: failed to build/send %s expiry alert - claims released", kind)
        return {"sent": False, "licenses": 0, "error": True}


def send_advance_license_expiry_reports() -> dict:
    """Builds and sends both the Import and Export Advance License expiry
    alerts. Each is independent - one failing/having nothing to report
    never blocks the other, same best-effort convention as every other
    batch report in this app."""
    today = timezone.localdate()
    import_result = _send_one("import", today)
    export_result = _send_one("export", today)
    return {
        "date": today.isoformat(),
        "importSent": import_result["sent"],
        "importLicenses": import_result["licenses"],
        "exportSent": export_result["sent"],
        "exportLicenses": export_result["licenses"],
    }
