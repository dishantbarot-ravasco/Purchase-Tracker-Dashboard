"""
Base units (2026-09-30, project owner: "build the base unit and conversions
keep the base units as KG, L, Nos, m").

Every material gets `base_uom` from its PO lines: the base unit (KG, L, NOS,
M) of the unit most of its lines are ordered in, else of the unit on the
material itself, else none. Then each MIR lot whose unit converts exactly into
its material's base unit (MT into KG) is converted - the lot, the voucher
lines about it and their allocations multiplied by the factor, the rate
re-worked from the MIR line - when every figure stays exact at 3 decimals and
every voucher line touching it is about that lot alone. Any other lot keeps
its unit and still works.
"""

import re
from collections import Counter
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models

from apps.services import procurement_rules, stock_rules

QTY = Decimal("0.001")
RATE = Decimal("0.0001")


def _base_from(raw):
    code, _known = procurement_rules.canonical_uom(raw)
    if not stock_rules.base_of(code):
        # The reference list writes "Kilogram (KG)".
        m = re.search(r"\(([^)]+)\)\s*$", raw or "")
        code = procurement_rules.canonical_uom(m.group(1))[0] if m else code
    return stock_rules.base_of(code)


def base_units(apps, schema_editor):
    Material = apps.get_model("core", "Material")
    PurchaseOrderLine = apps.get_model("core", "PurchaseOrderLine")
    by_material: dict = {}
    for material_id, uom in PurchaseOrderLine.objects.exclude(material__isnull=True).values_list("material_id", "uom"):
        base = stock_rules.base_of(uom)
        if base:
            by_material.setdefault(material_id, Counter())[base] += 1
    for material in Material.objects.all():
        counts = by_material.get(material.id)
        base = counts.most_common(1)[0][0] if counts else _base_from(material.uom)
        if base != material.base_uom:
            material.base_uom = base
            material.save(update_fields=["base_uom"])


def lots_in_base_units(apps, schema_editor):
    StockLot = apps.get_model("core", "StockLot")
    StockAllocation = apps.get_model("core", "StockAllocation")
    StockVoucherLine = apps.get_model("core", "StockVoucherLine")
    for lot in StockLot.objects.filter(source="MIR", factor=1).select_related("material", "mir_line"):
        base = lot.material.base_uom
        exact = stock_rules.EXACT.get(lot.uom)
        if not base or lot.uom == base or not exact or exact[0] != base:
            continue
        factor = exact[1]
        allocations = list(StockAllocation.objects.filter(lot=lot).select_related("voucher_line"))
        lines = {a.voucher_line_id: a.voucher_line for a in allocations}
        lines.update({vl.id: vl for vl in StockVoucherLine.objects.filter(lot=lot)})
        if any(vl.lot_id != lot.id for vl in lines.values()):
            continue
        figures = [a.qty for a in allocations]
        for vl in lines.values():
            figures += [x for x in (vl.qty, vl.counted_qty, vl.book_qty) if x is not None]
        if any((x * factor) != (x * factor).quantize(QTY) for x in figures):
            continue
        for a in allocations:
            a.qty = (a.qty * factor).quantize(QTY)
            a.save(update_fields=["qty"])
        for vl in lines.values():
            vl.qty = (vl.qty * factor).quantize(QTY)
            vl.counted_qty = None if vl.counted_qty is None else (vl.counted_qty * factor).quantize(QTY)
            vl.book_qty = None if vl.book_qty is None else (vl.book_qty * factor).quantize(QTY)
            vl.uom = base
            vl.save(update_fields=["qty", "counted_qty", "book_qty", "uom"])
        line = lot.mir_line
        lot.uom, lot.factor = base, factor
        lot.rate = stock_rules.lot_rate(line.taxable, line.qty_received, factor)
        lot.save(update_fields=["uom", "factor", "rate"])



class Migration(migrations.Migration):

    dependencies = [
        ('core', '0084_stock_lot_mir_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='material',
            name='base_uom',
            field=models.CharField(blank=True, choices=[('KG', 'KG - solids, by weight'), ('L', 'L - liquids'), ('NOS', 'Nos - pieces'), ('M', 'M - length')], default='', max_length=10),
        ),
        migrations.CreateModel(
            name='MaterialUnitFactor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uom', models.CharField(max_length=20)),
                ('factor', models.DecimalField(decimal_places=6, max_digits=16)),
                ('updated_by_email', models.CharField(blank=True, default='', max_length=255)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('material', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='unit_factors', to='core.material')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='core.ptuser')),
            ],
            options={
                'ordering': ['material_id', 'uom'],
                'constraints': [models.UniqueConstraint(fields=('material', 'uom'), name='uniq_material_unit_factor'), models.CheckConstraint(condition=models.Q(('factor__gt', 0)), name='material_unit_factor_positive')],
            },
        ),
        migrations.RunPython(base_units, migrations.RunPython.noop),
        migrations.RunPython(lots_in_base_units, migrations.RunPython.noop),
    ]
