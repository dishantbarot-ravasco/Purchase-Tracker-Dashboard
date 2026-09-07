"""
apps/services/data_quality.py — the DB-touching layer behind
apps/services/arithmetic_checks.py's pure check functions (Match Accuracy
Programme, fix 3.G). Kept separate from arithmetic_checks.py so that module
can stay fully dependency-free (no Django imports at all), same reasoning
apps/services/parsers/common.py's own module docstring gives for its own
pure functions.
"""

from apps.core.models import DataQualityFlag


def sync_data_quality_flags(plant: str, source_type: str, results: dict) -> None:
    """Upserts a DataQualityFlag for every source_id whose check returned a
    mismatch, and deletes any existing flag for a source_id that no longer
    mismatches (fixed in the source sheet, or the row no longer exists) -
    keeps this table representing current reality, not an ever-growing log.

    `results` maps source_id -> mismatch dict-or-None, one entry per
    (source_id, check_name) pair actually run this sync (a check that
    returned None because a required field was missing is still a "no
    flag" result, so its stale flag - if any - still gets cleared).
    """
    flagged_keys = set()
    for source_id, mismatch in results.items():
        if mismatch is None:
            continue
        check_name = mismatch["check_name"]
        flagged_keys.add((source_id, check_name))
        DataQualityFlag.objects.update_or_create(
            plant=plant,
            source_type=source_type,
            source_id=source_id,
            check_name=check_name,
            defaults={"expected": mismatch["expected"], "actual": mismatch["actual"]},
        )

    existing = DataQualityFlag.objects.filter(plant=plant, source_type=source_type)
    stale = [
        flag.id for flag in existing
        if (flag.source_id, flag.check_name) not in flagged_keys
    ]
    if stale:
        DataQualityFlag.objects.filter(id__in=stale).delete()
