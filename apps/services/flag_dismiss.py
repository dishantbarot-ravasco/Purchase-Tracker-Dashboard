"""
Shared logic behind "dismiss/reinstate a PO-level flag" - the Quantity/
Rate-Value Discrepancy critical flags and the Data Quality Flag category
shown in a PO detail modal's Flags & Corrections tab (Domestic: main.js's
computePoFlags()/categorizeFlag(); Import: apps/services/import_flags.py's
po_flags()). Byte-for-byte one implementation for all plants and both PO
types (Domestic/Import), same reasoning apps/services/match_dismiss.py
already uses for match dismissal - there's no per-plant variation to
protect here, just a different flag_key shape per PO type (see
FlagDismissal's own docstring).
"""

from django.utils import timezone

from apps.core.models import FlagDismissal


# ── Public API ───────────────────────────────────────────────────────────────

def dismiss_po_flag(plant, po_number, flag_key, user, dismissed: bool, reason: str):
    """Upserts the FlagDismissal row for (plant, po_number, flag_key).
    Returns the row. Clearing a dismissal (dismissed=False) also clears
    dismissed_by/dismissed_by_email/dismissed_reason/dismissed_at - same
    "no stale provenance for an undone decision" reasoning as
    match_dismiss.dismiss_match()."""
    defaults = {"dismissed": dismissed}
    if dismissed:
        defaults["dismissed_reason"] = reason or ""
        defaults["dismissed_by"] = user if getattr(user, "pk", None) else None
        defaults["dismissed_by_email"] = getattr(user, "email", "")
        defaults["dismissed_at"] = timezone.now()
    else:
        defaults["dismissed_reason"] = ""
        defaults["dismissed_by"] = None
        defaults["dismissed_by_email"] = ""
        defaults["dismissed_at"] = None

    obj, _ = FlagDismissal.objects.update_or_create(
        plant=plant, po_number=po_number, flag_key=flag_key, defaults=defaults,
    )
    return obj
