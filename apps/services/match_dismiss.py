"""
Shared logic behind the "dismiss a flagged match" endpoints (PO<->MIR and
MIR<->Stock), for all three plants.

Dismissing a match is byte-for-byte identical across HRS/Achhad/Vapi (only
the model class differs) - unlike matching.py/matching_achhad.py/
matching_vapi.py's own deliberate per-plant duplication (that duplication
exists because the *scoring* genuinely differs per plant - see matching.py's
module docstring). There's no such per-plant variation here, so this one
module is shared and each plant's hrs_views.py/achhad_views.py/vapi_views.py
just wires it to that plant's own match model classes.
"""

from django.utils import timezone


def dismiss_match(model_cls, match_id, user, dismissed: bool, reason: str):
    """Set/clear dismissed_by_override on a *POMirMatch or *MirStockMatch
    row. Returns the updated instance, or None if match_id doesn't exist.

    Clearing a dismissal (dismissed=False) also clears dismissed_by/
    dismissed_at/dismissed_reason - a match that's no longer dismissed
    shouldn't keep showing who dismissed it and when, that would read as
    still-active provenance for a decision that's been undone."""
    match = model_cls.objects.filter(pk=match_id).first()
    if not match:
        return None

    match.dismissed_by_override = dismissed
    if dismissed:
        match.dismissed_reason = reason or ""
        match.dismissed_by = user if getattr(user, "pk", None) else None
        match.dismissed_at = timezone.now()
    else:
        match.dismissed_reason = ""
        match.dismissed_by = None
        match.dismissed_at = None

    match.save(update_fields=["dismissed_by_override", "dismissed_reason", "dismissed_by", "dismissed_at"])
    return match
