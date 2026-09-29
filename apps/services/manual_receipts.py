"""
Manual receipt changes on a PO line - the service behind the PO modal's
"Edit receipts" panel (2026-09-29, project owner).

Two kinds of stored decision, both addressed by (plant, po_kind, PO number,
line position) and naming a MIR NUMBER, never a row:

  - ManualMirMatch, the older pin: "this line's receipt is document N" (or,
    with a blank number, "this line has no receipt").
  - ManualReceiptEdit: "count document N on this line as well" (add) or
    "never count document N on this line" (remove). One document each;
    the rest of the line stays with the matcher.

apply_change() is the one place either is written, for the Domestic and the
Import routers alike, so the rules that keep the two consistent live once:
adding a document clears a "no receipt" pin and a removal of the same
document; removing one clears an add of it and a pin naming it; "back to
automatic" clears everything on the line.

manual_changes() lists what a PO's lines carry, with who and when, plus
decisions on OTHER orders' lines that took a receipt citing this one - the
reader of that order did nothing and would otherwise not know why a receipt
naming it is missing.

candidate_insights() says, for each MIR document the picker offers, how it
relates to the line and why the matcher is not already counting it there.

The re-match itself is the caller's job (rematch.request_rematch()).
"""

from __future__ import annotations

import re

from django.db.models import Q

from apps.core.models import ManualMirMatch, ManualReceiptEdit
from apps.services.matching_core import (
    DATE_IMPOSSIBLE,
    _date_verdict,
    _forced_candidate,
    _import_matchable,
    _material_scorer,
    _names_this_po,
    _po_matchable,
    _po_number_contradicts,
    known_po_numbers,
    line_item_positions,
)
from apps.services.parsers.common import is_usable_po_reference

ACTIONS = ("add", "remove", "notReceived", "auto", "undo", "set")


def _user_fields(user) -> dict:
    return {
        "created_by": user if getattr(user, "pk", None) else None,
        "created_by_email": getattr(user, "email", "") or "",
    }


def apply_change(*, plant: str, po_kind: str, po_number: str, item_ref: str, item_description: str,
                 action: str, mir_model, mir_no: str = "", shared: bool = False, reason: str = "",
                 user=None, undo_type: str = "", undo_id=None) -> dict:
    """Writes one change to one line. Raises ValueError with a message for
    the reader when the change cannot be made. Returns {"action", "mirNo"}.

    `set` is the older "this document is the line's receipt" pin, kept for
    the callers that still send it (a PATCH with a mirNo and no action)."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}.")
    mir_no = (mir_no or "").strip()
    line = dict(plant=plant, po_kind=po_kind, po_number=po_number, item_ref=item_ref)
    pins = ManualMirMatch.objects.filter(**line)
    edits = ManualReceiptEdit.objects.filter(**line)
    if action in ("add", "remove", "set") and mir_no and not mir_model.objects.filter(
            is_active=True, mir_no=mir_no).exists():
        raise ValueError(f"No active MIR entry numbered {mir_no!r} at this plant.")

    if action == "add":
        if not mir_no:
            raise ValueError("Pick the MIR receipt to add.")
        if pins.filter(mir_no=mir_no).exists():
            raise ValueError(f"MIR {mir_no} is already this line's receipt.")
        # A line cannot be "not received" and have a receipt added.
        pins.filter(mir_no="").delete()
        edits.filter(mir_no=mir_no, action=ManualReceiptEdit.Action.REMOVE).delete()
        _save_edit(line, mir_no, ManualReceiptEdit.Action.ADD, shared, reason, item_description, user)
    elif action == "remove":
        if not mir_no:
            raise ValueError("Pick the MIR receipt to remove.")
        # Removing the pinned document drops the pin too: the line goes back
        # to the matcher, with this document kept off it by the removal.
        pins.filter(mir_no=mir_no).delete()
        edits.filter(mir_no=mir_no, action=ManualReceiptEdit.Action.ADD).delete()
        _save_edit(line, mir_no, ManualReceiptEdit.Action.REMOVE, False, reason, item_description, user)
    elif action == "notReceived":
        edits.filter(action=ManualReceiptEdit.Action.ADD).delete()
        ManualMirMatch.objects.update_or_create(**line, defaults=dict(
            mir_no="", shared=False, item_description=item_description, reason=reason, **_user_fields(user)))
    elif action == "auto":
        pins.delete()
        edits.delete()
    elif action == "undo":
        model = {"pin": ManualMirMatch, "edit": ManualReceiptEdit}.get(undo_type)
        if model is None or not str(undo_id or "").isdigit():
            raise ValueError("Say which change to undo.")
        deleted, _ = model.objects.filter(pk=int(undo_id), **line).delete()
        if not deleted:
            raise ValueError("That change is no longer there - it may already have been undone.")
    else:  # "set" - the older pin
        if mir_no:
            edits.filter(mir_no=mir_no).delete()
        ManualMirMatch.objects.update_or_create(**line, defaults=dict(
            mir_no=mir_no, shared=shared and bool(mir_no), item_description=item_description, reason=reason,
            **_user_fields(user)))
    return {"action": action, "mirNo": mir_no}


def _save_edit(line, mir_no, action, shared, reason, item_description, user):
    ManualReceiptEdit.objects.update_or_create(**line, mir_no=mir_no, defaults=dict(
        action=action, shared=bool(shared) and action == ManualReceiptEdit.Action.ADD, reason=reason,
        item_description=item_description, **_user_fields(user)))


def _decision_dict(obj, kind: str, description_now: str) -> dict:
    if kind == "pin":
        action = "pinned" if obj.mir_no else "notReceived"
    else:
        action = "added" if obj.action == ManualReceiptEdit.Action.ADD else "removed"
    return {
        "type": kind,
        "id": obj.pk,
        "action": action,
        "mirNo": obj.mir_no,
        "shared": bool(obj.shared),
        "by": obj.created_by_email or "",
        "byName": (getattr(obj.created_by, "full_name", "") or "") if obj.created_by_id else "",
        "at": obj.updated_at.isoformat() if obj.updated_at else None,
        "reason": obj.reason or "",
        # The line's description no longer matches the one recorded: the
        # PO's lines changed and the matcher is ignoring this decision.
        "stale": bool(obj.item_description) and obj.item_description.strip() != (description_now or "").strip(),
    }


def manual_changes(*, plant: str, po_kind: str, po, items, mir_model) -> dict:
    """{"lines": [...], "elsewhere": [...]} for one PO - see the module
    docstring. `items` are the PO's line items, in any order."""
    positions = line_item_positions(items)
    by_ref = {ref: (item, desc) for item, (_po, ref, desc) in
              ((i, positions[i.id]) for i in items)}
    line = dict(plant=plant, po_kind=po_kind, po_number=po.po_number)
    decisions: dict = {}
    for pin in ManualMirMatch.objects.filter(**line).select_related("created_by"):
        decisions.setdefault(pin.item_ref, []).append(("pin", pin))
    for edit in ManualReceiptEdit.objects.filter(**line).select_related("created_by"):
        decisions.setdefault(edit.item_ref, []).append(("edit", edit))
    lines = []
    for ref in sorted(decisions, key=lambda r: int(r) if r.isdigit() else 0):
        _item, desc = by_ref.get(ref, (None, ""))
        changes = [_decision_dict(obj, kind, desc) for kind, obj in decisions[ref]]
        changes.sort(key=lambda c: c["at"] or "", reverse=True)
        lines.append({
            "itemRef": ref,
            "line": int(ref) + 1 if ref.isdigit() else None,
            "description": desc if _item is not None else "",
            "missingLine": _item is None,
            "changes": changes,
        })

    # Receipts citing this order, placed by hand on some other order's line.
    citing = {
        (r.mir_no or "").strip() for r in mir_model.objects.filter(
            is_active=True, po_number_raw__icontains=_digits(po.po_number) or po.po_number)
        if r.mir_no and _names_this_po(po.po_number, r.po_number_raw)
    }
    elsewhere = []
    if citing:
        other = ~Q(po_number=po.po_number) | ~Q(po_kind=po_kind)
        placed = [("pin", p) for p in ManualMirMatch.objects.filter(other, plant=plant, mir_no__in=citing).select_related("created_by")]
        placed += [("edit", e) for e in ManualReceiptEdit.objects.filter(
            other, plant=plant, mir_no__in=citing, action=ManualReceiptEdit.Action.ADD).select_related("created_by")]
        for kind, obj in placed:
            elsewhere.append({
                "poNumber": obj.po_number,
                "poKind": obj.po_kind,
                "line": int(obj.item_ref) + 1 if str(obj.item_ref).isdigit() else None,
                "description": obj.item_description,
                **_decision_dict(obj, kind, obj.item_description),
            })
        elsewhere.sort(key=lambda c: c["at"] or "", reverse=True)
    return {"lines": lines, "elsewhere": elsewhere}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _bare_po(raw: str) -> str:
    """A MIR PO cell as a reader would type it: '3000001167.0' -> '3000001167'."""
    return re.sub(r"\.0+$", "", (raw or "").strip())


def _one_edit_apart(a: str, b: str) -> bool:
    """True when two digit strings differ by exactly one inserted, deleted,
    changed or swapped digit - '30000001167' against '3000001167'."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


# How the picker groups what it offers, most likely first.
GROUP_ORDER = {"cites": 0, "vendorMaterial": 1, "vendor": 2, "other": 3}


def candidate_insights(config, po, item, item_count: int, rows, *, is_import: bool = False,
                       line_cites_po: bool = False) -> dict:
    """{mir_no: {"group", "why": [sentences]}} for the MIR rows a picker
    offers one line. `line_cites_po` says the line already counts receipts
    citing its order, which is the one reason a receipt that identifies on
    vendor and material still is not counted there.

    Uses the matcher's own evidence (_forced_candidate(), the PO-number
    contradiction gate, the date verdict), so the sentence and the matcher
    can never disagree about what they saw. A document with several rows is
    judged by its row most like the line."""
    if not rows:
        return {}
    scorer = _material_scorer(config)
    known = known_po_numbers(config)
    matchable = (_import_matchable(config, item, item_count == 1) if is_import
                 else _po_matchable(item, item_count == 1))
    best: dict = {}
    for row in rows:
        if not row.mir_no:
            continue
        ev = _forced_candidate(config, matchable, row, po, scorer=scorer, known_pos=known)
        rank = (ev.po_number_matched, ev.vendor_matched and ev.material_matched, ev.material_score)
        if row.mir_no not in best or rank > best[row.mir_no][0]:
            best[row.mir_no] = (rank, row, ev)
    out = {}
    for mir_no, (_rank, row, ev) in best.items():
        if ev.po_number_matched:
            group = "cites"
        elif ev.vendor_matched and ev.material_matched:
            group = "vendorMaterial"
        elif ev.vendor_matched:
            group = "vendor"
        else:
            group = "other"
        out[mir_no] = {"group": group, "why": _why_not(config, po, row, ev, known, line_cites_po)}
    return out


def _why_not(config, po, row, ev, known, line_cites_po) -> list[str]:
    """Why the matcher does not count this receipt on the line by itself -
    empty when nothing stands in its way (it may simply have lost to a
    better-scoring line, which the picker's "currently matched to" says)."""
    why = []
    raw = _bare_po(row.po_number_raw)
    if not ev.po_number_matched:
        if raw and _po_number_contradicts(po.po_number, row.po_number_raw, known):
            why.append(f"Its PO column says {raw}, another order we hold, so the matcher keeps it off this one.")
        elif raw and is_usable_po_reference(raw):
            typo = _one_edit_apart(_digits(raw), _digits(po.po_number))
            why.append(
                f"Its PO column says {raw}, which is not one of our orders"
                + (f" - one digit away from {po.po_number}, so probably a typo. Correct it in the MIR sheet"
                   " and it will match on its own." if typo else "."))
        elif raw:
            why.append(f"Its PO column says {raw!r}, which is not a PO number.")
        else:
            why.append("It has no PO number.")
    if not ev.vendor_matched:
        why.append(f"The party ({row.party_name or 'blank'}) is not this PO's vendor.")
    if not ev.material_matched:
        why.append("Its material does not read like this line's.")
    agree = ev.po_number_matched + ev.vendor_matched + ev.material_matched
    if not ev.po_number_matched:
        if _date_verdict(config, po.po_created_date, getattr(row, "mir_date", None)) == DATE_IMPOSSIBLE:
            why.append("It is dated too far from this PO's date to belong to it.")
        elif agree >= 2 and line_cites_po:
            why.append("This line already counts the receipts that cite its PO number, and a receipt that"
                       " does not cite it is not added on top of those automatically.")
        elif agree < 2:
            why.append("The matcher needs two of PO number, vendor and material to agree.")
    return why
