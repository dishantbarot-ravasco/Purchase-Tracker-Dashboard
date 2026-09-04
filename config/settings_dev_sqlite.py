"""
DEV-ONLY settings override for local smoke-testing the sync management
commands without a local Postgres/Docker install. NOT used in production or
CI - config/settings.py's Postgres-only DATABASES stays the real config.

Usage:
    DJANGO_SETTINGS_MODULE=config.settings_dev_sqlite python manage.py migrate
    DJANGO_SETTINGS_MODULE=config.settings_dev_sqlite python manage.py sync_po_csv --file ...
"""

from config.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "dev_smoke_test.sqlite3",  # noqa: F405
    }
}
