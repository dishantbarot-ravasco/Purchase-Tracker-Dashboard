#!/bin/sh
# Start-time steps that need a live DB connection (unlike collectstatic,
# which is baked into the image at build time in the Dockerfile - see that
# file's own comments). Split this way so the image itself is DB-independent
# to build, matching render.yaml's buildCommand/startCommand split.
set -e

uv run python manage.py migrate --noinput

# createcachetable is not safely re-runnable against an existing table on
# every Django version's own terms - `|| true` makes a container restart
# non-fatal on "table already exists" (the expected, common case for this
# command specifically), not a general error swallow elsewhere in this
# script (note `set -e` above still applies to every other line).
uv run python manage.py createcachetable || true

exec "$@"
