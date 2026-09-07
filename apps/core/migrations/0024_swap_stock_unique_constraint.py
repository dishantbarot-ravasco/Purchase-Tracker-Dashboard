# Snapshot Pipeline Rebuild, Phase A.5 - migration 3 of 4 (see CLAUDE.md).
# CAN FAIL: this is the one step in the sequence where a real collision
# migration 0023 didn't catch (or a name clash) would surface, as a
# constraint-creation error - deliberately split from 0022/0023 so a
# failure here leaves those two committed and inspectable rather than
# rolling back the whole natural_key backfill.
#
# Drops each plant's old row-number UniqueConstraint(source_row_ref) and
# replaces it with a partial UniqueConstraint(natural_key) WHERE
# natural_key != '' - partial so any row a still-unmigrated old deploy
# left with a blank natural_key can coexist during rollout.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_populate_stock_natural_key'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='hrsstocklot',
            name='uniq_hrs_stock_row',
        ),
        migrations.RemoveConstraint(
            model_name='rtpachhadstocklot',
            name='uniq_achhad_stock_row',
        ),
        migrations.RemoveConstraint(
            model_name='rtpvapistocklot',
            name='uniq_vapi_stock_row',
        ),
        migrations.AddConstraint(
            model_name='hrsstocklot',
            constraint=models.UniqueConstraint(condition=models.Q(('natural_key__gt', '')), fields=('natural_key',), name='uniq_hrs_stock_natural_key'),
        ),
        migrations.AddConstraint(
            model_name='rtpachhadstocklot',
            constraint=models.UniqueConstraint(condition=models.Q(('natural_key__gt', '')), fields=('natural_key',), name='uniq_achhad_stock_natural_key'),
        ),
        migrations.AddConstraint(
            model_name='rtpvapistocklot',
            constraint=models.UniqueConstraint(condition=models.Q(('natural_key__gt', '')), fields=('natural_key',), name='uniq_vapi_stock_natural_key'),
        ),
    ]
