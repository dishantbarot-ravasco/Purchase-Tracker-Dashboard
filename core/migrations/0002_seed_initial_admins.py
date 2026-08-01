"""
One-time data migration seeding the first admins, so the app doesn't hit a
chicken-and-egg problem (nobody can grant access via the in-app Admin tab
until at least one admin already exists). Runs automatically as part of
`python manage.py migrate`, which is already in the Render Build Command -
no shell access needed (Render's Shell is a paid-plan feature; this sidesteps
that entirely).

Safe to run more than once (get_or_create), and safe on environments that
already have these rows - Django only runs a migration once per database
regardless, tracked in the django_migrations table.
"""
from django.db import migrations

INITIAL_ADMINS = [
    "dishant.barot@ravasco.com",
    "jitesh.kalra@ravasco.com",
    "masira.balouch@ravasco.com",
    "saurabh.dubey@ravasco.com",
]


def seed_admins(apps, schema_editor):
    UserAccess = apps.get_model("core", "UserAccess")
    for email in INITIAL_ADMINS:
        UserAccess.objects.get_or_create(
            email=email, defaults={"role": "admin", "plants": [], "is_active": True}
        )


def unseed_admins(apps, schema_editor):
    # Reversing this migration does NOT remove these admins - access
    # management should happen through the app's Admin tab from here on,
    # not by rolling migrations back and forth. This is intentionally a
    # no-op rather than deleting anyone's access as a side effect of an
    # unrelated migration rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_admins, reverse_code=unseed_admins),
    ]
