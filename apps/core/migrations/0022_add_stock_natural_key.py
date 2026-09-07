# Snapshot Pipeline Rebuild, Phase A.5 - migration 1 of 4 (see CLAUDE.md).
# AddField only, blank/indexed, no default-changing risk - safe to apply
# even before the sync commands are rewired to populate it (migration
# 0023) or the old row-number constraint is dropped (migration 0024).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_ptuser_token_version_alter_ptauditlog_action'),
    ]

    operations = [
        migrations.AddField(
            model_name='hrsstocklot',
            name='natural_key',
            field=models.CharField(blank=True, db_index=True, help_text="Stable business identity - see apps/services/stock_identity.py. Replaces source_row_ref as the sync upsert key so an inserted sheet row can't silently re-label this lot as a different material.", max_length=200),
        ),
        migrations.AddField(
            model_name='rtpachhadstocklot',
            name='natural_key',
            field=models.CharField(blank=True, db_index=True, help_text="See HRSStockLot.natural_key's help_text. Achhad's key is weaker by necessity - no vendor column, so two lots of the same material are separated only by the occurrence counter (apps/services/stock_identity.py) - the same weaker-gate precedent matching_achhad.py already sets, still strictly better than a row number.", max_length=200),
        ),
        migrations.AddField(
            model_name='rtpvapistocklot',
            name='natural_key',
            field=models.CharField(blank=True, db_index=True, help_text="See HRSStockLot.natural_key's help_text - Vapi's copy, keyed on hsn_code/supplier_name (apps/services/stock_identity.py).", max_length=200),
        ),
        migrations.AlterField(
            model_name='hrsstocklot',
            name='is_active',
            field=models.BooleanField(default=True, help_text="False once a sync no longer sees this natural_key in the sheet (lot sold out/removed). Deactivating instead of deleting preserves this lot's HRSStockSnapshot history (CASCADE) and excludes it from matching - see HRSMIREntry.is_active's help_text for the same is_active/CASCADE reasoning (that model still keys on source_row_ref; this one no longer does)."),
        ),
        migrations.AlterField(
            model_name='hrsstocklot',
            name='source_row_ref',
            field=models.CharField(blank=True, help_text="Sheet row number at last sync - diagnostic only. natural_key (below) is the real identity; a row number shifts if a row is inserted/deleted above it, see natural_key's own help_text.", max_length=20),
        ),
        migrations.AlterField(
            model_name='rtpachhadstocklot',
            name='source_row_ref',
            field=models.CharField(blank=True, help_text="See HRSStockLot.source_row_ref's help_text - diagnostic only, Achhad's copy.", max_length=20),
        ),
        migrations.AlterField(
            model_name='rtpvapistocklot',
            name='source_row_ref',
            field=models.CharField(blank=True, help_text="See HRSStockLot.source_row_ref's help_text - diagnostic only, Vapi's copy.", max_length=20),
        ),
    ]
