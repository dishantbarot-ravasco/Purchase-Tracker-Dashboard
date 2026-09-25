"""
Derived fields and data-quality flags for Import Purchase Orders, computed at
READ time (in apps/api/routers/imports_views.py) from already-fetched PO/item
rows, rather than stored. Import lines are matched to MIR by the same engine
as domestic ones (matching_core.match_import_po_mir_line_item()); the
receipt-based rollups here (delivery status, partial delivery, material
inwarded) read that match off `item.mir_match`, and never write anything.

Kept dependency-free (no Django imports) so this can be unit-tested with
nothing but plain Python and plain objects/dicts, same goal as
apps/services/parsers/common.py. Every function takes plain values (not model
instances) so it works the same whether called from a Django queryset or a
test fixture - see apps/services/tests/test_import_flags.py.

Row grain: everything here operates on one "item row" - the dict shape is
whatever the caller passes (model instance or plain namespace) as long as it
exposes the attributes referenced below. PO-level rollups take a list of such
item rows for one PO.
"""

import datetime

# ── Shipment stage (spec section 2) ─────────────────────────────────────────

STAGE_PLACED = "Placed"
STAGE_SHIPPED = "Shipped (BL)"
STAGE_CLEARED = "Cleared (BOE)"


def shipment_stage(item) -> str:
    """One item's customs-clearance stage, inferred from which fields are
    filled rather than a stored status column - BOE number present means
    cleared, else a bill-of-lading/laden-on-board date means shipped, else
    it's still just placed."""
    if item.boe_number:
        return STAGE_CLEARED
    if item.bill_of_lading_number or item.laden_on_board_date:
        return STAGE_SHIPPED
    return STAGE_PLACED


def po_shipment_stage(items) -> str:
    """PO-level rollup: the LEAST advanced stage among its items - a PO isn't
    "Cleared" until every item has cleared, mirroring how the mockup's
    per-item stepper only lights up all three dots once BOE is filed."""
    stages = [shipment_stage(i) for i in items]
    if any(s == STAGE_PLACED for s in stages):
        return STAGE_PLACED
    if any(s == STAGE_SHIPPED for s in stages):
        return STAGE_SHIPPED
    return STAGE_CLEARED


# ── Qty discrepancy PO vs BOE (spec section 2) ──────────────────────────────

def boe_totals(items) -> dict:
    """{id(line): BOE quantity to judge that line's order against} - keyed
    on object identity so plain test objects work too.

    A SPLIT SHIPMENT is one order line the Imports CSV writes as several rows,
    one per shipment - and it repeats the whole ordered quantity on every row
    (Vapi 1000001528: three rows each "64,800 ordered", each clearing 21,600).
    Judged row by row, each read 67% short of its order and, once received,
    "Delivered" while two-thirds of the order was still at sea. Rows are one
    split order line when they share the item_id and the ordered quantity,
    there are at least two, and each cleared less than that quantity; their
    BOE quantities are summed. Every other line judges on its own BOE qty."""
    groups: dict = {}
    for item in items:
        groups.setdefault((item.item_id or "", item.qty_as_per_po), []).append(item)
    totals = {}
    for (_item_id, ordered), group in groups.items():
        split = (
            len(group) > 1 and ordered is not None
            and all(i.qty_as_per_boe is not None and i.qty_as_per_boe < ordered for i in group)
        )
        for item in group:
            totals[id(item)] = sum(i.qty_as_per_boe for i in group) if split else item.qty_as_per_boe
    return totals


def qty_discrepancy(item, boe_total=None) -> tuple[bool, float | None]:
    """Returns (is_discrepancy, pct). QTY (As Per BOE) missing is NOT a
    discrepancy - it means "not yet cleared", not a mismatch. `boe_total` is
    boe_totals()'s figure for the line (a split shipment's summed BOE qty);
    without it the line's own BOE qty is used."""
    shipped = item.qty_as_per_boe if boe_total is None else boe_total
    if shipped is None:
        return False, None
    if item.qty_as_per_po is None or item.qty_as_per_po == 0:
        return False, None
    is_mismatch = shipped != item.qty_as_per_po
    pct = float((shipped - item.qty_as_per_po) / item.qty_as_per_po * 100)
    return is_mismatch, pct


def po_has_qty_discrepancy(items) -> bool:
    """PO-level rollup: true if any item's BOE quantity disagrees with its
    ordered quantity - drives the Import KPI row's "Qty Discrepancies (BOE
    vs MIR)" style cards."""
    totals = boe_totals(items)
    return any(qty_discrepancy(i, totals.get(id(i)))[0] for i in items)


# ── MIR receipt (the same rule Domestic's flags.js uses) ────────────────────

def _live_match(item):
    """The line's PO<->MIR match, or None when it has none or a reviewer
    dismissed it. A dismissed match means "this pairing is wrong", so it is
    not an arrival - same as flags.js's lineItemArrived(). getattr() with a
    default because a Django reverse one-to-one with no row raises an
    AttributeError subclass, and test stand-ins have no such attribute."""
    match = getattr(item, "mir_match", None)
    if match is None or getattr(match, "dismissed_by_override", False):
        return None
    return match


def item_received(item) -> bool:
    """Has anything arrived in MIR against this line?"""
    return _live_match(item) is not None


def item_fully_received(item, boe_total=None) -> bool:
    """Arrived and not short. Mirrors flags.js's lineItemFullyReceived():
    an over-delivered line still counts as received, a short one does not,
    and a line whose direction could not be measured (qty_over_delivered
    None) is not invented as short.

    And the ORDER is covered, not only what has cleared: the match compares
    MIR against the BOE quantity, so a line that shipped 252,000 of 504,000
    KG and received all 252,000 read "Delivered" and could never be Overdue
    (1000001450, 2026-09-25). `boe_total` is boe_totals()'s figure; a line
    whose BOE total is below its ordered quantity is still to come."""
    match = _live_match(item)
    if match is None:
        return False
    shipped = item.qty_as_per_boe if boe_total is None else boe_total
    ordered = getattr(item, "qty_as_per_po", None)
    if shipped is not None and ordered and shipped < ordered:
        return False
    qty_diff = getattr(match, "qty_diff_pct", None)
    return not (qty_diff is not None and qty_diff > 0 and getattr(match, "qty_over_delivered", None) is False)


def material_inwarded(items) -> bool:
    """Every line fully received in MIR - the Import "Material Inwarded" KPI,
    same definition as Domestic's Received status."""
    totals = boe_totals(items)
    return bool(items) and all(item_fully_received(i, totals.get(id(i))) for i in items)


# ── Delivery date status (spec section 2) ───────────────────────────────────

STATUS_UNKNOWN = "Unknown"
STATUS_OVERDUE = "Overdue"
STATUS_ON_ORDER = "On Order"
STATUS_DELIVERED = "Delivered"


def delivery_date_status(item, today: datetime.date, boe_total=None) -> str:
    """"Delivered" once the line is fully received in MIR; otherwise
    Unknown/Overdue/On Order from delivery_date. Customs clearance alone is
    not delivery: a BOE says the goods cleared the port, not that they
    reached the plant, and measured 2026-09-25 on Vapi 15 of 38 cleared
    import POs had no MIR receipt at all - they could never read Overdue
    while clearance counted as delivered."""
    if item_fully_received(item, boe_total):
        return STATUS_DELIVERED
    if item.delivery_date is None:
        return STATUS_UNKNOWN
    return STATUS_OVERDUE if item.delivery_date < today else STATUS_ON_ORDER


def po_delivery_date_status(items, today: datetime.date) -> str:
    """PO-level rollup, priority order matches how a buyer would triage a PO's
    own item list: any item still Overdue makes the whole PO "Overdue" to
    chase, else any item Unknown makes it worth flagging, else On Order beats
    Delivered (a PO isn't done until every item is), else Delivered."""
    totals = boe_totals(items)
    statuses = {delivery_date_status(i, today, totals.get(id(i))) for i in items}
    if STATUS_OVERDUE in statuses:
        return STATUS_OVERDUE
    if STATUS_UNKNOWN in statuses:
        return STATUS_UNKNOWN
    if STATUS_ON_ORDER in statuses:
        return STATUS_ON_ORDER
    return STATUS_DELIVERED


# ── Partial delivery (spec section 2, PO level) ─────────────────────────────

def partial_delivery(items) -> bool:
    """Something has arrived in MIR but the order is not complete - a line
    has no receipt yet, or one arrived short. Same rule as Domestic's
    Partial Delivered status. A BOE quantity below the ordered quantity is a
    partial SHIPMENT, not a partial delivery, and is already counted by the
    "Qty Mismatches (PO vs BOE)" card via qty_discrepancy()."""
    totals = boe_totals(items)
    return any(item_received(i) for i in items) and not all(item_fully_received(i, totals.get(id(i))) for i in items)


# ── Data quality flags F1-F7 (spec section 4) ───────────────────────────────

def _hsn_malformed(hsn: str) -> bool:
    """A valid Indian HSN code is all-digit and either 6 or 8 characters
    long - anything else (blank is exempted, not flagged as malformed;
    "blank" is its own separate data-quality signal elsewhere) trips F5."""
    if not hsn:
        return False
    digits = hsn.strip()
    return not (digits.isdigit() and len(digits) in (6, 8))


def item_flags(item) -> list[dict]:
    """Flags evaluated against a single item row (F1, F2, F4, F5, F6, F7).
    F3 needs the whole PO's items and is evaluated separately by po_flags()."""
    flags = []
    if item.boe_number and item.qty_as_per_boe is None:
        flags.append({
            "code": "F1", "item_id": item.item_id,
            "message": "BOE filed but QTY (As Per BOE) is missing.",
            "fields": ["BOE Number", "QTY (As Per BOE)"],
        })
    if item.boe_number and (not item.tax_type or item.exchange_rate is None or item.total_inclusive_value is None):
        flags.append({
            "code": "F2", "item_id": item.item_id,
            "message": "BOE filed but landed-cost fields are incomplete (Tax Type / Exchange Rate / Total Inclusive Value).",
            "fields": ["BOE Number", "Tax Type", "Exchange Rate", "Total Inclusive Value"],
        })
    if item.laden_on_board_date and not item.bill_of_lading_number:
        flags.append({
            "code": "F4", "item_id": item.item_id,
            "message": "Laden on Board date is present but Bill of Lading number is missing.",
            "fields": ["Laden on Board Date", "Bill Of Lading Number"],
        })
    if _hsn_malformed(item.hsn):
        flags.append({
            "code": "F5", "item_id": item.item_id,
            "message": f"HSN code {item.hsn!r} is not 6 or 8 digits.",
            "fields": ["HSN"],
        })
    if item.boe_number and not item.currency_after_taxes:
        flags.append({
            "code": "F6", "item_id": item.item_id,
            "message": "BOE filed but Currency (After Taxes) is blank - should default to INR once cleared.",
            "fields": ["BOE Number", "Currency (After Taxes)"],
        })
    if item.delivery_date is None and item.delivery_date_raw:
        flags.append({
            "code": "F7", "item_id": item.item_id,
            "message": f"Delivery Date {item.delivery_date_raw!r} is free text, not a parseable date.",
            "fields": ["Delivery Date"],
        })
    return flags


def po_flags(po_number: str, items) -> list[dict]:
    """All flags for a PO: every item's own flags, plus F3 (needs siblings
    under the same PO+BOE to compare completeness across)."""
    flags = []
    # Each flag's dismissal key. "code:item_id" as before where the item_id
    # names one line - so every dismissal already stored keeps applying - and
    # "code:item_id#<position>" where the order repeats an item_id across
    # shipment lines (12 Vapi orders do), since dismissing one line's flag
    # used to dismiss it on all of them. Position is the line's index by pk,
    # the same numbering as matching_core.line_item_positions().
    ordered = sorted(items, key=lambda i: getattr(i, "id", 0) or 0)
    id_counts: dict = {}
    for item in ordered:
        id_counts[item.item_id] = id_counts.get(item.item_id, 0) + 1
    for n, item in enumerate(ordered):
        for f in item_flags(item):
            f["item_ref"] = str(n)
            f["flag_key"] = f"{f['code']}:{item.item_id or ''}" + (f"#{n}" if id_counts[item.item_id] > 1 else "")
            flags.append(f)

    by_boe: dict[str, list] = {}
    for item in items:
        if item.boe_number:
            by_boe.setdefault(item.boe_number, []).append(item)
    for boe_number, group in by_boe.items():
        if len(group) < 2:
            continue

        def _complete(i):
            return bool(i.tax_type) and i.exchange_rate is not None and i.total_inclusive_value is not None

        completeness = {_complete(i) for i in group}
        if len(completeness) > 1:
            flags.append({
                "code": "F3", "item_id": None, "item_ref": None, "flag_key": "F3:",
                "message": f"PO {po_number}, BOE {boe_number}: some items have Tax Type/Exchange Rate/"
                           f"Total Inclusive Value filled and others don't, for the same BOE.",
                "fields": ["Tax Type", "Exchange Rate", "Total Inclusive Value"],
            })
    return flags
