"""
apps/core/management/commands/report_match_accuracy.py — Match Accuracy
Programme, Phase 1 (doc 03, 1.3). Turns the review screen's MatchReview rows
(apps/api/routers/review_views.py) into precision/recall/F1, split by tier
(po_number vs weighted) and by field-coverage band, so every fix in Phase 2
can be measured against real evidence instead of assumed to have helped.

Definitions (deliberately stated, since this screen only ever reviews rows
that already exist as a match - there is no signal here about a PO line item
that *should* have matched but didn't, so this is not textbook precision/
recall over the full space of possible matches):
  precision = correct / (correct + incorrect)   - of the matches a reviewer
              could confidently judge either way, how many were right.
  recall    = correct / (correct + incorrect + unsure) - of everything
              sampled, how many came back a confident "correct" (an "unsure"
              verdict counts against recall, not against precision, since it
              isn't evidence the match was wrong - just that a reviewer
              couldn't tell).
  f1        = harmonic mean of the two above.
A group with fewer than 5 reviews is shown but flagged - not enough sample
to trust yet.

Only the most recent verdict per (plant, match_type, match_id) is used, so a
reviewer correcting their own earlier call isn't stuck with it.

Usage:
    python manage.py report_match_accuracy
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.core.models import MatchReview, SyncRun
from apps.services.matching import MATCH_CONFIG as _HRS_CONFIG
from apps.services.matching_achhad import MATCH_CONFIG as _ACHHAD_CONFIG
from apps.services.matching_vapi import MATCH_CONFIG as _VAPI_CONFIG

_CONFIGS = {
    SyncRun.Plant.HRS: _HRS_CONFIG,
    SyncRun.Plant.RTP_ACHHAD: _ACHHAD_CONFIG,
    SyncRun.Plant.RTP_VAPI: _VAPI_CONFIG,
}

_MIN_SAMPLE = 5


def _model_for(config, match_type):
    if match_type == MatchReview.MatchType.PO_MIR:
        return config.po_mir_match_model
    if match_type == MatchReview.MatchType.IMPORT_PO_MIR:
        return config.import_po_mir_match_model
    return config.mir_stock_match_model


def _coverage_band(match):
    """field_coverage doesn't exist until fix 2.D lands (Match Accuracy
    Programme Phase 2) - bucket to the nearest 0.25 once it does, otherwise
    report a single "n/a" band so this command is usable from Phase 1
    onward rather than blocked on a later fix."""
    coverage = getattr(match, "field_coverage", None)
    if coverage is None:
        return "n/a"
    return f"{round(float(coverage) * 4) / 4:.2f}"


def _latest_verdicts():
    """One verdict per (plant, match_type, match_id) - the most recently
    reviewed one, so a reviewer's correction of their own earlier call
    wins."""
    latest = {}
    for review in MatchReview.objects.order_by("reviewed_at"):
        latest[(review.plant, review.match_type, review.match_id)] = review.verdict
    return latest


def _scores(counts):
    correct, incorrect, unsure = counts["correct"], counts["incorrect"], counts["unsure"]
    judged = correct + incorrect
    total = judged + unsure
    precision = correct / judged if judged else None
    recall = correct / total if total else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) else None
    return precision, recall, f1, total


class Command(BaseCommand):
    help = "Report PO<->MIR<->Stock match precision/recall/F1 from reviewed MatchReview rows, split by tier and field-coverage band."

    def handle(self, *args, **options):
        verdicts = _latest_verdicts()
        if not verdicts:
            self.stdout.write(self.style.WARNING("No reviews recorded yet - use the review screen (/review) to build a sample first."))
            return

        by_tier = defaultdict(lambda: defaultdict(int))
        by_coverage = defaultdict(lambda: defaultdict(int))
        by_match_type = defaultdict(lambda: defaultdict(int))
        overall = defaultdict(int)

        for (plant, match_type, match_id), verdict in verdicts.items():
            config = _CONFIGS.get(plant)
            if config is None:
                continue
            model = _model_for(config, match_type)
            match = model.objects.filter(id=match_id).first()
            if match is None:
                continue  # match row was deleted/reassigned since the review was recorded

            overall[verdict] += 1
            by_match_type[match_type][verdict] += 1
            tier = getattr(match, "tier", "n/a") or "n/a"
            by_tier[(match_type, tier)][verdict] += 1
            by_coverage[(match_type, _coverage_band(match))][verdict] += 1

        self.stdout.write(self.style.SUCCESS(f"Match Accuracy Report - {len(verdicts)} reviewed matches\n"))

        precision, recall, f1, total = _scores(overall)
        self._print_row("OVERALL", precision, recall, f1, total)

        self.stdout.write("\nBy match type:")
        for match_type, counts in sorted(by_match_type.items()):
            precision, recall, f1, total = _scores(counts)
            self._print_row(f"  {match_type}", precision, recall, f1, total)

        self.stdout.write("\nBy match type + tier:")
        for (match_type, tier), counts in sorted(by_tier.items()):
            precision, recall, f1, total = _scores(counts)
            self._print_row(f"  {match_type} / {tier}", precision, recall, f1, total)

        self.stdout.write("\nBy match type + field-coverage band:")
        for (match_type, band), counts in sorted(by_coverage.items()):
            precision, recall, f1, total = _scores(counts)
            self._print_row(f"  {match_type} / coverage {band}", precision, recall, f1, total)

    def _print_row(self, label, precision, recall, f1, total):
        def fmt(v):
            return f"{v:.3f}" if v is not None else "n/a"

        flag = " (small sample)" if total < _MIN_SAMPLE else ""
        self.stdout.write(f"{label:45s} n={total:<4d} precision={fmt(precision)} recall={fmt(recall)} f1={fmt(f1)}{flag}")
