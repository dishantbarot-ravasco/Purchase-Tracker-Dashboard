"""
Shared upsert logic for the three sync_*_imports_po_csv management commands.

Unlike the domestic sync_po_csv.py/sync_vapi_po_csv.py/sync_achhad_po_csv.py
trio (which duplicate their ~140-line upsert logic verbatim per plant), Import
PO line items carry a lot more fields (BOE/shipment/license, on top of the
domestic set) - factored into one shared, model-class-parametrized helper here
so a bug fix or field addition only has to happen once. Each per-plant command
stays a thin ~30-line shell (plant enum + Drive file title + this call).
"""

import hashlib

from apps.services.sync_utils import deactivate_missing_orders, sync_line_items
from django.db import transaction
from django.utils import timezone


# Every parsed column an import line carries, written by sync_line_items().
IMPORT_LINE_FIELDS = (
    "item_id", "description", "hsn", "qty_as_per_po", "qty_as_per_boe", "uom", "delivery_date",
    "delivery_date_raw", "net_price", "net_value", "tax_type", "currency_after_taxes", "exchange_rate",
    "total_inclusive_value", "boe_number", "bill_of_lading_number", "laden_on_board_date",
    "country_of_origin", "license_type", "license_number",
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _order_hash(order) -> str:
    """Hash of every order + line-item field the parser produces, used by
    _upsert_order() as the "did anything actually change" check, so an
    unchanged order is skipped without touching a row. A changed order's
    lines are then diffed field-by-field by sync_utils.sync_line_items()."""
    parts = [
        order.po_drive_folder_name, order.po_number, str(order.po_created_date),
        order.vendor_name, order.vendor_address, order.vendor_gstin, order.vendor_email,
        order.vendor_code, order.billing_address, order.ship_to, order.payment_terms,
        order.incoterms, order.currency, str(order.total_value), order.remarks,
    ]
    for item in order.items:
        parts += [
            item.item_id, item.description, item.hsn, str(item.qty_as_per_po), str(item.qty_as_per_boe),
            item.uom, str(item.delivery_date), item.delivery_date_raw, str(item.net_price), str(item.net_value),
            item.tax_type, item.currency_after_taxes, str(item.exchange_rate), str(item.total_inclusive_value),
            item.boe_number, item.bill_of_lading_number, str(item.laden_on_board_date), item.country_of_origin,
            item.license_type, item.license_number,
        ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ── Public API ───────────────────────────────────────────────────────────────

def sync_orders(po_model, line_item_model, parsed_orders: list) -> tuple[int, int, int]:
    """Upserts every parsed order (skipping unchanged ones via a row hash,
    same convention as sync_po_csv.py), then deactivates the ones the master
    CSV no longer lists. Returns (rows_seen, rows_changed, deactivated).
    Caller wraps this in transaction.atomic() at the command level if it also
    needs SyncRun bookkeeping to share the same transaction - here each order
    gets its own atomic block so one bad order doesn't roll back the rest.

    The deactivation pass (2026-09-18) is the import side of the fix
    described in sync_utils.deactivate_missing_orders(). Import POs were
    worse off than domestic ones: the domestic syncs at least REPORTED
    orphans to stdout, while these three had no orphan handling of any kind,
    so a renamed import order forked silently with nothing said anywhere.
    One pass here covers all three plants, since all three commands
    delegate to this function."""
    rows_seen = 0
    rows_changed = 0
    for parsed in parsed_orders:
        rows_seen += 1
        with transaction.atomic():
            if _upsert_order(po_model, line_item_model, parsed):
                rows_changed += 1
    # After every upsert, so an order that was renamed is re-created under its
    # new number before its old spelling is retired - never the other way
    # round, which would briefly leave the order book without it.
    deactivated = len(deactivate_missing_orders(po_model, parsed_orders))
    return rows_seen, rows_changed, deactivated


def _upsert_order(po_model, line_item_model, parsed) -> bool:
    """Upserts one order + its line items, skipping the write entirely if
    the row hash is unchanged. A changed order's header is written in place
    and its lines as a diff keyed on position (sync_utils.sync_line_items()),
    the same rule as the domestic syncs: a line keeps its pk, so the match
    dismissals keyed on it (match_pairs.py) survive."""
    row_hash = _order_hash(parsed)
    existing = po_model.objects.filter(po_number=parsed.po_number).first()
    # `existing.is_active and` matters: without it an order that was
    # deactivated (its number vanished from the CSV) and then came back
    # UNCHANGED would hash-skip here and stay invisible forever.
    if existing and existing.is_active and existing.synced_from_row_hash == row_hash:
        return False

    order, _ = po_model.objects.update_or_create(
        po_number=parsed.po_number,
        defaults=dict(
            is_active=True,
            po_drive_folder_name=parsed.po_drive_folder_name,
            po_created_date=parsed.po_created_date,
            vendor_name=parsed.vendor_name,
            vendor_address=parsed.vendor_address,
            vendor_gstin=parsed.vendor_gstin,
            vendor_email=parsed.vendor_email,
            vendor_code=parsed.vendor_code,
            billing_address=parsed.billing_address,
            ship_to=parsed.ship_to,
            payment_terms=parsed.payment_terms,
            incoterms=parsed.incoterms,
            currency=parsed.currency,
            total_value=parsed.total_value,
            remarks=parsed.remarks,
            synced_from_row_hash=row_hash,
            last_synced_at=timezone.now(),
        ),
    )
    # A diff keyed on line position, never delete-and-rebuild: a line's pk
    # keys its match dismissals (match_pairs.py), so a rebuild on a routine
    # change (a BOE number or exchange rate filled in) silently dropped them.
    sync_line_items(order, parsed.items, line_item_model, IMPORT_LINE_FIELDS)
    return True
