"""
Match Accuracy Programme, Phase 1 (doc 03, 1.2): a deliberately minimal
review screen. Shows a batch of random unreviewed matches (_BATCH_SIZE = 5,
bumped up from 1 on 2026-09-07 for throughput - see next_review()'s own
docstring) with both sides side by side per match, and records a
Correct/Incorrect/Unsure verdict as a MatchReview row (apps/core/models.py)
per match - no filtering, no search, no bulk actions beyond the batch size
itself. The design goal is throughput (roughly 200 reviews spread across
plants and tiers), not a full audit UI.

Each card carries the identifiers of both rows (PO number and date, MIR
number and date, the PO the MIR itself cites, invoice number, UOM on both
sides, the stock lot's item code and received date) plus the matcher's own
identification booleans as plain-English "why this matched" signals. That is
NOT audit-UI scope creep - it is the minimum needed for the verdict to mean
anything. The first version showed only description/qty/rate/value/vendor, so
a reviewer could not see the PO number a Tier-1 match was made ON, could not
tell KG from MTR, and had no key with which to look the row up in the source
sheet; judging on description similarity alone is roughly what the algorithm
already did, so agreement proved little. Filtering, search and bulk actions
are still deliberately absent (they would also bias the random sample the
accuracy figure depends on).

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

import io as _io
import random

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.api.routers._domestic_base import SafeCsvWriter

from apps.core.models import MatchReview
from apps.services.match_accuracy import CONFIGS as _CONFIGS
from apps.services.match_accuracy import MATCH_TYPE_LABELS as _MATCH_TYPE_LABELS
from apps.services.match_accuracy import PLANT_LABELS as _PLANT_LABELS
from apps.services.match_accuracy import REVIEW_TARGET, build_report, model_for as _model_for
from apps.services.matching_core import _import_rate_value_inr
# Every (match_type, plant) combination reviewed from one screen - picked in
# random order per request so no single group dominates the ~200-review
# sample (doc 03, 1.2).
_GROUPS = [(mt, plant) for mt in MatchReview.MatchType.values for plant in _CONFIGS]


def _num(value):
    return float(value) if value is not None else None


def _text(value):
    """Blank CharFields are "" in this schema, not None - collapse both to
    None so the frontend's single "-" placeholder covers them."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    value = value.strip()
    return value or None


def _iso(value):
    return value.isoformat() if value is not None else None


def _ref(label, value, kind="text"):
    """One identifying field on a side of the card - the PO number, the MIR
    number, a receipt date. Kept as an ordered list of label/value pairs
    rather than fixed keys because the three match types genuinely identify
    their rows by different things, and the frontend should not have to know
    which. `kind` is "date" for an ISO date the frontend renders dd/mm/yyyy
    (formatDateIN, shared.js), "num" for a plain unformatted number (an
    exchange rate or a foreign-currency amount, which must NOT go through
    formatInr).

    These exist because the original card showed description/qty/rate/value/
    vendor and nothing else: a reviewer could not see the PO number a Tier-1
    match was made ON, could not tell KG from MTR, and had no key to look
    the row up in the source sheet with. The verdict collected here becomes
    this app's only measured accuracy figure (CLAUDE.md, Known gaps), so it
    has to be judgeable from the card."""
    return {"label": label, "value": value, "kind": kind}


def _signal(label, state, hint=""):
    """One reason the matcher paired these two rows, as the matcher itself
    recorded it (the identification booleans on the match model - see
    matching_core.py's _identification_pool()). state: "yes" | "no" | "warn".
    This is the difference between a reviewer judging a pair on description
    similarity - roughly what the algorithm already did, so agreement proves
    little - and judging the actual evidence the match was built on."""
    return {"label": label, "state": state, "hint": hint}


def _po_mir_sides(match, config, is_import):
    """PO<->MIR and Import PO<->MIR share this shape. Two real differences:
    the qty field name (qty vs qty_as_per_boe, see HRSImportPOMirMatch's own
    docstring for why), and currency - an import line item is priced in the
    PO's own currency while MIR is always INR, so the matcher scores it
    against _import_rate_value_inr()'s converted figures. THE CARD SHOWS THE
    SAME CONVERTED FIGURES: showing the raw USD net_price against MIR's INR
    rate made every import match read as a ~90x rate discrepancy, i.e. it
    invited a wrong "Incorrect" verdict on a correct match. The
    original-currency amount and the exchange rate stay on the card as ref
    fields, so nothing is hidden - it is just no longer compared against the
    wrong unit."""
    po_item = match.po_line_item
    po = po_item.purchase_order
    mir = match.mir_entry

    if is_import:
        qty = _num(po_item.qty_as_per_boe)
        rate, value = _import_rate_value_inr(po_item)
        rate, value = _num(rate), _num(value)
    else:
        qty = _num(po_item.qty)
        rate, value = _num(po_item.net_price), _num(po_item.net_value)

    left_refs = [
        _ref("PO no.", _text(po.po_number)),
        _ref("PO date", _iso(po.po_created_date), "date"),
        _ref("Line", _text(po_item.item_id)),
        _ref("UOM", _text(po_item.uom)),
        _ref("HSN", _text(po_item.hsn)),
        _ref("Delivery date", _iso(po_item.delivery_date), "date"),
    ]
    if is_import:
        currency = _text(po.currency)
        left_refs += [
            _ref("Qty as per PO", _num(po_item.qty_as_per_po), "num"),
            _ref("Rate in " + (currency or "PO currency"), _num(po_item.net_price), "num"),
            _ref("Exchange rate", _num(po_item.exchange_rate), "num"),
        ]

    left = {
        "title": "Import PO line item" if is_import else "PO line item",
        "refs": left_refs,
        "description": _text(po_item.description),
        "qty": qty,
        "rate": rate,
        "value": value,
        "vendor": _text(po.vendor_name),
    }
    right = {
        "title": "MIR entry",
        "refs": [
            _ref("MIR no.", _text(mir.mir_no)),
            _ref("MIR date", _iso(mir.mir_date), "date"),
            _ref("PO cited on MIR", _text(mir.po_number_raw)),
            _ref("UOM", _text(mir.uom)),
            _ref("Invoice no.", _text(mir.invoice_no)),
            _ref("Invoice date", _iso(mir.invoice_date), "date"),
        ],
        "description": _text(mir.material_description),
        "qty": _num(mir.qty),
        "rate": _num(mir.rate),
        "value": _num(config.mir_value(mir)),
        "vendor": _text(mir.party_name),
    }
    signals = [
        _signal(
            "PO number cited on MIR",
            "yes" if match.po_number_matched else "no",
            "Tier-1 evidence: the MIR row names this PO itself."
            if match.po_number_matched
            else "The MIR row does not name this PO - the pair rests on the weighted score alone.",
        ),
        _signal("Vendor agrees", "yes" if match.vendor_matched else "no"),
        _signal(
            "Material description agrees",
            "yes" if match.material_matched else "no",
            "Token overlap at or above this plant's identification threshold.",
        ),
    ]
    if match.manually_pinned:
        signals.append(
            _signal(
                "Manually pinned",
                "warn",
                "A person forced this pair - the matcher did not choose it.",
            )
        )
    meta = {
        "tier": match.tier,
        "matchScore": _num(match.match_score),
        "qtyDiffPct": _num(match.qty_diff_pct),
        "rateDiffPct": _num(match.rate_diff_pct),
        "valueDiffPct": _num(match.value_diff_pct),
        "signals": signals,
    }
    return left, right, meta


def _mir_stock_sides(config, match):
    mir = match.mir_entry
    lot = match.stock_lot

    # stock_code_field/stock_uom_field are "" at a plant whose RM sheet has
    # no such column (Vapi has no item code, Achhad no UOM) - see
    # matching_core.py's _MatchConfig. Omit the row rather than showing an
    # empty one, so a blank never reads as "the sheet says nothing here".
    lot_refs = []
    if config.stock_code_field:
        lot_refs.append(_ref("Item code", _text(getattr(lot, config.stock_code_field))))
    lot_refs.append(_ref("Received date", _iso(lot.received_date), "date"))
    if config.stock_uom_field:
        lot_refs.append(_ref("UOM", _text(getattr(lot, config.stock_uom_field))))
    lot_refs.append(_ref("Category", _text(getattr(lot, "category", None))))

    left = {
        "title": "MIR entry",
        "refs": [
            _ref("MIR no.", _text(mir.mir_no)),
            _ref("MIR date", _iso(mir.mir_date), "date"),
            _ref("UOM", _text(mir.uom)),
            _ref("Invoice no.", _text(mir.invoice_no)),
        ],
        "description": _text(mir.material_description),
        "qty": _num(mir.qty),
        "rate": _num(mir.rate),
        "value": _num(config.mir_value(mir)),
        "vendor": _text(mir.party_name),
    }
    right = {
        "title": "RM stock lot",
        "refs": lot_refs,
        "description": _text(lot.description),
        "qty": None,
        "rate": _num(getattr(lot, config.stock_rate_field)),
        "value": None,
        "vendor": _text(getattr(lot, config.stock_vendor_field)) if config.stock_vendor_field else None,
    }
    signals = [
        _signal("Material agrees", "yes" if match.material_matched else "no"),
        _signal(
            "Receipt date agrees",
            "yes" if match.date_matched else "no",
            "The MIR date and the lot's received date line up."
            if match.date_matched
            else "The dates do not line up - a lot's received date is often simply blank.",
        ),
    ]
    if match.uom_mismatch:
        signals.append(
            _signal(
                "UOM mismatch",
                "warn",
                "The two sides are measured in different units - any quantity comparison here is meaningless.",
            )
        )
    meta = {
        "tier": None,
        "matchScore": None,
        "qtyDiffPct": _num(match.qty_diff_pct),
        "rateDiffPct": _num(match.rate_diff_pct),
        "valueDiffPct": _num(match.value_diff_pct),
        "signals": signals,
    }
    return left, right, meta


def _serialize(config, match_type, match):
    if match_type == MatchReview.MatchType.MIR_STOCK:
        return _mir_stock_sides(config, match)
    return _po_mir_sides(match, config, match_type == MatchReview.MatchType.IMPORT_PO_MIR)


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
        return Response({"done": True, "progress": _progress()})
    return Response({"matches": picked, "progress": _progress()})


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
    return Response({"id": review.id, "progress": _progress()}, status=201)


@api_view(["DELETE"])
def undo_review(request, review_id):
    """Undo: delete a verdict this user just recorded.

    A misclick was previously permanent as far as the screen was concerned -
    the buttons disabled themselves and never came back. The DATA model
    always allowed a correction (no unique constraint; report_match_accuracy
    takes the most recent verdict per match), but a reviewer had no way to
    reach it, so the realistic outcome of a slip was a wrong label sitting
    in the sample forever.

    Deletes rather than superseding, because the row is seconds old and a
    "correct, then actually incorrect" pair says nothing a reviewer meant.
    A REVIEWER CAN ONLY EVER DELETE THEIR OWN ROW - not an editor's, not an
    admin's, regardless of role. Nobody gets to quietly rewrite someone
    else's judgement out of the sample; that is the whole point of a
    measurement harness."""
    review = MatchReview.objects.filter(id=review_id, reviewer=request.user).first()
    if review is None:
        return Response({"detail": "Review not found."}, status=404)
    review.delete()
    return Response({"progress": _progress()}, status=200)


def _progress():
    """How much of the programme's ~200-review sample exists, counted in
    DISTINCT matches rather than MatchReview rows - re-reviewing the same
    match is a correction, not progress."""
    reviewed = (
        MatchReview.objects.values("plant", "match_type", "match_id").distinct().count()
    )
    return {"reviewed": reviewed, "target": REVIEW_TARGET}


@api_view(["GET"])
def review_stats(request):
    """The management view of the programme: precision/recall/F1 overall and
    per plant, match type, plant x match type and tier, plus who reviewed how
    much and the latest reviewer notes.

    Same numbers as `manage.py report_match_accuracy`, from the same code
    (apps/services/match_accuracy.py) - the point is that nobody has to have
    shell access to find out whether this app's matches can be trusted.

    Readable by any authenticated role, like the review screen itself: this
    is the accuracy of the app's own output, not per-plant business data, and
    a viewer being able to see "MIR<->Stock at Vapi is 0.62" is the entire
    reason the figure is collected."""
    return Response(build_report())


_EXPORT_SECTIONS = (
    ("Overall", "overall"),
    ("By plant", "byPlant"),
    ("By match type", "byMatchType"),
    ("By plant + match type", "byPlantAndType"),
    ("By match type + tier", "byTier"),
)


@api_view(["GET"])
def export_review_stats(request):
    """The accuracy panel's tables as a CSV, for pasting into a report or a
    board pack. Every section in one file with a `section` column rather than
    five downloads - the whole point is that the cuts sit next to each other.

    Assembled in memory, not streamed like the stock-snapshot export: this is
    at most a few dozen aggregate rows however long the programme runs
    (3 plants x 3 match types plus tiers), so the streaming machinery would be
    ceremony. SafeCsvWriter regardless - _domestic_base.py's own docstring
    says to use it for ANY new export, and a tier value ultimately originates
    in synced spreadsheet data."""
    report = build_report(note_limit=0)
    buffer = _io.StringIO()
    writer = SafeCsvWriter(buffer)
    writer.writerow(
        ["section", "group", "n", "correct", "incorrect", "unsure", "precision", "recall", "f1", "small_sample"]
    )

    def _row(section, label, row):
        def fmt(value):
            return round(value, 4) if value is not None else ""

        writer.writerow(
            [
                section,
                label,
                row["n"],
                row["correct"],
                row["incorrect"],
                row["unsure"],
                fmt(row["precision"]),
                fmt(row["recall"]),
                fmt(row["f1"]),
                "yes" if row["n"] < report["minSample"] else "",
            ]
        )

    for section, key in _EXPORT_SECTIONS:
        if key == "overall":
            _row(section, "All reviewed matches", report["overall"])
            continue
        for row in report[key]:
            _row(section, row["label"], row)

    filename = f"match-accuracy-{timezone.localdate().isoformat()}.csv"
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
