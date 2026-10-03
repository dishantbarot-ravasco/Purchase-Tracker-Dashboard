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
  - An order the app owns (source APP: uploaded or entered in the app) is
    never written here, even when the CSV lists the same PO number. The
    order stays one row (one PO per plant and number), so its quantities
    are never counted twice; the Drive side keeps its own copy in the
    legacy mirror, and the two are compared, never added.
  - If the CSV changes what a line IS (material, item code or unit) after a
    receipt was posted against it, the line is flagged needs_review rather
    than silently re-pointed: the receipt may no longer belong to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.services import materials
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
    # PO numbers the CSV also lists but the app owns (source APP): left as
    # the app has them.
    orders_held: list = field(default_factory=list)


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
        "material": materials.material_for(item.description, item.item_id, uom, item.hsn),
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
    write_lines(po, [_line_values(item) for item in legacy_items], result, "The PO sheet")


def write_lines(po, lines_values, result: ProjectionResult, changed_by: str) -> None:
    """Bring a PO's lines in line with `lines_values` (one dict of
    PurchaseOrderLine fields per line, in PO order; line N is position N), as
    a diff: an unchanged line is not touched, every change is logged, a line
    with posted receipts whose identity changes is flagged for review, and a
    line no longer listed is retired, never deleted. `changed_by` names the
    source in the review note ("The PO sheet", "The approved PO file")."""
    from apps.core.models import PurchaseOrderLine, PurchaseOrderLineChange

    existing = {line.line_no: line for line in po.lines.all()}
    for position, values in enumerate(lines_values, start=1):
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
        written = [f for f, _o, _n in changes] + (["is_active"] if reactivated else [])
        if received and identity:
            line.needs_review = True
            line.review_note = (f"{changed_by} changed {', '.join(identity)} after receipts were posted against this "
                                f"line on {timezone.localdate():%d-%m-%Y}. Confirm the receipts still belong here.")
            result.lines_flagged.append(f"{po.po_number} line {position}")
            written += ["needs_review", "review_note"]
        # Only what the sheet changed: the row was read without a lock, so a
        # whole-row save put back a short-close or review a person had just
        # committed on this line (closed_at and the rest).
        line.save(update_fields=written)
        result.lines_updated += 1
    for line_no, line in existing.items():
        if line_no > len(lines_values) and line.is_active:
            line.is_active = False
            if _has_receipts(line):
                line.needs_review = True
                line.review_note = (f"{changed_by} dropped this line on {timezone.localdate():%d-%m-%Y} after receipts "
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
    plants_by_code = {p.code: p for p in Plant.objects.all()}
    legacy_model = _legacy_model(plant_code)
    result = ProjectionResult()
    current = {po.po_number: po for po in PurchaseOrder.objects.filter(plant=plant, kind=PurchaseOrder.Kind.DOMESTIC)}
    # Oldest first, so the newest order's vendor details are the ones kept.
    legacy_orders = legacy_model.objects.order_by("po_created_date", "pk").prefetch_related("items")
    for legacy in legacy_orders:
        result.orders_seen += 1
        po = current.get(legacy.po_number)
        if po is not None and po.source == PurchaseOrder.Source.APP:
            result.orders_held.append(legacy.po_number)
            continue
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
            "billing_plant": plants_by_code.get(rules.billing_plant_code(legacy.billing_address or "")),
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


# The per-plant import CSV mirrors (2026-10-03), read-only here.
IMPORT_PO_MODELS = {
    "hrs": "HRSImportPurchaseOrder",
    "achhad": "RTPAchhadImportPurchaseOrder",
    "vapi": "RTPVapiImportPurchaseOrder",
}


def import_line_groups(items) -> list[list]:
    """The import CSV repeats a PO line once per shipment (1000001519's SBR
    line is three rows: one PO quantity, three BOEs). One ordered item is
    one PO line: rows group by item code, description and net price, in the
    order the item first appears. Each group's rows are its shipments."""
    groups: dict = {}
    for item in sorted(items, key=lambda i: i.pk):
        key = ((item.item_id or "").strip(), " ".join((item.description or "").lower().split()),
               None if item.net_price is None else Decimal(str(item.net_price)))
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def _import_line_values(rows) -> dict:
    first = rows[0]
    uom, _known = rules.canonical_uom(first.uom)
    qty = next((r.qty_as_per_po for r in rows if r.qty_as_per_po is not None), None)
    dates = [r.delivery_date for r in rows if r.delivery_date]
    return {
        "item_code": (first.item_id or "").strip(),
        "description": (first.description or "").strip(),
        "hsn": (first.hsn or "").strip(),
        "uom": uom,
        "uom_raw": (first.uom or "").strip()[:20],
        "qty_ordered": qty,
        "rate": first.net_price,
        "net_value": next((r.net_value for r in rows if r.net_value is not None), None),
        "delivery_date": min(dates) if dates else None,
        "material": materials.material_for(first.description, first.item_id, uom, first.hsn),
    }


@transaction.atomic
def project_plant_import_orders(plant_code: str) -> ProjectionResult:
    """The import CSV mirror into PurchaseOrder (kind IMPORT), like
    project_plant_orders(): one PO line per ordered item
    (import_line_groups()), in the PO's own currency, with no tax at order
    time. An order the app owns (source APP) is left as it is."""
    from apps.core import models as core_models
    from apps.core.models import Plant, PurchaseOrder

    plant = Plant.objects.get(code=plant_code)
    plants_by_code = {p.code: p for p in Plant.objects.all()}
    mirror = getattr(core_models, IMPORT_PO_MODELS[plant_code])
    result = ProjectionResult()
    current = {po.po_number: po for po in PurchaseOrder.objects.filter(plant=plant, kind=PurchaseOrder.Kind.IMPORT)}
    for legacy in mirror.objects.order_by("po_created_date", "pk").prefetch_related("items"):
        result.orders_seen += 1
        po = current.get(legacy.po_number)
        if po is not None and po.source == PurchaseOrder.Source.APP:
            result.orders_held.append(legacy.po_number)
            continue
        if po is not None and po.source_hash == legacy.synced_from_row_hash and po.is_active == legacy.is_active:
            continue
        vendor = upsert_vendor(legacy.vendor_gstin, legacy.vendor_name, legacy.vendor_code,
                               legacy.vendor_address, legacy.vendor_email)
        header = {
            "po_date": legacy.po_created_date, "vendor": vendor,
            "currency": (legacy.currency or "").strip().upper()[:10] or "INR",
            "tax_type": "", "tax_type_raw": "",
            "payment_terms": legacy.payment_terms or "", "incoterms": legacy.incoterms or "",
            "billing_address": legacy.billing_address or "",
            "billing_plant": plants_by_code.get(rules.billing_plant_code(legacy.billing_address or "")),
            "ship_to": legacy.ship_to or "", "total_value": legacy.total_value, "total_inclusive_value": None,
            "remarks": legacy.remarks or "", "is_active": legacy.is_active,
            "source_hash": legacy.synced_from_row_hash, "synced_at": timezone.now(),
        }
        if po is None:
            po = PurchaseOrder.objects.create(plant=plant, kind=PurchaseOrder.Kind.IMPORT, po_number=legacy.po_number, **header)
        else:
            for f, v in header.items():
                setattr(po, f, v)
            po.save()
        write_lines(po, [_import_line_values(rows) for rows in import_line_groups(legacy.items.all())], result,
                    "The import PO sheet")
        result.orders_written += 1
    return result
