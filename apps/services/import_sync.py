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

from django.db import transaction
from django.utils import timezone


# ── Internal helpers ──────────────────────────────────────────────────────────

def _order_hash(order) -> str:
    """Hash of every order + line-item field the parser produces, used by
    _upsert_order() as the "did anything actually change" check - same
    change-detection idea as apps/services/sync_utils.py's unchanged(), but
    a plain hash instead of a per-field Decimal-quantizing comparison. That
    difference is deliberate, not an oversight: an Import PO's line items
    are always fully deleted and recreated on any change (see
    _upsert_order() below), so there's no persisted row to compare a single
    field against field-by-field the way sync_utils.unchanged() can against
    an existing model instance - hashing the whole parsed shape is the only
    way to tell "changed" from "unchanged" before touching the DB at all."""
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

def sync_orders(po_model, line_item_model, parsed_orders: list) -> tuple[int, int]:
    """Upserts every parsed order (skipping unchanged ones via a row hash,
    same convention as sync_po_csv.py). Returns (rows_seen, rows_changed).
    Caller wraps this in transaction.atomic() at the command level if it also
    needs SyncRun bookkeeping to share the same transaction - here each order
    gets its own atomic block so one bad order doesn't roll back the rest."""
    rows_seen = 0
    rows_changed = 0
    for parsed in parsed_orders:
        rows_seen += 1
        with transaction.atomic():
            if _upsert_order(po_model, line_item_model, parsed):
                rows_changed += 1
    return rows_seen, rows_changed


def _upsert_order(po_model, line_item_model, parsed) -> bool:
    """Upserts one order + its line items, skipping the write entirely if
    the row hash is unchanged. Line items have no stable natural key on
    their own within a PO the way the PO itself does (po_number) - see
    CLAUDE.md's "Domestic line items have no stable natural key" section
    for the same limitation on the domestic side - so on any real change
    every existing item under this PO is deleted and bulk-recreated rather
    than diffed and updated in place; simpler and safe since Import line
    items aren't individually FK'd from anywhere that would orphan on
    delete."""
    row_hash = _order_hash(parsed)
    existing = po_model.objects.filter(po_number=parsed.po_number).first()
    if existing and existing.synced_from_row_hash == row_hash:
        return False

    order, _ = po_model.objects.update_or_create(
        po_number=parsed.po_number,
        defaults=dict(
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
    order.items.all().delete()
    line_item_model.objects.bulk_create([
        line_item_model(
            purchase_order=order,
            item_id=item.item_id,
            description=item.description,
            hsn=item.hsn,
            qty_as_per_po=item.qty_as_per_po,
            qty_as_per_boe=item.qty_as_per_boe,
            uom=item.uom,
            delivery_date=item.delivery_date,
            delivery_date_raw=item.delivery_date_raw,
            net_price=item.net_price,
            net_value=item.net_value,
            tax_type=item.tax_type,
            currency_after_taxes=item.currency_after_taxes,
            exchange_rate=item.exchange_rate,
            total_inclusive_value=item.total_inclusive_value,
            boe_number=item.boe_number,
            bill_of_lading_number=item.bill_of_lading_number,
            laden_on_board_date=item.laden_on_board_date,
            country_of_origin=item.country_of_origin,
            license_type=item.license_type,
            license_number=item.license_number,
        )
        for item in parsed.items
    ])
    return True
