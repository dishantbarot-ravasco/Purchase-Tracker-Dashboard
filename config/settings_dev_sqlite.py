"""
config/settings_dev_sqlite.py — DEV-ONLY settings override for local
smoke-testing the sync management commands without a local Postgres/Docker
install.

Imports everything from config.settings (the real config) via `import *`
and overrides only DATABASES to point at a throwaway local SQLite file. NOT
used in production or CI - config/settings.py's Postgres-only DATABASES
stays the real config; this module exists purely so a developer can run
`sync_*`/`match_*` commands against a local file without standing up
Postgres first (see CLAUDE.md's "What's still explicitly out of scope" for
why the pipeline itself has no automated test coverage and relies on this
kind of manual smoke test instead).

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
