"""
Django settings for the Purchase Tracker Dashboard.

Every secret (OAuth client secret, service account key, database URL) is read
from the environment, never hardcoded here. Locally, put them in a `.env`
file (see .env.example) and this file loads it via python-decouple. On
Render, set them in the service's Environment tab, and upload the service
account JSON as a Secret File rather than an env var (it's multi-line and
easy to mangle as a plain var).
"""
import os
from pathlib import Path

import dj_database_url
from decouple import Config, RepositoryEnv, RepositoryEmpty

BASE_DIR = Path(__file__).resolve().parent.parent

# python-decouple: use a real .env file if present (local dev), otherwise
# fall back to actual OS environment variables only (Render/production).
_env_path = BASE_DIR / '.env'
if _env_path.exists():
    config = Config(RepositoryEnv(str(_env_path)))
else:
    config = Config(RepositoryEmpty())

def env(key, default=None, cast=None):
    # Prefer a real OS env var (Render) over .env, then fall back to default.
    if key in os.environ:
        val = os.environ[key]
        return cast(val) if cast else val
    return config(key, default=default, cast=cast) if cast else config(key, default=default)


SECRET_KEY = env('SECRET_KEY', default='dev-only-insecure-key-change-in-production')
DEBUG = env('DEBUG', default='False') == 'True'
ALLOWED_HOSTS = [h.strip() for h in env('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'purchase_tracker.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'public'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'purchase_tracker.wsgi.application'

# --- Database -----------------------------------------------------------
# DATABASE_URL is provided automatically by Render once a Postgres instance
# is attached to this service. Falls back to local sqlite only if unset, for
# quick local smoke-testing before Postgres is wired up.
DATABASE_URL = env('DATABASE_URL', default='')
if DATABASE_URL:
    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = []  # no local passwords exist at all - auth is Google-only, see core/auth_views.py

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'public']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Session / cookie security -------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 12  # 12h, matches the earlier Node prototype's choice
CSRF_COOKIE_HTTPONLY = False  # frontend JS needs to read this to set the X-CSRFToken header
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')  # Render sits behind a proxy
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

# --- Google OAuth (Sign-In), restricted to the company domain -----------
GOOGLE_OAUTH_CLIENT_ID = env('GOOGLE_OAUTH_CLIENT_ID', default='')
GOOGLE_OAUTH_CLIENT_SECRET = env('GOOGLE_OAUTH_CLIENT_SECRET', default='')
GOOGLE_OAUTH_REDIRECT_URI = env('GOOGLE_OAUTH_REDIRECT_URI', default='')
ALLOWED_LOGIN_DOMAIN = env('ALLOWED_LOGIN_DOMAIN', default='ravasco.com')

# --- Google service account (Drive read access) --------------------------
# Distinct from the OAuth client above: this is what the app itself uses to
# read Drive files regardless of who's logged in. Never the signed-in user's
# own Drive permissions.
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = env('GOOGLE_SERVICE_ACCOUNT_JSON_PATH', default='secrets/service_account.json')

DRIVE_PLANT_ROOTS = {
    'HRS': env('DRIVE_HRS_ROOT', default=''),
    'RTP_ACHHAD': env('DRIVE_RTP_ACHHAD_ROOT', default=''),
    'RTP_VAPI': env('DRIVE_RTP_VAPI_ROOT', default=''),
}
DRIVE_EXTRACTION_QUEUE_FOLDER = env('DRIVE_EXTRACTION_QUEUE_FOLDER', default='')
