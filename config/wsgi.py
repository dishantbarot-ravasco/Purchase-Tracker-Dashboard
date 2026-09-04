"""
config/wsgi.py — WSGI entrypoint; this is the one actually used in
production (gunicorn ``config.wsgi:application``, see render.yaml).

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

# DJANGO_SETTINGS_MODULE is only defaulted here, not forced — an already-set
# env var (e.g. DJANGO_SETTINGS_MODULE=config.settings_dev_sqlite for local
# smoke-testing, see that module's own docstring) takes precedence.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()
