"""
apps/services/match_pairs.py - the identity of a match for the human
decisions recorded about it: review verdicts (MatchReview) and dismissals
(MatchDismissal).

A match row's own id is not a usable identity. run_full_match() deletes a
*POMirMatch row when its line matches nothing on a run and creates a new one
(new id) when the same pair matches again; a MIR<->Stock row is deleted
whenever the pair drops out. A verdict or dismissal pointing at that id was
silently lost. The two rows a match joins are stable instead - a PO line is
updated in place (sync_utils.sync_line_items()), MIR entries and stock lots
are deactivated, never deleted - so a decision is keyed on (left_id,
right_id):

  po_mir, import_po_mir   left = the PO line item   right = the primary MIR entry
  mir_stock               left = the MIR entry      right = the stock lot

Left/right is the same orientation as the review card's two sides.
"""

from collections import defaultdict

from apps.core.models import MatchDismissal, MatchReview

MatchType = MatchReview.MatchType

PAIR_FIELDS = {
    MatchType.PO_MIR: ("po_line_item_id", "mir_entry_id"),
    MatchType.IMPORT_PO_MIR: ("po_line_item_id", "mir_entry_id"),
    MatchType.MIR_STOCK: ("mir_entry_id", "stock_lot_id"),
}


def pair_of(match_type, match) -> tuple:
    left, right = PAIR_FIELDS[match_type]
    return getattr(match, left), getattr(match, right)


def rows_for_pairs(model, match_type, pairs, queryset=None) -> dict:
    """{(left_id, right_id): match row} for the pairs that currently exist
    in `model`. One query, filtered on the left side and checked against
    the right in Python - a line item or MIR entry has at most a handful of
    rows, so this stays proportional to `pairs`."""
    pairs = {p for p in pairs if p[0] is not None and p[1] is not None}
    if not pairs:
        return {}
    left, _right = PAIR_FIELDS[match_type]
    qs = queryset if queryset is not None else model.objects.all()
    rows = qs.filter(**{f"{left}__in": {p[0] for p in pairs}})
    out = {}
    for row in rows:
        key = pair_of(match_type, row)
        if key in pairs:
            out[key] = row
    return out


def restore_dismissals(config, match_types) -> int:
    """Copy each MatchDismissal of this plant back onto the match row that
    currently holds its pair, where that row is not already dismissed.
    Called by run_full_match() after it writes the rows of `match_types`,
    inside its transaction. Returns how many rows were restored.

    Only undismissed rows are touched, so a steady-state run (every
    dismissed pair still on its original row) writes nothing."""
    models_by_type = {
        MatchType.PO_MIR: config.po_mir_match_model,
        MatchType.IMPORT_PO_MIR: config.import_po_mir_match_model,
        MatchType.MIR_STOCK: config.mir_stock_match_model,
    }
    by_type = defaultdict(dict)
    for dismissal in MatchDismissal.objects.filter(plant=config.syncrun_plant, match_type__in=list(match_types)):
        by_type[dismissal.match_type][(dismissal.left_id, dismissal.right_id)] = dismissal

    restored = 0
    for match_type, dismissals in by_type.items():
        model = models_by_type[match_type]
        if model is None:
            continue
        rows =rows_for_pairs(model, match_type, dismissals, model.objects.filter(dismissed_by_override=False))
        for pair, row in rows.items():
            dismissal = dismissals[pair]
            model.objects.filter(pk=row.pk).update(
                dismissed_by_override=True,
                dismissed_by=dismissal.dismissed_by_id,
                dismissed_at=dismissal.dismissed_at,
                dismissed_reason=dismissal.dismissed_reason,
            )
            restored += 1
    return restored
