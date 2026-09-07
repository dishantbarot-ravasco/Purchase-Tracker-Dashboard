#!/bin/sh
# Start-time steps that need a live DB connection (unlike collectstatic,
# which is baked into the image at build time in the Dockerfile - see that
# file's own comments). Split this way so the image itself is DB-independent
# to build, matching render.yaml's buildCommand/startCommand split.
set -e

uv run python manage.py migrate --noinput

# Idempotent (get_or_create on name) - see that command's own module
# docstring for why a repeat run never resets next_run once the schedule
# already exists.
uv run python manage.py ensure_schedules

# createcachetable is not safely re-runnable against an existing table on
# every Django version's own terms - `|| true` makes a container restart
# non-fatal on "table already exists" (the expected, common case for this
# command specifically), not a general error swallow elsewhere in this
# script (note `set -e` above still applies to every other line).
uv run python manage.py createcachetable || true

exec "$@"
