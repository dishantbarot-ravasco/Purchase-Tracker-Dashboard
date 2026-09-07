# Manually written, NOT auto-generated - see this migration's own note.
#
# Renames 12 model classes (and their underlying DB tables) to a consistent,
# source-file-matched naming scheme (project owner, 2026-09-08):
#   *PurchaseOrder  -> *DomesticPurchaseOrder  (explicit "Domestic", matching
#                                                the existing *ImportPurchaseOrder's
#                                                own explicit naming - "domestic"
#                                                was previously only implicit)
#   *POLineItem     -> *DomesticPOLineItem     (same reasoning)
#   *StockLot       -> *RMLot                  ("RM" - Raw Material - matches
#                                                the term used everywhere else in
#                                                this app: the "Raw Material
#                                                Analysis" frontend page, the
#                                                "RM file" Drive file name, the
#                                                Days-Left Engine)
#   *StockSnapshot  -> *RMSnapshot             (same reasoning)
# for all three plants (HRS, RTP-Achhad, RTP-Vapi).
#
# Written by hand rather than accepting `makemigrations`'s auto-detected
# rename prompts: this migration renames 12 models across 3 plants at once,
# and Django's interactive "was X renamed to Y?" prompts, answered
# non-interactively, matched several of them to the WRONG plant (e.g. it
# proposed renaming RTPAchhadPOLineItem to HRSDomesticPOLineItem) and failed
# to detect the *StockLot -> *RMLot renames as renames at all - it planned
# to DELETE HRSStockLot/RTPAchhadStockLot/RTPVapiStockLot and CREATE new
# empty RMLot tables instead, which would have destroyed every existing
# Stock/RM row on the next `migrate`. Caught and discarded before this was
# ever applied to a real database - RenameModel (data-preserving, an
# ALTER TABLE RENAME under the hood) is used explicitly here for every one
# of the 12 renames instead.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0035_imports_data_mismatch_vapi'),
    ]

    operations = [
        migrations.RenameModel(old_name='HRSPurchaseOrder', new_name='HRSDomesticPurchaseOrder'),
        migrations.RenameModel(old_name='HRSPOLineItem', new_name='HRSDomesticPOLineItem'),
        migrations.RenameModel(old_name='HRSStockLot', new_name='HRSRMLot'),
        migrations.RenameModel(old_name='HRSStockSnapshot', new_name='HRSRMSnapshot'),
        migrations.RenameModel(old_name='RTPAchhadPurchaseOrder', new_name='RTPAchhadDomesticPurchaseOrder'),
        migrations.RenameModel(old_name='RTPAchhadPOLineItem', new_name='RTPAchhadDomesticPOLineItem'),
        migrations.RenameModel(old_name='RTPAchhadStockLot', new_name='RTPAchhadRMLot'),
        migrations.RenameModel(old_name='RTPAchhadStockSnapshot', new_name='RTPAchhadRMSnapshot'),
        migrations.RenameModel(old_name='RTPVapiPurchaseOrder', new_name='RTPVapiDomesticPurchaseOrder'),
        migrations.RenameModel(old_name='RTPVapiPOLineItem', new_name='RTPVapiDomesticPOLineItem'),
        migrations.RenameModel(old_name='RTPVapiStockLot', new_name='RTPVapiRMLot'),
        migrations.RenameModel(old_name='RTPVapiStockSnapshot', new_name='RTPVapiRMSnapshot'),
    ]
