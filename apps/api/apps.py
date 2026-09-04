"""
apps/api/apps.py — Django AppConfig for the `apps.api` app.

Standard boilerplate registering `apps.api` with Django's app registry;
nothing here is auth- or business-logic-specific. Kept as its own tiny
module only because Django's app-loading convention expects one AppConfig
per app in an `apps.py` file at the app's root.
"""

from django.apps import AppConfig


class ApiConfig(AppConfig):
    """Registers `apps.api` as a Django app. No custom `ready()` hook — this
    app has no signal handlers or startup wiring to register."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
