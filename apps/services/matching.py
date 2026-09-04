"""
PO <-> MIR and MIR <-> Stock reconciliation for HRS.

Vendor is always a hard gate, never scored - two records for different
vendors are never candidates for each other, no matter how well material/
amount line up. Everything else is a weighted score used only to rank
candidates that already passed the vendor gate.

PO <-> MIR (per HRSPOLineItem):
  Tier 1 - PO_NUMBER: mir.po_number_raw resolves to this exact PO (see
    _po_number_matches). HRS's own PO-number field is unreliable (~30%
    blank, ~25% non-standard - see HRSMIREntry.po_number_raw docstring) so
    this is a bonus shortcut, never the only path.
  Tier 2 - WEIGHTED, scored against every vendor-gated MIR candidate:
    material description token overlap  30%
    qty closeness                       20%
    rate closeness                      20%
    total/final value closeness         30%
  The best-scoring candidate above MATCH_THRESHOLD wins; below that, the
  line item is left unmatched rather than forced onto a poor candidate.

MIR <-> Stock (per HRSMIREntry):
  Candidates are gated on (material description, vendor) together, not
  material alone - HRS's real Stock sheet has one row per (material,
  vendor lot), e.g. six separate "SBR 1502" rows from six different
  vendors, so material-only matching would pick a plausible-looking but
  wrong lot. Scored purely on qty/rate closeness among the gated set.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    HRSImportPOMirMatch,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSStockLot,
)
from apps.services.parsers.common import normalize_material, normalize_vendor, tokenize

MATCH_THRESHOLD = Decimal("0.55")
# Zero tolerance (2026-09-04, project owner) - ANY nonzero qty/rate/value
# diff on an already-matched pair flags it, even 1kg out of 1000kg. This
# supersedes the earlier "5%, deliberately not the artifact's 10%" decision
# (see CLAUDE.md) - kept as a named constant rather than inlining `> 0`
# everywhere it's used, so a future policy change again still has exactly
# one place to edit. `d > FLAG_DIFF_PCT` with this at 0 means "not exactly
# equal", since _diff_pct() only ever returns None (a side is missing,
# never flagged) or a value >= 0 (0 itself means an exact match).
FLAG_DIFF_PCT = Decimal("0")

_WEIGHT_MATERIAL = Decimal("0.30")
_WEIGHT_QTY = Decimal("0.20")
_WEIGHT_RATE = Decimal("0.20")
_WEIGHT_VALUE = Decimal("0.30")


def _closeness(a, b) -> Decimal | None:
    """1.0 for an exact match, decaying linearly to 0 at a >=50% relative
    difference. None (treated as 0 in scoring) when either side is missing -
    a blank field should never look like a perfect match."""
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
    if a is None or b is None:
        return None
    a, b = Decimal(a), Decimal(b)
    if a == 0:
        return None if b == 0 else _MAX_DIFF_PCT
    # A tiny `a` against a much larger `b` (e.g. a rate typo'd as a fraction
    # of the real value) can blow past the column's own precision - clamp
    # rather than let the DB insert raise, since the column can't
    # distinguish "huge" from "astronomically huge" anyway. Ported from the
    # same fix in matching_achhad.py, found there first.
    return min(abs(a - b) / abs(a) * Decimal("100"), _MAX_DIFF_PCT)


def _token_overlap(a: str, b: str) -> Decimal:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return Decimal("0")
    return Decimal(len(ta & tb)) / Decimal(len(ta | tb))


def _vendor_matches(a: str, b: str) -> bool:
    """Containment, not equality - HRS's Stock sheet appends a city suffix
    to its party_name that neither MIR nor the PO CSV carry (confirmed:
    'Rubamin Private Limited' in MIR/PO vs 'Rubamin Private Limited -
    Vadodara' in Stock, both normalizing to 'rubamin...' with no exact
    match). The shorter normalized name appearing inside the longer one
    catches this without loosening the gate into a fuzzy/scored check -
    vendor stays a hard yes/no, just not a strict string equality.
    A length floor avoids a short normalized name trivially matching
    everything (e.g. an empty or near-empty normalization)."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter in longer


def _po_number_matches(po_number: str, po_number_raw: str) -> bool:
    """MIR's PO-number field is free text, not a guaranteed clean value -
    accept an exact match or the PO number appearing as a substring (some
    rows are typed as e.g. 'HRS/HO/26-27/003 & 004')."""
    if not po_number or not po_number_raw:
        return False
    return po_number.strip().upper() in po_number_raw.strip().upper()


def match_po_mir_line_item(po_line_item) -> "HRSPOMirMatch | None":
    """Finds the best MIR match for one HRSPOLineItem and upserts
    HRSPOMirMatch, or deletes any existing match if nothing clears the
    threshold. Returns the resulting match (or None)."""
    po = po_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)

    candidates = list(HRSMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name=""))
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), HRSPOMirMatch.Tier.PO_NUMBER
    else:
        for mir in candidates:
            material_score = _token_overlap(po_line_item.description, mir.material_description)
            qty_score = _closeness(po_line_item.qty, mir.qty) or Decimal("0")
            rate_score = _closeness(po_line_item.net_price, mir.rate) or Decimal("0")
            # PO's net_value is pre-tax; MIR's taxable_value is the pre-tax
            # equivalent on that side (invoice_final_value/total_amount are
            # post-GST/TCS and would make an exact qty+rate match look like
            # an ~18% "discrepancy" purely from tax - confirmed against real
            # rows this session).
            value_score = _closeness(po_line_item.net_value, mir.taxable_value or mir.net) or Decimal("0")

            score = (
                material_score * _WEIGHT_MATERIAL
                + qty_score * _WEIGHT_QTY
                + rate_score * _WEIGHT_RATE
                + value_score * _WEIGHT_VALUE
            )
            if score > best_score:
                best_entry, best_score, best_tier = mir, score, HRSPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        HRSPOMirMatch.objects.filter(po_line_item=po_line_item).delete()
        return None

    qty_diff = _diff_pct(po_line_item.qty, best_entry.qty)
    rate_diff = _diff_pct(po_line_item.net_price, best_entry.rate)
    value_diff = _diff_pct(po_line_item.net_value, best_entry.taxable_value or best_entry.net)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = HRSPOMirMatch.objects.update_or_create(
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


def match_import_po_mir_line_item(import_line_item) -> "HRSImportPOMirMatch | None":
    """Same shape as match_po_mir_line_item() (see that function and
    HRSImportPOMirMatch's docstring) - the only real differences are the
    qty field (`qty_as_per_boe`, not `qty_as_per_po` - see the model
    docstring for why), the currency conversion _import_rate_value_inr()
    does before scoring, and the match model class. Matches against the
    same HRSMIREntry table domestic matching uses; MIR is a shared Drive
    file across domestic and import purchases for a plant."""
    po = import_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)
    rate_inr, value_inr = _import_rate_value_inr(import_line_item)

    candidates = list(HRSMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name=""))
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), HRSImportPOMirMatch.Tier.PO_NUMBER
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
                best_entry, best_score, best_tier = mir, score, HRSImportPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        HRSImportPOMirMatch.objects.filter(po_line_item=import_line_item).delete()
        return None

    qty_diff = _diff_pct(import_line_item.qty_as_per_boe, best_entry.qty)
    rate_diff = _diff_pct(rate_inr, best_entry.rate)
    value_diff = _diff_pct(value_inr, best_entry.taxable_value or best_entry.net)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = HRSImportPOMirMatch.objects.update_or_create(
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


def match_mir_entry_stock(mir_entry) -> list["HRSMirStockMatch"]:
    """Finds every Stock lot that (material, vendor)-matches one MIR entry
    and upserts HRSMirStockMatch for each - a many-to-many relationship on
    purpose, since HRS receives the same material from the same vendor
    across multiple lots over time."""
    mir_material = normalize_material(mir_entry.material_description)
    mir_vendor = normalize_vendor(mir_entry.party_name)
    if not mir_material or not mir_vendor:
        return []

    candidates = [
        lot for lot in HRSStockLot.objects.filter(is_active=True).exclude(description="").exclude(party_name="")
        if normalize_material(lot.description) == mir_material
        and _vendor_matches(normalize_vendor(lot.party_name), mir_vendor)
    ]

    matches = []
    matched_lot_ids = set()
    for lot in candidates:
        # lot.received ("REC" in the sheet) is a formula column that reads 0
        # for nearly every real lot - it evidently clears once allocated
        # rather than holding a running total comparable to one MIR line's
        # qty, so qty is not compared here (confirmed against real data this
        # session: rate matched exactly in cases where "qty diff" would have
        # read as 100%). Rate is the only field this pairing can reliably
        # check.
        rate_diff = _diff_pct(mir_entry.rate, lot.basic_rate)
        is_flagged = rate_diff is not None and rate_diff > FLAG_DIFF_PCT
        match, _ = HRSMirStockMatch.objects.update_or_create(
            mir_entry=mir_entry,
            stock_lot=lot,
            defaults=dict(qty_diff_pct=None, rate_diff_pct=rate_diff, is_flagged=is_flagged),
        )
        matches.append(match)
        matched_lot_ids.add(lot.id)

    HRSMirStockMatch.objects.filter(mir_entry=mir_entry).exclude(stock_lot_id__in=matched_lot_ids).delete()
    return matches


@transaction.atomic
def run_full_match() -> dict:
    """Re-runs both matching passes over every HRS record - domestic PO
    line items AND import PO line items (both matched against the same
    shared HRSMIREntry table), plus MIR<->Stock. Safe to call repeatedly
    (idempotent upserts) - intended to run after each sync."""
    from apps.core.models import HRSImportPOLineItem, HRSPOLineItem

    po_matched = 0
    for item in HRSPOLineItem.objects.select_related("purchase_order").all():
        if match_po_mir_line_item(item) is not None:
            po_matched += 1

    import_po_matched = 0
    for item in HRSImportPOLineItem.objects.select_related("purchase_order").all():
        if match_import_po_mir_line_item(item) is not None:
            import_po_matched += 1

    # Deactivated MIR entries (see HRSMIREntry.is_active) are skipped by the
    # loop below, so match_mir_entry_stock() never runs for them to clean up
    # its own HRSMirStockMatch rows - drop those explicitly instead of
    # leaving them to reference a now-inactive entry forever.
    HRSMirStockMatch.objects.filter(mir_entry__is_active=False).delete()

    mir_matched = 0
    for entry in HRSMIREntry.objects.filter(is_active=True):
        if match_mir_entry_stock(entry):
            mir_matched += 1

    return {
        "po_line_items_matched": po_matched,
        "import_po_line_items_matched": import_po_matched,
        "mir_entries_stock_matched": mir_matched,
        "ran_at": timezone.now(),
    }
