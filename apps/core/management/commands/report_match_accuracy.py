"""
apps/core/management/commands/report_match_accuracy.py - Match Accuracy
Programme, Phase 1 (doc 03, 1.3). Prints precision/recall/F1 over the review
screen's MatchReview rows (apps/api/routers/review_views.py), split by plant,
match type, tier and field-coverage band, so every fix in Phase 2 can be
measured against real evidence instead of assumed to have helped.

THE SCORING ITSELF LIVES IN apps/services/match_accuracy.py, not here
(extracted 2026-09-22 when the same figures got a management panel on
review.html). This command is now a renderer: it holds the terminal layout
and nothing else. Definitions of precision/recall/f1, the MIN_SAMPLE flag
and the "most recent verdict per match wins" rule are all documented in that
module - two implementations that could drift is exactly what this app
cannot afford for the only measured accuracy statement it makes.

Usage:
    python manage.py report_match_accuracy
"""

from django.core.management.base import BaseCommand

from apps.services.match_accuracy import MIN_SAMPLE, build_report


class Command(BaseCommand):
    help = "Report PO<->MIR<->Stock match precision/recall/F1 from reviewed MatchReview rows, split by plant, tier and field-coverage band."

    def handle(self, *args, **options):
        report = build_report(note_limit=0)
        if not report["reviewsRecorded"]:
            self.stdout.write(self.style.WARNING("No reviews recorded yet - use the review screen (/review) to build a sample first."))
            return

        self.stdout.write(self.style.SUCCESS(f"Match Accuracy Report - {report['reviewedMatches']} reviewed matches\n"))
        if report["staleVerdicts"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{report['staleVerdicts']} verdict(s) skipped - their match row no longer exists "
                    "(deleted or re-pointed by a later match_* run).\n"
                )
            )

        self._print_row("OVERALL", report["overall"])

        for heading, key in (
            ("\nBy plant:", "byPlant"),
            ("\nBy match type:", "byMatchType"),
            ("\nBy plant + match type:", "byPlantAndType"),
            ("\nBy match type + tier:", "byTier"),
            ("\nBy match type + field-coverage band:", "byCoverage"),
        ):
            self.stdout.write(heading)
            for row in report[key]:
                self._print_row("  " + row["label"], row)

    def _print_row(self, label, row):
        def fmt(v):
            return f"{v:.3f}" if v is not None else "n/a"

        flag = " (small sample)" if row["n"] < MIN_SAMPLE else ""
        self.stdout.write(
            f"{label:45s} n={row['n']:<4d} precision={fmt(row['precision'])} "
            f"recall={fmt(row['recall'])} f1={fmt(row['f1'])}{flag}"
        )
