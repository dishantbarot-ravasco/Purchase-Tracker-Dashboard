"""
Review Matches removed (owner, 2026-10-01): the match-accuracy review page,
its endpoints, scoring and report command are gone, and so is the table of
Correct / Incorrect / Unsure verdicts. The owner chose to drop the data; the
nightly R2 backups taken before this migration still hold it.

MatchDismissal keeps the PO <-> MIR / Import PO <-> MIR / MIR <-> Stock
match types, now declared on itself - the values are unchanged, so its
column needs no change.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0088_activity_log'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='matchreview',
            name='reviewer',
        ),
        migrations.DeleteModel(
            name='MatchReview',
        ),
    ]
