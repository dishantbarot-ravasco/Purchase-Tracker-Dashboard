from django.db import migrations, models


def drop_stock_planner_presets(apps, schema_editor):
    """The Stock Planner tab was replaced by Stock & Orders the day it
    shipped (2026-09-29, project owner), so a preset saved for it names
    columns no list has any more - drop them rather than leave rows no page
    can read."""
    apps.get_model("core", "SortPreset").objects.filter(view="stock_planner").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0080_sort_preset_plant_stock_views"),
    ]

    operations = [
        migrations.RunPython(drop_stock_planner_presets, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="sortpreset",
            name="view",
            field=models.CharField(
                choices=[
                    ("materials", "Raw Material Analysis"),
                    ("purchase_orders", "Purchase Orders (Domestic)"),
                    ("import_purchases", "Import Purchases"),
                    ("material_lots", "Raw Material modal: Stock by Plant"),
                    ("material_open_pos", "Raw Material modal: Open Purchase Orders"),
                    ("po_lines", "PO modal: line items"),
                    ("po_receipts", "PO modal: MIR receipts"),
                    ("search_po", "Search PO: results"),
                    ("search_po_items", "Search PO: line items"),
                    ("plant_inventory", "Inventory"),
                    ("plant_on_order", "On Order"),
                    ("plant_combined", "Stock & Orders"),
                ],
                max_length=30,
            ),
        ),
    ]
