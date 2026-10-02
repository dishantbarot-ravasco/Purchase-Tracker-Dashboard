"""Layered access (owner, 2026-10-02): roles become admin / user, and a user
holds explicit plants and permissions (apps/api/permissions.py).

The owner's migration rule: admins stay as they are; every other account is
LOCKED - no permissions - until an admin grants them in the Admin Panel.
Editor and viewer access is deliberately not carried over. A user's plants
are kept (an admin trims or fills them when granting), and an admin's are
cleared, since an admin reaches every plant.

Bumping token_version signs every locked account out, so an open tab does
not keep showing data loaded under the old rules.
"""

from django.db import migrations, models
from django.db.models import F


def lock_non_admins(apps, schema_editor):
    PTUser = apps.get_model("core", "PTUser")
    PTUser.objects.filter(role="admin").update(plants=[], permissions=[])
    PTUser.objects.exclude(role="admin").update(
        role="user", permissions=[], token_version=F("token_version") + 1,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0091_document_import_kinds"),
    ]

    operations = [
        migrations.AddField(
            model_name="ptuser",
            name="permissions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="ptuser",
            name="role",
            field=models.TextField(choices=[("admin", "Admin"), ("user", "User")], default="user"),
        ),
        migrations.RunPython(lock_non_admins, migrations.RunPython.noop),
    ]
