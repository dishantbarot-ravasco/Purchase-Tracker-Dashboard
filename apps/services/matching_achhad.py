"""
PO <-> MIR and MIR <-> Stock reconciliation for RTP-Achhad.

Same scoring approach as apps/core/matching.py's HRS implementation (see
that module's docstring for the full rationale) with one real difference:
Achhad's Stock sheet has no vendor column at all (confirmed - see
RTPAchhadStockLot's docstring), so MIR<->Stock matching here gates on
normalized material description alone, not (material, vendor). This is
a materially weaker guarantee than HRS's matcher and is flagged as such
wherever it matters, not silently reused as if it were equivalent.

PO <-> MIR (per RTPAchhadPOLineItem) is otherwise identical to HRS's:
vendor name is still a hard gate (Achhad's PO CSV and MIR both carry a
real vendor name field), scored the same 30/20/20/30 material/qty/rate/
value weighting.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    RTPAchhadImportPOMirMatch,
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadPOMirMatch,
    RTPAchhadStockLot,
)
from apps.services.parsers.common import normalize_material, normalize_vendor, tokenize

MATCH_THRESHOLD = Decimal("0.55")
# Zero tolerance (2026-09-04, project owner) - see matching.py's own
# comment on this same constant for the full reasoning; kept identical
# across all three plants on purpose.
FLAG_DIFF_PCT = Decimal("0")

_WEIGHT_MATERIAL = Decimal("0.30")
_WEIGHT_QTY = Decimal("0.20")
_WEIGHT_RATE = Decimal("0.20")
_WEIGHT_VALUE = Decimal("0.30")


# ── Internal scoring/gating helpers ─────────────────────────────────────────
# Byte-for-byte identical to matching.py's copies (a deliberate, documented
# duplication, not an oversight - see matching.py's own module docstring and
# test_matching.py's docstring for why only HRS's copy is directly
# unit-tested). Keep any fix mirrored across all three plants' files.

def _closeness(a, b) -> Decimal | None:
    """1.0 for an exact match, decaying linearly to 0 at a >=50% relative
    difference; None when either side is missing. See matching.py's own
    copy for the full rationale."""
    if a is None or b is None:
        return None
    a, b = Decimal(a), Decimal(b)
    if a == 0 and b == 0:
        return Decimal("1")
    denom = max(abs(a), abs(b))
    if denom == 0:
        return Decimal("1")
    diff_ratio = abs(a - b) / denom
    return max(Decimal("0"), Decimal("1") - diff_ratio * 2)


_MAX_DIFF_PCT = Decimal("9999.99")  # *_diff_pct columns are DecimalField(max_digits=6, decimal_places=2)


def _diff_pct(a, b) -> Decimal | None:
    """Percent difference of b relative to a, or None if either side is
    missing (never flaggable). Found and fixed here first, then ported to
    matching.py/matching_vapi.py identically - see CLAUDE.md's "*_diff_pct
    columns need a clamp" section."""
    if a is None or b is None:
        return None
    a, b = Decimal(a), Decimal(b)
    if a == 0:
        return None if b == 0 else _MAX_DIFF_PCT
    # A tiny `a` against a much larger `b` (e.g. MIR rate typo'd as a
    # fraction of the real value) can blow past the column's own
    # precision - clamp rather than let the DB insert raise, since the
    # column can't distinguish "huge" from "astronomically huge" anyway.
    return min(abs(a - b) / abs(a) * Decimal("100"), _MAX_DIFF_PCT)


def _token_overlap(a: str, b: str) -> Decimal:
    """Jaccard similarity of the two strings' normalized token sets - the
    30%-weighted material-description factor. 0 (not a ZeroDivisionError)
    when either side has no tokens at all."""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return Decimal("0")
    return Decimal(len(ta & tb)) / Decimal(len(ta | tb))


def _vendor_matches(a: str, b: str) -> bool:
    """Containment, not equality, so a MIR/PO vendor name still matches a
    Stock-sheet vendor name carrying an extra suffix (city, branch, etc.) -
    see matching.py's own copy for the concrete example that forced this.
    Hard gate either way: vendor never contributes a partial/scored value."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter in longer


def _po_number_matches(po_number: str, po_number_raw: str) -> bool:
    """Exact match or substring - MIR's own PO-number field is free text,
    not a guaranteed clean value (some rows type two PO numbers together)."""
    if not po_number or not po_number_raw:
        return False
    return po_number.strip().upper() in po_number_raw.strip().upper()


# ── Public API ───────────────────────────────────────────────────────────────

def match_po_mir_line_item(po_line_item) -> "RTPAchhadPOMirMatch | None":
    """Finds the best MIR match for one RTPAchhadPOLineItem and upserts
    RTPAchhadPOMirMatch, or deletes any existing match if nothing clears
    MATCH_THRESHOLD. Same tier-1/tier-2 approach as matching.py's HRS
    version - see that module's docstring for the full rationale."""
    po = po_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)

    candidates = list(
        RTPAchhadMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name="")
    )
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), RTPAchhadPOMirMatch.Tier.PO_NUMBER
    else:
        for mir in candidates:
            material_score = _token_overlap(po_line_item.description, mir.material_description)
            qty_score = _closeness(po_line_item.qty, mir.qty) or Decimal("0")
            rate_score = _closeness(po_line_item.net_price, mir.rate) or Decimal("0")
            value_score = _closeness(po_line_item.net_value, mir.taxable_value or mir.net) or Decimal("0")

            score = (
                material_score * _WEIGHT_MATERIAL
                + qty_score * _WEIGHT_QTY
                + rate_score * _WEIGHT_RATE
                + value_score * _WEIGHT_VALUE
            )
            if score > best_score:
                best_entry, best_score, best_tier = mir, score, RTPAchhadPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        RTPAchhadPOMirMatch.objects.filter(po_line_item=po_line_item).delete()
        return None

    qty_diff = _diff_pct(po_line_item.qty, best_entry.qty)
    rate_diff = _diff_pct(po_line_item.net_price, best_entry.rate)
    value_diff = _diff_pct(po_line_item.net_value, best_entry.taxable_value or best_entry.net)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = RTPAchhadPOMirMatch.objects.update_or_create(
        po_line_item=po_line_item,
        defaults=dict(
            mir_entry=best_entry,
            tier=best_tier,
            match_score=best_score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
        ),
    )
    return match


def _import_rate_value_inr(import_line_item) -> tuple[Decimal | None, Decimal | None]:
    """Import line items are priced in the PO's own currency, but MIR's
    `rate`/`taxable_value`/`net` are always INR - see matching_vapi.py's
    own copy of this function (found and fixed there first, against real
    Vapi import data, then ported here identically) for the full reasoning
    and why this must run before any MIR comparison, not be optional."""
    exchange_rate = import_line_item.exchange_rate
    if import_line_item.net_price is None:
        rate_inr = None
    elif exchange_rate is not None:
        rate_inr = import_line_item.net_price * exchange_rate
    else:
        rate_inr = import_line_item.net_price

    if import_line_item.total_inclusive_value is not None:
        value_inr = import_line_item.total_inclusive_value
    elif import_line_item.net_value is not None and exchange_rate is not None:
        value_inr = import_line_item.net_value * exchange_rate
    else:
        value_inr = import_line_item.net_value

    return rate_inr, value_inr


def match_import_po_mir_line_item(import_line_item) -> "RTPAchhadImportPOMirMatch | None":
    """Same shape as match_po_mir_line_item() - see matching.py's
    match_import_po_mir_line_item() for the full reasoning (MIR is shared
    across domestic/import, qty compared is qty_as_per_boe not
    qty_as_per_po). See _import_rate_value_inr() for the currency
    conversion this needs that domestic matching doesn't."""
    po = import_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)
    rate_inr, value_inr = _import_rate_value_inr(import_line_item)

    candidates = list(
        RTPAchhadMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name="")
    )
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), RTPAchhadImportPOMirMatch.Tier.PO_NUMBER
    else:
        for mir in candidates:
            material_score = _token_overlap(import_line_item.description, mir.material_description)
            qty_score = _closeness(import_line_item.qty_as_per_boe, mir.qty) or Decimal("0")
            rate_score = _closeness(rate_inr, mir.rate) or Decimal("0")
            value_score = _closeness(value_inr, mir.taxable_value or mir.net) or Decimal("0")

            score = (
                material_score * _WEIGHT_MATERIAL
                + qty_score * _WEIGHT_QTY
                + rate_score * _WEIGHT_RATE
                + value_score * _WEIGHT_VALUE
            )
            if score > best_score:
                best_entry, best_score, best_tier = mir, score, RTPAchhadImportPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        RTPAchhadImportPOMirMatch.objects.filter(po_line_item=import_line_item).delete()
        return None

    qty_diff = _diff_pct(import_line_item.qty_as_per_boe, best_entry.qty)
    rate_diff = _diff_pct(rate_inr, best_entry.rate)
    value_diff = _diff_pct(value_inr, best_entry.taxable_value or best_entry.net)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = RTPAchhadImportPOMirMatch.objects.update_or_create(
        po_line_item=import_line_item,
        defaults=dict(
            mir_entry=best_entry,
            tier=best_tier,
            match_score=best_score.quantize(Decimal("0.0001")),
            qty_diff_pct=qty_diff,
            rate_diff_pct=rate_diff,
            value_diff_pct=value_diff,
            is_flagged=is_flagged,
        ),
    )
    return match


def match_mir_entry_stock(mir_entry) -> list["RTPAchhadMirStockMatch"]:
    """No vendor gate - see module docstring. Gated on normalized material
    description alone, exact match only (not fuzzy/token-overlap), to keep
    the false-positive rate from this weaker gate as low as reasonably
    possible."""
    mir_material = normalize_material(mir_entry.material_description)
    if not mir_material:
        return []

    candidates = [
        lot for lot in RTPAchhadStockLot.objects.filter(is_active=True).exclude(description="")
        if normalize_material(lot.description) == mir_material
    ]

    matches = []
    matched_lot_ids = set()
    for lot in candidates:
        rate_diff = _diff_pct(mir_entry.rate, lot.rate)
        is_flagged = rate_diff is not None and rate_diff > FLAG_DIFF_PCT
        match, _ = RTPAchhadMirStockMatch.objects.update_or_create(
            mir_entry=mir_entry,
            stock_lot=lot,
            defaults=dict(qty_diff_pct=None, rate_diff_pct=rate_diff, is_flagged=is_flagged),
        )
        matches.append(match)
        matched_lot_ids.add(lot.id)

    RTPAchhadMirStockMatch.objects.filter(mir_entry=mir_entry).exclude(stock_lot_id__in=matched_lot_ids).delete()
    return matches


@transaction.atomic
def run_full_match() -> dict:
    """Re-runs both matching passes over every Achhad record - domestic and
    import PO line items (both against the shared RTPAchhadMIREntry table),
    plus MIR<->Stock. Idempotent upserts, safe to call repeatedly; wired to
    match_achhad and the sync-trigger pipeline."""
    from apps.core.models import RTPAchhadImportPOLineItem, RTPAchhadPOLineItem

    po_matched = 0
    for item in RTPAchhadPOLineItem.objects.select_related("purchase_order").all():
        if match_po_mir_line_item(item) is not None:
            po_matched += 1

    import_po_matched = 0
    for item in RTPAchhadImportPOLineItem.objects.select_related("purchase_order").all():
        if match_import_po_mir_line_item(item) is not None:
            import_po_matched += 1

    # See matching.py's run_full_match() for why this cleanup is needed -
    # deactivated MIR entries are skipped below, so their own
    # RTPAchhadMirStockMatch rows would otherwise never get dropped.
    RTPAchhadMirStockMatch.objects.filter(mir_entry__is_active=False).delete()

    mir_matched = 0
    for entry in RTPAchhadMIREntry.objects.filter(is_active=True):
        if match_mir_entry_stock(entry):
            mir_matched += 1

    return {
        "po_line_items_matched": po_matched,
        "import_po_line_items_matched": import_po_matched,
        "mir_entries_stock_matched": mir_matched,
        "ran_at": timezone.now(),
    }
