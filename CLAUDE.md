# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this app is

A standalone Django service that reconciles Purchase Orders, MIR (Material Inward Register), and
Raw Material Stock for Ravasco's plants, replacing the earlier Claude Artifact "Purchase Tracker"
prototype (which re-fetched and re-parsed every Drive file live on every page load, with no
persistence). This app syncs Drive data into Postgres on demand/schedule instead, so every viewer
sees the same pre-computed reconciliation results without depending on their own Drive session.

**This is a separate service/repo from the TDS Automation App** (`C:\Users\Admin\OneDrive\Desktop\TDS
Automation App\tds_app`) — same architectural conventions (Django + DRF + Postgres + WhiteNoise,
`uv`-managed, static frontend with no build step), but no shared code, database, or deployment.
Do not conflate the two when reading past session history — they are unrelated codebases that
happen to look similar on purpose. **The auth/security scaffolding in this app (device-aware 2FA,
JWT-in-httpOnly-cookie, role permissions, `core`/`api`/`services` layering, CI) is a deliberate,
mechanical port of the TDS app's own auth architecture** — see "Auth & security architecture"
below for exactly what was ported vs. simplified, and why.

## Commands

All commands run from the repo root (there is no `django_backend/` subdirectory here, unlike the
TDS app).

```bash
uv sync                                    # install/update dependencies
uv run python manage.py runserver          # dev server
uv run python manage.py migrate            # apply migrations
uv run python manage.py makemigrations core
uv run python manage.py createcachetable   # one-off: creates DatabaseCache's table (pt_cache_table)
uv run python manage.py ensure_schedules   # idempotent: creates/corrects the daily-sync-all-plants django-q2 Schedule row
uv run pytest                              # test suite (real Postgres, no mocking - see below)
uv run python manage.py check --deploy --fail-level WARNING  # production security check - also runs in CI, see below

# Create/update a PTUser (bcrypt-hashes the password) - the only way to
# create the first admin account; re-running against an existing email
# updates password/role/full_name/designation instead of erroring.
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

# HRS pipeline
uv run python manage.py sync_po_csv        # PO master CSV -> HRSPurchaseOrder/HRSPOLineItem
uv run python manage.py sync_mir           # MIR xlsx -> HRSMIREntry
uv run python manage.py sync_stock         # Stock xlsx -> HRSStockLot (+ today's HRSStockSnapshot)
uv run python manage.py match_hrs          # run PO<->MIR and MIR<->Stock matching

# RTP-Achhad pipeline (identical shape, separate models/commands)
uv run python manage.py sync_achhad_po_csv
uv run python manage.py sync_achhad_mir
uv run python manage.py sync_achhad_stock
uv run python manage.py match_achhad

# RTP-Vapi pipeline (identical shape, separate models/commands)
uv run python manage.py sync_vapi_po_csv
uv run python manage.py sync_vapi_mir
uv run python manage.py sync_vapi_stock
uv run python manage.py match_vapi

# Every sync_* command accepts --file <path> to parse a local copy instead of
# fetching from Drive - useful for offline dev/testing without touching the
# real Drive files or needing live service-account credentials.
```

### Docker (local dev)

Added 2026-09-05, local-dev only - does **not** replace `render.yaml`'s own deploy pipeline;
Render still builds/deploys this app its own way. Exists so a developer can run a real Postgres +
this app together without installing Python/uv/Postgres directly, and so local dev can match
Render's actual runtime (Python 3.12.8, gunicorn, a separate `qcluster` worker process) rather
than whatever a developer happens to have installed locally - this repo's own `.python-version`
(`3.14`) is intentionally NOT what the Docker image uses; the `Dockerfile` pins `python:3.12-slim`
to match `render.yaml`'s `PYTHON_VERSION: "3.12.8"` instead, since that's the environment that
actually matters for parity.

```bash
docker compose up -d                              # app + worker (qcluster) + a real Postgres
docker compose exec app uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
docker compose logs app                           # check for tracebacks
```

`Dockerfile` bakes `uv sync --frozen` and `collectstatic --noinput` in at build time (static files
don't depend on a live DB connection); `docker-entrypoint.sh` runs `migrate --noinput` then
`createcachetable` (guarded with `|| true` so a container restart doesn't error on an
already-existing cache table - Django's `createcachetable` isn't safely re-runnable on its own
terms) at container start, before handing off to the CMD (gunicorn for the `app` service,
`manage.py qcluster` for the `worker` service - same image, different command, set per-service in
`docker-compose.yml`). `psycopg[binary]` (already pinned in `pyproject.toml`/`uv.lock`) ships a
prebuilt wheel, so the image needs no `libpq-dev`/build toolchain - keep it that way; if a future
dependency needs compiling, add build deps deliberately rather than by reflex.

`docker-compose.yml` reuses `.env.example`/`.env` as the single source of truth for app secrets
(Google/SMTP/JWT/OAuth vars) via `env_file: .env` - **you do not need to change anything in
`.env` for Docker to work.** `DATABASE_URL` is set explicitly in `docker-compose.yml` itself,
pointed at its own `db` service by name (`db`, not `localhost`) - this overrides whatever
`DATABASE_URL`/`PGHOST` happens to be in a developer's own `.env` (typically `localhost`, for the
non-Docker flow against a locally-installed Postgres). See `.env.example`'s own comment next to
`DATABASE_URL` for the same note in-place.

**Test coverage is split into two deliberately different scopes, mirroring the TDS app's own
split between `apps/api/tests/` (integration) and `apps/services/tests/` (pure calculation):**
- `apps/api/tests/test_auth_flow.py` — integration tests for the login → device-verify → cookie →
  protected-endpoint flow. Real Postgres test DB (`uv run pytest`, no `manage.py test` — this app
  kept its already-chosen `pytest`/`pytest-django`), no mocking, a shared `make_user()` factory
  (`apps/api/tests/factories.py`).
- `apps/services/tests/test_matching.py` / `test_parsers_common.py` — pure-function unit tests for
  the actual reconciliation math (`_closeness()`, `_diff_pct()`, `_token_overlap()`,
  `_vendor_matches()`, `_po_number_matches()` in `matching.py`) and the parser normalization layer
  (`to_decimal()`, `to_date()`, `normalize_vendor()`, etc. in `parsers/common.py`). No Django DB
  touched at all (these functions were deliberately written dependency-free for exactly this — see
  `parsers/common.py`'s own module docstring) — runs in well under a second. `matching_achhad.py`/
  `matching_vapi.py` carry byte-for-byte identical copies of the same five helpers (a deliberate,
  documented duplication, not an oversight — see `test_matching.py`'s module docstring); only
  HRS's copy is directly tested, so a change to one plant's copy without mirroring it in the other
  two won't be caught here.

**What's still explicitly out of scope**: the Drive-sync/parse/matching *pipeline* itself (the
DB-touching `sync_*`/`match_*` management commands, `run_full_match()`, the actual Drive API
calls) has no automated test coverage — deferred on purpose, verified so far by running parsers
directly against real downloaded files and inspecting sync/match output against both a throwaway
SQLite DB (`config/settings_dev_sqlite.py`, dev-only) and the real local Postgres. Don't extend
either existing test scope to cover this without a deliberate decision to do so.

## Architecture

**`apps/core`** — models and migrations only. No views, no business logic (this changed — see
"Auth & security architecture" below for why parsers/matching/Drive-access moved out).
**`apps/api`** — every HTTP-facing view, under `apps/api/routers/*_views.py`. Thin: builds response
dicts from the ORM or calls into `apps/services`, no business logic beyond what a view needs.
**`apps/services`** — Drive access (`google_client.py`), per-plant parsers (`parsers/`), the
PO↔MIR↔Stock matching engines (`matching.py`/`matching_achhad.py`/`matching_vapi.py`),
change-detection (`sync_utils.py`), and the auth-adjacent services (`device_service.py`,
`otp_service.py`, `email_service.py`). Management commands (`sync_*`/`match_*`) import from here,
not from `apps.core` — if you're about to add `import ... from apps.services.parsers` or
`apps.services.matching` anywhere, that's the old (pre-auth-pass) import path; it should be
`apps.services.*` now.

**Frontend is static HTML + vanilla JS**, served by WhiteNoise directly from `frontend/` (no
build step, no bundler) — same pattern as the TDS app. `frontend/js/main.js` is the dashboard;

**Superseded (2026-09-04): `main.js` was a single 3,460-line file; it's now split into one file
per concern** — `main.js` (shared `state`, per-plant caches, filter-reset helpers, and the
bootstrap/nav/sync-polling code `init()` calls directly), `charts.js` (Chart.js lifecycle +
plugins), `flags.js` (PO status/match-badge/Data-Quality-Flag rendering, shared across every
list/modal), `po-list.js`/`po-modal.js` (Domestic Purchases), `import-po.js` (Import Purchases
list + modal), `materials.js`/`material-modal.js` (Raw Material Analysis). Every mention of
"`main.js`'s `<function>`" elsewhere in this file may now actually live in one of these siblings
instead — grep the `frontend/js/` directory rather than assuming `main.js` still has it. Still no
bundler/ES modules: every file is a plain `<script>` tag in `index.html`, sharing one global scope
(same pattern `shared.js`/`auth.js` already used) — `main.js`'s own header comment has the full
module map and load-order rationale (every other file must load before `main.js`, since its
`init()` reaches into all of them).

`frontend/js/auth.js` (no imports, loads first) gates it behind `requireAuth()`;
`frontend/login.html` + `frontend/js/login.js` is the sign-in page (password + email-OTP step +
"Sign in with Google"). `frontend/css/style.css` is ported near-verbatim from the original
"Purchase Tracker" Claude Artifact this UI is modeled on for the dashboard itself, plus a page-
specific `<style>` block in `login.html` for the sign-in card (same TDS-app convention: cross-page
rules in the shared stylesheet, page-specific rules inline).

## Auth & security architecture

Ported from the TDS Automation App's own auth stack — device-aware 2FA (email OTP on a new
device, cookie-based device trust thereafter), JWT-in-httpOnly-cookie auth with a Bearer-header
fallback for non-browser clients, role-based permissions, the `core`/`api`/`services` layering,
DB-backed cache with a test-mode override, hardened security headers, admin-only CSRF, a shared
DRF exception handler, and CI (migration-drift check, migrate-from-scratch, test suite). See that
app's `CLAUDE.md`/`ARCHITECTURE.md` for the original design writeup if you need more context than
what's below.

**Deliberately NOT ported, and why** (don't "fix" these back to match TDS without re-deciding):
- **No `django-cors-headers`.** This app's frontend has always been same-origin (WhiteNoise serves
  it from the same process the API runs on) — there's no cross-origin case CORS would solve here,
  unlike TDS's historical separate-dev-origin workflow.
- **Package manager stayed `uv`/`pyproject.toml`, not `pip`/`requirements.txt`.** This app's own
  established convention; CI's `pip-audit` step runs against the `uv`-managed venv directly.
- **Test runner stayed `pytest`/`pytest-django`, not `manage.py test`.** Already a dev dependency
  here before this pass; kept rather than swapped.
- **No migration-history baggage.** `PTUser`/`OTPCode`/`TrustedDevice` are plain `AutoField`-PK
  models with a clean migration history — none of TDS's `TDSUser` pre-Django-schema workarounds
  apply here.
- **`cache_page` infrastructure exists, nothing uses it.** See ARCHITECTURE.md's "Known
  architectural constraints".

**`AUTH_USER_MODEL` is deliberately left at Django's default (`auth.User`) — this app's real user
model, `PTUser` (`apps.core.models`), is a plain, unrelated model, not a Django auth user.** Every
real authentication path resolves `PTUser` directly instead of touching `get_user_model()`/
`AUTH_USER_MODEL` at all: `PTUserBackend.authenticate()`/`get_user()`, `PTJWTAuthentication.get_user()`
(reads the `sub` claim), and `pt_user_authentication_rule()` (all in `apps/api/auth_backend.py`)
each query `PTUser` explicitly. **Any new code — or any djangorestframework-simplejwt upgrade —
that calls `django.contrib.auth.get_user_model()` will silently resolve to `auth.User`, not
`PTUser`, and almost certainly do the wrong thing or crash outright.** This is not a theoretical
risk: it caused a real production incident in the TDS app (simplejwt's stock
`TokenRefreshSerializer.validate()` calls `get_user_model().objects.get(**{USER_ID_FIELD: ...})`
to re-check the user is still active, and `auth.User` has no `user_id` field — every
`POST /api/auth/token/refresh` crashed). Fixed here the same way, proactively, before it could
bite: `PTTokenRefreshSerializer` (`apps/api/auth_serializers.py`) overrides `validate()` to
resolve `PTUser` directly, wired in via `PTTokenRefreshView.serializer_class`. **Before adopting
any simplejwt/DRF upgrade, grep it for new `get_user_model()` call sites.**

**`cache_page` must never sit above a permission check, and any `cache_page`-wrapped view must be
`AllowAny`.** `cache_page` short-circuits on a cache hit and returns the stored response without
ever re-invoking the view — so a permission check inside the view only actually runs on the
request that misses the cache; after that, the same response is served to any caller regardless
of auth, for as long as the cache entry lives. This bit the TDS app in production on a real
endpoint. No endpoint in this app currently uses `cache_page` (see ARCHITECTURE.md), but if one
ever does, it must be genuinely public data, `AllowAny`, and never gate anything sensitive behind
it.

**`SecurityHeadersMiddleware` must sit before `WhiteNoiseMiddleware` in `MIDDLEWARE`, not after.**
Django's middleware list is outermost-first for the request phase — a later entry is more inner.
`WhiteNoiseMiddleware` short-circuits static-file responses (every frontend HTML/CSS/JS page) by
returning directly, without calling further down the chain. A middleware placed after it never
runs for any static-file response — which, in a frontend with no separate SPA shell, is most of
what a browser actually renders and executes. The TDS app shipped with this ordering backwards for
a while before catching it; this app's `config/settings.py` has it correct from the start, but if
you ever reorder `MIDDLEWARE`, re-check this specifically.

**Role model**: `admin` (full access, plus user management — see "In-app user management" below),
`editor` (full dashboard access, plus dismissing/overriding a flagged match and the inline
"Edit Everywhere" field corrections), `viewer` (read-only dashboard access). Read endpoints (PO
list, materials, stock trend, sync status) stay plain `IsAuthenticated` (any role); write
endpoints are gated narrower — see "Known gaps" below for the current, accurate list of which
endpoints require `IsEditor` vs. `IsAdmin`.

**Superseded (2026-09-04): the paragraph above used to say dismiss/override "once that endpoint
exists" and that user management has "no custom admin UI yet."** Both are now built — the
dismiss/override endpoint (see "Dismiss/override a flagged match" below) and `admin.html`'s
in-app Users panel (see "In-app user management" below) — this paragraph just hadn't been updated
to match when those shipped. Don't trust an unqualified "no API yet"/"via Django Admin" claim
anywhere else in this file without checking the dated sections below first.

**A real bug, found and fixed, 2026-09-04: `LoginRateThrottle`/`DeviceVerifyThrottle` were keyed
per-IP, not per-account — an office full of colleagues could lock each other out of login.**
`AnonRateThrottle`'s default `get_cache_key()` keys its bucket on client IP. `PTLoginView`'s
`LoginRateThrottle` (5/minute) and `device_verify`'s `DeviceVerifyThrottle` (10/minute) inherited
that unchanged, which means every caller behind the same office router/VPN/NAT exit IP shared ONE
bucket — a handful of people signing in within the same minute was enough to exhaust it, after
which every subsequent login attempt from that IP got a generic `429 {"detail": "Request was
throttled..."}` response indistinguishable, from the frontend's perspective, from a real failure —
reported by the project owner as sign-in "misbehaving very much even [with] correct credentials."
Confirmed as the real mechanism, not just a plausible theory: `test_auth_flow.py`'s `setup_method`
already had to `cache.clear()` before every test specifically because this throttle's cache entries
aren't part of the per-test DB transaction rollback — proof the bucket really was one shared,
persistent counter, not scoped per test/account already.

Fixed in `apps/api/auth_views.py`'s `LoginRateThrottle.get_cache_key()` (keys on the submitted
`email`, falling back to the inherited IP-based key only when no email was submitted at all) and
`apps/api/routers/device_views.py`'s `DeviceVerifyThrottle.get_cache_key()` (keys on the Django
session's `pending_user_id`, set by `PTTokenObtainPairSerializer.validate()` on a new-device login
— unique per in-flight login attempt, not shared across users, same reasoning). Brute-force
protection is unchanged in strength per account; it just no longer pools unrelated accounts into
the same bucket. Regression test:
`test_auth_flow.py::TestLogin::test_login_throttle_is_scoped_per_email_not_shared_across_ip` (one
`APIClient`/IP, two different accounts — exhausting the first account's bucket must not block the
second's first attempt). **If you add another `AnonRateThrottle` subclass anywhere in the auth
flow, key it the same way — per-account/per-session, not the inherited per-IP default — or this
exact bug reappears.**

### Security hardening pass (added 2026-09-05)

Three parallel audits (frontend XSS/injection surface, backend injection/rate-limiting, auth/
token/dependency security) confirmed **no exploitable injection vulnerability existed anywhere in
this app** before this pass — zero raw SQL/eval/exec calls anywhere in `apps/`, ORM-only DB access,
the Drive query's `title` clause already correctly escaped, and the frontend consistently runs
dynamic content through `escapeHtml()`/`textContent` before touching `innerHTML` (confirmed by
reading every `innerHTML`/`insertAdjacentHTML` site in `frontend/js/`). `pip-audit` (run for real
against the exact pinned `uv.lock` versions, not guessed) found no known CVEs. This pass closes a
punch list of defense-in-depth/production-hardening gaps the audits found, not live bugs:

- **DRF's Browsable API is now disabled** (`REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]` pinned to
  `JSONRenderer` only, `config/settings.py`) — this is an internal, same-origin JSON API with a
  static-HTML frontend; the interactive HTML/schema UI DRF enables by default regardless of `DEBUG`
  was unnecessary attack surface for no benefit here.
- **`apps/services/google_client.py`'s `_escape()` now covers `parent_id`/`mime_type`, not just
  `title`.** Every current caller already passes env-configured constants (never anything derived
  from a synced file's own content) so this was never exploitable in practice — defense-in-depth
  only, closing the gap before a future caller could reintroduce it.
- **`pt_device` cookie's `secure` flag** (`apps/services/device_service.py`) now reads
  `settings.PT_DEVICE_COOKIE_SECURE` directly instead of `getattr(settings, ..., False)` — fails
  loudly (AttributeError) if that setting is ever removed, matching `pt_access`/`pt_refresh`'s own
  fail-closed style instead of silently degrading to an insecure cookie.
- **`SESSION_COOKIE_AGE` is now explicit** (30 minutes, `config/settings.py`) rather than relying on
  Django's 2-week default — this session only ever carries short-lived state (OAuth PKCE
  `code_verifier`, or `pending_user_id` during the 10-minute OTP window), not the main JWT-cookie
  auth path.
- **bcrypt cost is now pinned explicitly** (`rounds=12`) in `apps/api/routers/users_views.py`,
  `apps/core/management/commands/create_pt_user.py` — same value bcrypt's library default already
  used, just no longer implicit, so a future bcrypt version change can't silently alter it.
- **Stricter throttle scopes for higher-blast-radius writes**: `sync_trigger` (all 3 domestic
  plants + imports — each request queues a real background Drive-sync job) and
  `users_views.py`'s `create_user`/`update_user` no longer share the generic 200/min "user"
  bucket — see `apps/api/permissions.py`'s `SyncTriggerThrottle`/`AdminWriteThrottle` and
  `config/settings.py`'s `DEFAULT_THROTTLE_RATES["sync_trigger"]`/`["admin_write"]`.
- **Password policy tightened** (`apps/api/routers/users_views.py::_validate_password_strength`,
  mirrored in `create_pt_user.py`): minimum 10 characters (up from 8), rejects a purely-numeric
  password, rejects a password identical to the account's own email local-part. Deliberately
  modest — not a full breach-list/entropy policy, this is an admin-bootstrapped internal tool, not
  a public signup form.
- **Refresh-token rotation + revocation, real this time.** `SIMPLE_JWT["ROTATE_REFRESH_TOKENS"]`/
  `["BLACKLIST_AFTER_ROTATION"]` are now `True` (`config/settings.py`) — `PTTokenRefreshSerializer`
  (`apps/api/auth_serializers.py`) already had the rotate/blacklist branch, it was just dead code
  before this. **Do NOT enable `rest_framework_simplejwt.token_blacklist` in `INSTALLED_APPS` to
  back this** — confirmed the hard way, it crashes `device_verify` with `"OutstandingToken.user"
  must be a "User" instance` the moment a `PTUser` is passed to `RefreshToken.for_user()`, because
  that app's `OutstandingToken` model FKs to `AUTH_USER_MODEL` (Django's default `auth.User`),
  which this app deliberately never uses for real accounts (see this file's own note on why
  `AUTH_USER_MODEL` stays unchanged, above). Instead, `apps/core/models.py`'s `RevokedRefreshToken`
  is a small custom table keyed on `jti` alone — no user FK needed — written to by
  `apps/services/token_revocation.py`'s `revoke_refresh_jti()`/`is_refresh_jti_revoked()`. Wired
  into two places: `PTTokenRefreshSerializer.validate()` revokes the just-spent refresh token's
  `jti` on every successful rotation and rejects an already-revoked one, and
  `apps/api/routers/device_views.py`'s `logout_view` revokes the caller's current refresh token
  directly. `PTTokenRefreshView.post()` (`apps/api/auth_views.py`) now re-cookies the rotated
  refresh token via `set_refresh_cookie()` and strips the raw refresh string out of the JSON
  response body entirely (it only ever travels as the httpOnly `pt_refresh` cookie, same reasoning
  as the access token). **If you ever consider re-adding `rest_framework_simplejwt.token_blacklist`
  to `INSTALLED_APPS`, don't — re-read this note first,** it will reproduce the exact crash above.
- **Account lockout, on top of (not instead of) the existing `LoginRateThrottle`.** A rate limit
  alone slows down password guessing but never stops it, indefinitely, with no signal an account
  is under sustained attack. `PTUser.failed_login_attempts`/`locked_until` (migration `0020`) —
  `apps/api/auth_backend.py`'s `PTUserBackend.authenticate()` increments the counter on a wrong
  password and locks the account for 15 minutes once it reaches 5, resetting on a successful
  login. The lockout check runs before the bcrypt password check (same as the existing
  unknown-email branch) and burns an equivalent dummy-bcrypt delay via `_dummy_verify()` so a
  locked account isn't distinguishable-by-timing from a wrong-password attempt on an unlocked one
  — preserves this file's existing timing-safety property, doesn't introduce a new side channel.
- **Frontend defense-in-depth** (no functional change, tightens consistency — see the frontend
  security audit this pass started from): `import-po.js`'s Items table now runs `netPrice` through
  `formatInr()` like every other numeric cell, instead of interpolating it raw (it was never
  exploitable — a `DecimalField` always arrives as a JSON number, not attacker-controlled text —
  but the inconsistency was worth closing). `shared.js`'s `materialFieldsUrl()`/`dismissMatch()`
  and `material-modal.js`'s stock-trend fetch now `encodeURIComponent()` every URL segment
  (`lotId`/`matchType`/`matchId`), matching the already-encoded `poNumber`/`plantKey` call sites —
  these values are never attacker-influenced today (always backend-issued numeric ids or a small
  hardcoded enum), but a future change reusing this URL-building code without that guarantee won't
  silently regress. `index.html`'s jsdelivr-hosted Chart.js `<script>` tag now carries a
  Subresource-Integrity hash (`integrity="sha384-..."`, computed against the exact pinned
  `chart.js@4.5.0` build and cross-checked with two independent hash tools before writing it in) —
  a compromise of that specific CDN-hosted file would now be blocked by the browser rather than
  silently served and executed.
- **`script-src 'unsafe-inline'` removed (2026-09-05, second hardening pass, same day).** Originally
  scoped out as requiring a nonce/Django-template conversion for the 4 static pages — turned out to
  be unnecessary. Every inline `<script>` block was extracted to its own external file instead
  (`frontend/js/theme-init.js`, `login-theme-toggle.js`, `home-page.js`, `admin-page.js`,
  `search-po-page.js` — one straight extraction per page, no logic changes), and every inline
  event-handler attribute was converted to a real listener: the logo-fallback
  `onerror="this.style.display='none'"` pattern (6 sites across every page) is now a single
  capture-phase `error` listener in `theme-init.js` (loaded identically on all 5 pages, including
  `login.html` which loads none of `auth.js`/`shared.js`) keyed on a `data-hide-on-error` attribute;
  the modal close-button `onclick="closeModal()"` pattern (7 sites across `po-modal.js`/
  `import-po.js`/`material-modal.js`) is now one delegated click listener in `charts.js`. `'self'`
  already covers every extracted external file, so no nonce/hash machinery was needed at all —
  `config/security_headers.py`'s CSP now reads `script-src 'self' https://cdn.jsdelivr.net`, no
  `'unsafe-inline'`. Verified live in a real browser (not just read): every one of the 5 pages
  loaded with zero console errors and zero CSP violations both before and after the CSP change,
  and the extracted `login-theme-toggle.js` was click-tested end-to-end (dark mode toggled
  correctly). **`style-src` still has `'unsafe-inline'`** — dropping it would mean moving every
  dynamically-rendered inline `style="..."` attribute across `frontend/js/*.js` (used pervasively)
  into CSS classes, a much larger refactor left for a future pass, not silently forgotten.
  Tightening the `IsAuthenticated`-only read endpoints (documented in "Known gaps" below) and the
  data-accuracy/matching-threshold validation work remain separate, explicitly deferred efforts
  too — not silently resolved or forgotten by either hardening pass.

### Security hardening pass, round 2 (added 2026-09-05, same day as round 1 above)

Following an independent-security-analyst-style review of round 1's own results (a genuinely
different framing — residual business risk, not code-level controls in isolation — which landed on
a more conservative ~6.5-7/10 score, mainly for monitoring/alerting and second-factor strength), this
pass closed every item that was actually fixable in code:

- **Revoked-refresh-token pruning.** `RevokedRefreshToken` (round 1) had nothing that ever deleted a
  row — `apps/core/management/commands/prune_revoked_tokens.py` deletes rows past their own
  `expires_at`. Not wired to a scheduler (this app still has no cron/scheduled-task infrastructure —
  see "No scheduling" below) — run manually or via whatever scheduler eventually closes that gap.
- **"Log out everywhere."** `PTUser.token_version` (migration `0021`) is a per-account counter
  embedded as a `ver` claim in every JWT (`PTTokenObtainPairSerializer.get_token()`); both
  `PTJWTAuthentication.get_user()` (access tokens, checked on every request) and
  `PTTokenRefreshSerializer` (refresh tokens) reject any token whose `ver` doesn't match the user's
  current value. This is a stronger guarantee than `RevokedRefreshToken` alone: that table only
  knows about tokens explicitly rotated-away or logged out, not every token ever issued, and never
  covered a still-live access token. `apps/services/token_revocation.py`'s `revoke_all_sessions()`
  bumps the counter and deletes every `TrustedDevice` row for the account (a fresh sign-in anywhere
  goes through 2FA again). Wired to two endpoints:
  `POST /api/auth/logout-everywhere` (self-service, `IsAuthenticated`, any account can revoke its own
  sessions) and `POST /api/auth/users/<id>/logout-everywhere` (`IsAdmin`, for revoking a DIFFERENT
  account's sessions — e.g. a reported compromise). Verified with a real multi-client test
  (`test_logout_everywhere.py`) proving a second, already-logged-in client's live access token stops
  working the instant the first client revokes — not just that a future refresh would fail.
- **Security alerting** (`apps/services/security_alerts.py`) — this app previously had zero alerting
  on any security-relevant event, only passive log lines nobody was watching. Three new,
  best-effort/never-propagating email alerts, following the exact `render_email()`/`_dispatch_email()`
  pattern `device_service.py`'s existing admin-alert emails already use: (1) an account-locked alert
  the moment `PTUserBackend.authenticate()`'s lockout actually fires (not on every failed attempt);
  (2) a login-burst alert (cache-backed counter, 15 failed logins — across ANY accounts, including
  nonexistent emails — within a 5-minute window, suppressed to at most once per 30 minutes) for
  detecting a credential-stuffing-shaped attack that no single per-account throttle would catch; (3)
  a sync-pipeline-failure alert from `apps/services/sync_trigger.py`'s `_run_pipeline`/
  `_run_imports_pipeline`, so a failed Drive sync reaches an inbox instead of only `logs/app.log`.
- **Deploy-time system checks** (`apps/core/checks.py`, registered via `CoreConfig.ready()`) — run as
  part of `manage.py check --deploy --fail-level WARNING`, the same command CI already gates on.
  `check_jwt_signing_key_is_independent` warns (outside `DEBUG`) if `JWT_SIGNING_KEY` is still
  silently falling back to `SECRET_KEY`. `check_no_unexpected_django_superuser` warns if an
  `auth.User` superuser exists at all — this app's real login never touches `auth.User` (see the
  `AUTH_USER_MODEL` note earlier in this file), so a superuser account is an undocumented,
  unaudited path into `/admin/` that bypasses `PTAuditLog` for `PTUser` edits.
- **Dependabot** (`.github/dependabot.yml`) — continuous CVE monitoring for pip + GitHub Actions
  dependencies, on top of (not instead of) CI's own point-in-time `pip-audit` step. **7-day
  cooldown (added 2026-09-08, project owner: "open source library will be updated only after 7
  days of any new version")** — `cooldown.default-days: 7` on both ecosystems, so Dependabot won't
  propose a version bump until it's been out a week, giving the upstream community a window to
  catch an immediately-broken or malicious release first. A real CVE fix still lands within that
  same week, just not same-day.
- **Read-endpoint plant scoping** — see the "Role differentiation..." bullet just above this
  section; this was the one item this pass touched that could plausibly change real user-visible
  behavior, so it's documented there in full rather than duplicated here.
- **TOTP (authenticator-app) second factor — built, then fully removed same-day at the project
  owner's request ("Don't need TOTP, happy with device aware").** Briefly existed as a complete,
  tested, opt-in alternative to email OTP (`apps/services/totp_service.py`'s from-scratch RFC 6238
  implementation, `apps/api/routers/totp_views.py`'s setup/confirm/disable/verify endpoints, a
  `PTUser.totp_enabled` login-flow branch, `login.js`'s TOTP code-entry step) but was never wired
  into any UI, so no real account ever had it enabled. Fully reverted, not just left dormant: the
  `totp_service`/`totp_views`/`totp_urls` modules and their tests are deleted;
  `PTUser.totp_secret`/`totp_enabled`/`backup_codes` were removed from the model before the
  migration adding them was ever applied anywhere but this one local dev DB (migration `0021` was
  unapplied and regenerated clean, keeping only `token_version` — see "Log out everywhere" above,
  which is unrelated and stayed). `login.js` and `auth_serializers.py`/`device_views.py` are back
  to their pre-TOTP shape. If TOTP is wanted again later, it needs to be rebuilt from scratch - no
  code or migration history from this attempt remains to build on.

**Bootstrap**: without at least one `PTUser`, nobody can log in to create more via Django Admin —
use `manage.py create_pt_user --role admin` (see Commands above) to create the first account. Only
an email ending in `@<ALLOWED_EMAIL_DOMAIN>` (default `ravasco.com`) may ever have an account or
log in — enforced in `apps/api/permissions.py::is_allowed_email_domain()`, checked at login,
Google OAuth, and account creation.

**New required `.env` entries this pass added** (see `.env.example` for the full list with
comments): `SMTP_HOST`/`PORT`/`USER`/`PASS`/`FROM` (OTP + notification emails — blank is fine in
local dev, `DEBUG=True` falls back to printing the OTP to the console), `JWT_SIGNING_KEY`
(optional, defaults to `DJANGO_SECRET_KEY`), `ALLOWED_EMAIL_DOMAIN` (optional, defaults to
`ravasco.com`). `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` are a
**separate concern from `GOOGLE_SERVICE_ACCOUNT_JSON`/`FILE`** — the former is a human-login OAuth
client, the latter is the unattended Drive-sync service account; do not conflate them, one cannot
do the other's job.

**Audit log and server logs** (added 2026-09-03, ported from the TDS app the same way the rest of
this section was): `apps/core/audit_log.py` defines `PTAuditLog` (`pt_audit_log` table) and
`log_pt_action(request, action, detail='', actor=None)`. Unlike TDS's audit log, this app is
read-only (no create/approve/decline/delete workflow), so the action set is deliberately scoped to
just `ACTION_LOGIN`/`ACTION_LOGOUT` — **don't add more action types speculatively; add one only
when a real mutating endpoint exists to log.** Wired into all three login paths (trusted-device
fast path in `PTLoginView.post`, new-device email-OTP verify in `device_verify`, Google OAuth
trusted-device path in `google_callback`) and into `logout_view` — all in
`apps/api/{auth_views,routers/device_views,routers/google_oauth_views}.py`. Browsable read-only in
Django Admin (`PTAuditLogAdmin` in `apps/core/admin.py` — add/change/delete permissions all
disabled, matching the append-only intent). `log_pt_action()` never raises: a broken audit write
must not block a real login/logout, same reasoning as TDS's `log_tds_action()`.

Separately, `config/settings.py`'s `LOGGING` dict now writes every `INFO`+ log line (Django's own
plus every `apps.*` logger — parsers, matching, auth, sync commands) to a rotating file at
`logs/app.log` (10MB × 5 backups), on top of the existing console output. `logs/` is gitignored.
This is unrelated to the audit table above: `logs/app.log` is an operational trace (what the server
did, all requests/errors/warnings — e.g. `[WARNING] ... Unauthorized: /api/purchase-orders`);
`pt_audit_log` is a security-relevant, permanent record of who logged in/out and from where. Don't
conflate the two or assume one substitutes for the other.

## Frontend pages and the shared top nav (added 2026-09-03)

There are now 4 protected frontend pages, all sharing one look modeled on the TDS Automation App's
own top nav (gold/navy `css/brand.css`, ported earlier for login.html/home.html only — now used for
the shared header on every page):

- **`index.html`** (`/`) — the real PO↔MIR↔Stock reconciliation dashboard (`js/main.js`). Its own
  content below the shared nav still uses `css/style.css`'s separate navy/blue/red palette — both
  stylesheets are loaded together (brand.css first, for `.topnav`/`.nav-tabs`/`.nav-user` only;
  style.css owns everything else). Don't merge the two palettes; that separation is deliberate (see
  brand.css's own header comment).
- **`home.html`** — landing page shown right after login. Real KPI row (Total PO's, Suppliers, This
  Month, This Week), computed client-side from all 3 plants' live PO data fetched in parallel on
  load (`loadKpis()`) — not placeholders, and a plant that fails to load degrades the count with a
  visible warning rather than silently reading as zero.
- **`search-po.html`** (new) — look up a PO by number across all 3 plants at once (substring match,
  case-insensitive, ≥2 characters). Deliberately self-contained: fetches/caches its own copy of each
  plant's PO list rather than depending on `js/main.js`'s cache or its modal, and its own detail
  panel is a simpler view (no match-confidence/flag-severity styling) — a full-parity detail view
  already exists at `/`, this page links there. See the file's own header comment before assuming
  this is an oversight.
- **`admin.html`** (new, real in-app user management as of 2026-09-03 — see below) — admin-only.
  Sync status for all 3 plants + a real Users panel: create/edit/activate/deactivate, not links out
  to Django Admin (the first version of this page linked to `/admin/core/ptuser/` etc. — the user
  explicitly rejected that: *"don't django admin, use our builded as we did in tds_app"* — replaced
  in the same pass, no Django Admin links remain on this page). Non-admin visitors get a plain
  access-denied panel (defense in depth — the Admin nav tab and home.html's Admin Panel quick-action
  card are already hidden for them via role-gating, this isn't the primary gate; the 3 endpoints
  below enforcing `IsAdmin` server-side is).

**In-app user management** (`apps/api/routers/users_views.py` + `users_urls.py`, ported from the TDS
Automation App's own `users_views.py` — same design, ported deliberately after the Django-Admin-links
version above was explicitly rejected):
- `GET /api/auth/users` — list (moved here from `auth_views.py`, was already `IsAdmin`-gated).
- `POST /api/auth/users/create` — create a user. Body: `email`, `password` (≥8 chars, bcrypt-hashed
  server-side via `bcrypt.hashpw(..., bcrypt.gensalt())` — same call shape as `manage.py
  create_pt_user`), `fullName`, `designation`, `role`. Validates the email domain
  (`is_allowed_email_domain()`) and rejects a duplicate email with a real `409`, not a stack trace.
- `PATCH /api/auth/users/<id>` — update `role`/`isActive`/`fullName`/`designation`/`plants`/
  `password`. **Superseded (2026-09-04): this endpoint now accepts an optional `password` field**
  (see "In-app password reset" in the roadmap section below) — the "no password field, reset only
  via `manage.py create_pt_user`" claim that used to be here was TDS's own real gap, carried over
  deliberately at the time; it no longer applies to this app. `manage.py create_pt_user` still
  remains the only way to create the very first account, before any admin exists to use this panel
  at all.
- URL naming deliberately differs from TDS's own `users_urls.py`: TDS disambiguates GET `/users` vs
  POST `/users/` by trailing slash alone (two `path()` entries, same string except for that slash) —
  fragile and easy to break by accident. This app uses distinct path segments instead
  (`/auth/users` for GET, `/auth/users/create` for POST) — same capability, clearer routing, not a
  functional deviation from the pattern being ported.

**`js/shared.js`** (loaded on every protected page right after `auth.js`, before that page's own
script) holds what would otherwise be 4 duplicate copies: the `PLANTS`/`PLANT_KEYS` map, the
authenticated `apiForPlant()` fetch wrapper (401 → bounce to `/login.html`, same everywhere), and
`escapeHtml`/`formatInr`/`formatDateIN`. `js/main.js` no longer defines these itself — if you're
looking for them and don't find them in main.js, they moved here, they weren't deleted.

**`js/auth.js`'s `renderNavTabs(container, activePage)`** renders the 4 shared nav tabs: **Home**
(`/home.html`) → **Dashboard** (`/`, the real reconciliation UI — a distinct destination from Home,
not an alias of it) → **Search PO** (`/search-po.html`) → **Admin** (`/admin.html`, only for
`role === 'admin'` — hidden, not shown-then-denied). Each page marks its own tab active (`home.html`
→ `'home'`, `/` → `'dashboard'`, etc.) — there is no page that maps to two tabs at once.

**`js/auth.js`'s `renderUserBadge(container)`** renders a circle-avatar user menu (initials, role-
colored background — admin=gold, editor=blue, viewer=navy — same color coding as `admin.html`'s
role pills), stacked name/role text, and a click-to-open dropdown containing Logout — same visual
pattern as the TDS Automation App's own top nav, replacing the earlier plain-text "Name · role +
Logout button" version.

**Sync status labels renamed**: what was "PO CSV" / "MIR" / "Stock" in the dashboard's sync badges
and `admin.html`'s per-plant sync cards is now **"PO Updated" / "MIR" / "RM"** — display labels
only, the underlying `sync` dict keys (`po_csv`/`mir`/`stock`) and `SyncRun.source` values are
unchanged; don't go looking for a `source='rm'` value that doesn't exist.

### Per-plant models, not a shared schema — this is deliberate, don't "fix" it

`apps/core/models.py` has fully separate model classes per plant (`HRSPurchaseOrder` /
`RTPAchhadPurchaseOrder`, `HRSMIREntry` / `RTPAchhadMIREntry`, etc.) rather than one shared schema
with a `plant` discriminator column. This was an explicit instruction, re-confirmed by inspecting
the real files: **each plant's MIR/Stock spreadsheets have genuinely different column layouts**,
not just different values in the same columns —

- HRS's MIR sheet (`'RAW MATERIAL'` tab) has 4 PO-related columns (Purchase Order No./Date + SAP
  P.O. No./Date) and a SAP GRN Number column; Achhad's MIR sheet (`'R.M. '` tab — note the
  trailing space) has exactly one PO-number/PO-date pair and no GRN column at all.
- HRS's Stock file is one row per **(material, vendor) lot** — the same material can appear many
  times from different vendors at different rates, and `HRSStockLot.party_name` captures that.
  Achhad's Stock file is one row per **material, full stop** — no vendor column exists on that
  sheet at all (`RTPAchhadStockLot` has no `party_name` field). This is a real, structural
  difference: Achhad's `MirStockMatch` can only gate on material description, which is a
  materially weaker guarantee than HRS's (material, vendor) gate — see
  `apps/services/matching_achhad.py`'s module docstring.
- HRS's Stock sheet keeps a fixed tab name (`'Stock'`); Achhad's Stock file's single tab is
  renamed every month (e.g. `'Aug 26-27'`) — `apps/services/parsers/achhad_stock.py` reads
  `wb.sheetnames[0]` instead of matching a literal name. Vapi's Stock sheet is back to a fixed
  `'Stock'` name (same as HRS's, purely coincidentally), but its own header sits one row lower
  (row 6 vs. HRS's row 6 — actually the same row number, but preceded by a 5-row document-control
  title block Vapi's sheet has that HRS's doesn't structure the same way; see
  `apps/services/parsers/vapi_stock.py`'s docstring for the exact layout).
- Vapi's MIR file is the most structurally different of the three: no `Net`/discount columns at
  all (goes straight from `RATE` to `TAXABLE VALUE`), GST split into one overall rate column plus
  three amount-only IGST/CGST/SGST columns (no per-component rate columns the way HRS/Achhad
  have), a single TCS amount instead of a rate+amount pair, and — critically — its own PO-number
  field (`SAP P.O`) was **100% blank** across every one of ~1,330 real rows checked, worse than
  HRS's ~30% blank rate. Vapi's PO↔MIR matching currently has zero usable Tier-1 shortcut and
  runs entirely on the weighted score. Also: Vapi's GST rate column stores a **whole percentage
  number** (`18.00` = 18%), not a fraction like HRS/Achhad's `0.18` — confirmed by cross-checking
  `Taxable Value × GST% ÷ 100 = IGST` exactly against a real row. `RTPVapiMIREntry.gst_rate_pct`
  is `decimal_places=2` for this reason, not HRS/Achhad's `decimal_places=3`-for-a-fraction
  convention — don't "fix" it to match the other two plants, they're genuinely different units.
- Vapi's Stock file is a real shared **multi-plant ledger**, not exclusively Vapi's own stock —
  confirmed real `PLANT` column values in the live file: `RTP-1` (145 rows), `HRS` (20 rows),
  `RTP-2` (3 rows). `RTPVapiStockLot.plant_tag` captures this without filtering, the same
  design HRS's own `location_tag` field already uses for its own file's cross-location rows.
  Unlike Achhad's Stock sheet, Vapi's Stock sheet **does** have a real vendor column (`Supplier
  Name`) — confirmed genuine (not just an echo of `PLANT`: only 16 of 168 rows have Supplier
  Name equal to their own Plant value) — so `RTPVapiMirStockMatch` uses HRS's stronger
  (material, vendor) gate, not Achhad's material-only one.

Forcing all three plants into one shared table would mean a pile of always-null columns for
whichever plant doesn't have that field, and — worse — would tempt writing matching logic that
silently assumes a field exists uniformly across plants when it doesn't. Each plant gets its own
model set, parser modules, matching module (`matching.py` / `matching_achhad.py` /
`matching_vapi.py`), API router (`hrs_views.py` / `achhad_views.py` / `vapi_views.py`), and sync
management commands instead. `SyncRun` is the one shared table — it's a generic log with a
`plant` field, not a plant-shaped data table.

### Domestic router de-duplication (added 2026-09-05) — status: all three plants migrated

The per-plant-model decision above is real and correct, but it does **not** mean the three
domestic routers' *view/HTTP-layer* code should also be duplicated — that part has nothing to do
with the genuine per-plant schema differences and was previously copy-pasted near-verbatim across
`hrs_views.py`/`vapi_views.py`/`achhad_views.py` (confirmed byte-for-byte identical except which
model classes to query, which plant's `matching*.py` module to call, and a handful of real schema
differences: Achhad's Stock lot has `rate`/`msl` instead of `basic_rate`/`sub_category`/`uom` and
no vendor column at all). That HTTP-layer logic now lives once in
`apps/api/routers/_domestic_base.py` — a `_PlantConfig` dataclass (model classes, `run_full_match`
reference, plant key, material-field allow-lists, and the three lot-attribute names that
genuinely vary: `lot_rate_field`/`lot_code_field`/`lot_vendor_field`) plus `make_*` factory
functions (`make_purchase_orders`, `make_correct_field`, `make_materials`,
`make_correct_material_field`, `make_stock_trend`, `make_sync_status`, `make_sync_trigger`,
`make_dismiss_po_mir_match`, `make_dismiss_mir_stock_match`, `make_dismiss_flag`) that each plant's
own router file calls once at import time and re-exports under the original function name.
**`urls.py` and every endpoint's URL/response shape are unchanged** — only where the code
physically lives changed, done as a strangler-fig migration (one plant file rewritten at a time,
each verified against that plant's own test suite before moving to the next) specifically because
`vapi_views.py`/`achhad_views.py` had no direct test coverage of their own before this pass (only
HRS did — see "Test coverage is split into two deliberately different scopes" above) - characterization
tests for Vapi/Achhad (`test_vapi_correct_field.py`, `test_vapi_dismiss_match.py`,
`test_vapi_dismiss_flag.py`, `test_achhad_correct_field.py`, `test_achhad_dismiss_match.py`,
`test_achhad_dismiss_flag.py`, plus `TestVapiCorrectMaterialField`/extended
`TestAchhadCorrectMaterialField` in `test_material_correct_field.py`) were written against the
unmodified originals first, specifically to prove behavioral equivalence before/after each
plant's migration. **All three plants are migrated as of this pass** — `hrs_views.py`, `vapi_views.py`, and
`achhad_views.py` each now just build their own `_CONFIG` and re-export the ten view functions;
none of them has its own private `_serialize`/`_coerce_value`/`_lot_dict`/etc. helpers anymore
(confirmed by grep — zero matches for those names across all three files). Achhad's genuine
schema divergence (`rate`/`msl` instead of `basic_rate`/`sub_category`/`uom`, no vendor field,
`sap_code` instead of `sap_item_code`/`hsn_code`) is handled entirely through `_PlantConfig`
values, not through any per-plant branching inside `_domestic_base.py` itself. `imports_views.py` is intentionally not part of this migration (it already has
its own, different shared-config pattern via its `_PLANTS` dict). Its own `_field_warning`/
`_serialize` now import from `_domestic_base` too (byte-for-byte identical, field-set-independent
— safe to share); its `_coerce_value` stays its own local copy, deliberately **not** shared,
because `imports_views.py`'s own `_DATE_FIELDS`/`_DECIMAL_FIELDS` are a strict superset of the
domestic routers' (BOE/exchange-rate fields the domestic plants don't have) and
`_domestic_base._coerce_value` reads its own module-level field sets — sharing it as-is would
silently reject valid import-only fields. Parametrizing `_coerce_value` to accept the field sets
as arguments would fix this but is a further design change, not attempted in this pass.

**The PO master CSV format is identical across HRS, Achhad, and Vapi** (confirmed against real
files, byte-for-byte identical header) — `apps/services/parsers/po_csv.py` is reused as-is for all
three plants; only the target model class and the Drive file title differ per plant's
`sync_*_po_csv` command.

### Two Drive folders per plant, not one — check `.env.example` before assuming a shared folder

Real Drive structure (confirmed by inspecting live file parent IDs, not assumed): every plant's PO
master CSV lives together in one shared folder (`PURCHASE_TRACKER_DB_FOLDER_ID`), while each
plant's own live MIR and Stock xlsx files live in their own **separate** folder per plant
(`HRS_MIR_STOCK_FOLDER_ID` / `ACHHAD_MIR_STOCK_FOLDER_ID` / `VAPI_MIR_STOCK_FOLDER_ID` — three
distinct folder IDs, confirmed by inspecting each). `google_client.find_file_id_by_title()` takes
an explicit
`parent_id` — every sync command passes the correct one for what it's fetching. Do not
consolidate these into one folder ID; sharing the common parent folder ("Purchase HO") as Viewer
with the service account covers all of them since Drive permissions cascade to subfolders, but
the *code* still needs to search the right subfolder for each file.

### Drive API v3, not v2 — a real bug already hit and fixed

`apps/services/google_client.py`'s search query must use `name = '...'` (Drive API v3 field name), not
`title = '...'` (the old v2 field name) — using `title` returns an opaque `HTTP 400 Invalid Value`
with no indication of what's wrong. Confirmed hitting this in practice; fixed. If a Drive search
ever starts failing with "Invalid Value" again, check this first.

### `.env` gotchas that have already bitten this project — read before debugging Drive/DB failures

- **Never paste multi-line JSON directly into `GOOGLE_SERVICE_ACCOUNT_JSON=`.** `.env` files
  parse one `KEY=VALUE` per line; a multi-line paste breaks every line after it (symptom:
  `python-dotenv could not parse statement starting at line N` repeated for many lines, then
  `json.loads()` fails with something like `Expecting property name enclosed in double quotes:
  line 1 column 2` because the captured value is just the opening `{`). Put the full JSON as one
  line if using this var, or better, use `GOOGLE_SERVICE_ACCOUNT_FILE` with a path instead.
- **Never quote a `GOOGLE_SERVICE_ACCOUNT_FILE` Windows path with double quotes.**
  `python-dotenv` applies backslash-escape processing inside double-quoted values (like a
  JSON/shell string) — a path containing `\r` as part of a longer word (e.g.
  `...\secrets\ravasco-...`) gets that `\r` silently read as a carriage-return escape and
  corrupted, breaking the file open with a cryptic `[Errno 22] Invalid argument`. Leave the path
  unquoted; Windows paths under a user's home directory essentially never contain spaces, so
  quoting was never necessary anyway. Confirmed hitting this in practice.
- Both of the above are now called out directly in `.env.example`'s comments — read them before
  re-deriving this from scratch if a sync command fails immediately with a parsing/credentials
  error.
- **This repo's working directory can itself be inside a OneDrive-synced folder** (confirmed true
  for at least one developer's setup: `.../OneDrive/Desktop/Purchase Automation App/...`). `.env`
  is gitignored (confirmed — `git log -- .env` shows no history), so it's never at risk of landing
  in the repo's own history, but OneDrive sync operates independently of git and doesn't respect
  `.gitignore` at all — a real secret sitting in `.env` can still get synced to Microsoft's cloud
  and any other device signed into that same OneDrive account, which is a meaningfully different
  (and easy to overlook) exposure path from "is it committed." **Fix (documentation only, no
  secret rotation performed as part of this note)**: exclude this project's folder from OneDrive
  sync (Windows: OneDrive Settings → Account → "Choose folders", uncheck this folder — or move the
  repo outside the OneDrive-synced tree entirely if that's feasible later). If you're setting this
  project up fresh inside a OneDrive-synced location, do this before populating a real `.env`, not
  after.
- **Docker's `PGHOST`/`DATABASE_URL` note**: see "Docker (local dev)" below — this is not a `.env`
  gotcha in the same sense as the two above (nothing about `.env` itself needs to change for
  Docker), but it's easy to assume `PGHOST=localhost` in `.env` needs editing for
  `docker compose up` to work, and it doesn't — `docker-compose.yml` sets `DATABASE_URL` itself,
  pointed at its own `db` service.

### Change-detection must compare quantized Decimal values, rounded the way Postgres actually rounds

`apps/services/sync_utils.py`'s `unchanged(model_cls, existing, parsed, fields)` is the shared
helper every `sync_*` command uses to decide whether to skip re-writing an unchanged row. Getting
this right took three attempts, each disproven by real data from a different plant — if you touch
this function, re-run the idempotency check below across all three plants before trusting a fix:

1. A `hashlib.sha256("|".join(str(f) for f in fields))` comparison between the parsed value and
   the DB-stored value fails almost immediately — a `DecimalField` gets requantized to its
   declared `decimal_places` somewhere between Python and storage (e.g. a parsed `Decimal('950')`
   comes back from the DB as `Decimal('950.000')`), so the string reprs never match even though
   the values are equal.
2. Direct `==` comparison between `Decimal` values at different precision looks equal in Python,
   but a naive per-field `getattr(existing, f) == getattr(parsed, f)` without quantizing first
   still breaks because a currency amount computed with more precision than the DB column holds
   (e.g. sheet computes `cgst_amt` as `8633.625`, but the column is `decimal_places=2`) gets
   rounded on save, making every re-sync of that row look "changed" forever since the freshly
   parsed value is never rounded until compared.
3. Quantizing both sides before comparing is the right idea, but the **rounding mode matters**
   and the obvious choice is wrong: `unchanged()` originally used `ROUND_HALF_EVEN` (Python
   decimal's default), on the assumption that Django rounds via `format_number()` before sending
   a value to the DB. That assumption was wrong for this project's actual Postgres/psycopg3
   setup — confirmed directly with `SELECT CAST('353057.445' AS numeric(16,2))`, which returns
   `353057.45`, not `format_number()`'s `353057.44`. Postgres receives the full-precision Decimal
   from psycopg3 and does its **own** rounding on cast into a `numeric(p,s)` column, using
   standard round-half-away-from-zero, not banker's rounding. `ROUND_HALF_EVEN` here caused two
   real Vapi MIR rows (values landing exactly on a `X.XX5` boundary — `353057.445`, `84582.225`)
   to report as "changed" on every single re-sync, never converging to 0, because the Python-side
   quantization disagreed with what was actually sitting in the DB. Switched to `ROUND_HALF_UP`,
   confirmed this converges to 0 on repeated re-syncs for all three plants with no regression.

`unchanged()` also coerces plain `int`/`float` values through `Decimal(str(x))` first, since a
parser's `to_decimal(...) or 0` fallback can hand back a literal `0` (an `int`) where the DB
always stores a `Decimal`.

**If you add a new sync command for a new data source, use `sync_utils.unchanged()` — do not
re-derive a hash-based or naive-equality comparison, and do not "fix" the rounding mode back to
`ROUND_HALF_EVEN` without re-testing against real data landing on an exact `X.XX5` boundary; it
looks like the more "correct" Python default and is wrong for this project's actual DB behavior.**

**Idempotency check** (run after touching this function or any sync command): run every plant's
`sync_*` commands twice in a row against real Drive data; the second run must report `0
created/updated` for every source. A steady-state nonzero count on back-to-back runs (seconds
apart, not enough time for the live file to genuinely change again) means something isn't
converging — find the exact diff with a script like:
```python
from apps.core.models import <Model>
from apps.services.parsers.<parser_module> import <parse_function>
from apps.services.sync_utils import unchanged
from apps.services.google_client import download_file_bytes
entries = <parse_function>(download_file_bytes("<drive-file-id>"))
for p in entries:
    e = <Model>.objects.filter(source_row_ref=p.source_row_ref).first()
    if e and not unchanged(<Model>, e, p, _FIELDS):
        print(p.source_row_ref, [(f, getattr(e, f), getattr(p, f)) for f in _FIELDS if getattr(e, f) != getattr(p, f)])
```

### GST rate fields need 3 decimal places, not 2 — a real precision bug, already fixed (but check the unit per plant)

Every `*_rate_pct` field on `HRSMIREntry` / `RTPAchhadMIREntry` (`discount_rate_pct`,
`tax_rate_pct`, `cgst_rate_pct`, `sgst_rate_pct`, `tcs_rate_pct`) is `decimal_places=3`, not the
more obviously-sufficient-looking `2`. The source sheets store these as a **fraction**, not a
percentage — a 2.5% GST rate is the literal cell value `0.025`, and 2 decimal places silently
truncates that to `0.02` or `0.03` on save. Confirmed against a real row this session (CGST/SGST
both `0.025` in the live MIR sheet).

**This does not generalize to every plant — check the actual unit before copying the convention.**
`RTPVapiMIREntry.gst_rate_pct` is deliberately `decimal_places=2`, because Vapi's MIR sheet stores
this as a whole percentage number (`18.00` = 18%), not a fraction — confirmed by cross-checking
`Taxable Value × GST% ÷ 100 = IGST` exactly against a real row. If you add a new rate-like field
from any of these sheets, check whether the source stores a fraction or a whole percentage before
picking `decimal_places`; don't assume it matches whichever other plant you looked at last.

### `*_diff_pct` columns need a clamp — DecimalField(max_digits=6, decimal_places=2) has a ceiling

`HRSPOMirMatch.qty_diff_pct` / `rate_diff_pct` / `value_diff_pct` (and the Achhad/Vapi
equivalents) can hold at most `9999.99`. A pathological pair — a tiny reference value against a
much larger actual value (e.g. a rate typo'd as a fraction of the real one) — can produce a
percentage difference far beyond that, which would raise a DB error on insert rather than just
recording "very large diff, flag it". `apps/services/matching.py`, `matching_achhad.py`, and
`matching_vapi.py`'s `_diff_pct()` all clamp to `_MAX_DIFF_PCT = Decimal("9999.99")` for exactly
this reason. If you touch any matching module, keep the clamp — it was found and fixed in the
Achhad matcher first, then ported back into the HRS matcher for consistency, and Vapi's matcher
was written with it from the start.

## PO↔MIR↔Stock matching

Implemented per-plant in `apps/services/matching.py` (HRS), `apps/services/matching_achhad.py`
(RTP-Achhad), and `apps/services/matching_vapi.py` (RTP-Vapi), all three with the identical scoring
approach:

- **Vendor name is always a hard gate, never a scored factor.** Two records for different
  vendors are never candidates for each other, however well material/qty/rate/value line up.
  Vendor names are normalized (`apps/services/parsers/common.py`'s `normalize_vendor()` — strips
  legal suffixes like "Pvt Ltd", lowercases, strips punctuation) and then compared with
  **containment**, not exact equality — HRS's Stock sheet appends a city suffix to its
  `party_name` that MIR/PO data doesn't carry (`"Rubamin Private Limited"` in MIR vs `"Rubamin
  Private Limited - Vadodara"` in Stock), so exact-match vendor gating produced **zero**
  MIR↔Stock matches until switched to `shorter in longer` containment (see `_vendor_matches()`
  in all three matching modules).
- **PO↔MIR**, per line item: Tier 1 is an exact/substring `po_number_raw` match (a free shortcut
  when MIR's own PO-number field happens to be populated and valid — it's unreliable on real data
  for every plant checked so far: ~30% blank on HRS, and **100% blank** on Vapi across every row
  checked, so it's never the *only* path — Vapi's matching currently runs entirely on Tier 2).
  Tier 2 is a weighted score among vendor-gated candidates: material description token overlap
  30%, qty closeness 20%, rate closeness 20%, pre-tax value closeness 30%. Below
  `MATCH_THRESHOLD = 0.55`, a line item is left unmatched rather than forced onto a poor
  candidate.
- **The value comparison must use pre-tax figures on both sides.** PO's `net_value` is pre-tax;
  the correct MIR-side comparison is `taxable_value` (also pre-tax — used directly for Vapi,
  which has no separate `net` field to fall back to the way HRS/Achhad do), not
  `invoice_final_value`/`total_amount` (both post-GST/TCS). Comparing pre-tax to post-tax produced
  a bogus ~18% "value discrepancy" on line items that matched exactly on qty and rate — 18% being
  roughly the GST rate on many of these materials, not a real discrepancy. Confirmed and fixed
  this session.
- **MIR↔Stock** gates differently per plant, reflecting the real schema difference described
  above: HRS and Vapi both gate on (material description, vendor) together — HRS via
  `HRSStockLot.party_name`, Vapi via `RTPVapiStockLot.supplier_name` (confirmed genuine, not a
  duplicate of its `PLANT` column). Achhad gates on material description alone since its Stock
  sheet has no vendor column at all, which is explicitly documented as a materially
  weaker/more false-positive-prone match, not treated as equivalent to HRS's/Vapi's guarantee.
  No plant compares stock quantity in this pairing — HRS's `received` field (the sheet's "REC"
  formula column) reads `0` for nearly every real lot, evidently clearing once allocated rather
  than holding a running total comparable to one MIR line's qty; only rate is compared for every
  plant's MIR↔Stock pairing.

All three matching modules expose `run_full_match()`, wired to `match_hrs` / `match_achhad` /
`match_vapi` management commands — safe to re-run any time (idempotent upserts via
`update_or_create`), intended to run after each plant's three sync commands.

## Match accuracy: manual validation is required, not optional

There is no automated precision/recall measurement for either matcher yet - `MATCH_THRESHOLD = 0.55`
was picked, not measured against labeled ground truth (`FLAG_DIFF_PCT` was also picked rather than
measured when it was `5.00`; as of 2026-09-04 it's `0` - zero tolerance - a deliberate policy choice,
not a measured value at all, see "Artifact-parity decisions" below for the full reasoning). Real numbers from
a full sync against live Drive data this session: HRS 68.6% of PO line items matched to MIR (68
tier-1/13 tier-2), Achhad 86.0% (71/3), Vapi 50.5% (0/48, since Vapi's `po_number_raw` is 0% populated
on live data - every Vapi match runs on the weighted score alone). MIR↔Stock match rates are much
lower (1.6-24%) but that's mostly structural, not a matcher failure: Stock is a current-snapshot
table (one row per live lot), MIR is a full historical log, so most older MIR rows correctly have no
current stock lot to match against.

**Until an audit/sample-based accuracy check exists, treat every match as a suggestion, not a fact** -
this is a process instruction, not just a UI note (the dashboard's `.validation-note` banner and each
line item's confidence badge exist to prompt this, but the actual verification has to be a human
decision). Do not wire any downstream action (auto-approving a PO, auto-updating stock) off a match
without a human in the loop first.

To help a reviewer triage instead of trusting every checkmark equally, `apps/api/routers/*_views.py`'s
`_line_item_dict()` exposes `matchScore`, `qtyDiffPct`, `rateDiffPct`, and `valueDiffPct` per line item
(previously only a single blended `matchFlagged` boolean was exposed, even though the match models
always stored the three diff percentages separately). `frontend/js/main.js`'s `matchStatusHtml()`
turns that into a confidence badge (high = exact PO-number match, medium = weighted score ≥0.75, low =
below that) plus up to three separate flag badges (qty/rate/value) instead of one ambiguous "review"
badge - a partial delivery (qty differs, rate/value in line) no longer reads identically to a real
price discrepancy. The value badge is deliberately suppressed when qty is already flagged, since
value ≈ qty × rate and a value gap fully explained by a partial-delivery qty gap isn't a second,
independent problem worth its own badge.

**Getting factory/plant staff to reliably fill in MIR's own PO-number field was considered and
rejected as a lever** - not a technical fix, a training/process one, and out of scope for this app to
solve. Vapi in particular will likely keep running on the weighted score alone indefinitely; don't
assume Tier-1 coverage will improve on its own.

## Days-Left Engine (added 2026-09-05)

Per-material consumption rate and days-of-cover, added to the Raw Material Analysis view. Built
from a project-owner-authored build plan ("Document 02 of 03: Days-Left Engine") which itself
depended on a prerequisite "Document 01: stable lot identity" that **does not exist in this repo**
(confirmed by a full search of code/docs/git history before starting - no such document or prior
work exists). Lot identity today is genuinely not stable: every `*StockLot` model is uniquely
keyed on `source_row_ref` (a row-position reference into the synced spreadsheet), and
`is_active`'s own help_text already documents that a row shift in the source sheet can flip a lot
inactive and create a "new" lot row for the same real-world material, silently resetting its
snapshot history. Built anyway, on the project owner's explicit decision, relying on the
confidence-band mechanism below as the mitigation rather than waiting on an unstarted lot-identity
fix - a lot whose history keeps resetting will simply sit at LOW/NONE confidence rather than
reporting a wrong number with false certainty.

**Why this can't use `issued`/`received`**: those columns mean a different thing at each plant's
Stock sheet - RTP-Achhad's `issued` is a period-to-date summary that resets each period; HRS's/
RTP-Vapi's `issued`/`received` are formula cells pulled from a separate Receipt/Issue tab, and
HRS's own `received` is already documented elsewhere in this file as reading 0 for nearly every
real lot. The one field that means the same thing everywhere is the closing balance,
`todays_stock` - so the portable signal is the day-over-day drawdown between consecutive
snapshots of it, not either of those columns.

- **`apps/services/stock_consumption.py`** - the pure algorithm (`consumption_stats()`), tested
  dependency-free in `apps/services/tests/test_stock_consumption.py` (no DB), same convention as
  `matching.py`/`test_matching.py`. Skips any consecutive-snapshot gap over 7 days (averaging
  across a long hole would invent a drawdown that never happened) and excludes any interval where
  stock rose (a receipt landed) entirely rather than counting it as zero consumption, which would
  drag the average down and overstate days-left - the exact direction of error that hides a real
  reorder need. Confidence travels in the payload as `high`/`medium`/`low`/`none` (based on days
  spanned + usable intervals), rendered rather than used as a hidden filter - the whole point is to
  show a thin-history figure while being honest about how thin it is. `issued` still feeds a
  secondary cross-check (`estimatesAgree`) where it happens to behave cumulatively, but never
  overrides the primary drawdown-based number.
- **`apps/api/routers/_domestic_base.py`** - `make_materials()`'s `_consumption_by_lot()` fetches
  every active lot's window of snapshots in one query (`values_list` + `itertools.groupby`), not
  one query per lot - regression-tested in `apps/api/tests/test_materials_days_left.py` via
  `django_assert_num_queries(1)`. `_lot_dict()` exposes the result as a `consumption` block per
  lot, plus `daysToMsl` (Achhad's `msl` column only - HRS/Vapi have no equivalent field at all,
  same `getattr(lot, "msl", None)` no-branching pattern already used for `no_of_days`/
  `sub_category`). `daysToMsl` is `0` the instant current stock is at/below msl, independent of
  whether a consumption rate is even known yet (already-below-reorder-point shouldn't wait on 30
  days of history to surface); otherwise an ETA once `avgDaily` is available, else `None`.
- **`frontend/js/materials.js`** - a "Days Left" column (with a green/amber/grey confidence dot,
  tooltipped with the history it's based on) in both the "View all" `<table>` and the top-5
  `.top5-row` grid preview, plus `matFilterCells`'s placeholder-entry convention (an empty `''`
  keeps the header-filter row's columns aligned even for a column with no filter control of its
  own). `aggregateMaterialsByName()` sums consumption *rates* across a group's contributing lots,
  then divides the already-summed quantity - days-left itself is never summed or averaged across
  lots, that's simply wrong. Group confidence is the **weakest** contributing lot's band, not the
  best or a mean - one thin lot makes the whole group's rate thin. A new "Low Stock" state on the
  existing Status filter (`matStatusFilter`, KPI card + both `<select>`s) fires on `daysLeft < 15`
  or any contributing lot's `daysToMsl === 0`.

## Data Export (added 2026-09-08)

An "Export Data" button next to "Refresh Data" (dashboard's `.sync-bar`, `main.js`) opens a small
panel (`frontend/js/export-panel.js`) to download the full daily RM stock snapshot history as a
CSV, optionally narrowed to a date range. **Deliberately scoped to ONLY this one dataset** - the
project owner asked for it specifically, not Purchase Orders/Import Purchases: those are already
fully present in their own source CSVs, but the Stock xlsx files only ever hold *today's* position
(each sync overwrites the prior day's numbers via `update_or_create` - see `sync_utils.py`), so the
`*RMSnapshot` table (captured once daily, see "Snapshot Pipeline Rebuild" below) is the ONLY place
this day-by-day history exists at all - it can't be reconstructed from the spreadsheets afterwards.

- **Backend**: `apps/api/routers/_domestic_base.py`'s `make_export_stock_snapshots()` - one shared
  implementation, same per-plant `_PlantConfig` pattern as every other view in that file. Builds a
  plain CSV (Python's `csv` module + `io.StringIO`, not a library like `openpyxl`) and returns it as
  a Django `HttpResponse` with `Content-Disposition: attachment` - **CSV, not Excel**, per the
  project owner's own "whichever is less compute and faster" - a `.xlsx` would need a whole library
  for no real benefit over a plain CSV any spreadsheet program already opens directly. Columns:
  Plant, Snapshot Date, Material Description, Material Code, Category, Sub Category, Vendor, UOM,
  Opening Stock, Received, Issued, Today's Stock, Rate, Value, Lot Currently Active - reads the
  same `cfg.lot_code_field`/`lot_vendor_field`/`lot_rate_field` getattr pattern
  `stock_snapshots_for_date()` (just above it in that file) already uses for the same per-plant
  schema differences (Achhad has no vendor/uom/sub_category column at all). `from`/`to` query
  params are both optional; omitting both exports everything captured so far. **`IsEditor` +
  `user_can_access_plant()`-gated, unlike every other GET endpoint in this file (plain
  `IsAuthenticated`-any-role)** - the project owner explicitly asked for Editor/Admin only here,
  a deliberate exception to this file's own read-endpoint convention, not an oversight. Wired per
  plant: `GET /api/stock-snapshots/export`, `/api/achhad/stock-snapshots/export`,
  `/api/vapi/stock-snapshots/export`. Regression tests: `apps/api/tests/test_stock_snapshots_api.py`'s
  `TestExportStockSnapshots`.
- **Frontend**: `export-panel.js`'s `openExportPanel()` reuses the shared `#modalBackdrop`/
  `#modalBody` the PO/Material detail modals already use (same close-button/backdrop-click wiring,
  no new CSS) rather than a bespoke panel. The "Export Data" button itself is only rendered when
  `PLANT_KEYS.some(canEditField)` is true (same role+plant check the backend enforces
  independently - see `shared.js`'s `canEditField()`), and each plant's own row inside the panel is
  gated the same way, individually, since a plant-scoped editor may have export access to some
  plants but not others. **Download is a plain `window.open()` to the endpoint URL, not a
  `fetch()`+blob dance** - this app's browser auth is httpOnly-cookie-based (see "Auth & security
  architecture"), so the cookie rides along on a normal same-origin navigation with no extra JS
  needed; the backend's `Content-Disposition` header does the rest. Opened in a new tab rather than
  the current one so a same-tab navigation failure (e.g. a stale session) can't blow away the whole
  SPA - it would just show a JSON error in that new tab instead, closable on its own.
- **Not built (out of scope for this pass)**: exporting Purchase Orders/Import Purchases/current
  Stock (as opposed to snapshot *history*) - the project owner's own request named RM snapshot data
  specifically, for the reason above; extending Export to other datasets is a deliberate follow-up
  if asked for, not assumed here.

## Outgoing email inventory

Every email this app sends goes through one of three builders: `apps/services/email_service.py`'s
`render_email()` (plain-paragraph layout, shared by 7 of the 10 emails below), `apps/services/
consumption_report.py`'s own table-shaped builder (the daily/monthly reports, 8-9), or `apps/
services/plant_mismatch_report.py`'s own similar table-shaped builder (10). **Emails 1-9 end with
"This is a system generated email. Please do not reply." in both the HTML and plain-text body,
and none use an em dash anywhere in body content** (confirmed/fixed 2026-09-08 - `render_email()`
always appended this footer already, but `consumption_report.py`'s own plain-text body was missing
it, and its HTML header used `&mdash;` plus a bare `—` placeholder for a missing rate/days-left
value - both fixed to match this app's plain `" - "`/`"N/A"` convention used everywhere else).
**Email 10 deliberately has NO such footer** - see its own entry below for why.

1. **Login OTP** (`device_service.py: send_device_otp()`) - to the signing-in user, on a new/
   untrusted device.
2. **New device signed in** (`device_service.py: send_new_device_notification()`) - to that same
   user, right after they verify.
3. **New device login admin alert** (`device_service.py: notify_admins_new_device_login()`) - to
   every other active admin.
4. **Password change OTP** (`password_service.py: send_password_change_otp()`) - to the user
   requesting a self-service password change.
5. **Account locked admin alert** (`security_alerts.py: notify_admins_account_locked()`) - to every
   other active admin, once, at the moment 5 consecutive bad passwords actually lock the account.
6. **Unusual login activity admin alert** (`security_alerts.py:
   record_failed_login_and_maybe_alert()`) - to every active admin, when ≥15 failed logins across
   any accounts land within a 5-minute window (suppressed to at most once per 30 min).
7. **Sync failure admin alert** (`security_alerts.py: notify_admins_sync_failure()`) - hardcoded to
   `dishant.barot@ravasco.com` only (explicit request, not every admin, unlike 3/5/6/8).
8. **Daily Raw Material Consumption Report** (`consumption_report.py: send_daily_consumption_reports()`,
   ×3 - one per plant) - to every active admin, once daily (e.g. 20:30 IST - the exact time is
   whatever the external cron-job.org job is configured for, not hardcoded here), triggered by an
   external free scheduler (cron-job.org) hitting `POST /api/internal/send-daily-report` (shared-secret
   auth, `REPORT_CRON_SECRET` - see `reports_views.py`). One of the two (with #9) that are a real
   HTML table (Material / Issued Today / Latest Rate / Days Left), not plain paragraphs. **Grouped
   by category (added 2026-09-08, project owner: "divide the material by category, add a category
   pane in the table")** - `build_plant_report()` reads the same `category` field every `*RMLot`
   model already has (no new data needed), and `_render_report_email()` renders one shaded heading
   row per category (HTML) / one heading line (plain text) before that category's own materials,
   in both cases ordered by whichever category contains that plant's single biggest mover for the
   day (materials within a category keep the existing issued-qty-descending order). A blank
   category buckets as **"Uncategorized"** rather than an empty-titled group or being dropped -
   real on some rows (e.g. Achhad's own `category` is itself a backfilled section-divider label,
   not a guaranteed-present column - see `RTPAchhadRMLot`'s docstring).
9. **Monthly Raw Material Consumption Report** (`consumption_report.py:
   send_monthly_consumption_reports()`, ×3 - one per plant, added 2026-09-08, project owner: "can
   we create a monthly consumption email too... via all the data for that month in the same
   category wise mail like we do for daily") - to every active admin, once on the 1st of each
   month, triggered by the same external scheduler hitting `POST /api/internal/send-monthly-report`
   (same `REPORT_CRON_SECRET` scheme, a separate endpoint rather than a mode flag since the two run
   on genuinely different schedules - see `reports_views.py`). Same category-grouped shape as #8
   (both now share `_render_consumption_rows()`/`_render_consumption_email()` - factored out so the
   two reports' grouping/formatting can't drift apart), just summed over a whole calendar month
   instead of one day; column header reads "Issued This Month" instead of "Issued Today". Defaults
   to the most recently **completed** month (today's month minus one) - the natural target for a
   report meant to fire on the 1st, summarizing the month that just ended; `year`/`month` query
   params override this for a manual re-send. `daysLeft`/confidence stay a present-tense estimate
   (as of right now), not tied to the summarized month, same meaning as in the daily report.
   **Summing, not reading the live period-to-date column, for the primary figure** - `HRS`/`Vapi`
   sum `*RMSnapshot.issued` across every snapshot dated within the target month (already a genuine
   per-day figure - see the module docstring - so summing it is exactly as trustworthy as the daily
   report's own single-day read); `Achhad` sums `RTPAchhadRMDailyMovement.issued` across the month
   (the same dated, reconciliation-confirmed source the daily report trusts), deliberately NOT the
   live `issued` column directly - reading that for an already-closed month would be flatly wrong
   the moment the period rolls over and the sheet resets it for the new month, not just imprecise.
   Achhad's `isEstimate` fallback (same "(est.)" marker as the daily report) exists here too, but is
   **only ever allowed when the requested month is the CURRENT, still-open one** - never for a
   closed past month, since the live column would no longer represent it at all by then. If Achhad
   genuinely has zero `RTPAchhadRMDailyMovement` rows for an already-closed month (the day-matrix
   gap turning out to be a lasting habit rather than a same-day timing quirk, per #8's own note),
   that material is silently excluded from the monthly report rather than estimated - a real,
   visible gap is more honest than a number that can't be trusted. Regression tests:
   `apps/services/tests/test_consumption_report.py`'s `TestBuildPlantMonthlyReportHRS`/
   `TestBuildPlantMonthlyReportAchhad`/`TestSendMonthlyConsumptionReports`,
   `apps/api/tests/test_reports_views.py`'s `TestTriggerMonthlyReport`.
10. **Plant Data Correction Report** (`plant_mismatch_report.py: send_plant_mismatch_reports()`, ×3 -
    one per plant, added 2026-09-08, project owner request: "I need to send them emails individually
    as per their plants based on mismatch or data entry errors... PO number... qty mismatch in MIR
    or RM whichever applicable... rate mismatch... kindly correct them") - unlike every other email
    here, this one goes to a **fixed, real individual per plant, not the internal admin list**:
    `avijit.ghosh@ravasco.com` (HRS), `anil.khatri@ravasco.com` (RTP-Achhad),
    `mahendra.patil@ravasco.com` (RTP-Vapi) - see `plant_mismatch_report.py`'s own `_PLANT_HEADS`.
    None of the three is necessarily a `PTUser` account; the recipient list is a fixed dict, not
    derived from the database. **Every active admin is CC'd** (project owner, same day: "in all the
    emails except that sync, keep all the admins either in cc or direct mail" - "that sync" being
    #7 above, which stays a single fixed recipient per its own earlier explicit request; this is the
    only other email in this app that doesn't already reach every admin as its primary recipient, so
    it CCs them instead). Lists that plant's own currently-flagged (not dismissed)
    `qty_mismatched`/`rate_mismatched` rows from both `*POMirMatch` (PO Number, Material) and
    `*MirStockMatch` (Material) - the exact same booleans the dashboard's Data Quality Flags already
    read, not a re-derived definition. **"Whichever applicable"**: a row only shows a percentage for
    the mismatch that's actually true on it (`-` for the other), never a stale or zero-looking figure
    for something that wasn't flagged. **Deliberately has NO "system generated / do not reply"
    footer** (explicit instruction, same day: "remove that system footer from this emails") - unlike
    every other email in this app, this one is meant to prompt a real reply/action (correcting a
    source document), so telling the recipient not to reply would defeat the point; ends with a plain
    "Regards," signature instead, same as `render_email()`'s own closing but without that one line.
    A plant with nothing currently flagged is skipped entirely (no email to the plant head or admins)
    rather than a routine "all clear" ping. Triggered via `POST /api/internal/send-mismatch-report`
    (same shared-secret scheme as #8/#9) - no built-in cadence; set whatever interval you want on the
    external scheduler. **Correcting the source and re-syncing does reach the DB and the dashboard
    automatically** - the next successful `sync_*`/`match_*` run (auto every 3 hours, or manually
    triggered) re-parses the corrected PO CSV/MIR/Stock file and re-runs that plant's matching
    engine, which recomputes `qty_diff_pct`/`rate_diff_pct`/`is_flagged` in place; the dashboard reads
    this live from the API on every load with no cache to invalidate, so a corrected mismatch simply
    stops appearing (and no longer triggers this email) once its next sync completes - no separate
    manual step. Regression tests: `apps/services/tests/test_plant_mismatch_report.py`,
    `apps/api/tests/test_reports_views.py`'s `TestTriggerMismatchReport`.

**Achhad's "Issued Today" fallback (added 2026-09-08, project owner request) - a real gap found
by checking a live exported `RAVASCO ACHHAD RM STOCK FILE.xlsx` directly, not assumed.** The report
prefers a genuinely-dated source per plant (see `consumption_report.py`'s own module docstring for
why HRS/Vapi's snapshot `issued` is already trustworthy per-day but Achhad's own `issued` column is
a period-to-date summary that resets each period) - `RTPAchhadRMDailyMovement`, parsed from the
Stock file's own daily Recp./Issue matrix. Confirmed directly against a real file the project owner
supplied: that day-matrix can sit **completely blank** for the current day at export time (every
recp/issue cell `None`, not even zero) while the lot's own period-to-date `Issued` column already
has real nonzero values - meaning the plant simply hadn't filled in today's day-matrix column yet.
Before this fix, that meant the report silently said "No material was issued today" even when real
activity was visible elsewhere in the same file. Now, `_todays_issued_rows()` falls back to a lot's
own live `issued` column ONLY when that lot has no `RTPAchhadRMDailyMovement` row for today at all
(a lot that does have one keeps using it - the fallback never overrides a real dated entry), and
every such row is flagged `isEstimate=True` and rendered with a `(est.)` suffix plus an explanatory
footnote (only shown on a plant report that actually has ≥1 estimate row) - **explicitly presented
as a rough period-to-date indicator, not a confirmed same-day figure**, since that unreliability is
real and was never fixed, only deliberately surfaced as an accepted trade-off instead of silence.
If Achhad's day-matrix entry lag turns out to be a permanent habit rather than a same-day timing
quirk, most days going forward will show `(est.)` rows - that's expected under this design, not a
sign something is broken. Regression tests: `apps/services/tests/test_consumption_report.py`'s
`TestBuildPlantReportAchhad`/`TestSendDailyConsumptionReports`.

## Artifact-parity decisions (dashboard redesign)

The dashboard's nav structure and PO-list KPI row were rebuilt to match the original "Purchase
Tracker" Claude Artifact prototype (the tool this whole app replaces) after the user shared
screenshots of it and asked for parity. **I read the artifact's actual source directly** (via the
`Artifact` tool's `list`/`read` actions, not screenshots-only guessing) before touching anything —
every claim below is grounded in that source, not inferred.

**Nav structure matched the artifact exactly as of this pass**: Purchase Orders' plant selector was
HRS/RTP-Achhad/RTP-Vapi only (no "All Plants"), and its level 3 was Domestic/Import Purchases only
(no "Combined") — an earlier pass here had added both based on a verbal description that turned out
not to match the real reference design; both were removed. Raw Material Analysis's plant selector
kept "All Plants" — that part was already correct.

**Superseded (2026-09-04): Purchase Orders' plant selector now DOES include "All Plants"** (listed
first, before HRS), on a direct request from the project owner — a deliberate deviation from the
artifact's own structure described just above, not a reversion to the earlier verbal-description
mistake. Level 3 (Domestic/Import Purchases, no "Combined") is unchanged. See
`frontend/js/main.js`'s file-header comment and `plantTabOptions()`/`PURCHASE_TYPES` for the
current, correct shape — don't trust the "no All Plants" claim above over what's actually in that
function.

**Correction (2026-09-03): the first parity pass was visually wrong despite matching the nav
structure and KPI logic above.** The user reported "the UI isn't matched with the images" after
that first pass shipped. The root cause: I had only sampled a few CSS snippets from the artifact's
source, not read its full CSS block, so I reused one `.view-tab` (filled pill) class for all three
nav levels and invented a `.view-tabs.sub-tabs` override for level 3. The real artifact uses
**three structurally different tab components**, confirmed by reading its complete CSS this time:
`.view-tab` (filled pill — Level 1, Purchase Orders/Raw Material Analysis, only) → `.plant-tab`
(underline tab, no fill — Level 2, plant selector) → `.sub-tab` (small filled pill, distinct sizing
from `.view-tab` — Level 3, Domestic/Import). `frontend/css/style.css` and `frontend/js/main.js`
(`renderPlantTabs()`/`renderPurchaseTypeTabs()`) now emit the correct class per level. Same pass
also fixed: the two "critical" KPI cards (Quantity/Rate-Value Discrepancy) were using `.kpi-card
overdue` (border-only) instead of `.kpi-card critical` (red-soft fill, per the artifact's own
`cardDef`); and the "top 5" PO list was a plain HTML `<table>` where the artifact uses CSS-grid
card rows (`.list-header-row.po-cols` + `.top5-list`/`.top5-row`) — the plain `<table>` is now used
only for the "View all" expanded view (a disclosed simplification: the artifact uses the `gridjs`
library there, which this app deliberately does not adopt). **Lesson for future parity work in this
file: sample-checking a reference's CSS is not enough — read the complete stylesheet and exact
markup before claiming a match**, and note that none of this has been confirmed in an actual
rendered browser in this session (no browser/screenshot tool available) — only via static code
comparison against the artifact's read source and a curl check that the dev server is serving the
updated files. Ask the user to visually re-check before treating this as fully resolved.

**"(Changed Purchase Order)" needed no new code.** The artifact has no amendment/revision-detection
logic at all (`_isNewPo` is hardcoded `true` throughout its source) — that text is literal content
already present in some real HRS POs' `po_number` field (confirmed: 5 real rows, e.g.
`'3000001104 (Changed Purchase Order)'`), written there by the CSV extraction process when only an
amended version of a PO was found in Drive. `po_csv.py` already captures it verbatim; the frontend
already displays `po.poNumber` verbatim. If you see this pattern and think "I should build
amendment detection," don't — it already works, it's just raw data.

**Data Quality Flags** (`frontend/js/main.js`'s `FLAG_CATEGORY_RULES`/`categorizeFlag()`/
`DISCREPANCY_LEGEND`) are a direct port of the artifact's `CATEGORY_RULES`/`categorizeFlag()` — 11
plain regexes run client-side against each PO's own `remarks` field (already parsed and already
returned by the API, no schema change needed), plus 2 "critical" categories (Quantity Discrepancy,
Rate/Value Discrepancy) derived from the diff percentages the matching engine already computes
(`qty_diff_pct`/`rate_diff_pct`/`value_diff_pct`). **The threshold is zero tolerance as of
2026-09-04** (`FLAG_PCT` in `main.js`, mirroring `matching*.py`'s/`matching_achhad.py`'s/
`matching_vapi.py`'s `FLAG_DIFF_PCT`, all set to `0`) — **supersedes the earlier "5%, deliberately
not the artifact's 10%" decision** made and tested in an earlier session; the project owner
explicitly asked for no tolerance at all, down to a 1kg-out-of-1000kg (0.1%) qty diff or the
equivalent for rate/value. Since every comparison uses strict `>`, an exact match (a computed diff
of `0`) still never flags — only a real, nonzero difference does. If this ever needs to change
again, change it in exactly one place per side: `FLAG_PCT` in `main.js` (frontend — drives the PO
KPI cards/row flags/line-item badges *and* the Raw Material Analysis view's own Quantity/Rate
Discrepancy KPI cards, which reuse the same constant via `computeMaterialPoLinkage()`) and
`FLAG_DIFF_PCT` in each of the three `matching*.py` files (backend — drives the stored
`is_flagged` on both PO↔MIR and MIR↔Stock matches; keep all three plant files identical).
`DISCREPANCY_LEGEND`'s two critical-category entries no longer interpolate the numeric threshold
into their wording (a "0" would read oddly as "by more than 0 percent") — they state "no
tolerance" directly instead, so re-word them too if the policy ever changes back to a nonzero cutoff.

**Superseded (2026-09-08): Data Quality Flags now cover every computed match-quality signal, not
just qty/rate/remarks.** `apps/services/matching_core.py`'s `_diffs_and_flag()` had always computed
`taxable_value_flagged`/`final_value_flagged` (and the model layer already had
`tax_type_mismatch`/`uom_mismatch`) alongside `net_value_mismatched` — but before this pass, only
qty/rate ever reached the frontend as a visible category; the rest were computed and stored, then
silently discarded. The project owner asked for this directly: *"add all the error like on make it
clear like PO not found, tax rate, taxable value, final amount mismatch all the errors under one KPI
data quality flag and then in filter categorize them based on the issue."* `flags.js`'s
`computePoFlags()` now also sets 6 more categories per PO (all derived from the PO's own line items,
same as Quantity/Rate Mismatch already were): **PO Not Found in MIR** (critical — `!it.matched`, no
MIR entry crossed `MATCH_THRESHOLD` at all), **Tax Type Mismatch in MIR** (`it.taxTypeMismatch`),
**Net Value Mismatch in MIR** (`it.netValueMismatched`), **Taxable Value Mismatch in MIR**
(`it.taxableValueMismatched`), **Final Amount Mismatch in MIR** (`it.finalValueMismatched`), **UOM
Mismatch in MIR** (`it.uomMismatch`) — all added to `DISCREPANCY_LEGEND`/`CATEGORY_COLORS` too, so
the legend panel and row-flag icon colors stay in sync with the same category list. New backend
columns (migration `0040`): `net_value_mismatched`/`taxable_value_mismatched`/
`final_value_mismatched` on all 6 `*POMirMatch`/`*ImportPOMirMatch` models, surfaced by
`_domestic_base.py`'s/`imports_views.py`'s `_line_item_dict()`. The "Data Quality Flags" KPI card
(`po-list.js`) already meant "any category at all" (redefined the same day, see that file's own
comment at the `flagsCount` definition) — it just had far fewer categories feeding it before this.
**"Filter by Flags" no longer has a fixed 4-option list** — `po-list.js` now also appends one
`<option value="cat:<label>">` per category actually present in the current date range/plant
selection (built from `flagCategoryCounts`, a per-label tally over `po._categories`), so a reviewer
can jump straight to e.g. "Taxable Value Mismatch in MIR (3)" instead of opening every flagged PO to
find out which ones have that problem. **Extended the same day to Import Purchases and Raw Material Analysis**, per the project owner's
direct follow-up request. Each view already had its own per-item/per-link category computation
(mirroring Domestic's, not sharing code with it — same duplication precedent as the rest of this
app's per-plant/per-view logic) — extended in place rather than unified into one shared function:
- **Import Purchases** (`import-po.js`'s `importCategoriesFor()`, `flags.js`'s
  `importCriticalFlagsFor()` for the PO detail modal) — reads the same 6 fields off each item's
  nested `mirMatch` object (`imports_views.py`'s `_mir_match_dict()`, which already exposed them —
  see the note above). "PO Not Found in MIR" here means `!i.mirMatch` (no match payload at all,
  same as Domestic's `!it.matched`). `counts.flags`/`flagsOptionsHtml` gained the same "any
  category" redefinition and `cat:<label>` options as Domestic (excluding the 3 labels —
  `qtydisc`/`qtydiscmir`/`ratedisc` — that already have a dedicated dropdown option, tracked via
  each category's own `key`). **`importRowFlags()` was also fixed to read `po._categories`
  instead of only `po.dataQualityFlags`** — previously the qty/rate/PO-not-found/value-mismatch
  categories had no per-row flag icon at all, only a KPI-card count; this brought it to the same
  per-category, per-color icon Domestic's `rowFlags()` already had.
- **Raw Material Analysis** (`materials.js`'s `computeMaterialPoLinkage()`, `material-modal.js`'s
  `materialCritPos`) — reads the same 6 fields off `l.item` (the material's fuzzy-linked PO line
  item, from `linkedPoItemsForMaterial()`), same per-item scoping the existing qty/rate checks
  already used (never misattributed from a different line item on the same multi-item PO).
  `flaggedMats`/the "Data Quality Flags" KPI redefined the same "any category" way, with
  `cat:<label>` options added to `matFlagsSelect` (excluding the 2 dedicated Quantity/Rate Mismatch
  labels). The material modal's Flags & Corrections tab (`materialCritPos`, previously a bare
  label→PO-set map with every entry hardcoded "CRITICAL") now also carries each entry's real
  severity, rendering "INFO" for the 5 new info-severity categories.
- **Note on "PO Not Found in MIR" density**: unlike Domestic, where a PO only exists once its own
  line items are placed, both Import's and Materials' linked-item universe includes every
  still-open (not-yet-received) PO by construction — so this category will naturally fire for
  most on-order materials/import POs, same as it does for Domestic's own on-order POs. This is the
  expected, consistent behavior, not a bug to suppress.

**The regex categorization is intentionally imprecise, same as the original.** E.g. a remark
mentioning "no Item ID/Vendor Code printed" (an old-format-PO-template note) matches the "Vendor
code scheme inconsistency" rule via a bare `/vendor code/i` test, even though that's not quite the
right semantic bucket. This is a known, accepted characteristic of simple regex categorization —
carried over from the artifact as-is, not a new bug introduced during the port.

**Roadmap status (updated 2026-09-04)** — items 1, 2, and 5 below are now built; the rest are
explicitly deferred to a v2 pass, per the project owner:
1. ~~**Materials consolidated cross-plant view**~~ — **built.** `frontend/js/main.js`'s
   `aggregateMaterialsByName()` sums stock qty/value across every vendor lot for a material name,
   scoped to the current plant selection (a single plant tab sums that plant's own lots; "All
   Plants" sums across all three) - matches the artifact's `buildMaterialIndex()` summary-number
   behavior without losing the underlying per-vendor/per-lot detail (`vendors`/`lots` on every
   group, used by the drill-down modal and `materialLinksToItem()`) - the exact vendor-blending
   trap this project's own audit found once already (see "Why the data model looks the way it
   does" in README.md) is avoided by keeping that detail, not by refusing to aggregate.
2. ~~**3rd stepper step ("Stocked")**~~ — **built**, 2026-09-04. Unlike the artifact's
   `poReceivedInStock()` (a coarse "does this material show `received > 0` anywhere in this
   plant's current Stock file"), this is a real per-line-item chain: `stockMatched` on each PO
   line item (`hrs_views.py`/`achhad_views.py`/`vapi_views.py`'s `_line_item_dict()`) is true only
   when the *specific MIR entry that line item matched to* itself has a real MIR<->Stock match
   (`*MirStockMatch`), not just "this material exists somewhere in stock." `miniStepperHtml()` in
   `main.js` renders it as the 3rd dot/line (Ordered → Inwarded → Stocked). Expect it to rarely
   show "done" for HRS specifically — HRS's `received`/REC column is already documented elsewhere
   in this file as reading 0 for nearly every real lot, which limits how often a fresh
   `HRSMirStockMatch` forms at all; this is a real data-shape limitation, not a bug in the field.
3. ~~**Import PO parsing for RTP-Vapi**~~ — **built**, 2026-09-04 (superseding the "deferred to
   v2" note this item used to carry). `apps/core/management/commands/sync_vapi_imports_po_csv.py`
   syncs `Master_RTP_VAPI_Imports_Purchase_Data.csv` (29 POs / 37 line items confirmed live
   2026-09-04) into `RTPVapiImportPurchaseOrder`/`RTPVapiImportPOLineItem`. **The assumption behind
   the original "deferred, genuinely different shape" item turned out to be wrong**: Vapi's own
   Imports CSV uses the exact same column layout as HRS's/Achhad's (BOE number, bill of lading,
   exchange rate, dual quantities qty-per-PO vs qty-per-BOE, license numbers, etc.) — unlike
   MIR/Stock, which really do differ per plant (see "Per-plant models, not a shared schema"
   above), the Imports CSV format turned out to be shared across all three plants. All three now
   go through the one shared `apps/services/parsers/import_po_csv.py` parser (`sync_hrs_imports_
   po_csv.py`/`sync_achhad_imports_po_csv.py`/`sync_vapi_imports_po_csv.py` each just point it at
   their own plant's model classes and Drive file). `boe_number`/`bill_of_lading_number`/
   `exchange_rate`/`qty_as_per_boe` etc. are fields on `HRSImportPOLineItem`/
   `RTPAchhadImportPOLineItem`/`RTPVapiImportPOLineItem` alike.
4. **Licenses** (Advance Authorisation letter tracking, resolving each import PO's own Drive
   folder) — deferred to v2 (project owner, 2026-09-04). No `License`/`AdvanceLicense` model exists
   in `apps/core/models.py` at all; don't build without asking first when this is picked back up.
5. ~~**Dismiss/override a flagged match**~~ — **built**, 2026-09-04. See "Dismiss/override a
   flagged match" below.
6. ~~**In-app password reset**~~ — **built**, 2026-09-04. See "Frontend pages and the shared top
   nav" above (Users panel) - `PATCH /api/auth/users/<id>` now accepts an optional `password` field.
7. **Scheduling** (Render Cron Job or similar for `sync_*`/`match_*` commands) — deferred to v2
   (project owner, 2026-09-04), grouped with the Render-hosting decisions generally. Every command
   still only runs when invoked manually or via the admin-triggered `sync-trigger` endpoints.

## Inline "Edit Everywhere" (added 2026-09-04)

Every PO detail modal (Domestic and Import) now has per-field pencil-to-edit
corrections, not just Import's. **This mutates the real row and writes an
append-only audit row — it is not a resolve-at-read overrides table.** A
project-owner spec initially described the latter (separate `original_value`/
`override_value` rows, resolved at display time), but Import's edit feature
already shipped with the mutate+audit pattern before this pass — the decision
made here was to extend that existing, working pattern to Domestic rather than
rebuild both around the spec's overrides-table design. The original "Purchase
Tracker" Claude Artifact prototype's own correction UX (pencil → jump to a
shared form → queue a request to a Drive folder for manual review) was
consulted for small UI/copy cues only (pencil styling, "Correcting: X" copy) —
its human-review-queue mechanism doesn't apply here, this app already has a
real instant write path.

- **Backend**: `apps/services/validation.py` (`is_valid_gstin`/`is_valid_email`
  — warn, never block, a real vendor GSTIN/email can be genuinely unusual).
  `DomesticPOCorrection` (`apps/core/models.py`) is `ImportPOCorrection`'s
  audit-row shape, kept as its own model rather than a shared/generalized
  table — same per-PO-type-gets-its-own-model convention as everything else in
  this file. Each of `hrs_views.py`/`achhad_views.py`/`vapi_views.py` (not a
  shared cross-plant module — Domestic stays one-router-per-plant everywhere
  else) gained its own `correct_field` PATCH view, byte-for-byte the same
  shape as `imports_views.py`'s (allow-listed field names split PO-level vs.
  item-level, date/decimal coercion, mutate + audit row in one
  `transaction.atomic()`). Editing a field that feeds PO↔MIR matching (`qty`,
  `net_price`, `net_value`, `vendor_name`, `description`, `vendor_gstin`)
  synchronously re-runs that plant's `run_full_match()` so discrepancy badges
  update immediately instead of waiting for the next scheduled `match_*` run
  — safe because `run_full_match()` is already idempotent and plant data
  volumes are small (see "Match accuracy" above).
- **Domestic line items have no stable natural key** — `item_id` isn't unique
  or required, and a plant's `sync_*` command deletes+recreates every line
  item on any change (see `sync_po_csv.py`). Item-level corrections key on
  `(plant, po_number, item_id)`, same as `ImportPOCorrection` already does;
  this is an inherited limitation, not a new one, and a blank-`item_id` item
  still can't be individually addressed.
- **Per-plant admin scoping is new**: `PTUser.plants` (`JSONField`, default
  `[]`) — **empty means "all plants,"** not "no plants," so every admin that
  existed before this field keeps full access with no backfill.
  `apps/api/permissions.py::user_can_edit_plant(user, plant_key)` is a plain
  function, not a `BasePermission` subclass, called explicitly inside every
  `correct_field` view (the plant key isn't always a URL kwarg — Domestic's
  three routers have it implicit per-file). Only gates writes; every role can
  still read every plant's dashboard. Editable in `admin.html`'s user form as
  a set of plant checkboxes (`frontend/js/shared.js`'s `PLANT_KEYS`), exposed
  on `GET /api/auth/users` and `GET /api/auth/me` (the latter is what
  `shared.js`'s `canEditField()` reads client-side to decide whether to render
  a pencil at all — a non-editing viewer or a plant-scoped-out admin never
  gets the markup, not just a disabled icon).
- **Frontend**: `editableLine()`/`plainLine()`/`wireEditableLines()`/
  `startFieldEdit()`/`savePoField()`/`distinctFieldValues()` moved from
  `main.js` into `shared.js` (both `openPoModal()` and
  `renderImportPoModalBody()` need them now). `fieldType` (`'text'`/`'date'`/
  `'number'`/`'select'`) picks the input control — `'select'` options are
  always derived from distinct values already present in the loaded PO list,
  never hardcoded. Explicit Save (✓)/Cancel (✗) icons sit next to the active
  input in addition to the original Enter/Escape/blur behavior — additive,
  not a replacement. Domestic's PO detail modal gained a third tab, **Flags &
  Corrections** (it never had one — Domestic's flags were always client-side-
  only `categorizeFlag()` output with nowhere to show them together); Import's
  same-named tab now also renders its correction history, not just flags,
  finally living up to its name.
- **Domestic has no Shipment & License tab** — those fields (BOE/BL/laden-on-
  board/country-of-origin/license) don't exist on domestic PO/line-item models
  at all (domestic POs never clear customs); don't add one without a real
  schema change first.

**A real bug, found and fixed, 2026-09-04: `decimal.InvalidOperation` wasn't caught.** Every
`correct_field` view's `_coerce_value()` (`hrs_views.py`/`achhad_views.py`/`vapi_views.py`/
`imports_views.py`) parses a decimal field with `Decimal(str(raw_value))` inside a
`try/except (TypeError, ValueError)`. Genuinely non-numeric input (e.g. `"abc"` typed into a qty
field) makes `Decimal()` raise `decimal.InvalidOperation`, which is an `ArithmeticError`, not a
`ValueError` — it wasn't caught, so it propagated as an unhandled 500 instead of the clean 400
every other bad-input case returns. Fixed by catching `decimal.InvalidOperation` inside
`_coerce_value` and re-raising as `ValueError`, so the existing outer `except (TypeError,
ValueError)` in the view catches it like any other invalid value. Regression test:
`apps/api/tests/test_hrs_correct_field.py::test_invalid_decimal_value_returns_400_not_500`. If you
add a new decimal-coercing field anywhere, this is already handled — but if you write a *new*
`_coerce_value`-shaped function from scratch elsewhere, remember `Decimal()`'s failure mode isn't
a plain `ValueError`.

**A real bug, found and fixed, 2026-09-04: the save/cancel ticks were always visible, hiding the pencil.**
`frontend/css/style.css`'s `.edit-actions { position:absolute; ...; display:flex; }` has the same
specificity as the browser's built-in `[hidden] { display:none }` rule, and this stylesheet loads
after the browser default - so the class rule won its ties, and `actions.hidden = true`
(`startFieldEdit`'s initial/reverted state) had no visible effect. The save (✓) / cancel (✗) icons
rendered at all times, stacked exactly on top of the pencil (both `position:absolute; right:0;
top:1px`) - reported by the project owner as "the right and wrong ticks" appearing instead of a
pencil in the Domestic/Import PO detail modals. Fixed with `.edit-actions[hidden] { display:none
!important; }`. **If you add a new absolutely-positioned class alongside a `hidden`-toggled
element anywhere in this app, check this specifically** - `[hidden]` alone is not reliable once
any same-or-higher-specificity display rule exists on that element.

**Raw Material Analysis modal gained the same inline-edit capability, 2026-09-04** - it had none at
all before this (unlike the PO detail modals, which already had it; the original artifact
prototype's own material modal never had one either, so this is new ground, not a re-port).
Category and Rate are now editable per Stock lot row in the "Stock by Plant" tab (`openMaterialModal`
in `main.js`) - not on the Overview tab's own rolled-up Classification block, which shows one
"first found" value across every sibling lot across all 3 plants and would be ambiguous about which
underlying lot an edit should target. Each row edits its own specific lot (`shared.js`'s new
`editableCell()`, a compact inline sibling of `editableLine()` for a table cell rather than a block
`<div>` line), which is why `wireEditableLines()` (`shared.js`) now also accepts a `(lineEl) => url`
function instead of only a fixed URL string - this table's rows span 3 different plants' own PATCH
endpoints (`materialFieldsUrl()`), unlike a PO modal where every line targets the same one PO.
**Backend**: `MaterialCorrection` (`apps/core/models.py`, migration `0013`) is the audit-row model,
same append-only mutate+audit shape as `DomesticPOCorrection`, keyed on `(plant, lot_id)` since each
plant's Stock lot table has its own independent id space. Each of `hrs_views.py`/`achhad_views.py`/
`vapi_views.py` gained its own `correct_material_field` PATCH view + `_MATERIAL_EDITABLE_FIELDS`
allow-list - genuinely different per plant, not copy-paste: HRS/Vapi's decimal rate field is
`basic_rate`, Achhad's is `rate` (Achhad's `RTPAchhadStockLot` also has no `sub_category`/`uom`/
vendor field at all - its own Stock sheet is material-shaped, not lot-shaped). Editing
`description`/vendor/rate re-runs that plant's `run_full_match()` synchronously (same reasoning as
the PO-side `_REMATCH_TRIGGER_FIELDS`) since those feed the MIR<->Stock match gate. New endpoints:
`PATCH .../materials/<lot_id>/fields` for all three plants (`apps/api/urls.py`). Regression tests:
`apps/api/tests/test_material_correct_field.py`.

**Every PO-level flag can now be manually dismissed/reinstated, 2026-09-04** - not just PO<->MIR/
MIR<->Stock match flags (which already had `dismissed_by_override`, see "Dismiss/override a
flagged match" below). The Quantity/Rate-Value Discrepancy critical flags and the Data Quality
Flag category (Domestic: `main.js`'s `computePoFlags()`/`categorizeFlag()`; Import: `apps/
services/import_flags.py`'s `po_flags()`) are computed at read time from `remarks`/diff
percentages, not stored rows, so there was no column anywhere to carry a dismissal the way a real
match row can. `FlagDismissal` (`apps/core/models.py`, migration `0014`) is a new generic table -
`(plant, po_number, flag_key)` unique, upserted in place by `apps/services/flag_dismiss.py`'s
`dismiss_po_flag()`, same shape/audit fields as `dismiss_match()`. `flag_key` is Domestic's own
flag label directly (e.g. `"Quantity Discrepancy"`, safe since a PO has at most one flag per label
- `computePoFlags()`'s `Map` only ever holds one entry per label) or, for Import, `<code>:<item_id>`
(e.g. `"F7:ITEM3"`, since `import_flags.py`'s flags are per-item and already carry a stable
`code`). New endpoints: `PATCH .../purchase-orders/<po_number>/flags/dismiss` (all three Domestic
plants) and `PATCH /api/imports/purchase-orders/<plant>/<po_number>/flags/dismiss`. Frontend:
`shared.js`'s `dismissPoFlag()`, `main.js`'s `poFlagHtml()`/`importFlagHtml()` (rendered in each
PO detail modal's Flags & Corrections tab, reading `po.flagDismissals` from the detail payload),
and `wireDismissLinks()` grew a `data-match-type="po-flag"`/`"import-po-flag"` branch that routes
to `dismissPoFlag()` instead of `dismissMatch()` - one shared dismiss-link wiring function for
both flag families, not two near-duplicates. Regression tests:
`apps/api/tests/test_dismiss_flag.py`.

**A real bug, found and fixed, 2026-09-04: stale modal data from a fast row-switch.**
`openImportPoModal()` and `openMaterialModal()` in `frontend/js/main.js` had no request-token
guard around their `await` calls — clicking one PO/material row, then clicking a *different* row
before the first row's fetch resolved, could let the first (now-stale) response land after the
second and silently overwrite the modal with the first row's data, even though the modal's title/
backdrop by then already showed the second row. Fixed with a module-level `modalRequestId`
counter (declared near the top of `main.js`): each call captures `++modalRequestId` on entry into
its own `myModalRequestId`, and re-checks `myModalRequestId !== modalRequestId` after every
`await` before touching the DOM, discarding its response if a newer modal open has since
superseded it. Same pattern used in both functions - if a third modal-opening function gets added
later, follow it there too rather than re-deriving a different fix.

## Dismiss/override a flagged match (added 2026-09-04)

Closes the gap `apps/api/permissions.py`'s `IsEditor` docstring had been describing since the auth
pass ("dismissing/overriding a flagged match once that endpoint exists"). `dismissed_by_override`
already existed as a column on every `*POMirMatch`/`*MirStockMatch` model - this pass added the
endpoint plus three audit columns (`dismissed_by`, `dismissed_at`, `dismissed_reason`, migration
`0011`) and the frontend controls.

- **Backend**: `apps/services/match_dismiss.py`'s `dismiss_match(model_cls, match_id, user,
  dismissed, reason)` is the one real implementation, shared across all three plants (dismissing a
  match is byte-for-byte identical per plant, unlike `matching.py`'s own deliberate per-plant
  scoring duplication - there's no per-plant variation here to protect). Each of
  `hrs_views.py`/`achhad_views.py`/`vapi_views.py` wires it to that plant's own match model classes
  via two thin views, `dismiss_po_mir_match`/`dismiss_mir_stock_match` -
  `PATCH .../matches/po-mir/<id>/dismiss` and `PATCH .../matches/mir-stock/<id>/dismiss` (see
  `apps/api/urls.py`). `IsEditor` + `user_can_edit_plant()`-gated, same pattern as `correct_field`.
  Body: `{"dismissed": true/false, "reason": "<optional>"}`. Clearing a dismissal
  (`dismissed: false`) also clears `dismissed_by`/`dismissed_at`/`dismissed_reason` - a match
  that's no longer dismissed shouldn't keep showing stale provenance for an undone decision.
  `update_or_create`'s `defaults` dict in `matching.py`/`matching_achhad.py`/`matching_vapi.py`
  never touches `dismissed_*`, so a dismissal survives every re-match (see
  `test_dismiss_match.py`'s `test_editor_can_dismiss_with_reason_and_it_survives_rematch`, which
  exercises this directly).
- **Frontend**: `matchStatusHtml()` (PO<->MIR, in the PO detail modal's Item & Stock tab) and the
  new `mirStockMatchHtml()` (MIR<->Stock, in the material modal's Stock by Plant tab) both render a
  "dismiss"/"reinstate" link next to a flagged badge when `canEditField(plantKey)` is true - a
  dismissed flag stays visible but muted (struck-through, `.flag-badge.dismissed`) rather than
  hidden, so a reviewer can still see what was dismissed and why (title tooltip carries who/when/
  reason). `shared.js`'s `dismissMatch(plantKey, matchType, matchId, dismissed, reason)` is the one
  fetch wrapper both badges' click handlers (`wireDismissLinks()` in `main.js`) go through. A plain
  `window.prompt()` for the optional reason, not a custom modal - this app has no reusable
  modal-with-textarea component and a full one felt like overkill for one optional text field.

## Import PO <-> MIR reconciliation (added 2026-09-04)

Import PO line items now match against the same MIR (and, transitively, Stock) data domestic PO
line items already used — **MIR/Stock are shared Drive files across domestic and import purchases
for a plant; only the PO-source CSV differs** (project owner correction, 2026-09-04 — see the
"Superseded again" note under "Known gaps" for what this replaces). Before this, `imports_views.py`
had no MIR-derived fields at all; the three previously-`disabled: true` "Awaiting MIR" KPI cards
(Material Inwarded, Qty Discrepancies (BOE vs MIR), Rate Discrepancies) are real now.

- **New per-plant models**: `HRSImportPOMirMatch` / `RTPAchhadImportPOMirMatch` /
  `RTPVapiImportPOMirMatch` (migration `0012`) — same shape as the existing `*POMirMatch` models
  (tier, match_score, qty/rate/value_diff_pct, `dismissed_*`), FK'd to that plant's *Import*
  PO line item on one side and the plant's ordinary `*MIREntry` on the other (the same MIR table
  domestic matching already uses — not a separate import-only MIR table). No separate
  MIR<->Stock match table was needed: the existing `*MirStockMatch` models are already keyed on
  `mir_entry` alone, independent of whether that MIR row traces back to a domestic or an import PO,
  so they already cover the Stock leg for both.
- **Which import quantity is compared**: `qty_as_per_boe`, not `qty_as_per_po` (project owner,
  2026-09-04) — the Bill of Entry quantity is what customs recorded as actually clearing/arriving,
  the real-world equivalent of what MIR logs as physically received; `qty_as_per_po` is only the
  originally ordered amount and can legitimately differ (partial/split shipments).
- **A real currency bug, found and fixed against live data the same session**: import line items
  are priced in the PO's own currency (confirmed: every real Vapi import PO today is USD), but
  MIR's `rate`/`taxable_value`/`net` are always INR. The first version of this matcher compared
  them raw and scored every real pair near zero (a ~94x gap, not a rounding difference — India's
  USD/INR rate, not a few percent). `_import_rate_value_inr()` (same function, copy-identical, in
  `matching.py`/`matching_achhad.py`/`matching_vapi.py`) converts before scoring/diffing: rate =
  `net_price * exchange_rate` (falls back to bare `net_price` if `exchange_rate` is null — 2 of 37
  real rows); value = `total_inclusive_value` (the real landed-in-India INR figure, confirmed
  empirically closer to MIR's `taxable_value` than `net_value * exchange_rate` is) falling back to
  `net_value * exchange_rate`. Confirmed against a real row this session: PO net_price $1.95 ×
  exchange_rate 93.8 = ₹182.91 vs. the matched MIR entry's ₹225.50 rate (a real ~23% discrepancy,
  not a currency artifact) — before the fix this same pair scored effectively 0 and never matched
  at all. Real result after the fix: Vapi (the only plant with live import data today) went from 0
  of 37 import line items matched to 25 of 37 (68%), consistent with this app's other real match
  rates.
- **`run_full_match()` in all three `matching*.py` modules now also loops over that plant's Import
  PO line items** (`match_import_po_mir_line_item()`, alongside the existing domestic
  `match_po_mir_line_item()` loop) — one idempotent pass covers both, returned as a new
  `import_po_line_items_matched` key alongside the existing `po_line_items_matched`. `match_hrs`/
  `match_achhad`/`match_vapi`'s stdout now reports both counts.
- **`apps/services/sync_trigger.py`'s `_IMPORT_PLANT_COMMANDS`** now runs that plant's
  `match_<plant>` command after its imports CSV sync (previously just the one sync command, no
  match step at all) — newly-synced import POs get matched immediately, the same way the domestic
  pipeline's own `match_<plant>` step already worked.
- **`imports_views.py`**: `_item_dict()` gained a `mirMatch` field (`_mir_match_dict()`) — null
  when the item never crossed `MATCH_THRESHOLD`, otherwise `matchId`/`tier`/`matchScore`/
  `qtyDiffPct`/`rateDiffPct`/`valueDiffPct`/`isFlagged`/`dismissedByOverride`/`dismissedReason`/
  `stockMatched` (`stockMatched` reads the `mir_entry.stock_matches` prefetch cache, same
  `len(...) > 0` pattern `hrs_views.py`'s own `_line_item_dict()` uses, not a fresh query per
  item — `purchase_orders()`/`purchase_order_detail()`'s `prefetch_related` was extended to include
  the same 3-level chain). A new `PATCH /api/imports/matches/po-mir/<plant>/<match_id>/dismiss`
  (`dismiss_import_po_mir_match`) reuses the existing plant-agnostic `dismiss_match()` — no new
  dismiss logic needed, since that function was already model-class-parametrized. `correct_field`
  gained a `_REMATCH_TRIGGER_FIELDS` set (`vendor_name`/`vendor_gstin`/`description`/
  `qty_as_per_boe`/`net_price`/`net_value`) and now re-runs that plant's `run_full_match()`
  synchronously when one of those is edited — same reasoning as domestic's own
  `_REMATCH_TRIGGER_FIELDS`, ported here since imports previously had nothing to re-match at all.
- **Frontend**: `main.js`'s `renderImportPoList()` computes `poInwarded`/`poQtyDiscMir`/
  `poRateDiscMir` client-side (a PO "has" a MIR condition if any of its line items does) since
  there's no server-computed PO-level MIR aggregate for imports, only per-item `mirMatch` — unlike
  domestic's `qtyDiscrepancy` etc., which the backend already rolls up to PO level via
  `flags.po_has_qty_discrepancy()`. The Items tab in the import PO detail modal gained an "MIR
  Match" column, rendered by the new `importMatchStatusHtml(item, plantKey)` (same badge shapes as
  `matchStatusHtml()`, reading the nested `mirMatch` object instead of domestic's flat fields).
  Dismiss links from that column route through `shared.js`'s `dismissMatch()`, which now branches
  on `matchType === 'import-po-mir'` to call `apiImports()` instead of `apiForPlant()` — imports'
  dismiss endpoint lives under the cross-plant `/api/imports/...` router with `plant` as a path
  segment, not under a per-plant `apiPrefix` the way domestic's `matches/po-mir/<id>/dismiss` does.

## Full-codebase audit (2026-09-10)

The first pass to deliberately go looking for bugs across every layer at once (models/migrations,
auth/admin backend, matching engines, sync pipeline/parsers, outgoing email, frontend JS), rather
than finding them incidentally while building a feature. Six parallel reviews, each scoped to one
subsystem and told to read this file first so it wouldn't re-flag already-documented, deliberate
design decisions as bugs. Every real finding below was independently verified by reading the actual
code (not trusted on the reviewing pass's word alone) before being fixed, and every fix has a
regression test proving it - `uv run pytest` was green (581 passed) after all of them, and
`manage.py check` / `makemigrations --check --dry-run` were clean throughout.

**1. MIR<->Stock matching compared rate/qty with no unit conversion at all — the most consequential
finding.** `apps/services/matching_core.py`'s `match_mir_entry_stock()` compared `mir_entry.rate`/
`qty` directly against the Stock lot's own `basic_rate`/`received` with no call to `_uom_adjust()`
at all — unlike PO<->MIR (`_score_components()`/`_diffs_and_flag()`, both just above it in the same
file), which has always normalized both sides to a common base unit first (fix 2.C, documented
earlier in this file). A material logged in MIR as MT against a Stock lot recorded in KG would
report a ~1000x "rate mismatch" that's actually just a unit-mismatch artifact, not a real
discrepancy — and both `HRSMIREntry`/`HRSRMLot` (and the Achhad/Vapi equivalents) carry independent
`uom` fields with no guarantee they agree for a given material, so this wasn't a hypothetical edge
case. None of the three `*MirStockMatch` models had a `uom_mismatch` field at all (unlike every
`*POMirMatch`/`*ImportPOMirMatch` model, which already has one) — confirming this wasn't a
deliberately-scoped-out concern with a field just not wired up, it was an outright gap.

Fixed by adding `uom_mismatch` to `HRSMirStockMatch`/`RTPAchhadMirStockMatch`/
`RTPVapiMirStockMatch` (migration `0044`, same meaning as the existing `*POMirMatch` field: qty/rate
diffs are `None`, not a nonsense percentage, when the two units belong to different, non-convertible
families) and calling `_uom_adjust()` in `match_mir_entry_stock()` before comparing rate/qty, exactly
the way PO<->MIR already does — value stays uncompared-for-units since it's a currency amount, not a
per-unit figure. Surfaced on the API as `uomMismatch` in `_domestic_base.py`'s `mirStockMatches`
list (not yet rendered as a frontend badge — the fix closes the actual data-correctness bug; adding
a UI badge for it is a natural small follow-up, not done in this pass to keep the fix itself
minimal and reviewable). Regression tests:
`apps/services/tests/test_run_full_match_pipeline.py`'s
`test_rate_and_qty_are_unit_converted_before_comparing_not_raw`/
`test_incompatible_units_are_flagged_as_uom_mismatch_not_a_nonsense_percentage`.

**2. Two of the four PO/RoDTEP detail-modal openers were missing the stale-response guard the
other two got fixed with on 2026-09-04.** This file already documents `modalRequestId` (see "Inline
'Edit Everywhere'" above) as the fix for `openImportPoModal()`/`openMaterialModal()`'s own race
(clicking row A then row B before A's fetch resolves could let A's stale response overwrite the
modal after B's). `openPoModal()` (`po-modal.js`) and `openRodtepScriptDetail()` (`rodtep-panel.js`)
were never given the same guard when it was first applied — confirmed by grep: `modalRequestId` was
referenced only in `material-modal.js`/`import-po.js`, nowhere else, even though `charts.js`'s own
comment defining the counter claimed it was used by "every" modal-opener. Fixed by applying the
identical `const myModalRequestId = ++modalRequestId` / `if (myModalRequestId !== modalRequestId)
return;` pattern to both functions, and corrected `charts.js`'s comment to name all four functions
and note a fifth needs the same treatment if one is ever added.

**3. A TOCTOU race in the "can't remove/deactivate/delete the last active admin" check.**
`users_views.py`'s `update_user()` (both the PATCH branch's role/isActive change and the DELETE
branch added for the hardcoded-admin delete feature) used to do a plain read
(`PTUser.objects.filter(role=admin, is_active=True).exclude(pk=target).exists()`) with no locking,
followed by a completely separate `.save()`/`.delete()`. With exactly two active admins A and B, two
concurrent requests — one acting on A while excluding A from its own count (sees B, passes), the
other acting on B while excluding B (sees A, passes) — could both succeed, since each transaction's
own "exclude self" query never locks the same row the other transaction is about to act on (they
exclude *different* rows, so a naive `select_for_update()` scoped the same way wouldn't have fixed
it either). Fixed with `_assert_not_last_active_admin()` — locks **every** currently-active admin
row via `select_for_update()`, not just "every active admin other than this one," so any two
concurrent callers always contend for the same lock regardless of which admin each one excludes,
wrapped in `transaction.atomic()` around the whole check-and-mutate in both branches. Regression
test: `apps/api/tests/test_last_admin_race.py` (a real two-thread test against a genuine separate
DB connection per thread — `@pytest.mark.django_db(transaction=True)`, since a row lock is only
meaningful across two actually-separate transactions, which the default single-wrapping-transaction
`django_db` fixture would never exercise) — proves exactly one of two concurrent deactivation
attempts against two different admins succeeds, never both. The PATCH path's own last-admin
protection (`test_users_plants.py`) had no test coverage at all before this pass, on top of the race
itself.

**4. Three non-atomic brute-force/session counters.** `auth_backend.py`'s
`_register_failed_attempt()` and `otp_service.py`'s `verify_otp()` both computed a new counter value
from an in-memory Python object (`user.failed_login_attempts + 1`, `entry.attempts += 1`) and wrote
it back separately, rather than locking the row for the duration of the read-check-write. Concurrent
requests against the same account/OTP (e.g. a scripted parallel brute-force rather than serial
guesses) could each read the same stale count and each write the same single increment, undercounting
attempts and weakening (not eliminating — the lockout/invalidation still fires eventually) the
5-attempt guarantees. Fixed both with `select_for_update()` + `transaction.atomic()` around the whole
check. `token_revocation.py`'s `revoke_all_sessions()` had the same shape for `PTUser.token_version`
(milder consequence — a lost bump doesn't break the revocation itself, just possibly leaves it based
on a stale starting count) — fixed with an `F("token_version") + 1` in-DB expression instead, since
this one didn't need a decision to be made based on the read value the way the other two did.

**5. Two stale doc/comment drifts, corrected in place** (no behavior change, just true-again
comments): `config/settings.py`'s `SIMPLE_JWT` comment used to claim `ROTATE_REFRESH_TOKENS`/
`BLACKLIST_AFTER_ROTATION` require `rest_framework_simplejwt.token_blacklist` in `INSTALLED_APPS` —
never true here, and a live footgun, since enabling that app for real reproduces the exact
`OutstandingToken.user must be a "User" instance` crash this file already documents elsewhere (see
"A real bug, found and fixed, 2026-09-04" above) — corrected to describe the actual
`RevokedRefreshToken`-based mechanism. `apps/services/sync_trigger.py`'s module docstring used to say
match commands "write no SyncRun row of their own and have no internal error handling" — also
already false by the time this audit found it (`match_hrs.py`/`match_achhad.py`/`match_vapi.py` each
document their own 2026-09-04 fix for exactly this) — corrected to point at those three files
instead of re-describing a gap that no longer exists.

**6. This file's own "Known gaps" section had drifted too** — see the `style-src 'unsafe-inline'`
entry immediately below, which claimed a fix already shipped 2026-09-08 was "still not dropped."
Left as a lesson in place rather than silently deleted: a "Known gaps" list is exactly the kind of
section that quietly goes stale as fixes land elsewhere in the file without this list being
revisited — check the section/file a gap entry points at directly before trusting the entry's own
claim, the same way this audit did.

**7. `PTCookieJWTAuthentication.authenticate()`'s exception handling was narrowed** (not a redesign,
just a tightened catch) — it used to catch bare `Exception` around cookie JWT validation, silently
treating a genuine bug (e.g. a DB error inside `get_user()`) the same as an ordinary invalid/expired
cookie, logged only at `DEBUG`. Narrowed to catch `(InvalidToken, AuthenticationFailed)`
specifically at `DEBUG`, with a separate `except Exception` that logs at `WARNING` with a full
traceback before still falling through to the Bearer-header attempt (raising outright here would
incorrectly turn an ordinary bad cookie into a hard error for browser clients that never send a
Bearer header anyway) — the behavior for a genuinely bad cookie is unchanged, an actually-unexpected
failure is just no longer invisible in `logs/app.log`.

**8. Dedup guard added for the two scheduled reports with a real fixed cadence** (follow-up the same
day, after asking the project owner how the third report should behave — see below). Neither
`send_daily_consumption_reports()` nor `send_monthly_consumption_reports()`
(`apps/services/consumption_report.py`) had any protection against the external cron-job.org
scheduler double-firing its trigger endpoint (a network retry, or a misconfigured overlapping
schedule) — every admin would get a duplicate email for the same day/month with nothing to stop it.
Fixed with `ReportSendLog` (migration `0045`, own model docstring has the full design) — one row per
`(report_type, plant, period_key)` actually sent, unique-constrained; each plant claims its row via
`get_or_create()` **before** building/sending (closes the actual race, not just a "check then send"
that a double-fire could still slip through), and releases the claim again (deletes the row) if
building/sending then genuinely raises, so a real transient failure stays retryable rather than
permanently burning that day's/month's slot. **Deliberately NOT applied to the Plant Data Correction
(mismatch) report** (`send_plant_mismatch_reports`, `apps/services/plant_mismatch_report.py`) — asked
the project owner directly rather than guessing, since that report has no fixed cadence by design
("no built-in cadence; set whatever interval you want on the external scheduler" — see "Outgoing
email inventory" above); a once-per-day lock could block an intentional same-day re-trigger. Answer:
leave it exactly as-is, only the daily/monthly reports (which do have a real fixed cadence) get the
guard. Regression tests: `apps/services/tests/test_consumption_report.py`'s
`test_calling_twice_the_same_day_does_not_resend`/`test_calling_twice_for_the_same_month_does_not_resend`/
`test_a_failed_build_does_not_permanently_block_retry`.

**Everything else each review checked came back clean** — no migration drift, no per-plant field
mismatches beyond CLAUDE.md's own documented ones, no injection/XSS findings, the `_MAX_DIFF_PCT`
clamp and `sync_utils.unchanged()`'s `ROUND_HALF_UP` quantization both applied consistently
everywhere they should be, and the internal report-trigger endpoints' shared-secret check already
uses `hmac.compare_digest()` (constant-time), not a timing-unsafe `==`.

## Known gaps (confirm still true before treating as blocking)

- ~~**`style-src 'unsafe-inline'` in `config/security_headers.py`'s CSP — still not dropped**~~ —
  **built, 2026-09-08.** This bullet was already stale by the time a full-codebase audit
  (2026-09-10) found the drift: `config/security_headers.py`'s own "CSP notes" docstring documents
  this was removed the same day it's dated, and `_build_csp()`'s actual output
  (`"style-src 'self' https://fonts.googleapis.com"`) confirms no `'unsafe-inline'` is present —
  confirmed directly against the live code, not just the docstring's own claim. See that file's
  docstring for exactly what replaced every inline `style="..."` attribute (brand.css utility
  classes for static values, `data-*` attributes + a small JS pass for the genuinely dynamic ones —
  `shared.js`'s `applyDynamicStyles()`/`admin-page.js`'s `renderBarList()`) and every page's inline
  `<style>` block (extracted to its own `css/<page>-page.css` file). If a "Known gaps" entry here
  ever looks stale again, check the file/section it points at directly before trusting this list —
  this one had drifted for at least a few days before being caught.
- ~~**Import PO parsing for RTP-Vapi's own BOE/customs-shaped CSV**~~ — **built**, 2026-09-04, see
  roadmap item 3 above. All three plants' Import CSVs are parsed and live now.
- **Licenses (Advance Authorisation tracking)** — deferred to v2, see the roadmap section above.
  Confirmed still true as of 2026-09-04: no `License`/`AdvanceLicense` model exists anywhere in
  `apps/core/models.py`.
- ~~**No scheduling**~~ — **built, 2026-09-05 (Snapshot Pipeline Rebuild, Phase B)**. Admin-triggered
  `sync-trigger` endpoints still exist unchanged, but every plant's sync+match pipeline now also
  runs on its own: `apps/services/sync_trigger.py`'s `run_daily_sync_all_plants()`, scheduled as a
  real `django_q.models.Schedule` row by `manage.py ensure_schedules` (idempotent, wired into
  `render.yaml`'s `buildCommand` and `docker-entrypoint.sh`, see that command's own module
  docstring). Per-plant skip (not queue-behind) if a manual refresh is already mid-flight for that
  plant. **Cadence changed twice since**: once daily → every 3 hours around the clock (2026-09-07)
  → **9:00 AM through 8:00 PM IST, once an hour, 12 runs/day (2026-09-08, project owner: "I want the
  first sync to happen at 9:00 am daily and thereafter every hour and last sync at 8:00 pm")** -
  `Schedule.CRON` with `cron="0 9-20 * * *"`, requiring the `croniter` package (added as a real
  dependency this pass - pure-Python, no native build step). Same idempotent-drift-correction
  command, same row (`name="daily-sync-all-plants"`, kept stale on purpose - see the command's own
  module docstring for why renaming it would risk a duplicate schedule row). `next_run` is
  deliberately never reset on a cadence change either (same policy as the earlier daily→3-hourly
  migration) - the very next fire after a cadence change may land at a stale timestamp left over
  from the previous cadence, then self-corrects to the new cron boundaries from that point on.
- ~~**Daily `*StockSnapshot` capture is a side effect of `sync_stock`/`sync_achhad_stock`/
  `sync_vapi_stock` only**~~ — **built, 2026-09-05, same pass as directly above** (this was always
  tied to the scheduling gap, exactly as this bullet predicted). A day where qcluster was down still
  has no snapshot (deliberately not backfilled - see stock_identity.py/the Snapshot Pipeline Rebuild
  plan's "deliberately not building" note), but that gap is now visible: `sync-status`'s
  `lastSnapshotDate`/`snapshotGapDays` fields, surfaced as a badge in the dashboard's sync-status
  banner when `snapshotGapDays > 1`.
- **Superseded again (2026-09-04): "Imports has no MIR-equivalent reconciliation" was actually
  wrong, not just half-true — see "Import PO <-> MIR reconciliation" below.** The premise behind
  both this bullet's earlier versions (and the roadmap section's original "Import PO parsing for
  RTP-Vapi" framing) was that imports had no MIR-equivalent data source on Drive at all. The
  project owner corrected this directly: **the MIR and Stock (RM) xlsx files are the same files
  for domestic and imports, in each plant's Drive folder — only the PO-source CSV differs.**
  `apps/services/import_flags.py`'s read-time reconciliation layer (shipment-stage rollup,
  PO-vs-BOE qty discrepancy, delivery-date status, partial-delivery, the 7 data-quality flags)
  described in earlier revisions of this bullet is still real and still there, but it was never the
  full story — it's what could be computed with no MIR at all, back when that was believed to be
  the constraint. `main.js`'s Import KPI row no longer has any `disabled: true` "Awaiting MIR"
  cards — Material Inwarded, Qty Discrepancies (BOE vs MIR), and Rate Discrepancies are all real
  now too.
- **Role differentiation is real; plant scoping on reads was tightened 2026-09-05 (hardening
  pass).** Every read endpoint (PO list, materials, stock trend, sync status, both Domestic and
  Import) is still plain `IsAuthenticated` on ROLE (any role can read, by design) — that part is
  unchanged. What changed: reads now also respect `PTUser.plants` scoping via
  `apps/api/permissions.py`'s `user_can_access_plant()` (the same underlying check
  `user_can_edit_plant()` already used for writes — the two are no longer separate concepts, just
  separate call sites). Previously an editor explicitly scoped to `plants=["hrs"]` could still read
  every other plant's dashboard, which was inconsistent with what `PTUser.plants`' own docstring
  already promised. **Deliberately low blast-radius**: an empty `plants` list still means "all
  plants" (unchanged default), so this only affects accounts an admin has already explicitly
  scoped — confirmed directly in `apps/api/tests/test_read_endpoint_plant_scoping.py`, which
  asserts an unscoped account sees zero behavior difference. Domestic single-plant read endpoints
  (`_domestic_base.py`) 403 outright for an out-of-scope plant; Import's cross-plant
  `purchase_orders`/`sync_status` (`imports_views.py`) silently narrow the combined result instead
  (same "you see less, not an error" shape the endpoint already had for a plant with zero rows);
  `purchase_order_detail` 404s (not 403) for an out-of-scope plant, matching its existing
  unknown-plant-segment 404 rather than confirming a PO exists. The write paths gated at `IsEditor`
  are `correct_field` (Domestic and Import), `dismiss_po_mir_match`/`dismiss_mir_stock_match` (see
  "Dismiss/override a flagged match" above), and the Import PO `correct_field` in
  `imports_views.py`. `IsAdmin` gates `sync_trigger` and every `users_views.py` endpoint (including
  the password-reset field on `PATCH /api/auth/users/<id>`, see "In-app user management" above).
  If a new read OR write endpoint is added, gate both role (`IsAuthenticated`/`IsEditor`/`IsAdmin`)
  and plant (`user_can_access_plant()`/`user_can_edit_plant()`) the same way — don't leave a new
  endpoint unscoped by plant just because it's "only a read."
- **No automated tests for the Drive-sync/parse/matching pipeline itself** — still explicitly
  deferred; verification there stays manual/smoke-level (real parser runs against real files, real
  sync/match runs against a real Postgres, direct inspection of the rendered dashboard). This is
  narrower than it used to read: the auth flow, inline-edit endpoints, dismiss/override endpoints,
  password reset, and the `stockMatched` field all now have real integration test coverage
  (`apps/api/tests/`) - what's still untested is specifically the DB-touching `sync_*`/`match_*`
  management commands and the actual Drive API calls, not "this app has no tests."
