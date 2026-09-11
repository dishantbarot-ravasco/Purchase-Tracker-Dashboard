"""
Django settings for the Purchase Tracker Dashboard.

Mirrors the architecture conventions of the TDS Automation app: static
frontend served by WhiteNoise directly from frontend/ (no build step),
Postgres, DRF for the API surface, device-aware 2FA + JWT-in-httpOnly-cookie
auth, config via environment variables. See CLAUDE.md for the full rundown
of what was ported from the TDS app vs. deliberately simplified/skipped.
"""

import logging
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
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# ---------------------------------------------------------------------------
# Sentry (added 2026-09-09) - real-time error tracking, closing the gap
# flagged in a pre-go-live review: this app previously only ever surfaced a
# failure via logs/app.log (see LOGGING below) or a SyncRun row - nobody was
# actually watching either in real time. Runs in every process that loads
# this settings module (gunicorn web workers AND the qcluster worker
# process - both `manage.py runserver`/`gunicorn` and `manage.py qcluster`
# import config.settings the same way), so a sync-pipeline exception logged
# via log.exception() in apps/services/sync_trigger.py reaches Sentry from
# the worker process too, not just web-request errors.
#
# No DSN set (SENTRY_DSN blank, the default in every environment until set)
# -> sentry_sdk.init() is simply never called - a deliberate no-op, not a
# silently-broken integration; local dev and CI need no Sentry account.
#
# LoggingIntegration piggybacks on the EXISTING `apps`/`django` loggers
# (LOGGING dict below) rather than requiring every call site to explicitly
# report to Sentry - every current log.error()/log.exception() call across
# this app's sync commands, auth flows, and OAuth error paths becomes a
# Sentry event for free, with no per-call-site changes needed.
# send_default_pii=False - this app's logs/exceptions can carry a real
# email address (e.g. a failed login) but not payment/financial data;
# still, default to NOT attaching request user/cookie/IP data to events
# unless a real triage need justifies turning it on later.
# traces_sample_rate defaults to 0 (performance tracing off) - error
# tracking alone doesn't need it, and it consumes a separate Sentry quota;
# raise SENTRY_TRACES_SAMPLE_RATE explicitly if performance monitoring is
# ever wanted.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
# `or` (not a dict-default) - .env.example ships this key present-but-blank
# (documenting it exists, same convention as other optional vars in that
# file), and os.environ.get()'s own fallback only applies when the key is
# entirely ABSENT, not when it's set to "" - a blank-but-present env var
# would otherwise silently override the computed development/production
# default with an empty string (the exact bug already caught once in this
# app - see ADVANCE_LICENSE_FILE_TITLE's own comment for the same footgun).
SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT") or ("development" if DEBUG else "production")

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        integrations=[
            DjangoIntegration(),
            # Breadcrumbs from INFO+ logs, a real Sentry event from ERROR+ -
            # matches this app's own LOGGING level split (apps.* loggers are
            # already INFO/DEBUG, see below) rather than introducing a
            # separate threshold to keep in sync with it.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        # Same blank-vs-absent reasoning as SENTRY_ENVIRONMENT above - a
        # present-but-empty env var would otherwise crash float("") with a
        # ValueError at Django startup, not just silently misconfigure.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE") or "0"),
        send_default_pii=False,
    )

# No django-cors-headers app/middleware anywhere in this file, deliberately:
# the frontend is same-origin (WhiteNoise serves frontend/ from the same
# process the API runs on), so there is no cross-origin request for CORS to
# solve here — unlike the TDS Automation App, which historically ran its
# frontend from a separate dev origin. Don't add it back without a real
# cross-origin use case first.
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
#
# IMPORTANT if a future endpoint ever adds @cache_page: it must never sit
# above a permission check, and the view it wraps must be AllowAny.
# cache_page short-circuits on a cache hit and returns the stored response
# without re-invoking the view at all, so a permission check inside the view
# body only actually runs on the request that misses the cache — every
# request after that gets served the same cached response regardless of who
# they are or whether they're authenticated. This bit the TDS Automation App
# in production on a real endpoint; don't repeat it here.
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
# 10MB x 5 backups (see the "file" handler below) so it can't grow unbounded
# on disk.
#
# This is a separate concern from the pt_audit_log DB table
# (apps/core/audit_log.py): logs/app.log is an operational trace of
# everything the server did (every INFO+ line from Django itself plus every
# apps.* logger - parsers, matching, auth, sync commands - e.g. a plain
# "[WARNING] ... Unauthorized: /api/purchase-orders" line), rotated and
# eventually discarded. pt_audit_log is a permanent, security-relevant
# record of who logged in/out and from where (ACTION_LOGIN/ACTION_LOGOUT
# only). Neither substitutes for the other - don't assume one covers what
# the other is for.
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
    # JSON only - this is an internal, same-origin JSON API with a static-HTML
    # frontend (see CLAUDE.md), not a public API that benefits from DRF's
    # Browsable API's interactive HTML/schema UI. Without this override, DRF's
    # own default renderer list includes BrowsableAPIRenderer regardless of
    # DEBUG, which is unnecessary attack surface (extra JS/CSS, self-
    # documenting forms) for no real benefit here.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "200/minute",
        "login": "5/minute",       # POST /api/auth/login
        "otp_verify": "10/minute",  # POST /api/auth/device-verify
        # Higher-blast-radius writes than an ordinary field correction - a
        # real Drive sync job or a user-management change shouldn't share the
        # generic 200/min "user" bucket. See apps/api/permissions.py's
        # SyncTriggerThrottle/AdminWriteThrottle.
        "sync_trigger": "10/minute",
        "admin_write": "30/minute",
        # Self-service "Change Password" (apps/api/routers/password_views.py)
        # - requesting a fresh code, same cadence as login's own throttle.
        "password_change_request": "5/minute",
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

# ---------------------------------------------------------------------------
# SafeCube (Sinay) Container Tracking API
# ---------------------------------------------------------------------------
# Backs the Import Purchases page's "Track" links (BL-number shipment
# lookup - see apps/services/bl_tracking.py). Blank in an environment
# without a key: bl_tracking.track_bl() returns a clean "not configured"
# error rather than the view crashing - never required for the rest of the
# dashboard to work.
SAFECUBE_API_KEY = os.environ.get("SAFECUBE_API_KEY", "")

# ---------------------------------------------------------------------------
# Report cron triggers (daily/monthly consumption + plant mismatch reports)
# ---------------------------------------------------------------------------
# Shared secret for apps/api/routers/reports_views.py's trigger_daily_report/
# trigger_monthly_report/trigger_mismatch_report - an external free scheduler
# (cron-job.org) hits each endpoint on its own schedule (e.g. daily 20:30 IST,
# monthly 1st 10:00 IST, mismatch report whatever cadence is wanted) since
# Render's free web plan has no built-in cron and its own Cron Jobs feature
# isn't free either. Same pattern as the TDS Automation App's own
# REPORT_CRON_SECRET. Empty means every endpoint refuses every request (503)
# rather than failing open - see each view's own docstring.
REPORT_CRON_SECRET = os.environ.get("REPORT_CRON_SECRET", "")

# Temporary killswitch (added 2026-09-11, project owner: "remove the data
# mismatch email module to plant heads completely or comment in for a while
# till i get matching logic correct") - the matching engine's qty/rate
# mismatch flags are still being actively tuned (see CLAUDE.md's "Match
# accuracy" / recent aggregation-fix notes), so the per-plant-head Data
# Correction email (apps/services/plant_mismatch_report.py) would currently
# hand real plant heads a mix of genuine and false-positive mismatches.
# Defaults to DISABLED (real plant-head/admin delivery is skipped) until
# explicitly turned back on via this env var - no code change needed to
# re-enable, just set MISMATCH_REPORT_PLANT_HEADS_ENABLED=true in Render's
# environment once the matching logic is trusted again. The test_recipient
# override (send_plant_mismatch_reports(test_recipient=...)) is NEVER
# gated by this - it already never reaches a real plant head or admin, and
# stays available for verifying the pipeline while matching is tuned.
MISMATCH_REPORT_PLANT_HEADS_ENABLED = os.environ.get("MISMATCH_REPORT_PLANT_HEADS_ENABLED", "false").lower() == "true"

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    # 30 days - backs the persistent 'remember me' pt_refresh cookie.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    # A stolen refresh token used to remain valid for its full 30-day
    # REFRESH_TOKEN_LIFETIME even after an explicit logout, since nothing
    # revoked it server-side - PTTokenRefreshSerializer.validate() already
    # had the rotate/blacklist logic (apps/api/auth_serializers.py) but it
    # was dead code until these two flags were turned on. Every successful
    # /api/auth/token/refresh now issues a new refresh token and revokes the
    # one just spent, and apps/api/routers/device_views.py's logout_view
    # revokes the current one directly on logout.
    #
    # Comment corrected during a full-codebase audit (2026-09-10): this used
    # to say these flags require rest_framework_simplejwt.token_blacklist in
    # INSTALLED_APPS - that was never true here and INSTALLED_APPS below
    # correctly does NOT include it. Revocation is backed by our own
    # apps/core/models.py's RevokedRefreshToken table instead (written via
    # apps/services/token_revocation.py's revoke_refresh_jti()/
    # is_refresh_jti_revoked()) precisely BECAUSE token_blacklist's own
    # OutstandingToken model FKs to AUTH_USER_MODEL (Django's default
    # auth.User, which this app deliberately never uses for real accounts -
    # see CLAUDE.md's own note on why) - enabling that app crashes
    # device_verify with `"OutstandingToken.user" must be a "User" instance`
    # the moment a PTUser is passed to RefreshToken.for_user(). Do NOT add
    # rest_framework_simplejwt.token_blacklist to INSTALLED_APPS to try to
    # make this stale comment true - it reproduces that exact crash.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
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
# RoDTEP scrip ledger (added 2026-09-09) - company-wide, not per-plant (see
# SyncRun.Plant.COMPANY's own comment). Unlike every other Drive source in
# this app, there is no fixed file title here - the folder holds one file
# per Script Number ("RODTEP-JNPT-<N>.xlsx"), listed and parsed in full by
# manage.py sync_rodtep (apps/services/google_client.py's
# list_files_in_folder()), not searched for by name.
RODTEP_FOLDER_ID = os.environ.get("RODTEP_FOLDER_ID", "16gdQxPCDutLTxZ2oiX1M1TCi64kjnEnq")

# ---------------------------------------------------------------------------
# Advance License ledger (added 2026-09-09) - company-wide, not per-plant,
# same reasoning as RoDTEP above (one shared IEC, licenses aren't split per
# plant the way MIR/Stock/PO data is). ONE fixed file - a native Google
# Sheet, per the project owner's own share link (docs.google.com/spreadsheets/
# d/<id>/...) - so manage.py sync_advance_license fetches it directly by
# file id (google_client.download_spreadsheet_bytes_by_id()), not by
# searching a folder for a title the way every other sync_* command does.
# Real id confirmed by the project owner 2026-09-09, same as
# RODTEP_FOLDER_ID's own hardcoded default above.
ADVANCE_LICENSE_FILE_ID = os.environ.get("ADVANCE_LICENSE_FILE_ID", "1zqxUwxUn2fpftBUJhqjjfRmzjgkob0rb2U11VRXudX4")

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
# Explicit rather than Django's 2-week default - this session only ever
# carries short-lived, single-purpose state (the OAuth PKCE code_verifier
# round-trip, or pending_user_id during the device-verify OTP flow, which
# itself expires in 10 minutes - see apps/services/otp_service.py). It is
# not part of the main JWT-cookie auth path, so there's no reason for it to
# outlive a single sign-in attempt by much.
SESSION_COOKIE_AGE = 1800  # 30 minutes

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

# ---------------------------------------------------------------------------
# `manage.py check --deploy --fail-level WARNING` (see CLAUDE.md's Commands
# section, and ci.yml's "Production security check" step) - a real gate,
# not advisory: DEBUG defaulting to True (settings.py's own DEBUG line
# above) or SECRET_KEY defaulting to the hardcoded dev-only string would
# otherwise fail OPEN with no error anywhere, since neither is checked at
# startup. `--fail-level WARNING` makes every security.W0xx warning (W004/
# W008/W012/W018 - missing HSTS/SSL-redirect/secure-cookie/DEBUG=True, all
# gated behind `if not DEBUG` above) a real CI failure if it ever
# reappears. security.W003 (no CsrfViewMiddleware in MIDDLEWARE) is
# silenced deliberately, not an oversight - this app's API surface
# authenticates via JWT (cookie or Bearer), never Django's session CSRF
# token; AdminOnlyCsrfMiddleware (config/middleware.py) already restores
# the real, unmodified check for /admin/, the one place that still uses
# session auth + {% csrf_token %} forms. See MIDDLEWARE's own comment
# above for the full reasoning - don't remove this silence entry without
# also reading that.
SILENCED_SYSTEM_CHECKS = ["security.W003"]
