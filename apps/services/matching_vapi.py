"""
PO <-> MIR and MIR <-> Stock reconciliation for RTP-Vapi.

Same scoring approach as apps/core/matching.py's HRS implementation (see
that module's docstring for the full rationale). Uses HRS's stronger
(material, vendor) gate for MIR<->Stock, not Achhad's material-only gate -
confirmed this session that Vapi's Stock sheet has a real 'Supplier Name'
column (see the RTP-Vapi section header comment in apps/core/models.py for
how that was verified).

Two real differences from HRS's PO<->MIR matching:
  - Vapi's MIR has no "Net" column - taxable_value is used directly as the
    value-closeness comparator against the PO line item's net_value (both
    pre-tax), instead of HRS's `mir.taxable_value or mir.net` fallback
    (Vapi's RTPVapiMIREntry has no `net` field to fall back to at all).
  - RTPVapiMIREntry.po_number_raw (MIR's own "SAP P.O" column) was empty on
    every real row confirmed this session, so the tier-1 PO-number shortcut
    below will not actually fire against current data - it's still wired up
    (not deleted) so it starts working automatically the day that column
    gets populated, same reasoning as keeping the model field at all.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    RTPVapiImportPOMirMatch,
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiPOMirMatch,
    RTPVapiStockLot,
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


def _closeness(a, b) -> Decimal | None:
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
    return min(abs(a - b) / abs(a) * Decimal("100"), _MAX_DIFF_PCT)


def _token_overlap(a: str, b: str) -> Decimal:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return Decimal("0")
    return Decimal(len(ta & tb)) / Decimal(len(ta | tb))


def _vendor_matches(a: str, b: str) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter in longer


def _po_number_matches(po_number: str, po_number_raw: str) -> bool:
    if not po_number or not po_number_raw:
        return False
    return po_number.strip().upper() in po_number_raw.strip().upper()


def match_po_mir_line_item(po_line_item) -> "RTPVapiPOMirMatch | None":
    po = po_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)

    candidates = list(
        RTPVapiMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name="")
    )
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), RTPVapiPOMirMatch.Tier.PO_NUMBER
    else:
        for mir in candidates:
            material_score = _token_overlap(po_line_item.description, mir.material_description)
            qty_score = _closeness(po_line_item.qty, mir.qty) or Decimal("0")
            rate_score = _closeness(po_line_item.net_price, mir.rate) or Decimal("0")
            value_score = _closeness(po_line_item.net_value, mir.taxable_value) or Decimal("0")

            score = (
                material_score * _WEIGHT_MATERIAL
                + qty_score * _WEIGHT_QTY
                + rate_score * _WEIGHT_RATE
                + value_score * _WEIGHT_VALUE
            )
            if score > best_score:
                best_entry, best_score, best_tier = mir, score, RTPVapiPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        RTPVapiPOMirMatch.objects.filter(po_line_item=po_line_item).delete()
        return None

    qty_diff = _diff_pct(po_line_item.qty, best_entry.qty)
    rate_diff = _diff_pct(po_line_item.net_price, best_entry.rate)
    value_diff = _diff_pct(po_line_item.net_value, best_entry.taxable_value)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = RTPVapiPOMirMatch.objects.update_or_create(
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
    """Import line items are priced in the PO's own currency (confirmed
    real data 2026-09-04: Vapi's live import POs are all USD), but MIR's
    `rate`/`taxable_value` are always INR - comparing them raw understates
    a real match's score by ~90x (India's USD/INR rate), not a few percent,
    so this MUST run before scoring/diffing against MIR, not be treated as
    an optional refinement.
      - rate: net_price * exchange_rate. Falls back to bare net_price if
        exchange_rate is missing (2 of 37 real rows) - better than dropping
        the comparison outright, on the assumption a missing rate is closer
        to already-INR than to silently wrong.
      - value: total_inclusive_value (the real landed-in-India INR figure,
        confirmed empirically closer to MIR's taxable_value than
        net_value*exchange_rate is - landed cost apparently already factors
        into how this plant's MIR rates get recorded) when present, else
        net_value * exchange_rate."""
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


def match_import_po_mir_line_item(import_line_item) -> "RTPVapiImportPOMirMatch | None":
    """Same shape as match_po_mir_line_item() - see matching.py's
    match_import_po_mir_line_item() for the full reasoning (MIR is shared
    across domestic/import, qty compared is qty_as_per_boe not
    qty_as_per_po). Vapi is the plant with real import data live today
    (confirmed 2026-09-04), so this is the path that actually exercises
    against real rows first. See _import_rate_value_inr() for the
    currency-conversion step this needs that domestic matching doesn't."""
    po = import_line_item.purchase_order
    po_vendor = normalize_vendor(po.vendor_name)
    rate_inr, value_inr = _import_rate_value_inr(import_line_item)

    candidates = list(
        RTPVapiMIREntry.objects.filter(is_active=True, party_name__isnull=False).exclude(party_name="")
    )
    candidates = [c for c in candidates if _vendor_matches(normalize_vendor(c.party_name), po_vendor)]

    tier1 = next((c for c in candidates if _po_number_matches(po.po_number, c.po_number_raw)), None)

    best_entry = None
    best_score = Decimal("0")
    best_tier = None

    if tier1 is not None:
        best_entry, best_score, best_tier = tier1, Decimal("1.0000"), RTPVapiImportPOMirMatch.Tier.PO_NUMBER
    else:
        for mir in candidates:
            material_score = _token_overlap(import_line_item.description, mir.material_description)
            qty_score = _closeness(import_line_item.qty_as_per_boe, mir.qty) or Decimal("0")
            rate_score = _closeness(rate_inr, mir.rate) or Decimal("0")
            value_score = _closeness(value_inr, mir.taxable_value) or Decimal("0")

            score = (
                material_score * _WEIGHT_MATERIAL
                + qty_score * _WEIGHT_QTY
                + rate_score * _WEIGHT_RATE
                + value_score * _WEIGHT_VALUE
            )
            if score > best_score:
                best_entry, best_score, best_tier = mir, score, RTPVapiImportPOMirMatch.Tier.WEIGHTED

    if best_entry is None or best_score < MATCH_THRESHOLD:
        RTPVapiImportPOMirMatch.objects.filter(po_line_item=import_line_item).delete()
        return None

    qty_diff = _diff_pct(import_line_item.qty_as_per_boe, best_entry.qty)
    rate_diff = _diff_pct(rate_inr, best_entry.rate)
    value_diff = _diff_pct(value_inr, best_entry.taxable_value)
    is_flagged = any(d is not None and d > FLAG_DIFF_PCT for d in (qty_diff, rate_diff, value_diff))

    match, _ = RTPVapiImportPOMirMatch.objects.update_or_create(
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


def match_mir_entry_stock(mir_entry) -> list["RTPVapiMirStockMatch"]:
    """(material, vendor)-gated - see module docstring for why this uses
    HRS's stronger gate rather than Achhad's material-only one."""
    mir_material = normalize_material(mir_entry.material_description)
    mir_vendor = normalize_vendor(mir_entry.party_name)
    if not mir_material or not mir_vendor:
        return []

    candidates = [
        lot for lot in RTPVapiStockLot.objects.filter(is_active=True).exclude(description="").exclude(supplier_name="")
        if normalize_material(lot.description) == mir_material
        and _vendor_matches(normalize_vendor(lot.supplier_name), mir_vendor)
    ]

    matches = []
    matched_lot_ids = set()
    for lot in candidates:
        rate_diff = _diff_pct(mir_entry.rate, lot.basic_rate)
        is_flagged = rate_diff is not None and rate_diff > FLAG_DIFF_PCT
        match, _ = RTPVapiMirStockMatch.objects.update_or_create(
            mir_entry=mir_entry,
            stock_lot=lot,
            defaults=dict(qty_diff_pct=None, rate_diff_pct=rate_diff, is_flagged=is_flagged),
        )
        matches.append(match)
        matched_lot_ids.add(lot.id)

    RTPVapiMirStockMatch.objects.filter(mir_entry=mir_entry).exclude(stock_lot_id__in=matched_lot_ids).delete()
    return matches


@transaction.atomic
def run_full_match() -> dict:
    from apps.core.models import RTPVapiImportPOLineItem, RTPVapiPOLineItem

    po_matched = 0
    for item in RTPVapiPOLineItem.objects.select_related("purchase_order").all():
        if match_po_mir_line_item(item) is not None:
            po_matched += 1

    import_po_matched = 0
    for item in RTPVapiImportPOLineItem.objects.select_related("purchase_order").all():
        if match_import_po_mir_line_item(item) is not None:
            import_po_matched += 1

    mir_matched = 0
    # See matching.py's run_full_match() for why this cleanup is needed -
    # deactivated MIR entries are skipped below, so their own
    # RTPVapiMirStockMatch rows would otherwise never get dropped.
    RTPVapiMirStockMatch.objects.filter(mir_entry__is_active=False).delete()

    for entry in RTPVapiMIREntry.objects.filter(is_active=True):
        if match_mir_entry_stock(entry):
            mir_matched += 1

    return {
        "po_line_items_matched": po_matched,
        "import_po_line_items_matched": import_po_matched,
        "mir_entries_stock_matched": mir_matched,
        "ran_at": timezone.now(),
    }
