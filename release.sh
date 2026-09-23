#!/bin/sh
# Release tasks - the steps that need a live DB connection (unlike
# collectstatic, which is baked into the image at build time - see the
# Dockerfile). Run exactly ONCE per deploy, never once per container:
#   - Render: the web service's preDeployCommand in render.yaml. The qcluster
#     worker deliberately does not run it - two services migrating the same
#     Postgres at the same moment is a race, not redundancy.
#   - docker-compose: the `app` service's entrypoint, via RUN_RELEASE_TASKS=1
#     (see docker-entrypoint.sh).
set -e

python manage.py migrate --noinput

# Idempotent (get_or_create on name) - see that command's own module
# docstring for why a repeat run never resets next_run once the schedule
# already exists.
python manage.py ensure_schedules

# createcachetable is not safely re-runnable against an existing table on
# every Django version's own terms - `|| true` makes a repeat run non-fatal
# on "table already exists" (the expected, common case for this command
# specifically), not a general error swallow elsewhere in this script (note
# `set -e` above still applies to every other line).
python manage.py createcachetable || true
