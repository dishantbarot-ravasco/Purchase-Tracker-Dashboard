"""
What a manual receipt change would do, worked out before it is saved
(2026-09-29, project owner: "show the effect before saving").

The question "if I add 59/09 to this line, what happens to the others?" has
exactly one honest answer: the matcher's. So the preview runs the plant's
real run_full_match() twice inside transactions that are rolled back - once
as things stand, once with the change applied through
manual_receipts.apply_change() - and compares every PO line's receipts and
received quantity between the two. Nothing is kept: both runs roll back,
dry_run skips the MIR<->Stock pass and the data stamp, so no dashboard
reloads.

Running it as things stand (rather than reading the match tables) matters:
the tables hold the last run's result, which can predate a deploy or a
change still queued, and the preview would then show that difference as the
effect of this change.

A full match is seconds of CPU, and Render's gunicorn kills a request at
30 s (see rematch.py), so the run happens on the qcluster like a re-match:
request_preview() queues it and returns an id, the page polls status().
The per-plant advisory lock in run_full_match() keeps a preview and a real
re-match of one plant from overlapping.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.services import rematch
from apps.services.manual_receipts import apply_change
from apps.services.matching_core import line_item_positions, received_against_line

log = logging.getLogger(__name__)

_KEY = "pt:receipt-preview:{}"
_TTL_SECONDS = 900


def _config(plant_key: str):
    from apps.services import matching, matching_achhad, matching_vapi

    return {"hrs": matching, "achhad": matching_achhad, "vapi": matching_vapi}[plant_key].MATCH_CONFIG


def request_preview(plant_key: str, change: dict) -> str:
    """Queue a preview of `change` (apply_change()'s keyword arguments,
    minus the user) and return its id. Inline under pytest, like a re-match."""
    preview_id = uuid.uuid4().hex
    cache.set(_KEY.format(preview_id), {"state": "queued", "queuedAt": timezone.now().isoformat(),
                                        "plantKey": plant_key}, _TTL_SECONDS)
    if rematch._inline():
        run_preview(preview_id, plant_key, change)
    else:
        from django_q.tasks import async_task

        async_task("apps.services.receipt_preview.run_preview", preview_id, plant_key, change)
    return preview_id


def status(preview_id: str) -> dict | None:
    """The preview's state and, once done, its result. None for an unknown
    or expired id. `stalled` as rematch.status() means it."""
    data = cache.get(_KEY.format(preview_id))
    if data is None:
        return None
    if data.get("state") == "queued":
        try:
            from datetime import datetime

            waited = (timezone.now() - datetime.fromisoformat(data["queuedAt"])).total_seconds()
            data = {**data, "stalled": waited > rematch._STALL_SECONDS}
        except (KeyError, TypeError, ValueError):
            pass
    return data


class _Rollback(Exception):
    pass


def run_preview(preview_id: str, plant_key: str, change: dict) -> None:
    """The queued task."""
    key = _KEY.format(preview_id)
    base = cache.get(key) or {}
    cache.set(key, {**base, "state": "running"}, _TTL_SECONDS)
    try:
        result = compute(plant_key, change)
    except ValueError as exc:
        cache.set(key, {**base, "state": "failed", "error": str(exc)}, _TTL_SECONDS)
        return
    except Exception as exc:
        log.exception("receipt_preview: failed for plant=%s", plant_key)
        cache.set(key, {**base, "state": "failed", "error": str(exc)[:300]}, _TTL_SECONDS)
        return
    cache.set(key, {**base, "state": "done", **result}, _TTL_SECONDS)


def compute(plant_key: str, change: dict) -> dict:
    """{"lines": [...], "unfilled": bool} - see the module docstring."""
    config = _config(plant_key)
    match = rematch._match_fn(plant_key)
    before = after = report = None
    try:
        with transaction.atomic():
            match(dry_run=True)
            before = _snapshot(config)
            raise _Rollback
    except _Rollback:
        pass
    try:
        with transaction.atomic():
            apply_change(mir_model=config.mir_model, **change)
            report = match(dry_run=True)
            after = _snapshot(config)
            raise _Rollback
    except _Rollback:
        pass
    target = (change["po_kind"], change["po_number"])
    lines = []
    for key in sorted(set(before) | set(after), key=lambda k: (k[0], k[1], int(k[2]) if k[2].isdigit() else 0)):
        b, a = before.get(key), after.get(key)
        info = a or b
        changed = (b or {}).get("receipts") != (a or {}).get("receipts") or \
            (b or {}).get("received") != (a or {}).get("received")
        if not changed and (key[0], key[1]) != target:
            continue
        lines.append({
            "poKind": key[0], "poNumber": key[1], "itemRef": key[2],
            "line": int(key[2]) + 1 if key[2].isdigit() else None,
            "description": info["description"], "uom": info["uom"], "ordered": info["ordered"],
            "before": _side(b), "after": _side(a),
            "changed": changed, "samePo": (key[0], key[1]) == target,
            "editedLine": (key[0], key[1]) == target and key[2] == change["item_ref"],
        })
    mir_no = change.get("mir_no") or ""
    unfilled = any(
        u["poNumber"] == change["po_number"] and u["itemRef"] == change["item_ref"] and u["mirNo"] == mir_no
        and u.get("poKind", change["po_kind"]) == change["po_kind"]
        for u in (report or {}).get("manual_edits_unfilled", []) + (report or {}).get("manual_pins_unfilled", [])
    ) if mir_no else False
    return {"lines": lines, "unfilled": unfilled}


def _side(s):
    if s is None:
        return {"receipts": [], "received": None, "diffPct": None}
    return {"receipts": s["receipts"], "received": s["received"], "diffPct": s["diffPct"]}


def _snapshot(config) -> dict:
    """{(po_kind, po_number, item_ref): {...}} for every active PO line of
    the plant, both kinds, as the match tables now read."""
    out = {}
    for kind, item_model, match_model in (
        ("domestic", config.po_item_model, config.po_mir_match_model),
        ("import", config.import_item_model, config.import_po_mir_match_model),
    ):
        items = list(item_model.objects.filter(purchase_order__is_active=True).select_related("purchase_order"))
        positions = line_item_positions(items)
        matches = {
            m.po_line_item_id: m for m in match_model.objects.filter(po_line_item__in=items)
            .select_related("mir_entry").prefetch_related("group_entries")
        }
        for item in items:
            po_number, ref, description = positions[item.id]
            m = matches.get(item.id)
            ordered_qty = getattr(item, "qty_as_per_boe", None) if kind == "import" else item.qty
            if kind == "import" and ordered_qty is None:
                ordered_qty = getattr(item, "qty_as_per_po", None)
            entry = {"description": description, "uom": item.uom,
                     "ordered": float(ordered_qty) if ordered_qty is not None else None,
                     "receipts": [], "received": None, "diffPct": None}
            if m is not None:
                rows = list(m.group_entries.all()) or [m.mir_entry]
                entry["receipts"] = sorted({r.mir_no for r in rows if r.mir_no})
                qty = received_against_line(config, item.uom, rows)["qty"]
                if qty is not None and m.receipt_share is not None:
                    qty = qty * m.receipt_share
                if qty is not None:
                    entry["received"] = float(Decimal(qty).quantize(Decimal("0.001")))
                    if ordered_qty:
                        entry["diffPct"] = float(((Decimal(qty) - Decimal(ordered_qty)) / Decimal(ordered_qty)
                                                  * 100).quantize(Decimal("0.01")))
            out[(kind, po_number, ref)] = entry
    return out
