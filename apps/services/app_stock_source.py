"""
Where the Inventory / On Order / Stock & Orders tabs read from, per plant
(project owner, 2026-10-03): "keep drive sheets until we totally move to in
app, so keep it changeable easily at a moment's notice".

  - "drive" (the default): the Drive RM sheet mirror and the Drive PO / MIR
    matching, served by the domestic routers' /materials and
    /purchase-orders as before.
  - "app": the app's own records - each RM store receipt with what it holds
    today (stock_service.plant_holdings()), and each PO line's open quantity
    from POSTED MIR lines (mir_service.accepted_by_line()).

The app payloads below have the SAME shape as the Drive ones (the fields
frontend/js/plant-stock.js and the materials.js helpers it calls read), so
the tabs render either without a branch. The two sources are shown one at
a time and never added: a plant reads one or the other.

An admin switches a plant with set_source(); nothing is copied or deleted, so
switching back is instant.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from apps.services import mir_service, stock_rules, stock_service

DRIVE = "drive"
APP = "app"
SOURCES = (DRIVE, APP)
# The issue history a Days Left rate is read over, like the Drive ledger's.
WINDOW_DAYS = 45


def sources() -> dict:
    """{plant code: "drive" | "app"} for every plant (no row = drive)."""
    from apps.core.models import Plant, PlantStockSource

    chosen = dict(PlantStockSource.objects.values_list("plant__code", "source"))
    return {p.code: chosen.get(p.code, DRIVE) for p in Plant.objects.all()}


def source_of(plant) -> str:
    from apps.core.models import PlantStockSource

    row = PlantStockSource.objects.filter(plant=plant).first()
    return row.source if row else DRIVE


def set_source(plant, source: str, user):
    from apps.core.models import PlantStockSource

    if source not in SOURCES:
        raise ValueError("Choose drive or app.")
    row, _ = PlantStockSource.objects.get_or_create(plant=plant)
    row.source = source
    row.updated_by_email = getattr(user, "email", "") or ""
    row.save()
    return row


def _band(observed_days: int) -> str:
    if observed_days >= 30:
        return "high"
    if observed_days >= 14:
        return "medium"
    return "low" if observed_days >= 1 else "none"


def _f(value):
    return None if value is None else float(value)


def materials_payload(plant) -> list[dict]:
    """One row per receipt the plant keeps in store, shaped like the Drive
    /materials rows (apps/api/routers/_domestic_base.py's _lot_dict()).
    Days Left's rate is the material's issues net of returns over the last
    WINDOW_DAYS (or since its first receipt here, when that is shorter); the
    minimum level is the RM store's StockSetting."""
    from apps.core.models import StockSetting

    holdings = stock_service.plant_holdings(plant, window_days=WINDOW_DAYS)
    today = timezone.localdate()
    used, first, held = defaultdict(Decimal), {}, defaultdict(Decimal)
    for h in holdings:
        lot = h["lot"]
        used[lot.material_id] += h["used"]
        first[lot.material_id] = min(first.get(lot.material_id, lot.received_date), lot.received_date)
        held[(lot.material_id, lot.uom)] += h["balance"]
    settings = {s.material_id: s for s in StockSetting.objects.filter(plant=plant)}

    def consumption(material_id):
        observed = min(WINDOW_DAYS, (today - first[material_id]).days + 1)
        quantity = used[material_id]
        band = _band(observed)
        if band == "none":
            return None
        return {"avgDaily": float(quantity) / observed if quantity > 0 else None, "quantity": float(quantity),
                "confidence": band, "coverageDays": observed, "observedDays": observed, "windowDays": WINDOW_DAYS}

    rows = []
    for h in holdings:
        lot, balance = h["lot"], h["balance"]
        material = lot.material
        cons = consumption(lot.material_id)
        setting = settings.get(lot.material_id)
        days_to_msl = None
        if setting and setting.min_level is not None and (setting.min_level_uom or lot.uom) == lot.uom:
            remaining = float(held[(lot.material_id, lot.uom)] - setting.min_level)
            if remaining <= 0:
                days_to_msl = 0.0
            elif cons and cons["avgDaily"]:
                days_to_msl = remaining / cons["avgDaily"]
        mir_line = lot.mir_line
        rows.append({
            "lotId": lot.id,
            "materialCode": (mir_line.po_line.item_code if mir_line else "") or str(lot.id),
            "description": material.name,
            "category": material.category or "Uncategorized",
            "subCategory": material.subcategory or "",
            "uom": lot.uom,
            "qty": float(balance),
            "rate": _f(lot.rate),
            "value": float(stock_rules.value(balance, lot.rate)) if balance > 0 and lot.currency == "INR" else 0.0,
            "vendor": lot.vendor.name if lot.vendor_id else None,
            "receivedDate": lot.received_date.isoformat(),
            "locationTag": lot.location.name if lot.location_id else None,
            "noOfDays": (today - lot.received_date).days,
            "consumption": cons,
            "daysToMsl": days_to_msl,
            # The receipt this row is, for the material panel.
            "mirNo": mir_line.mir.mir_no if mir_line else stock_service.doc_of(lot),
            # Reconciliation fields the Drive rows carry; the app has none.
            "corrections": [], "mirMatched": mir_line is not None, "mirStockMatches": [], "dataQualityFlags": [],
        })
    return rows


def purchase_orders_payload(plant) -> list[dict]:
    """The plant's active POs with every line still on order, shaped like the
    Drive /purchase-orders rows: a line's received quantity is what POSTED
    MIRs accepted (received less rejected) - exact, no matching. A
    short-closed or retired line is not on order and is left out; so is a PO
    with no such line left."""
    from apps.core.models import PurchaseOrder

    orders = list(PurchaseOrder.objects.filter(plant=plant, is_active=True)
                  .select_related("vendor").prefetch_related("lines__material").order_by("-po_date", "-id"))
    lines = [ln for po in orders for ln in po.lines.all() if ln.is_active and ln.closed_at is None]
    accepted = mir_service.accepted_by_line([ln.id for ln in lines])
    by_po = defaultdict(list)
    for ln in lines:
        by_po[ln.purchase_order_id].append(ln)
    out = []
    for po in orders:
        items = []
        for ln in sorted(by_po.get(po.id, []), key=lambda x: x.line_no):
            acc = accepted.get(ln.id, Decimal("0"))
            ordered = ln.qty_ordered
            diff = (abs(acc - ordered) / ordered * 100) if acc > 0 and ordered else None
            items.append({
                "itemId": str(ln.line_no), "description": ln.description, "qty": _f(ordered), "uom": ln.uom,
                "netPrice": _f(ln.rate), "netValue": _f(ln.net_value),
                "deliveryDate": ln.delivery_date.isoformat() if ln.delivery_date else None,
                "matched": acc > 0, "dismissedByOverride": False,
                "received": {"qty": float(acc), "comparable": True},
                "qtyDiffPct": _f(diff), "qtyOverDelivered": (acc >= ordered) if acc > 0 and ordered is not None else None,
                "category": ln.material.category if ln.material_id else "",
                "subCategory": ln.material.subcategory if ln.material_id else "",
            })
        if not items:
            continue
        out.append({
            "poNumber": po.po_number, "vendorName": po.vendor.name if po.vendor_id else "",
            "createdDate": po.po_date.isoformat() if po.po_date else None, "currency": po.currency,
            "totalValue": _f(po.total_value), "remarks": po.remarks, "flagDismissals": [], "items": items,
        })
    return out
