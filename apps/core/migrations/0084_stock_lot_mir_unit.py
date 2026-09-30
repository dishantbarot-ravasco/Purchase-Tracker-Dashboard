"""
A stock lot keeps its MIR line's own unit (2026-09-30, project owner: "why the
weight is always in kg ... why can't we copy the same UOM as PO or MIR?").

Lots made before this held MT and G in KG (factor 1000 / 0.001). Each is
turned back into its MIR line's unit - the lot, the voucher lines about it and
their allocations divided by the factor, the rate re-worked from the line - when
every figure divides exactly at 3 decimals and every voucher line touching it
is about that lot alone. A lot that fails either test (an old FIFO line that
drew several lots, or a quantity that would round) is left in KG, where it
still works.
"""

from decimal import Decimal

from django.db import migrations

QTY = Decimal("0.001")
RATE = Decimal("0.0001")


def lots_in_their_mir_unit(apps, schema_editor):
    StockLot = apps.get_model("core", "StockLot")
    StockAllocation = apps.get_model("core", "StockAllocation")
    StockVoucherLine = apps.get_model("core", "StockVoucherLine")
    for lot in StockLot.objects.filter(source="MIR").exclude(factor=1).select_related("mir_line__po_line"):
        factor = lot.factor
        allocations = list(StockAllocation.objects.filter(lot=lot).select_related("voucher_line"))
        lines = {a.voucher_line_id: a.voucher_line for a in allocations}
        lines.update({vl.id: vl for vl in StockVoucherLine.objects.filter(lot=lot)})
        if any(vl.lot_id != lot.id for vl in lines.values()):
            continue
        figures = [a.qty for a in allocations]
        for vl in lines.values():
            figures += [x for x in (vl.qty, vl.counted_qty, vl.book_qty) if x is not None]
        if any((x / factor) != (x / factor).quantize(QTY) for x in figures):
            continue
        unit = (lot.mir_line.po_line.uom or "").strip().upper()
        for a in allocations:
            a.qty = (a.qty / factor).quantize(QTY)
            a.save(update_fields=["qty"])
        for vl in lines.values():
            vl.qty = (vl.qty / factor).quantize(QTY)
            vl.counted_qty = None if vl.counted_qty is None else (vl.counted_qty / factor).quantize(QTY)
            vl.book_qty = None if vl.book_qty is None else (vl.book_qty / factor).quantize(QTY)
            vl.uom = unit
            vl.save(update_fields=["qty", "counted_qty", "book_qty", "uom"])
        lot.uom = unit
        # Straight from the MIR line (taxable over quantity), not the rounded
        # per-KG rate scaled up.
        line = lot.mir_line
        lot.rate = (line.taxable / line.qty_received).quantize(RATE) if line.qty_received > 0 else None
        lot.factor = 1
        lot.save(update_fields=["uom", "rate", "factor"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0083_stock_issue_from_mir"),
    ]

    operations = [
        migrations.RunPython(lots_in_their_mir_unit, migrations.RunPython.noop),
    ]
