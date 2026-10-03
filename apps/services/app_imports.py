"""
The Import Purchases page from the app's own records (owner, 2026-10-03), for
a plant whose import source is "app" (PlantStockSource.import_source).

The page is built from the import CSV's rows - one per PO line per shipment -
by imports_views._po_dict() and the rules in import_flags.py (shipment stage,
delivery status, partial delivery, PO vs BOE quantity). This module gives
those the same thing from the app: stand-in rows carrying the CSV row's
attribute names, one per ImportShipmentLine (a PO line on one Bill of Entry)
and one per PO line not shipped yet, and a stand-in order around them. Every
existing rule then applies unchanged - the two sources are shown one at a
time, never added.

What arrived comes from POSTED import MIRs against that shipment line
(mir_service.accepted_by_shipment_line()), exactly - no matching. The row's
`app_match_payload` is the "mirMatch" the page shows for it.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

ZERO = Decimal("0")


def _f(v):
    return None if v is None else float(v)


class _Items(list):
    """`po.items.all()`, as the CSV order's reverse relation reads."""

    def all(self):
        return self


def _receipt(shipment_line, mir_lines, accepted):
    """The stand-in match for one shipment line, or None when nothing was
    received against it."""
    if not mir_lines:
        return None
    boe = shipment_line.qty_as_per_boe
    diff = abs(accepted - boe) / boe * 100 if boe else None
    rows = [{
        "manualNote": None, "mirNo": ml.mir.mir_no, "mirDate": ml.mir.mir_date.isoformat(), "invoiceNo": ml.mir.invoice_no,
        "sheetRow": None, "qty": _f(ml.qty_received - ml.qty_rejected), "uom": ml.uom or shipment_line.po_line.uom,
        "qtyInPoUnit": _f(ml.qty_received - ml.qty_rejected), "rate": _f(ml.rate), "value": _f(ml.taxable),
    } for ml in sorted(mir_lines, key=lambda m: (m.mir.mir_date, m.mir.mir_no))]
    value = sum((ml.taxable for ml in mir_lines), ZERO)
    payload = {
        "matchId": None, "tier": "app", "matchScore": 1.0, "matchedMirs": rows,
        "received": {"qty": _f(accepted), "rate": _f(value / accepted) if accepted else None, "value": _f(value), "comparable": True},
        "qtyDiffPct": _f(diff), "qtyOverDelivered": accepted >= boe, "qtyWithinTolerance": False,
        "rateDiffPct": None, "valueDiffPct": None, "isFlagged": False, "uomMismatch": False, "severity": "",
        "dismissedByOverride": False, "dismissedReason": "", "dismissedBy": None, "stockMatched": True,
        "materialMatched": True, "poNumberMatched": True, "vendorMatched": True, "qtyMismatched": False,
        "rateMismatched": False, "dataMismatch": False, "taxTypeMismatch": False, "exchangeRateMismatched": False,
        "netValueMismatched": False, "taxableValueMismatched": False, "finalValueMismatched": False,
        "rollsOrdered": None, "rollsReceived": None, "poolLineRefs": "", "receiptShare": None,
    }
    match = SimpleNamespace(qty_diff_pct=diff, qty_over_delivered=accepted >= boe, dismissed_by_override=False)
    return match, payload


def _row(n, line, sl=None, receipt=None):
    sh = sl.shipment if sl else None
    match, payload = receipt or (None, None)
    return SimpleNamespace(
        id=n, item_id=line.item_code, description=line.description, hsn=line.hsn,
        qty_as_per_po=line.qty_ordered, qty_as_per_boe=sl.qty_as_per_boe if sl else None, uom=line.uom,
        delivery_date=line.delivery_date, delivery_date_raw="", net_price=line.rate, net_value=line.net_value,
        tax_type="IGST" if sl else "", currency_after_taxes=sh.currency_after_taxes if sh else "",
        exchange_rate=sh.exchange_rate if sh else None, total_inclusive_value=sl.total_inclusive_value if sl else None,
        boe_number=sh.boe_number if sh else "", bill_of_lading_number=sh.bill_of_lading_number if sh else "",
        laden_on_board_date=sh.laden_on_board_date if sh else None, country_of_origin=sh.country_of_origin if sh else "",
        license_type=sl.license_type if sl else "", license_number=sl.license_number if sl else "",
        mir_match=match, app_match_payload=payload,
    )


def order_standins(plant, po_number: str | None = None) -> list:
    """The plant's active import POs as stand-in orders (one, by number, when
    `po_number` is given)."""
    from apps.core.models import ImportShipmentLine, MirLine, PurchaseOrder
    from apps.services import mir_service

    qs = (PurchaseOrder.objects.filter(plant=plant, kind=PurchaseOrder.Kind.IMPORT, is_active=True)
          .select_related("vendor").prefetch_related("lines"))
    if po_number is not None:
        qs = qs.filter(po_number=po_number)
    orders = list(qs)
    shipment_lines = defaultdict(list)
    for sl in (ImportShipmentLine.objects.filter(po_line__purchase_order__in=orders, is_active=True, shipment__is_active=True)
               .select_related("shipment", "po_line").order_by("shipment__boe_date", "shipment_id")):
        shipment_lines[sl.po_line_id].append(sl)
    all_sl = [sl for v in shipment_lines.values() for sl in v]
    accepted = mir_service.accepted_by_shipment_line([sl.id for sl in all_sl])
    mir_lines = defaultdict(list)
    for ml in MirLine.objects.filter(shipment_line__in=all_sl, mir__status="POSTED").select_related("mir"):
        mir_lines[ml.shipment_line_id].append(ml)
    out, n = [], 0
    for po in orders:
        items = _Items()
        for line in sorted(po.lines.all(), key=lambda x: x.line_no):
            if not line.is_active:
                continue
            sls = shipment_lines.get(line.id, [])
            if not sls:
                n += 1
                items.append(_row(n, line))
            for sl in sls:
                n += 1
                items.append(_row(n, line, sl, _receipt(sl, mir_lines.get(sl.id, []), accepted.get(sl.id, ZERO))))
        v = po.vendor
        out.append(SimpleNamespace(
            po_number=po.po_number, po_drive_folder_name="", vendor_name=v.name if v else "",
            po_created_date=po.po_date, payment_terms=po.payment_terms, incoterms=po.incoterms, currency=po.currency,
            vendor_address=v.address if v else "", vendor_gstin=v.gstin if v else "", vendor_email=v.email if v else "",
            vendor_code=v.vendor_code if v else "", billing_address=po.billing_address, ship_to=po.ship_to,
            total_value=po.total_value, remarks=po.remarks, items=items,
        ))
    return out
