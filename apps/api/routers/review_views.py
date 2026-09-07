"""
Match Accuracy Programme, Phase 1 (doc 03, 1.2): a deliberately minimal
review screen. Shows a batch of random unreviewed matches (_BATCH_SIZE = 5,
bumped up from 1 on 2026-09-07 for throughput - see next_review()'s own
docstring) with both sides side by side per match, and records a
Correct/Incorrect/Unsure verdict as a MatchReview row (apps/core/models.py)
per match - no filtering, no search, no bulk actions beyond the batch size
itself. The design goal is throughput (roughly 200 reviews spread across
plants and tiers), not a full audit UI.

Cross-plant by design, like imports_views.py - a reviewer picks up whatever
match needs reviewing next regardless of plant, so this is one shared router
rather than three per-plant ones. Reuses each plant's own MATCH_CONFIG
(apps/services/matching.py/matching_achhad.py/matching_vapi.py) as the
single source of truth for which model classes and stock-lot field names
belong to that plant, instead of re-deriving a second per-plant mapping here.

Any authenticated role can review (IsAuthenticated is this project's
default permission class - see config/settings.py) - this is data
collection, not a privileged write.
"""

import random

from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.core.models import MatchReview, SyncRun
from apps.services.matching import MATCH_CONFIG as _HRS_CONFIG
from apps.services.matching_achhad import MATCH_CONFIG as _ACHHAD_CONFIG
from apps.services.matching_vapi import MATCH_CONFIG as _VAPI_CONFIG

_CONFIGS = {
    SyncRun.Plant.HRS: _HRS_CONFIG,
    SyncRun.Plant.RTP_ACHHAD: _ACHHAD_CONFIG,
    SyncRun.Plant.RTP_VAPI: _VAPI_CONFIG,
}
_PLANT_LABELS = {
    SyncRun.Plant.HRS: "HRS",
    SyncRun.Plant.RTP_ACHHAD: "RTP-Achhad",
    SyncRun.Plant.RTP_VAPI: "RTP-Vapi",
}
_MATCH_TYPE_LABELS = {
    MatchReview.MatchType.PO_MIR: "PO ↔ MIR",
    MatchReview.MatchType.IMPORT_PO_MIR: "Import PO ↔ MIR",
    MatchReview.MatchType.MIR_STOCK: "MIR ↔ Stock",
}
# Every (match_type, plant) combination reviewed from one screen - picked in
# random order per request so no single group dominates the ~200-review
# sample (doc 03, 1.2).
_GROUPS = [(mt, plant) for mt in MatchReview.MatchType.values for plant in _CONFIGS]


def _num(value):
    return float(value) if value is not None else None


def _model_for(config, match_type):
    if match_type == MatchReview.MatchType.PO_MIR:
        return config.po_mir_match_model
    if match_type == MatchReview.MatchType.IMPORT_PO_MIR:
        return config.import_po_mir_match_model
    return config.mir_stock_match_model


def _po_mir_sides(match, qty_field, config):
    """PO<->MIR and Import PO<->MIR share this shape - only the qty field
    name differs (qty vs qty_as_per_boe, see HRSImportPOMirMatch's own
    docstring for why)."""
    po_item = match.po_line_item
    mir = match.mir_entry
    left = {
        "description": po_item.description,
        "qty": _num(getattr(po_item, qty_field)),
        "rate": _num(po_item.net_price),
        "value": _num(po_item.net_value),
        "vendor": po_item.purchase_order.vendor_name,
    }
    right = {
        "description": mir.material_description,
        "qty": _num(mir.qty),
        "rate": _num(mir.rate),
        "value": _num(config.mir_value(mir)),
        "vendor": mir.party_name,
    }
    meta = {
        "tier": match.tier,
        "matchScore": _num(match.match_score),
        "qtyDiffPct": _num(match.qty_diff_pct),
        "rateDiffPct": _num(match.rate_diff_pct),
        "valueDiffPct": _num(match.value_diff_pct),
    }
    return left, right, meta


def _mir_stock_sides(config, match):
    mir = match.mir_entry
    lot = match.stock_lot
    left = {
        "description": mir.material_description,
        "qty": _num(mir.qty),
        "rate": _num(mir.rate),
        "value": None,
        "vendor": mir.party_name,
    }
    right = {
        "description": lot.description,
        "qty": None,
        "rate": _num(getattr(lot, config.stock_rate_field)),
        "value": None,
        "vendor": getattr(lot, config.stock_vendor_field) if config.stock_vendor_field else None,
    }
    meta = {
        "tier": None,
        "matchScore": None,
        "qtyDiffPct": _num(match.qty_diff_pct),
        "rateDiffPct": _num(match.rate_diff_pct),
        "valueDiffPct": None,
    }
    return left, right, meta


def _serialize(config, match_type, match):
    if match_type == MatchReview.MatchType.PO_MIR:
        return _po_mir_sides(match, "qty", config)
    if match_type == MatchReview.MatchType.IMPORT_PO_MIR:
        return _po_mir_sides(match, "qty_as_per_boe", config)
    return _mir_stock_sides(config, match)


_BATCH_SIZE = 5


def _pick_one(match_type, plant, exclude_ids):
    config = _CONFIGS[plant]
    model = _model_for(config, match_type)
    qs = model.objects.exclude(id__in=exclude_ids)
    if match_type == MatchReview.MatchType.MIR_STOCK:
        qs = qs.select_related("mir_entry", "stock_lot")
    else:
        qs = qs.select_related("mir_entry", "po_line_item__purchase_order")
    return qs.order_by("?").first()


def _match_payload(match_type, plant, match):
    config = _CONFIGS[plant]
    left, right, meta = _serialize(config, match_type, match)
    return {
        "plant": plant,
        "plantLabel": _PLANT_LABELS[plant],
        "matchType": match_type,
        "matchTypeLabel": _MATCH_TYPE_LABELS[match_type],
        "matchId": match.id,
        "left": left,
        "right": right,
        **meta,
    }


@api_view(["GET"])
def next_review(request):
    """Picks up to _BATCH_SIZE random unreviewed matches (project owner,
    2026-09-07: reviewing 1 at a time was too slow for the ~200-review
    throughput goal - see this module's own docstring). Spreads the batch
    across groups round-robin (one fresh pick per (match_type, plant) group
    per pass) rather than filling all 5 from whichever group happens first
    in the shuffle, so a batch still samples multiple plants/tiers the way
    the original one-at-a-time version did over many calls. Returns fewer
    than _BATCH_SIZE once few unreviewed matches remain, and {"done": true}
    (no "matches" key) only once every group is genuinely exhausted."""
    groups = list(_GROUPS)
    random.shuffle(groups)
    reviewed_ids_by_group = {
        (mt, plant): set(MatchReview.objects.filter(plant=plant, match_type=mt).values_list("match_id", flat=True))
        for mt, plant in groups
    }
    picked_ids_by_group = {g: set() for g in groups}
    picked = []

    while len(picked) < _BATCH_SIZE:
        progressed = False
        for match_type, plant in groups:
            if len(picked) >= _BATCH_SIZE:
                break
            group = (match_type, plant)
            exclude_ids = reviewed_ids_by_group[group] | picked_ids_by_group[group]
            match = _pick_one(match_type, plant, exclude_ids)
            if match is None:
                continue
            picked_ids_by_group[group].add(match.id)
            picked.append(_match_payload(match_type, plant, match))
            progressed = True
        if not progressed:
            break  # every group exhausted - stop even if picked is short of _BATCH_SIZE

    if not picked:
        return Response({"done": True})
    return Response({"matches": picked})


@api_view(["POST"])
def submit_review(request):
    plant = request.data.get("plant")
    match_type = request.data.get("matchType")
    match_id = request.data.get("matchId")
    verdict = request.data.get("verdict")
    note = request.data.get("note") or ""

    if plant not in _CONFIGS:
        return Response({"detail": "Invalid plant."}, status=400)
    if match_type not in MatchReview.MatchType.values:
        return Response({"detail": "Invalid matchType."}, status=400)
    if verdict not in MatchReview.Verdict.values:
        return Response({"detail": "Invalid verdict."}, status=400)
    try:
        match_id = int(match_id)
    except (TypeError, ValueError):
        return Response({"detail": "matchId must be an integer."}, status=400)

    config = _CONFIGS[plant]
    model = _model_for(config, match_type)
    if not model.objects.filter(id=match_id).exists():
        return Response({"detail": "Match not found."}, status=404)

    review = MatchReview.objects.create(
        plant=plant,
        match_type=match_type,
        match_id=match_id,
        reviewer=request.user,
        verdict=verdict,
        note=note,
    )
    return Response({"id": review.id}, status=201)
