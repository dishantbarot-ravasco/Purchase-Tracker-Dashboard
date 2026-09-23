#!/bin/sh
# Container entrypoint for BOTH services (web and qcluster share one image).
#
# Release tasks (migrate, ensure_schedules, createcachetable - see
# release.sh) are opt-in, not run on every start. They used to run
# unconditionally here, which was harmless while this image was local-dev
# only but wrong for production: every container start - both services on
# every deploy, plus every restart - would migrate the shared Postgres, the
# web and worker containers racing each other to do it. On Render they run
# once per deploy as the web service's preDeployCommand instead; only
# docker-compose's `app` service sets RUN_RELEASE_TASKS=1.
set -e

if [ "${RUN_RELEASE_TASKS:-0}" = "1" ]; then
    /app/release.sh
fi

exec "$@"
