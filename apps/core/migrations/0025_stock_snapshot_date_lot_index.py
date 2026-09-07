# Snapshot Pipeline Rebuild, Phase C.4 - migration 4 of 4 (see CLAUDE.md).
# No risk (AddIndex only). Phase C's "read a plant's position on a date"
# endpoints scan by snapshot_date and join by stock_lot; the existing
# single-column snapshot_date index alone doesn't cover that access
# pattern. The Days-Left Engine (companion build plan 02) reads the same
# shape.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_swap_stock_unique_constraint'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='hrsstocksnapshot',
            index=models.Index(fields=['snapshot_date', 'stock_lot'], name='core_hrssto_snapsho_24d802_idx'),
        ),
        migrations.AddIndex(
            model_name='rtpachhadstocksnapshot',
            index=models.Index(fields=['snapshot_date', 'stock_lot'], name='core_rtpach_snapsho_3824c9_idx'),
        ),
        migrations.AddIndex(
            model_name='rtpvapistocksnapshot',
            index=models.Index(fields=['snapshot_date', 'stock_lot'], name='core_rtpvap_snapsho_fb5b73_idx'),
        ),
    ]
