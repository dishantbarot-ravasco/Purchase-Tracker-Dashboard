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
uv run pytest                              # test suite (real Postgres, no mocking - see below)

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

## Known gaps (confirm still true before treating as blocking)

- ~~**Import PO parsing for RTP-Vapi's own BOE/customs-shaped CSV**~~ — **built**, 2026-09-04, see
  roadmap item 3 above. All three plants' Import CSVs are parsed and live now.
- **Licenses (Advance Authorisation tracking)** — deferred to v2, see the roadmap section above.
  Confirmed still true as of 2026-09-04: no `License`/`AdvanceLicense` model exists anywhere in
  `apps/core/models.py`.
- **No scheduling** — deferred to v2, see the roadmap section above. Every sync/match command still
  only runs when invoked manually or via the admin-triggered `sync-trigger` endpoints.
- **Daily `*StockSnapshot` capture is a side effect of `sync_stock`/`sync_achhad_stock`/
  `sync_vapi_stock` only** — there's no independent daily trigger, so a day where a plant's stock
  sync doesn't run has no snapshot for that day and nothing backfills it. Tied to the scheduling
  gap above - fixing that would fix this too, as a side effect, not a separate build.
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
- **Role differentiation is now real but narrow** — every read endpoint (PO list, materials, stock
  trend, sync status) is still plain `IsAuthenticated` (any role) by design; the write paths gated
  at `IsEditor` are `correct_field` (Domestic and Import), `dismiss_po_mir_match`/
  `dismiss_mir_stock_match` (see "Dismiss/override a flagged match" above), and the Import PO
  `correct_field` in `imports_views.py`. `IsAdmin` gates `sync_trigger` and every
  `users_views.py` endpoint (including the password-reset field on `PATCH /api/auth/users/<id>`,
  see "In-app user management" above). There's no third tier of endpoint waiting on role gating
  right now - if a new write endpoint is added, gate it the same way, don't leave it at
  `IsAuthenticated` by default.
- **No automated tests for the Drive-sync/parse/matching pipeline itself** — still explicitly
  deferred; verification there stays manual/smoke-level (real parser runs against real files, real
  sync/match runs against a real Postgres, direct inspection of the rendered dashboard). This is
  narrower than it used to read: the auth flow, inline-edit endpoints, dismiss/override endpoints,
  password reset, and the `stockMatched` field all now have real integration test coverage
  (`apps/api/tests/`) - what's still untested is specifically the DB-touching `sync_*`/`match_*`
  management commands and the actual Drive API calls, not "this app has no tests."
