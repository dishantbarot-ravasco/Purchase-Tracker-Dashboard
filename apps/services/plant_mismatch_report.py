"""
apps/services/plant_mismatch_report.py — Per-plant Data Correction email
(added 2026-09-08, project owner request) - sent individually to each
plant's own plant head, not to the internal admin list every other alert in
this app goes to (see security_alerts.py/consumption_report.py) - these are
the people who can actually fix the underlying source documents (the PO
extraction, the MIR register, the RM Stock sheet), not just review a
dashboard.

Lists every currently-flagged (not dismissed) PO<->MIR quantity/rate
mismatch and MIR<->Stock quantity/rate mismatch for that plant - the exact
same `qty_mismatched`/`rate_mismatched` booleans the dashboard's own Data
Quality Flags already read (see CLAUDE.md's "Identification/Financial-Check
redesign" and matching_core.py) - not a re-derived or looser definition.
"Whichever applicable" (project owner's own phrasing): a row only shows a
diff percentage for the mismatch that's actually true on it - a row that's
only qty_mismatched shows a rate column of "-", never a stale/zero-looking
number for something that wasn't actually flagged.

Deliberately its own small module, not folded into consumption_report.py or
security_alerts.py: this is neither a "how much stock moved" report (that's
consumption_report.py's concern) nor an internal admin-facing security
alert (security_alerts.py) - it's a plant-head-facing correction request
about the accuracy of THEIR OWN plant's source documents.

Recipients are fixed, real individuals - not derived from PTUser (none of
them necessarily has a login account here at all) and not the same list as
_admin_emails(). If a plant head's email ever changes, update _PLANT_HEADS
below directly.

**Deliberately has NO "This is a system generated email. Please do not
reply." footer**, unlike every other email in this app (see CLAUDE.md's
"Outgoing email inventory") - project owner's own explicit instruction for
this one: these emails ask for a real response/action (going to fix a
source document), so telling the recipient not to reply would work against
the entire point of sending it.
"""
from __future__ import annotations

import datetime
import html
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.utils import timezone

from apps.core.models import (
    HRSMirStockMatch,
    HRSPOMirMatch,
    RTPAchhadMirStockMatch,
    RTPAchhadPOMirMatch,
    RTPVapiMirStockMatch,
    RTPVapiPOMirMatch,
)
from apps.services.security_alerts import _admin_emails

log = logging.getLogger(__name__)

# Real individuals, per the project owner (2026-09-08) - see module docstring
# for why this is a fixed list, not derived from PTUser/_admin_emails().
_PLANT_HEADS = {
    "hrs": {
        "label": "HRS (Hindustan Rubbers, Silvassa)",
        "email": "avijit.ghosh@ravasco.com",
        "po_mir_match_model": HRSPOMirMatch,
        "mir_stock_match_model": HRSMirStockMatch,
    },
    "achhad": {
        "label": "RTP-Achhad (Ravasco Transmission and Packing, Achhad)",
        "email": "anil.khatri@ravasco.com",
        "po_mir_match_model": RTPAchhadPOMirMatch,
        "mir_stock_match_model": RTPAchhadMirStockMatch,
    },
    "vapi": {
        "label": "RTP-Vapi (Ravasco Transmission and Packing, Vapi)",
        "email": "mahendra.patil@ravasco.com",
        "po_mir_match_model": RTPVapiPOMirMatch,
        "mir_stock_match_model": RTPVapiMirStockMatch,
    },
}


def _display_name_from_email(email: str) -> str:
    """"mahendra.patil@ravasco.com" -> "Mahendra Patil" - these 3 recipients
    aren't PTUser accounts (see module docstring), so there's no full_name
    field to read the way every other email in this app greets its
    recipient by (e.g. device_service.py's `user.full_name or
    user.email.split("@")[0]`); derived from the email's own local-part
    instead."""
    local = email.split("@")[0]
    return " ".join(part.capitalize() for part in local.split("."))


def build_plant_mismatch_report(plant_key: str) -> dict:
    """Returns {'label', 'email', 'poMismatches': [...], 'stockMismatches': [...]}.
    poMismatches row: poNumber, mirNo, month, material, qtyDiffPct, rateDiffPct
    (either diff can be None - "whichever applicable", see module docstring).
    stockMismatches row: mirNo, month, material, qtyDiffPct, rateDiffPct.

    mirNo/month (added 2026-09-09, project owner request) come straight off
    the matched HRSMIREntry/RTPAchhadMIREntry/RTPVapiMIREntry row (every
    plant's own MIR model already carries both fields - no new data, no new
    sync needed) - lets the plant head jump straight to the exact MIR
    register page/month instead of searching by material/PO number alone.

    Only currently-flagged, NOT dismissed matches are included - a match an
    editor already reviewed and dismissed as fine has no business prompting
    the plant head to go "fix" something that was already accepted."""
    cfg = _PLANT_HEADS[plant_key]
    mismatch_filter = Q(qty_mismatched=True) | Q(rate_mismatched=True)

    po_rows = []
    po_matches = (
        cfg["po_mir_match_model"].objects
        .filter(mismatch_filter, dismissed_by_override=False)
        .select_related("po_line_item__purchase_order", "mir_entry")
    )
    for m in po_matches:
        po_rows.append({
            "poNumber": m.po_line_item.purchase_order.po_number,
            "mirNo": m.mir_entry.mir_no,
            "month": m.mir_entry.month,
            "material": m.po_line_item.description,
            "qtyDiffPct": float(m.qty_diff_pct) if m.qty_mismatched and m.qty_diff_pct is not None else None,
            "rateDiffPct": float(m.rate_diff_pct) if m.rate_mismatched and m.rate_diff_pct is not None else None,
        })
    po_rows.sort(key=lambda r: (r["poNumber"], r["material"]))

    stock_rows = []
    stock_matches = (
        cfg["mir_stock_match_model"].objects
        .filter(mismatch_filter, dismissed_by_override=False)
        .select_related("stock_lot", "mir_entry")
    )
    for m in stock_matches:
        stock_rows.append({
            "mirNo": m.mir_entry.mir_no,
            "month": m.mir_entry.month,
            "material": m.stock_lot.description,
            "qtyDiffPct": float(m.qty_diff_pct) if m.qty_mismatched and m.qty_diff_pct is not None else None,
            "rateDiffPct": float(m.rate_diff_pct) if m.rate_mismatched and m.rate_diff_pct is not None else None,
        })
    stock_rows.sort(key=lambda r: r["material"])

    return {
        "label": cfg["label"],
        "email": cfg["email"],
        "poMismatches": po_rows,
        "stockMismatches": stock_rows,
    }


def _pct_cell(value) -> str:
    return f"{value:.2f}%" if value is not None else "-"


def _text_cell(value: str) -> str:
    """month/mirNo are plain CharFields, blank ('') rather than None when
    the source MIR sheet's row never had one - same "-" placeholder
    convention as _pct_cell above, not an empty-looking table cell."""
    return value if value else "-"


def _render_mismatch_table_html(headers: list, rows: list, row_cells) -> str:
    head_html = "".join(f'<th style="padding:8px;text-align:left;border-bottom:2px solid #CBD5E0;">{html.escape(h)}</th>' for h in headers)
    body_html = "".join(
        "<tr>" + "".join(f'<td style="padding:8px;border-bottom:1px solid #E2E8F0;">{cell}</td>' for cell in row_cells(r)) + "</tr>"
        for r in rows
    )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:12px;color:#1A202C;margin-bottom:20px;">'
        f'<thead><tr style="background:#F7FAFC;">{head_html}</tr></thead><tbody>{body_html}</tbody></table>'
    )


def _render_plant_mismatch_email(report: dict, today: datetime.date) -> tuple:
    """Builds (html_body, text_body) for one plant head's Data Correction
    email. A bespoke small builder, like consumption_report.py's own - not
    apps/services/email_service.py's render_email(), which has no concept
    of tabular rows AND always appends the "system generated / do not
    reply" footer this email must NOT have (see module docstring)."""
    name = _display_name_from_email(report["email"])
    label = html.escape(report["label"])
    po_rows = report["poMismatches"]
    stock_rows = report["stockMismatches"]

    po_table_html = (
        _render_mismatch_table_html(
            ["PO Number", "MIR Number", "Month", "Material", "Qty Mismatch", "Rate Mismatch"], po_rows,
            lambda r: [
                html.escape(r["poNumber"]), html.escape(_text_cell(r["mirNo"])), html.escape(_text_cell(r["month"])),
                html.escape(r["material"]), _pct_cell(r["qtyDiffPct"]), _pct_cell(r["rateDiffPct"]),
            ],
        )
        if po_rows else '<p style="font-size:12.5px;color:#718096;">No open Purchase Order vs MIR mismatches right now.</p>'
    )
    stock_table_html = (
        _render_mismatch_table_html(
            ["MIR Number", "Month", "Material", "Qty Mismatch", "Rate Mismatch"], stock_rows,
            lambda r: [
                html.escape(_text_cell(r["mirNo"])), html.escape(_text_cell(r["month"])),
                html.escape(r["material"]), _pct_cell(r["qtyDiffPct"]), _pct_cell(r["rateDiffPct"]),
            ],
        )
        if stock_rows else '<p style="font-size:12.5px;color:#718096;">No open Raw Material Stock vs MIR mismatches right now.</p>'
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 16px;font-size:13px;color:#1A202C;">Hi {html.escape(name)},</p>
  <p style="margin:0 0 16px;font-size:13px;color:#4A5568;line-height:1.6;">
    The Purchase Tracker dashboard has flagged the following quantity/rate mismatches for
    {label} as of {today.isoformat()} - each one means the Purchase Order, MIR (Material Inward
    Register), or Raw Material Stock record disagrees with another for the same material.
    Kindly review and correct the source document(s) at your end where needed.
  </p>
  <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#1A202C;">Purchase Order vs MIR Mismatches</p>
  {po_table_html}
  <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#1A202C;">Raw Material Stock vs MIR Mismatches</p>
  {stock_table_html}
  <p style="margin:20px 0 0;font-size:11px;color:#718096;">
    A mismatch here clears on its own once the corrected figures are synced from the source
    document and this plant's reconciliation is re-run - no separate step needed on the
    dashboard side.
  </p>
  <p style="margin:20px 0 0;font-size:13px;color:#1A202C;line-height:1.6;">
    Regards,<br />Ravasco Transmission and Packing Pvt Ltd.
  </p>
</body>
</html>"""

    def _po_text_rows():
        if not po_rows:
            return "  (none)"
        return "\n".join(
            f'  - PO {r["poNumber"]}, MIR {_text_cell(r["mirNo"])} ({_text_cell(r["month"])}), {r["material"]}: '
            f'qty mismatch {_pct_cell(r["qtyDiffPct"])}, rate mismatch {_pct_cell(r["rateDiffPct"])}'
            for r in po_rows
        )

    def _stock_text_rows():
        if not stock_rows:
            return "  (none)"
        return "\n".join(
            f'  - MIR {_text_cell(r["mirNo"])} ({_text_cell(r["month"])}), {r["material"]}: '
            f'qty mismatch {_pct_cell(r["qtyDiffPct"])}, rate mismatch {_pct_cell(r["rateDiffPct"])}'
            for r in stock_rows
        )

    text_body = (
        f"Hi {name},\n\n"
        f"The Purchase Tracker dashboard has flagged the following quantity/rate mismatches for "
        f"{report['label']} as of {today.isoformat()} - each one means the Purchase Order, MIR "
        f"(Material Inward Register), or Raw Material Stock record disagrees with another for the "
        f"same material. Kindly review and correct the source document(s) at your end where needed.\n\n"
        f"Purchase Order vs MIR Mismatches\n{_po_text_rows()}\n\n"
        f"Raw Material Stock vs MIR Mismatches\n{_stock_text_rows()}\n\n"
        "A mismatch here clears on its own once the corrected figures are synced from the source "
        "document and this plant's reconciliation is re-run - no separate step needed on the "
        "dashboard side.\n\n"
        "Regards,\nRavasco Transmission and Packing Pvt Ltd.\n"
    )
    return html_body, text_body


def send_plant_mismatch_reports(*, test_recipient: str | None = None) -> dict:
    """Builds and emails each plant's Data Correction report individually to
    that plant's own head (see _PLANT_HEADS), **CC'ing every active admin**
    (project owner, 2026-09-08: "in all the emails except that sync, keep
    all the admins either in cc or direct mail" - "that sync" being
    security_alerts.py's notify_admins_sync_failure(), which stays a fixed
    single recipient by its own earlier, separate explicit request; every
    other email in this app already reaches every admin directly as its
    primary recipient, so this one CCs them instead of TO'ing them, since
    the plant head is the one actually being asked to act). Uses
    EmailMultiAlternatives directly (not the send_mail() wrapper every other
    email in this app uses) since send_mail() has no `cc` parameter. A plant
    with nothing currently flagged is skipped entirely (no email to the
    plant head OR the admins) rather than sending an empty "well done"
    notice - this email exists to prompt action, not to be a routine status
    ping.

    `test_recipient` (project owner, 2026-09-08: "just fire all the mails to
    me only for now this is testing") redirects ALL 3 plants' emails to that
    one address instead of the real plant heads, and drops the admin CC
    entirely (a test run must never leak into a real plant head's or a real
    admin's inbox) - real content, per-plant, still built and sent
    separately (not one combined email), just addressed differently.

    Best-effort per plant: one plant's report failing to build/send must
    not block the other two - same convention as
    send_daily_consumption_reports()/send_monthly_consumption_reports()."""
    today = timezone.localdate()
    admin_emails = [] if test_recipient else _admin_emails()
    sent = 0
    skipped_empty = 0
    for plant_key, cfg in _PLANT_HEADS.items():
        try:
            report = build_plant_mismatch_report(plant_key)
            if not report["poMismatches"] and not report["stockMismatches"]:
                skipped_empty += 1
                continue
            html_body, text_body = _render_plant_mismatch_email(report, today)
            to_email = test_recipient or cfg["email"]
            subject = f"[Purchase Tracker] Data Correction Needed - {cfg['label']} - {today.isoformat()}"
            if test_recipient:
                subject = f"[TEST] {subject}"
            message = EmailMultiAlternatives(
                subject=subject, body=text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                to=[to_email], cc=admin_emails,
            )
            message.attach_alternative(html_body, "text/html")
            message.send(fail_silently=True)
            sent += 1
            log.info(
                "send_plant_mismatch_reports: sent %s report to %s (cc %s admin(s))",
                plant_key, to_email, len(admin_emails),
            )
        except Exception:
            log.exception("send_plant_mismatch_reports: failed to build/send report for plant=%s", plant_key)

    return {"date": today.isoformat(), "plants_sent": sent, "plants_skipped_no_mismatches": skipped_empty}
