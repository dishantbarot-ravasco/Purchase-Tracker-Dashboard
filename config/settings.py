"""
Django settings for the Purchase Tracker Dashboard.

Mirrors the architecture conventions of the TDS Automation app: static
frontend served by WhiteNoise directly from frontend/ (no build step),
Postgres, DRF for the API surface, device-aware 2FA + JWT-in-httpOnly-cookie
auth, config via environment variables. See CLAUDE.md for the full rundown
of what was ported from the TDS app vs. deliberately simplified/skipped.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

from config.middleware import frontend_cache_headers

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_q",
    "apps.core",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Must sit BEFORE WhiteNoise - see config/security_headers.py's module
    # docstring for why the reverse ordering silently disables these headers
    # on every static-file response (most of a page load).
    "config.security_headers.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Full CsrfViewMiddleware is NOT used app-wide: every /api/ endpoint
    # authenticates via JWT bearer token / httpOnly pt_access cookie
    # (SameSite=Lax), never Django's session CSRF token, and the global
    # middleware doesn't know that - it would 403 every unsafe-method API
    # call regardless of auth type. AdminOnlyCsrfMiddleware (config/middleware.py)
    # restores the exact same, unmodified Django CSRF check but only for
    # requests under /admin/ (Django Admin - a real session+form app that
    # renders {% csrf_token %} and expects this check); every /api/ request
    # skips it entirely.
    "config.middleware.AdminOnlyCsrfMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Dev: force browser to always fetch fresh assets instead of trusting
# WhiteNoise's default 60s Cache-Control.
if DEBUG:
    MIDDLEWARE.insert(1, "config.middleware.NoCacheMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "frontend"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database - Postgres only (matches the TDS app's "no sqlite" convention).
# dj_database_url handles DATABASE_URL parsing (replaces the previous
# hand-rolled regex - same robustness the TDS app gets from it, with a
# discrete-PG*-vars fallback preserved for local dev without DATABASE_URL set.
# conn_max_age=600 keeps connections alive for 10 min (connection pooling).
# ---------------------------------------------------------------------------
if os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.config(conn_max_age=600, conn_health_checks=True)
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "purchase_tracker"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }

# ---------------------------------------------------------------------------
# Cache - backs any future @cache_page-wrapped read-only/reference endpoint.
# DatabaseCache is shared across gunicorn worker *processes* the way
# LocMemCache is not, and needs no extra infrastructure (Redis/memcached) -
# it uses the same Postgres instance. Requires `manage.py createcachetable`
# once (see CI / deployment). No current endpoint uses @cache_page yet - see
# CLAUDE.md for why (this app's read endpoints are the business data the
# auth layer exists to protect, not public reference data), but the
# infrastructure is wired up and test-safe for when one does.
# ---------------------------------------------------------------------------
if "pytest" in sys.modules:
    # DatabaseCache's table isn't created by a migration (createcachetable
    # is a one-off management command) - the throwaway test DB never gets
    # it, so cache_page would 500 with "relation ... does not exist".
    # Checks sys.modules, not `'test' in sys.argv` (the TDS app's original
    # pattern, ported here initially and then found to never match - this
    # app's test runner is pytest/pytest-django, not `manage.py test`, so
    # sys.argv is something like ['pytest', 'apps/...'] with no bare 'test'
    # element. sys.modules already has 'pytest' loaded by the time settings
    # are imported under any pytest invocation, bare or with args.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "pt_cache_table",
        }
    }

# ---------------------------------------------------------------------------
# Task queue - runs Drive sync/match pipelines out of the web process
# entirely, via a separate `manage.py qcluster` worker process. Replaces the
# previous threading.Thread background-thread approach in
# apps/services/sync_trigger.py, which lived inside the gunicorn worker
# process handling the HTTP request - a worker recycle (Render's default
# 30s gunicorn --timeout, no override in render.yaml) killed the sync
# mid-flight with no retry. django-q2's ORM broker stores queued tasks as
# Postgres rows (via this same `default` DATABASES connection) instead of
# needing Redis/RabbitMQ - matches the CACHES block's own
# no-extra-infrastructure choice above. `retry` must exceed `timeout` (django-q2
# requeues a task if it isn't marked complete within `retry` seconds, so a
# retry shorter than the timeout would re-run an already-running sync).
# ---------------------------------------------------------------------------
Q_CLUSTER = {
    "name": "purchase_tracker",
    "orm": "default",
    "workers": 2,
    "timeout": 900,
    "retry": 1200,
    "queue_limit": 20,
    "bulk": 5,
    "catch_up": False,
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / frontend - same WhiteNoise-serves-frontend-directly pattern as
# the TDS app. No bundler, no build step: frontend/index.html is served at
# the site root, frontend/css and frontend/js linked directly from it.
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
WHITENOISE_ROOT = BASE_DIR / "frontend"
STATICFILES_DIRS = [BASE_DIR / "frontend"]
# Forces .html/.js/.css to always revalidate with the server instead of
# trusting WhiteNoise's default 60s Cache-Control - see frontend_cache_headers()
# in config/middleware.py.
WHITENOISE_ADD_HEADERS_FUNCTION = frontend_cache_headers

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Logging - ported from the TDS Automation App's config/settings.py.
# Every request/error already goes to the console (runserver, or whatever
# process manager runs `manage.py runserver` in prod); this adds a rotating
# file on top so history survives past the console's scrollback, capped at
# 10MB x 5 backups so it can't grow unbounded on disk.
# ---------------------------------------------------------------------------
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {asctime} {module}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "app.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB per file
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console", "file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # Cookie-first JWT auth (tries the httpOnly cookie, falls back to the
    # Authorization: Bearer header for non-browser clients).
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.auth_backend.PTCookieJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "apps.api.exceptions.custom_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "200/minute",
        "login": "5/minute",       # POST /api/auth/login
        "otp_verify": "10/minute",  # POST /api/auth/device-verify
    },
}

# ---------------------------------------------------------------------------
# SimpleJWT
# ---------------------------------------------------------------------------
# JWT_SIGNING_KEY defaults to SECRET_KEY for zero-downtime compatibility,
# but is a distinct env var so it CAN be set independently - SECRET_KEY also
# signs Django's session/CSRF tokens, so reusing it for JWTs means a leak in
# one context compromises the other.
JWT_SIGNING_KEY = os.environ.get("JWT_SIGNING_KEY", SECRET_KEY)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    # 30 days - backs the persistent 'remember me' pt_refresh cookie.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "user_id",   # PTUser PK field name
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "UPDATE_LAST_LOGIN": False,
    "USER_AUTHENTICATION_RULE": "apps.api.auth_backend.pt_user_authentication_rule",
}

# ---------------------------------------------------------------------------
# Google API - two SEPARATE concerns, both configured via env vars:
#   1. GOOGLE_SERVICE_ACCOUNT_JSON/FILE - unattended Drive/Sheets access for
#      the sync jobs (apps/services/google_client.py). Unrelated to login.
#   2. GOOGLE_CLIENT_ID/SECRET/GOOGLE_OAUTH_REDIRECT_URI - "Sign in with
#      Google" for human users (apps/api/routers/google_oauth_views.py).
# Do not conflate these - a Drive service account cannot log a person in,
# and an OAuth client cannot read Drive files unattended.
# ---------------------------------------------------------------------------
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:8000/api/auth/google/callback/",
)

# Drive file/folder ids this app reads. HRS only for now.
#
# Two DIFFERENT Drive folders, confirmed by inspecting the live files, not
# one shared folder - do not collapse these back into a single id:
#   - PURCHASE_TRACKER_DB_FOLDER_ID holds the PO master CSV.
#   - HRS_MIR_STOCK_FOLDER_ID holds the live MIR and Stock xlsx files.
HRS_PO_CSV_TITLE = "Master_HRS_SILVASSA_Domestic_Purchase_Data.csv"
# Import PO master CSV - lives in the same PURCHASE_TRACKER_DB_FOLDER_ID as
# the domestic one (confirmed live 2026-09-04: all three plants' Import CSVs
# are siblings of their domestic CSVs in that same folder).
HRS_IMPORTS_PO_CSV_TITLE = "Master_HRS_SILVASSA_Imports_Purchase_Data.csv"
HRS_MIR_FILE_TITLE = "HRS MIR FILE 2026-2027.xlsx"
HRS_STOCK_FILE_TITLE = "HRS RAW MATERIAL STOCK.xlsx"
PURCHASE_TRACKER_DB_FOLDER_ID = os.environ.get("PURCHASE_TRACKER_DB_FOLDER_ID", "17cP2suZv26IAU5mqSKyC8U9zL0EysF9a")
HRS_MIR_STOCK_FOLDER_ID = os.environ.get("HRS_MIR_STOCK_FOLDER_ID", "1mmZrMukdhuYyHQGEfH6ci_LIiNOEQ8Qp")

# RTP-Achhad. Its PO master CSV lives in the same
# PURCHASE_TRACKER_DB_FOLDER_ID as HRS's (confirmed - both master CSVs are
# siblings in that folder); its MIR and Stock files live together in their
# own folder, distinct from HRS's MIR/Stock folder.
ACHHAD_PO_CSV_TITLE = "Master_RTP_Achhad_Domestic_Purchase_Data.csv"
ACHHAD_IMPORTS_PO_CSV_TITLE = "Master_RTP_Achhad_Imports_Purchase_Data.csv"
ACHHAD_MIR_FILE_TITLE = "RTP ACHHAD MIR FILE 2026-27.xlsx"
ACHHAD_STOCK_FILE_TITLE = "RAVASCO ACHHAD RM STOCK FILE.xlsx"
ACHHAD_MIR_STOCK_FOLDER_ID = os.environ.get("ACHHAD_MIR_STOCK_FOLDER_ID", "1mviBKPDyZLVLfGVZ780BX_xPR9fLxdtH")

# RTP-Vapi. Its PO master CSV lives in the same PURCHASE_TRACKER_DB_FOLDER_ID
# as HRS's/Achhad's (confirmed - all three master CSVs are siblings in that
# folder); its MIR and Stock files live together in their own folder,
# distinct from HRS's and Achhad's MIR/Stock folders.
VAPI_PO_CSV_TITLE = "Master_RTP_VAPI_Domestic_Purchase_Data.csv"
VAPI_IMPORTS_PO_CSV_TITLE = "Master_RTP_VAPI_Imports_Purchase_Data.csv"
VAPI_MIR_FILE_TITLE = "RTP VAPI MIR FILE 2026-27.xlsx"
VAPI_STOCK_FILE_TITLE = "RAVASCO VAPI RM STOCK FILE.xlsx"
VAPI_MIR_STOCK_FOLDER_ID = os.environ.get("VAPI_MIR_STOCK_FOLDER_ID", "1kzWf8sf9UfXBG34WalJxgu7djX5fwNUU")

# ---------------------------------------------------------------------------
# Session - DB-backed, required for the Google OAuth PKCE code_verifier
# round-trip and for the pending_user_id stored during the device-verify OTP
# flow. SESSION_SAVE_EVERY_REQUEST is essential: without it, session writes
# made inside a redirecting view (google_login, TDS's equivalent already hit
# this) may not be committed before the HttpResponseRedirect leaves the
# domain, causing a state-mismatch on callback.
# ---------------------------------------------------------------------------
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG

# ---------------------------------------------------------------------------
# JWT access cookie - httpOnly cookie carrying the JWT access token after
# login/2FA completion. PTCookieJWTAuthentication reads this on every
# authenticated request.
# ---------------------------------------------------------------------------
PT_COOKIE_NAME = "pt_access"
PT_COOKIE_SAMESITE = "Lax"
PT_COOKIE_SECURE = not DEBUG

# The `pt_device` httpOnly cookie that identifies a trusted browser/device.
PT_DEVICE_COOKIE_SECURE = not DEBUG

# ---------------------------------------------------------------------------
# CSRF (Django Admin only - see AdminOnlyCsrfMiddleware). Only ever set/
# checked for requests under /admin/; the JWT API never touches this cookie.
# ---------------------------------------------------------------------------
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------
# Authentication backends
#   PTUserBackend: authenticates via email + bcrypt against pt_users.
#   ModelBackend:  kept so Django Admin (superuser) still works.
# ---------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "apps.api.auth_backend.PTUserBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# ---------------------------------------------------------------------------
# Email / SMTP - used by the device-login OTP / new-device notification
# emails. Port 465 = SSL | Port 587 = STARTTLS.
# ---------------------------------------------------------------------------
_smtp_port = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
EMAIL_PORT = _smtp_port
EMAIL_HOST_USER = os.environ.get("SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASS", "")
EMAIL_USE_SSL = _smtp_port == 465
EMAIL_USE_TLS = _smtp_port == 587
DEFAULT_FROM_EMAIL = os.environ.get("SMTP_FROM", EMAIL_HOST_USER)
# Django's SMTP backend has no timeout by default (blocks on the OS's own
# TCP timeout, which can be minutes) - login/device-verify send mail
# synchronously-committed-but-backgrounded (see device_service.py), a fixed
# timeout turns an unreachable mail server into a fast, clean failure.
EMAIL_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Account domain restriction - only email addresses ending in
# "@<this domain>" may ever have an account or log in (enforced in
# apps/api/permissions.py::is_allowed_email_domain(), called from login and
# Google OAuth). Configurable via env rather than hardcoded.
# ---------------------------------------------------------------------------
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "ravasco.com")

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    # Render's edge terminates TLS and sets X-Forwarded-Proto - trust it.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

if "pytest" in sys.modules:
    # Django's test client talks over plain HTTP - without this override,
    # SECURE_SSL_REDIRECT (on whenever DEBUG=False, which CI sets) would
    # 301-redirect every single test request before it reaches a view. See
    # the CACHES block above for why this checks sys.modules, not sys.argv.
    SECURE_SSL_REDIRECT = False
