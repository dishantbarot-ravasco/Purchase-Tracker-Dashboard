# Local-dev Docker image. Does NOT replace render.yaml's own deploy pipeline
# (Render still builds/deploys this app its own way) - this exists purely so
# a developer can run a real Postgres + this app locally without installing
# Python/uv/Postgres directly on their machine. See CLAUDE.md's "Docker
# (local dev)" section and README.md's "Docker alternative" for the
# accompanying docker-compose.yml and setup instructions.
#
# Base image pinned to 3.12, matching render.yaml's PYTHON_VERSION=3.12.8 -
# the environment that actually matters for parity - not this repo's local
# .python-version (3.14), which only affects `uv run` on a developer's own
# machine outside Docker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# psycopg[binary] (see pyproject.toml) ships a prebuilt wheel, so no
# libpq-dev/build-essential is needed here - keep it that way; if a future
# dependency needs compiling, add build deps deliberately rather than by
# reflex.
RUN pip install --no-cache-dir uv

# Copy dependency manifests first so `uv sync` is cached by Docker unless
# pyproject.toml/uv.lock actually change - avoids re-resolving on every code
# edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

# Static files don't depend on a live DB - safe to bake into the image at
# build time rather than at container start. STATICFILES_STORAGE is
# whitenoise's CompressedManifestStaticFilesStorage (config/settings.py),
# which raises on missing manifest entries if this step is ever skipped.
# DJANGO_SECRET_KEY/DATABASE_URL aren't needed for collectstatic - the
# dev-only SECRET_KEY fallback in settings.py covers this build step.
RUN uv run python manage.py collectstatic --noinput

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Overridden per-service in docker-compose.yml (web uses gunicorn, worker
# uses `manage.py qcluster`) - this default is just for a bare `docker run`.
CMD ["uv", "run", "gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
