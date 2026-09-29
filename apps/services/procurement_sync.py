"""
Projects each plant's PO master CSV mirror (HRSDomesticPurchaseOrder etc.,
written by sync_*_po_csv) into the normalized procurement tables the MIR form
reads (apps/core/models/procurement.py), as a diff (2026-09-28).

Runs at the end of every plant's PO sync, and on demand through
`manage.py sync_procurement_pos`. The CSV stays the only source of POs: this
writes nothing the CSV did not say, it only cleans it into one shape -
vendor rows keyed on GSTIN, units and tax types in canonical codes.

The rules that protect receipts:

  - A line is identified by its position in the order. Only fields that
    changed are written, and every change is logged in
    PurchaseOrderLineChange (old and new value).
  - A line is never deleted. One the CSV no longer lists is deactivated; an
    order the CSV no longer lists is deactivated. Receipts against either
    stay exactly as posted.
  - If the CSV changes what a line IS (material, item code or unit) after a
    receipt was posted against it, the line is flagged needs_review rather
    than silently re-pointed: the receipt may no longer belong to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.services import procurement_rules as rules

# The per-plant CSV mirrors, read-only here.
LEGACY_PO_MODELS = {
    "hrs": "HRSDomesticPurchaseOrder",
    "achhad": "RTPAchhadDomesticPurchaseOrder",
    "vapi": "RTPVapiDomesticPurchaseOrder",
}

# A change to one of these after a receipt needs a person to confirm the
# receipt still belongs to the line.
_IDENTITY_FIELDS = ("item_code", "description", "uom")
_LINE_FIELDS = ("item_code", "description", "hsn", "uom", "uom_raw", "qty_ordered", "rate", "net_value", "delivery_date")


@dataclass
class ProjectionResult:
    orders_seen: int = 0
    orders_written: int = 0
    lines_created: int = 0
    lines_updated: int = 0
    lines_deactivated: int = 0
    lines_flagged: list = field(default_factory=list)


def _legacy_model(plant_code: str):
    from apps.core import models as core_models

    return getattr(core_models, LEGACY_PO_MODELS[plant_code])


def upsert_vendor(gstin_raw: str, name: str, code: str = "", address: str = "", email: str = ""):
    """The Vendor for a PO's vendor fields, created or refreshed. Keyed on
    GSTIN; a vendor with none is keyed on its cleaned name. None when the
    PO names no vendor at all."""
    from apps.core.models import Vendor

    gstin = rules.clean_gstin(gstin_raw)
    name = (name or "").strip()
    name_key = rules.vendor_name_key(name)
    if not gstin and not name_key:
        return None
    values = {"name": name or gstin, "name_key": name_key or gstin.lower(), "vendor_code": (code or "").strip(),
              "address": (address or "").strip(), "email": (email or "").strip()}
    lookup = {"gstin": gstin} if gstin else {"gstin": "", "name_key": values["name_key"]}
    vendor = Vendor.objects.filter(**lookup).first()
    if vendor is None:
        return Vendor.objects.create(gstin=gstin, **values)
    # The newest PO's details win (orders are projected oldest first); a
    # blank value never erases one already on file.
    changed = [f for f, v in values.items() if v and getattr(vendor, f) != v]
    for f in changed:
        setattr(vendor, f, values[f])
    if changed:
        vendor.save(update_fields=changed + ["updated_at"])
    return vendor


def _has_receipts(line) -> bool:
    return line.pk is not None and line.mir_lines.filter(mir__status="POSTED").exists()


def _line_values(item) -> dict:
    uom, _known = rules.canonical_uom(item.uom)
    return {
        "item_code": (item.item_id or "").strip(),
        "description": (item.description or "").strip(),
        "hsn": (item.hsn or "").strip(),
        "uom": uom,
        "uom_raw": (item.uom or "").strip()[:20],
        "qty_ordered": item.qty,
        "rate": item.net_price,
        "net_value": item.net_value,
        "delivery_date": item.delivery_date,
    }


def _same(a, b) -> bool:
    """Equal as stored: Decimals compared by value (1.50 == 1.5), the rest
    as they are."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return Decimal(str(a)) == Decimal(str(b))
    return a == b


def _project_lines(po, legacy_items, result: ProjectionResult) -> None:
    from apps.core.models import PurchaseOrderLine, PurchaseOrderLineChange

    existing = {line.line_no: line for line in po.lines.all()}
    for position, item in enumerate(legacy_items, start=1):
        values = _line_values(item)
        line = existing.get(position)
        if line is None:
            PurchaseOrderLine.objects.create(purchase_order=po, line_no=position, **values)
            result.lines_created += 1
            continue
        changes = [(f, getattr(line, f), v) for f, v in values.items() if not _same(getattr(line, f), v)]
        reactivated = not line.is_active
        if not changes and not reactivated:
            continue
        received = _has_receipts(line)
        for f, _old, new in changes:
            setattr(line, f, new)
        if reactivated:
            line.is_active = True
        PurchaseOrderLineChange.objects.bulk_create(
            [PurchaseOrderLineChange(po_line=line, field=f, old_value="" if old is None else str(old),
                                     new_value="" if new is None else str(new)) for f, old, new in changes]
            + ([PurchaseOrderLineChange(po_line=line, field="is_active", old_value="False", new_value="True")] if reactivated else [])
        )
        identity = [f for f, _o, _n in changes if f in _IDENTITY_FIELDS]
        if received and identity:
            line.needs_review = True
            line.review_note = (f"The PO sheet changed {', '.join(identity)} after receipts were posted against this "
                                f"line on {timezone.localdate():%d-%m-%Y}. Confirm the receipts still belong here.")
            result.lines_flagged.append(f"{po.po_number} line {position}")
        line.save()
        result.lines_updated += 1
    for line_no, line in existing.items():
        if line_no > len(legacy_items) and line.is_active:
            line.is_active = False
            if _has_receipts(line):
                line.needs_review = True
                line.review_note = (f"The PO sheet dropped this line on {timezone.localdate():%d-%m-%Y} after receipts "
                                    "were posted against it.")
                result.lines_flagged.append(f"{po.po_number} line {line_no}")
            line.save(update_fields=["is_active", "needs_review", "review_note"])
            PurchaseOrderLineChange.objects.create(po_line=line, field="is_active", old_value="True", new_value="False")
            result.lines_deactivated += 1


@transaction.atomic
def project_plant_orders(plant_code: str) -> ProjectionResult:
    """Bring the plant's normalized POs in line with its CSV mirror. Safe to
    run any number of times; an order whose mirror hash and active flag are
    unchanged is not touched."""
    from apps.core.models import Plant, PurchaseOrder

    plant = Plant.objects.get(code=plant_code)
    legacy_model = _legacy_model(plant_code)
    result = ProjectionResult()
    current = {po.po_number: po for po in PurchaseOrder.objects.filter(plant=plant)}
    # Oldest first, so the newest order's vendor details are the ones kept.
    legacy_orders = legacy_model.objects.order_by("po_created_date", "pk").prefetch_related("items")
    for legacy in legacy_orders:
        result.orders_seen += 1
        po = current.get(legacy.po_number)
        if po is not None and po.source_hash == legacy.synced_from_row_hash and po.is_active == legacy.is_active:
            continue
        vendor = upsert_vendor(legacy.vendor_gstin, legacy.vendor_name, legacy.vendor_code,
                               legacy.vendor_address, legacy.vendor_email)
        header = {
            "po_date": legacy.po_created_date,
            "vendor": vendor,
            "currency": (legacy.currency or "INR").strip() or "INR",
            "tax_type": rules.canonical_tax_type(legacy.tax_type),
            "tax_type_raw": (legacy.tax_type or "")[:100],
            "payment_terms": legacy.payment_terms or "",
            "incoterms": legacy.incoterms or "",
            "billing_address": legacy.billing_address or "",
            "ship_to": legacy.ship_to or "",
            "total_value": legacy.total_value,
            "total_inclusive_value": legacy.total_inclusive_value,
            "remarks": legacy.remarks or "",
            "is_active": legacy.is_active,
            "source_hash": legacy.synced_from_row_hash,
            "synced_at": timezone.now(),
        }
        if po is None:
            po = PurchaseOrder.objects.create(plant=plant, po_number=legacy.po_number, **header)
        else:
            for f, v in header.items():
                setattr(po, f, v)
            po.save()
        _project_lines(po, sorted(legacy.items.all(), key=lambda i: i.pk), result)
        result.orders_written += 1
    return result
