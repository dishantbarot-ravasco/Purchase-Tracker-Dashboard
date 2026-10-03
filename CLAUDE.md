# CLAUDE.md

Guidance for Claude Code when working in this repository. This file holds the rules, the commands,
and the traps. The detail - how each area works, why, and a reference for every source file - lives
in [`docs/`](docs/). Read the doc for the area you are about to touch before changing it.

**No em dashes anywhere** (project owner, 2026-09-22). Not in code comments, docstrings, markdown,
UI copy, or emails - use a spaced hyphen " - ". `test_no_em_dashes.py` enforces it.

**Push directly to `main`** (project owner, 2026-09-28). Commit and push finished work straight to
`main`, not to a feature branch or pull request, unless the owner says otherwise for that change.
CI's full suite then runs on `main`, so run the local checks you can before pushing.

---

## Documentation rule - keep docs/ in step with the code

Every change to code updates the documentation in the **same change**:

1. Find the doc that covers the file (table below). Update its rationale section and its
   `## File reference` entry for that file.
2. **Describe the code as it is now.** Rewrite or delete the text that described the old behaviour.
   No changelogs, no "previously it did X" - unless the history is the reason a trap exists, in
   which case it goes in a trap entry.
3. A new file gets a new `###` entry in its doc's File reference; a deleted file's entry is removed.
4. If a rule, trap, command, or known gap changed, update this file too.
5. Keep existing `###` headings stable when you can - other docs link to their anchors.

This is enforced by hooks in [`.claude/settings.json`](.claude/settings.json)
([`docs_guard.py`](.claude/hooks/docs_guard.py)): editing a code file injects a reminder naming its
doc, and finishing with code changed but no doc changed blocks once and asks for the update. If a
change genuinely needs no doc update (a pure refactor with no behavioural change), say so.

| Area | Doc | Covers |
| --- | --- | --- |
| Architecture, models, config | [architecture.md](docs/architecture.md) | layering, per-plant models, decimal precision, lot identity, `config/`, `apps/core/models/`, admin, checks |
| Data sources and sync | [data-sync.md](docs/data-sync.md) | Drive layout, parsers, `sync_*` commands, change detection, scheduling, full command reference |
| Matching engine | [matching-engine.md](docs/matching-engine.md) | PO↔MIR, MIR↔Stock, import PO↔MIR, registries, flag thresholds, `matching_core.py` |
| API and features | [api-and-features.md](docs/api-and-features.md) | endpoint table, routers, feature rules, inline edits, pins, dismissals, licences, exports, match accuracy, activity log |
| Frontend | [frontend.md](docs/frontend.md) | pages, load order, every JS/CSS file, modals, freshness watcher, accessibility |
| Consumption | [consumption.md](docs/consumption.md) | consumption ledger, rollups, Days-Left, consumption reports |
| Auth, security, email | [auth-security-email.md](docs/auth-security-email.md) | login/OTP/device trust, roles and plant scoping, throttling, tokens, headers, outgoing email |
| Testing and deployment | [testing-deployment.md](docs/testing-deployment.md) | test suites, ruff/ESLint, CI, Render, Docker, `.env` |

See also [README.md](README.md) (setup) and [ARCHITECTURE.md](ARCHITECTURE.md) (request/auth flow
diagrams).

---

## What this app is

A standalone Django service that reconciles Purchase Orders, MIR (Material Inward Register), and Raw
Material Stock for Ravasco's three plants - HRS, RTP-Achhad, RTP-Vapi. It replaces an earlier Claude
Artifact prototype that re-parsed every Drive file on every page load; this app syncs Drive into
Postgres on a schedule, so every viewer sees the same pre-computed results.

**This is a separate service from the TDS Automation App**
(`C:\Users\Admin\OneDrive\Desktop\TDS Automation App\tds_app`) - same conventions (Django + DRF +
Postgres + WhiteNoise, `uv`-managed, static frontend with no build step), but no shared code,
database, or deployment. The auth scaffolding is a deliberate port of TDS's; see
[auth-security-email.md](docs/auth-security-email.md).

---

## Commands

```
uv sync
uv run python manage.py runserver | migrate | makemigrations core | createcachetable
uv run python manage.py ensure_schedules      # idempotent Schedule row (release.sh runs it)
uv run python manage.py qcluster              # worker; nothing scheduled runs without it
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

uv run pytest -q                              # full suite, Postgres from .env, ~4 min, --reuse-db
uv run pytest -q --create-db                  # after adding a migration
uv run pytest --cov=apps --cov=config --cov-report=term --cov-fail-under=92   # CI's gate
uv run ruff check .
uv run python manage.py makemigrations --check --dry-run
DJANGO_DEBUG=false uv run python manage.py check --deploy --fail-level WARNING
# ESLint is CI-only (no Node locally): npx --yes eslint@8 frontend/js --ext .js --max-warnings 0

# Per plant, in pipeline order:
#   HRS:    sync_po_csv        sync_mir        sync_stock        match_hrs     compute_hrs_consumption
#   Achhad: sync_achhad_po_csv sync_achhad_mir sync_achhad_stock match_achhad  compute_achhad_consumption
#   Vapi:   sync_vapi_po_csv   sync_vapi_mir   sync_vapi_stock   match_vapi    compute_vapi_consumption
# Imports: sync_hrs_imports_po_csv / sync_achhad_imports_po_csv / sync_vapi_imports_po_csv
# Ledgers: sync_rodtep, sync_advance_license; reference: load_material_category_reference --file x.csv
# Flags: every sync_* takes --file <path>; sync_*_stock takes --no-snapshot;
#        compute_*_consumption takes --all | --since YYYY-MM-DD (default 45-day lookback)
# Read-only: report_retired_pos, backfill_achhad_po_numbers
# Procurement (MIR entry): sync_procurement_pos [--plant x] - DB-only projection, also in release.sh
# Build only (Dockerfile): strip_js_comments - rewrites frontend/js in place, never on a working copy
# Maintenance: prune_revoked_tokens; backup_database (pg_dump to R2 now - the nightly job by hand);
#              verify_backup (restore the newest R2 dump into <db>_restore_check, compare, drop)
```

Full reference: [data-sync.md#commands](docs/data-sync.md#commands) and
[testing-deployment.md#how-to-run](docs/testing-deployment.md#how-to-run).

---

## Must-know rules by area

Read the linked section before breaking any of these. Each is there because it was broken once.

### Architecture
- Import models from `apps.core.models`; a new model goes in its plant/concern file and into
  `__init__.py`'s imports and `__all__`. Business logic lives in `apps.services`, never `apps.core`.
  [layering](docs/architecture.md#layering)
- Keep `parsers/common.py`, `validation.py`, `stock_identity.py`, `material_identity.py`, `arithmetic_checks.py`,
  `consumption_engine.py` and `stock_rules.py` free of Django imports - migrations import them.
  [layering](docs/architecture.md#layering)
- Per-plant model classes are deliberate for the **Drive mirrors**; never merge them into one table
  with a `plant` column. Records the app owns (procurement: POs for MIR entry, MIRs) are the opposite:
  normalized, one table per entity with a `plant` FK, derived figures never stored.
  [per-plant models](docs/architecture.md#per-plant-models-not-a-shared-schema---deliberate-dont-fix-it),
  [normalized](docs/architecture.md#the-apps-own-records-are-normalized---procurement-2026-09-28)
- Plant differences in domestic routers go in `_PlantConfig` values, and in matching in
  `_MatchConfig` fields - never a branch or a per-plant copy of a helper.
  [routers](docs/architecture.md#domestic-router-de-duplication),
  [matching](docs/matching-engine.md#the-five-core-helpers-live-once-in-matching_corepy)
- HRS/Achhad GST rates have 3 decimals (fractions), Vapi's `gst_rate_pct` has 2 (percentage); keep
  the `_MAX_DIFF_PCT` clamp on `*_diff_pct`.
  [decimal precision](docs/architecture.md#decimal-precision-conventions)
- Stock lots are keyed on `natural_key`, never `source_row_ref`; MIR rows key on `source_row_ref`
  and are deactivated, not deleted. [lot identity](docs/architecture.md#stable-lot-identity)
- Raise `ValueError` with a message to show a user an error; `KeyError` deliberately becomes a 500.
  Only a `ValueError`/`DoesNotExist` raised from code under `apps/` is shown - one from inside Django or
  a library gets a generic 400, so don't rely on a library's wording reaching the user.
  [exceptions.py](docs/architecture.md#appsapiexceptionspy)

### Data sync
- Drive search uses v3 `name =`, never `title =`; the filename prefix re-check stays
  case-insensitive. [Drive API v3](docs/data-sync.md#drive-api-v3-not-v2)
- Change detection uses `sync_utils.unchanged()` with `ROUND_HALF_UP`, never `HALF_EVEN`. Two
  back-to-back syncs must report 0 changed.
  [change detection](docs/data-sync.md#change-detection-must-compare-quantized-decimals-rounded-the-way-postgres-rounds)
- POs are deactivated, never deleted; a hash-skip must check `existing.is_active`. PO lines are
  written as a diff keyed on position (`sync_line_items()`), never delete-and-rebuild - import lines
  too (their pks key the match dismissals); a procurement
  PO line is never deleted at all (MIR lines point at it).
  [PO diff](docs/data-sync.md#po-csv-into-the-procurement-tables-2026-09-28)
  [retired POs](docs/data-sync.md#purchase-orders-are-retired-not-deleted---and-until-2026-09-18-they-were-neither)
- A procurement PO with `source = app` is never written by the CSV projection: the same PO number
  arriving from Drive and from the app stays one row, owned by the app. The two systems are
  **compared, never added** - never sum a Drive-side quantity with an app-side one.
  [app-owned POs](docs/data-sync.md#po-csv-into-the-procurement-tables-2026-09-28)
- The PO CSVs are written upstream by an extraction agent. For Madura fabric it must calculate the
  weight (KG = GSM x width m x length m x rolls / 1000) into QTY, UOM `KG`, in the fixed description
  shape; with no GSM printed (HRS's fabric orders) it never guesses one and writes ROLLS.
  [extraction rules](docs/data-sync.md#rules-for-the-po-extraction-agent---madura-fabric-weight-2026-09-30)
- Parsers use `read_only=True` + `stream_rows()`, never `ws.max_row`; a layout change raises
  `HeaderMismatch` rather than being guessed around. [parsers](docs/data-sync.md#parser-conventions)
- Nothing scheduled runs unless `qcluster` is running; missed snapshot days can't be recovered. The
  cron `0 9-20 * * *` is IST. Never rename `daily-sync-all-plants`.
  [scheduling](docs/data-sync.md#scheduling)
- `compute_*_consumption` stays the last step of each plant's pipeline, and is skipped (with a
  `FAILED` row saying why) when that plant's stock sync failed.
  [scheduling](docs/data-sync.md#scheduling)
- Never use cron-job.org's "test run" on the report jobs. [scheduling](docs/data-sync.md#scheduling)
- The Drive client is per-thread on purpose. [google_client.py](docs/data-sync.md#appsservicesgoogle_clientpy)

### Matching
- Identification is 2-of-3 (PO number, vendor, material) at all plants, behind the PO-contradiction
  and date gates; evidence tiers dominate ranking and `MATCH_THRESHOLD` does not gate matches.
  [PO ↔ MIR](docs/matching-engine.md#po--mir)
- Value comparisons are pre-tax (MIR `net` at HRS/Achhad, `taxable_value` at Vapi); imports compare
  `net_value x exchange_rate`, never the duty-inclusive `total_inclusive_value`. Import *rate* is the
  exception: a cleared line also agrees on landed value / BOE qty against MIR final value / qty, per
  unit, never as totals. Import receipts pair by Bill of Entry number first (MIR `invoice_no`), with
  one corroborating vote, and a BOE shared across different Bills of Lading is not trusted.
  [PO ↔ MIR](docs/matching-engine.md#po--mir),
  [import rate](docs/matching-engine.md#import-po--mir-convert-currency-first)
- Receipts citing exactly one known PO all belong to it; every counted row is saved in
  `group_entries`, and readers must read it. Identical lines of one order (same material, rate,
  unit) are pooled last by `_pool_duplicate_lines()`: shared by quantity once arrived in full, filled
  in line order while partly delivered - never shared by quantity when partial.
  [pooling](docs/matching-engine.md#identical-lines-of-one-order-are-pooled-2026-09-26)
  [one PO, many receipts](docs/matching-engine.md#one-po-many-receipts---the-po-number-is-the-join-key-2026-09-24)
- The no-PO and RM-untracked registries suppress the match, never the row; don't merge the lists.
  `NOT_STOCKED_MATERIALS` is per plant - follow the admission tests before adding an entry.
  [registries](docs/matching-engine.md#what-mirstock-deliberately-skips---two-registries-neither-shared-with-the-po-side)
- "Purchased without a PO" uses `cites_a_po()` (PO-shaped or names a held PO); never loosen
  `is_usable_po_reference()` itself, which guards the contradiction gate.
  [no PO](docs/matching-engine.md#no-purchase-order-behind-it-is-three-questions-not-one-2026-09-21)
- MIR↔Stock date+rate identification is only safe behind the grade-code gate plus tier-1
  exclusivity; stock qty must never become an identifier.
  [date + rate](docs/matching-engine.md#date--rate-is-this-pairings-po-number---behind-two-guards)
- Keep both `_assign_pairs()` termination guards; a losing shipment group is demoted to per-row
  edges, never re-formed. [guards](docs/matching-engine.md#_assign_pairs-needs-both-its-termination-guards)
- `run_full_match()` is one transaction; keep `@transaction.atomic` directly on it (it once drifted
  onto the next function). [overview](docs/matching-engine.md#overview-what-run_full_match-does)
- Every early exit in `match_mir_entry_stock()` goes through `_no_matches()`, or standalone
  re-matches leave stale rows. [file reference](docs/matching-engine.md#file-reference)
- Import figures convert to INR first and compare `qty_as_per_boe`.
  [import currency](docs/matching-engine.md#import-po--mir-convert-currency-first)
- Zero-tolerance `FLAG_DIFF_PCT` stays identical in the three plant modules and `flags.js`'s
  `FLAG_PCT`. The one exception is `qty_tolerance.py`: steam coal, HM plastic, HDPE and Madura fabric
  may come in up to 10% OVER (qty and value, never under, never rate); Madura's roll count, when both
  sides state it, overrides the weight. Client-side qty checks must use `isQtyMismatch()`. [flag thresholds](docs/matching-engine.md#flag-thresholds)

### API and features
- Every new endpoint, reads included, names its permission - `@permission_classes([requires(Perm.X)])`,
  `IsAdmin` or `HasAnyAccess`, never bare `IsAuthenticated` - and calls `user_can_access_plant()` for
  its plant; `test_endpoint_permission_guard.py` enforces both, and `test_plant_scope_no_leak.py`
  checks no GET hands a scoped account another plant's data. A page that reads it must list the same
  permission in `auth.js`'s `PAGE_ACCESS`.
  [roles](docs/auth-security-email.md#roles-and-plant-scoping),
  [conventions](docs/api-and-features.md#endpoint-conventions-worth-knowing-before-adding-one)
- Never name a query param `format` (DRF reserves it). Read body flags with `_request_bool()`,
  never `bool(...)` (`bool("false")` is `True`). Use `timezone.localdate()`, never `date.today()`. [conventions](docs/api-and-features.md#endpoint-conventions-worth-knowing-before-adding-one)
- Every CSV export uses `SafeCsvWriter`. [data export](docs/api-and-features.md#data-export)
- Treat every match as a suggestion; never wire an automatic action off one.
  [match accuracy](docs/api-and-features.md#match-accuracy-manual-validation-is-required-not-optional)
- Manual receipt decisions (pins and `ManualReceiptEdit` add/remove) are written only by
  `manual_receipts.apply_change()`, and every change in the panel is previewed through
  `receipt_preview.py` on the worker, never in the request. An added receipt joins the line's automatic
  receipts; only a pin replaces them.
  [receipts](docs/api-and-features.md#editing-a-lines-receipts-one-at-a-time-2026-09-29)
- A manual pin names a MIR number, not a row, and `po_kind` belongs in every pin query. "Keep both"
  (`shared`) claims nothing; an import pin naming its own BOE's receipt defers to BOE settlement. A
  pinned line still collects its other receipts citing the same order (PO-number groups, step two) -
  never exclude it there, or they land on a sibling line (HRS 3000001167).
  [pins](docs/api-and-features.md#editing-which-mir-a-po-line-matched-2026-09-21)
- Corrections mutate the real row plus an audit row, re-match on the background worker
  (`rematch.request_rematch()`, never inside the request), and are overwritten by the next sync. Matcher `defaults` never carry `dismissed_*`; only `_save_po_mir_match()` clears it,
  when a line is re-pointed to a different MIR row.
- Match dismissals are keyed on the pair (`match_pairs.py`: PO line + MIR row, or MIR row + lot),
  never the match row's id - the matcher recreates rows under new ids. The match types live on
  `MatchDismissal.MatchType` (the Review Matches page and its `MatchReview` table were removed
  2026-10-01, owner's request).
  `run_full_match()` restores `MatchDismissal` onto recreated rows; readers still filter on
  `dismissed_by_override`.
  [dismiss](docs/api-and-features.md#dismiss--override-a-flagged-match-or-flag)
  [inline edit](docs/api-and-features.md#inline-edit-everywhere),
  [dismiss](docs/api-and-features.md#dismiss--override-a-flagged-match-or-flag)
- The licence CSV says which licence was used, not how much; never derive a balance from it.
  [licences](docs/api-and-features.md#licences-the-import-side-was-in-the-csv-all-along-2026-09-22)

### Frontend
- No build step: all scripts share one global scope, so load order is the dependency graph and a
  name defined in two files silently overrides.
  [load order](docs/frontend.md#serving-load-order-and-the-one-global-scope)
- No literal `style="..."`, inline `<script>`, or `on*=` handlers - CSP blocks them silently. Use
  `data-*` + `applyDynamicStyles()` or a real listener.
  [load order](docs/frontend.md#serving-load-order-and-the-one-global-scope)
- A header-filter keystroke re-renders the list region only; `debounceRender()` is a factory,
  created once.
  [filters](docs/frontend.md#a-header-filter-keystroke-re-renders-the-list-region-only---never-the-whole-view)
- Every modal opener that awaits a fetch needs the `modalRequestId` guard after each await, plus
  `openModalA11y()`. Never merge `pageCharts` and `modalCharts`.
  [modals](docs/frontend.md#modals-one-shared-shell-and-the-stale-response-guard)
- Refresh paths call `clearDataCaches()` and release `MANUAL_SYNC_RUNNING` on every exit.
  [freshness watcher](docs/frontend.md#the-dashboard-keeps-itself-fresh---mainjss-freshness-watcher)
- Deep-link params are attacker-supplied: whitelist, render via `textContent`, consume in `finally`.
  `?po=` is Domestic unless `kind=import` - one PO number can be both.
  [deep links](docs/frontend.md#search-po-deep-links-into-the-dashboard-rather-than-)
- The UI never explains how matching works (owner, 2026-10-02): no method, weights or score on screen;
  the confidence badge names its level only. [matching copy](docs/frontend.md#the-ui-does-not-explain-how-matching-works-owner-2026-10-02)
- Don't merge `brand.css` and `style.css`, and never define the same custom property in both.
  [CSS collisions](docs/frontend.md#css-custom-property-collisions)
- A selected or primary control uses `--dash-solid` (navy light, gold dark), never `--navy-solid`,
  which vanishes on the dark page. No `nowrap` on list-cell content: `* { min-width: 0 }` lets a track
  shrink under it and it paints over the next column (Days Left's "No movement" did).
  [overlap](docs/frontend.md#nothing-may-paint-over-the-next-column-2026-09-30)
- Raw Material links each PO line to its best material through `lineLinksFor()`'s token index, built
  over the unfiltered scope; never reintroduce a per-(material x line) loop (it was 1.3M checks).
  [materials.js](docs/frontend.md#frontendjsmaterialsjs)
- Every sortable list and table (Raw Material list and modal, Purchase Orders, Import Purchases, the
  PO modals' lines and receipts, Search PO, the three plant stock tabs) shares `list-sort.js`; presets, picked values included, are saved
  per user and per list on the server. Each list's `*_SORT_COLUMNS` keys and `pick: true` columns
  must equal `sort_presets.py`'s `SORT_KEYS_BY_VIEW` / `PICK_KEYS_BY_VIEW` (a test checks). A modal
  table's sort moves its rows (`resortSortedTables()`), never re-renders them - that would reset an
  in-progress correction. A list must sort before it draws its sort bar.
  [list-sort.js](docs/frontend.md#frontendjslist-sortjs)
- Raw Material Analysis is admin-only, on the server too: `/materials` drops `mirStockMatches`,
  `dataQualityFlags`, `corrections` and `mirMatched` for non-admins, so the plant tabs must never read
  them. Each of Inventory / On Order / Stock & Orders is its own permission (`plant-stock.js`), built from Raw Material Analysis's own classes and layout. Those tabs reuse
  materials.js's helpers for every figure - never re-derive stock, open qty or Days Left there - and
  never add stock to ordered quantity (Achhad lots have no unit). Material links go through
  `openMaterialLink()`, which routes by role. `vendorContains()` matches identical names at any length
  first, like `_vendor_matches()` - never let the 4-character floor apply to equal names. Each plant's
  tabs read ONE source, Drive or the in-app RM store and MIRs (`PlantStockSource`, admin-switched,
  `app_stock_source.py` in the Drive rows' shape); never add the two, and pass `{stockTabs: true}` to the
  loaders only from the plant tabs.
  [plant tabs](docs/frontend.md#plant-stock-tabs---inventory-on-order-stock--orders-2026-09-29)

### MIR entry
- MIR entry has **no fuzzy matching and no tolerance**: quantity (accepted = received - rejected) and
  rate compare exactly against the PO line picked; a difference needs a reason of its kind and becomes
  an OPEN `MirMismatch`. The only allowance is the Rs 1 an invoice total rounds to.
  [MIR entry](docs/api-and-features.md#mir-entry-2026-09-28)
- `mir_service.evaluate()` is the only place a MIR is checked and priced; the page previews through it
  and never computes a figure. Received-so-far is summed from POSTED lines, never stored.
- The invoice number is required but NOT unique (owner, 2026-09-29): one invoice can be several
  deliveries, so earlier MIRs of it come back as a notice, never an error - don't add a uniqueness
  check. **A PO belongs to the plant on its billing address (owner, 2026-10-03)**: only that plant files it
  or enters a MIR against it. `PurchaseOrder.billing_plant` comes from `procurement_rules.billing_plant_code()`;
  a PO billed to a plant other than its sheet's takes no receipt until moved. The PO lookups cover the
  caller's plants only (another plant's PO is a 404), search by PO number only, and the duplicate-invoice
  notice stays within the receiving plant. Never reintroduce cross-plant receiving.
- A posted MIR's figures (qty received, rates, GST, discount, tax type, invoice total, lines) are never
  edited - cancel and re-enter. `edit_mir()` takes paperwork within 7 days (the SAP GRN any time) and
  `record_rejection()` raises a rejection within 30 days; both need a reason and log to `MirChange`.
- A material's category lives on `Material`, keyed on `material_identity.material_key()` - never the SAP
  item code (reused across grades). A MIR line has no category of its own.
- MIR reasons live only in migration `0068`'s `REASONS`; a new reason goes there plus a migration that
  re-runs its `seed()` (as `0076` does). Never delete a reason.
- Reference rows (plants, MIR reasons) come from migration `0068`; a `transaction=True` test flushes
  them, so the root `conftest.py` re-seeds them for every database test. Never make a test depend on
  migration data without it. [helpers](docs/testing-deployment.md#test-helpers)

### PO extraction (2026-10-03)
- An uploaded PO file is read on the worker (`po_extraction.run()`, one structured-output Claude call, no
  tools, no database access) into a DRAFT; nothing reaches `PurchaseOrder` until a person approves it, and
  approval is refused unless the billing address names the uploading plant and every compulsory field is
  there. Never write procurement rows from the model's output directly; never call the model in a request.
- An approved reading owns the PO (`source = app`, the CSV projection skips it); its lines go through
  `procurement_sync.write_lines()` by position, like the projection - never delete-and-rebuild.
- `PurchaseOrder.kind` is `domestic` or `import` (2026-10-03): every query names the kind it wants. Import
  orders are projected one line per ordered item (`import_line_groups()` - the CSV repeats a line per
  shipment; never sum its PO quantity) and are not receivable in MIR entry until their shipment's Bill of
  Entry is in the app. [imports](docs/data-sync.md#import-csv-into-the-procurement-tables-2026-10-03)
- `ANTHROPIC_API_KEY` blank = extraction off (uploads still work). Tests stub `_call_claude()` and
  `documents.read_bytes()`; nothing in the suite calls the API.
  [PO extraction](docs/api-and-features.md#po-extraction-2026-10-03)

### Files and backups (2026-09-30)
- Uploaded PO and invoice files live in private Cloudflare R2 buckets; `documents.py` is the only writer
  of `Document` rows. A PO file is keyed on (plant, PO number), never a `PurchaseOrder` FK: a re-upload is
  the next revision, a cancelled PO or wrong upload is withdrawn with a reason, and **nothing is ever
  deleted** (row or object). Import papers (BOE, Advance License, RoDTEP scrip) are filed under their
  PO with a required `reference` number, and revise per (kind, plant, PO, reference) through
  `documents._siblings()` - never per PO alone (one PO has several BOEs). Only the license kinds take
  .xlsx, and `_check_xlsx()` refuses macros and embedded objects - never loosen it.
  [files](docs/api-and-features.md#po-and-invoice-files-2026-09-30)
- A file opens through `/api/documents/<id>/open` (302 to a five-minute link) in a new tab - never
  fetch the link and navigate a blank tab: the app's COOP header leaves it on `about:blank`.
- **Check uploads in production with real documents or a `TEST-` PO number - never anything else.**
  An upload writes only `Document` rows and R2 objects (no PO, MIR, stock or match data), but
  nothing can be deleted, so: the normal flow is checked with a real PO's PDF under its real number
  and a real MIR's real invoice; the edge cases (revision, duplicate, withdraw) only under a dummy
  PO number such as `TEST-0001`, withdrawn afterwards with a reason. Never replace a real MIR's
  invoice with a test file - the replaced copy stays in its history. Take or confirm a fresh backup
  first. [checking uploads](docs/testing-deployment.md#checking-uploads-in-production-2026-10-01)
- The nightly `nightly-db-backup` schedule dumps the database to R2. `pg_dump` must be at least the
  server's major version: raise the Dockerfile's `postgresql-client-18` BEFORE upgrading Render's
  Postgres major version. [backups](docs/testing-deployment.md#backups-2026-09-30)

### RM stock entry
- The MIR is the ONLY way stock comes in (owner, 2026-09-30): a posted MIR line IS the receipt -
  `stock_service.receive_mir()` makes its lot at the receiving plant - and nothing is added by hand (no
  opening balances; `mode: "add"` is refused). A lot never stores a quantity - it is derived from the MIR
  line (received less rejected) while that is posted. Never add a stored balance.
  [RM stock entry](docs/api-and-features.md#rm-stock-entry-2026-09-29)
- Every voucher line names the MIR receipt it acts on (`lot_id`); the storekeeper picks the MIR and types only
  the quantity - everything else comes from the MIR. Never draw FIFO across receipts: the store runs two
  receipts of one material in parallel.
- Stock may never go below zero on ANY day: every draw, cancellation and MIR cancel/rejection goes through
  `stock_rules.min_running_balance()` over the lot's dated movements, with the lots row-locked in id order. A
  MIR whose stock was issued cannot be cancelled or have more rejected.
- `stock_service.evaluate()` is the only place a voucher is checked and valued; the page previews through it.
  Vouchers are never edited - cancel and re-enter. A stock difference (count or write-off, the page's "Open
  mismatches") by an editor waits for an admin; the one who entered it cannot approve it.
- Stock is held in the material's base unit (KG / L / NOS / M): exact units convert (`stock_rules.EXACT`: MT
  into KG), a pack unit (ROLL, SET, BAG) only by the material's `MaterialUnitFactor`, anything else stays as
  the MIR had it. Never guess a conversion. A unit change applies to MIRs posted afterwards only.
  `stock_unit()` is only migration 0082's history. An issue is for one plant, set by the first MIR picked.
  Values are before GST.
- Stock reasons live only in migration `0082`'s `REASONS` (re-seeded by the root `conftest.py`); a new one
  goes there plus a migration that re-runs its `seed()`. Never delete one.

### Consumption
- `received`/`issued` are period-to-date cumulative at all three plants. Consumption is
  `issued_1 - issued_0`; branch on whether `issued` reset, not on whether `opening` changed.
  [ledger](docs/consumption.md#consumption-ledger-2026-09-21---supersedes-the-days-left-engines-arithmetic)
- `material_key` is `normalize_material(description)`, never `natural_key` or HSN. Every period
  total is a SUM over `MaterialConsumptionDaily`; never re-derive from snapshots.
  [ledger](docs/consumption.md#consumption-ledger-2026-09-21---supersedes-the-days-left-engines-arithmetic)
- Rates divide by `ConsumptionCoverage` days, not the window length; don't wire
  `rate_from_daily()` or `consumption_rate()` into a reader.
  [read path](docs/consumption.md#the-read-path-migrated-2026-09-21-same-day)
- `stock_consumption.py` is dead code carrying the cumulative-`received` bug; delete it only
  together with its test. [stock_consumption.py](docs/consumption.md#appsservicesstock_consumptionpy)

### Activity log (2026-10-01)
- Every `/api/` write, download and refused sign-in is recorded by `ActivityLogMiddleware` (last in
  `MIDDLEWARE`) through `activity_log.record_request()`, which never raises; page visits come from
  `auth.js`'s `recordPageView()`. **Only `ACTIVITY_LOG_OWNER_EMAIL` may read it** (owner: "only for me
  and private"): `IsActivityLogOwner` 404s everyone else, other admins included - never widen it to
  `IsAdmin`.
  [activity log](docs/api-and-features.md#activity-log-2026-10-01)
- **Never store a secret in it**: bodies go through `activity_log.redact()` (any key naming a password,
  code, OTP, token or secret, at every depth); a multipart body is never read by the middleware, and a
  file is kept as name and size only. A new secret-bearing field name must match `_SECRET_KEY`.
- Successful `/api/auth/` calls are skipped because their views call `log_pt_action()` - keep those
  explicit calls, or the event disappears from the log. A new endpoint is logged automatically; give it
  a sentence in `ROUTE_LABELS`, and add a keystroke-rate endpoint (a preview) to the skips.
- Changes, downloads and page visits are kept 90 days (`RETENTION_DAYS`, owner's choice), pruned by
  the nightly `activity-log-prune` schedule; sign-in rows are kept one year (`SIGNIN_RETENTION_DAYS`) and
  user management for good. A new action type must join a retention tuple (a test checks).
- `auth.js`'s `renderNavTabs()` page keys must equal `activity_log.PAGES`, or visits are refused.
- "Last active" is never `last_login_at` (sessions last 30 days): it is the newest of `last_seen_at`, the
  log, saved work (`WORK_SOURCES`) and the sign-in. Never estimate use the app did not record.
- Google sign-in asks only for `openid email` with `access_type="online"` - don't add scopes or offline
  access without a feature that needs them.

### Auth, security, email
- **Access is layered (owner, 2026-10-02):** admin (everything; only `OWNER_EMAIL` makes, unmakes or
  edits an admin) / plants (`PTUser.plants`, **empty = none, never "all"**) / permissions
  (`PTUser.permissions`, `permissions.Perm`; none = locked). The rules live only in
  `apps/api/permissions.py`; `auth.js` mirrors them for display. Migration `0092` locked every
  non-admin on purpose - don't "fix" it by granting defaults.
  [roles](docs/auth-security-email.md#roles-and-plant-scoping)
- Never call `get_user_model()` in auth code (`PTUser` is not `auth.User`); never add simplejwt's
  `token_blacklist`. [non-negotiables](docs/auth-security-email.md#non-negotiables)
- `SecurityHeadersMiddleware` stays before WhiteNoise, `SelectiveGZipMiddleware` after it; anything
  returning a token or OTP in a body lives under `/api/auth/` (not gzipped - BREACH).
  [non-negotiables](docs/auth-security-email.md#non-negotiables)
- Accounts and sign-in are limited to `ALLOWED_EMAIL_DOMAINS` (ravasco.com, hindustanrubbers.com), matched
  on the whole domain - never `endswith()` on a bare domain. The old `ALLOWED_EMAIL_DOMAIN` is not read.
- Auth-flow throttles key on the account or pending session, never the IP. Django admin's `/admin/login/`
  is the exception: `AdminLoginThrottleMiddleware` locks per username AND per IP (5 failures / 15 min).
- `RequestSizeLimitMiddleware` (25 MB) stays before anything that reads a body, and above
  `documents.MAX_BYTES`. Uploads refuse PDFs with active content (`documents._check_pdf()`) - never
  loosen it to accept `/JavaScript`, `/Launch` or embedded files.
  [throttling](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters)
- The refresh token only travels in the httpOnly cookie. Password change calls
  `revoke_all_tokens()`; "log out everywhere" and a Forgot-password reset call `revoke_all_sessions()` -
  keep them separate. [sessions](docs/auth-security-email.md#sessions-and-tokens)
- **Passwords are the holder's alone (2026-10-02):** admins never set or reset one (`update_user()`
  refuses `password`); the holder changes it signed in (current password + emailed code) or resets it
  from the sign-in page (Forgot password). Every emailed code is bound to its `OTPCode.Purpose` -
  `generate_otp()`/`verify_otp()` take it with no default. The reset endpoints answer identically and
  at equal bcrypt cost for every address; keep both when changing them (`test_password_reset.py`).
  [password_views.py](docs/auth-security-email.md#appsapirouterspassword_viewspy)
- **Session limits (2026-10-03):** access tokens last 1 hour (`auth.js` keeps a page's session alive);
  a sign-in renews for at most `PT_SESSION_MAX_AGE` (the `auth_time` claim survives rotation); logout
  revokes the access jti too; a trusted device lapses after 90 idle days or a year. A Forgot-password
  code's wrong guesses count apart from the account's (`otp_service._RESET_FAILURE_PREFIX`), so a
  stranger cannot block someone's sign-in. Only the owner signs an admin out. The cron secret travels
  only in `X-Report-Secret` (or a body field); `?secret=` is refused.
  [session limits](docs/auth-security-email.md#sessions-and-tokens)
- If the bcrypt cost changes, regenerate `_DUMMY_HASH`.
  [throttling](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters)
- Never `fail_silently=True`; test send failures with `refusing_email_backends.py`, not by
  monkeypatching `send_mail`. Don't move OTP/alert email onto django-q2.
  [email](docs/auth-security-email.md#outgoing-email),
  [dispatch](docs/auth-security-email.md#dispatch-two-bounded-thread-pools-not-the-task-queue)

### Testing and deployment
- Run tests against Postgres only; SQLite gives false failures (Decimal `SUM`).
  [Postgres](docs/testing-deployment.md#run-against-postgres-never-sqlite)
- A test must fail when its fix is reverted - build that into the test; never mutate source to
  prove it. [reverted fix](docs/testing-deployment.md#a-test-must-fail-when-the-fix-is-reverted)
- The coverage floor (92) is a ratchet: raise it, never lower it.
  [coverage](docs/testing-deployment.md#coverage-is-a-ratchet-and-not-assertion)
- Ruff is narrow on purpose (no mass `--fix`); ESLint is a CI-only hard gate, never
  `continue-on-error`. [ruff](docs/testing-deployment.md#ruff-is-a-real-gate-deliberately-narrow),
  [eslint](docs/testing-deployment.md#eslint-runs-only-in-ci-and-is-a-real-gate)
- Production and CI run Python 3.12 while `.python-version` says 3.14 - no 3.13+ syntax.
  [python](docs/testing-deployment.md#ci-runs-python-312-via-uv_python)
- Never recreate the Render services; release tasks run once as the web `preDeployCommand` only;
  the container is non-root and uses plain `python`, not `uv run`. [render](docs/testing-deployment.md#render)
- Every ignore pattern goes in both `.gitignore` and `.dockerignore` (a test enforces it).
- CI actions are pinned by commit SHA (version in a comment). `secrets-scan` (gitleaks) checks only the
  commits being pushed; the image's `frontend/js` is comment-stripped at build (`strip_js_comments`), and
  CI fails if stripping changes any script's syntax tree.
  [CI jobs](docs/testing-deployment.md#ci-jobs)
  [.env gotchas](docs/testing-deployment.md#env-gotchas-that-have-already-cost-debugging-time)

---

## Traps already hit - don't re-introduce these

| Trap | Where it's explained |
| --- | --- |
| Drive v2 `title =` instead of v3 `name =` → opaque HTTP 400 | [data-sync](docs/data-sync.md#drive-api-v3-not-v2) |
| Case-sensitive filename prefix re-check silently dropping a new file | [data-sync](docs/data-sync.md#drive-api-v3-not-v2) |
| `ROUND_HALF_EVEN` breaking sync idempotency on `X.XX5` values | [data-sync](docs/data-sync.md#change-detection-must-compare-quantized-decimals-rounded-the-way-postgres-rounds) |
| 2-decimal GST rate truncating a `0.025` fraction to `0.02` | [architecture](docs/architecture.md#decimal-precision-conventions) |
| Unclamped `*_diff_pct` overflowing `max_digits=6` | [architecture](docs/architecture.md#decimal-precision-conventions) |
| `source_row_ref` as lot identity splicing two materials' histories | [architecture](docs/architecture.md#stable-lot-identity) |
| Header check stripping whitespace, row lookup not → `KeyError` on a resaved CSV (both PO parsers now re-key rows) | [architecture](docs/architecture.md#per-plant-models-not-a-shared-schema---deliberate-dont-fix-it) |
| Comparing pre-tax PO value to post-tax MIR value → bogus ~18% gap | [matching](docs/matching-engine.md#po--mir) |
| Vendors with no PO looking like matcher failures on every run | [matching](docs/matching-engine.md#some-vendors-never-have-a-po---that-is-registered-not-inferred) |
| `<str:po_number>` 404ing every per-PO edit, pin and flag dismissal on a PO number with "/" (the server decodes `%2F`); now `<path:>`, detail route last | [api](docs/api-and-features.md#appsapiurlspy) |
| Import PO lines deleted and rebuilt on every CSV change, silently dropping the match dismissals keyed on their pks | [data-sync](docs/data-sync.md#appsservicesimport_syncpy) |
| BOE settlement ignoring manual receipt edits: a removed BOE receipt came back, an added one vanished unreported | [matching](docs/matching-engine.md#file-reference) |
| An over-long domestic PO CSV row (`None` key) crashing the whole plant's PO sync on `.strip()` | [data-sync](docs/data-sync.md#appsservicesparserspo_csvpy) |
| `?format=csv` silently 404ing - DRF reserves `format` | [matching](docs/matching-engine.md#no-purchase-order-behind-it-is-three-questions-not-one-2026-09-21) |
| Exact vendor equality → zero MIR↔Stock matches (city suffix) | [matching](docs/matching-engine.md#vendor-name-is-a-hard-gate-on-mirstock-one-of-three-votes-on-pomir-everywhere-now) |
| Letter-for-letter material equality as MIR↔Stock's only rule → 1.6-27% coverage | [matching](docs/matching-engine.md#three-tiers-not-one-equality-test-2026-09-21) |
| Date+rate identification without a grade-code gate → same-day same-price SKUs cross-matched | [matching](docs/matching-engine.md#date--rate-is-this-pairings-po-number---behind-two-guards) |
| An early `return []` in `match_mir_entry_stock()` skipping the standalone stale-row cleanup | [matching](docs/matching-engine.md#file-reference) |
| A not-stocked pattern with a bare `belt` word killing a real compound match | [matching](docs/matching-engine.md#not_stocked_materials---the-material-keyed-half) |
| A unit-clash pair shown green though nothing was compared; MT vs KG → ~1000x phantom mismatch | [matching](docs/matching-engine.md#units-are-normalised-before-comparing---on-both-pairings) |
| Import USD rate compared raw against INR MIR → ~94x gap, 0 matches | [matching](docs/matching-engine.md#import-po--mir-convert-currency-first) |
| Full-table SELECTs inside per-row loops → quadratic matching | [matching](docs/matching-engine.md#performance-the-engine-was-quadratic) |
| `_assign_pairs()` never terminating on a positive-gain cycle (twice) | [matching](docs/matching-engine.md#_assign_pairs-needs-both-its-termination-guards) |
| A multi-line fabric order's PO-cited receipt of one width handed to a sibling of another (`_grade_codes()` can't read "67cm") | [matching](docs/matching-engine.md#a-fabric-receipt-of-another-width-or-grade-belongs-to-a-sibling-line-2026-09-25) |
| An import edit keyed on a repeated item_id landing on the first shipment line | [api](docs/api-and-features.md#inline-edit-everywhere) |
| `legacy_po_matches()` running 844k times where it could never match | [matching](docs/matching-engine.md#two-hot-paths-in-matching-are-cached-or-short-circuited-for-a-reason) |
| The contradiction gate re-scanning every known PO per line item (26.8M calls), pushing a Vapi pin's synchronous re-match past gunicorn's 30 s timeout - an HTML 502 after the pin had committed | [matching](docs/matching-engine.md#two-hot-paths-in-matching-are-cached-or-short-circuited-for-a-reason) |
| Renaming a PO upstream forking it into two permanent rows; orphans reported only to stdout | [data-sync](docs/data-sync.md#purchase-orders-are-retired-not-deleted---and-until-2026-09-18-they-were-neither) |
| `fail_silently=True` making every sender's `except` unreachable | [email](docs/auth-security-email.md#outgoing-email) |
| Testing a send failure by monkeypatching `send_mail`, which passes with the bug present | [email](docs/auth-security-email.md#outgoing-email) |
| Duplicate scheduled report emails from a double-fired cron | [email](docs/auth-security-email.md#outgoing-email) |
| Unbounded thread-per-email dispatch | [email](docs/auth-security-email.md#dispatch-two-bounded-thread-pools-not-the-task-queue) |
| The frontend never calling `/token/refresh`, signing everyone out at hour 12 | [auth](docs/auth-security-email.md#sessions-and-tokens) |
| Two concurrent refreshes presenting one rotated token, the loser signed out | [auth](docs/auth-security-email.md#sessions-and-tokens) |
| Password change not evicting any session; refresh token in the login response body | [auth](docs/auth-security-email.md#sessions-and-tokens) |
| Per-code OTP attempts reset by a new sign-in (unbounded guesses over days); DRF throttles keyed on a spoofable X-Forwarded-For; a refresh token rotatable twice at once | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| Per-IP login throttle locking out a whole office | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| A malformed dummy bcrypt hash making unknown-email logins ~240 ms faster | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| Google OAuth ignoring account lockout; non-atomic failed-login / OTP counters undercounting | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| A blank `JWT_SIGNING_KEY=` signing every JWT with an empty key; a CLI password reset leaving sessions alive | [auth](docs/auth-security-email.md#configsettingspy-auth-and-security-parts) |
| `KeyError` in `exceptions.py` leaking dict key names as a 400 | [auth](docs/auth-security-email.md#alerts-audit-log-and-logs) |
| CSV formula injection in the export | [api](docs/api-and-features.md#data-export) |
| A licence number with and without its leading zero reading as two authorisations; unguarded multi-value cell splits; Balance = Sanctioned read as "nothing spent" | [api](docs/api-and-features.md#licences-the-import-side-was-in-the-csv-all-along-2026-09-22) |
| A `<input type="number">` reporting `''` for garbage, saving a NULL over a real figure | [api](docs/api-and-features.md#validation-runs-before-the-write-not-after) |
| A newest-first pin order computed then thrown away; one PO number existing as Domestic and Import so a pin hits the wrong table (the domestic endpoint lacked `po_kind` until 2026-09-24) | [api](docs/api-and-features.md#editing-which-mir-a-po-line-matched-2026-09-21) |
| Import customs clearance counted as delivery, so a cleared PO never received at the plant could never be Overdue; the Import status cards' tooltips describing rules the code did not apply | [matching](docs/matching-engine.md#appsservicesimport_flagspy) |
| A dismissed PO-level flag still counting on the KPI cards (only the modal honoured it); the status doughnut's Overdue slice filtering to the card's larger overlay set | [api](docs/api-and-features.md#dismiss--override-a-flagged-match-or-flag) |
| A `critical` category firing on every not-yet-due order, red on nearly every row | [api](docs/api-and-features.md#row-flags-are-four-buckets-one-icon-each-2026-09-22) |
| Raw Material's in-transit KPIs summing per material (a fuzzy-linked PO line counted once per material it touched), counting the full ordered qty of a part-delivered line, and adding KG to metres | [frontend](docs/frontend.md#frontendjsmaterialsjs) |
| An aggregate (`annotate(Sum(...))`) query silently dropping `Meta.ordering`, so the MIR register listed oldest first | [api](docs/api-and-features.md#appsapiroutersmir_viewspy) |
| A `transaction=True` test flushing migration-seeded reference rows, breaking every later test by order | [testing](docs/testing-deployment.md#test-helpers) |
| A page script re-declaring `let CURRENT_USER` (auth.js's) - a SyntaxError that silently killed the whole MIR page; `test_frontend_global_names.py` now fails on any repeated top-level `let`/`const`/`class` on one page | [frontend](docs/frontend.md#serving-load-order-and-the-one-global-scope) |
| `vendorContains()` applying its 4-character floor to identical names, so every SRF and GRP order linked to no material (Rs 1.72 cr missing from Value in Transit) - the backend had fixed it, the browser port had not | [frontend](docs/frontend.md#frontendjssharedjs) |
| Stale modal data from a fast row-switch | [frontend](docs/frontend.md#modals-one-shared-shell-and-the-stale-response-guard) |
| `[hidden]` losing a specificity tie to a `display` rule (now a global `[hidden]{display:none !important}` in `brand.css`) | [frontend](docs/frontend.md#other-traps) |
| Duplicate CSS custom properties across two stylesheets | [frontend](docs/frontend.md#css-custom-property-collisions) |
| A top-level `"//"` key in `.eslintrc.json` crashing ESLint, hidden by `continue-on-error` | [testing](docs/testing-deployment.md#eslint-runs-only-in-ci-and-is-a-real-gate) |
| `setup-uv@v3` ignoring `python-version`, so CI tested 3.14 while production ran 3.12 | [testing](docs/testing-deployment.md#ci-runs-python-312-via-uv_python) |
| Running the suite on SQLite, where a Decimal `SUM()` loses its scale | [testing](docs/testing-deployment.md#run-against-postgres-never-sqlite) |

Three traps that don't belong to one area:

**Use `timezone.localdate()`, never `date.today()`.** Render runs containers in UTC while `TIME_ZONE`
is `Asia/Kolkata`, so between 00:00 and 05:29 IST the server's date was still yesterday - overdue POs
read "On Order" and consumption windows started a day late. Invisible in local dev, which runs on IST.
Ruff's `DTZ` rules and `test_local_date_timezone.py` prevent a new call site.

**The "can't remove the last active admin" check locks every active admin row.** A plain read then
`.save()` let two requests (one on admin A, one on B) both pass and leave zero admins; a
`select_for_update()` scoped to "admins except the target" doesn't fix it, since the two transactions
lock different rows. `_assert_not_last_active_admin()` locks every active admin inside
`transaction.atomic()`, and its test uses two real threads with `django_db(transaction=True)`.

**`Decimal()` raises `decimal.InvalidOperation`, not `ValueError`, on non-numeric input.** An
`except (TypeError, ValueError)` misses it and returns a 500. Every `_coerce_value()` re-raises it as
`ValueError`; a new coercion function must too.

---

## Known gaps

Confirm a gap is still true before treating it as blocking - check the file it points at.

- **No automated tests for the Drive API calls or the `sync_*` commands' file-fetching.** Everything
  else, including `run_full_match()`, has real coverage.
- **PO extraction has never read a real PO** (built 2026-10-03 with no `ANTHROPIC_API_KEY` anywhere):
  the Claude call is stubbed in every test. Before relying on it, set the key on Render, upload a real
  PO, and compare the reading with the PO sheet's row (the review shows the differences).
- **No measured match accuracy at all.** The Review Matches harness never reached a usable sample and
  was removed with its data on 2026-10-01 at the owner's request; a future measurement starts from
  scratch.
- **Older git history has 12 gitleaks `generic-api-key` hits** (2026-10-03 scan, values not reviewed by
  Claude): `render.yaml` 2, `.env.example` 2, `core/plant_config.py` 2, `lib/drive.js` 1,
  `config/settings.py` 1, and one each in four `apps/api/tests/` files. The owner should check each is a
  placeholder or test value; rotate anything real. CI's scan skips history for this reason.
- **`dev_smoke_test.sqlite3.bak_pre_vendorgate` is still in git history** with 4 dev/test `pt_users`
  bcrypt hashes. History was deliberately not rewritten - rotate any reused password.
- **`prune_revoked_tokens` has a trigger endpoint but no fixed cadence.**
- **PO and invoice uploads are not yet checked in production** - R2 is set up and the first
  production backup uploaded (2026-10-01, 1.4 MB); the upload flow awaits the check described in
  [checking uploads](docs/testing-deployment.md#checking-uploads-in-production-2026-10-01).
- **Backups are restore-checked by hand, not on a schedule.** The first production dump
  (`purchase_tracker_6su8-20261001-111321.dump`) passed `verify_backup` on the worker's Shell on
  2026-10-01: 98 tables, every key-table count matched live. Run it monthly and after any Postgres
  major upgrade.
- **`cache_page` infrastructure exists and nothing uses it** - every endpoint is business data behind auth.
- **A day where qcluster was down has no stock snapshot**, deliberately not backfilled; `sync-status`
  exposes `snapshotGapDays` as a badge.
- **RM stock entry has no inter-plant transfer yet** (challan, job work): stock issued at one plant cannot
  be received at another, and there is no way to bring the Drive RM sheets' current stock in - stock enters
  only through MIRs.
  [RM stock entry](docs/api-and-features.md#rm-stock-entry-2026-09-29)

- **Several code comments and docstrings are stale** - found by the 2026-09-24 documentation audit
  and listed in each doc's File reference. The bugs that audit found are all fixed.

**Deliberately not done, each re-decided rather than forgotten:**

- **API pagination** - would move every client-side filter server-side, a rewrite of the most-used surface.
- **A JS bundler / ES modules** - the no-build-step decision rules it out.
- **Consolidating the per-plant model classes** - see [per-plant models](docs/architecture.md#per-plant-models-not-a-shared-schema---deliberate-dont-fix-it).
- **A blanket `ruff --fix`** - see [ruff](docs/testing-deployment.md#ruff-is-a-real-gate-deliberately-narrow).
- **Moving email to django-q2** - see [dispatch](docs/auth-security-email.md#dispatch-two-bounded-thread-pools-not-the-task-queue).
- **TOTP second factor** - built, then fully removed the same day at the owner's request ("Don't need
  TOTP, happy with device aware"). No code or migration history remains; it would need rebuilding
  from scratch.
- **Licences beyond the Advance Licence ledger** - resolving each import PO's own Drive folder of
  Advance Authorisation letters was deferred; ask before building it.
- **Consumption report scheduling on the qcluster** - the owner will wire it later; don't build it unprompted.
