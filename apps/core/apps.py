"""
apps/core/apps.py — Django AppConfig for the `core` app.

`apps.core` is deliberately just models + migrations (see models.py's
module docstring) - the one ready() hook here (added 2026-09-05, hardening
pass) only imports apps/core/checks.py so its @register()-decorated system
checks are registered; it defines no signal wiring or other startup
behavior. default_auto_field pins new models to BigAutoField rather than
Django's own default (AutoField) so a future model doesn't silently get a
32-bit PK by omission.
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self):
        from apps.core import checks  # noqa: F401 - import registers the checks
