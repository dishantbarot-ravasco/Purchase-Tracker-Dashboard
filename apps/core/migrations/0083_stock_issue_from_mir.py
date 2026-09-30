"""
RM store issues from a chosen MIR (2026-09-30, project owner: "in the RM the
user will have the option to select the MIR and issue the quantity ... all
other data gets transferred from the MIR data").

Each voucher line now names the MIR receipt (StockLot) it acts on, and an
issue's department is optional (the store's own sheet never recorded one).
Lines entered before this drew oldest-first and may span several lots; one
that drew exactly one lot is given it here, the rest stay null and keep
working through their allocations.
"""

import django.db.models.deletion
from django.db import migrations, models


def lot_for_single_lot_lines(apps, schema_editor):
    StockVoucherLine = apps.get_model("core", "StockVoucherLine")
    for line in StockVoucherLine.objects.filter(lot__isnull=True):
        lot_ids = set(line.allocations.values_list("lot_id", flat=True))
        if len(lot_ids) == 1:
            line.lot_id = lot_ids.pop()
            line.save(update_fields=["lot"])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0082_stock_entry'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='stockvoucher',
            name='stock_issue_has_department',
        ),
        migrations.AddField(
            model_name='stockvoucherline',
            name='lot',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='voucher_lines', to='core.stocklot'),
        ),
        migrations.AlterField(
            model_name='stockvoucher',
            name='kind',
            field=models.CharField(choices=[('ISSUE', 'Issue'), ('RETURN', 'Return to store'), ('ADJUST', 'Stock difference')], max_length=10),
        ),
        migrations.RunPython(lot_for_single_lot_lines, migrations.RunPython.noop),
    ]
