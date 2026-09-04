"""
apps/core/apps.py — Django AppConfig for the `core` app.

Nothing custom here (no ready() hooks, no signal wiring) - `apps.core` is
deliberately just models + migrations (see models.py's module docstring),
so there's no app-startup behavior to register. default_auto_field pins new
models to BigAutoField rather than Django's own default (AutoField) so a
future model doesn't silently get a 32-bit PK by omission.
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
