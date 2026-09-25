# CLAUDE.md

Guidance for Claude Code when working in this repository. This file holds the rules, the commands,
and the traps. The detail - how each area works, why, and a reference for every source file - lives
in [`docs/`](docs/). Read the doc for the area you are about to touch before changing it.

**No em dashes anywhere** (project owner, 2026-09-22). Not in code comments, docstrings, markdown,
UI copy, or emails - use a spaced hyphen " - ". `test_no_em_dashes.py` enforces it.

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
| API and features | [api-and-features.md](docs/api-and-features.md) | endpoint table, routers, feature rules, inline edits, pins, dismissals, licences, exports, match accuracy |
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
# Read-only: report_retired_pos, report_match_accuracy, backfill_achhad_po_numbers
# Maintenance: prune_revoked_tokens
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
- Keep `parsers/common.py`, `validation.py`, `stock_identity.py`, `arithmetic_checks.py`, and
  `consumption_engine.py` free of Django imports - migrations import them.
  [layering](docs/architecture.md#layering)
- Per-plant model classes are deliberate; never merge them into one table with a `plant` column.
  [per-plant models](docs/architecture.md#per-plant-models-not-a-shared-schema---deliberate-dont-fix-it)
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
  [exceptions.py](docs/architecture.md#appsapiexceptionspy)

### Data sync
- Drive search uses v3 `name =`, never `title =`; the filename prefix re-check stays
  case-insensitive. [Drive API v3](docs/data-sync.md#drive-api-v3-not-v2)
- Change detection uses `sync_utils.unchanged()` with `ROUND_HALF_UP`, never `HALF_EVEN`. Two
  back-to-back syncs must report 0 changed.
  [change detection](docs/data-sync.md#change-detection-must-compare-quantized-decimals-rounded-the-way-postgres-rounds)
- POs are deactivated, never deleted; a hash-skip must check `existing.is_active`.
  [retired POs](docs/data-sync.md#purchase-orders-are-retired-not-deleted---and-until-2026-09-18-they-were-neither)
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
  `group_entries`, and readers must read it.
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
  `FLAG_PCT`. [flag thresholds](docs/matching-engine.md#flag-thresholds)

### API and features
- Every new endpoint needs both a role gate and a plant-scoping call;
  `test_endpoint_permission_guard.py` enforces it.
  [conventions](docs/api-and-features.md#endpoint-conventions-worth-knowing-before-adding-one)
- Never name a query param `format` (DRF reserves it). Read body flags with `_request_bool()`,
  never `bool(...)` (`bool("false")` is `True`). Use `timezone.localdate()`, never `date.today()`. [conventions](docs/api-and-features.md#endpoint-conventions-worth-knowing-before-adding-one)
- Every CSV export uses `SafeCsvWriter`. [data export](docs/api-and-features.md#data-export)
- Treat every match as a suggestion; never wire an automatic action off one.
  [match accuracy](docs/api-and-features.md#match-accuracy-manual-validation-is-required-not-optional)
- A manual pin names a MIR number, not a row, and `po_kind` belongs in every pin query. "Keep both"
  (`shared`) claims nothing; an import pin naming its own BOE's receipt defers to BOE settlement.
  [pins](docs/api-and-features.md#editing-which-mir-a-po-line-matched-2026-09-21)
- Corrections mutate the real row plus an audit row, re-match synchronously, and are overwritten by
  the next sync. Matcher `defaults` never carry `dismissed_*`; only `_save_po_mir_match()` clears it,
  when a line is re-pointed to a different MIR row.
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
- Don't merge `brand.css` and `style.css`, and never define the same custom property in both.
  [CSS collisions](docs/frontend.md#css-custom-property-collisions)
- Nothing per-pair goes in the Raw Material linkage loop (~780k calls per render).
  [materials.js](docs/frontend.md#frontendjsmaterialsjs)

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

### Auth, security, email
- Never call `get_user_model()` in auth code (`PTUser` is not `auth.User`); never add simplejwt's
  `token_blacklist`. [non-negotiables](docs/auth-security-email.md#non-negotiables)
- `SecurityHeadersMiddleware` stays before WhiteNoise, `SelectiveGZipMiddleware` after it; anything
  returning a token or OTP in a body lives under `/api/auth/` (not gzipped - BREACH).
  [non-negotiables](docs/auth-security-email.md#non-negotiables)
- Auth-flow throttles key on the account or pending session, never the IP.
  [throttling](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters)
- The refresh token only travels in the httpOnly cookie. Password change calls
  `revoke_all_tokens()`; "log out everywhere" calls `revoke_all_sessions()` - keep them separate.
  [sessions](docs/auth-security-email.md#sessions-and-tokens)
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
| Per-IP login throttle locking out a whole office | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| A malformed dummy bcrypt hash making unknown-email logins ~240 ms faster | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| Google OAuth ignoring account lockout; non-atomic failed-login / OTP counters undercounting | [auth](docs/auth-security-email.md#throttling-lockout-and-brute-force-counters) |
| A blank `JWT_SIGNING_KEY=` signing every JWT with an empty key; a CLI password reset leaving sessions alive | [auth](docs/auth-security-email.md#configsettingspy-auth-and-security-parts) |
| `KeyError` in `exceptions.py` leaking dict key names as a 400 | [auth](docs/auth-security-email.md#alerts-audit-log-and-logs) |
| The review queue serving every plant's cards to a plant-scoped account | [api](docs/api-and-features.md#match-accuracy-manual-validation-is-required-not-optional) |
| CSV formula injection in the export | [api](docs/api-and-features.md#data-export) |
| A licence number with and without its leading zero reading as two authorisations; unguarded multi-value cell splits; Balance = Sanctioned read as "nothing spent" | [api](docs/api-and-features.md#licences-the-import-side-was-in-the-csv-all-along-2026-09-22) |
| A `<input type="number">` reporting `''` for garbage, saving a NULL over a real figure | [api](docs/api-and-features.md#validation-runs-before-the-write-not-after) |
| A newest-first pin order computed then thrown away; one PO number existing as Domestic and Import so a pin hits the wrong table (the domestic endpoint lacked `po_kind` until 2026-09-24) | [api](docs/api-and-features.md#editing-which-mir-a-po-line-matched-2026-09-21) |
| Import customs clearance counted as delivery, so a cleared PO never received at the plant could never be Overdue; the Import status cards' tooltips describing rules the code did not apply | [matching](docs/matching-engine.md#appsservicesimport_flagspy) |
| A dismissed PO-level flag still counting on the KPI cards (only the modal honoured it); the status doughnut's Overdue slice filtering to the card's larger overlay set | [api](docs/api-and-features.md#dismiss--override-a-flagged-match-or-flag) |
| A `critical` category firing on every not-yet-due order, red on nearly every row | [api](docs/api-and-features.md#row-flags-are-four-buckets-one-icon-each-2026-09-22) |
| Raw Material's in-transit KPIs summing per material (a fuzzy-linked PO line counted once per material it touched), counting the full ordered qty of a part-delivered line, and adding KG to metres | [frontend](docs/frontend.md#frontendjsmaterialsjs) |
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
- **No measured match accuracy over a sample big enough to act on.** The harness is complete
  (`review.html` + `match_accuracy.py`); what is missing is reviews, not code.
- **`dev_smoke_test.sqlite3.bak_pre_vendorgate` is still in git history** with 4 dev/test `pt_users`
  bcrypt hashes. History was deliberately not rewritten - rotate any reused password.
- **`prune_revoked_tokens` has a trigger endpoint but no fixed cadence.**
- **`cache_page` infrastructure exists and nothing uses it** - every endpoint is business data behind auth.
- **A day where qcluster was down has no stock snapshot**, deliberately not backfilled; `sync-status`
  exposes `snapshotGapDays` as a badge.

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
