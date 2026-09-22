"""
apps/services/match_accuracy.py - the Match Accuracy Programme's scoring
layer (doc 03, 1.3), extracted 2026-09-22 so the management panel on
review.html and `manage.py report_match_accuracy` report the SAME numbers
from the SAME code. Before the panel existed this all lived inside the
management command; two implementations of precision/recall that could
drift is exactly the failure this app cannot afford here, since these
figures are the only measured statement it makes about whether its matches
are trustworthy at all (CLAUDE.md, "Match accuracy: manual validation is
required, not optional").

Definitions (deliberately stated, since only rows that ALREADY exist as a
match are ever reviewed - there is no signal here about a PO line item that
*should* have matched but didn't, so this is not textbook precision/recall
over the full space of possible matches):
  precision = correct / (correct + incorrect)   - of the matches a reviewer
              could confidently judge either way, how many were right.
  recall    = correct / (correct + incorrect + unsure) - of everything
              sampled, how many came back a confident "correct" (an "unsure"
              verdict counts against recall, not against precision, since it
              isn't evidence the match was wrong - just that a reviewer
              couldn't tell).
  f1        = harmonic mean of the two above.

A group with fewer than MIN_SAMPLE reviews is reported but flagged - not
enough sample to trust yet. Read that flag as load-bearing: a single
"incorrect" in a group of 2 is a precision of 0.500 that means nothing.

Only the most recent verdict per (plant, match_type, match_id) counts, so a
reviewer correcting their own earlier call isn't stuck with it.
"""

from collections import defaultdict

from apps.core.models import MatchReview, SyncRun
from apps.services.matching import MATCH_CONFIG as _HRS_CONFIG
from apps.services.matching_achhad import MATCH_CONFIG as _ACHHAD_CONFIG
from apps.services.matching_vapi import MATCH_CONFIG as _VAPI_CONFIG

CONFIGS = {
    SyncRun.Plant.HRS: _HRS_CONFIG,
    SyncRun.Plant.RTP_ACHHAD: _ACHHAD_CONFIG,
    SyncRun.Plant.RTP_VAPI: _VAPI_CONFIG,
}
PLANT_LABELS = {
    SyncRun.Plant.HRS: "HRS",
    SyncRun.Plant.RTP_ACHHAD: "RTP-Achhad",
    SyncRun.Plant.RTP_VAPI: "RTP-Vapi",
}
MATCH_TYPE_LABELS = {
    MatchReview.MatchType.PO_MIR: "PO ↔ MIR",
    MatchReview.MatchType.IMPORT_PO_MIR: "Import PO ↔ MIR",
    MatchReview.MatchType.MIR_STOCK: "MIR ↔ Stock",
}

MIN_SAMPLE = 5

# The programme's sample-size goal (doc 03, 1.2: "roughly 200 reviews spread
# across plants and tiers"). Exposed so the review screen can show progress
# against it rather than an open-ended counter - a reviewer with no idea how
# much is left has no reason to do the next five.
REVIEW_TARGET = 200


def model_for(config, match_type):
    if match_type == MatchReview.MatchType.PO_MIR:
        return config.po_mir_match_model
    if match_type == MatchReview.MatchType.IMPORT_PO_MIR:
        return config.import_po_mir_match_model
    return config.mir_stock_match_model


def coverage_band(match):
    """field_coverage doesn't exist until fix 2.D lands (Match Accuracy
    Programme Phase 2) - bucket to the nearest 0.25 once it does, otherwise
    report a single "n/a" band so this is usable from Phase 1 onward rather
    than blocked on a later fix."""
    coverage = getattr(match, "field_coverage", None)
    if coverage is None:
        return "n/a"
    return f"{round(float(coverage) * 4) / 4:.2f}"


def latest_verdicts():
    """One verdict per (plant, match_type, match_id) - the most recently
    reviewed one, so a reviewer's correction of their own earlier call
    wins."""
    latest = {}
    for review in MatchReview.objects.order_by("reviewed_at"):
        latest[(review.plant, review.match_type, review.match_id)] = review.verdict
    return latest


def scores(counts):
    correct, incorrect, unsure = counts["correct"], counts["incorrect"], counts["unsure"]
    judged = correct + incorrect
    total = judged + unsure
    precision = correct / judged if judged else None
    recall = correct / total if total else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) else None
    return precision, recall, f1, total


def _match_rows(verdicts):
    """Resolve every reviewed (plant, match_type, match_id) to its tier and
    coverage band, ONE QUERY PER (plant, match_type) GROUP rather than one
    per review. The per-review lookup this replaced was fine at 20 reviews
    and is 200+ queries at the programme's own target - the same avoidable
    shape as the RM Analysis render that had to be memoised later.

    A verdict whose match row no longer exists (the row was deleted or
    re-pointed by a later `match_*` run) is dropped and counted as stale -
    it is a verdict about a pair that no longer exists, so scoring it would
    be scoring the past."""
    wanted = defaultdict(set)
    for plant, match_type, match_id in verdicts:
        if plant in CONFIGS:
            wanted[(plant, match_type)].add(match_id)

    resolved = {}
    for (plant, match_type), ids in wanted.items():
        model = model_for(CONFIGS[plant], match_type)
        has_tier = any(f.name == "tier" for f in model._meta.get_fields())
        fields = ["id", "tier"] if has_tier else ["id"]
        for row in model.objects.filter(id__in=ids).only(*fields):
            resolved[(plant, match_type, row.id)] = {
                "tier": (getattr(row, "tier", None) or "n/a"),
                "coverage": coverage_band(row),
            }
    return resolved


def _group(counts_by_key, label_for):
    """Turn a {key: {verdict: n}} tally into the sorted list of scored rows
    the API and the CLI both render."""
    out = []
    for key, counts in counts_by_key.items():
        precision, recall, f1, total = scores(counts)
        out.append(
            {
                "key": key if isinstance(key, str) else list(key),
                "label": label_for(key),
                "correct": counts["correct"],
                "incorrect": counts["incorrect"],
                "unsure": counts["unsure"],
                "n": total,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "smallSample": total < MIN_SAMPLE,
            }
        )
    return sorted(out, key=lambda row: row["label"])


def build_report(note_limit=25):
    """The whole accuracy picture as plain data - what the management panel
    renders and what report_match_accuracy prints.

    `byPlantAndType` is the cut the CLI never had and management asked for
    first: one row per (plant, match type), which is where "our matches are
    85% right" stops being a single reassuring number and starts saying
    WHICH pairing at WHICH plant is the weak one. It doubles as the sampling
    map - a cell with n=0 has never been reviewed at all, and no amount of
    reviewing elsewhere says anything about it."""
    verdicts = latest_verdicts()
    resolved = _match_rows(verdicts)

    overall = defaultdict(int)
    by_plant = defaultdict(lambda: defaultdict(int))
    by_match_type = defaultdict(lambda: defaultdict(int))
    by_plant_type = defaultdict(lambda: defaultdict(int))
    by_tier = defaultdict(lambda: defaultdict(int))
    by_coverage = defaultdict(lambda: defaultdict(int))
    stale = 0

    for (plant, match_type, match_id), verdict in verdicts.items():
        row = resolved.get((plant, match_type, match_id))
        if row is None:
            stale += 1
            continue
        overall[verdict] += 1
        by_plant[plant][verdict] += 1
        by_match_type[match_type][verdict] += 1
        by_plant_type[(plant, match_type)][verdict] += 1
        by_tier[(match_type, row["tier"])][verdict] += 1
        by_coverage[(match_type, row["coverage"])][verdict] += 1

    # Every (plant, match type) cell exists in the report even at n=0 - an
    # unsampled cell is a hole in the evidence, and a table that simply
    # omits it reads as though the ground is covered.
    for plant in CONFIGS:
        for match_type in MatchReview.MatchType.values:
            by_plant_type[(plant, match_type)]  # noqa: B018 - defaultdict touch, materialises the cell

    reviewers = [
        {"reviewer": name or email, "count": count}
        for name, email, count in _reviewer_counts()
    ]
    notes = [
        {
            "plant": review.plant,
            "plantLabel": PLANT_LABELS.get(review.plant, review.plant),
            "matchType": review.match_type,
            "matchTypeLabel": MATCH_TYPE_LABELS.get(review.match_type, review.match_type),
            "matchId": review.match_id,
            "verdict": review.verdict,
            "note": review.note,
            "reviewer": review.reviewer.full_name or review.reviewer.email,
            "reviewedAt": review.reviewed_at.isoformat(),
        }
        for review in (
            MatchReview.objects.exclude(note="")
            .select_related("reviewer")
            .order_by("-reviewed_at")[:note_limit]
        )
    ]

    precision, recall, f1, total = scores(overall)
    last = MatchReview.objects.order_by("-reviewed_at").values_list("reviewed_at", flat=True).first()

    return {
        "target": REVIEW_TARGET,
        "minSample": MIN_SAMPLE,
        "reviewedMatches": total,
        "staleVerdicts": stale,
        "reviewsRecorded": MatchReview.objects.count(),
        "lastReviewedAt": last.isoformat() if last else None,
        "overall": {
            "correct": overall["correct"],
            "incorrect": overall["incorrect"],
            "unsure": overall["unsure"],
            "n": total,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "smallSample": total < MIN_SAMPLE,
        },
        "byPlant": _group(by_plant, lambda key: PLANT_LABELS.get(key, key)),
        "byMatchType": _group(by_match_type, lambda key: MATCH_TYPE_LABELS.get(key, key)),
        "byPlantAndType": _group(
            by_plant_type,
            lambda key: f"{PLANT_LABELS.get(key[0], key[0])} · {MATCH_TYPE_LABELS.get(key[1], key[1])}",
        ),
        "byTier": _group(by_tier, lambda key: f"{MATCH_TYPE_LABELS.get(key[0], key[0])} · {key[1]}"),
        "byCoverage": _group(by_coverage, lambda key: f"{MATCH_TYPE_LABELS.get(key[0], key[0])} · coverage {key[1]}"),
        "reviewers": reviewers,
        "notes": notes,
    }


def _reviewer_counts():
    """Who has reviewed how much. Not a leaderboard - it is the answer to
    "is this sample one person's judgement?", which changes how much the
    precision figure below it is worth."""
    rows = (
        MatchReview.objects.select_related("reviewer")
        .values_list("reviewer__full_name", "reviewer__email")
        .order_by()
    )
    tally = defaultdict(int)
    for full_name, email in rows:
        tally[(full_name, email)] += 1
    return sorted(
        ((name, email, count) for (name, email), count in tally.items()),
        key=lambda row: -row[2],
    )
