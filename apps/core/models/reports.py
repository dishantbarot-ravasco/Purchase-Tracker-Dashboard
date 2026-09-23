"""
apps/core/models/reports.py - Report dedup log (claim-before-send for the scheduled report emails).

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


class ReportSendLog(models.Model):
    """Dedup guard for the Daily/Monthly Raw Material Consumption reports
    (apps/services/consumption_report.py) - added 2026-09-10 after a
    full-codebase audit flagged that neither report had any protection
    against the external cron-job.org scheduler double-firing its trigger
    endpoint (a network retry, or a misconfigured overlapping schedule)
    would previously re-send every admin a duplicate email for the same
    day/month, with nothing to stop it.

    One row per (report_type, plant, period_key) actually sent - the unique
    constraint below is the actual guard: send_daily_consumption_reports()/
    send_monthly_consumption_reports() try to create a row for each plant
    BEFORE sending that plant's email, and skip sending (not an error, just
    a no-op) if the row already existed. `period_key` is the daily report's
    ISO date (e.g. "2026-09-10") or the monthly report's "YYYY-MM" - kept as
    a single opaque string column rather than separate date/year/month
    columns since the two report types never share a period shape and
    nothing here needs to query by date range.

    The Advance License expiry reports use '<license_number>@<validity date>'
    (widened from a bare license_number on 2026-09-22, project owner request).
    The date is IN the key deliberately: an Advance License's validity can be
    extended, sometimes more than once, and a bare-number key meant the very
    first alert burned that license forever - the extended date would then
    come and go in silence, which is precisely the case the alert exists for.
    Keying on the date being alerted on keeps the "never repeat while the
    same deadline is pending" guarantee (an unchanged date re-claims the same
    row on every daily run) while letting a genuinely NEW deadline alert once
    on its own merits. Import and export are already separate report_types,
    so each side of a license extends and re-alerts independently.

    Deliberately NOT applied to the Plant Data Correction (mismatch) report
    (apps/services/plant_mismatch_report.py) - project owner's explicit
    decision (2026-09-10): that report has no fixed cadence by design ("no
    built-in cadence; set whatever interval you want on the external
    scheduler" - see CLAUDE.md's "Outgoing email inventory"), so a
    once-per-day lock could block an intentional same-day re-trigger. Only
    the two reports with a real fixed cadence (once a day, once a month)
    get this guard."""

    class ReportType(models.TextChoices):
        DAILY = "daily", "Daily Consumption Report"
        MONTHLY = "monthly", "Monthly Consumption Report"
        ADV_LICENSE_IMPORT = "adv_import", "Advance License Import Validity Expiry"
        ADV_LICENSE_EXPORT = "adv_export", "Advance License Export Validity Expiry"

    report_type = models.CharField(max_length=10, choices=ReportType.choices)
    plant = models.CharField(max_length=20)
    period_key = models.CharField(
        max_length=64,
        help_text=(
            "Daily report: ISO date. Monthly report: 'YYYY-MM'. "
            "Advance License expiry reports: 'license_number@YYYY-MM-DD', the validity date "
            "being alerted on (plant is 'all')."
        ),
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pt_report_send_log"
        constraints = [
            models.UniqueConstraint(fields=["report_type", "plant", "period_key"], name="uniq_report_send_per_period")
        ]

    def __str__(self):
        return f"{self.report_type}:{self.plant}:{self.period_key}"
