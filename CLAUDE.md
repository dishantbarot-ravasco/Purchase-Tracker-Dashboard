# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

This file describes **how things are now** and **why they are that way**. Where a decision looks
odd, there is almost always a real incident behind it — those are recorded here so they are not
re-derived or "fixed" back. See [README.md](README.md) for setup and
[ARCHITECTURE.md](ARCHITECTURE.md) for the request/auth flow diagrams.

---

## What this app is

A standalone Django service that reconciles Purchase Orders, MIR (Material Inward Register), and
Raw Material Stock for Ravasco's three plants — HRS, RTP-Achhad, RTP-Vapi. It replaces an earlier
Claude Artifact "Purchase Tracker" prototype that re-fetched and re-parsed every Drive file live on
every page load with no persistence. This app syncs Drive into Postgres on a schedule instead, so
every viewer sees the same pre-computed reconciliation results without depending on their own Drive
session.

**This is a separate service/repo from the TDS Automation App**
(`C:\Users\Admin\OneDrive\Desktop\TDS Automation App\tds_app`) — same architectural conventions
(Django + DRF + Postgres + WhiteNoise, `uv`-managed, static frontend with no build step), but no
shared code, database, or deployment. Do not conflate the two when reading past session history.
The auth/security scaffolding here is a deliberate, mechanical port of TDS's auth architecture; see
[Auth & security](#auth--security) for what was ported, what was not, and why.

---

## Commands

All commands run from the repo root (there is no `django_backend/` subdirectory here, unlike TDS).

```bash
uv sync                                    # install/update dependencies
uv run python manage.py runserver          # dev server
uv run python manage.py migrate
uv run python manage.py makemigrations core
uv run python manage.py createcachetable   # one-off: DatabaseCache's table (pt_cache_table)
uv run python manage.py ensure_schedules   # idempotent: creates/corrects the django-q2 Schedule row
uv run pytest                              # test suite (real Postgres, no mocking)
uv run ruff check .                        # lint gate (red/green, also in CI)
uv run python manage.py check --deploy --fail-level WARNING   # production security check, also in CI

# Create/update a PTUser (bcrypt-hashes the password). The only way to create the FIRST admin
# account; re-running against an existing email updates password/role/full_name/designation.
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

# HRS pipeline
uv run python manage.py sync_po_csv        # PO master CSV -> HRSDomesticPurchaseOrder/HRSDomesticPOLineItem
uv run python manage.py sync_mir           # MIR xlsx -> HRSMIREntry
uv run python manage.py sync_stock         # Stock xlsx -> HRSRMLot (+ today's HRSRMSnapshot)
uv run python manage.py match_hrs          # PO<->MIR and MIR<->Stock matching

# RTP-Achhad and RTP-Vapi: identical shape, separate models/commands
uv run python manage.py sync_achhad_po_csv / sync_achhad_mir / sync_achhad_stock / match_achhad
uv run python manage.py sync_vapi_po_csv   / sync_vapi_mir   / sync_vapi_stock   / match_vapi

# Import POs (one command per plant, one shared parser)
uv run python manage.py sync_hrs_imports_po_csv / sync_achhad_imports_po_csv / sync_vapi_imports_po_csv

# Company-wide ledgers and reference data (not per-plant)
uv run python manage.py sync_rodtep
uv run python manage.py sync_advance_license
uv run python manage.py load_material_category_reference

# Maintenance
uv run python manage.py prune_revoked_tokens
uv run python manage.py report_match_accuracy
```

Every `sync_*` command accepts `--file <path>` to parse a local copy instead of fetching from Drive
— useful for offline dev without live service-account credentials.

---

## Architecture

### Layering

- **`apps/core`** — models and migrations only. No views, no business logic.
- **`apps/api`** — every HTTP-facing view, under `apps/api/routers/*_views.py`. Thin: build response
  dicts from the ORM or call into `apps/services`. No business logic beyond what a view needs.
- **`apps/services`** — Drive access (`google_client.py`), per-plant parsers (`parsers/`), the
  matching engines (`matching.py` / `matching_achhad.py` / `matching_vapi.py` over the shared
  `matching_core.py`), change-detection (`sync_utils.py`), and the auth-adjacent services
  (`device_service.py`, `otp_service.py`, `email_service.py`, `password_service.py`,
  `token_revocation.py`, `security_alerts.py`).

Management commands import from `apps.services`, never from `apps.core`. If you find yourself
writing `from apps.core.parsers ...` or `apps.core.matching ...`, that is the old pre-auth-pass
import path — it is `apps.services.*` now.

Several modules are **deliberately dependency-free** (no Django imports at all), so they unit-test
as plain Python and are safe to import from a migration's `RunPython`: `parsers/common.py`,
`validation.py`, `stock_identity.py`, `arithmetic_checks.py`, `stock_consumption.py`. Keep them that
way — the DB-touching counterpart lives in its own module (e.g. `data_quality.py` is the DB layer
over `arithmetic_checks.py`).

### Frontend

Static HTML + vanilla JS, served by WhiteNoise directly from `frontend/`. **No bundler, no build
step, no ES modules** — every file is a plain `<script>` tag sharing one global scope, so load order
matters. `main.js`'s header comment holds the current module map; `auth.js` has no imports and must
load first on every protected page, gating it behind `requireAuth()`.

`main.js` was once a single ~3,460-line file. It is now one file per concern — `main.js` (shared
`state`, per-plant caches, filter-reset helpers, bootstrap/nav/sync-polling), `charts.js` (Chart.js
lifecycle + plugins + the delegated modal-close listener), `flags.js` (status/match-badge/flag
rendering, shared across every list and modal), `po-list.js` / `po-modal.js`, `import-po.js`,
`materials.js` / `material-modal.js`, plus `export-panel.js`, `rodtep-panel.js`,
`advance-license-panel.js`. **If you are looking for a function that used to be in `main.js`, grep
`frontend/js/` rather than assuming it was deleted.**

Each page's own bootstrap was likewise extracted (`theme-init.js`, `login-theme-toggle.js`,
`home-page.js`, `admin-page.js`, `search-po-page.js`, `review-page.js`). That split was done so
CSP's `script-src` could drop `'unsafe-inline'` entirely, not for tidiness.

**The dashboard keeps itself fresh — `main.js`'s freshness watcher (2026-09-19).**
`ensurePOsLoaded()` fills `PURCHASE_ORDERS_BY_PLANT` once and only refetches when the cache is
cleared, and the only two places that cleared it were the viewer's own "Refresh Data" click and the
end of an admin-triggered sync's polling loop. **Every other way the database moves left an open page
showing its open-time numbers indefinitely, with nothing on screen saying so** — the hourly scheduled
sync, a colleague's sync, a sync from `admin.html` or another tab, an admin sync whose poll hit
`SYNC_POLL_TIMEOUT_MS` before the sync actually finished, or a `match_*` command run by hand.
Reported against a real screenshot: 7 of 11 KPI cards stale, the 4 that agreed only because nothing
in their input had moved.

`checkFreshness()` polls each selected plant's own `/sync-status` (the endpoint the badges already
read — deliberately not a new one), builds a stamp from the newest `finishedAt`/`startedAt` across
its sources, and when that differs from what the current render was built on it clears the caches and
re-renders. A **timestamp, not a row count**: matching can re-point a match row without any count
changing, and the KPI cards read match rows.

Three guards, each load-bearing: `MANUAL_SYNC_RUNNING` (set for the whole of
`triggerRealSyncAndRefresh`, cleared in `pollSyncUntilDone()`'s `finally` so a timeout or a throw
cannot disable the watcher for the session) stops it fighting the manual reload; an open
`.modal-backdrop.open` defers it, so the list never rebuilds under a reviewer reading a PO; and a
failed poll returns quietly to try again next tick.

**The hidden-tab check sits on the interval, not inside `checkFreshness()`** — it is a polling
policy, not a freshness rule. That placement is also what makes the function testable: an
embedded/automated browser can report `document.hidden` as `true` permanently, which made every call
a silent no-op while it lived inside. Verified with a throwaway in-browser harness (17 assertions,
all passing) since there is no Node here to run a JS test runner; the harness was deleted after, same
convention as the earlier `_a11y_tmp`.

`shared.js` (loaded on every protected page right after `auth.js`) holds what would otherwise be
duplicated per page: `PLANTS`/`PLANT_KEYS`, the authenticated `apiForPlant()` wrapper (401 → bounce
to `/login.html`), `escapeHtml`/`formatInr`/`formatDateIN`, the inline-edit helpers, and the
accessibility helpers.

**A header-filter keystroke re-renders the list region only — never the whole view (2026-09-19).**
Reported as the Raw Materials search "refreshing/reloading every time I type something, it's
horrifying", and "somewhat in Purchase order too". Two separate defects, and the loud one was not
the obvious one:

- **`preserveFocus()` only ever looked at `data-cf`.** It is defined in `flags.js` and called from
  all three list views, but Materials' inputs carry `data-mcf` and Import's `data-icf`, so in those
  two views nothing re-focused the rebuilt input — the caret was gone after the first character and
  the next keystroke went nowhere. Domestic PO was the only view where it worked, which is exactly
  why the bug survived in the two files that call the helper without owning it. It now matches on
  all three attributes (`FILTER_ATTRS`), the same set `applyAccessibleNames()` reads.
- **Every keystroke rebuilt the entire view.** A text filter is table-only — it narrows `tableRecs`,
  never `filtered` — yet the `input` handler re-ran the whole render: KPI cards restarting their
  count-up animations from 0, `destroyPageCharts()` plus fresh `new Chart(...)` for every canvas,
  and in Materials the PO↔material linkage pass as well. That flash *is* what "it reloads" was
  describing.

Each of `po-list.js` / `import-po.js` / `materials.js` now renders its list (heading, header filter
row, rows, pagination) into its own `#poListRegion` / `#importListRegion` / `#matListRegion`
container via `poListRegionHtml()` / `importListRegionHtml()` / `materialsListRegionHtml()`, reading
a module-level `PO_LIST_CTX` / `IMPORT_LIST_CTX` / `MAT_LIST_CTX` that the last **full** render
filled with what a text filter cannot affect. A keystroke re-renders that region alone, debounced
through `shared.js`'s `debounceRender()` (`LIST_FILTER_DEBOUNCE_MS = 150`; search-po's own 250ms is
longer because that one can await a fetch).

**The split of which control takes which path is load-bearing, not cosmetic.** A control that writes
into a "global" filter — one the KPI row or the charts are built from — must still call the full
render: Domestic's Created On and Status, Materials' Category/Sub Category/Status, and every
"Clear" that resets those. Everything else is table-only and stays in the region. If you add a
header filter, decide which it is by checking whether `filtered` or only `tableRecs` sees it; the
cheap path silently shows stale KPI numbers for a global filter. The state write itself is never
debounced — only the render — so the next full render always sees what was typed.

**The header filter row must be built inside the region function, never handed over in the context
object.** Each cell carries its filter's *current* value, so a snapshot taken at full-render time
rewrites the box being typed into back to its pre-keystroke value: the list narrows correctly while
the text vanishes under the cursor. All three files had exactly that bug for the length of one
verification run, which is the only reason it is written down here.

Verified with a throwaway in-browser harness against the running dev server (~85 assertions: each
view rendered end-to-end from synthetic data, a real `input` event on its search box, then asserting
the list narrowed, focus and caret and typed text survived, and the KPI card / chart-panel elements
were *the same DOM nodes* as before the keystroke — plus `preserveFocus` on all three attributes,
`debounceRender` collapsing 5 keystrokes into 1 render, pagination, the view-all toggle, the
global-filter full-render path, and the deep links below). Same convention and same reason as the
freshness watcher above — there is no Node here. The harness was not committed.

**Search PO deep-links into the dashboard rather than "/" (2026-09-19).** Project owner: "instead of
open entire dashboard can't we take user to the info about that PO, or raw material". The detail
panel's old `Open Full Dashboard →` landed the reader on All Plants with no filters, leaving them to
find by hand the record they had just searched for. `search-po-page.js` now builds
`/?plant=<key>&po=<number>` and, per line item, `/?plant=all&material=<description>`; `main.js`'s
`readDeepLinkParams()` reads them before the tabs render (so the right plant is fetched once, not
twice) and `openDeepLinkTarget()` opens that PO's or material's own modal after the first render.

Four details: `plant` is honoured only if it is a real plant key or `all` — **everything in a URL is
attacker-supplied**, and the miss message goes through `textContent`, never `innerHTML`; a PO link
names its plant because PO numbers are not unique across plants, while a material link is
deliberately cross-plant (`plant=all`) since that view rolls a material up across all three anyway;
a target that no longer resolves (a **retired/renamed PO** is the realistic case — see
[Purchase orders are retired](#purchase-orders-are-retired-not-deleted--and-until-2026-09-18-they-were-neither))
says so in a `.validation-note` instead of opening silently as if nothing was asked for; and the URL
is left in the address bar on purpose, so the link is shareable and survives a reload.

**CSS:** `brand.css` owns the shared top nav (gold/navy, ported from TDS); `style.css` owns the
dashboard's separate navy/blue/red palette; each page layers its own `css/<page>-page.css`.
`index.html` is the only page loading both — **do not merge the two palettes**, that separation is
deliberate (see `brand.css`'s header comment) and a test guards against custom-property collisions
between them (see [CSS custom-property collisions](#css-custom-property-collisions)).

### Per-plant models, not a shared schema — deliberate, don't "fix" it

`apps/core/models.py` has fully separate model classes per plant (`HRSDomesticPurchaseOrder` /
`RTPAchhadDomesticPurchaseOrder` / `RTPVapiDomesticPurchaseOrder`, and their `*POLineItem` /
`*MIREntry` / `*RMLot` / `*RMSnapshot` / `*POMirMatch` / `*MirStockMatch` siblings) rather than one
shared schema with a `plant` discriminator column.

This was an explicit instruction, re-confirmed by inspecting the real files: **each plant's
MIR/Stock spreadsheets have genuinely different column layouts**, not just different values.

- **HRS MIR** (`'RAW MATERIAL'` tab) has four PO-related columns (Purchase Order No./Date + SAP
  P.O. No./Date) and a SAP GRN Number column. **Achhad MIR** (`'R.M. '` tab — note the trailing
  space) has exactly one PO-number/PO-date pair and no GRN column at all.
- **HRS Stock** is one row per **(material, vendor) lot** — the same material appears many times
  from different vendors at different rates; `HRSRMLot.party_name` captures that. **Achhad Stock**
  is one row per **material, full stop** — no vendor column exists on that sheet, so `RTPAchhadRMLot`
  has no vendor field and Achhad's MIR↔Stock match can only gate on material description. That is a
  materially weaker guarantee, documented as such in `matching_achhad.py`'s module docstring, not
  treated as equivalent.
- **Tab names:** HRS Stock keeps a fixed `'Stock'` tab. Achhad's single tab is renamed every month
  (e.g. `'Aug 26-27'`), so `parsers/achhad_stock.py` reads `wb.sheetnames[0]` instead of matching a
  literal name. Vapi is back to a fixed `'Stock'` name, but preceded by a five-row document-control
  title block HRS doesn't have — see `parsers/vapi_stock.py`'s docstring for the exact layout.
- **Vapi MIR is the most structurally different**: no `Net`/discount columns at all (straight from
  `RATE` to `TAXABLE VALUE`), GST as one overall rate column plus three amount-only IGST/CGST/SGST
  columns (no per-component rate columns), a single TCS amount instead of a rate+amount pair, and
  its own `SAP P.O` field was **100% blank** across ~1,330 real rows checked — re-confirmed
  2026-09-18, still 0 usable values, so `sap_po_number` remains dead weight for matching.
  **Its other PO column is no longer blank, though**, and the widely-repeated "Vapi has no usable
  Tier-1 shortcut" is out of date: measured 2026-09-19 across 1,489 rows, `po_number_raw` is 97.5%
  populated (only 2.5% blank) but **29.0% (432 rows) names an order the master CSV holds** — most of
  the rest is the literal word `VERBAL` (536 rows), or several orders joined by a hyphen (334 rows,
  see [PO↔MIR](#po--mir) on `_MULTI_PO_HYPHEN_RE`). Someone has been filling it in. That
  is the single biggest reason Vapi's match rate jumped (see the accuracy table below).
- **Vapi Stock is a real shared multi-plant ledger**, not exclusively Vapi's: confirmed `PLANT`
  values `RTP-1` (145 rows), `HRS` (20), `RTP-2` (3). `RTPVapiRMLot.plant_tag` captures this without
  filtering, the same design HRS's own `location_tag` uses. Unlike Achhad, Vapi's Stock sheet **does**
  have a real vendor column (`Supplier Name`, confirmed genuine — only 16 of 168 rows echo the
  `PLANT` value), so Vapi uses HRS's stronger (material, vendor) gate.

Forcing all three into one table would mean a pile of always-null columns, and — worse — would tempt
matching logic that silently assumes a field exists uniformly when it doesn't. Each plant gets its
own models, parsers, matcher, router, and sync commands. `SyncRun` is the one shared table: a
generic job log with a `plant` field, not a plant-shaped data table.

**The PO master CSV format is identical across all three plants** (confirmed byte-for-byte on real
files), so `parsers/po_csv.py` is reused as-is; only the target model class and Drive file title
differ per command. **The Import PO CSV format is likewise shared** across all three (BOE number,
bill of lading, exchange rate, dual PO/BOE quantities, licence numbers) — one shared
`parsers/import_po_csv.py`. It is only MIR and Stock that genuinely differ per plant.

**`parse_po_csv()`'s header check and its row lookups must use the same normalization.** Four
`EXPECTED_HEADER` columns (`"HSN "`, `"Delivery Date "`, `"Payment Terms "`, `"Currency "`) carry a
trailing space because that is what the live file's header literally looked like when the parser was
built. The header-equality check strips whitespace before comparing, so it tolerates a file that has
since lost one of those trailing spaces — but `csv.DictReader` keys each row off the file's *actual*
header text, not `EXPECTED_HEADER`'s. A row lookup by the literal `row["Payment Terms "]` then raised
a bare `KeyError` the moment HRS's live master CSV had exactly this drift (2026-09-17, a routine
resave in Excel/Sheets, invisible to anyone looking at the file), which `sync_po_csv`'s blanket
`except Exception` turned into `sync_po_csv: failed - 'Payment Terms '` and took down that day's whole
HRS PO sync. Every row is now re-keyed by its stripped header name once, right after validation, so
the row lookups can never again disagree with what the header check already accepted — see
`parse_po_csv()`'s own comment and `test_po_csv_parser.py`.

### Domestic router de-duplication

The per-plant *model* decision above does **not** extend to the view layer. That part has nothing to
do with real schema differences and was previously copy-pasted near-verbatim across `hrs_views.py` /
`vapi_views.py` / `achhad_views.py`.

HTTP-layer logic now lives once in `apps/api/routers/_domestic_base.py`: a `_PlantConfig` dataclass
(model classes, `run_full_match` reference, plant key, material-field allow-lists, and the
lot-attribute names that genuinely vary — `lot_rate_field` / `lot_code_field` / `lot_vendor_field`)
plus `make_*` factory functions each plant's router calls once at import time and re-exports under
the original name. **Every endpoint's URL and response shape is unchanged.**

Achhad's genuine divergence (`rate`/`msl` instead of `basic_rate`/`sub_category`/`uom`, no vendor
field, `sap_code` instead of `sap_item_code`/`hsn_code`) is handled entirely through `_PlantConfig`
values, never by branching inside `_domestic_base.py` itself. Keep it that way.

This was done as a strangler-fig migration — one plant at a time, characterisation tests written
against the unmodified originals first to prove behavioural equivalence — specifically because
Vapi/Achhad had no direct test coverage of their own beforehand.

`imports_views.py` is intentionally **not** part of this migration; it already has its own
shared-config pattern via a `_PLANTS` dict. It does import `_field_warning`/`_serialize` from
`_domestic_base` (identical and field-set-independent, safe to share), but keeps its own
`_coerce_value` **deliberately**: its `_DATE_FIELDS`/`_DECIMAL_FIELDS` are a strict superset
(BOE/exchange-rate fields domestic plants don't have) and `_domestic_base._coerce_value` reads
module-level field sets, so sharing it as-is would silently reject valid import-only fields.

---

## Data sources and sync

### Drive layout — two folders per plant, not one

Confirmed by inspecting live file parent IDs, not assumed:

- Every plant's **PO master CSV** lives together in one shared folder (`PURCHASE_TRACKER_DB_FOLDER_ID`).
- Each plant's **MIR and Stock xlsx** live in their own separate folder (`HRS_MIR_STOCK_FOLDER_ID` /
  `ACHHAD_MIR_STOCK_FOLDER_ID` / `VAPI_MIR_STOCK_FOLDER_ID`).
- **RoDTEP** names a whole *folder* (`RODTEP_FOLDER_ID`) holding one file per scrip; **Advance
  Licence** names a single Drive *file id* (`ADVANCE_LICENSE_FILE_ID`).

`google_client.find_file_id_by_title()` takes an explicit `parent_id`; every sync command passes the
right one. Do not consolidate these into one folder ID. Sharing the common parent folder
("Purchase HO") as Viewer with the service account covers all of them (Drive permissions cascade to
subfolders), but the *code* still needs the right subfolder per file.

**MIR and Stock are shared between domestic and import purchases for a plant** — only the PO-source
CSV differs. Do not build a separate import-only MIR.

### Drive API v3, not v2

`google_client.py`'s search query must use `name = '...'` (v3 field name), not `title = '...'` (v2).
Using `title` returns an opaque `HTTP 400 Invalid Value` with no indication of what's wrong. If a
Drive search ever starts failing with "Invalid Value", check this first.

`list_files_in_folder()` narrows Drive's case-insensitive `name contains` query with a Python-side
prefix re-check — **that re-check lower-cases both sides**. It was once a plain `startswith()`, so a
file named `Rodtep-JNPT-16.xlsx` instead of `RODTEP-JNPT-16.xlsx` passed Drive's query and was then
silently dropped right back out, indistinguishable from "the sync isn't picking up the new file".

`_escape()` covers `parent_id` and `mime_type`, not just `title`. Every current caller passes
env-configured constants (never anything derived from a synced file's own content), so this was
never exploitable — it closes the gap before a future caller reintroduces it.

### Change detection must compare quantized Decimals, rounded the way Postgres rounds

`sync_utils.unchanged(model_cls, existing, parsed, fields)` is the shared helper every `sync_*`
command uses to decide whether to skip re-writing a row. Getting this right took three attempts,
each disproven by real data from a different plant:

1. **A `sha256` of the joined `str(field)` values fails immediately.** A `DecimalField` gets
   requantized to its declared `decimal_places` somewhere between Python and storage (a parsed
   `Decimal('950')` comes back as `Decimal('950.000')`), so the string reprs never match even when
   the values are equal.
2. **Naive per-field `==` still breaks.** A currency amount computed at higher precision than the
   column holds (sheet computes `8633.625`, column is `decimal_places=2`) is rounded on save, so
   every re-sync of that row looks "changed" forever.
3. **Quantizing both sides is right, but the rounding mode matters and the obvious choice is
   wrong.** `ROUND_HALF_EVEN` (Python's default) assumes Django rounds via `format_number()` before
   sending a value to the DB. That is false for this project's psycopg3/Postgres setup — confirmed
   directly: `SELECT CAST('353057.445' AS numeric(16,2))` returns `353057.45`, not `353057.44`.
   Postgres receives the full-precision Decimal and does its **own** round-half-away-from-zero on
   cast. `ROUND_HALF_EVEN` made two real Vapi MIR rows (`353057.445`, `84582.225`) report as
   "changed" on every single re-sync, never converging. **It is `ROUND_HALF_UP`.**

`unchanged()` also coerces plain `int`/`float` through `Decimal(str(x))` first, since a parser's
`to_decimal(...) or 0` fallback can hand back a literal `0` where the DB always stores a `Decimal`.

**If you add a sync command for a new source, use `sync_utils.unchanged()`** — do not re-derive a
hash-based or naive-equality comparison, and do not "fix" the rounding mode back to
`ROUND_HALF_EVEN` without re-testing against real data landing on an exact `X.XX5` boundary.

**Idempotency check** (run after touching this function or any sync command): run every plant's
`sync_*` commands twice in a row against real Drive data. The second run must report
`0 created/updated` for every source. A steady-state nonzero count on back-to-back runs means
something isn't converging — find the exact diff with:

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

Import POs are the one deliberate exception: `import_sync.py` uses a whole-order SHA-256 hash,
because an Import PO's line items are always deleted and recreated together on any change, so there
is no persisted row to diff field-by-field. `sync_advance_license.py` uses the same
whole-licence-hash reasoning, and `sync_po_csv.py` its own `_po_hash`.

### Purchase orders are retired, not deleted — and until 2026-09-18 they were neither

Every `*PurchaseOrder` model has `is_active` (migration `0051`), and every PO sync — domestic and
import — flips it off for orders the master CSV no longer lists, via
`sync_utils.deactivate_missing_orders()`.

**This was the one entity in the pipeline with no such flag.** MIR entries and stock lots have always
had one and their syncs deactivate vanished rows; the PO sync upserted on `po_number` as a natural key
and never removed anything. So **renaming a PO upstream forked it permanently**: the new spelling was
created as a fresh row and the old one stayed forever, carrying a duplicate set of line items that went
on competing for the same MIR rows. Measured 2026-09-12: 6 ghosts for HRS, 5 for Achhad, 11 for Vapi,
every one a rename rather than a real deletion.

It surfaced when the project owner cleaned `(Changed Purchase Order)` off every PO number at once and
reported that the suffix was still on the dashboard. It wasn't stale data — it was a second, permanent
PO record per renamed order.

**The detection already existed and was invisible.** `orphaned_orders()` found them and the domestic
syncs wrote a warning to **stdout only**, which under the scheduled django-q2 run nobody ever reads —
not a `SyncRun` error, not a flag, not on the dashboard. The three import syncs had no detection at all.
A count now rides on each plant's `sync-status` as `retiredPoCount` for exactly that reason.

Four details that are load-bearing:

- **Deactivate, never delete.** An order withdrawn upstream and one merely renamed are
  indistinguishable from here, and a delete is irreversible — which is why `orphaned_orders()`
  originally refused to act at all. Deactivation is reversible, so it is safe to do automatically.
- **The hash-skip must be guarded on `is_active`.** `_upsert_order()` skips writing when the row hash
  is unchanged; without `existing.is_active and` in that condition, an order that was deactivated and
  then came back **unchanged** would skip the write and stay invisible forever. Every MIR/stock sync's
  `unchanged()` check already had this guard for the same reason.
- **`is_active=True` belongs in the upsert `defaults`**, or a returning order is rewritten with fresh
  data and still never reappears.
- **`run_full_match()` deletes matches belonging to inactive orders explicitly.** Every other stale
  match is cleared by the per-item loops, which now only visit active orders — so a match written
  before retirement would otherwise survive every subsequent run untouched, still holding its MIR row
  against the live order that replaced it.

`manage.py report_retired_pos` is the read-only companion: `--already-retired` reads the DB (no Drive
credentials needed), the default fetches each master CSV to show what the next sync *would* retire.

**The contradiction gate hides this bug in one narrow case, which is why it took so long to notice.**
When MIR names the clean number, `_po_number_contradicts()` already drops the annotated ghost for free
— the ghost's own multi-token number doesn't match what MIR wrote, while the clean number is a known
order. That protection evaporates the moment the receipt has no PO reference to contradict with, and
~64% of Achhad's MIR rows have none.

### Decimal precision conventions

**GST rate fields need 3 decimal places at HRS/Achhad, 2 at Vapi — check the unit per plant.** Every
`*_rate_pct` on `HRSMIREntry`/`RTPAchhadMIREntry` (`discount_rate_pct`, `tax_rate_pct`,
`cgst_rate_pct`, `sgst_rate_pct`, `tcs_rate_pct`) is `decimal_places=3` because those sheets store a
**fraction** — a 2.5% GST rate is the literal cell value `0.025`, and 2 places silently truncates it
to `0.02`. `RTPVapiMIREntry.gst_rate_pct` is deliberately `decimal_places=2` because Vapi stores a
**whole percentage** (`18.00` = 18%), confirmed by cross-checking `Taxable Value × GST% ÷ 100 = IGST`
against a real row. **Don't "fix" Vapi to match the other two — they are genuinely different units.**
Check the source before picking `decimal_places` for any new rate-like field.

**`*_diff_pct` columns need a clamp.** `DecimalField(max_digits=6, decimal_places=2)` tops out at
`9999.99`. A pathological pair (a tiny reference value against a much larger actual — e.g. a rate
typo'd as a fraction of the real one) can exceed that and raise a DB error on insert instead of
recording "very large diff, flag it". All three matchers' `_diff_pct()` clamp to
`_MAX_DIFF_PCT = Decimal("9999.99")`. Keep the clamp.

### Stable lot identity

`*RMLot` rows are keyed on a **natural key** (`stock_identity.lot_natural_key()`:
`<code or normalized description>|<normalized vendor>`, with `#N` appended to disambiguate a genuine
duplicate), not on `source_row_ref` (the openpyxl row index). A row number is not an identity —
inserting one row mid-sheet shifts every row below it, and the next sync would re-label an existing
lot as whatever material now occupies its old row while that lot's `*RMSnapshot` history stays
attached by foreign key, silently splicing two materials' histories together. Migrations `0022`–`0024`
carried out the swap. An explicit material code wins over the description, since a SAP/HSN code
survives a description being reworded in the sheet (which happens).

### Scheduling

Every plant's sync+match pipeline runs on its own via a single `django_q.models.Schedule` row
created/corrected idempotently by `manage.py ensure_schedules` (wired into `render.yaml`'s
`buildCommand` and `docker-entrypoint.sh`). `Schedule.CRON`, `cron="0 9-20 * * *"` — **9:00 AM
through 8:00 PM IST, hourly, 12 runs/day**, requiring `croniter`. The entry point is
`sync_trigger.run_daily_sync_all_plants()`, which also runs the company-wide RoDTEP and Advance
Licence syncs. Admin/dashboard-triggered `sync-trigger` endpoints still exist alongside it, and a
plant is **skipped, not queued behind**, if a manual refresh is already mid-flight for it.

The schedule row is still named `"daily-sync-all-plants"` even though the cadence changed twice
(daily → 3-hourly → hourly 9–20). **Renaming it risks creating a duplicate schedule row** — see the
command's own module docstring. `next_run` is deliberately never reset on a cadence change either:
the first fire after a change may land at a stale timestamp left over from the previous cadence,
then self-corrects to the new cron boundaries.

The three report emails run on an **external free scheduler** (cron-job.org) hitting shared-secret
endpoints, since Render's free plan has no built-in cron. `prune_revoked_tokens` has its own trigger
endpoint too.

### Sync resilience rules

- **One malformed file must not abort a whole multi-file sync.** `sync_rodtep.py` isolates each
  file: a bad file is skipped and named in `error_detail`, every good file still syncs, and the
  overall `SyncRun` status is `PARTIAL` (not `FAILED`) as long as at least one file succeeded — the
  same convention `sync_stock.py`'s `rows_skipped` handling already uses. If RoDTEP still doesn't
  pick up a specific file, check its header row/column layout against `parsers/rodtep.py`'s
  `EXPECTED_HEADERS`; `error_detail` names the file and the mismatch directly.
- **Ledgers have no deactivation step.** `sync_rodtep` and `sync_advance_license` are hand-maintained
  financial ledgers, not current-state snapshots: a row that stops appearing in a re-download is
  *not* treated as "no longer real" the way a vanished stock lot is.
- A failed sync raises an admin email alert via `security_alerts.notify_admins_sync_failure()`, so it
  reaches an inbox rather than only `logs/app.log`.

---

## Matching engine

Implemented per plant in `matching.py` (HRS), `matching_achhad.py`, `matching_vapi.py` over the
shared `matching_core.py`. All three share one scoring approach. Each exposes `run_full_match()`,
wired to `match_hrs` / `match_achhad` / `match_vapi`; it is idempotent (`update_or_create` upserts)
and safe to re-run any time. One pass covers both domestic and import PO line items, reported as
`po_line_items_matched` and `import_po_line_items_matched`.

### Vendor name is a hard gate on MIR↔Stock, one of three votes on PO↔MIR everywhere now

Two records for different vendors are never candidates for each other on MIR↔Stock, however
well material/qty/rate/value line up (Achhad excepted — its Stock sheet has no vendor column at
all, see below). Names are normalised (`parsers/common.py`'s
`normalize_vendor()` — strips legal suffixes like "Pvt Ltd", lowercases, strips punctuation) and
then compared with **containment, not equality**: HRS's Stock sheet appends a city suffix its
MIR/PO data doesn't carry (`"Rubamin Private Limited"` vs `"Rubamin Private Limited - Vadodara"`),
and exact matching produced **zero** MIR↔Stock matches until switched to `shorter in longer`.

**All three plants run PO↔MIR identification as 2-of-3 now — Achhad and HRS since 2026-09-18, Vapi
since 2026-09-19.** `_MatchConfig.identification_two_of_three=True` in all three of
`matching_achhad.py`/`matching.py`/`matching_vapi.py` means identification there requires any **two
of {PO number, vendor, material}** rather than vendor plus one of the other two. Vendor keeps all of
its weight for the rows that have nothing else (423 of Achhad's 663 MIR rows carry no PO number at
all, and those still identify on vendor plus material exactly as before) but it can now be
**outvoted** by a PO number that agrees with the material.

This became safe only because the project owner filled in the PO-number column of Achhad's MIR for
every vendor an order is raised against. Against that file the change is measurable and one-sided:
**166 → 175 matched line items, zero lost, zero re-pointed** to a different MIR row. Four of the
gains are rows where the PO number is right and the **party name is wrong** — MIR 96/05 books
Barytes Powder (10000 @ 8.5 = 85000 on both sides) against Sunjay International's order under the
name "Prestige Industries"; MIR 122/08's party column reads the literal placeholder
`Seller / Consigner`; MIR 20/09 types "Kadr Metals" for "Kedar Metals" (0.842 similarity, under
`_VENDOR_SIMILARITY_THRESHOLD`'s 0.90, which cannot go lower). Those matches are made **and
flagged**: `vendor_matched=False` surfaces as the `Vendor Name Mismatch in MIR` data-quality flag,
because a disagreeing name is always an error somebody should fix at source.

**HRS enabled the same flag the same day for a different reason** — `matching.py`'s own comment has
the numbers. Its MIR file's PO coverage is actually better than Achhad's (236 of 498 rows carry a
usable PO reference, 232 of which name an order the master CSV holds), but **no HRS row is blocked
by the vendor gate at all**: every PO-confirmed row also passes `_vendor_matches()` against its own
order's vendor, so the 2-of-3 rule proper cannot fire there and `candidates_for()`'s union widening
adds nothing. What HRS gets is the flag's *other* half — the narrowed no-PO-vendor exclusion below.
Tinna Rubber is a registered `NO_PO_SUPPLIER` that the master CSV now raises four real line items
against (3000001081 ×3, 3000001098), and those four MIR rows were being dropped before any gate
ran. HRS's match models needed `vendor_matched` adding first (migration 0049) — enabling the flag
on a model without that column raises `FieldError` from `_vendor_matched_field()`.

**Why 2-of-3 and not a weighted score**, which is the obvious alternative and was considered: no set
of weights with one threshold can express this data. `vendor+material` **must** identify (it is the
only evidence 423 rows have) while `PO-number alone` **must not** — MIR 74/06 cites a mistyped
number belonging to another supplier's order for a completely different material, and PO-alone
identification would bind it. 2-of-3 states that rule directly instead of hiding it in constants.
Weight still decides *which* identified candidate wins; that is what the evidence tiers already do.

**Vapi enabled the same flag 2026-09-19**, once its `'PURCHASE ORDER'` MIR column's coverage was
actually measured rather than assumed still-blank: 29.0% of 1,489 MIR rows name an order the master
CSV holds (up from an earlier, already-stale "27.2%" figure — the column keeps filling in). Migration
`0052` added `vendor_matched` to `RTPVapiPOMirMatch`/`RTPVapiImportPOMirMatch` first, same prerequisite
HRS needed. Measured directly (real `run_full_match()` against a throwaway DB copy): **588 → 599
domestic matches, strictly additive — 0 lost, 0 re-pointed.** Same two-part split as HRS/Achhad: 4 are
the 2-of-3 rule proper (all on Madura Technical Textiles' PO `1000001433`, whose receipts are written
under `'MADURA INDL TEXTILES LTD'`/`'MADURA TECHNICAL FABRICS LTD.'` — neither clears
`_vendor_matches()` against `'Madura Technical Textiles Ltd'`), 7 are the narrowed no-PO-vendor
exclusion (Tinna Rubber, Eternia Trading — both registered `NO_PO_SUPPLIER` but now genuinely PO'd).
See `matching_vapi.py`'s own comment for the full measurement.

**Vapi's PO column also introduced a genuinely new shape neither HRS nor Achhad has: one MIR row
naming several open orders at once**, joined by hyphens (`'1000001552-1000001630'`) — a delivery
allocated across several of a vendor's concurrently open orders for the same material. Confirmed real,
not a typo: Madura Industrial Textiles alone runs up to 21 concurrently open orders for the same
fabric code (EE250) at once. 334 of Vapi's 1,489 MIR rows write this shape (never wider than 3 orders
per cell), and `matching_core._MULTI_PO_HYPHEN_RE` now splits it into separate tokens so
`_po_number_matches()` treats naming ANY of them as positive evidence — guarded to digits-only
segments of at least 8 digits (`is_usable_po_reference()`'s own floor) so it can never fire on a cell
that merely *contains* a hyphen for another reason: HRS's/Achhad's legacy slashed form uses one for
its own fiscal-year segment (`'HRS/HO/26-27/003'`), and so does Vapi's own single-PO legacy form
(`'RTP1/HO/26-27/008'`, `'DDO-0095/26-27'`) — both excluded by the slash/letters before the hyphen is
ever inspected.

**This one, unlike the flag above, is not strictly additive, and that is expected, not a bug.**
Measured together with 2-of-3: domestic matches move 588 → 577, a **net loss of 11** against today's
live baseline. Nearly all of the loss and the accompanying 295 re-pointings concentrate in one vendor
— Madura Industrial Textiles — whose ~100 concurrent orders are priced almost identically (₹230–250
across most of its EE-series fabric codes), so qty/rate/value tie-breaking there was always weak; most
of the matches this removes were already flagged `severity=material` and bound to a MIR row whose own
PO column named a *specific, different* order with zero corroboration before this fix existed to read
it. Where the fix re-points an item, the new destination is frequently the row that names that exact
PO number — a correction, not a regression — but it was shipped without a labelled-ground-truth check
(see [Match accuracy](#match-accuracy-manual-validation-is-required-not-optional)), so review a sample
of Madura's re-pointed pairs through `review.html` before trusting the new count over the old one.

### Legacy slashed PO numbers drift between the two files

The 10-digit SAP numbers reconcile on their own; the legacy slashed form does not. Achhad's MIR
writes `Eng/0007/2026-27` where the master CSV writes `RTP2/HO/26-27/ENGG-0007` — the same order,
and five of them. `parsers/common.py`'s `legacy_po_matches()` folds that shape on **(fiscal year,
serial) plus a shared series name by prefix**, tried only after `_po_number_matches()`'s exact token
test. The series condition is the safety argument, not decoration: Achhad's MIR carries both
`0014/2026-27` (Triambakam Impex) and `14/26-27` (Polyols & Polymers), which reduce to the identical
`('26-27', 14)` and would otherwise both claim the same order. Refusing those digits-only shapes
costs nothing — they still identify on vendor plus material.

### Some vendors never have a PO — that is registered, not inferred

`parsers/common.py`'s **`NO_PO_VENDORS`** registry lists the vendors this company never raises a
purchase order against. `matching_core._MirCandidateIndex` drops their MIR rows from the PO↔MIR
candidate pool at build time — **except**, on a plant running `identification_two_of_three`, a row
whose own PO column names an order we actually hold. That exception exists because the registry has
already gone stale once and nothing said so: Achhad's master CSV now raises real orders against JMF
Performance Materials (1100000792/799/875) and Eternia Trading (3000001072), and ten MIR rows
carrying those PO numbers were being dropped before any gate ran, indistinguishable from rows the
matcher had simply failed on. Narrowing the exclusion is self-correcting; relying on someone
remembering to edit this list is not. Keeping a row does **not** make it match — it still has to
pass identification. Roughly **890 of ~2,460 active MIR rows** across the three plants fall
in this bucket — a large block that previously sat in every pool forever, matched nothing, and had
nothing anywhere saying why.

**One list, shared by all three plants.** The first version was scoped per plant, on the assumption
that a vendor bought without a PO at one plant might be properly PO'd at another. The project owner's
list says otherwise — these are no-PO everywhere — so per-plant scoping would only give the three
copies a way to drift apart. Reintroduce scoping if a genuine per-plant exception appears, not before.

Two categories, kept separate because they have **opposite futures**:

- **`INTERNAL_TRANSFER`** — the company's own plants and sister units (Ravasco Vapi/Achhad,
  Hindustan Rubbers Silvassa/Achhad). An inter-plant jobwork/ex-work movement is not a purchase and
  will never generate a PO. Permanent.
- **`NO_PO_SUPPLIER`** — real third-party suppliers genuinely bought from, but without a PO being
  raised today (Gangamani, Eternia, Harsha Impex, K-Flex, 2M Elastomers, Gurvinder Singh HUF, Forech,
  Star Polymers, Sumitra, DS Industries, Tinna, JMF). This is a **process gap, not a fact about the
  data model**. If one of them starts being PO'd, its entry must be removed or its orders are
  silently excluded from reconciliation.

**The registry suppresses matching, never the row.** The rows stay in the MIR table, still reconcile
against Stock via `_StockLotPool` (an internal transfer really does land in stock), and are counted
and labelled for the dashboard by `services/no_po_vendors.py`, surfaced as `noPoVendors` on each
plant's `sync-status`. A silent exclusion would recreate the exact confusion the registry exists to
end. Keep that split if you extend this.

**Lookup is exact normalized equality — deliberately NOT `_vendor_matches()`.** No containment, no
0.90-similarity arm. A false positive here removes a real supplier's receipts from reconciliation;
a false negative merely leaves a row unmatched exactly as it already was. The containment arm makes
the false positive easy to hit with a short name (`"mit"` is a substring of `"limited"`), and the
similarity arm scores genuinely different companies as high as 0.857.

The cost is that **a genuinely different word needs its own line**. Normalization already folds
casing, punctuation, `&`/`and`, legal suffixes and trailing plurals, so most real variants collapse
on their own (`"STAR POLYMER"`/`"STAR POLYMERS INC."`, `"K-Flex"`/`"Kflex"`). Only three entries exist
purely as spellings: `"Packaging"` vs `"Packing"` for Ravasco, Vapi's `"... Pvt Ltd ACHHAD"` suffix,
and Achhad's misspelled `"JMF Perfomance"`. `test_no_po_vendors.py` pins each of them, so a change to
the normalization rules that splits one fails loudly instead of quietly re-admitting those rows.

It is also a real false-positive guard on the matcher itself: `_vendor_matches()` scores
`'Ravasco Transmission And Packing Pvt Ltd ACHHAD'` against `'Ravasco Transmission & Packing Pvt
Ltd'` at **0.897** — 0.003 under the threshold is all that stopped one plant's internal transfers
from being claimed by another plant's POs.


### PO ↔ MIR

Per line item:

- **Tier 1** — an exact/substring `po_number_raw` match. A free shortcut when MIR's own PO-number
  field happens to be populated and valid; it is unreliable on real data everywhere (~30% blank at
  HRS; **Vapi was ~100% blank and is now 97.5% populated, 29.0% of it a usable, recognized
  reference**, measured 2026-09-19), so it is never the only path. Vapi's own column can also name
  *more than one* order in a single cell (`_MULTI_PO_HYPHEN_RE`, see below) — a shape HRS/Achhad
  don't have.
- **Tier 2** — a weighted score among vendor-gated candidates: material description token overlap
  **30%**, qty closeness **20%**, rate closeness **20%**, pre-tax value closeness **30%**. Below
  `MATCH_THRESHOLD = 0.55` a line item is left unmatched rather than forced onto a poor candidate.

This extends the Artifact prototype's 55/45 description+amount split: qty and rate are real signals
now, not folded into one blended total, so a coincidental amount match is much less likely to fool
the matcher.

**The value comparison must use pre-tax figures on both sides.** PO's `net_value` is pre-tax; the
correct MIR-side field is `taxable_value` (also pre-tax — used directly for Vapi, which has no
separate `net` field to fall back to). It is **not** `invoice_final_value`/`total_amount`, both
post-GST/TCS. Comparing pre-tax to post-tax produced a bogus ~18% "value discrepancy" on line items
that matched exactly on qty and rate — 18% being roughly the GST rate on many of these materials,
not a real discrepancy.

**PO Item Id is not a trustworthy join key** and the matcher never relies on it: an audit found one
code (`11287940`) reused across three chemically unrelated materials from two different vendors on
the same PO source data.

**A MIR row can name more than one open order at once — Vapi only, `_MULTI_PO_HYPHEN_RE`
(2026-09-19).** Confirmed real, not a data-entry slip: Madura Industrial Textiles alone carries up to
21 concurrently open orders for the same fabric code (EE250) at once, so a single delivery genuinely
gets allocated across several of them, written as e.g. `'1000001552-1000001630'`. 334 of Vapi's 1,489
MIR rows write this shape (never wider than 3 orders per cell; 319 have every number recognized).
`_po_tokens()` splits it into separate tokens — guarded to digits-only segments of at least 8 digits
(`is_usable_po_reference()`'s own floor), so it can never fire on a cell that merely *contains* a
hyphen for another reason: HRS's/Achhad's legacy slashed form uses one for its own fiscal-year segment
(`'HRS/HO/26-27/003'`), and Vapi has its own single-order legacy form that does the same
(`'RTP1/HO/26-27/008'`, `'DDO-0095/26-27'`) — both excluded by the slash/letters before the hyphen is
ever inspected. Splitting lets `_po_number_matches()` treat naming *either* order as positive evidence
and `_po_number_contradicts()` correctly treat a candidate naming *neither* as contradicted, leaving
the ordinary qty/rate/value scoring to decide which one actually wins the claim.

**This one is not strictly additive, and that is expected.** Measured together with 2-of-3: Vapi's
domestic matches move 588 → 577, a net loss of 11 against the live baseline, with 295 re-pointings —
almost all inside Madura Industrial Textiles, whose ~100 concurrent orders are priced nearly
identically (₹230–250 across most EE-series codes), so qty/rate tie-breaking there was always weak.
Most of what this removes was already flagged `severity=material` and bound to a MIR row whose own PO
column named a *specific, different* order with zero corroboration before this fix could read it; most
re-pointed items land on the row that actually names their own PO number. Shipped without a
labelled-ground-truth check for the same reason nothing else on this page has one (see
[Match accuracy](#match-accuracy-manual-validation-is-required-not-optional)) — review a sample of
Madura's re-pointed pairs through `review.html` before trusting the new count over the old one.

### MIR ↔ Stock

Gates differently per plant, reflecting the real schema difference: HRS and Vapi gate on
**(material description, vendor)** — HRS via `HRSRMLot.party_name`, Vapi via
`RTPVapiRMLot.supplier_name`. Achhad gates on **material description alone** since its Stock sheet
has no vendor column, explicitly documented as weaker and more false-positive-prone.

**No plant compares stock quantity in this pairing.** HRS's `received` field (the sheet's "REC"
formula column) reads `0` for nearly every real lot — it evidently clears once allocated rather than
holding a running total comparable to one MIR line's qty. Only rate is compared.

### Units are normalised before comparing — on both pairings

`match_mir_entry_stock()` calls `_uom_adjust()` before comparing rate/qty, exactly as
`_score_components()`/`_diffs_and_flag()` have always done for PO↔MIR. This was once missing entirely
on the MIR↔Stock side: a material logged in MIR as MT against a Stock lot recorded in KG reported a
~1000x "rate mismatch" that was purely a unit artifact. Both sides carry independent `uom` fields
with no guarantee they agree for a given material, so this was never hypothetical.

When the two units belong to different, non-convertible families, qty/rate diffs are recorded as
`None` and `uom_mismatch` is set — **not** a nonsense percentage (migration `0044` added the field to
all three `*MirStockMatch` models, matching what `*POMirMatch` already had). Value stays
unit-uncompared; it is a currency amount, not a per-unit figure. It surfaces on the API as
`uomMismatch` in `_domestic_base.py`'s `mirStockMatches`; a frontend badge for it is a natural small
follow-up that has not been done.

### Flag thresholds

`FLAG_DIFF_PCT = 0` in all three matcher modules, mirrored by `FLAG_PCT` in `main.js` — **zero
tolerance**, a deliberate policy choice (the project owner asked for no tolerance at all, down to a
1kg-in-1000kg qty diff or the rate/value equivalent). Comparisons use strict `>`, so an exact match
never flags. `FLAG_DIFF_PCT` was `5.00` earlier; that value was picked rather than measured, and is
superseded.

If this policy changes again, change it in exactly one place per side: `FLAG_PCT` in `main.js`
(drives PO KPI cards, row flags, line-item badges, and Raw Material Analysis's own discrepancy cards
via `computeMaterialPoLinkage()`) and `FLAG_DIFF_PCT` in each of the three `matching*.py` files
(drives stored `is_flagged` on both pairings; keep all three identical). Also re-word
`DISCREPANCY_LEGEND`'s two critical entries, which now say "no tolerance" rather than interpolating a
number that would read oddly as "by more than 0 percent".

`matching_core.VALUE_FLAG_EPSILON` is a ₹1.00 absolute tolerance, shared with `arithmetic_checks.py`
— rounding from multiplying and summing several already-rounded currency fields is expected and is
not itself a data-quality problem.

### Import PO ↔ MIR: convert currency first

Import line items are priced in the PO's own currency (every real Vapi import PO today is USD); MIR's
`rate`/`taxable_value`/`net` are always INR. The first version of this matcher compared them raw and
scored every real pair near zero — a ~94x gap, not a rounding difference.

`_import_rate_value_inr()` (copy-identical in all three matchers) converts before scoring and
diffing: rate = `net_price × exchange_rate` (falling back to bare `net_price` when `exchange_rate` is
null — 2 of 37 real rows); value = `total_inclusive_value` (the real landed-in-India INR figure,
empirically closer to MIR's `taxable_value` than `net_value × exchange_rate` is), falling back to
`net_value × exchange_rate`. Vapi went from 0/37 to 25/37 import line items matched after the fix.

**Which import quantity is compared:** `qty_as_per_boe`, not `qty_as_per_po`. The Bill of Entry
quantity is what customs recorded as actually clearing — the real-world equivalent of what MIR logs
as physically received. `qty_as_per_po` is only the originally ordered amount and can legitimately
differ (partial/split shipments).

No separate MIR↔Stock table was needed for imports: `*MirStockMatch` is keyed on `mir_entry` alone,
independent of whether that MIR row traces back to a domestic or an import PO, so it already covers
the Stock leg for both.

### Performance: the engine was quadratic

Two full-table SELECTs used to sit inside per-row loops — `_candidate_mir_entries()` loaded the
entire MIR table once per PO line item, and `match_mir_entry_stock()` loaded the entire Stock table
once per active MIR entry — plus a stale-row cleanup DELETE firing once per MIR entry. Measured
against the real dev database on HRS, the *smallest* plant (120 domestic line items, 474 active MIR
rows, 195 stock lots):

```
before:  2,035 queries,  9.36 s
after:     972 queries,  0.81 s
```

The shape mattered more than the numbers: cost grew with the **product** of two independently growing
tables, in a pipeline that runs 12× a day across 3 plants — ten times the data meant roughly a
hundred times the work.

Fixed with `_MirCandidateIndex` and `_StockLotPool` (fetch once per pass, memoize the vendor gate per
distinct normalized vendor, pre-normalize each row's vendor string once) threaded through
`run_full_match()`, plus a batched cleanup. **All three are pure caching — no scoring, gating or
ordering logic changed**, verified by dumping every resulting match row before and after against the
real dev DB (86 PO-MIR rows × 17 fields, 145 MIR-Stock rows × 10 fields) and diffing: byte-identical.
The single-item entry points keep their exact previous behaviour via optional parameters and may
still be called for one row.

`test_matching_query_scaling.py` deliberately does **not** assert a magic query count (that just gets
bumped later) — it runs the same pipeline against two dataset sizes and asserts the count barely
moves, so a reintroduced N+1 fails by construction.

### `_assign_pairs()` needs both its termination guards

The optimal-assignment step is Bellman-Ford over a graph whose displacement edges carry negative
weight. Where a **positive-gain cycle** exists it has two independent ways to never terminate, and
both were live until 2026-09-18, when `match_vapi` simply stopped returning:

1. **The relaxation loop re-queues forever.** Bounded by `dequeue_budget` (total work per source).
2. **The path flip then walks a cyclic path forever.** `came_from_left`/`came_from_right` describe a
   path only if the search ran to completion; a search that stops early — on the budget, or on any
   future guard — can leave them describing a cycle. The flip walked it *while mutating as it went*,
   leaving the assignment half-applied. The path is now collected and validated **before** anything
   is written, and a cyclic one abandons that source rather than corrupting `match_left`/
   `match_right` into disagreement.

Fixing only the first moves the hang from one loop to the other — the faulthandler stack moved from
the `best_gain` comparison to the flip loop and kept hanging. **Keep both.**

**The graph size was never the cause**, and assuming it was cost two wrong fixes. Vapi's graph was
**869 candidate pairs across 278 line items, a median of 2 each**. Instrumenting the loop counters is
what actually settled it: 233 of the 234 source searches used **79 dequeues between them**, and
exactly one ran away. A per-node re-queue bound of V (the textbook SPFA guard) is also too loose to
help — V² dequeues × 234 sources is still hundreds of millions of iterations.

Vapi is the plant that hits this because most of its MIR rows still have no PO number, so it matches
on material alone and produces large sets of same-vendor, same-material candidates at near-identical
weight —
exactly the ties that create the cycles. HRS and Achhad have real PO-number coverage, which breaks
them. It went critical when Vapi's order book roughly doubled (131 → 222 POs) in one sync.

`TestAssignPairsTerminates` in `test_matching.py` pins this with graphs that hang the unguarded code.

### A shipment group that loses its rows must fall back, not lose its match

`run_full_match()` settles multi-shipment groups **before** the optimal assignment, because a grouped
edge stands for several MIR rows at once and plain bipartite matching cannot express that. A group
whose member rows are already claimed has to give up — but until 2026-09-18 the line item was then
**dropped from the assignment entirely**, because `ungrouped_edges` was built from
`key not in groups_by_key`. It ended up unmatched with free rows still sitting in its own pool: the
exact outcome fix 2.B exists to prevent.

Grouping is an optimization on **how** an item takes its rows (all of a split delivery at once, so
qty/rate/value compare against the aggregate), not a claim that single-row matching is wrong for that
item. A losing group is now **demoted** — it rejoins the ordinary assignment with its per-row edges
(`row_edges`, kept for every item now, not just ungrouped ones) and competes for what is still free.

Measured on live data: **HRS 170 → 180 line items (75.6% → 80.0%)**, Achhad 178 → 179, Vapi +4. HRS
PO `3000001046` was the clearest case — one line item, two unclaimed MIR rows both naming that exact
PO number, same vendor, identical material, matched instantly by `match_po_mir_line_item()` on its own
and left unmatched by the full run.

### Two hot paths in matching are cached or short-circuited for a reason

`_po_number_contradicts()` scans **every known PO number** for **every candidate** of **every line
item**, so anything it calls is on a cubic-ish path. Profiled on one real Vapi match run:
`_po_number_matches` at 851,590 calls and `_po_tokens` at 1,691,328 — **12.5s of a 15.6s run**.

- `_po_tokens` is `lru_cache`d. It is a pure function of a short string drawn from a corpus of well
  under 2,000 distinct values, and returns a **tuple** so a cached value cannot be mutated by one
  caller and handed back corrupted to the next.
- `_po_number_matches` short-circuits the `legacy_po_matches()` fallback unless **both** sides
  contain `/`. Without that guard it ran 844,956 times in one Vapi run, none of which could match —
  Vapi's PO numbers are bare SAP numerals. `legacy_po_matches()` checks this itself, but only after
  two calls and a `strip()` apiece; at this volume the cheap test has to come first.

### The five helpers are duplicated across the three matchers on purpose

`_closeness()`, `_diff_pct()`, `_token_overlap()`, `_vendor_matches()`, `_po_number_matches()` are
byte-for-byte identical copies in `matching.py` / `matching_achhad.py` / `matching_vapi.py` — a
deliberate, documented duplication (see `test_matching.py`'s module docstring). **Only HRS's copy is
directly tested**, so a change to one plant's copy without mirroring it in the other two will not be
caught here. Mirror it.

Dismissing a match, by contrast, is genuinely identical per plant with no variation worth protecting,
so it lives once in `match_dismiss.py`.

### Match accuracy: manual validation is required, not optional

There is no automated precision/recall measurement. `MATCH_THRESHOLD = 0.55` was **picked, not
measured** against labelled ground truth. Real rates from a full sync against live Drive data:

| Plant | PO line items matched to MIR (2026-09-18, latest Drive files) |
| --- | --- |
| HRS | **179 of 225 (79.6%)** |
| RTP-Achhad | **179 of 197 (90.9%)** |
| RTP-Vapi | **567 of 690 (82.2%)** |

**HRS's remaining gap is mostly not the matcher.** 21 of its 45 unmatched line items are on POs
raised in the last 30 days, where the goods have not been received or not yet booked into the MIR
(Achhad's equivalent is 5 of 19); most of the older ones are POs no MIR row mentions at all. HRS's
MIR carried **177 of 497 rows with day/month transposed dates**, 38 of them dated in the future;
`parsers/mir.py` now repairs them at parse time (2026-09-18) the way `parsers/vapi_mir.py` has since
2026-09-12 — see `_repair_mir_date()` there. **It costs one match** (180 → 179) and that is the
point: the date gate now runs on true dates, so pairs that only matched because a date was wrong no
longer do. Achhad needs no repair — its date column is stored as plain text, so Excel never
reinterprets it.

The historical figures below are superseded; the earlier table read HRS 68.6% / Achhad 86.0% /
Vapi 50.5%. As of 2026-09-18 it
matches **577 of 690 line items (83.6%)** against 1,415 active MIR rows. Three things moved at once:
its MIR PO column went from ~100% blank to 27.2% populated, its order book grew 131 → 222 POs, and
`match_vapi` had been **hanging outright** (see the termination guards above) so whatever was in the
database predated all of it. Re-measure before comparing anything to the table above.

**2026-09-19: `identification_two_of_three` and `_MULTI_PO_HYPHEN_RE` both landed the same day** (see
[PO↔MIR](#po--mir) and [Vendor gate](#vendor-name-is-a-hard-gate-on-mirstock-one-of-three-votes-on-pomir-everywhere-now)
for each in isolation) — 588 → 599 from the flag alone, 588 → 577 with both together. **Don't quote a
single before/after Vapi percentage without saying which of the two changes it includes** — they move
the same count in opposite directions and by design, not by accident.

MIR↔Stock rates are much lower (1.6–24%), but that is mostly structural, not matcher failure: Stock
is a current snapshot (one row per live lot) while MIR is a full historical log, so most older MIR
rows correctly have no current stock lot to match against.

**Until a sample-based accuracy check exists, treat every match as a suggestion, not a fact.** This
is a process instruction, not just a UI note (the dashboard's `.validation-note` banner and the
per-item confidence badge exist to prompt it, but the verification itself has to be a human
decision). Do not wire any downstream action — auto-approving a PO, auto-updating stock — off a match
without a human in the loop.

To help a reviewer triage rather than trust every checkmark equally, `_line_item_dict()` exposes
`matchScore`, `qtyDiffPct`, `rateDiffPct`, `valueDiffPct` per line item, and `matchStatusHtml()`
renders a confidence badge (high = exact PO-number match, medium = weighted score ≥ 0.75, low = below
that) plus up to three separate flag badges. A partial delivery (qty differs, rate/value in line) no
longer reads identically to a real price discrepancy. **The value badge is deliberately suppressed
when qty is already flagged**, since value ≈ qty × rate and a value gap fully explained by a qty gap
isn't a second, independent problem worth its own badge.

`review.html` + `apps/api/routers/review_views.py` back a deliberately minimal review queue: a batch
of 5 random unreviewed matches, both sides side by side, recording Correct/Incorrect/Unsure as a
`MatchReview` row. No filtering, no search, no bulk actions — the goal is throughput (roughly 200
reviews across plants and tiers), not a full audit UI. It is cross-plant by design (one shared
router, like `imports_views.py`) and reuses each plant's own `MATCH_CONFIG` as the single source of
truth for model classes and lot field names rather than re-deriving a second mapping. Any
authenticated role can review — this is data collection, not a privileged write.

**Getting plant staff to reliably fill MIR's own PO-number field was considered and rejected as a
lever** — that is a training/process fix, not this app's to solve. Vapi in particular will likely keep
running on the weighted score alone indefinitely; don't assume Tier-1 coverage will improve on its
own.

`apps/services/arithmetic_checks.py` is a separate, matching-independent layer: it self-validates the
source sheets' own arithmetic (a transposed digit, a rate typed as a fraction, a missing tax
component, a broken stock formula) — a class of error nothing else in this app can see.
`data_quality.py` turns the results into `DataQualityFlag` rows, upserting current mismatches and
**deleting flags that no longer mismatch**, so the table represents current reality rather than an
ever-growing log. Confirmed empirically against real synced data: all three checks reconcile 100% for
HRS/Achhad and ~96–99.5% for Vapi, with the remainder being genuine data issues, not tolerance
artifacts.
---

## Feature areas

### Frontend pages and the shared top nav

Five protected pages share one header (`brand.css`, modelled on the TDS Automation App's nav):

- **`index.html`** (`/`) — the PO↔MIR↔Stock reconciliation dashboard. Loads `brand.css` first (for
  `.topnav`/`.nav-tabs`/`.nav-user` only) and `style.css` for everything else.
- **`home.html`** — landing page after login. A real KPI row (Total PO's, Suppliers, This Month, This
  Week) computed client-side from all three plants' live PO data fetched in parallel (`loadKpis()`) —
  not placeholders. A plant that fails to load degrades the count **with a visible warning** rather
  than silently reading as zero.
- **`search-po.html`** — look up a PO number across all three plants at once (substring,
  case-insensitive, ≥2 characters). **Deliberately self-contained**: it fetches and caches its own
  copy of each plant's PO list rather than depending on `main.js`'s cache or modal, and its detail
  panel is a simpler view (no match-confidence/flag-severity styling) — a full-parity detail view
  already exists at `/`, and this page links there. See the file's header comment before treating
  this as an oversight.
- **`review.html`** — the match-accuracy review queue (above).
- **`admin.html`** — admin only. Per-plant sync status, an Overview tab, and the in-app Users panel.
  Non-admin visitors get a plain access-denied panel; that is defence in depth, not the primary gate
  (the nav tab and home's quick-action card are role-hidden, and the endpoints enforce `IsAdmin`
  server-side).

`auth.js`'s `renderNavTabs(container, activePage)` renders the shared tabs: **Home** → **Dashboard**
(`/`, a distinct destination from Home, not an alias) → **Search PO** → **Admin** (hidden entirely
for non-admins, not shown-then-denied). Each page marks its own tab active; no page maps to two tabs.
`renderUserBadge(container)` renders a circle-avatar user menu (initials, role-coloured background —
admin gold, editor blue, viewer navy, matching `admin.html`'s role pills) with a Logout dropdown.

**Sync status labels**: the dashboard's badges and `admin.html`'s per-plant cards read **"PO Updated"
/ "MIR" / "RM"**. Those are display labels only — the underlying `sync` dict keys (`po_csv`/`mir`/
`stock`) and `SyncRun.source` values are unchanged. Don't go looking for a `source='rm'` value.

### In-app user management

`apps/api/routers/users_views.py` + `users_urls.py`, ported from TDS's own `users_views.py`. This
replaced an earlier version of `admin.html` that linked out to Django Admin, which was explicitly
rejected ("don't django admin, use our builded as we did in tds_app") — **no Django Admin links
remain on that page**.

- `GET /api/auth/users` — list.
- `POST /api/auth/users/create` — create. Body: `email`, `password`, `fullName`, `designation`,
  `role`. Validates the email domain and rejects a duplicate with a real `409`, not a stack trace.
- `PATCH /api/auth/users/<id>` — update `role` / `isActive` / `fullName` / `designation` / `plants` /
  `password` (the password field is the in-app reset path).
- `manage.py create_pt_user` remains the only way to create the **very first** account, before any
  admin exists to use the panel.

URL naming deliberately differs from TDS's, which disambiguates `GET /users` from `POST /users/` by
trailing slash alone — fragile and easy to break. This app uses distinct path segments
(`/auth/users` vs `/auth/users/create`).

`admin_overview_views.py` backs the Overview tab. TDS's own concepts don't map here literally (a TDS
document is *created* by a user; a PO is synced from a CSV and nobody authors one), so they were
adapted: **Top Correctors** (inline field corrections per user — the closest real analogue to "who
is actively doing work here"), **Top Vendors** (PO count per vendor across all three plants), and
**Recent Activity** (last 10 corrections across all three correction tables). The KPI row reuses
`home.html`'s existing client-side `loadKpis()` rather than duplicating it server-side; this endpoint
covers only the two things with no existing client-side source.

### Dashboard structure (Artifact parity)

The nav structure and PO-list KPI row were rebuilt to match the original Claude Artifact prototype.
Its actual source was read directly (not guessed from screenshots) before anything was changed.

**Three structurally different tab components, one per level** — this matters, and getting it wrong
is what made the first parity attempt look wrong despite matching structure and logic:

1. `.view-tab` — filled pill. Level 1: Purchase Orders / Raw Material Analysis.
2. `.plant-tab` — underline tab, no fill. Level 2: plant selector.
3. `.sub-tab` — small filled pill, distinct sizing from `.view-tab`. Level 3: Domestic / Import.

`renderPlantTabs()`/`renderPurchaseTypeTabs()` emit the right class per level. Purchase Orders' plant
selector **does** include "All Plants" (listed first) on a direct request from the project owner — a
deliberate deviation from the artifact. Level 3 is Domestic/Import only, no "Combined".

Other parity details: the two critical KPI cards (Quantity / Rate-Value Discrepancy) use
`.kpi-card critical` (red-soft fill), not `.kpi-card overdue` (border-only). The "top 5" PO list uses
CSS-grid card rows (`.list-header-row.po-cols` + `.top5-list`/`.top5-row`); the plain `<table>` is
used only for the "View all" expanded view — a disclosed simplification, since the artifact uses the
`gridjs` library there and this app deliberately does not adopt it.

**Lesson recorded for future parity work: sample-checking a reference's CSS is not enough.** Read the
complete stylesheet and exact markup before claiming a match.

**"(Changed Purchase Order)" needs no code.** The artifact has no amendment-detection logic
(`_isNewPo` is hardcoded `true` throughout). That text is literal content already present in some
real HRS POs' `po_number` field (confirmed on 5 real rows, e.g. `'3000001104 (Changed Purchase
Order)'`), written by the CSV extraction process. `po_csv.py` captures it verbatim and the frontend
displays it verbatim. **If you see this and think "I should build amendment detection" — don't.**

### Data Quality Flags

`flags.js`'s `FLAG_CATEGORY_RULES`/`categorizeFlag()`/`DISCREPANCY_LEGEND` started as a direct port of
the artifact's own: 11 plain regexes run client-side against each PO's `remarks` field (already
parsed, already returned by the API — no schema change needed).

**The category list now covers every computed match-quality signal**, not just qty/rate/remarks.
`matching_core._diffs_and_flag()` had always computed `taxable_value_flagged`/`final_value_flagged`,
and the model layer already had `tax_type_mismatch`/`uom_mismatch`, but only qty/rate ever reached
the frontend — the rest were computed, stored, then silently discarded. `computePoFlags()` now also
sets: **PO Not Found in MIR** (critical — no MIR entry crossed `MATCH_THRESHOLD`), **Tax Type
Mismatch in MIR**, **Net Value Mismatch in MIR**, **Taxable Value Mismatch in MIR**, **Final Amount
Mismatch in MIR**, **UOM Mismatch in MIR**. Backend columns for the value mismatches came in
migration `0040` across all six `*POMirMatch`/`*ImportPOMirMatch` models.

The "Data Quality Flags" KPI card means "any category at all". **"Filter by Flags" has no fixed
option list** — one `<option value="cat:<label>">` is appended per category actually present in the
current selection (built from `flagCategoryCounts`), so a reviewer can jump straight to e.g.
"Taxable Value Mismatch in MIR (3)" instead of opening every flagged PO to find out which ones have
that problem.

The same treatment extends to **Import Purchases** (`import-po.js`'s `importCategoriesFor()`,
`flags.js`'s `importCriticalFlagsFor()`, reading the same six fields off each item's nested
`mirMatch`) and **Raw Material Analysis** (`materials.js`'s `computeMaterialPoLinkage()`,
`material-modal.js`'s `materialCritPos`, reading them off the material's fuzzy-linked PO line item
with the same per-item scoping, never misattributed from a different line item on the same PO). Each
view computes its own categories rather than sharing one function — the same per-view duplication
precedent as the rest of the app.

**Note on "PO Not Found in MIR" density**: Import's and Materials' linked-item universe includes every
still-open, not-yet-received PO by construction, so this category fires for most on-order
materials/import POs. That is expected and consistent, not a bug to suppress.

**The regex categorisation is intentionally imprecise, same as the original.** A remark mentioning
"no Item ID/Vendor Code printed" (an old-PO-template note) matches the "Vendor code scheme
inconsistency" rule via a bare `/vendor code/i` test even though that's not quite the right bucket.
Known, accepted, carried over as-is — not a new bug.

### Inline "Edit Everywhere"

Every PO detail modal (Domestic and Import) and the Raw Material Analysis modal have per-field
pencil-to-edit corrections. **This mutates the real row and writes an append-only audit row — it is
not a resolve-at-read overrides table.** A spec initially described the latter (separate
`original_value`/`override_value` rows resolved at display time), but Import's edit feature had
already shipped with mutate+audit; the decision was to extend that working pattern rather than
rebuild both. The artifact prototype's own correction UX (pencil → shared form → queue a request to a
Drive folder for manual review) informed small UI/copy cues only; its human-review-queue mechanism
doesn't apply here.

**Backend.** `validation.py`'s `is_valid_gstin`/`is_valid_email` **warn, never block** — a real
vendor GSTIN or email can be genuinely unusual. Audit models: `DomesticPOCorrection`,
`ImportPOCorrection`, `MaterialCorrection` — kept as separate models rather than one generalised
table, same per-type convention as everything else here. Each `correct_field` view allow-lists field
names (split PO-level vs item-level), coerces dates/decimals, and does the mutate + audit row in one
`transaction.atomic()`.

Editing a field that feeds matching (`qty`, `net_price`, `net_value`, `vendor_name`, `description`,
`vendor_gstin`; imports also `qty_as_per_boe`; materials `description`/vendor/rate) **synchronously
re-runs that plant's `run_full_match()`** so badges update immediately instead of waiting for the next
scheduled run — safe because the function is idempotent and plant data volumes are small.

**Domestic line items have no stable natural key.** `item_id` isn't unique or required, and a plant's
`sync_*` command deletes and recreates every line item on any change. Item-level corrections key on
`(plant, po_number, item_id)`, same as `ImportPOCorrection`. A blank-`item_id` item still can't be
individually addressed — an inherited limitation, not a new one.

**Material edits are per-lot.** Category and Rate are editable per Stock lot row in the "Stock by
Plant" tab — **not** on the Overview tab's rolled-up Classification block, which shows one
"first found" value across sibling lots in all three plants and would be ambiguous about which lot an
edit should target. `MaterialCorrection` keys on `(plant, lot_id)` since each plant's lot table has
its own id space. The allow-lists are genuinely different per plant, not copy-paste: HRS/Vapi's rate
field is `basic_rate`, Achhad's is `rate`, and Achhad has no `sub_category`/`uom`/vendor at all.

**Frontend.** `editableLine()`/`editableCell()`/`plainLine()`/`wireEditableLines()`/
`startFieldEdit()`/`savePoField()`/`distinctFieldValues()` live in `shared.js` (both the PO modal and
the import modal need them). `fieldType` (`'text'`/`'date'`/`'number'`/`'select'`) picks the control;
`'select'` options are **always derived from distinct values already in the loaded PO list**, never
hardcoded. `wireEditableLines()` accepts a `(lineEl) => url` function as well as a fixed URL string,
because the material table's rows span three plants' own PATCH endpoints. Explicit Save (✓) / Cancel
(✗) icons sit next to the active input **in addition to** Enter/Escape/blur, not replacing them.

Domestic's PO modal has a third tab, **Flags & Corrections**; Import's same-named tab renders
correction history too, not just flags.

**Domestic has no Shipment & License tab** — BOE/BL/laden-on-board/country-of-origin/licence fields
don't exist on domestic models at all (domestic POs never clear customs). Don't add one without a
real schema change first.

### Dismiss / override a flagged match or flag

Two mechanisms, because the two things are stored differently:

**Match dismissal.** `dismissed_by_override` already existed on every `*POMirMatch`/`*MirStockMatch`;
migration `0011` added `dismissed_by`/`dismissed_at`/`dismissed_reason`.
`match_dismiss.dismiss_match(model_cls, match_id, user, dismissed, reason)` is the one implementation
across all three plants. Endpoints: `PATCH .../matches/po-mir/<id>/dismiss`,
`.../matches/mir-stock/<id>/dismiss`, and `PATCH /api/imports/matches/po-mir/<plant>/<id>/dismiss`
(which reuses the same model-class-parametrized function). Body:
`{"dismissed": true/false, "reason": "<optional>"}`. Clearing a dismissal also clears the three audit
columns — a match that is no longer dismissed shouldn't keep stale provenance for an undone decision.
**Every matcher's `update_or_create` `defaults` dict deliberately never touches `dismissed_*`, so a
dismissal survives every re-match** (exercised directly by
`test_dismiss_match.py::test_editor_can_dismiss_with_reason_and_it_survives_rematch`).

**PO-level flag dismissal.** The Quantity/Rate-Value critical flags and the Data Quality Flag
categories are computed at read time from `remarks`/diff percentages, not stored rows, so there was no
column anywhere to carry a dismissal. `FlagDismissal` (migration `0014`) is a generic table unique on
`(plant, po_number, flag_key)`, upserted by `flag_dismiss.dismiss_po_flag()` with the same audit
fields. `flag_key` is Domestic's own flag label directly (safe — `computePoFlags()`'s `Map` holds at
most one entry per label) or, for Import, `<code>:<item_id>` (e.g. `"F7:ITEM3"`, since
`import_flags.py`'s flags are per-item and already carry a stable code).

Frontend: a dismissed flag **stays visible but muted** (struck through, `.flag-badge.dismissed`)
rather than hidden, so a reviewer can still see what was dismissed and why (tooltip carries
who/when/reason). `wireDismissLinks()` is one shared wiring function with a `data-match-type` branch
covering both families, not two near-duplicates. The optional reason uses a plain `window.prompt()` —
this app has no reusable modal-with-textarea component and one felt like overkill for a single
optional field.

### Import purchases

Import POs are parsed for all three plants through the one shared `parsers/import_po_csv.py`, with a
read-time reconciliation layer in `import_flags.py`: shipment-stage rollup, PO-vs-BOE qty discrepancy
detection, delivery-date status, partial-delivery detection, and 7 data-quality flags. That layer
predates import↔MIR matching and was what could be computed with no MIR at all; it is still real and
still used.

`imports_views.py` is cross-plant by design (plant is a path segment, not a per-router constant).
`_item_dict()` carries a nested `mirMatch` (null when the item never crossed `MATCH_THRESHOLD`) with
`matchId`/`tier`/`matchScore`/diff percentages/`isFlagged`/`dismissed*`/`stockMatched`, the last read
from the `mir_entry.stock_matches` prefetch cache, not a fresh query per item.

`main.js`'s `renderImportPoList()` computes `poInwarded`/`poQtyDiscMir`/`poRateDiscMir` client-side (a
PO "has" a MIR condition if any line item does), since there is no server-computed PO-level MIR
aggregate for imports — unlike domestic, where the backend rolls up via `flags.po_has_qty_discrepancy()`.

**BL tracking**: `bl_tracking.py` is a thin, single-call passthrough to SafeCube's (Sinay's) public
Container Tracking API v2, backing the "Track" links next to a PO's BL number. **Nothing is stored** —
every call hits the API live, with no caching or rate-limit tracking on this side. If the trial key's
quota becomes a problem, that is the point to add a short-TTL cache keyed on `bl_number`; caching
before then would be guessing at a problem that may never appear.

**RoDTEP** (`RodtepScrollEntry`, `RodtepUsage`) and **Advance Licence** (`AdvanceLicense`,
`AdvanceLicenseMaterial`) are company-wide ledgers surfaced by `rodtep-panel.js` /
`advance-license-panel.js` under the Imports view, each with its own "Sync Now" trigger. Both are also
covered by the dashboard's "Refresh Data" button — triggered **once per click, not once per selected
plant**, since they are company-wide with one shared lock each, and `pollSyncUntilDone()` waits on the
`rodtepInProgress`/`advanceLicenseInProgress` flags from `GET /api/imports/sync-status` before
finishing.

### Days-Left engine

Per-material consumption rate and days-of-cover in Raw Material Analysis.

**Why it can't use `issued`/`received` as the primary signal**: those columns mean different things
per plant — Achhad's `issued` is a period-to-date summary that resets each period; HRS's and Vapi's
`issued`/`received` are formula cells pulled from a separate Receipt/Issue tab, and HRS's `received`
reads 0 for nearly every real lot. The one field that means the same thing everywhere is the closing
balance, `todays_stock`, so the portable signal is the **day-over-day drawdown between consecutive
snapshots** of it.

This was checked, not asserted: comparing every consecutive-day snapshot pair's logged
`received`/`issued` against what the balance actually did, the books reconcile with the real balance
change on only **45% of HRS's days, 87% of Achhad's, 82% of Vapi's**, and in aggregate the logged
figures are off by roughly 2×–10× depending on plant. `issued` alone is worse still — HRS has real
cases of a multi-thousand-unit balance drop recorded as `issued: 0`.

**`received` recovers real consumption a receipt would otherwise hide.** One formula, always true
regardless of which direction the balance moved:
`real consumption for an interval = (stock_yesterday - stock_today) + received`.

- **Stock fell or stayed flat** (the ordinary case) — the drop alone proves that much was consumed;
  same-day `received` can only mean MORE was consumed, so it is added straight in. **This is the
  bigger, more common gap**: a receipt can land the same day as heavy consumption without the net
  balance ever reversing direction. Confirmed across all three plants — **178,282 units of HRS
  consumption, 82,012 of Achhad's, 190,501 of Vapi's** were invisible this way in one week of synced
  history. Real HRS example: balance dropped by only 1,040 net, but 5,040 was also received the same
  day — real consumption was 6,080.
- **Stock rose overall** (a receipt landed, net direction reversed) — the same formula applies and is
  trusted whenever it yields a real positive answer. Real HRS lot: balance rose 4,350 → 25,000 with
  `received` logged as 25,000, meaning 4,350 units were ALSO consumed that day. `received` is
  frequently blank even on a genuine receipt day (98 of 111 real HRS receipt days in that data), so a
  non-positive result is never counted and those days are simply skipped.
- **Double-counting guard.** `received` can stay frozen at the same nonzero figure across several
  consecutive snapshots instead of resetting to 0 once used — confirmed on a real HRS lot showing
  7,487 unchanged across three snapshots. Naively adding it into every interval it appears in would
  count one real receipt two or three times. **`received` is only trusted as a new event when it
  differs from the immediately preceding snapshot's reading for that lot**; a repeat contributes 0.

`stock_consumption.consumption_stats()` is the pure algorithm, tested dependency-free. It **skips any
consecutive-snapshot gap over 7 days** — averaging across a long hole would invent a drawdown that
never happened. Confidence travels in the payload as `high`/`medium`/`low`/`none` (based on days
spanned + usable intervals) and is **rendered, not used as a hidden filter** — the point is to show a
thin-history figure while being honest about how thin it is. `issued` still feeds a secondary
cross-check (`estimatesAgree`) where it happens to behave cumulatively, but never overrides the
primary number.

This was built on the project owner's explicit decision despite its stated prerequisite (a "stable lot
identity" document) not existing in this repo. Lot identity has since been made stable (see
[Stable lot identity](#stable-lot-identity)); the confidence band remains the mitigation for a lot
whose history does reset — it sits at LOW/NONE rather than reporting a wrong number with false
certainty.

**Query shape matters here**: `_consumption_by_lot()` fetches every active lot's snapshot window in
**one** query (`values_list` + `itertools.groupby`), regression-tested with
`django_assert_num_queries(1)`. `daysToMsl` uses Achhad's `msl` column only (HRS/Vapi have no
equivalent — the same `getattr(lot, "msl", None)` no-branching pattern used for `no_of_days`/
`sub_category`), and is `0` the instant stock is at or below MSL, independent of whether a
consumption rate is known yet — being already below the reorder point shouldn't wait on 30 days of
history to surface.

**Aggregation rules** (`aggregateMaterialsByName()`): sum consumption *rates* across a group's lots,
then divide the already-summed quantity. **Days-left itself is never summed or averaged across lots
— that is simply wrong.** Group confidence is the **weakest** contributing lot's band, not the best
or a mean: one thin lot makes the whole group's rate thin. The "Low Stock" status fires on
`daysLeft < 15` or any contributing lot's `daysToMsl === 0`.

### Data export

An "Export Data" button next to "Refresh Data" opens a small panel (`export-panel.js`) to download
the full daily RM stock snapshot history as CSV, optionally date-filtered.

**Deliberately scoped to this one dataset.** POs and Import POs are already fully present in their own
source CSVs, but the Stock xlsx files only ever hold *today's* position (each sync overwrites the
prior day's numbers), so the `*RMSnapshot` table is the **only** place day-by-day history exists at
all — it cannot be reconstructed from the spreadsheets afterwards. Extending Export to other datasets
is a deliberate follow-up if asked for, not assumed.

- **CSV, not Excel** — per "whichever is less compute and faster". A `.xlsx` would need a whole
  library for no real benefit over a plain CSV any spreadsheet program opens directly.
- **`IsEditor` + `user_can_access_plant()`-gated**, unlike every other GET in `_domestic_base.py`
  (plain `IsAuthenticated`, any role). This is an explicit, deliberate exception, not an oversight.
- **It streams.** A `StreamingHttpResponse` over a generator, so peak memory is one row rather than
  the full export — on a table that grows by one row per active lot per day forever, across three
  plants, where "export everything" is the default request since both date params are optional.
  *Behavioural note:* a streaming response has no `Content-Length` (browsers show an indeterminate
  progress bar) and `.content` no longer exists on the response object — **any test or client reading
  an export must use `.streaming_content`.** The frontend uses `window.open()` and is unaffected.
- **Every string cell is escaped against CSV formula injection.** Material descriptions, categories
  and vendor names come from Drive spreadsheets plant staff edit by hand; a cell beginning `=`, `+`,
  `-` or `@` executes as a formula the moment the download is opened — and "open this in Excel" is the
  entire purpose of the export, so the payload reaches its execution context by design. `csv_safe()`
  is applied through a `SafeCsvWriter` **wrapper rather than per-column calls**, so a column added
  later is protected by construction. Non-string values pass through untouched — quoting a Decimal
  would turn every quantity into text Excel refuses to sum.
- **Download is a plain `window.open()`, not a `fetch()`+blob dance** — browser auth is
  httpOnly-cookie-based, so the cookie rides along on a normal same-origin navigation and
  `Content-Disposition` does the rest. It opens in a new tab so a stale-session failure shows a JSON
  error there instead of blowing away the dashboard.
- The button renders only when `PLANT_KEYS.some(canEditField)`, and each plant's row inside the panel
  is gated individually — a plant-scoped editor may have export access to some plants but not others.

---

## Auth & security

Ported from the TDS Automation App's auth stack: device-aware 2FA (email OTP on a new device, cookie
device-trust thereafter), JWT-in-httpOnly-cookie auth with a Bearer-header fallback for non-browser
clients, role-based permissions, the `core`/`api`/`services` layering, DB-backed cache with a
test-mode override, hardened security headers, admin-only CSRF, a shared DRF exception handler, and
CI. See that app's docs for the original design writeup.

**Deliberately NOT ported** — don't "fix" these back without re-deciding:

- **No `django-cors-headers`.** This frontend is same-origin (WhiteNoise serves it from the same
  process), so there is no cross-origin case CORS would solve.
- **`uv`/`pyproject.toml`, not `pip`/`requirements.txt`.** CI's `pip-audit` runs against the
  `uv`-managed venv directly.
- **`pytest`/`pytest-django`, not `manage.py test`.** Already the convention here before the port.
- **No migration-history baggage.** `PTUser`/`OTPCode`/`TrustedDevice` are plain `AutoField`-PK models
  with a clean history; none of TDS's `TDSUser` workarounds apply.
- **`cache_page` infrastructure exists, nothing uses it.**

### Non-negotiables

**`AUTH_USER_MODEL` stays at Django's default (`auth.User`).** This app's real user model, `PTUser`,
is a plain unrelated model, not a Django auth user. Every authentication path resolves `PTUser`
directly instead of touching `get_user_model()`: `PTUserBackend.authenticate()`/`get_user()`,
`PTJWTAuthentication.get_user()` (reads the `sub` claim), and `pt_user_authentication_rule()` (all in
`auth_backend.py`).

**Any new code — or any simplejwt/DRF upgrade — that calls `get_user_model()` will silently resolve to
`auth.User`, not `PTUser`, and almost certainly do the wrong thing or crash.** This is not
theoretical: it caused a real production incident in TDS (simplejwt's stock
`TokenRefreshSerializer.validate()` calls `get_user_model().objects.get(**{USER_ID_FIELD: ...})` to
re-check the user is active, and `auth.User` has no `user_id` field — every token refresh crashed).
Fixed here proactively: `PTTokenRefreshSerializer` overrides `validate()` to resolve `PTUser`
directly, wired in via `PTTokenRefreshView.serializer_class`. **Before adopting any simplejwt/DRF
upgrade, grep it for new `get_user_model()` call sites.**

**Never add `rest_framework_simplejwt.token_blacklist` to `INSTALLED_APPS`.** Confirmed the hard way:
it crashes `device_verify` with `"OutstandingToken.user" must be a "User" instance` the moment a
`PTUser` is passed to `RefreshToken.for_user()`, because that app's `OutstandingToken` FKs to
`AUTH_USER_MODEL`. Revocation here is `RevokedRefreshToken` instead — a small custom table keyed on
`jti` alone, no user FK needed, written by `token_revocation.py`'s
`revoke_refresh_jti()`/`is_refresh_jti_revoked()`. (`ROTATE_REFRESH_TOKENS`/`BLACKLIST_AFTER_ROTATION`
are `True` and work fine without that app.)

**`cache_page` must never sit above a permission check, and any `cache_page`-wrapped view must be
`AllowAny`.** `cache_page` short-circuits on a hit and returns the stored response without re-invoking
the view — so a permission check inside the view only runs on the request that *misses* the cache;
after that, the same response is served to any caller regardless of auth for as long as the entry
lives. This bit TDS in production on a real endpoint. No endpoint here uses it; if one ever does, it
must be genuinely public data and gate nothing sensitive.

**`SecurityHeadersMiddleware` must sit before `WhiteNoiseMiddleware` in `MIDDLEWARE`, not after.**
Django's list is outermost-first for the request phase, so a later entry is more inner.
`WhiteNoiseMiddleware` short-circuits static-file responses (every frontend HTML/CSS/JS page) by
returning directly, without calling further down the chain — a middleware placed after it never runs
for any static response, which in a frontend with no SPA shell is most of what a browser renders.
TDS shipped with this backwards for a while; this app has it correct. **If you reorder `MIDDLEWARE`,
re-check this specifically.**

### Roles and plant scoping

Roles: **`admin`** (full access + user management), **`editor`** (full dashboard access + dismissing/
overriding flags + inline corrections), **`viewer`** (read-only).

Read endpoints are plain `IsAuthenticated` on *role* — any role can read, by design — **but they also
narrow by `PTUser.plants`** via `permissions.user_can_access_plant()`, the same underlying check
`user_can_edit_plant()` uses for writes. **An empty `plants` list means "all plants"**, not "no
plants", so accounts predating the field are unaffected and no backfill was needed.

How an out-of-scope plant is refused differs by endpoint shape, deliberately: domestic single-plant
reads **403** outright; Import's cross-plant `purchase_orders`/`sync_status` **silently narrow** the
combined result (the same "you see less, not an error" shape the endpoint already had for a plant with
zero rows); `purchase_order_detail` **404s** rather than 403, matching its existing unknown-plant
behaviour rather than confirming a PO exists.

`IsEditor` gates: `correct_field` (domestic and import), `correct_material_field`,
`dismiss_po_mir_match`/`dismiss_mir_stock_match`/`dismiss_flag`, and the snapshot export.
`IsAdmin` gates: `sync_trigger` and every `users_views.py` endpoint (including the password field on
`PATCH /api/auth/users/<id>`).

**If you add a new read OR write endpoint, gate both role and plant** — don't leave a new endpoint
unscoped by plant just because it's "only a read".

`permissions.is_allowed_email_domain()` restricts accounts and logins to `@<ALLOWED_EMAIL_DOMAIN>`
(default `ravasco.com`), enforced at login, Google OAuth, and account creation. The delete-user gate
reads `DELETE_USER_ALLOWED_EMAIL` (defaulting to the previously hardcoded address) so the restriction
survives a personnel change without a code change and redeploy.

### Throttling, lockout, and brute-force counters

**Login throttles are keyed per-account, not per-IP.** `AnonRateThrottle`'s default `get_cache_key()`
keys on client IP, which `LoginRateThrottle` (5/min) and `DeviceVerifyThrottle` (10/min) inherited —
so every caller behind the same office router/VPN/NAT exit IP shared **one** bucket. A handful of
colleagues signing in within a minute exhausted it, after which every attempt from that IP got a
generic `429 {"detail": "Request was throttled..."}` indistinguishable from a real failure. Reported
as sign-in "misbehaving very much even [with] correct credentials".

Fixed: `LoginRateThrottle.get_cache_key()` keys on the submitted `email` (falling back to the IP key
only when no email was submitted); `DeviceVerifyThrottle.get_cache_key()` keys on the session's
`pending_user_id`, which is unique per in-flight login attempt. Per-account strength is unchanged; it
just no longer pools unrelated accounts. **If you add another `AnonRateThrottle` subclass anywhere in
the auth flow, key it the same way or this exact bug reappears.**

Higher-blast-radius writes have their own scopes rather than the generic 200/min "user" bucket:
`SyncTriggerThrottle` (each request queues a real Drive-sync job) and `AdminWriteThrottle` (user
create/update).

**Account lockout sits on top of throttling, not instead of it.** A rate limit slows password guessing
but never stops it, with no signal an account is under sustained attack. `PTUser.failed_login_attempts`
/`locked_until` (migration `0020`): `PTUserBackend.authenticate()` increments on a wrong password and
locks for 15 minutes at 5, resetting on success. **The lockout check runs before the bcrypt check and
burns an equivalent dummy-bcrypt delay** (`_dummy_verify()`), so a locked account isn't
distinguishable by timing from a wrong password on an unlocked one. **Google OAuth honours the lockout
too** — it once checked only `is_active`, so five failed passwords locked the password door and left
the Google door open.

**The brute-force counters are row-locked.** `_register_failed_attempt()` and `otp_service.verify_otp()`
each used to compute a new value from an in-memory object and write it back separately; concurrent
requests could read the same stale count and each write the same single increment, undercounting
attempts. Both now use `select_for_update()` + `transaction.atomic()` around the whole check.
`revoke_all_sessions()`'s `token_version` bump uses an `F("token_version") + 1` in-DB expression
instead, since it needs no decision based on the read value.

### Sessions and tokens

`pt_access` (12h) and `pt_refresh` (30 days, path-scoped to `/api/auth/`) are httpOnly. A non-browser
client can send `Authorization: Bearer <token>` instead — `PTCookieJWTAuthentication` tries the cookie
first, then the header.

**The refresh token never appears in a response body.** It once did in `login`/`device-verify`,
handing a 30-day credential to any script on the page at the exact moment of authentication; it now
travels only as the httpOnly cookie, passed to the view under a private `_refresh` key the view pops.
`PTTokenRefreshView.post()` re-cookies the rotated token the same way.

**"Log out everywhere"** is `PTUser.token_version` (migration `0021`), embedded as a `ver` claim in
every JWT. Both `PTJWTAuthentication.get_user()` (checked on every request) and
`PTTokenRefreshSerializer` reject a token whose `ver` doesn't match. This is stronger than
`RevokedRefreshToken` alone, which only knows about tokens explicitly rotated away or logged out and
never covered a live access token. `revoke_all_sessions()` bumps the counter **and** deletes every
`TrustedDevice` row (a fresh sign-in goes through 2FA again). Wired to
`POST /api/auth/logout-everywhere` (self-service, any authenticated account) and
`POST /api/auth/users/<id>/logout-everywhere` (`IsAdmin`, e.g. a reported compromise). Verified with a
real multi-client test proving a second already-logged-in client's live access token stops working the
instant the first revokes.

**A password change evicts sessions.** Both password-setting paths once saved a new hash and stopped,
touching nothing that invalidates an already-issued JWT — so a stolen access token stayed valid for its
full 12h and the sliding refresh cookie could renew indefinitely. Since the overwhelmingly common
reason to change a password is suspected compromise, the remedy did not work against the case it
exists for. `revoke_all_tokens()` is **split out from `revoke_all_sessions()` on purpose**: the latter
also deletes every `TrustedDevice`, which is right for a panic button but wrong for a routine rotation
(a `pt_device` cookie only ever skips the OTP step, never the password, so it is worthless to an
attacker once the password changes — wiping it would re-challenge every colleague on every device for
no security gain). The self-service path re-issues fresh cookies for the caller in the same response,
so changing your own password doesn't sign you out of the browser you changed it in.

`prune_revoked_tokens` deletes `RevokedRefreshToken` rows past their own `expires_at`. Session cookie
age is an explicit 30 minutes (not Django's 2-week default) — that session only ever carries
short-lived state (OAuth PKCE `code_verifier`, or `pending_user_id` during the 10-minute OTP window),
never the main JWT auth path.

bcrypt cost is pinned explicitly at `rounds=12` in both `users_views.py` and `create_pt_user.py` —
the same value bcrypt already defaulted to, just no longer implicit.

**Password policy** (`_validate_password_strength`, mirrored in `create_pt_user.py`): minimum **10**
characters, rejects a purely-numeric password, rejects one identical to the account's email
local-part. Deliberately modest — this is an admin-bootstrapped internal tool, not a public signup
form. The minimum lives in five places across three languages, so
`test_password_policy_is_stated_consistently.py` probes the real validator and asserts every
placeholder, client-side check, error message and CLI copy agrees with it.

### Alerts, audit log, and logs

`security_alerts.py` sends three best-effort, never-propagating email alerts (this app previously had
zero alerting on security events, only passive log lines nobody watched): an **account-locked** alert
the moment lockout actually fires (not on every failed attempt); a **login-burst** alert (cache-backed
counter — 15 failed logins across *any* accounts, including nonexistent emails, within 5 minutes,
suppressed to at most once per 30 minutes) for a credential-stuffing shape no per-account throttle
would catch; and a **sync-failure** alert.

`apps/core/audit_log.py` defines `PTAuditLog` (`pt_audit_log`) and
`log_pt_action(request, action, detail='', actor=None)`. Actions: login, logout, user created/updated/
deleted, device revoked, sessions revoked. **Don't add action types speculatively — add one only when
a real mutating endpoint exists to log.** Wired into all three login paths (trusted-device fast path,
new-device OTP verify, Google OAuth) and logout. Browsable read-only in Django Admin with add/change/
delete disabled, matching the append-only intent. `log_pt_action()` never raises: a broken audit write
must not block a real login/logout.

Separately, `LOGGING` writes every `INFO`+ line (Django's own plus every `apps.*` logger) to a rotating
`logs/app.log` (10MB × 5 backups) on top of console. **Don't conflate the two**: `logs/app.log` is an
operational trace (what the server did); `pt_audit_log` is a permanent security record of who logged
in/out and from where. Neither substitutes for the other.

`PTCookieJWTAuthentication.authenticate()` catches `(InvalidToken, AuthenticationFailed)` at `DEBUG`
and everything else at `WARNING` with a full traceback, still falling through to the Bearer attempt.
It used to catch bare `Exception`, treating a genuine bug (e.g. a DB error inside `get_user()`) the
same as an ordinary expired cookie. Raising outright here would wrongly turn a bad cookie into a hard
error for browser clients that never send a Bearer header.

`exceptions.py` deliberately excludes `KeyError` from `_DESCRIBABLE_EXCEPTIONS` — returning `str(exc)`
for one leaked internal dict key names AND reported a genuine server bug to the caller as a 400. It
falls through to a generic 500 now, still logged with a traceback.

### Deploy-time checks and headers

`apps/core/checks.py` (registered via `CoreConfig.ready()`) runs as part of
`manage.py check --deploy --fail-level WARNING`, the same command CI gates on:

- `check_jwt_signing_key_is_independent` warns (outside `DEBUG`) if `JWT_SIGNING_KEY` is still silently
  falling back to `SECRET_KEY`.
- `check_no_unexpected_django_superuser` warns if an `auth.User` superuser exists at all — real login
  never touches `auth.User`, so a superuser is an undocumented, unaudited path into `/admin/` that
  bypasses `PTAuditLog`.

**CSP** (`config/security_headers.py`): `script-src 'self' https://cdn.jsdelivr.net`,
`style-src 'self' https://fonts.googleapis.com` — **no `'unsafe-inline'` in either**. Dropping
`script-src`'s required extracting every inline `<script>` to its own file and converting every inline
event-handler attribute to a real listener (the logo-fallback `onerror` pattern is one capture-phase
listener in `theme-init.js` keyed on `data-hide-on-error`; the modal close-button `onclick` pattern is
one delegated listener in `charts.js`). Dropping `style-src`'s required moving static inline styles to
`brand.css` utility classes and genuinely dynamic ones to `data-*` attributes plus a small JS pass
(`shared.js`'s `applyDynamicStyles()`, `admin-page.js`'s `renderBarList()`).

DRF's **Browsable API is disabled** (`DEFAULT_RENDERER_CLASSES` pinned to `JSONRenderer`) — this is an
internal same-origin JSON API with a static frontend; the interactive HTML/schema UI DRF enables by
default regardless of `DEBUG` was attack surface for no benefit.

`index.html`'s jsdelivr-hosted Chart.js `<script>` carries a Subresource-Integrity hash computed
against the exact pinned `chart.js@4.5.0` build, so a compromise of that CDN file is blocked by the
browser rather than silently executed.

The `pt_device` cookie's `secure` flag reads `settings.PT_DEVICE_COOKIE_SECURE` directly rather than
`getattr(settings, ..., False)` — it fails loudly if the setting is ever removed instead of silently
degrading to an insecure cookie, matching `pt_access`/`pt_refresh`'s fail-closed style.

**Injection status**: three parallel audits found no exploitable injection anywhere — zero raw
SQL/eval/exec in `apps/`, ORM-only DB access, the Drive query escaped, and the frontend consistently
running dynamic content through `escapeHtml()`/`textContent` before touching `innerHTML`. Keep it that
way. Every URL segment in `shared.js`/`material-modal.js` is `encodeURIComponent()`-wrapped even where
the value is backend-issued today, so a future reuse of that URL-building code can't silently regress.

**Dependabot** (`.github/dependabot.yml`) monitors pip + GitHub Actions with a **7-day cooldown**
(`cooldown.default-days: 7`, project owner: "open source library will be updated only after 7 days of
any new version") — giving the upstream community a window to catch a broken or malicious release
first. A real CVE fix still lands within the same week.

---

## Outgoing email

Every email goes through one of three builders: `email_service.render_email()` (plain paragraphs,
shared by 7 of the 10), `consumption_report.py`'s table builder (8–9), or
`plant_mismatch_report.py`'s table builder (10). **Emails 1–9 end with "This is a system generated
email. Please do not reply." in both HTML and plain text, and none use an em dash in body content**
(this app's convention is a plain `" - "` and `"N/A"`). **Email 10 deliberately has no such footer.**

| # | Email | Recipients |
| --- | --- | --- |
| 1 | Login OTP (`device_service.send_device_otp()`) | the signing-in user, on a new/untrusted device |
| 2 | New device signed in (`send_new_device_notification()`) | that same user, right after verifying |
| 3 | New device login alert (`notify_admins_new_device_login()`) | every other active admin |
| 4 | Password change OTP (`password_service.send_password_change_otp()`) | the requesting user |
| 5 | Account locked (`security_alerts.notify_admins_account_locked()`) | every other active admin, once, when lockout fires |
| 6 | Unusual login activity (`record_failed_login_and_maybe_alert()`) | every active admin |
| 7 | Sync failure (`notify_admins_sync_failure()`) | `dishant.barot@ravasco.com` only — an explicit request, unlike 3/5/6/8 |
| 8 | Daily RM Consumption Report (×3, one per plant) | every active admin |
| 9 | Monthly RM Consumption Report (×3) | every active admin |
| 10 | Plant Data Correction Report (×3) | a fixed plant head each, CC'ing every admin |

**8 — Daily RM Consumption Report.** Triggered by the external scheduler hitting
`POST /api/internal/send-daily-report` (shared-secret `REPORT_CRON_SECRET`). A real HTML table
(Material / Issued Today / Latest Rate / Days Left), **grouped by category** — `build_plant_report()`
reads the `category` field every `*RMLot` already has, rendering one shaded heading row per category,
ordered by whichever category holds that plant's single biggest mover for the day (materials within a
category stay issued-qty-descending). A blank category buckets as **"Uncategorized"** rather than
being dropped — real on some rows.

*Achhad's "Issued Today" fallback.* The report prefers a genuinely-dated source per plant, because
Achhad's own `issued` column is a period-to-date summary that resets each period — so it uses
`RTPAchhadRMDailyMovement`, parsed from the Stock file's daily Recp./Issue matrix. Confirmed against a
real exported file: that matrix can sit **completely blank** for the current day (every cell `None`)
while the lot's period-to-date `Issued` already has nonzero values — the plant simply hadn't filled in
today's column yet, and the report silently said "No material was issued today". Now
`_todays_issued_rows()` falls back to the live `issued` column **only** when a lot has no
`RTPAchhadRMDailyMovement` row for today at all (a lot that has one keeps using it — the fallback never
overrides a real dated entry), flags it `isEstimate=True`, and renders `(est.)` plus a footnote. This
is **explicitly presented as a rough period-to-date indicator, not a confirmed same-day figure**. If
Achhad's entry lag is a permanent habit, most days will show `(est.)` — that's expected under this
design, not a sign something is broken.

**9 — Monthly RM Consumption Report.** `POST /api/internal/send-monthly-report`, a separate endpoint
rather than a mode flag since the two run on genuinely different schedules. Same category-grouped shape
as #8 (both share `_render_consumption_rows()`/`_render_consumption_email()` so they can't drift
apart), summed over a calendar month; the column reads "Issued This Month". Defaults to the most
recently **completed** month — the natural target for a report firing on the 1st; `year`/`month` query
params override for a manual re-send. `daysLeft`/confidence stay a present-tense estimate, same meaning
as in the daily report.

**It sums, rather than reading a live period-to-date column.** HRS/Vapi sum `*RMSnapshot.issued` across
the month (already a genuine per-day figure, so summing is exactly as trustworthy as the daily read);
Achhad sums `RTPAchhadRMDailyMovement.issued`, deliberately **not** the live `issued` column — reading
that for an already-closed month would be flatly wrong the moment the period rolls over and the sheet
resets. Achhad's `(est.)` fallback exists here too but is **only ever allowed for the current, still-open
month**. If Achhad genuinely has no movement rows for a closed month, that material is **silently
excluded rather than estimated** — a visible gap is more honest than an untrustworthy number.

**10 — Plant Data Correction Report.** `POST /api/internal/send-mismatch-report`. Unlike every other
email here, this goes to a **fixed, real individual per plant, not the internal admin list**:
`avijit.ghosh@ravasco.com` (HRS), `anil.khatri@ravasco.com` (RTP-Achhad), `mahendra.patil@ravasco.com`
(RTP-Vapi) — see `_PLANT_HEADS`. None is necessarily a `PTUser`; the list is a fixed dict, not derived
from the DB. **Every active admin is CC'd.**

It lists that plant's currently-flagged (not dismissed) `qty_mismatched`/`rate_mismatched` rows from
both `*POMirMatch` (PO Number, Material) and `*MirStockMatch` (Material) — **the exact same booleans
the dashboard's Data Quality Flags read, not a re-derived definition**. "Whichever applicable": a row
only shows a percentage for the mismatch actually true on it (`-` for the other), never a stale or
zero-looking figure. **No "system generated / do not reply" footer** — this email is meant to prompt a
real reply and a corrected source document, so telling the recipient not to reply would defeat the
point; it ends with a plain "Regards,". A plant with nothing flagged is **skipped entirely**, not sent
a routine all-clear.

Correcting the source and re-syncing reaches the dashboard automatically: the next `sync_*`/`match_*`
run re-parses the corrected file and recomputes `qty_diff_pct`/`rate_diff_pct`/`is_flagged` in place,
and the dashboard reads live from the API with no cache to invalidate — so a corrected mismatch simply
stops appearing. No separate manual step.

**Dedup guard.** Neither daily nor monthly report had protection against the external scheduler
double-firing (a network retry, or an overlapping schedule) — every admin would get a duplicate email
with nothing to stop it. `ReportSendLog` (migration `0045`) holds one row per
`(report_type, plant, period_key)`, unique-constrained; each plant claims its row via `get_or_create()`
**before** building/sending (closing the actual race, not just a check-then-send a double-fire could
slip through), and **releases the claim if building/sending then raises**, so a transient failure stays
retryable rather than permanently burning that period's slot.

**Deliberately NOT applied to the Plant Data Correction report** — asked rather than guessed. That
report has no fixed cadence by design, so a once-per-day lock could block an intentional same-day
re-trigger.

### Dispatch: two bounded thread pools, not the task queue

`_dispatch_email()` used to run `threading.Thread(...).start()` per message — a fresh OS thread per
email with nothing capping how many could exist. One login sends three, so twenty colleagues signing in
from new devices at 9am meant sixty threads, each holding an SMTP socket for up to `EMAIL_TIMEOUT`
(10s). `password_service.send_password_change_otp()` was worse: it bypassed the shared helper and
spawned its own `daemon=True` thread, so a worker restart mid-send silently killed that OTP — exactly
what `_dispatch_email`'s non-daemon behaviour exists to prevent, and which the *login* OTP was already
protected from. It also had no inline-under-pytest branch, so it behaved differently under test than
every other email.

**Why not django-q2, which is installed and already running a worker:** it would make **login depend on
the qcluster worker being alive and responsive**. Today the send happens in the web process, so signing
in is self-contained; queued, a worker that is down or backed up means nobody can sign in from a new
device at all. The ORM broker also polls, adding seconds to a code someone is watching the screen for,
and django-q2 pickles its tasks while these sends are closures over local state. **Trading a login
outage for architectural tidiness is a bad deal.** The scheduled reports already run on the worker,
which is the right home for that kind of mail. **Read this before "finishing the job" by queueing
these — the reasoning against it is the point, not an omission.**

**The fix is two bounded `ThreadPoolExecutor`s** — two rather than one for the same reason the queue was
rejected: an OTP must never wait behind a backlog of admin alerts. `_dispatch_email(fn, priority=True)`
routes the two user-is-waiting emails (login OTP, password-change OTP) to a dedicated lane; everything
else stays on the bulk lane. Sizes are env-tunable (`EMAIL_OTP_POOL_WORKERS`/`EMAIL_BULK_POOL_WORKERS`,
default 4 each).

**Preserved deliberately:** the inline-under-pytest branch (load-bearing — every test asserting against
`mail.outbox` right after a request depends on it); non-daemon shutdown semantics
(`ThreadPoolExecutor` workers are non-daemon and Python joins them at exit, so a clean shutdown lets an
in-flight OTP finish); and a fallback that sends **inline rather than dropping the message** when the
pool refuses new work at interpreter shutdown — a user holding a code that was never sent is the one
outcome worth avoiding at any cost.

Measured, not assumed: dispatching 40 emails creates 8 threads, not 40, with all 40 delivered; with the
bulk lane deliberately jammed by 40 stalled sends, OTP start latency stayed at ~2ms where a single
shared lane would have waited ~4s.

---

## Testing, lint, CI

**Test scopes are split deliberately**, mirroring TDS's own split:

- **`apps/api/tests/`** — integration. Real Postgres test DB, no mocking, a shared `make_user()`
  factory (`factories.py`). Covers the auth flow, inline-edit and dismiss endpoints, password reset,
  plant scoping, exports, reports, and the cross-file consistency guards.
- **`apps/services/tests/`** — pure-function unit tests for the reconciliation math (`_closeness()`,
  `_diff_pct()`, `_token_overlap()`, `_vendor_matches()`, `_po_number_matches()`) and the parser
  normalization layer (`to_decimal()`, `to_date()`, `normalize_vendor()`, …). **No Django DB touched**
  — those functions were written dependency-free for exactly this — so it runs in well under a second.
  Pipeline-level tests that *do* seed rows and run `run_full_match()` live here too.

Coverage was measured at **92%** across `apps/` + `config/`, with CI enforcing `--cov-fail-under=90`
(set below the measured value so ordinary refactors don't fail it). **Coverage is not assertion**:
`middleware.py` and `security_headers.py` were ~95% covered but never *asserted* about, so
`test_security_headers_and_csrf_scope.py` now pins the CSP's actual contents and
`AdminOnlyCsrfMiddleware`'s scope in both directions.

**A test must fail when the fix is reverted.** One CSRF test initially passed even with
`ADMIN_PATH_PREFIX` deliberately broken, because Django's admin login view carries its own
`@csrf_protect` — it was testing Django, not this middleware. It was replaced with a direct
`process_view()` test that fails in both directions.

Several guards are **cross-file by design**, because the drift they catch isn't visible to a
single-unit test:

- `test_local_date_timezone.py` scans app code for any new naive `date.today()`/`datetime.now()`.
- `test_password_policy_is_stated_consistently.py` probes the real validator and checks every copy of
  the minimum agrees.
- `test_css_token_collisions.py` checks `brand.css` and `style.css` don't define the same custom
  property with different values — it **found a fifth collision a manual grep sweep had missed**,
  which is precisely why it is a test and not a one-time cleanup.
- `test_deploy_checks.py`, `test_ensure_schedules.py`, `test_material_category_reference.py`.

**Ruff is a real gate, deliberately narrow.** The default ruleset reports ~1,000 findings here, almost
all style (572 `FURB157` alone). Mass-`--fix`ing that across 20k lines is exactly the sweeping,
untestable churn that breaks a working app, so `[tool.ruff.lint]` selects only rule families that catch
a real defect (`F`, `E4/E7/E9`, `B`, `DTZ`, `RUF013`, `PLE`) and was **driven to zero by hand**.
**Adding a rule later is fine — drive it to zero in the same commit that selects it. Never add a rule
and leave existing violations behind; a lint gate with a known-failing baseline stops being a gate.**

**ESLint has never been executed locally** — there is no Node in this development environment, so
`.eslintrc.json` is unproven and its first CI run establishes the baseline. If it reports findings, fix
them or narrow a rule with a recorded reason; **don't delete the step to get a green build.**
`no-undef` is deliberately OFF and the config records why: all frontend files share one global scope by
design, so ESLint can't resolve cross-file calls without an exhaustive hand-maintained globals list
that would itself become a second source of truth.

**CI** (`.github/workflows/ci.yml`) runs against a real Postgres 16 service: `pip-audit`,
`ruff check .`, `makemigrations --check --dry-run`, `manage.py check`,
`check --deploy --fail-level WARNING` (plain `check --deploy` never fails a build on its own — every
deploy check is Warning-level — so `--fail-level WARNING` is what makes it real), the test suite with
the coverage ratchet, and ESLint.

**Still untested**: the actual Drive API calls and the `sync_*` management commands' own file-fetching.
Verification there stays manual — real parser runs against real files, real sync/match runs against a
real Postgres, direct inspection of the rendered dashboard.

### Accessibility conventions

The app had none of the four basics before a dedicated pass: no `aria-live` regions in an app built on
async sync/filter/save, ~50 generated `<input>`/`<select>` controls with no accessible name (several
identified only by the column above them, all sharing the placeholder "Search..."), no
`role="dialog"`/`aria-modal`/focus trap/Escape on any of the seven modals, 109 `<th>` with no `scope`.
(Div-based buttons and tabs already had keyboard activation and `:focus-visible` rings.)

`shared.js` now holds `openModalA11y()`/`closeModalA11y()` (dialog semantics, `aria-labelledby` from
the modal's own heading, focus moved in and **restored to the opener** on close, Tab/Shift+Tab wrapped,
Escape to close, listeners torn down), `announce()` (one shared polite live region, cleared first so a
repeated message is re-announced), and `applyAccessibleNames()` driven by a **MutationObserver rather
than per-render calls** — same reasoning as `SafeCsvWriter`: make it structural, not a discipline a
future render site has to remember.

Two details worth keeping: `.sr-only` in `brand.css` uses the clip-rect technique, **not
`display:none`**, which would remove the live region from the accessibility tree and silence it. And
the labelling pass debounces with `setTimeout`, **not `requestAnimationFrame`** — browsers don't fire
rAF at all while a tab is hidden, so a render in a background tab stayed unlabelled until the tab was
fronted. Nothing here touches layout, so there was never a reason to wait for a frame.

Skip links and `role="main"` are on all five pages, annotating the **existing** container rather than
introducing a `<main>` wrapper, so there was no layout risk.

---

## Deployment, Docker, and `.env`

### Render

`render.yaml` defines the web service and the qcluster worker. Both must share the same
`DJANGO_SECRET_KEY` **and** `JWT_SIGNING_KEY` via `fromService` — the worker once had its own
`generateValue: true` for `JWT_SIGNING_KEY`, producing a different key from the web service (the exact
bug shape already fixed for `DJANGO_SECRET_KEY`), harmless only because the worker never mints or
verifies a token. `DJANGO_ALLOWED_HOSTS` is pinned to this service's own hostname, not `.onrender.com`,
which matches every other Render customer.

### Docker (local dev only)

Does **not** replace `render.yaml`'s deploy pipeline. It exists so a developer can run a real Postgres +
this app without installing Python/uv/Postgres directly, and so local dev matches Render's actual
runtime. **The `Dockerfile` pins `python:3.12-slim` to match `render.yaml`'s `PYTHON_VERSION: "3.12.8"`,
deliberately not this repo's own `.python-version` (`3.14`)** — parity with the deployed environment is
what matters here.

```bash
docker compose up -d
docker compose exec app uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
docker compose logs app
```

`Dockerfile` bakes `uv sync --frozen` and `collectstatic --noinput` in at build time (static files don't
need a live DB). `docker-entrypoint.sh` runs `migrate --noinput` then `createcachetable` — the latter
guarded with `|| true`, since Django's `createcachetable` isn't safely re-runnable and a container
restart would otherwise error — before handing off to the CMD (gunicorn for `app`, `manage.py qcluster`
for `worker`; same image, different command).

`psycopg[binary]` ships a prebuilt wheel, so the image needs no `libpq-dev`/build toolchain. **Keep it
that way** — if a future dependency needs compiling, add build deps deliberately rather than by reflex.

`docker-compose.yml` reuses `.env` for app secrets via `env_file`, and sets `DATABASE_URL` itself
pointed at its own `db` service by name. **You do not need to change anything in `.env` for Docker to
work**, even though a developer's own `PGHOST`/`DATABASE_URL` typically says `localhost`.

### `.env` gotchas that have already cost debugging time

- **Never paste multi-line JSON into `GOOGLE_SERVICE_ACCOUNT_JSON=`.** `.env` parses one `KEY=VALUE`
  per line; a multi-line paste breaks every following line (symptom: `python-dotenv could not parse
  statement starting at line N` repeated, then `json.loads()` failing with `Expecting property name
  enclosed in double quotes: line 1 column 2`, because the captured value is just `{`). Put it on one
  line, or better, use `GOOGLE_SERVICE_ACCOUNT_FILE` with a path.
- **Never quote a `GOOGLE_SERVICE_ACCOUNT_FILE` Windows path with double quotes.** `python-dotenv`
  applies backslash-escape processing inside double-quoted values, so a path containing `\r` as part of
  a longer word (e.g. `...\secrets\ravasco-...`) has that `\r` read as a carriage return and corrupted,
  breaking the open with a cryptic `[Errno 22] Invalid argument`. Leave the path unquoted.
- **`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` are a separate concern from
  `GOOGLE_SERVICE_ACCOUNT_JSON`/`FILE`** — the former is a human-login OAuth client, the latter the
  unattended Drive-sync service account. One cannot do the other's job.
- **This repo can sit inside a OneDrive-synced folder.** `.env` is gitignored (confirmed — no history),
  but OneDrive syncs independently of git and doesn't respect `.gitignore` at all, so a real secret in
  `.env` can reach Microsoft's cloud and every device on that account. That is a meaningfully different
  exposure path from "is it committed". **Exclude this folder from OneDrive sync before populating a
  real `.env`**, not after.
- `SMTP_HOST`/`PORT`/`USER`/`PASS`/`FROM` may be blank in local dev — `DEBUG=True` falls back to
  printing the OTP to the console. `JWT_SIGNING_KEY` defaults to `DJANGO_SECRET_KEY` (and the deploy
  check warns about that outside DEBUG); `ALLOWED_EMAIL_DOMAIN` defaults to `ravasco.com`.

---

## Traps already hit — don't re-introduce these

Short index of defects that have actually occurred here. The full reasoning is in the section named
alongside each.

| Trap | Where it's explained |
| --- | --- |
| Drive v2 `title =` instead of v3 `name =` → opaque HTTP 400 | [Drive API v3](#drive-api-v3-not-v2) |
| Case-sensitive filename prefix re-check silently dropping a new file | [Drive API v3](#drive-api-v3-not-v2) |
| `ROUND_HALF_EVEN` breaking sync idempotency on `X.XX5` values | [Change detection](#change-detection-must-compare-quantized-decimals-rounded-the-way-postgres-rounds) |
| 2-decimal GST rate truncating a `0.025` fraction to `0.02` | [Decimal precision](#decimal-precision-conventions) |
| Unclamped `*_diff_pct` overflowing `max_digits=6` | [Decimal precision](#decimal-precision-conventions) |
| `source_row_ref` as lot identity splicing two materials' histories | [Stable lot identity](#stable-lot-identity) |
| Header check stripping whitespace, row lookup not → `KeyError` on a resaved CSV | [Per-plant models](#per-plant-models-not-a-shared-schema--deliberate-dont-fix-it) |
| Comparing pre-tax PO value to post-tax MIR value → bogus ~18% gap | [PO ↔ MIR](#po--mir) |
| Vendors with no PO looking like matcher failures for every run | [No-PO vendors](#some-vendors-never-have-a-po--that-is-registered-not-inferred) |
| Exact vendor equality → zero MIR↔Stock matches (city suffix) | [Vendor gate](#vendor-name-is-always-a-hard-gate-never-a-scored-factor) |
| MIR↔Stock comparing MT against KG → ~1000x phantom rate mismatch | [Unit normalisation](#units-are-normalised-before-comparing--on-both-pairings) |
| Import USD rate compared raw against INR MIR → ~94x gap, 0 matches | [Import currency](#import-po--mir-convert-currency-first) |
| Full-table SELECTs inside per-row loops → quadratic matching | [Performance](#performance-the-engine-was-quadratic) |
| `_assign_pairs()` never terminating on a positive-gain cycle (twice: search, then path flip) | [Termination guards](#_assign_pairs-needs-both-its-termination-guards) |
| `legacy_po_matches()` running 844k times where it could never match | [Two hot paths](#two-hot-paths-in-matching-are-cached-or-short-circuited-for-a-reason) |
| Renaming a PO upstream forking it into two permanent rows | [Purchase orders are retired](#purchase-orders-are-retired-not-deleted--and-until-2026-09-18-they-were-neither) |
| Orphan detection reporting only to a stdout nobody reads | [Purchase orders are retired](#purchase-orders-are-retired-not-deleted--and-until-2026-09-18-they-were-neither) |
| `date.today()` returning the server's UTC date, not IST | below |
| Per-IP login throttle locking out a whole office | [Throttling](#throttling-lockout-and-brute-force-counters) |
| Google OAuth ignoring account lockout | [Throttling](#throttling-lockout-and-brute-force-counters) |
| Non-atomic failed-login / OTP-attempt counters undercounting | [Throttling](#throttling-lockout-and-brute-force-counters) |
| TOCTOU race leaving zero active admins | below |
| Password change not evicting any session | [Sessions and tokens](#sessions-and-tokens) |
| Refresh token returned in the login response body | [Sessions and tokens](#sessions-and-tokens) |
| `KeyError` in `exceptions.py` leaking dict key names as a 400 | [Alerts and logs](#alerts-audit-log-and-logs) |
| CSV formula injection in the export | [Data export](#data-export) |
| Duplicate scheduled report emails from a double-fired cron | [Outgoing email](#outgoing-email) |
| Unbounded thread-per-email dispatch | [Dispatch](#dispatch-two-bounded-thread-pools-not-the-task-queue) |
| Stale modal data from a fast row-switch | below |
| `decimal.InvalidOperation` escaping as a 500 | below |
| `[hidden]` losing a CSS specificity tie | below |
| Duplicate CSS custom properties across two stylesheets | below |

**Timezone: use `timezone.localdate()`, never `date.today()`.** Four sites computed "today" in the
*server* process's timezone. Render runs containers in UTC while `TIME_ZONE` is `Asia/Kolkata`, so every
day between 00:00 and 05:29 IST the server's date was still yesterday: `imports_views.py`'s
`deliveryDateStatus` reported "On Order" for POs that had already become Overdue, and
`_domestic_base.py`/`stock_consumption.py`'s consumption windows started a day late. **Invisible in
local dev, which already runs on IST** — the divergence only ever appears on the UTC deployment. Both
the ruff `DTZ` rules and `test_local_date_timezone.py`'s source guard now prevent a new call site.

**The "can't remove the last active admin" check needs a lock over *every* admin row.** The plain read
(`filter(role=admin, is_active=True).exclude(pk=target).exists()`) followed by a separate `.save()`/
`.delete()` had a TOCTOU race: with exactly two active admins A and B, one request acting on A (sees B,
passes) and one acting on B (sees A, passes) could both succeed, leaving zero active admins with no way
back in short of direct DB access. **A naive `select_for_update()` scoped the same way wouldn't have
fixed it** — the two transactions exclude *different* rows and never contend. `_assert_not_last_active_admin()`
locks **every** currently-active admin row, so any two concurrent callers always contend regardless of
which admin each excludes, wrapped in `transaction.atomic()` around the whole check-and-mutate in both
the PATCH and DELETE branches. The regression test is a real two-thread test with a genuine separate DB
connection per thread (`@pytest.mark.django_db(transaction=True)`) — a row lock is only meaningful
across two actually-separate transactions, which the default `django_db` fixture would never exercise.

**Every modal opener needs the stale-response guard.** Clicking row A then row B before A's fetch
resolves can let A's stale response land after B's and silently overwrite the modal, even though the
title and backdrop already show B. The fix is a module-level `modalRequestId` counter: each opener
captures `const myModalRequestId = ++modalRequestId` on entry and re-checks
`if (myModalRequestId !== modalRequestId) return;` after **every** `await` before touching the DOM.
This currently covers `openPoModal()`, `openImportPoModal()`, `openMaterialModal()`, and
`openRodtepScriptDetail()` — two of them were missed when the guard was first applied, and
`charts.js`'s own comment claimed it was used by "every" opener while grep showed only two. **If a
fifth modal opener is added, apply it there too.**

**`Decimal()`'s failure mode is not a plain `ValueError`.** Genuinely non-numeric input (e.g. `"abc"` in
a qty field) makes `Decimal(str(raw))` raise `decimal.InvalidOperation`, an `ArithmeticError` — so a
`try/except (TypeError, ValueError)` doesn't catch it and it propagates as an unhandled 500 instead of
the clean 400 every other bad-input case returns. Every `_coerce_value()` catches it and re-raises as
`ValueError`. If you write a *new* coercion function from scratch, remember this.

**`[hidden]` alone is not reliable once any same-or-higher-specificity display rule exists.**
`.edit-actions { position:absolute; …; display:flex; }` had the same specificity as the browser's
built-in `[hidden] { display:none }` and this stylesheet loads later, so the class rule won every tie
and `actions.hidden = true` had no visible effect: the save (✓) / cancel (✗) icons rendered at all
times, stacked exactly on top of the pencil (both `position:absolute; right:0; top:1px`). Reported as
"the right and wrong ticks" appearing instead of a pencil. Fixed with
`.edit-actions[hidden] { display:none !important; }`. **If you add a new absolutely-positioned class
alongside a `hidden`-toggled element, check this specifically.**

<a id="css-custom-property-collisions"></a>
**Two stylesheets must not define the same CSS custom property.** Five properties were defined with
*different* values in both `brand.css` and `style.css`. `index.html` is the only page loading both and
loads `style.css` second, so `style.css` silently won every shared name — **including for `brand.css`'s
own rules**, which reference those names 16 times to style the shared top nav. The navigation bar that
is supposed to be identical everywhere rendered in a different blue, green, purple and shadow on the
dashboard than on every other page. Confirmed in a real browser by resolving the computed values, not
inferred from the CSS. This was a **repeat** — `style.css`'s own comment already recorded an earlier
instance (`--navy`, `#0f1b2d` vs `#1A2535`) fixed in place with nothing to stop the next one. Fixed by
renaming `style.css`'s copies to `--dash-*` with values byte-for-byte unchanged, so every rule in that
file keeps its exact colour and the only visible change is the nav matching everywhere.

---

## Known gaps

Confirm a gap is still true before treating it as blocking — this kind of list goes stale as fixes land
elsewhere. Check the file or section it points at directly.

- **No automated tests for the Drive API calls or the `sync_*` commands' file-fetching.** Everything
  else — auth, inline edits, dismissals, password reset, exports, reports, and `run_full_match()`
  itself — has real coverage. Verification for the Drive layer stays manual/smoke-level.
- **No measured match accuracy.** `MATCH_THRESHOLD = 0.55` was picked, not measured, and there is no
  precision/recall check. `review.html` exists to collect the labelled data that would enable one.
- **ESLint has never been executed** (no Node in this development environment). Its first CI run
  establishes the baseline.
- **`dev_smoke_test.sqlite3.bak_pre_vendorgate` is still in git HISTORY** (untracked going forward;
  `.gitignore`'s `*.sqlite3` never matched the `.bak_` suffix, now widened to `*.sqlite3.*`). It carried
  4 dev/test `pt_users` rows with bcrypt hashes. History rewriting was deliberately not attempted — if
  any of those passwords was ever reused on a real account, rotate it.
- **`uom_mismatch` on MIR↔Stock matches has no frontend badge yet.** The data-correctness fix is done
  and the field is on the API; rendering it is a small follow-up.
- **`prune_revoked_tokens` has a trigger endpoint but no fixed cadence** configured.
- **`cache_page` infrastructure exists and nothing uses it** — every current endpoint is real business
  data behind auth, not the public reference data `cache_page` is safe in front of.
- **A day where qcluster was down still has no stock snapshot**, and it is deliberately not backfilled.
  The gap is at least visible: `sync-status` exposes `lastSnapshotDate`/`snapshotGapDays`, surfaced as a
  badge when the gap exceeds a day.

**Deliberately not done, each re-decided rather than forgotten:**

- **API pagination** — would mean moving every client-side filter in `po-list.js`/`materials.js`/
  `import-po.js` server-side, a rewrite of the app's most-used surface.
- **A JS bundler / ES modules** — the no-build-step decision rules this out on purpose.
- **Consolidating the per-plant model classes** — see [Per-plant models](#per-plant-models-not-a-shared-schema--deliberate-dont-fix-it).
- **A blanket `ruff --fix`** across 20k lines — see [Ruff](#testing-lint-ci).
- **Moving email to django-q2** — see [Dispatch](#dispatch-two-bounded-thread-pools-not-the-task-queue).
- **TOTP (authenticator-app) second factor** — was built complete and tested, then **fully removed the
  same day** at the project owner's request ("Don't need TOTP, happy with device aware"). It was never
  wired into any UI and no real account ever had it enabled. Reverted, not left dormant: the modules,
  tests, and model fields are gone, and the migration that would have added them was unapplied and
  regenerated clean. **If TOTP is wanted again, it needs rebuilding from scratch** — no code or
  migration history remains to build on.
- **Licenses beyond the Advance Licence ledger.** `AdvanceLicense`/`AdvanceLicenseMaterial` and
  `sync_advance_license` are built and surfaced in the Imports view. Resolving each import PO's own
  Drive folder of Advance Authorisation letters was the deferred part — ask before building it.
