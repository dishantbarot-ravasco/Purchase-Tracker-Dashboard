"""
config/asgi.py — ASGI entrypoint (not currently used to serve this app; kept
for parity/optionality, see below).

It exposes the ASGI callable as a module-level variable named ``application``.

This app is deployed with gunicorn's WSGI worker (see config/wsgi.py and
render.yaml), not an ASGI server — nothing here does WebSockets/async views.
This file exists because `manage.py startproject` generates it by default and
Django tooling (e.g. `runserver`'s autodetection, some ASGI-aware hosts)
expects it to be present; removing it isn't necessary and keeps the door open
for an async view/consumer later without a project-structure change.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

# DJANGO_SETTINGS_MODULE is only defaulted here, not forced — an already-set
# env var (e.g. DJANGO_SETTINGS_MODULE=config.settings_dev_sqlite for local
# smoke-testing, see that module's own docstring) takes precedence.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()
