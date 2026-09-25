# Architecture, config, and data models

This doc covers how the Django project is laid out (`config/`, `apps/core`, `apps/api`,
`apps/services`), how settings and middleware are wired, and the shape of every model in
`apps/core/models/`. It also records the schema decisions that look odd until you know the
incident behind them: separate model classes per plant, shared tables where nothing is
plant-shaped, natural-key stock lots, and per-plant decimal precision.

Related docs: [data-sync.md](data-sync.md) (parsers, sync commands, change detection),
[matching-engine.md](matching-engine.md) (what fills the `*Match` tables),
[api-and-features.md](api-and-features.md) (the routers), [consumption.md](consumption.md)
(the consumption ledger tables), [auth-security-email.md](auth-security-email.md) (auth models,
middleware security rationale), [testing-deployment.md](testing-deployment.md) (CI, Docker,
Render).

### Layering

- **`apps/core`** - models, migrations, and the thin Django plumbing around them: the Admin
  registrations (`admin.py`), the custom system checks (`checks.py`), the audit-log model and its
  `log_pt_action()` helper (`audit_log.py`), and every management command
  (`apps/core/management/commands/`). No views and no business logic. **`apps/core/models/` is a
  package** (split from one 3,255-line `models.py` on 2026-09-23): `hrs.py` / `achhad.py` /
  `vapi.py` hold each plant's PO/MIR/Stock/match models, and `sync.py`, `review.py` (corrections,
  dismissals, manual pins, category reference, flags, match reviews), `auth.py`, `reports.py`,
  `ledgers.py` and `consumption.py` the shared ones. `__init__.py` re-exports every class -
  **always import from `apps.core.models`**, never a submodule. The split moved no model and
  changed no schema (`makemigrations --check`: no changes; Django registers models by app label,
  not module path, so migrations still name them `core.<Model>`). A new model goes in the file for
  its plant or concern and gets added to `__init__.py`'s imports **and** `__all__`.
- **`apps/api`** - every HTTP-facing view. Plant and feature views live under
  `apps/api/routers/*_views.py`; the auth views (`auth_views.py`), the health/readiness views
  (`views.py`), the DRF exception handler, permissions and the JWT auth backend sit at the
  `apps/api/` root. Thin: build response dicts from the ORM or call into `apps/services`. No
  business logic beyond what a view needs.
- **`apps/services`** - Drive access (`google_client.py`), per-plant parsers (`parsers/`), the
  matching engines (`matching.py` / `matching_achhad.py` / `matching_vapi.py` over the shared
  `matching_core.py`), change-detection (`sync_utils.py`), the licence<->import join
  (`license_links.py`), the consumption ledger, the report builders, and the auth-adjacent
  services (`device_service.py`, `otp_service.py`, `email_service.py`, `password_service.py`,
  `token_revocation.py`, `security_alerts.py`).

Management commands get their logic from `apps.services` and touch `apps.core` only through
`apps.core.models`. If you find yourself writing `from apps.core.parsers ...` or
`apps.core.matching ...`, that is the old pre-auth-pass import path - it is `apps.services.*` now.
Several model comments and docstrings still cite those old paths (`apps/core/parsers/achhad_mir.py`,
`apps/core/matching_achhad.py`, `apps/core/parsers/po_csv.py`); read them as `apps/services/...`.

Several modules are **deliberately dependency-free** (no Django imports at all), so they unit-test
as plain Python and are safe to import from a migration's `RunPython`: `parsers/common.py`,
`validation.py`, `stock_identity.py` (imports only `parsers/common.py`), `arithmetic_checks.py`,
`consumption_engine.py`. Migration `0023` really does import `stock_identity` from `RunPython`.
Keep them that way - the DB-touching counterpart lives in its own module (e.g. `data_quality.py`
is the DB layer over `arithmetic_checks.py`). `stock_consumption.py` (superseded, dead code) is
**not** on this list: it imports `django.utils.timezone`.

### Per-plant models, not a shared schema - deliberate, don't "fix" it

`apps/core/models/` has fully separate model classes per plant (`HRSDomesticPurchaseOrder` /
`RTPAchhadDomesticPurchaseOrder` / `RTPVapiDomesticPurchaseOrder`, and their `*POLineItem` /
`*MIREntry` / `*RMLot` / `*RMSnapshot` / `*POMirMatch` / `*MirStockMatch` siblings, plus the
`*Import*` trio) rather than one shared schema with a `plant` discriminator column.

This was an explicit instruction, re-confirmed by inspecting the real files: **each plant's
MIR/Stock spreadsheets have genuinely different column layouts**, not just different values.

- **HRS MIR** (`'RAW MATERIAL'` tab, `parsers/mir.py`'s `SHEET_NAME`) carries Purchase Order
  No./Date plus SAP P.O. No./Date and a SAP GRN Number column; `HRSMIREntry` stores
  `po_number_raw` and `sap_grn_number`. **Achhad MIR** (`'R.M. '` tab - note the trailing space,
  `parsers/achhad_mir.py`) has exactly one PO-number/PO-date pair and no GRN column at all, so
  `RTPAchhadMIREntry` has no `sap_grn_number`. Achhad's MIR also spells out full state names, so
  its `state` is `max_length=50` where HRS's is 10.
- **HRS Stock** is one row per **(material, vendor) lot** - the same material appears many times
  from different vendors at different rates; `HRSRMLot.party_name` captures that. **Achhad Stock**
  is one row per **material, full stop** - no vendor column exists on that sheet, so
  `RTPAchhadRMLot` has no vendor field and Achhad's MIR<->Stock match can only gate on material
  description. That is a materially weaker guarantee, documented as such in `matching_achhad.py`'s
  module docstring, not treated as equivalent. Achhad's lot instead carries sheet-specific columns
  (`msl`, `physical_stock`, `zone`, `category_sr_no`, and a `category` backfilled from
  section-divider rows), and a dated daily movement matrix (`RTPAchhadRMDailyMovement`) no other
  plant has.
- **Tab names:** HRS Stock keeps a fixed `'Stock'` tab. Achhad's single tab is renamed every month
  (e.g. `'Aug 26-27'`), so `parsers/achhad_stock.py` reads `wb.sheetnames[0]` instead of matching a
  literal name. Vapi is back to a fixed `'Stock'` name; its rows 1-5 are a document-control title
  block and the header is row 6, the same header row HRS's `parsers/stock.py` uses (`HEADER_ROW = 6`
  in both) - see `parsers/vapi_stock.py`'s docstring for the exact layout.
- **Vapi MIR is the most structurally different**: no `Net`/discount columns at all (straight from
  `RATE` to `TAXABLE VALUE`), GST as one overall rate column plus three amount-only IGST/CGST/SGST
  columns (no per-component rate columns), a single TCS amount instead of a rate+amount pair, extra
  columns neither other plant has (`park_invoice_no`, `post`, `post_no_correction`, `item_code`,
  `date_sent_to_office`, `date_sent_to_ho`), and its own `SAP P.O` field (`sap_po_number`) was
  **100% blank** across ~1,330 real rows checked - re-confirmed 2026-09-18, still 0 usable values,
  so `sap_po_number` remains dead weight for matching. **Its other PO column is no longer blank,
  though**: `po_number_raw` reads the `'PURCHASE ORDER'` column the plant added 2026-09-11, and
  measured 2026-09-19 across 1,489 rows it is 97.5% populated but **29.0% (432 rows) names an order
  the master CSV holds** - most of the rest is the literal word `VERBAL` (536 rows), or several
  orders joined by a hyphen (334 rows, see [matching-engine.md](matching-engine.md) on
  `_MULTI_PO_HYPHEN_RE`). That is the single biggest reason Vapi's match rate jumped. Don't
  conflate `sap_po_number` and `po_number_raw`.
- **Vapi Stock is a real shared multi-plant ledger**, not exclusively Vapi's: confirmed `PLANT`
  values `RTP-1` (145 rows), `HRS` (20), `RTP-2` (3). `RTPVapiRMLot.plant_tag` captures this without
  filtering, the same design HRS's own `location_tag` uses. Unlike Achhad, Vapi's Stock sheet
  **does** have a real vendor column (`Supplier Name` -> `supplier_name`, confirmed genuine - only
  16 of 168 rows echo the `PLANT` value), so Vapi uses HRS's stronger (material, vendor) gate. Its
  column E was renamed from 'Sub Category' to 'Batch No.' on 2026-09-12, so `batch_no` is written
  and `sub_category` is no longer written by the sync.

Forcing all three into one table would mean a pile of always-null columns, and - worse - would
tempt matching logic that silently assumes a field exists uniformly when it doesn't. Each plant
gets its own models, parsers, matcher, router, and sync commands.

The tables that **are** shared carry a `plant` column (`SyncRun.Plant` choices) and hold nothing
that comes from a plant's spreadsheet columns: `SyncRun` (a generic job log), the correction/
dismissal/pin/review tables in `review.py`, and the consumption ledger in `consumption.py`. That
is the test for a new table: if its columns come from a sheet whose layout differs per plant, it is
per-plant; if it is a log, a human decision, or a uniform derived output, it is shared.

**The PO master CSV format is identical across all three plants** (confirmed byte-for-byte on real
files), so `parsers/po_csv.py` is reused as-is; only the target model class and Drive file title
differ per command. **The Import PO CSV format is likewise shared** across all three (BOE number,
bill of lading, exchange rate, dual PO/BOE quantities, licence numbers) - one shared
`parsers/import_po_csv.py`. It is only MIR and Stock that genuinely differ per plant.

**`parse_po_csv()`'s header check and its row lookups must use the same normalization.** Four
`EXPECTED_HEADER` columns (`"HSN "`, `"Delivery Date "`, `"Payment Terms "`, `"Currency "`) carry a
trailing space because that is what the live file's header literally looked like when the parser
was built. The header-equality check strips whitespace before comparing, so it tolerates a file
that has since lost one of those trailing spaces - but `csv.DictReader` keys each row off the
file's *actual* header text, not `EXPECTED_HEADER`'s. A row lookup by the literal
`row["Payment Terms "]` then raised a bare `KeyError` the moment HRS's live master CSV had exactly
this drift (2026-09-17, a routine resave in Excel/Sheets, invisible to anyone looking at the file),
which `sync_po_csv`'s blanket `except Exception` turned into
`sync_po_csv: failed - 'Payment Terms '` and took down that day's whole HRS PO sync. Every row is
now re-keyed by its stripped header name once, right after validation
(`row = {k.strip(): v for k, v in raw_row.items()}`), so the row lookups can never again disagree
with what the header check already accepted - see `parse_po_csv()`'s own comment and
`test_po_csv_parser.py`.

### Domestic router de-duplication

The per-plant *model* decision above does **not** extend to the view layer. That part has nothing
to do with real schema differences and was previously copy-pasted near-verbatim across
`hrs_views.py` / `vapi_views.py` / `achhad_views.py`.

HTTP-layer logic now lives once in `apps/api/routers/_domestic_base.py`: a `_PlantConfig`
dataclass (model classes, `run_full_match` reference, plant key, material-field allow-lists, and
the lot-attribute names that genuinely vary - `lot_rate_field` / `lot_code_field` /
`lot_vendor_field`) plus `make_*` factory functions each plant's router calls once at import time
and re-exports under the original name. **Every endpoint's URL and response shape is unchanged.**

Achhad's genuine divergence (`rate`/`msl` instead of `basic_rate`/`sub_category`/`uom`, no vendor
field, `sap_code` instead of `sap_item_code`/`hsn_code`) is handled entirely through `_PlantConfig`
values, never by branching inside `_domestic_base.py` itself. Keep it that way.

This was done as a strangler-fig migration - one plant at a time, characterisation tests written
against the unmodified originals first to prove behavioural equivalence - specifically because
Vapi/Achhad had no direct test coverage of their own beforehand.

`imports_views.py` is intentionally **not** part of this migration; it already has its own
shared-config pattern via a `_PLANTS` dict. It does import `_field_warning`/`_serialize` from
`_domestic_base` (identical and field-set-independent, safe to share), but keeps its own
`_coerce_value` **deliberately**: its `_DATE_FIELDS`/`_DECIMAL_FIELDS` are a strict superset
(BOE/exchange-rate fields domestic plants don't have) and `_domestic_base._coerce_value` reads
module-level field sets, so sharing it as-is would silently reject valid import-only fields.

### Decimal precision conventions

**GST rate fields need 3 decimal places at HRS/Achhad, 2 at Vapi - check the unit per plant.**
Every `*_rate_pct` on `HRSMIREntry`/`RTPAchhadMIREntry` (`discount_rate_pct`, `tax_rate_pct`,
`cgst_rate_pct`, `sgst_rate_pct`, `tcs_rate_pct`) is `decimal_places=3` because those sheets store
a **fraction** - a 2.5% GST rate is the literal cell value `0.025`, and 2 places silently truncates
it to `0.02`. `RTPVapiMIREntry.gst_rate_pct` is deliberately `decimal_places=2` because Vapi stores
a **whole percentage** (`18.00` = 18%), confirmed by cross-checking
`Taxable Value x GST% / 100 = IGST` against a real row. **Don't "fix" Vapi to match the other two -
they are genuinely different units.** Check the source before picking `decimal_places` for any new
rate-like field.

**`*_diff_pct` columns need a clamp.** `DecimalField(max_digits=6, decimal_places=2)` tops out at
`9999.99`. A pathological pair (a tiny reference value against a much larger actual - e.g. a rate
typo'd as a fraction of the real one) can exceed that and raise a DB error on insert instead of
recording "very large diff, flag it". The one shared `_diff_pct()` in `matching_core.py`, which all
three plants run, clamps to `_MAX_DIFF_PCT = Decimal("9999.99")` (and returns the clamp, not an
infinite percentage, when the reference is zero and the actual is not). Keep the clamp.

Other width conventions the models follow consistently: quantities `max_digits=14,
decimal_places=3`; unit rates and prices `14, 4`; currency amounts `16, 2`; exchange rate `10, 4`;
`match_score` `5, 4`; `field_coverage` `3, 2`.

### Stable lot identity

`*RMLot` rows are keyed on a **natural key** (`stock_identity.lot_natural_key()`:
`<code or normalized description>|<normalized vendor>`, with `#N` appended to disambiguate a
genuine duplicate), not on `source_row_ref` (the openpyxl row index). A row number is not an
identity - inserting one row mid-sheet shifts every row below it, and the next sync would re-label
an existing lot as whatever material now occupies its old row while that lot's `*RMSnapshot`
history stays attached by foreign key, silently splicing two materials' histories together.
Migrations `0022`-`0024` carried out the swap (add the column, backfill it with the same
`OccurrenceCounter` the syncs use - raising on any collision rather than merging - then swap the
unique constraint). An explicit material code wins over the description, since a SAP/HSN code
survives a description being reworded in the sheet (which happens).

Per plant, the code and vendor fields feeding the key are: HRS `sap_item_code` + `party_name`;
Achhad `sap_code` + no vendor (so two lots of one material are separated only by the occurrence
suffix); Vapi `hsn_code` + `supplier_name`. The `#N` suffix is assigned in sheet row order by
`OccurrenceCounter`, one instance per sync run, so it is stable only as long as the relative order
of genuine duplicates holds. A blank key is never upserted (callers skip it), and the unique
constraint is conditional on `natural_key > ''` for that reason. `source_row_ref` survives on lots
as a diagnostic only.

This applies to stock lots only. **MIR entries still key on `source_row_ref`** (unique per plant)
and survive a row shift by being deactivated rather than deleted; that is also why a manual MIR pin
names a MIR *number* rather than a row (see [api-and-features.md](api-and-features.md)). And the
consumption ledger deliberately does **not** use `natural_key` (see
[consumption.md](consumption.md)).

## File reference

### config/settings.py

The single settings module, driven by environment variables (loaded from `.env` via
`python-dotenv`). Key blocks, top to bottom:

- **Core**: `SECRET_KEY` (`DJANGO_SECRET_KEY`, insecure dev default), `DEBUG` (`DJANGO_DEBUG`,
  default false), `ALLOWED_HOSTS` (comma-separated `DJANGO_ALLOWED_HOSTS`). `TIME_ZONE =
  "Asia/Kolkata"`, `USE_TZ = True`. `DEFAULT_AUTO_FIELD` is `BigAutoField`.
- **Sentry**: initialised only when `SENTRY_DSN` is set. `SENTRY_ENVIRONMENT` and
  `SENTRY_TRACES_SAMPLE_RATE` use `os.environ.get(...) or default` rather than a `.get()` default,
  because `.env.example` ships those keys present-but-blank and a blank value would otherwise win
  (or crash `float("")`). `LoggingIntegration` turns every existing ERROR log into an event;
  `send_default_pii=False`.
- **Apps**: `rest_framework`, `rest_framework_simplejwt`, `django_q`, `apps.core`, `apps.api`. No
  `django-cors-headers` (same-origin frontend) and never `rest_framework_simplejwt.token_blacklist`
  (see [auth-security-email.md](auth-security-email.md)).
- **Middleware order** (outermost first): `SecurityMiddleware`, `SecurityHeadersMiddleware`
  (**must** precede WhiteNoise), `WhiteNoiseMiddleware`, `SelectiveGZipMiddleware` (after
  WhiteNoise so static files keep their own compression and ETags), `ApiNoStoreMiddleware`,
  sessions, common, `AdminOnlyCsrfMiddleware` (instead of the global `CsrfViewMiddleware`), auth,
  messages, clickjacking. When `DEBUG` is on, `NoCacheMiddleware` is inserted at index 1.
- **Database**: Postgres only. `DATABASE_URL` via `dj_database_url` (`conn_max_age=600`,
  health checks), else discrete `PG*` vars.
- **Cache**: `DatabaseCache` in table `pt_cache_table` (needs `createcachetable` once);
  `LocMemCache` under pytest, detected by `"pytest" in sys.modules` (not `sys.argv`, which never
  contained a bare `test`). Nothing uses `cache_page`; the comment records why it must never sit
  above a permission check.
- **`Q_CLUSTER`** (django-q2 ORM broker on the same DB): 2 workers, `timeout` 900, `retry` 1200
  (`retry` must exceed `timeout` or a running sync is re-queued), `catch_up: False`.
- **Static**: `WHITENOISE_ROOT` and `STATICFILES_DIRS` both `frontend/`, default
  `StaticFilesStorage` (the old `STATICFILES_STORAGE` line was a no-op on Django 5.2 and was
  deleted), `WHITENOISE_ADD_HEADERS_FUNCTION = frontend_cache_headers`. `TEMPLATES` DIRS is
  `frontend/` so `/` can render `index.html`.
- **Logging**: console plus rotating `logs/app.log` (10 MB x 5); `LOGS_DIR` is created at import
  time (the one runtime write under `BASE_DIR`). `apps.*` loggers are DEBUG when `DEBUG`, else INFO.
- **DRF**: `PTCookieJWTAuthentication`, default `IsAuthenticated`, the custom exception handler,
  JSON renderer only (no Browsable API), throttle scopes `anon` 60/min, `user` 200/min, `login`
  5/min, `otp_verify` 10/min, `sync_trigger` 10/min, `admin_write` 30/min,
  `password_change_request` 5/min.
- **SimpleJWT**: access 12 h, refresh 30 days, HS256 signed with `JWT_SIGNING_KEY` (defaults to
  `SECRET_KEY`; the deploy check warns), rotate + "blacklist" after rotation (backed by the app's
  own `RevokedRefreshToken`), `USER_ID_FIELD`/`USER_ID_CLAIM` `user_id`, custom
  `USER_AUTHENTICATION_RULE`.
- **App config values**: `SAFECUBE_API_KEY`, `REPORT_CRON_SECRET` (empty means every cron endpoint
  refuses with 503), `MISMATCH_REPORT_PLANT_HEADS_ENABLED` (default false),
  `HEALTH_SYNC_STALE_HOURS` (default 26), Google service-account vs OAuth-client settings (two
  separate concerns), every Drive file title and folder id per plant, `RODTEP_FOLDER_ID`,
  `ADVANCE_LICENSE_FILE_ID`, `ALLOWED_EMAIL_DOMAIN` (default `ravasco.com`).
- **Session/cookies**: DB sessions, `SESSION_SAVE_EVERY_REQUEST = True` (needed for the OAuth PKCE
  redirect), 30-minute `SESSION_COOKIE_AGE`; `pt_access` cookie name and `Secure` flags all tied to
  `not DEBUG`. `AUTHENTICATION_BACKENDS` is `PTUserBackend` then `ModelBackend` (for Django Admin).
- **Email**: SMTP from `SMTP_*` vars; port 465 means SSL, 587 means STARTTLS; `EMAIL_TIMEOUT = 10`.
- **Security**: outside `DEBUG`, trusts `X-Forwarded-Proto`, SSL redirect, one-year HSTS with
  subdomains and preload. Under pytest `SECURE_SSL_REDIRECT` is forced off (CI runs with
  `DEBUG=False`). `SILENCED_SYSTEM_CHECKS = ["security.W003"]` because CSRF is admin-scoped on
  purpose.

### config/settings_dev_sqlite.py

`from config.settings import *` with `DATABASES` pointed at `dev_smoke_test.sqlite3`. For smoke-
testing sync/match commands without Postgres, via `DJANGO_SETTINGS_MODULE`. Never run the test
suite on it: SQLite loses Decimal scale in `SUM()` and produces false failures (see
[testing-deployment.md](testing-deployment.md)).

### config/middleware.py

Four middleware classes and one WhiteNoise hook.

- `frontend_cache_headers(headers, path, url)` - WhiteNoise add-headers hook. Sets
  `Cache-Control: no-cache, public` on `.html`/`.js`/`.mjs`/`.css` so a browser revalidates every
  load instead of running a stale script against a retired endpoint for WhiteNoise's default 60 s.
- `NoCacheMiddleware` - DEBUG only. Stamps `no-store, no-cache, must-revalidate, max-age=0` (plus
  `Pragma`/`Expires`) on every response so an edited frontend file is never masked by cache.
- `AdminOnlyCsrfMiddleware(CsrfViewMiddleware)` - runs Django's unmodified CSRF check only when the
  path starts with `/admin/`; every other request skips it. The JWT API would otherwise 403 on
  every unsafe method.
- `SelectiveGZipMiddleware(GZipMiddleware)` - compresses responses except under
  `UNCOMPRESSED_PATH_PREFIXES = ("/api/auth/", "/admin/")`, the two places a secret shares a body
  with reflected input (BREACH). A new endpoint returning a token or OTP in its body must live under
  `/api/auth/` or be added to that tuple.
- `ApiNoStoreMiddleware` - `setdefault("Cache-Control", "no-store")` on every `/api/` response, so
  a view's own header still wins. Invisible in dev because `NoCacheMiddleware` already stamps
  no-store; `test_api_no_store.py` tests the class directly for that reason.

### config/security_headers.py

`SecurityHeadersMiddleware` adds `X-Content-Type-Options`, `X-Frame-Options: DENY`,
`Referrer-Policy`, a restrictive `Permissions-Policy`, HSTS (production only, decided once at
init from `settings.DEBUG`), and the CSP from `_build_csp()`: `default-src 'self'`, `script-src
'self' https://cdn.jsdelivr.net` (Chart.js), `style-src 'self' https://fonts.googleapis.com`,
`font-src 'self' https://fonts.gstatic.com`, `img-src 'self' data: blob:`, `connect-src 'self'`,
`frame-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'self'`,
`form-action 'self'`, plus an optional `settings.CSP_EXTRA_DIRECTIVES` (not defined in settings
today). No `'unsafe-inline'` anywhere: a new inline `<script>`, inline handler or `style="..."`
attribute is silently blocked, and a new CDN origin must be added here or its script fails in every
browser. It must sit before WhiteNoise in `MIDDLEWARE` or static pages get no headers. The module
docstring holds the full history; rationale is in [auth-security-email.md](auth-security-email.md)
and [frontend.md](frontend.md).

### config/urls.py

Root URLconf: `admin/` (Django Admin, for bootstrap and read-only audit browsing), `api/`
(includes `apps.api.urls`), and `""` rendering `index.html` via `TemplateView`. Every other
frontend asset is served by WhiteNoise directly; this catch-all covers only `/`.

### config/wsgi.py

Production entrypoint (`gunicorn config.wsgi:application`). `setdefault`s
`DJANGO_SETTINGS_MODULE=config.settings`, so an already-set value (e.g. the SQLite override) wins.

### config/asgi.py

Standard ASGI entrypoint, unused (the app runs on gunicorn WSGI; no async views). Kept for tooling
and optionality; same `setdefault` behaviour as `wsgi.py`.

### config/\_\_init\_\_.py

Empty package marker.

### manage.py

Stock Django entrypoint; defaults `DJANGO_SETTINGS_MODULE` to `config.settings`. Run everything from
the repo root (`uv run python manage.py ...` locally, plain `python manage.py ...` inside the
container).

### apps/core/models/\_\_init\_\_.py

Re-exports every model class from the submodules and lists them in `__all__`. Its docstring holds
the package layout and the per-plant rationale. The HRS classes carry the full field-by-field
docstrings; the Achhad and Vapi equivalents say "same shape as HRS<Model>" and comment only real
differences, so read the HRS class first.

### apps/core/models/sync.py

- `SyncRun` - one row per pipeline step execution: `plant`, `source`, `status`, `started_at`,
  `finished_at`, `rows_seen`, `rows_changed`, `error_detail`; ordered newest first, indexed on
  `(plant, source, -started_at)`.
  - `SyncRun.Plant` - `HRS`, `RTP-VAPI`, `RTP-ACHHAD`, and `COMPANY` for the company-wide ledgers.
    These uppercase values are also the `plant` choices of every shared table; they are **not** the
    lowercase `hrs`/`achhad`/`vapi` keys that `PTUser.plants` and the frontend use.
  - `SyncRun.Source` - `po_csv`, `mir`, `stock`, `import_po_csv`, `match`, `rodtep`,
    `advance_license`, `consumption`. `match` and `consumption` exist because derive-from-DB steps
    look healthy when stale; `/api/health/ready` watches both. The dashboard's "PO Updated / MIR /
    RM" labels are display-only - there is no `rm` source.
  - `SyncRun.Status` - `success`, `partial` (some rows skipped, data current), `failed`.

### apps/core/models/hrs.py

HRS's ten models, the reference versions of each family.

- `HRSDomesticPurchaseOrder` - PO-level fields from the domestic master CSV; `po_number` unique;
  `is_old_format_template` marks pre-SAP `HRS/HO/26-27/xxx` POs; `is_active` is flipped off by the
  sync for orders the CSV no longer lists (retired, never deleted - see
  [data-sync.md](data-sync.md)); `synced_from_row_hash` drives change detection.
- `HRSDomesticPOLineItem` - FK `items` to the PO. `item_id` is neither unique nor required and the
  sync deletes and recreates every line on any change, so line items have no stable identity.
- `HRSMIREntry` - one MIR row. Unique on `source_row_ref`; `is_active` instead of delete so a
  shifted row does not cascade away its match history. `po_number_raw` is a tier-1 shortcut only.
  Rate fields at 3 decimal places (fractions). `plant_tag` is MIR's own plant column.
- `HRSRMLot` - one (material, vendor) lot; `natural_key` is the upsert identity (conditional unique
  constraint), `party_name` the vendor, `location_tag` the sheet's warehouse tag, `received`/
  `issued` cumulative period-to-date (see [consumption.md](consumption.md)).
- `HRSRMSnapshot` - one row per (lot, day), unique on both, indexed on `snapshot_date` and
  `(snapshot_date, stock_lot)`. The only place day-by-day stock history exists.
- `HRSPOMirMatch` - one-to-one on a domestic line item (`related_name="mir_match"`), FK to the
  primary `mir_entry`, and M2M `group_entries` holding every MIR row a multi-receipt match counted
  (empty for a one-row match; rebuilt each run). Carries `tier` (`po_number` / `material` /
  legacy `weighted`), `match_score`, the diff percentages, `qty_over_delivered` (tri-state
  direction), identification booleans (`material_matched`, `po_number_matched`, `vendor_matched`
  defaulting True, `manually_pinned` which is re-derived every run), the financial-check booleans
  (`qty_mismatched`, `rate_mismatched`, `data_mismatch`, `tax_type_mismatch`,
  `net_value_mismatched`, `taxable_value_mismatched`, `final_value_mismatched`, `uom_mismatch`),
  `field_coverage`, `severity` (`rounding`/`minor`/`material`), `is_flagged`, and the
  `dismissed_*` columns, which the matcher never writes so a dismissal survives re-matching.
- `HRSMirStockMatch` - one row per (MIR entry, lot) pair, unique on the pair (many-to-many by
  design); `material_matched`/`date_matched`, qty/rate/value diffs, mismatch booleans,
  `uom_mismatch`, dismissal columns. No tier or score.
- `HRSImportPurchaseOrder` / `HRSImportPOLineItem` - the import master CSV. PO-level fields are a
  subset of the domestic PO's; BOE, bill of lading, exchange rate, `tax_type`, landed
  `total_inclusive_value`, licence type/number and both `qty_as_per_po`/`qty_as_per_boe` live on the
  line item because one PO can clear customs in several partial shipments. `delivery_date_raw`
  keeps unparseable free text rather than guessing a date.
- `HRSImportPOMirMatch` - same columns as `HRSPOMirMatch` for an import line (FK `mir_entry` with
  `related_name="import_po_matches"`), compared on `qty_as_per_boe`. There is no import
  MIR<->Stock table: `*MirStockMatch` keys on the MIR entry and covers both.

### apps/core/models/achhad.py

The RTP-Achhad eleven, same families as HRS with these real differences:

- `RTPAchhadMIREntry` - no `sap_grn_number`, one PO pair, `state` widened to 50.
- `RTPAchhadRMLot` - one row per material, no vendor field; `rate` (not `basic_rate`), `sap_code`,
  `zone`, `msl`, `physical_stock`, `overall_sr_no`/`category_sr_no`, backfilled `category`.
- `RTPAchhadRMSnapshot` - carries `rate` instead of `basic_rate`.
- `RTPAchhadRMDailyMovement` - Achhad only. One row per (lot, day) with activity from the stock
  file's daily Recp./Issue matrix, unique on `(stock_lot, movement_date)`; re-syncs overwrite. It is
  the authoritative per-day source the consumption ledger reads for the days it covers.
- `RTPAchhadMirStockMatch` - no vendor gate by construction, so read a material match here as
  weaker evidence.
- The PO, line item, match and import models are field-for-field HRS's.

### apps/core/models/vapi.py

The RTP-Vapi ten. The module header comment documents the MIR and Stock layouts column by column.

- `RTPVapiMIREntry` - `po_number_raw` (the populated `'PURCHASE ORDER'` column) and the dead
  `sap_po_number` side by side; `sap_grn_number`, `park_invoice_no`, `post`,
  `post_no_correction`, `item_code`; no `net` or discount fields; `gst_rate_pct` at 2 decimal places
  (whole percentage); `igst_amt`/`cgst_amt`/`sgst_amt` amounts only; single `tcs_amt`;
  `others_with_gst`; `date_sent_to_office`/`date_sent_to_ho`.
- `RTPVapiRMLot` - `plant_tag` (multi-plant sheet), `supplier_name` (real vendor), `batch_no`
  (column E since 2026-09-12) with the frozen `sub_category`, `billing_on_plant`,
  `material_location`, `hsn_code` (the natural-key code).
- The PO, line item, snapshot, match and import models are field-for-field HRS's.

### apps/core/models/review.py

Cross-plant human decisions and reference data, all with a `plant` column.

- `ImportPOCorrection`, `DomesticPOCorrection` - append-only audit rows for inline edits
  (`plant`, `po_number`, `item_id` blank for PO-level, `field_name`, `old_value`, `new_value`,
  `reason`, `corrected_by`/`corrected_by_email`, `corrected_at`). The edit mutates the real row;
  these record it. Kept as two models because domestic and import schemas differ.
- `MaterialCorrection` - the same for stock-lot edits, keyed on `(plant, lot_id)` since each plant's
  lot table has its own id space.
- `*ImportPOMirMatch.receipt_share` / `mir_exchange_rate` / `exchange_rate_mismatched` and tier
  `boe_number` (migration `0060`) - Bill of Entry pairing, shared receipts and exchange-rate differences;
  see [matching-engine.md](matching-engine.md#import-po--mir-convert-currency-first).
- `FlagDismissal` - current dismissed state (upserted, not a log) for read-time-computed PO flags;
  unique on `(plant, po_number, flag_key)`. `flag_key` is opaque: a domestic flag label, or
  `<code>:<item_id>` for import flags.
- `ManualMirMatch` - a human pin of a PO line to a MIR **number** (blank means "leave unmatched");
  unique on `(plant, po_kind, po_number, item_ref)`. `po_kind` (`domestic`/`import`) is in the key
  because one PO number can exist in both tables. `item_ref` is the line's zero-based position by
  pk; `item_description` is a staleness tripwire.
- `MaterialCategoryReference` - company-wide category/subcategory lookup, unique on
  `normalized_description` (exact match after `normalize_material()`, never SAP code). Loaded by
  `load_material_category_reference`; editable in Django Admin.
- `DataQualityFlag` - one row per source-sheet arithmetic inconsistency (`source_type` +
  `source_id` generic pointer, `check_name`, `expected`, `actual`), unique on all four keys;
  `data_quality.py` deletes rows that stop mismatching.
- `MatchReview` - one reviewer verdict (`correct`/`incorrect`/`unsure`) on a match identified by
  `plant` + `match_type` (`po_mir`/`import_po_mir`/`mir_stock`) + plain `match_id` (not an FK). No
  unique constraint: latest verdict wins.

### apps/core/models/auth.py

- `PTUser` (`pt_users`) - the real account model, unrelated to `auth.User`; PK `user_id`; `email`
  unique; bcrypt `password_hash`; `role` (`admin`/`editor`/`viewer`); `plants` JSON list of
  lowercase plant keys (empty means all plants); lockout (`failed_login_attempts`, `locked_until`);
  `token_version` (the `ver` JWT claim, bumped to revoke every session). Declares
  `is_authenticated = True`/`is_anonymous = False` because it is not an `AbstractBaseUser`. Its
  docstring says `plants` has no bearing on reads; that is stale - reads are also scoped via
  `permissions.user_can_access_plant()` (see [auth-security-email.md](auth-security-email.md)).
- `OTPCode` (`pt_otp_codes`) - one active bcrypt-hashed code per email (unique), 10-minute expiry,
  attempt counter.
- `RevokedRefreshToken` (`pt_revoked_refresh_tokens`) - revoked `jti` (unique) with `expires_at`.
  The help text says rows are not purged; `manage.py prune_revoked_tokens` and
  `/api/internal/prune-revoked-tokens` now purge expired ones on demand (no fixed cadence).
- `TrustedDevice` (`pt_trusted_devices`) - SHA-256 of the `pt_device` cookie token (unique), device
  name, IP, timestamps; FK to `PTUser`.

### apps/core/models/reports.py

- `ReportSendLog` (`pt_report_send_log`) - claim-before-send dedup for scheduled emails, unique on
  `(report_type, plant, period_key)`. Types: `daily`, `monthly`, `adv_import`, `adv_export`.
  `period_key` is an ISO date, `YYYY-MM`, or `license_number@YYYY-MM-DD` (64 chars since migration
  `0058`, so an extended validity date re-arms the alert). Not used by the mismatch report. See
  [auth-security-email.md](auth-security-email.md).

### apps/core/models/ledgers.py

Company-wide import-incentive ledgers (their `SyncRun` rows use `Plant.COMPANY`).

- `RodtepScrollEntry` (`rodtep_scroll_entry`) - one shipping bill's credit on one scrip, unique on
  `(script_no, sb_number)`; `source_file_name` traces the Drive file. Its docstring's claim that
  nothing links a scrip to an import is outdated: the import CSV's licence columns do
  (`license_links.py`, see [api-and-features.md](api-and-features.md)).
- `RodtepUsage` (`rodtep_usage`) - hand-entered debits against a scrip; no FK to the scroll entry
  on purpose. The entry form is gone; the table and its read path stay.
- `AdvanceLicense` (`advance_license`) - licence-level fields, `license_number` unique, validity
  dates, `synced_from_row_hash` (whole-licence hash).
- `AdvanceLicenseMaterial` (`advance_license_material`) - one row per (licence, material, usage);
  rebuilt whole when the licence hash changes.

### apps/core/models/consumption.py

The derived consumption ledger (fully rebuildable from snapshots), shared tables by design. Detail
in [consumption.md](consumption.md).

- `MaterialConsumptionDaily` - quantity per `(plant, material_key, consumption_date)` (unique);
  `material_key` is `normalize_material(description)`, not `natural_key`; `quality` is `counted` or
  `spread`; display columns are denormalised and off the key.
- `ConsumptionEvent` - an interval deliberately not counted (or logged for disagreement), with
  `lot_ref` as a `'<model>#<pk>'` string, `kind`, both issue-book and balance quantities.
- `ConsumptionCoverage` - one row per (plant, day) observed; the rate denominator.

### apps/core/audit_log.py

- `PTAuditLog` (`pt_audit_log`) - append-only auth/account events: `ACTION_LOGIN`, `LOGOUT`,
  `USER_CREATED`, `USER_UPDATED`, `USER_DELETED`, `DEVICE_REVOKED`, `SESSIONS_REVOKED`; `actor_id`
  is a plain integer, `actor_email` denormalised. Lives outside the `models` package but is still an
  `apps.core` model (import it from `apps.core.audit_log`).
- `log_pt_action(request, action, detail="", actor=None)` - writes one row, taking the IP from
  `device_service.get_client_ip()`. Never raises (logs and swallows) so a broken audit write cannot
  block a login. Pass `actor` at login sites, where `request.user` is still anonymous. Add an action
  type only when a real endpoint needs it; field corrections and dismissals have their own tables and
  are not duplicated here.

### apps/core/admin.py

Django Admin as a debugging surface, not the user-facing admin (that is `admin.html`). Plain
registrations for every plant model, `SyncRun`, `MatchReview`, `DataQualityFlag`, `OTPCode` and the
import tables. Custom classes where the default would fight the design:
`MaterialCategoryReferenceAdmin` (search/filter, `normalized_description` read-only),
`ImportPOCorrectionAdmin` and `PTAuditLogAdmin` (all fields read-only, add/change/delete disabled),
`PTUserAdmin` (excludes `password_hash`), `TrustedDeviceAdmin` (hash read-only; revoke in the app).
Not registered at all: `DomesticPOCorrection`, `MaterialCorrection`, `FlagDismissal`,
`ManualMirMatch`, `RTPAchhadRMDailyMovement`, the ledger, consumption, `ReportSendLog` and
`RevokedRefreshToken` models, and the `*ImportPOMirMatch` tables.

### apps/core/apps.py

`CoreConfig` - `default_auto_field = BigAutoField`, and `ready()` imports `apps.core.checks` so its
`@register()` checks exist. No signals or other startup work.

### apps/core/checks.py

Two system checks, both surfaced by `manage.py check --deploy --fail-level WARNING` (a CI gate).

- `check_jwt_signing_key_is_independent` - `apps.core.W001` outside DEBUG when `JWT_SIGNING_KEY ==
  SECRET_KEY`.
- `check_no_unexpected_django_superuser` - `apps.core.W002` when any active `auth.User` superuser
  exists (an unaudited way into `/admin/`). Returns nothing if the DB is not migrated yet. This is
  one of the few deliberate `get_user_model()` calls: it really does mean `auth.User`.

### apps/api/apps.py

`ApiConfig` - registers `apps.api`, `BigAutoField`, no `ready()` hook.

### apps/api/urls.py

Everything under `/api/`. Groups: `health` and `health/ready`; `internal/*` cron triggers (shared
secret, see [auth-security-email.md](auth-security-email.md)); `auth/*` (login, token
refresh/verify, `me`, change-password, admin-overview) plus the included `device_urls`,
`google_oauth_urls` and `users_urls`; HRS routes **unprefixed** (`purchase-orders`, `materials`,
`stock-snapshots`, `mir-without-po`, `sync-status`, `sync-trigger`, `matches/...`) and the same set
under `achhad/` and `vapi/`; the cross-plant `imports/*` routes with the plant as a path segment
(including RoDTEP and Advance Licence); and `review/*`. Per-plant URL prefixes rather than a
`?plant=` parameter mirror the per-plant model decision. Endpoint behaviour is in
[api-and-features.md](api-and-features.md).

### apps/api/views.py

- `health(request)` - plain Django view, `{"status": "ok"}`; liveness only, touches nothing.
- `readiness(request)` - `GET /api/health/ready`, DRF view with empty `authentication_classes`
  (a stale cookie cannot turn it into a 401) and `AllowAny` (anon throttle still applies). One
  aggregate query for the latest completed (`success` or `partial`) `finished_at` per
  (plant, source) over the three plants and five sources (`po_csv`, `mir`, `stock`, `match`,
  `consumption`); any pair missing or older than `HEALTH_SYNC_STALE_HOURS` goes in `stale`
  (`"HRS/match"` style). Returns 200 `ok`, 503 `degraded` with the list, or 503 `down` on
  `DatabaseError`. No timestamps or data in the body. Polled by UptimeRobot; deliberately not
  Render's `healthCheckPath`. Scheduling context is in [data-sync.md](data-sync.md).

### apps/api/exceptions.py

`custom_exception_handler(exc, context)` - wired via `REST_FRAMEWORK["EXCEPTION_HANDLER"]`. When
DRF handles the exception itself, it wraps a dict body lacking `detail` into `{"detail": ...}`.
When DRF does not (`response is None`), it logs with traceback and returns 400 with `str(exc)` for
`_DESCRIBABLE_EXCEPTIONS` (`ValueError`, Django `ValidationError`, `ObjectDoesNotExist`), 400 with
`_describe_integrity_error()`'s SQL-free message for `IntegrityError`, else a generic 500 (with the
exception type appended in DEBUG). `KeyError` is deliberately **not** describable: its message is a
bare internal key name and it is a server bug, so it is a 500. To show a user a message, raise
`ValueError` with written text.
