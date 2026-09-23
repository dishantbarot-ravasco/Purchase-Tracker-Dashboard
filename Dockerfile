# Production AND local-dev image. Render builds and runs this exact file for
# both services in render.yaml (runtime: docker, since 2026-09-23), and
# docker-compose.yml builds the same image locally - one image, so dev and
# prod cannot drift apart. See CLAUDE.md's "Deployment, Docker, and `.env`".
#
# Base image pinned to 3.12 - the interpreter production runs on. This repo's
# own .python-version (3.14) is a local-dev convenience and must NOT leak in
# here: `COPY . .` below brings that file into the image, and uv honors it
# over the base image's Python. UV_PYTHON pins the interpreter explicitly and
# UV_PYTHON_DOWNLOADS=never makes a mismatch fail the build loudly instead of
# uv quietly fetching 3.14 and rebuilding the venv on it.
#
# Pinned by digest, not just by tag. `3.12-slim` is a moving tag that Debian
# security fixes land on, and Render caches base layers, so a tag-only FROM
# can keep building on a stale OS indefinitely. The digest makes every build
# reproducible; Dependabot's "docker" entry (.github/dependabot.yml) bumps it
# weekly, behind the same 7-day cooldown as every other dependency. Keep the
# `3.12-slim` tag in front of the digest - Dependabot reads it to stay on
# 3.12, and a human reads it to know what the hash is.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# PATH puts the project venv first, so the runtime calls `python`/`gunicorn`
# directly rather than through `uv run`. `uv run` re-syncs the venv before
# every command, which as the non-root user below (the venv is owned by root)
# is at best wasted startup time and at worst a crash.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# psycopg[binary] (see pyproject.toml) ships a prebuilt wheel, so no
# libpq-dev/build-essential is needed here - keep it that way; if a future
# dependency needs compiling, add build deps deliberately rather than by
# reflex. uv is pinned so a new uv release can't change how uv.lock resolves
# between two otherwise identical builds.
RUN pip install --no-cache-dir "uv==0.11.19"

# Copy dependency manifests first so `uv sync` is cached by Docker unless
# pyproject.toml/uv.lock actually change - avoids re-resolving on every code
# edit. --no-dev keeps pytest/ruff/coverage out of the production image (CI
# asserts this - see .github/workflows/ci.yml's docker job).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Static files don't depend on a live DB - safe to bake into the image at
# build time rather than at container start. Storage is Django's plain
# StaticFilesStorage (see config/settings.py's STATIC_ROOT comment for why
# it is not the whitenoise manifest storage the old comment here claimed).
# DJANGO_SECRET_KEY/DATABASE_URL aren't needed for collectstatic - the
# dev-only SECRET_KEY fallback in settings.py covers this build step. No
# ARG lines anywhere in this file on purpose: Render only forwards a
# service's env vars into a build as declared ARGs, so none of its secrets
# can end up baked into an image layer.
RUN python manage.py collectstatic --noinput

# Non-root runtime user. The only path the app writes to at runtime is
# logs/ (settings.py creates it at import and RotatingFileHandler appends to
# logs/app.log) - verified 2026-09-23, nothing else under BASE_DIR is written
# - so that one directory is all this user owns. Everything else, including
# the venv and staticfiles/, stays root-owned and read-only to the app.
RUN chmod +x /app/docker-entrypoint.sh /app/release.sh \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/logs \
    && chown -R app:app /app/logs
USER app

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Web default. Shell form via `sh -c` so $PORT expands - Render injects PORT
# for web services, and an exec-form CMD cannot read env vars. Falls back to
# 8000 for docker-compose. The qcluster worker overrides this (render.yaml's
# dockerCommand, docker-compose.yml's command).
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2"]
