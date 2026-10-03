# A PO belongs to the plant on its billing address (project owner,
# 2026-10-03). Adds PurchaseOrder.billing_plant and fills it for every
# existing order from its billing address - the CSV projection skips an
# unchanged order, so it would never fill them itself.

import django.db.models.deletion
from django.db import migrations, models

from apps.services.procurement_rules import billing_plant_code


def backfill(apps, schema_editor):
    Plant = apps.get_model("core", "Plant")
    PurchaseOrder = apps.get_model("core", "PurchaseOrder")
    plants = {p.code: p.id for p in Plant.objects.all()}
    for po in PurchaseOrder.objects.only("id", "billing_address").iterator():
        plant_id = plants.get(billing_plant_code(po.billing_address))
        if plant_id:
            PurchaseOrder.objects.filter(pk=po.pk).update(billing_plant_id=plant_id)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0093_otpcode_purpose'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseorder',
            name='billing_plant',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.plant'),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
