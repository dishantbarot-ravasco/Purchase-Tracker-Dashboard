# Snapshot Pipeline Rebuild, Phase A.5 - migration 2 of 4 (see CLAUDE.md).
#
# Data migration: backfills natural_key for every existing StockLot row
# across all three plants, using apps/services/stock_identity.py's
# lot_natural_key()/OccurrenceCounter - the same functions the rewired
# sync_stock/sync_achhad_stock/sync_vapi_stock commands use going forward
# (migration risk: DATA, per the build plan - a real failure here is
# expected to be rare but possible, hence the collision check below and the
# real reverse, so this step can be retried/rolled back independently of
# migration 0022 (safe, already committed) and 0024 (the constraint swap
# that actually depends on this data being clean).
#
# .order_by("id") approximates each row's original sheet order (rows were
# created in sheet order by the very first sync that ever saw them) - this
# is what makes the OccurrenceCounter's suffixing reproducible. A fresh
# counter per model per direction of this migration; raises RuntimeError on
# any collision (two rows landing on the same key even after the occurrence
# suffix) rather than silently merging them - that would be a real bug in
# key composition, far cheaper to catch here than as silently spliced
# inventory discovered later.

from django.db import migrations

# (model_name, code_field, vendor_field) - vendor_field is None for Achhad,
# which has no vendor column at all (see RTPAchhadStockLot's docstring).
_PLANT_MODELS = [
    ("HRSStockLot", "sap_item_code", "party_name"),
    ("RTPAchhadStockLot", "sap_code", None),
    ("RTPVapiStockLot", "hsn_code", "supplier_name"),
]


def _populate(apps, model_name, code_field, vendor_field):
    from apps.services.stock_identity import OccurrenceCounter

    Model = apps.get_model("core", model_name)
    counter = OccurrenceCounter()
    seen_keys = set()
    batch = []
    for obj in Model.objects.order_by("id").iterator(chunk_size=500):
        vendor = getattr(obj, vendor_field) if vendor_field else ""
        key = counter.key_for(code=getattr(obj, code_field), description=obj.description, vendor=vendor)
        if key:
            if key in seen_keys:
                raise RuntimeError(
                    f"{model_name} id={obj.id}: natural_key {key!r} collides with an already-"
                    f"assigned key even after the occurrence suffix. This is a bug in "
                    f"stock_identity's key composition, not a genuine duplicate lot - fix it "
                    f"before re-running this migration."
                )
            seen_keys.add(key)
        obj.natural_key = key
        batch.append(obj)
        if len(batch) >= 500:
            Model.objects.bulk_update(batch, ["natural_key"])
            batch = []
    if batch:
        Model.objects.bulk_update(batch, ["natural_key"])


def populate_natural_keys(apps, schema_editor):
    for model_name, code_field, vendor_field in _PLANT_MODELS:
        _populate(apps, model_name, code_field, vendor_field)


def reverse_populate_natural_keys(apps, schema_editor):
    for model_name, _code_field, _vendor_field in _PLANT_MODELS:
        apps.get_model("core", model_name).objects.update(natural_key="")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_add_stock_natural_key'),
    ]

    operations = [
        migrations.RunPython(populate_natural_keys, reverse_populate_natural_keys),
    ]
