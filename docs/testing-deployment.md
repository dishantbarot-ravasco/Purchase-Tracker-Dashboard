# Testing, lint, CI, and deployment

How this app is tested, linted, built, and deployed, and why each gate is shaped the way it is.
The test suite is pytest + pytest-django against a real Postgres; lint is ruff (Python) and ESLint
(frontend, CI only); CI is one GitHub Actions workflow with three jobs; production is two Render
services (web + qcluster worker) running one Docker image, with docker-compose as the local mirror.
For the app's own structure see [architecture.md](architecture.md); for auth, security headers and
email see [auth-security-email.md](auth-security-email.md).

## Testing, lint, CI

### Test scopes

Test scopes are split deliberately, mirroring the sibling TDS app's own split:

- **[`apps/api/tests/`](../apps/api/tests/)** - integration. Real Postgres test DB, no mocking, a
  shared `make_user()` factory ([`factories.py`](../apps/api/tests/factories.py)). Covers the auth
  flow, inline-edit and dismiss endpoints, password reset, plant scoping, exports, reports, and the
  cross-file consistency guards.
- **[`apps/services/tests/`](../apps/services/tests/)** - two kinds of test side by side:
  - pure-function unit tests for the reconciliation math (`_closeness()`, `_diff_pct()`,
    `_token_overlap()`, `_vendor_matches()`, `_po_number_matches()`), the parser normalization
    layer (`to_decimal()`, `to_date()`, `normalize_vendor()`, ...), the consumption engine and the
    other dependency-free helpers. **No Django DB touched** - those functions were written
    dependency-free for exactly this - so `test_matching.py` + `test_parsers_common.py` (205 tests)
    run in under a second;
  - pipeline-level tests that *do* seed rows and run `run_full_match()`, the `sync_*` commands
    (via `call_command(..., "--file", ...)` against a local file, never Drive), the consumption
    ledger and the report senders. About 29 of the 44 test files here use the DB.

The whole suite is ~1,217 tests and takes roughly 4 minutes locally, almost all of it the DB tests.

### Run against Postgres, never SQLite

**Run the suite against Postgres, never `config.settings_dev_sqlite`.** A local Postgres is
available on the development machine. SQLite produces false failures: `SUM()` over a
`decimal_places=3` column comes back as `75` on SQLite and `75.000` on Postgres, so
`test_estimate_rows_are_marked_and_explained_in_the_email_body` fails on SQLite and passes in CI -
which cost an investigation before the cause was clear. `settings_dev_sqlite` exists only for
manually smoke-testing `sync_*`/`match_*` commands without Postgres.

pytest uses `DJANGO_SETTINGS_MODULE = "config.settings"` (set in `pyproject.toml`), which reads
`DATABASE_URL` or the discrete `PG*` vars from `.env`. `addopts = "--reuse-db"` keeps the test
database between runs; pass `--create-db` after adding a migration or if the test DB looks stale.

### Coverage is a ratchet, and not assertion

Coverage was last measured at **94%** across `apps/` + `config/` (2026-09-23), with CI enforcing
`--cov-fail-under=92` - two points below the measured value so ordinary refactors don't fail it.
It is a ratchet: raise it when coverage genuinely improves, **never lower it to turn a red build
green**.

**Coverage is not assertion.** `middleware.py` and `security_headers.py` were ~95% covered but
never *asserted* about, so `test_security_headers_and_csrf_scope.py` pins the CSP's actual contents
and `AdminOnlyCsrfMiddleware`'s scope in both directions.

### A test must fail when the fix is reverted

One CSRF test initially passed even with `ADMIN_PATH_PREFIX` deliberately broken, because Django's
admin login view carries its own `@csrf_protect` - it was testing Django, not this middleware. It
was replaced with a direct `process_view()` test that fails in both directions. The same principle
is why [`refusing_email_backends.py`](../apps/services/tests/refusing_email_backends.py) fails at
the *backend* layer: monkeypatching `send_mail` to raise bypasses `fail_silently`, so a test built
that way passes whether or not the bug is present. Build the discrimination into the test itself;
never mutate source to "prove" a test catches something.

### Cross-file guards

Several guards are **cross-file by design**, because the drift they catch isn't visible to a
single-unit test:

- `test_local_date_timezone.py` scans app code for any new naive `date.today()` /
  `datetime.now()` / `datetime.today()`, independently of ruff's `DTZ` rules.
- `test_password_policy_is_stated_consistently.py` probes the real validator and checks every copy
  of the minimum (forms, error messages, `create_pt_user`) agrees.
- `test_css_token_collisions.py` checks `brand.css` and `style.css` don't define the same custom
  property with different values - it **found a fifth collision a manual grep sweep had missed**,
  which is precisely why it is a test and not a one-time cleanup.
- `test_endpoint_permission_guard.py` - every write endpoint declares `permission_classes` and every
  plant endpoint calls a plant-scoping helper (see
  [auth-security-email.md](auth-security-email.md)).
- `test_email_delivery_is_observable.py` - no application module passes `fail_silently=True`, and a
  failed send logs an ERROR (see [auth-security-email.md](auth-security-email.md)).
- `test_dockerignore_mirrors_gitignore.py` - `.dockerignore` must exclude everything `.gitignore`
  does. It claimed to and had not, missing `*.sqlite3.*`, the pattern added after a committed
  backup carried real bcrypt hashes.
- `test_no_em_dashes.py` - the em dash sweep as a rule (it had missed an em dash in a live email
  subject and all of `.env.example`).
- `test_models_package.py` - every model in `apps/core/models/` is re-exported from the package.
- `test_deploy_checks.py`, `test_ensure_schedules.py`, `test_material_category_reference.py`.

### Ruff is a real gate, deliberately narrow

The default ruleset reports ~1,000 findings here, almost all style (572 `FURB157` alone).
Mass-`--fix`ing that across 20k lines is exactly the sweeping, untestable churn that breaks a
working app, so `[tool.ruff.lint]` selects only rule families that catch a real defect (`F`,
`E4/E7/E9`, `B`, `DTZ`, `RUF013`, `PLE`, ignoring `B008` and `B904`) and was **driven to zero by
hand**. `DTZ` paid for itself immediately: it found four naive `date.today()` calls that made Import
POs read "On Order" instead of "Overdue" from 00:00 to 05:29 IST every day (Render runs in UTC).

**Adding a rule later is fine - drive it to zero in the same commit that selects it. Never add a
rule and leave existing violations behind; a lint gate with a known-failing baseline stops being a
gate.**

### ESLint runs only in CI, and is a real gate

**ESLint cannot run locally** - there is no Node in this development environment - so CI is where
it runs. If it reports findings, fix them or narrow a rule with a recorded reason; **don't delete
the step to get a green build.**

It once went a week without ever executing: every run ended with exit code **2** - ESLint's
configuration-error code, not its findings code (1) - because the config kept its notes in a
top-level `"//"` key, which ESLint 8's schema rejects, and `continue-on-error` showed the job green
the whole time. The notes are a real `/* */` comment now (ESLint's JSON config allows comments;
**don't move them back into a key**). The first genuine run (CI #102, commit `165360b`) was clean,
so `continue-on-error` was removed: **ESLint is a real red/green gate**, like ruff. A non-blocking
step whose exit code nobody reads can hide a crash as easily as a finding - don't add the flag
back.

`no-undef` is deliberately OFF and `.eslintrc.json` records why: all frontend files are plain
`<script>` tags sharing one global scope by design, so ESLint can't resolve cross-file calls without
an exhaustive hand-maintained globals list that would itself become a second source of truth.

### CI jobs

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull request, on
any branch, as three jobs on `ubuntu-22.04`:

- **`django`** (Postgres 16 service): `uv sync`, `pip-audit`, `ruff check .`,
  `makemigrations --check --dry-run`, `manage.py check`, `check --deploy --fail-level WARNING`
  (plain `check --deploy` never fails a build on its own - every deploy check is Warning-level - so
  `--fail-level WARNING` is what makes it real; `security.W003` is silenced in `settings.py` because
  the API uses JWT, not session CSRF), `migrate`, `createcachetable`, then the test suite with the
  coverage ratchet.
- **`docker-image`** - builds and boots the production image (see [Render](#render)).
- **`frontend-lint`** (Node 22): `node --check` on every `frontend/js/*.js`, then ESLint 8.

**CI actions run on Node 24** (`actions/checkout@v7`, `actions/setup-node@v7`,
`astral-sh/setup-uv@v7`): older majors targeted the deprecated Node 20 runtime and warned on every
run (`setup-uv@v6` is still Node 20 - v7 is the first on Node 24). ESLint itself runs on Node 22 LTS.

### CI runs Python 3.12 via UV_PYTHON

**CI runs Python 3.12 via the job-level `UV_PYTHON` env var, matching production (the Dockerfile's
`python:3.12-slim`).** It used to pass `python-version: '3.12'` to `setup-uv@v3`, an input v3 does
not have: every run logged "Unexpected input(s) 'python-version'", ignored it, and let uv fall back
to `.python-version` (3.14) - so CI tested on 3.14 while production ran 3.12. `UV_PYTHON` overrides
`.python-version`, which stays 3.14 for local dev. Every `.py` file in the repo parses under the
3.12 grammar (`ast.parse(..., feature_version=(3, 12))`); keep it that way - 3.13+/3.14-only syntax
passes locally and breaks CI and production.

### Still untested

The actual Drive API calls and the `sync_*` management commands' own file-fetching (the pipeline
tests feed them a local `--file`). Verification there stays manual - real parser runs against real
files, real sync/match runs against a real Postgres, direct inspection of the rendered dashboard.
The frontend has no JS test runner; `test_po_status_inputs.py` pins the API fields its status logic
depends on instead.

## Deployment, Docker, and `.env`

### Render

[`render.yaml`](../render.yaml) defines the Postgres database, the web service and the qcluster
worker. Both services must share the same `DJANGO_SECRET_KEY` **and** `JWT_SIGNING_KEY`: the web
service generates each once (`generateValue: true`) and the worker reads them via `fromService`.
Two independent `generateValue` entries give two different keys; for `DJANGO_SECRET_KEY` that broke
every queued task (django-q2 signs tasks with `SECRET_KEY`, so the worker failed every one with
`BadSignature`), and the worker once had its own `JWT_SIGNING_KEY` too - harmless only because the
worker never mints or verifies a token. `DJANGO_ALLOWED_HOSTS` is pinned to this service's own
hostname, not `.onrender.com`, which matches every other Render customer.

**Both services run `runtime: docker`** - one image, built from the repo's `Dockerfile`, the same
one `docker compose` builds locally. The runtime was changed **in place via a Blueprint sync**,
which Render supports for an existing service; recreating the services instead would have handed
out a new hostname and broken `ALLOWED_HOSTS` and the Google OAuth redirect, both pinned to `-pqgi`.
If a Blueprint sync ever refuses a runtime change, stop there - do not delete and recreate.

The move retired the native-runtime workaround for `.python-version` (`3.14`): `PYTHON_VERSION` plus
`--python 3.12` on every `uv` call, one of which, left unpinned, once crash-looped a deploy. The
image pins the interpreter instead (`python:3.12-slim`, `UV_PYTHON=3.12`,
`UV_PYTHON_DOWNLOADS=never`). **`UV_PYTHON` is load-bearing, not belt-and-braces**: `COPY . .`
brings `.python-version` into the image, and without it uv would honour that file over the base
image's Python.

**The base image is pinned by digest** (`python:3.12-slim@sha256:...`) and Dependabot's `docker`
entry bumps that digest weekly, ignoring minor/major Python versions. On the native runtime Render
patched the OS; on Docker it is this repo's job, and a tag-only `FROM` plus Render's layer cache can
build on a stale OS indefinitely. **Merge those Dependabot PRs** - they are the OS security updates.

Four things about the image and how Render runs it:

- **Release tasks run once per deploy, on the web service only.** `release.sh` (migrate,
  `ensure_schedules`, `createcachetable || true`) is the web service's `preDeployCommand`; a failure
  aborts the deploy and the old version keeps serving. Running them from `docker-entrypoint.sh` on
  every container start would have the web and worker containers migrating the shared Postgres at
  the same moment on every deploy, and again on every restart. The entrypoint runs them only when
  `RUN_RELEASE_TASKS=1`, which only docker-compose's `app` sets. **Do not add a `preDeployCommand`
  to the worker.** (The worker can start new code slightly before the web migration finishes; a
  task that touches a not-yet-migrated table fails and the next scheduled run retries it.)
- **Non-root.** The container runs as `app` (uid 10001), which owns only `logs/` - the one path
  written at runtime (`settings.py` creates it at import; `RotatingFileHandler` appends to
  `logs/app.log`; nothing else under `BASE_DIR` is written). If you add a runtime write anywhere
  else in the tree, give that path to `app` in the Dockerfile or it fails with a permission error in
  production only.
- **No `uv run` at runtime.** The venv is on `PATH` and root-owned; `uv run` re-syncs before every
  command, which as a non-root user is wasted startup at best and a crash at worst. Commands inside
  the container are plain `python manage.py ...`. `UV_NO_SYNC=1` is set as a backstop.
- **`--no-dev`** keeps pytest/ruff/coverage out of the image, and the web `CMD` is shell-form so it
  binds Render's `$PORT` (default 8000 for compose). The worker's command is `render.yaml`'s
  `dockerCommand` (`python manage.py qcluster`).

**CI's `docker-image` job is the only place the image is built before Render builds it** - there is
no Docker on the development machine. It builds the image, asserts the invariants above (non-root,
3.12, no dev deps, `logs/` writable, collectstatic ran: `staticfiles/js/main.js` and
`staticfiles/admin` exist), runs `release.sh` twice against a real Postgres (it must be
re-runnable), boots the web `CMD` on `PORT=10000` and requires a 200 from both `/api/health` and `/`
(Render's `healthCheckPath`), sending `X-Forwarded-Proto: https` so `SECURE_SSL_REDIRECT` does not
answer 301, and checks `qcluster` is still running after ten seconds.

Beyond the liveness check, `/api/health/ready` is a readiness probe meant for an external uptime
monitor: it reports degraded/stale when scheduled pipeline steps stop running, which is the silent
failure mode this app has actually had (qcluster down while every page still loaded - see
[data-sync.md](data-sync.md)).

**Static storage is plain `StaticFilesStorage`, and always has been in production.** `settings.py`
once named `STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"`, but
Django 5.1 removed that setting and this app runs 5.2, so it was silently ignored - no manifest, no
hashed names, no `.gz` files, ever. The `docker-image` job's first run caught it by asserting
`staticfiles.json` existed. The dead line was deleted rather than revived, because switching (via
`STORAGES["staticfiles"]`) is a behaviour change: hashed URLs from `index.html`'s `{% static %}`
tags, far-future cache headers, and a file missing from the manifest becoming a 500. `collectstatic`
under the manifest storage was checked to succeed if it is ever taken up. **A setting Django no
longer reads raises no warning** - check the effective value
(`django.contrib.staticfiles.storage.staticfiles_storage`) rather than the settings file.

### Docker (local dev)

The same image, plus a Postgres 16, via `docker-compose.yml`. It exists so a developer can run the
real stack without installing Python/uv/Postgres directly. Commands are under
[How to run](#how-to-run).

`app` sets `RUN_RELEASE_TASKS=1`, so it migrates before gunicorn starts - the local stand-in for the
`preDeployCommand`. `worker` depends on `app` having started and restarts on failure, since on a
fresh database it can come up before the migration finishes. `.gitattributes` pins `*.sh` to LF: a
`core.autocrlf` checkout would otherwise give the scripts CRLF endings, and a local build copies the
working tree (`/bin/sh^M: not found`).

`psycopg[binary]` ships a prebuilt wheel, so the image needs no `libpq-dev`/build toolchain. **Keep
it that way** - if a future dependency needs compiling, add build deps deliberately rather than by
reflex.

`docker-compose.yml` reuses `.env` for app secrets via `env_file`, and sets `DATABASE_URL` itself,
pointed at its own `db` service by name. **You do not need to change anything in `.env` for Docker
to work**, even though a developer's own `PGHOST`/`DATABASE_URL` typically says `localhost`.

### `.env` gotchas that have already cost debugging time

- **Never paste multi-line JSON into `GOOGLE_SERVICE_ACCOUNT_JSON=`.** `.env` parses one `KEY=VALUE`
  per line; a multi-line paste breaks every following line (symptom: `python-dotenv could not parse
  statement starting at line N` repeated, then `json.loads()` failing with `Expecting property name
  enclosed in double quotes: line 1 column 2`, because the captured value is just `{`). Put it on
  one line, or better, use `GOOGLE_SERVICE_ACCOUNT_FILE` with a path. (On Render only the JSON var
  works - the filesystem is ephemeral.)
- **Never quote a `GOOGLE_SERVICE_ACCOUNT_FILE` Windows path with double quotes.** `python-dotenv`
  applies backslash-escape processing inside double-quoted values, so a path containing `\r` as part
  of a longer word (e.g. `...\secrets\ravasco-...`) has that `\r` read as a carriage return and
  corrupted, breaking the open with a cryptic `[Errno 22] Invalid argument`. Leave the path unquoted.
- **`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` are a separate concern from
  `GOOGLE_SERVICE_ACCOUNT_JSON`/`FILE`** - the former is a human-login OAuth client, the latter the
  unattended Drive-sync service account. One cannot do the other's job.
- **This repo can sit inside a OneDrive-synced folder.** `.env` is gitignored (and has no git
  history), but OneDrive syncs independently of git and doesn't respect `.gitignore` at all, so a
  real secret in `.env` can reach Microsoft's cloud and every device on that account. That is a
  meaningfully different exposure path from "is it committed". **Exclude this folder from OneDrive
  sync before populating a real `.env`**, not after.
- `SMTP_USER`/`SMTP_PASS`/`SMTP_FROM` may be blank in local dev (`SMTP_HOST` defaults to
  `smtp.gmail.com`) - with `DJANGO_DEBUG=true` a failed OTP send falls back to printing the OTP to
  the console. `DJANGO_DEBUG` itself defaults to **false** when unset, so set it explicitly in a
  dev `.env` (`.env.example` does). `JWT_SIGNING_KEY` defaults to `DJANGO_SECRET_KEY` (and the
  deploy check warns about that outside DEBUG); `ALLOWED_EMAIL_DOMAIN` defaults to `ravasco.com`.

## How to run

All from the repo root. Local Python commands go through `uv run`; inside a container, use plain
`python`.

Tests (Postgres from `.env`; pytest defaults come from `pyproject.toml`):

```bash
uv run pytest -q                                   # full suite, ~4 min, reuses the test DB
uv run pytest -q --create-db                       # after adding a migration
uv run pytest -q apps/services/tests/test_matching.py   # one file; pure tests run in <1 s
uv run pytest --cov=apps --cov=config --cov-report=term --cov-fail-under=92   # what CI runs
```

Lint and checks (the same commands CI runs):

```bash
uv run ruff check .
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py check
# check --deploy needs production-like settings, so it fails under a dev .env (DEBUG=true):
DJANGO_DEBUG=false uv run python manage.py check --deploy --fail-level WARNING
# ESLint: CI only (no Node locally). CI runs: npx --yes eslint@8 frontend/js --ext .js --max-warnings 0
```

Local server (no Docker):

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createcachetable
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
uv run python manage.py runserver            # http://127.0.0.1:8000/login.html
uv run python manage.py qcluster             # the worker; needed for "Refresh Data" and schedules
```

`.claude/launch.json` defines `pt-dashboard-dev` (runserver on port 8020) and
`purchase-tracker-dev` (attaches to an already-running server on 8010).

Docker (the production image plus Postgres):

```bash
docker compose up -d
docker compose exec app python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
docker compose logs app
```

Deploy: push to `main`. Render builds the Dockerfile for both services, runs `/app/release.sh` on
the web service as the pre-deploy step, then swaps traffic. Secrets marked `sync: false` in
`render.yaml` are set in Render's dashboard (service -> Environment), never in the file.

## File reference

### pyproject.toml

[pyproject.toml](../pyproject.toml). Project metadata, `requires-python = ">=3.12"`, runtime
dependencies (Django `>=5.2,<7.0`, DRF, simplejwt, `psycopg[binary]`, whitenoise, django-q2,
croniter, sentry-sdk, gunicorn, Google client libs, openpyxl, bcrypt) and the `dev` dependency group
(pytest, pytest-cov, pytest-django, ruff). `[tool.pytest.ini_options]` sets
`DJANGO_SETTINGS_MODULE = "config.settings"`, `python_files = "test_*.py"` and `--reuse-db`.
`[tool.ruff]` targets `py312`, line length 140, excludes migrations/`.venv`/`staticfiles`; the lint
selection and per-file ignore (`**/tests/*` exempt from `DTZ`, since tests pin their own clock) are
described in [Ruff is a real gate, deliberately narrow](#ruff-is-a-real-gate-deliberately-narrow).
`uv.lock` is the real version pin - CI and the image install `--frozen` from it, so widening a range
here without re-locking changes nothing that is tested.

### Dockerfile

[Dockerfile](../Dockerfile). Single image for web and worker, local and production. `FROM` is
`python:3.12-slim` pinned by digest; env sets `UV_PYTHON=3.12`, `UV_PYTHON_DOWNLOADS=never`,
`UV_NO_SYNC=1`, `UV_LINK_MODE=copy` and puts `/app/.venv/bin` first on `PATH`. Installs a pinned
`uv` via pip, then `uv sync --frozen --no-dev --no-install-project` on the manifests alone (cached
layer), `COPY . .`, a second `uv sync --frozen --no-dev`, and `collectstatic` at build time (the
dev-only `SECRET_KEY` fallback covers it; no DB needed). There are **no `ARG` lines on purpose**:
Render only forwards env vars into a build as declared ARGs, so no secret can be baked into a layer.
Creates user `app` (uid 10001), which owns only `/app/logs`. `ENTRYPOINT` is
`docker-entrypoint.sh`; `CMD` is shell-form gunicorn on `${PORT:-8000}` with 2 workers.

### docker-compose.yml

[docker-compose.yml](../docker-compose.yml). Local dev only. `db` is `postgres:16` with a
`pg_isready` healthcheck and a named volume `pt_postgres_data` (credentials `purchase_tracker`,
local only). `app` builds the Dockerfile, reads `.env`, overrides `DATABASE_URL` to the `db`
service, sets `RUN_RELEASE_TASKS=1`, publishes 8000. `worker` runs `python manage.py qcluster`,
`restart: on-failure`, and waits for `db` healthy and `app` started.

### docker-entrypoint.sh

[docker-entrypoint.sh](../docker-entrypoint.sh). POSIX `sh`, `set -e`. Runs `/app/release.sh` only
if `RUN_RELEASE_TASKS=1`, then `exec "$@"` so gunicorn/qcluster become PID 1 and receive signals.
Must stay LF (see `.gitattributes`).

### release.sh

[release.sh](../release.sh). The DB-dependent release steps: `migrate --noinput`,
`ensure_schedules` (idempotent, never resets an existing schedule's `next_run`), and
`createcachetable || true` (the `|| true` covers only "table already exists"; `set -e` still
applies to every other line). Must be safe to run on every deploy against the same database - CI
runs it twice. Runs as Render's web `preDeployCommand` and as compose `app`'s entrypoint step.

### render.yaml

[render.yaml](../render.yaml). Render Blueprint. Database `purchase-tracker-db` on `basic-256mb`
(legacy slugs like `starter` are rejected for new Postgres). Web service
`purchase-tracker-dashboard` and worker `purchase-tracker-qcluster`: both `runtime: docker`,
`plan: starter`, `region: oregon`, `branch: main`. The web service has `healthCheckPath: /` and
`preDeployCommand: /app/release.sh`, no `dockerCommand`; the worker has
`dockerCommand: python manage.py qcluster` and no pre-deploy step. Env vars: `DJANGO_DEBUG=false`,
`DJANGO_ALLOWED_HOSTS` and `GOOGLE_OAUTH_REDIRECT_URI` pinned to the `-pqgi` hostname (rename or
recreate the service and both must change, or every request is a 400 and Google sign-in breaks),
`DATABASE_URL` from the database's `connectionString`, Drive folder/file ids and SMTP host/port as
plain values, and `sync: false` for every secret (`GOOGLE_SERVICE_ACCOUNT_JSON`, `SMTP_USER/PASS/FROM`,
`GOOGLE_CLIENT_ID/SECRET`, `SAFECUBE_API_KEY`, `SENTRY_DSN`). `REPORT_CRON_SECRET` is generated
(web only) and must be copied into the external cron-job.org jobs. The worker gets
`DJANGO_SECRET_KEY` and `JWT_SIGNING_KEY` via `fromService`; `SENTRY_DSN` is not shared
automatically and must be set to the same value on both services.

### .github/workflows/ci.yml

[.github/workflows/ci.yml](../.github/workflows/ci.yml). Jobs `django`, `docker-image`,
`frontend-lint` as described in [CI jobs](#ci-jobs). The `django` job's env holds CI-only dummy
keys, `DATABASE_URL` for the `pt_ci` Postgres service, blank/placeholder Google values (no live
Drive/OAuth call happens in CI) and `UV_PYTHON: "3.12"`. `DJANGO_DEBUG` is `false` in CI, which
turns on `SECURE_SSL_REDIRECT`; `settings.py` switches it off when pytest is loaded. `docker-image`
writes the same dummy values to a `ci.env` and runs containers with `--network host`. `pip-audit`
is installed ad hoc into the venv each run.

### .github/dependabot.yml

[.github/dependabot.yml](../.github/dependabot.yml). Weekly updates for `pip`, `github-actions` and
`docker`, all with a 7-day cooldown (owner rule: no library version is proposed until it has been
out a week). Django **major** versions are ignored - a major is a deliberate upgrade (re-lock, check
for settings the new version silently stops reading). For `docker`, minor/major Python bumps are
ignored so the digest updates stay on `3.12-slim`; changing the interpreter also means changing
CI's `UV_PYTHON`.

### .eslintrc.json

[.eslintrc.json](../.eslintrc.json). ESLint 8 config, `root: true`, browser + ES2022, `sourceType:
"script"` (no modules, no bundler). Only defect-catching rules (`no-redeclare`, `no-dupe-keys`,
`no-unreachable`, `no-fallthrough`, `valid-typeof`, `no-self-compare`, ...), no style rules;
`no-empty` allows empty `catch`; `Chart` is the one declared global. `no-undef` is off by design.
Its notes live in a leading `/* */` comment - a top-level `"//"` key crashes ESLint 8.

### .dockerignore

[.dockerignore](../.dockerignore). Everything in `.gitignore` plus `.git/`. Enforced by
`test_dockerignore_mirrors_gitignore.py`: **add every new ignore pattern to both files**.

### .gitignore

[.gitignore](../.gitignore). `.venv/`, `__pycache__/`, `*.pyc`, `.env`, `staticfiles/`,
`*.sqlite3` **and** `*.sqlite3.*` (the bare glob missed a suffixed backup that was committed with
real bcrypt hashes), `.pytest_cache/`, `logs/`, `_a11y_tmp/`, and coverage artifacts.

### .gitattributes

[.gitattributes](../.gitattributes). One rule: `*.sh text eol=lf`, so a Windows `core.autocrlf`
checkout cannot give the container scripts CRLF endings.

### .python-version

[.python-version](../.python-version). `3.14`, a local-dev convenience only. CI (`UV_PYTHON`) and the
image (`UV_PYTHON` + `UV_PYTHON_DOWNLOADS=never`) both override it with 3.12, which is what
production runs.

### .env.example

[.env.example](../.env.example). The template for `.env` (copy it, then fill in). Groups: Django
core (`DJANGO_SECRET_KEY`, `DJANGO_DEBUG=true`, `DJANGO_ALLOWED_HOSTS`), Sentry (`SENTRY_DSN` blank
disables it, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`), database (`DATABASE_URL` or
`PGDATABASE/PGUSER/PGPASSWORD/PGHOST/PGPORT`), Google service account (`GOOGLE_SERVICE_ACCOUNT_JSON`
or `_FILE`, never both), Drive folder ids, auth (`ALLOWED_EMAIL_DOMAIN`, `JWT_SIGNING_KEY`), SMTP,
Google OAuth client, `SAFECUBE_API_KEY`, `REPORT_CRON_SECRET` (blank makes every report endpoint
refuse with 503), `MISMATCH_REPORT_PLANT_HEADS_ENABLED` (killswitch, default `false`),
`DELETE_USER_ALLOWED_EMAIL`, and `EMAIL_OTP_POOL_WORKERS`/`EMAIL_BULK_POOL_WORKERS`. The file is
covered by the em dash guard. Never read or paste the real `.env`.

### .claude/launch.json

[.claude/launch.json](../.claude/launch.json). Dev-server entries for the in-app browser preview:
`pt-dashboard-dev` runs `uv run python manage.py runserver 0.0.0.0:8020` via `cmd`;
`purchase-tracker-dev` has no command and attaches to `http://localhost:8010`.

### config/settings_dev_sqlite.py

[config/settings_dev_sqlite.py](../config/settings_dev_sqlite.py). `from config.settings import *`
with `DATABASES` swapped for `dev_smoke_test.sqlite3`. For manually smoke-testing `sync_*`/`match_*`
commands without Postgres (`DJANGO_SETTINGS_MODULE=config.settings_dev_sqlite python manage.py ...`).
Not used by CI, production or the test suite, and **not valid for running tests** (see
[Run against Postgres, never SQLite](#run-against-postgres-never-sqlite)).

### Test helpers (no conftest.py)

There is no `conftest.py` anywhere; configuration is `pyproject.toml` plus these:

- [`apps/api/tests/factories.py`](../apps/api/tests/factories.py) - `make_user(email, password,
  role="viewer", **extra)` creates an active `PTUser` with a real bcrypt hash; `extra` passes through
  (e.g. `plants=[...]`). Most API tests pair it with `APIClient.force_authenticate()`.
- [`apps/services/tests/refusing_email_backends.py`](../apps/services/tests/refusing_email_backends.py)
  - email backends that fail like a dead SMTP server and honour `fail_silently` exactly as Django's
  SMTP backend does: `REFUSE_ALL`, `REFUSE_IMPORT_VALIDITY`, `REFUSE_ALL_BUT_VAPI`,
  `STALL_ON_CONNECT`, plus `LOCMEM`. Set `settings.EMAIL_BACKEND` to one of these dotted paths. No
  `test_` prefix, so not collected.
- Pytest-only switches in `config/settings.py`, keyed on `"pytest" in sys.modules` (not `sys.argv`):
  `CACHES` becomes `LocMemCache` (the test DB never gets the `DatabaseCache` table) and
  `SECURE_SSL_REDIRECT` is forced off. The email dispatch pools also run inline under pytest (see
  `test_email_dispatch_pool.py`).

### Test suites

`apps/services/tests/` (DB column: uses `@pytest.mark.django_db`):

| File | DB | Covers |
|---|---|---|
| test_advance_license_report.py | yes | Advance License Import/Export validity-expiry alerts: 30-day window, once per license, re-alert on extension, claim released on send failure. |
| test_arithmetic_checks.py | no | `arithmetic_checks.py` qty x rate = value checks and tolerance. |
| test_consumption_engine.py | no | Pure consumption engine: issue-book deltas, receipts, restatements, closeouts, which intervals come back as events; several fixtures are real rows. |
| test_consumption_ledger.py | yes | Consumption ledger DB layer and period rollups: lot-to-material rollup, closeout/restatement events (including zero-quantity restatements), idempotent rebuilds. |
| test_consumption_report.py | yes | Daily/monthly Raw Material Consumption emails read from the ledger; per-plant sends, estimates, send-failure handling, one confidence legend for both bodies. |
| test_email_delivery_is_observable.py | yes | Source guard: no `fail_silently=True`; a failed admin alert logs ERROR and never claims "sent". |
| test_email_dispatch_pool.py | yes | Bounded OTP/bulk email thread pools: no thread per email, OTP lane never blocked, inline fallback. |
| test_google_client_query.py | no | `find_file_id_by_title()` Drive query construction and escaping, newest-first pick among duplicates, `list_files_in_folder()` pagination (fake service, no API call). |
| test_import_flags.py | no | Import PO stage (placed/shipped/cleared) and BOE qty discrepancy flags. |
| test_license_links.py | yes | Import line License Type/Number normalisation and join to the RoDTEP/Advance License ledgers. |
| test_manual_mir_match.py | yes | Domestic manual MIR pins in `run_full_match()`: pin outranks matcher, survives rematch, forced-unmatched, collisions. |
| test_manual_mir_match_imports.py | yes | Import MIR pins: `po_kind` separation, domestic vs import pins competing for one MIR table. |
| test_matching.py | no | Pure PO<->MIR<->Stock scoring helpers (`_closeness`, `_diff_pct`, `_token_overlap`, vendor/PO-number matching). |
| test_matching_query_scaling.py | yes | `run_full_match()` query count does not scale with row count (N+1 regression). |
| test_material_category_reference_parser.py | no | Material category reference sheet parser. |
| test_mir_stock_identification.py | no | MIR<->Stock description cleaning, fuzzy tokens, grade-code contradiction gate, `NO_RM_STOCK_VENDORS`. |
| test_mir_stock_pipeline_tiers.py | yes | MIR<->Stock three-tier identification against real rows (name, fuzzy, date+rate). |
| test_mir_without_po.py | yes | "Purchased without a PO" three-bucket classification and badge reconciliation. |
| test_no_po_vendors.py | yes | `NO_PO_VENDORS` registry lookups and its wiring into the matcher's candidate pool. |
| test_parsers_common.py | no | Parser normalisation helpers (`to_decimal`, `to_date`, `normalize_vendor`, ...). |
| test_plant_mismatch_report.py | yes | Per-plant Data Correction email: rows, dismissals, plant head + admin CC routing. |
| test_po_csv_parser.py | no | PO master CSV header/row-shape edge cases (trailing-space headers, blank currency, grouping). |
| test_po_deactivation.py | yes | POs dropped from the master CSV are deactivated (not deleted) and stop competing for MIR rows. |
| test_po_number_groups.py | yes | One PO, many MIR receipts: every receipt naming the PO is saved and compared as one total. |
| test_received_against_line.py | no | `received_against_line()` sums matched MIR qty in the PO line's unit. |
| test_run_full_match_achhad_pipeline.py | yes | Achhad `run_full_match()` against real rows (no stock vendor column, legacy PO formats). |
| test_run_full_match_import_pipeline.py | yes | Import PO<->MIR matching with currency conversion and BOE qty. |
| test_run_full_match_pipeline.py | yes | HRS `run_full_match()` against real rows: flags, vendor gate, NO_PO vendors, idempotency. |
| test_run_full_match_atomic.py | yes | `run_full_match()` is one transaction: a failure part-way rolls back writes already made. |
| test_run_full_match_vapi_pipeline.py | yes | Vapi `run_full_match()`: taxable value, vendor containment, date-confirmed rate flags. |
| test_security_alerts.py | yes | Admin alerts for failed-login bursts and sync failures, with suppression window. |
| test_stock_consumption.py | no | Pure `consumption_stats()` days-of-cover math with receipt handling. |
| test_stock_identity.py | no | `lot_natural_key()` composition and `OccurrenceCounter` suffixing. |
| test_sync_achhad_pipeline.py | yes | Achhad `sync_*` commands against local files: idempotency, change pickup, deactivation, bad sheet. |
| test_sync_advance_license_pipeline.py | yes | `sync_advance_license`: header detection, whole-license hash, materials rebuilt together. |
| test_sync_imports_pipeline.py | yes | Import PO CSV sync (`sync_orders()`): idempotency, trailing blank headers, whitespace in header names, header mismatch. |
| test_sync_mir_pipeline.py | yes | HRS `sync_mir`: per-field change detection, deactivation, wrong sheet name. |
| test_sync_po_csv_pipeline.py | yes | HRS `sync_po_csv`: idempotency, change pickup, header mismatch recorded as failed SyncRun. |
| test_sync_rodtep_pipeline.py | yes | `sync_rodtep`: (script_no, sb_number) keys, idempotency, one bad file does not block others. |
| test_sync_stock_row_insert.py | yes | Stock lot identity and snapshot history survive a mid-sheet row insert. |
| test_sync_trigger_daily.py | yes | `run_daily_sync_all_plants()` locking and per-plant isolation (pipeline stubbed). |
| test_sync_vapi_pipeline.py | yes | Vapi `sync_*` commands: sheet shapes, GST as whole percent, PO column, deactivation. |
| test_validation.py | no | GSTIN and email format validation for inline edits. |

`apps/api/tests/` (all integration; the four marked "no" are source/file guards):

| File | DB | Covers |
|---|---|---|
| test_achhad_correct_field.py | yes | Achhad inline "Edit Everywhere": roles, plant scoping, audit row, validation. |
| test_achhad_dismiss_flag.py | yes | Achhad PO-level flag dismiss/reinstate. |
| test_achhad_dismiss_match.py | yes | Achhad PO<->MIR / MIR<->Stock match dismissal, survives rematch. |
| test_admin_overview.py | yes | `GET /api/auth/admin-overview` admin-only aggregates. |
| test_advance_license_api.py | yes | Advance License ledger read and admin sync trigger (409 while running). |
| test_api_no_store.py | yes | `ApiNoStoreMiddleware`: API responses default to `no-store`. |
| test_auth_flow.py | yes | Login, new-device OTP, cookies, throttling, lockout and its admin alert. |
| test_browser_session_renewal.py | yes | Refresh-cookie renewal contract `authFetch()` relies on. |
| test_css_token_collisions.py | no | `brand.css` vs `style.css` custom-property value collisions. |
| test_csv_formula_injection.py | yes | CSV exports neutralise formula-prefixed cells. |
| test_delete_user.py | yes | User delete restricted to one configured admin; last admin protected. |
| test_deploy_checks.py | yes | Custom deploy checks: JWT key fallback, active superuser warning. |
| test_signing_key_and_cli_reset.py | yes | A blank or whitespace `JWT_SIGNING_KEY` falls back to `SECRET_KEY` (fresh interpreter); a `create_pt_user` reset bumps `token_version`. |
| test_device_hashing.py | yes | Trusted-device tokens stored only as SHA-256 hashes. |
| test_dismiss_flag.py | yes | HRS and import PO-level flag dismiss/reinstate. |
| test_dismiss_match.py | yes | HRS match dismissal with reason, survives rematch; string `"false"` reinstates, a missing flag dismisses. |
| test_dockerignore_mirrors_gitignore.py | no | `.dockerignore` covers every `.gitignore` pattern. |
| test_endpoint_permission_guard.py | no | Source scan: write endpoints declare permissions, plant endpoints scope by plant. |
| test_ensure_schedules.py | yes | `ensure_schedules` is idempotent and never resets `next_run`. |
| test_error_visibility.py | yes | No silent failures: OTP generation error, SyncRun rows and error detail. |
| test_hrs_correct_field.py | yes | HRS inline "Edit Everywhere" with plant scoping. |
| test_imports_api.py | yes | Import dashboard API: combined plants, derived fields, correct_field, retired POs 404 on detail and correction. |
| test_imports_sync_status_company_wide_flags.py | yes | Import sync-status exposes RoDTEP/Advance License in-progress flags. |
| test_last_admin_race.py | yes | Concurrent last-admin deactivation is serialised (row locking). |
| test_local_date_timezone.py | yes | Delivery status follows `TIME_ZONE`; source guard for naive dates. |
| test_login_timing_dummy_hash.py | yes | Unknown-email login costs a real bcrypt check (timing oracle). |
| test_logout_everywhere.py | yes | Self and admin "log out everywhere" via `token_version`. |
| test_manual_mir_match_api.py | yes | Domestic `mir-candidates` / `mir-match` endpoints; a domestic set or clear never touches an import pin on the same PO number. |
| test_manual_mir_match_imports_api.py | yes | Import MIR pin endpoints: 404 for out-of-scope plant, `po_kind=import`. |
| test_material_category_reference.py | yes | Canonical category/subcategory lookup on `GET /api/materials`. |
| test_material_correct_field.py | yes | Raw Material modal inline edit on stock lots. |
| test_materials_days_left.py | yes | Days-of-cover on `/materials` read from the consumption ledger. |
| test_mir_without_po_endpoint.py | yes | `mir-without-po` drill-down: shape, bucket filter, plant gate, CSV. |
| test_models_package.py | no | `apps/core/models/` package re-exports every model. |
| test_no_em_dashes.py | no | No em dash in any text file in the repo (code, docs, config; skips `.venv`, migrations, `staticfiles`). |
| test_oauth_lockout_and_error_leak.py | yes | Google OAuth respects lockout; exception handler does not leak internals. |
| test_password_change.py | yes | OTP-gated self-service password change. |
| test_password_change_revokes_sessions.py | yes | Password change/reset revokes other sessions, keeps device trust. |
| test_password_policy.py | yes | Password strength policy on create/reset. |
| test_password_policy_is_stated_consistently.py | no | Every stated password minimum matches the enforced one. |
| test_password_reset.py | yes | Admin password reset via `update_user()`. |
| test_po_status_inputs.py | yes | Line-item fields the frontend's `computeStatus()` depends on. |
| test_prune_revoked_tokens.py | yes | `prune_revoked_tokens` deletes only expired rows. |
| test_raw_material_order_payload.py | yes | `locationTag` on lots and per-line import categories. |
| test_read_endpoint_plant_scoping.py | yes | Read endpoints honour `PTUser.plants` (404 not 403 for import details). |
| test_readiness_probe.py | yes | `/api/health/ready` ok/degraded/stale/down semantics. |
| test_refresh_token_never_in_body.py | yes | Refresh token only ever in the httpOnly cookie. |
| test_reports_views.py | yes | Shared-secret report cron endpoints: 503/403/200/502 behaviour. |
| test_response_compression.py | yes | `SelectiveGZipMiddleware`: API gzipped, `/api/auth/` and admin never. |
| test_review_card_payload.py | yes | Match-review card identifiers, UOM, signals, converted rate. |
| test_review_plant_scoping.py | yes | Review queue respects plant scoping. |
| test_review_stats_and_undo.py | yes | Precision/recall figures, latest verdict wins, undo, CSV export. |
| test_rodtep_api.py | yes | RoDTEP scrip ledger API and import citations. |
| test_security_headers_and_csrf_scope.py | yes | CSP contents, Permissions-Policy, CSRF only under `/admin/`. |
| test_stock_matched_field.py | yes | `stockMatched` on line items requires a real MIR<->Stock match. |
| test_stock_snapshots_api.py | yes | Stock snapshot date list and by-date endpoints, 404/400, plant scoping. |
| test_user_devices.py | yes | Trusted devices admin list/revoke, hash never exposed, audit rows. |
| test_users_plants.py | yes | `plants` on user create/update and `/api/auth/me`. |
| test_vapi_correct_field.py | yes | Vapi inline "Edit Everywhere". |
| test_vapi_dismiss_flag.py | yes | Vapi PO-level flag dismiss/reinstate. |
| test_vapi_dismiss_match.py | yes | Vapi match dismissal. |
