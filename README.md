# Purchase Tracker Dashboard

Standalone Django service reconciling Purchase Orders, MIR (Material Inward
Register), and Raw Material Stock for Ravasco's plants. Built to replace the
Claude Artifact prototype of the same name, which re-fetches and re-parses
every Drive file live on every page load with no persistence - this app
syncs into Postgres instead, so it works for every viewer without depending
on their own Drive session. Syncing is currently a manual management-command
run, not yet on an automatic schedule - see "Status" below.

**HRS, RTP-Achhad, and RTP-Vapi are all built.** Each plant's MIR/Stock files
were inspected directly before writing any code for it, and each turned out
to have a genuinely different column layout - not just relabeled columns:
Achhad's MIR has no SAP GRN number and no vendor column in Stock at all;
Vapi's MIR has no Net/discount columns and a 100%-blank PO-number field
(worse than HRS's ~30% blank), and its Stock file is a real shared
multi-plant ledger with an extra `PLANT` column plus a genuine vendor
column HRS's own Stock file also has (see
`apps/services/parsers/achhad_mir.py` / `achhad_stock.py` / `vapi_mir.py` /
`vapi_stock.py` docstrings and `apps/services/matching_achhad.py` /
`matching_vapi.py` for what each difference means for match confidence).
Each plant got its own parsers/models/matcher/views rather than being
force-fit into a shared shape.

## Architecture

- **Django + Postgres + WhiteNoise**, same conventions as the TDS Automation
  app: static frontend served directly from `frontend/` with no build step,
  `apps/core` for models, `apps/api` for HTTP views, `apps/services` for
  Drive access/parsing/matching and the auth-adjacent services.
- **Device-aware 2FA + JWT-in-httpOnly-cookie auth**, ported from the TDS
  Automation App's own auth architecture - see CLAUDE.md's "Auth & security
  architecture" and ARCHITECTURE.md's auth flow diagram.
- **Google Drive/Sheets access via a service account**, not a user OAuth
  session, since sync jobs need to run without a human/Claude session in
  the loop. Share the relevant Drive folders/files with the service
  account's email. Scheduling (Render Cron Job or similar) isn't set up
  yet - commands currently only run when invoked manually.
- **Three independent sync sources per plant**, one management command each:
  - HRS: `sync_po_csv`, `sync_mir`, `sync_stock`, then `match_hrs`
  - RTP-Achhad: `sync_achhad_po_csv`, `sync_achhad_mir`, `sync_achhad_stock`,
    then `match_achhad`
  - RTP-Vapi: `sync_vapi_po_csv`, `sync_vapi_mir`, `sync_vapi_stock`, then
    `match_vapi`
- **Reconciliation runs after sync**, not live on page load - `POMirMatch`
  and `MirStockMatch` are computed and stored, so a viewer opening the
  dashboard reads pre-computed results, not something recalculated per view.

## Why the data model looks the way it does

Two audits (see project history) found real problems the schema is built
around, not against:

- **HRS's own PO Number field in MIR is unreliable** (~30% blank, ~25% in a
  non-standard format that won't string-match the CSV's PO Number). Vendor
  name is used as a hard gate in matching, never a scored factor - two
  different vendors are never the same PO. PO Number match is a free "tier
  1" shortcut when it happens to be present and valid, not the primary key.
- **HRS's Stock file is one row per (material, vendor lot), not one row per
  material.** `StockLot` reflects that directly, and `MirStockMatch` joins
  on (material, vendor) instead of material name alone - the earlier
  Artifact prototype aggregated everything by material name only, which
  blends different vendors' different rates into one number and can hide or
  manufacture discrepancies that aren't real.
- **`StockSnapshot` is a real daily row per stock lot**, not a dated copy of
  the whole file (which is what Drive's `RM_Stock_Daily_Snapshots` /
  `RM Stock Snapshots` folders currently do, inconsistently - only one dated
  snapshot exists in each as of this writing). This makes "what was the rate
  on this material two weeks ago" an actual query instead of a manual diff
  across xlsx files.
- **PO Item Id is not always a trustworthy join key** - audit found one code
  (`11287940`) reused across three chemically unrelated materials from two
  different vendors on the same PO source data. The PO<->MIR matcher never
  relies on Item Id; it scores on material description + qty + rate +
  total/final value, gated by vendor name.

## PO<->MIR matching

Built and verified against real Drive data - see `apps/services/matching.py` /
`matching_achhad.py` / `matching_vapi.py`.

- Vendor name: hard gate (normalized, legal suffixes stripped, matched by
  containment rather than exact equality - see CLAUDE.md for why)
- Material description token overlap: 30%
- Qty closeness: 20%
- Rate closeness: 20%
- Pre-tax value closeness: 30%

Extended from the Artifact prototype's 55/45 description+amount split - qty
and rate are now real signals, not folded into one blended total, since a
coincidental amount match is much less likely to fool the matcher when qty
and rate both have to line up too. MIR<->Stock matching runs alongside it,
gated on (material, vendor) for HRS and Vapi (both have a real vendor column
on their Stock sheet) and material alone for Achhad (its Stock sheet has no
vendor column) - see CLAUDE.md for the full breakdown of what's genuinely
different between each plant's matcher and why.

## Status

**HRS, RTP-Achhad, and RTP-Vapi are all fully wired and verified end-to-end
against real Drive data**, not just local test files: sync commands,
matching, API, and the frontend's plant tabs (HRS, Silvassa / RTP-Achhad /
RTP-Vapi) sharing one rendering path for Purchase Orders and Raw Material
Analysis. The Google service account has real Viewer access to all three
plants' Drive folders and every plant's `sync_*`/`match_*` commands have
been run successfully against the live files (not just `--file` against
downloaded copies).

**Updated 2026-09-04 - the paragraph below is stale in several ways; see the corrections that
follow it.** Import POs are now built for all three plants (not just domestic), including a real
reconciliation layer, and a custom in-app user-management UI exists. What's still genuinely not
built: Licenses (Advance Authorisation tracking) and scheduling (every sync/match command is still
manual, triggered by hand or via the admin panel's sync-trigger buttons).

~~Not yet built: import POs for any plant (only domestic is parsed so far -
each plant has its own separate Imports CSV on Drive with a different
column set), scheduling (every sync/match command is still manual), and a
custom user-management UI (accounts are created via `manage.py
create_pt_user` or Django Admin for now - see "Local setup" below).~~
**Corrections (2026-09-04):**
- **Import POs are built for HRS, RTP-Achhad, and RTP-Vapi alike.** All three plants' Imports
  CSVs turned out to share one identical column layout (BOE number, bill of lading, exchange
  rate, dual PO/BOE quantities, license numbers, etc.) - unlike MIR/Stock, which really do differ
  per plant - so all three go through one shared parser
  (`apps/services/parsers/import_po_csv.py`). Import POs also get a real reconciliation layer
  computed at read time (`apps/services/import_flags.py`): shipment-stage rollup, PO-vs-BOE qty
  discrepancy detection, delivery-date status, partial-delivery detection, and 7 data-quality
  flags. See CLAUDE.md's "Known gaps" section for exactly which 3 KPI cards (the ones that would
  need a real MIR-equivalent data source) are still disabled.
- **There's now a real in-app user-management UI**, not just Django Admin/`manage.py
  create_pt_user`: `frontend/admin.html`'s Users panel (create/edit/activate/deactivate/role/
  per-plant scoping/password reset), backed by `apps/api/routers/users_views.py`. `manage.py
  create_pt_user` remains the only way to create the very first account, before any admin exists
  to use the panel.
- Scheduling and Licenses (Advance Authorisation tracking) are still genuinely not built - both
  remain correct as written above.

Every
API endpoint now requires authentication (device-aware 2FA login, see
CLAUDE.md). See CLAUDE.md for the full list of known gaps and the bugs
already found and fixed along the way
- several real ones (a Drive API v2/v3 field-name bug, a Decimal-precision
bug in GST rate fields, a pre-tax/post-tax mismatch in the value comparison,
a Postgres numeric-rounding mismatch that broke change-detection
idempotency for one plant's data, `.env` parsing gotchas, an uncaught
`decimal.InvalidOperation` in the inline field-correction endpoints, a
stale-response race in the PO/material detail modals, and a login/OTP
throttle that was keyed per-IP instead of per-account, so a handful of
colleagues signing in from the same office network within a minute could
lock everyone else out of login with "Request was throttled" even with
correct credentials - see CLAUDE.md's "Auth & security architecture") came
out of getting this far.

## Frontend pages

Four protected pages share one top nav (see CLAUDE.md's "Frontend pages and the shared top nav"
for the full breakdown): **`home.html`** (landing page, live cross-plant KPI row), **`index.html`**
(`/`, the real PO<->MIR<->Stock reconciliation dashboard), **`search-po.html`** (look up a PO by
number across all 3 plants at once), and **`admin.html`** (admin-only: per-plant sync status plus
the in-app Users panel above). Every mutating write (inline field corrections, dismiss/override,
user management) is logged: `PTAuditLog` (`pt_audit_log`) covers login/logout, and each
correction/dismissal writes its own audit row (`DomesticPOCorrection`/`ImportPOCorrection`,
`dismissed_by`/`dismissed_at`/`dismissed_reason` columns on the match models) - see CLAUDE.md for
specifics. `PTUser.plants` lets an admin be scoped to specific plants for write access (empty list
= all plants); every role can still read every plant's dashboard.

## Local setup

```bash
uv sync
cp .env.example .env   # fill in DB, Google service account, SMTP, and OAuth details
                        # (see CLAUDE.md's ".env gotchas" if this repo's folder is
                        # synced by OneDrive/similar - .env is gitignored but not
                        # automatically excluded from that kind of cloud sync)
uv run python manage.py migrate
uv run python manage.py createcachetable   # one-off: creates the DatabaseCache table

# Bootstrap the first account - nobody can log in without at least one.
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

uv run python manage.py runserver
# -> open http://127.0.0.1:8000/login.html
```

### Docker alternative

Gives you a real Postgres + this app running together without installing Python/uv/Postgres
directly - useful if you'd rather not set up a local toolchain, or want dev to match Render's
Python 3.12/gunicorn/qcluster setup exactly. Does not replace `render.yaml`'s own deploy pipeline;
this is local-dev only.

```bash
cp .env.example .env   # same first step as above - fill in the same values
docker compose up -d
# docker-compose.yml points the app/worker containers at its own `db` service automatically -
# you don't need to change PGHOST/DATABASE_URL in .env for this to work.

# Bootstrap the first account, same as the non-Docker flow:
docker compose exec app uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
# -> open http://localhost:8000/login.html
```

To confirm it's actually working: `docker compose logs app` should show no tracebacks, and the
login page/dashboard should load with no 404s on `/static/...` assets (a broken `collectstatic`
step is the most likely thing to go wrong, and shows up exactly there). See CLAUDE.md's "Docker
(local dev)" for the underlying `Dockerfile`/`docker-entrypoint.sh` details.
