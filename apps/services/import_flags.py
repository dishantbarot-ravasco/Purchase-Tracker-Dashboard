"""
Derived fields and data-quality flags for Import Purchase Orders, computed at
READ time (in apps/api/routers/imports_views.py) from already-fetched PO/item
rows, rather than stored - there is no separate reconciliation pass here the
way HRS/Achhad/Vapi's PO<->MIR matching has (no MIR-equivalent data source for
imports yet), so there is nothing to persist beyond the raw synced fields.

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

def qty_discrepancy(item) -> tuple[bool, float | None]:
    """Returns (is_discrepancy, pct). QTY (As Per BOE) missing is NOT a
    discrepancy - it means "not yet cleared", not a mismatch."""
    if item.qty_as_per_boe is None:
        return False, None
    if item.qty_as_per_po is None or item.qty_as_per_po == 0:
        return False, None
    is_mismatch = item.qty_as_per_boe != item.qty_as_per_po
    pct = float((item.qty_as_per_boe - item.qty_as_per_po) / item.qty_as_per_po * 100)
    return is_mismatch, pct


def po_has_qty_discrepancy(items) -> bool:
    """PO-level rollup: true if any item's BOE quantity disagrees with its
    ordered quantity - drives the Import KPI row's "Qty Discrepancies (BOE
    vs MIR)" style cards."""
    return any(qty_discrepancy(i)[0] for i in items)


# ── Delivery date status (spec section 2) ───────────────────────────────────

STATUS_UNKNOWN = "Unknown"
STATUS_OVERDUE = "Overdue"
STATUS_ON_ORDER = "On Order"
STATUS_DELIVERED = "Delivered"


def delivery_date_status(item, today: datetime.date) -> str:
    """A cleared item is always "Delivered" regardless of its own delivery
    date field (customs clearance is a stronger, later signal than a
    planned delivery date); otherwise Unknown/Overdue/On Order based on
    whether delivery_date is set and in the past."""
    if shipment_stage(item) == STAGE_CLEARED:
        return STATUS_DELIVERED
    if item.delivery_date is None:
        return STATUS_UNKNOWN
    return STATUS_OVERDUE if item.delivery_date < today else STATUS_ON_ORDER


def po_delivery_date_status(items, today: datetime.date) -> str:
    """PO-level rollup, priority order matches how a buyer would triage a PO's
    own item list: any item still Overdue makes the whole PO "Overdue" to
    chase, else any item Unknown makes it worth flagging, else On Order beats
    Delivered (a PO isn't done until every item is), else Delivered."""
    statuses = {delivery_date_status(i, today) for i in items}
    if STATUS_OVERDUE in statuses:
        return STATUS_OVERDUE
    if STATUS_UNKNOWN in statuses:
        return STATUS_UNKNOWN
    if STATUS_ON_ORDER in statuses:
        return STATUS_ON_ORDER
    return STATUS_DELIVERED


# ── Partial delivery (spec section 2, PO level) ─────────────────────────────

def partial_delivery(items) -> bool:
    """True if any item's cleared BOE quantity is strictly less than what
    was ordered - a real partial shipment, not just any qty mismatch
    (qty_discrepancy() above also flags a BOE qty that's *higher* than
    ordered, which isn't a "partial" delivery)."""
    return any(
        i.qty_as_per_boe is not None and i.qty_as_per_po is not None and i.qty_as_per_boe < i.qty_as_per_po
        for i in items
    )


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
    for item in items:
        flags.extend(item_flags(item))

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
                "code": "F3", "item_id": None,
                "message": f"PO {po_number}, BOE {boe_number}: some items have Tax Type/Exchange Rate/"
                           f"Total Inclusive Value filled and others don't, for the same BOE.",
                "fields": ["Tax Type", "Exchange Rate", "Total Inclusive Value"],
            })
    return flags
