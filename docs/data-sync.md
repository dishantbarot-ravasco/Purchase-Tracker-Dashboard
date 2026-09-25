# Data sources, Drive sync, parsers and scheduling

This app never reads Google Drive at request time. Management commands pull each plant's source
files (PO master CSV, Import PO CSV, MIR xlsx, RM Stock xlsx) plus two company-wide ledgers
(RoDTEP, Advance Licence) from Drive, parse them with strict header checks, and upsert them into
Postgres with change detection. A django-q2 schedule runs the whole pipeline hourly from 9:00 to
20:00 IST; the dashboard's "Refresh Data" button queues the same pipeline on demand. Matching and the
consumption ledger run as the last steps of each plant's pipeline - see
[matching-engine.md](matching-engine.md) and [consumption.md](consumption.md) for what they do.

---

## Commands

All commands run from the repo root (there is no `django_backend/` subdirectory here, unlike TDS).
Inside the Docker image, drop `uv run` and call `python manage.py ...` directly (see
[testing-deployment.md](testing-deployment.md)).

```bash
uv sync                                    # install/update dependencies
uv run python manage.py runserver          # dev server
uv run python manage.py migrate
uv run python manage.py makemigrations core
uv run python manage.py createcachetable   # one-off: DatabaseCache's table (pt_cache_table)
uv run python manage.py ensure_schedules   # idempotent: creates/corrects the django-q2 Schedule row
uv run python manage.py qcluster           # the worker that actually fires the schedule and queued syncs
uv run pytest                              # test suite (real Postgres, no mocking)
uv run ruff check .                        # lint gate (red/green, also in CI)
uv run python manage.py check --deploy --fail-level WARNING   # production security check, also in CI

# Create/update a PTUser (bcrypt-hashes the password). The only way to create the FIRST admin
# account; re-running against an existing email updates password/role/full_name/designation.
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

# HRS pipeline (this is the order sync_trigger.py runs it in)
uv run python manage.py sync_po_csv        # PO master CSV -> HRSDomesticPurchaseOrder/HRSDomesticPOLineItem
uv run python manage.py sync_mir           # MIR xlsx -> HRSMIREntry
uv run python manage.py sync_stock         # Stock xlsx -> HRSRMLot (+ today's HRSRMSnapshot)
uv run python manage.py match_hrs          # PO<->MIR and MIR<->Stock matching
uv run python manage.py compute_hrs_consumption   # consumption ledger (reads the DB, no Drive fetch)
#   --all rebuilds the whole history; --since YYYY-MM-DD from a date; default is a 45-day lookback

# RTP-Achhad and RTP-Vapi: identical shape, separate models/commands
uv run python manage.py sync_achhad_po_csv / sync_achhad_mir / sync_achhad_stock / match_achhad
uv run python manage.py sync_vapi_po_csv   / sync_vapi_mir   / sync_vapi_stock   / match_vapi
uv run python manage.py compute_achhad_consumption / compute_vapi_consumption

# Import POs (one command per plant, one shared parser)
uv run python manage.py sync_hrs_imports_po_csv / sync_achhad_imports_po_csv / sync_vapi_imports_po_csv

# Company-wide ledgers and reference data (not per-plant)
uv run python manage.py sync_rodtep
uv run python manage.py sync_advance_license
uv run python manage.py load_material_category_reference --file path.csv   # --file is required

# Read-only diagnostics
uv run python manage.py report_retired_pos                 # what the next PO sync would retire (fetches the CSVs)
uv run python manage.py report_retired_pos --already-retired --plant achhad   # DB only, no Drive
uv run python manage.py report_match_accuracy
uv run python manage.py backfill_achhad_po_numbers --mir-file in.xlsx --output out.xlsx [--po-csv-file po.csv]

# Maintenance
uv run python manage.py prune_revoked_tokens
```

Every `sync_*` command accepts `--file <path>` to parse a local copy instead of fetching from Drive
- useful for offline dev without live service-account credentials. `sync_rodtep --file` parses
**one** local file instead of listing the folder. The three `sync_*_stock` commands also accept
`--no-snapshot` to skip writing today's `*RMSnapshot` rows (for a same-day re-run after a fix; the
snapshot upsert is keyed on `(stock_lot, snapshot_date)`, so re-running with snapshots on simply
overwrites today's row anyway).

Every `sync_*`, `match_*` and `compute_*_consumption` command writes exactly one `SyncRun` row
(`SUCCESS` / `PARTIAL` / `FAILED`, with `error_detail`) in a `finally` block and exits with
`SystemExit(1)` on failure. That row is what the dashboard's sync badges, the admin panel and
`/api/health/ready` read.

---

## Data sources and sync

### Drive layout - two folders per plant, not one

Confirmed by inspecting live file parent IDs, not assumed:

- Every plant's **PO master CSV** lives together in one shared folder (`PURCHASE_TRACKER_DB_FOLDER_ID`).
  The three **Import PO CSVs** live in that same shared folder.
- Each plant's **MIR and Stock xlsx** live in their own separate folder (`HRS_MIR_STOCK_FOLDER_ID` /
  `ACHHAD_MIR_STOCK_FOLDER_ID` / `VAPI_MIR_STOCK_FOLDER_ID`).
- **RoDTEP** names a whole *folder* (`RODTEP_FOLDER_ID`) holding one file per scrip; **Advance
  Licence** names a single Drive *file id* (`ADVANCE_LICENSE_FILE_ID`).

`google_client.find_file_id_by_title()` takes an explicit `parent_id`; every sync command passes the
right one. Do not consolidate these into one folder ID. Sharing the common parent folder
("Purchase HO") as Viewer with the service account covers all of them (Drive permissions cascade to
subfolders), but the *code* still needs the right subfolder per file.

The folder IDs are env-overridable with hardcoded defaults; the file **titles** are constants in
`config/settings.py` (`HRS_PO_CSV_TITLE`, `HRS_IMPORTS_PO_CSV_TITLE`, `HRS_MIR_FILE_TITLE`,
`HRS_STOCK_FILE_TITLE`, and the `ACHHAD_*` / `VAPI_*` equivalents). A file renamed on Drive breaks
its sync with `FileNotFoundError` until the constant is updated.

**MIR and Stock are shared between domestic and import purchases for a plant** - only the PO-source
CSV differs. Do not build a separate import-only MIR.

### Drive API v3, not v2

`google_client.py`'s search query must use `name = '...'` (v3 field name), not `title = '...'` (v2).
Using `title` returns an opaque `HTTP 400 Invalid Value` with no indication of what's wrong. If a
Drive search ever starts failing with "Invalid Value", check this first.

`list_files_in_folder()` narrows Drive's case-insensitive `name contains` query with a Python-side
prefix re-check - **that re-check lower-cases both sides**. It was once a plain `startswith()`, so a
file named `Rodtep-JNPT-16.xlsx` instead of `RODTEP-JNPT-16.xlsx` passed Drive's query and was then
silently dropped right back out, indistinguishable from "the sync isn't picking up the new file".

`_escape()` covers `parent_id` and `mime_type`, not just `title`. Every current caller passes
env-configured constants (never anything derived from a synced file's own content), so this was
never exploitable - it closes the gap before a future caller reintroduces it.

The Drive client is cached **per thread** (`threading.local`), not as a module singleton.
`httplib2` is not thread-safe, and when several plants' syncs ran on concurrent threads a shared
client produced intermittent SSL errors (`DECRYPTION_FAILED_OR_BAD_RECORD_MAC`,
`WRONG_VERSION_NUMBER`) on whichever thread lost the race (2026-09-04). Keep it per-thread.

`list_files_in_folder()` follows `nextPageToken` until the listing is complete (one call used to
stop silently at 100 files). `find_file_id_by_title()` orders by `modifiedTime desc`, so when
several files share a title the most recently modified one wins, and it logs a warning naming the
count and the chosen id; without the ordering a duplicate upload could switch which file a sync
read from run to run.

### Change detection must compare quantized Decimals, rounded the way Postgres rounds

`sync_utils.unchanged(model_cls, existing, parsed, fields)` is the shared helper the MIR, Stock,
RoDTEP and material-category syncs use to decide whether to skip re-writing a row. Getting this
right took three attempts, each disproven by real data from a different plant:

1. **A `sha256` of the joined `str(field)` values fails immediately.** A `DecimalField` gets
   requantized to its declared `decimal_places` somewhere between Python and storage (a parsed
   `Decimal('950')` comes back as `Decimal('950.000')`), so the string reprs never match even when
   the values are equal.
2. **Naive per-field `==` still breaks.** A currency amount computed at higher precision than the
   column holds (sheet computes `8633.625`, column is `decimal_places=2`) is rounded on save, so
   every re-sync of that row looks "changed" forever.
3. **Quantizing both sides is right, but the rounding mode matters and the obvious choice is
   wrong.** `ROUND_HALF_EVEN` (Python's default) assumes Django rounds via `format_number()` before
   sending a value to the DB. That is false for this project's psycopg3/Postgres setup - confirmed
   directly: `SELECT CAST('353057.445' AS numeric(16,2))` returns `353057.45`, not `353057.44`.
   Postgres receives the full-precision Decimal and does its **own** round-half-away-from-zero on
   cast. `ROUND_HALF_EVEN` made two real Vapi MIR rows (`353057.445`, `84582.225`) report as
   "changed" on every single re-sync, never converging. **It is `ROUND_HALF_UP`.**

`unchanged()` also coerces plain `int`/`float` through `Decimal(str(x))` first, since a parser's
`to_decimal(...) or 0` fallback can hand back a literal `0` where the DB always stores a `Decimal`.

**If you add a sync command for a new source, use `sync_utils.unchanged()`** - do not re-derive a
hash-based or naive-equality comparison, and do not "fix" the rounding mode back to
`ROUND_HALF_EVEN` without re-testing against real data landing on an exact `X.XX5` boundary.

**Idempotency check** (run after touching this function or any sync command): run every plant's
`sync_*` commands twice in a row against real Drive data. The second run must report
`0 created/updated` for every source. A steady-state nonzero count on back-to-back runs means
something isn't converging - find the exact diff with the snippet below. It is written for a MIR
table (keyed on `source_row_ref`); for a Stock table look the row up by `natural_key` instead, since
lots are no longer keyed on the row number (see
[architecture.md](architecture.md#stable-lot-identity)).

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

Whole-object hashes are the deliberate exception, used wherever child rows are always deleted and
recreated together so there is no persisted row to diff field-by-field: `import_sync.py`'s
`_order_hash` (Import POs), `sync_po_csv.py`'s `_po_hash` (and its Achhad/Vapi copies), and
`sync_advance_license.py`'s `_license_hash`. Each stores the hash on the parent row as
`synced_from_row_hash`.

### Purchase orders are retired, not deleted - and until 2026-09-18 they were neither

Every `*PurchaseOrder` model has `is_active` (migration `0051`), and every PO sync - domestic and
import - flips it off for orders the master CSV no longer lists, via
`sync_utils.deactivate_missing_orders()`.

**This was the one entity in the pipeline with no such flag.** MIR entries and stock lots have always
had one and their syncs deactivate vanished rows; the PO sync upserted on `po_number` as a natural key
and never removed anything. So **renaming a PO upstream forked it permanently**: the new spelling was
created as a fresh row and the old one stayed forever, carrying a duplicate set of line items that went
on competing for the same MIR rows. Measured 2026-09-12: 6 ghosts for HRS, 5 for Achhad, 11 for Vapi,
every one a rename rather than a real deletion.

It surfaced when the project owner cleaned `(Changed Purchase Order)` off every PO number at once and
reported that the suffix was still on the dashboard. It wasn't stale data - it was a second, permanent
PO record per renamed order.

**The detection already existed and was invisible.** `orphaned_orders()` found them and the domestic
syncs wrote a warning to **stdout only**, which under the scheduled django-q2 run nobody ever reads -
not a `SyncRun` error, not a flag, not on the dashboard. The three import syncs had no detection at all.
A count now rides on each plant's `sync-status` as `retiredPoCount` (all inactive domestic orders,
`_domestic_base.py`) for exactly that reason.

Four details that are load-bearing:

- **Deactivate, never delete.** An order withdrawn upstream and one merely renamed are
  indistinguishable from here, and a delete is irreversible - which is why `orphaned_orders()`
  originally refused to act at all. Deactivation is reversible, so it is safe to do automatically.
- **The hash-skip must be guarded on `is_active`.** `_upsert_order()` skips writing when the row hash
  is unchanged; without `existing.is_active and` in that condition, an order that was deactivated and
  then came back **unchanged** would skip the write and stay invisible forever. Every MIR/stock sync's
  `unchanged()` check already had this guard for the same reason.
- **`is_active=True` belongs in the upsert `defaults`**, or a returning order is rewritten with fresh
  data and still never reappears.
- **`run_full_match()` deletes matches belonging to inactive orders explicitly.** Every other stale
  match is cleared by the per-item loops, which now only visit active orders - so a match written
  before retirement would otherwise survive every subsequent run untouched, still holding its MIR row
  against the live order that replaced it.

Deactivation runs **after** every parsed order has been upserted, so a renamed order is re-created
under its new number before its old spelling is retired - never the reverse, which would briefly
leave the order book without it. The domestic syncs do this inside the same `transaction.atomic()`
as the upserts; `import_sync.sync_orders()` gives each order its own atomic block and runs the
deactivation after the loop.

`manage.py report_retired_pos` is the read-only companion: `--already-retired` reads the DB (no Drive
credentials needed), the default fetches each master CSV to show what the next sync *would* retire.

**The contradiction gate hides this bug in one narrow case, which is why it took so long to notice.**
When MIR names the clean number, `_po_number_contradicts()` already drops the annotated ghost for free
- the ghost's own multi-token number doesn't match what MIR wrote, while the clean number is a known
order. That protection evaporates the moment the receipt has no PO reference to contradict with, and
~64% of Achhad's MIR rows have none.

### Parser conventions

Every parser in `apps/services/parsers/` follows the same four rules, and a new one should too:

- **Fail loudly on a layout change.** Each checks the sheet name (where fixed) and every labelled
  header cell against an `EXPECTED_HEADERS` constant and raises its own `HeaderMismatch` rather than
  silently reading misaligned columns. The sync command turns that into a `FAILED` `SyncRun` with the
  offending cell named in `error_detail`.
- **`read_only=True` plus `common.stream_rows()`.** Default-mode openpyxl loads pivot caches this app
  never reads and ran Render's 512 MB worker out of memory (2026-09-08). In read-only mode
  `ws.max_row` can be `None` (a broken `<dimension>` tag crashed every plant's sync for 16+ hours on
  2026-09-09) and random cell access costs time proportional to row depth, so the data loop must
  stream forward. Header checks at a fixed shallow row may use `ws["A6"]`-style access; data loops
  must not.
- **Coerce through `common.to_*()`.** `to_decimal`/`to_date`/`to_str`/`to_code_str` never raise; a
  bad cell becomes `None`/`""` and surfaces downstream (arithmetic flag, blank field), never a crashed
  sync.
- **A row with no primary text is not a row.** MIR skips a blank Party Name, Stock a blank
  Description, the CSVs a blank PO Number, RoDTEP a blank Script No, Advance Licence a blank License
  Number.

**`parse_po_csv()`'s header check and its row lookups must use the same normalization.** Four
`EXPECTED_HEADER` columns (`"HSN "`, `"Delivery Date "`, `"Payment Terms "`, `"Currency "`) carry a
trailing space because the live file had them. The header check strips whitespace, but
`csv.DictReader` keys each row off the file's *actual* header, so a lookup by the literal
`row["Payment Terms "]` raised `KeyError` the moment a resave dropped that space (2026-09-17), taking
down that day's HRS PO sync. Every row is now re-keyed by its stripped header name once, right after
validation.

Per-plant layout facts that each parser encodes (why they are separate files at all is in
[architecture.md](architecture.md#per-plant-models-not-a-shared-schema---deliberate-dont-fix-it)):

| Source | Sheet | Header row / data from | Notes |
| --- | --- | --- | --- |
| HRS MIR | `RAW MATERIAL` | 6 / 7 | Day/month-transposed DATE cells repaired from the MIR number's month |
| Achhad MIR | `R.M. ` (trailing space) | 2 / 3 | Month and MIR No. cells Excel turned into dates are reconstructed; DATE is plain text, no repair needed |
| Vapi MIR | ` MIR FILE 26-27 RM` (leading space) | 1 / 2 | `PURCHASE ORDER` column at K since 2026-09-11; DATE repaired like HRS; GST rate is a whole percentage |
| HRS Stock | `Stock` | 6 / 7 | O = party name, P = location tag, both unlabelled; only A-N header-checked |
| Achhad Stock | first sheet (renamed monthly) | 2 / 5 | No vendor column; category comes from divider rows; day-matrix (col O+) deliberately not parsed |
| Vapi Stock | `Stock` | 6 / 7 | Five-row title block above; E is `Batch No.` (was Sub Category); shared multi-plant ledger (`PLANT`) |
| RoDTEP scrip | first sheet | scanned, rows 1-5 | Header row position varies between files |
| Advance Licence | first sheet | scanned, rows 1-5 | Hand-maintained Google Sheet or xlsx |

### Scheduling

Every plant's sync+match pipeline runs on its own via a single `django_q.models.Schedule` row
created/corrected idempotently by `manage.py ensure_schedules` (wired into `release.sh`, which runs
as `render.yaml`'s `preDeployCommand` and as docker-compose's `app` entrypoint when
`RUN_RELEASE_TASKS=1`). `Schedule.CRON`, `cron="0 9-20 * * *"` - **9:00 AM through 8:00 PM IST,
hourly, 12 runs/day**, requiring `croniter`. **The cron is evaluated in `settings.TIME_ZONE`
(`Asia/Kolkata`), not UTC** (django-q2's `Schedule.calculate_next_run()` calls
`croniter(self.cron, localtime())`), so those hours are IST as written - the stored `next_run` is
displayed in UTC and reading it as the schedule is a repeatable mistake.

**A `Schedule` row is inert on its own - something has to be running `manage.py qcluster` to fire
it.** Checked 2026-09-21 on the local dev DB: the row, the cron and `croniter` were all correct and
nothing had run since 2026-09-07, because no qcluster process existed on that machine (no service,
no Windows Scheduled Task). `next_run` simply froze 13 days in the past and every snapshot in the
database came from a human clicking Refresh Data - snapshot dates and manual `SyncRun` dates were
the same set exactly. **When snapshot history has holes, check for a live worker before suspecting
the snapshot code.** `Q_CLUSTER["catch_up"]` is `False` and the Stock xlsx only holds today's
position, so missed days are permanently unrecoverable. The entry point is
`sync_trigger.run_daily_sync_all_plants()`, which also runs each plant's Import PO pipeline and the
company-wide RoDTEP and Advance Licence syncs. Admin/dashboard-triggered `sync-trigger` endpoints
still exist alongside it, and a plant is **skipped, not queued behind**, if a manual refresh is
already mid-flight for it.

What one scheduled run does, in order, on a single qcluster worker (the plants run sequentially, not
as parallel tasks: `Q_CLUSTER["workers"]` is 2, and parallel pipelines would contend for the queue):

1. For each plant: `sync_*_po_csv` -> `sync_*_mir` -> `sync_*_stock` -> `match_*` ->
   `compute_*_consumption`. Consumption must be last: it derives entirely from what the stock sync
   just wrote to `*RMSnapshot`. A failing step is logged and emailed, and the **next step still
   runs** (so a failed MIR sync still leaves match and consumption running on the previous MIR data).
2. For each plant: `sync_*_imports_po_csv` -> `match_*` again (one `run_full_match()` pass covers
   domestic and import line items, idempotently).
3. `sync_rodtep`, then `sync_advance_license`.

Each stage takes its own cache lock (`cache.add()` on the DB-backed cache, atomic across the
gunicorn workers and the qcluster process) with a 900 s safety expiry; see
[sync_trigger.py](#appsservicessync_triggerpy). `Q_CLUSTER["timeout"]` is also 900 s and applies to
`run_daily_sync_all_plants()` as one task, so the whole run - three plants plus imports plus both
ledgers - has to finish inside 15 minutes or django-q2 kills it mid-pipeline. It does comfortably
today; keep it in mind before adding a slow step.

**`GET /api/health/ready` makes a dead worker visible (2026-09-23).** `/api/health` only proves the web
process is up; the 13-day outage above had every page loading normally. The readiness probe
(`apps/api/views.py`'s `readiness()`) returns 503 `degraded` naming each `plant/step` - PO CSV, MIR,
stock, **match and consumption** (the two derive-from-DB steps that look healthy when stale) - that
has not completed (`success`/`partial`) within `HEALTH_SYNC_STALE_HOURS` (default **26**: the schedule
leaves a 13h overnight gap by design, so a shorter window would alarm every night), and 503 `down` if
the DB is unreachable. Unauthenticated and minimal (step names only, no data); `authentication_classes`
is empty so a monitor sending a stale cookie never gets a 401. **UptimeRobot polls it every 5 minutes
(set up 2026-09-23, free plan, on the project owner's account)** and emails on any non-200, then again
on recovery. A site or DB outage therefore alerts within minutes; a dead worker or a failing plant step
only after the 26h window, by design. When it alerts, open the URL: `stale` names the plant/step, and
every step stale at once means the qcluster worker is down. The live endpoint returned 200 with
nothing stale on the day it was set up. Deliberately NOT Render's `healthCheckPath` (that stays `/`): a stale sync is
no reason to restart the web service. On the local box it reports all 15 steps stale, which is the
point. Import PO, RoDTEP and Advance Licence syncs are not watched by it.

The schedule row is still named `"daily-sync-all-plants"` even though the cadence changed twice
(daily -> 3-hourly -> hourly 9-20). **Renaming it risks creating a duplicate schedule row** - see the
command's own module docstring (`get_or_create()` is keyed on `name`). `next_run` is deliberately
never reset on a cadence change either: the first fire after a change may land at a stale timestamp
left over from the previous cadence, then self-corrects to the new cron boundaries. On an existing
row `ensure_schedules` corrects only `func`/`schedule_type`/`cron` (and clears `minutes`) when they
drift.

The report emails run on an **external scheduler** (cron-job.org) hitting shared-secret endpoints,
since Render has no built-in cron on this plan. Three jobs are configured (2026-09-22), all in
**Asia/Kolkata** - which matches `TIME_ZONE`, so the cron times and the dates printed in the reports
agree with no offset arithmetic (the django-q2 sync schedule above is evaluated in `TIME_ZONE` too):

| Job | Endpoint | Cron | Time |
| --- | --- | --- | --- |
| Daily RM consumption | `/api/internal/send-daily-report` | `30 20 * * *` | 20:30 IST daily |
| Monthly RM consumption | `/api/internal/send-monthly-report` | `0 10 1 * *` | 10:00 IST on the 1st (reports the month just ended) |
| Advance Licence expiry | `/api/internal/send-advance-license-expiry-report` | `0 10 * * *` | 10:00 IST daily |

**The licence job runs daily on purpose** - the 30-day window is enforced in code, not by the
schedule. See `advance_license_report.py`'s module docstring: each licence claims a `ReportSendLog`
row the first time it is seen inside the window, so a daily run means "alerted within a day of
crossing 30 days out" with no dependence on one specific run firing. Scheduling it to fire only on a
licence's 30-days-out date would need a job per licence and would miss one entirely on any skipped run.

The secret is passed as an `X-Report-Secret` header rather than `?secret=` so it stays out of
cron-job.org's execution history and Render's access logs. Note that `REPORT_CRON_SECRET` is
base64-ish and can contain `+`, which decodes as a space in a query string - a URL-embedded secret
must be percent-encoded, another reason to prefer the header.

**Never use cron-job.org's "test run" on these.** They are not dry runs: daily/monthly mail every
admin and burn that period's dedup slot (so the real run silently skips), and the licence job
permanently claims every in-window licence, which cannot be undone. Validate URL and secret against
`/api/internal/prune-revoked-tokens` instead - same secret, sends no email, idempotent. It has no
cadence configured.

The plant data correction report (`/api/internal/send-mismatch-report`) is deliberately NOT scheduled
while `MISMATCH_REPORT_PLANT_HEADS_ENABLED` is False - it would return `plant_heads_disabled: true`
every run.

The emails themselves are documented in [auth-security-email.md](auth-security-email.md).

### Sync resilience rules

- **One malformed file must not abort a whole multi-file sync.** `sync_rodtep.py` isolates each
  file's `HeaderMismatch`: a bad file is skipped and named in `error_detail`, every good file still
  syncs, and the overall `SyncRun` status is `PARTIAL` (not `FAILED`) as long as at least one file
  succeeded (`FAILED` if none did) - the same convention `sync_*_stock.py`'s `rows_skipped` handling
  already uses. The isolation covers header mismatches only: a download error, or a file openpyxl
  cannot open at all, still fails the whole run, and every file is downloaded before any is parsed.
  If RoDTEP still doesn't pick up a specific file, check its header row/column layout against
  `parsers/rodtep.py`'s `EXPECTED_HEADERS`; `error_detail` names the file and the mismatch directly.
- **Ledgers have no deactivation step.** `sync_rodtep` and `sync_advance_license` are hand-maintained
  financial ledgers, not current-state snapshots: a row that stops appearing in a re-download is
  *not* treated as "no longer real" the way a vanished stock lot is.
- A failed step inside the scheduled or button-triggered pipeline raises an admin email alert via
  `security_alerts.notify_admins_sync_failure()` (a fixed two-person recipient list, not every
  admin), so it reaches an inbox rather than only `logs/app.log`. Running a command by hand from a
  shell sends no email - only the `SyncRun` row and stderr record it.
- **A stock row with neither a code nor a description is skipped, not keyed on a blank**, and turns
  the run `PARTIAL` with a count in `error_detail` - see `stock_identity.lot_natural_key()`.

---

## File reference

### [apps/services/google_client.py](../apps/services/google_client.py)

The only module that talks to Google Drive (read-only scope `drive.readonly`, service account from
`GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_FILE`). Kept separate so every parser can be
tested against local files with no credentials.

- `DriveNotConfigured` - raised when neither credential setting is present; surfaces as a `FAILED`
  `SyncRun`.
- `get_drive_service()` - builds and caches a v3 client per thread (see the SSL note under
  [Drive API v3](#drive-api-v3-not-v2)).
- `find_file_id_by_title(title, parent_id=None, mime_type=None)` - exact-name search
  (`name = '...' and trashed = false`), optionally scoped to a folder/mime type; returns the first
  hit's id or raises `FileNotFoundError`.
- `list_files_in_folder(parent_id, name_prefix=None)` - every non-trashed file in a folder, narrowed
  by a case-insensitive prefix, sorted by name. Built for RoDTEP. Single page of 100.
- `download_file_bytes(file_id, export_mime_type=None)` - raw bytes via `get_media`, or an export
  when a mime type is given (needed for a native Google Sheet).
- `download_spreadsheet_bytes_by_id(file_id)` - looks up the file's mimeType first, exports a native
  Google Sheet as xlsx or downloads an uploaded xlsx as-is. Used only by the Advance Licence sync.
  Every other source is an uploaded file and goes through plain `download_file_bytes()`, so
  converting one of them into a native Google Sheet on Drive would break its sync.
- `_escape()` - backslash/quote escaping for every interpolated query clause.

### [apps/services/sync_utils.py](../apps/services/sync_utils.py)

Shared, DB-light helpers for the sync commands.

- `unchanged(model_cls, existing, parsed, fields) -> bool` - field-by-field equality, quantizing any
  Decimal side to the model field's `decimal_places` with `ROUND_HALF_UP` after coercing ints/floats
  through `Decimal(str(x))`. See
  [Change detection](#change-detection-must-compare-quantized-decimals-rounded-the-way-postgres-rounds).
- `orphaned_orders(order_model, parsed_orders) -> list[str]` - read-only: every stored `po_number`
  (active or not) absent from the parsed CSV. Used by `report_retired_pos`.
- `deactivate_missing_orders(order_model, parsed_orders) -> list[str]` - flips `is_active=False` on
  every *active* order absent from the CSV and returns the sorted numbers it retired **this run**
  (not the standing list, which would repeat the same names forever). Materializes the queryset before
  the UPDATE because its own filter is `is_active=True`.

### [apps/services/import_sync.py](../apps/services/import_sync.py)

The one upsert implementation behind all three `sync_*_imports_po_csv` commands (the domestic trio
duplicate theirs per plant instead).

- `sync_orders(po_model, line_item_model, parsed_orders) -> (rows_seen, rows_changed, deactivated)` -
  upserts each order in its own `transaction.atomic()` (one bad order does not roll back the rest),
  then calls `deactivate_missing_orders()`.
- `_upsert_order()` - skips when the order is active and `synced_from_row_hash` matches; otherwise
  `update_or_create` on `po_number` with `is_active=True` in the defaults, then deletes and
  bulk-recreates every line item.
- `_order_hash()` - SHA-256 over every order and line-item field the parser produces, including the
  BOE/BL/licence columns.

Import syncs write no `DataQualityFlag` rows (the domestic ones do).

### [apps/services/sync_trigger.py](../apps/services/sync_trigger.py)

**Each plant is one job with one match** (2026-09-25). `_pipeline_commands(plant)` is the plant's
Import PO CSV sync followed by its domestic steps, whose `match_<plant>` matches domestic and import
lines together; both Refresh Data (`trigger_plant_sync()`) and the hourly `run_daily_sync_all_plants()`
run it. Before, each queued the imports pipeline as a second job with its own full match - about 50 s
of worker time an hour for Vapi, and a slower Refresh. `trigger_plant_imports_sync()` and its endpoint
remain for a direct import-only sync; Refresh Data no longer calls it.

A plant lock holds the ISO time it was taken (`_lock_value()`). `sync_stalled(plant_key,
syncrun_plant)` is True when the lock is over `_STALL_AFTER_SECONDS` (180 s) old and no SyncRun of that
plant has started since - the qcluster worker is not running. `sync-status` serves it as
`syncStalled`; the dashboard shows "sync not starting" and stops waiting, instead of "syncing..." for
the lock's 15 minutes and then "still running".

Runs the per-plant pipelines, both for the schedule and for the `sync-trigger` endpoints behind
"Refresh Data". Uses `call_command()` so the pipeline is exactly the management commands, nothing
reimplemented.

- `_PLANT_COMMANDS` / `_IMPORT_PLANT_COMMANDS` - the ordered command lists per plant key
  (`hrs`/`achhad`/`vapi`); see [Scheduling](#scheduling) for the order and why consumption is last.
- `_run_pipeline(plant_key)` / `_run_imports_pipeline(plant_key)` - run each command, catching
  `SystemExit` (a command that already recorded its own `FAILED` `SyncRun`) and any other exception;
  each failure is logged and emailed via `notify_admins_sync_failure()`, and the loop continues to
  the next command - except a step named in `_STEP_PREREQUISITES` whose prerequisite failed this
  run: `compute_<plant>_consumption` is skipped when that plant's stock sync failed, and
  `_record_skipped_consumption()` writes a `FAILED` consumption `SyncRun` saying why. Re-deriving
  from yesterday's snapshots would record `SUCCESS` and make a stale ledger look freshly computed.
  Matching is deliberately not gated: it pairs whatever is in the DB, and a failed PO sync says
  nothing about the MIR sync that succeeded. `_imports` has no gated step. The lock is always
  released in `finally`. Tested in `test_sync_trigger_pipeline.py`.
- `trigger_plant_sync(plant_key)` / `trigger_plant_imports_sync(plant_key) -> bool` - acquire the
  lock with `cache.add()` (atomic; a get-then-set would race), then `async_task()` onto the qcluster.
  Return `False` when a run is already in progress, which the views turn into a 409. Under pytest
  (`"pytest" in sys.modules`) they run inline instead, since no worker exists there.
- `trigger_rodtep_sync()` / `trigger_advance_license_sync() -> bool` - same lock contract, but run
  **synchronously in the request** even outside tests: each is one or two small files, a few seconds
  end to end, well under gunicorn's 30 s timeout.
- `run_daily_sync_all_plants()` - the schedule's `func`. Already runs on a worker, so it calls the
  `_run_*` functions directly rather than queueing more tasks; skips (logs, does not wait for) any
  stage whose lock is held; wraps each stage in its own `try/except` so one plant cannot stop the rest.
- `is_sync_in_progress()` / `is_imports_sync_in_progress()` / `is_rodtep_sync_in_progress()` /
  `is_advance_license_sync_in_progress()` - read the locks for the `sync-status` endpoints.
- Lock keys: `sync_trigger_in_progress:<plant>`, `sync_trigger_imports_in_progress:<plant>` (separate
  namespace, so a domestic and an import sync do not block each other), and one company-wide key each
  for RoDTEP and Advance Licence. `_LOCK_TIMEOUT_SECONDS = 900` self-heals a lock left by a killed
  worker.

Why a queue and not a thread: the first version (2026-09-04) ran the pipeline on a
`threading.Thread` inside gunicorn, and a worker recycle after a missed heartbeat killed the sync
mid-pipeline. A qcluster worker is a separate OS process. A queued task with no worker running just
sits in `django_q_ormq` until the lock expires - nothing syncs.

### [apps/services/validation.py](../apps/services/validation.py)

Dependency-free format checks for the inline "Edit Everywhere" corrections (not used by the sync
itself). `is_valid_gstin()` (15-char GSTIN regex) and `is_valid_email()` (basic `local@domain.tld`)
both return `True` for blank or a placeholder such as "Not available"/"N/A"/"NA"/"none". A `False`
is a **warning** only; callers must never turn it into a 400. The frontend mirrors these regexes -
see [frontend.md](frontend.md) and [api-and-features.md](api-and-features.md).

### [apps/services/arithmetic_checks.py](../apps/services/arithmetic_checks.py)

Dependency-free self-validation of the source sheets' own arithmetic, independent of matching - it
catches a typo in the spreadsheet (transposed digit, rate typed as a fraction, missing tax component,
broken stock formula). Each check returns `None` when an input is missing or the sum holds within a
**Rs 1.00 absolute tolerance** (`_TOLERANCE`, the same epsilon as `matching_core.VALUE_FLAG_EPSILON`),
else `{"check_name", "expected", "actual"}`.

- `check_po_line_item(qty, rate, net_value)` - `po_qty_rate_value`: qty x rate = net value.
- `check_mir_entry(taxable_value, gst_amt, tcs_amt, discount_amt, final_value)` -
  `mir_tax_arithmetic`: taxable + GST + TCS - discount = final. The caller pre-sums GST: HRS/Achhad
  pass `igst + cgst_amt + sgst_amt`; Vapi passes `igst_amt + cgst_amt + sgst_amt + others_with_gst`
  and a zero discount (it has no discount column; without `others_with_gst` ~4% of rows mismatch).
- `check_stock_lot(opening, received, issued, closing)` - `stock_balance`: opening + received -
  issued = closing.

Measured on real data: 100% reconciliation for HRS/Achhad and ~96-99.5% for Vapi, the remainder
genuine data issues.

### [apps/services/data_quality.py](../apps/services/data_quality.py)

The DB layer over `arithmetic_checks.py`. `sync_data_quality_flags(plant, source_type, results)`
takes `{source_id: mismatch-or-None}`, upserts a `DataQualityFlag` per mismatch (keyed on plant,
source type, source id, check name) and **deletes every other flag for that plant and source type**,
so the table is current reality rather than a log. Called at the end of every domestic PO, MIR and
Stock sync, after the upsert transaction. The MIR and Stock syncs check active rows only; the PO
syncs check every line item in the table, including those on retired orders.

### [apps/services/bl_tracking.py](../apps/services/bl_tracking.py)

Live Bill-of-Lading lookup behind the Import Purchases "Track" links; not part of the sync.
`track_bl(bl_number)` calls SafeCube (Sinay) Container Tracking API v2
(`/container-tracking/api/v2/shipment`, `shipmentType=BL`, `API_KEY` header, 15 s timeout) and
**never raises**: it returns `{"ok": True, "data": ...}` or `{"ok": False, "error": "..."}` for an
unset `SAFECUBE_API_KEY`, a blank number, a network failure or a non-200 (SafeCube's own `message`
and `details` joined). Nothing is stored or cached; add a short-TTL cache keyed on `bl_number` only
if the trial key's quota becomes a real problem. Many real BL numbers legitimately return "not
found" (carrier coverage, completed shipments) - that is expected, not a bug.

### [apps/services/parsers/common.py](../apps/services/parsers/common.py)

Dependency-free coercion and normalization shared by every parser and by the matchers. The
matching-only registries and PO-number helpers are documented in depth in
[matching-engine.md](matching-engine.md); what matters for the sync layer:

- `to_decimal()` - numbers, strings with commas, `"NULL"`/blank -> `None`; never raises.
- `to_str()` - trimmed string, `None`/`"NULL"` -> `""`.
- `to_code_str()` - drops the `.0` openpyxl adds to a numeric PO number/SAP code/HSN cell
  (`1100000768.0` -> `"1100000768"`).
- `to_date()` - datetime/date, ISO and several slash/dot formats, or a bare Excel serial in
  36526-73050 (Vapi's Imports CSV once exported `"46064"`); unparseable -> `None`. Tries `%d/%m/%Y`
  before `%m/%d/%Y`, so an ambiguous text date reads day-first.
- `stream_rows(ws, min_row, max_col)` - forward-streams rows as `(row_number, cells)` with each cell
  addressable by index and letter, padded to `max_col`. Row numbers are counted from `min_row`, blank
  rows included, which is what `source_row_ref` stores.
- `repair_month_swapped_date(value, expected_month)` - swaps day and month only when the stored month
  disagrees with `expected_month`, the stored day equals it, and the result is a real date; otherwise
  returns the value unchanged.
- `normalize_vendor()` - **persisted-identity** normalization (part of the stock lot natural key);
  must never change output for a real name. `normalize_vendor_for_matching()` is the looser, matching-
  only fold.
- `normalize_material()` - also a persisted join key (`MaterialCategoryReference`,
  `lot_natural_key()`); `tokenize()`, `clean_mir_material_for_stock()` and `clean_stock_material()`
  exist so that looser handling never touches it.
- Also here, used by matching: `normalize_uom()`, `is_usable_po_reference()`, `clean_po_number()`,
  `legacy_po_key()`/`legacy_po_series()`/`legacy_po_matches()`, and the `NO_PO_VENDORS`,
  `NO_RM_STOCK_VENDORS`, `NOT_STOCKED_MATERIALS` and `VENDOR_ALIASES` registries.

### [apps/services/parsers/po_csv.py](../apps/services/parsers/po_csv.py)

Domestic PO master CSV, one shared parser for all three plants (byte-identical headers).
`parse_po_csv(csv_text) -> list[ParsedPurchaseOrder]` groups the one-row-per-line CSV by `PO Number`
in first-seen order, each order carrying `ParsedLineItem`s. Header check strips whitespace and rows
are re-keyed by stripped header (see [Parser conventions](#parser-conventions)). `currency` defaults
to `"INR"`. `is_old_format_template` is a heuristic (`/`, `HRS` or `HO` in the PO number). The
`PO Number | Item Id` column is only header-checked, never read. The PO number is stored verbatim,
annotations such as `(Changed Purchase Order)` included.

### [apps/services/parsers/import_po_csv.py](../apps/services/parsers/import_po_csv.py)

Import PO master CSV, shared by all three plants. `parse_import_po_csv()` returns
`ParsedImportPurchaseOrder`s with `ParsedImportLineItem`s carrying both `qty_as_per_po` and
`qty_as_per_boe` (matching compares the BOE quantity), exchange rate, landed value, BOE/BL numbers,
laden-on-board date, country of origin and licence type/number. Trailing blank-named header columns
are dropped before the header comparison (a sheet-editing artifact that broke Vapi's sync on
2026-09-07); any real header change still raises. An unparseable delivery date keeps its verbatim
text in `delivery_date_raw`. Like `po_csv.py`, every row is re-keyed by its stripped header right
after validation, so a stray space in a column name (" PO Number ") passes the check and still
reads; the `None` key DictReader uses for an over-long row's extra cells is dropped.

### [apps/services/parsers/mir.py](../apps/services/parsers/mir.py), [achhad_mir.py](../apps/services/parsers/achhad_mir.py), [vapi_mir.py](../apps/services/parsers/vapi_mir.py)

One parser per plant's MIR workbook (`parse_mir_xlsx`, `parse_achhad_mir_xlsx`,
`parse_vapi_mir_xlsx`), each returning a flat list of dataclass rows with `source_row_ref` = the
sheet row number. Shared shape; the differences:

- **HRS** (`RAW MATERIAL`, 37 columns A-AK): four PO-related columns and a SAP GRN column;
  `po_number_raw` is column J read with plain `to_str()`. `_month_label()`/`_mir_no_label()` rebuild
  the typed text of Month/MIR No. cells Excel converted into dates (100% of Month and ~16% of MIR No.
  values were corrupted before). `_repair_mir_date()` fixes the DATE column from the MIR number's
  `/MM` month (177 of 497 cells transposed, 38 of them in the future, measured 2026-09-18). Invoice
  and PO dates carry the same defect but have no independent month to repair from.
- **Achhad** (`R.M. `, A-AG): one PO number/date pair, no GRN; PO number via `to_code_str()`. Same
  Month/MIR No. reconstruction as HRS; DATE is stored as text, so no transposition repair.
- **Vapi** (` MIR FILE 26-27 RM`, A-AD): `po_number_raw` is the `PURCHASE ORDER` column K (added
  2026-09-11); the old `SAP P.O` column D is kept as `sap_po_number` and is blank. No Net/discount
  columns, a whole-percentage `gst_rate_pct` plus amount-only IGST/CGST/SGST, a single TCS amount,
  two "date sent" columns. MIR No. is plain text (`MIR01/04`) and Month is formatted `Mon-YY`.
  `_repair_mir_date()` as for HRS (267 of 782 cells transposed, measured 2026-09-12).

### [apps/services/parsers/stock.py](../apps/services/parsers/stock.py), [achhad_stock.py](../apps/services/parsers/achhad_stock.py), [vapi_stock.py](../apps/services/parsers/vapi_stock.py)

One parser per plant's RM Stock workbook (`parse_stock_xlsx`, `parse_achhad_stock_xlsx`,
`parse_vapi_stock_xlsx`). Formula cells are read with `data_only=True` (cached values). Opening,
received, issued and today's stock default to `0` when blank. `received`/`issued` are period-to-date
cumulative at all three plants - see [consumption.md](consumption.md).

- **HRS**: `Stock` sheet, one row per (material, vendor) lot; party name (O) carries a city suffix
  MIR never has; location tag (P).
- **Achhad**: the workbook's first sheet, whatever its monthly name. No vendor column. A row with
  column A blank is a category divider (its name is stamped onto following rows) or the `TOTAL`
  footer, and is never a lot. Reads only A-N; the daily Recp./Issue matrix from column O onwards was
  deliberately dropped on 2026-09-09, so `RTPAchhadRMDailyMovement` is no longer written by the sync.
- **Vapi**: `Stock` sheet under a five-row title block. `plant_tag` from `PLANT` (a shared ledger:
  RTP-1/HRS/RTP-2 rows). Column E is `Batch No.` since 2026-09-12 and is stored as `batch_no`;
  `sub_category` is no longer populated. `Supplier Name` is a real vendor column; HSN via
  `to_code_str()`.

### [apps/services/parsers/rodtep.py](../apps/services/parsers/rodtep.py)

One RoDTEP scrip workbook (`RODTEP-JNPT-<N>.xlsx`), one row per export Shipping Bill that earned
credit. `parse_rodtep_xlsx()` finds the header by scanning rows 1-5 (`_find_header_row()`) because
real files place it on row 1 or row 2, then reads columns by index. `sanctioned_amount` defaults to
`0`. Nothing in the file references an import; the import side of a licence comes from the Import PO
CSV's licence columns (see [api-and-features.md](api-and-features.md)).

### [apps/services/parsers/advance_license.py](../apps/services/parsers/advance_license.py)

The hand-maintained Advance Licence sheet: one row per (licence, input material, usage), the first
nine columns repeating per licence. `parse_advance_license_xlsx()` uses the same 1-5 header scan as
RoDTEP and groups rows into `ParsedAdvanceLicense` (licence level: numbers, CIF/FOB, import and
export validity dates, status) with `ParsedAdvanceLicenseMaterial` children (material, ITC(HS), qty
and CIF authorised, duty saved %, and the BOE usage columns, blank until imported against).

### [apps/services/parsers/material_category_reference.py](../apps/services/parsers/material_category_reference.py)

The plant manager's Category/Subcategory CSV (`SAP Item Code, Description, HSN Code, Category,
Subcategory (SAP Product Group), UOM`). `parse_material_category_reference_csv()` strips the trailing
`(item code)` from each description before computing `normalized_description` (the match key - SAP
codes are not trusted), and splits `"CARBON BLACK (RM-CB001)"` into subcategory and
`subcategory_code`. Exact header match after stripping; blank descriptions skipped.

### Domestic PO syncs: [sync_po_csv.py](../apps/core/management/commands/sync_po_csv.py), [sync_achhad_po_csv.py](../apps/core/management/commands/sync_achhad_po_csv.py), [sync_vapi_po_csv.py](../apps/core/management/commands/sync_vapi_po_csv.py)

Identical apart from model classes, `*_PO_CSV_TITLE` and the `SyncRun.Plant` tag. Download from the
shared PO folder (or `--file`), `parse_po_csv()`, then in one transaction: `_upsert_order()` per
order (skip on matching `_po_hash` when active; otherwise `update_or_create` on `po_number` with
`is_active=True`, delete and bulk-recreate line items), then `deactivate_missing_orders()`. After the
transaction: `po_qty_rate_value` data-quality flags, and `_report_retired()` prints this run's retired
numbers to stdout, calling out annotated ones as likely renames. Line items are recreated on any
change, so their primary keys are not stable - anything pointing at a line item must key on position
or description (see the manual-pin rules in [matching-engine.md](matching-engine.md)).

### MIR syncs: [sync_mir.py](../apps/core/management/commands/sync_mir.py), [sync_achhad_mir.py](../apps/core/management/commands/sync_achhad_mir.py), [sync_vapi_mir.py](../apps/core/management/commands/sync_vapi_mir.py)

Upsert keyed on `source_row_ref` (the sheet row number) with `unchanged()` over a per-plant
`_FIELDS` list, `is_active=True` in the defaults, then deactivate every active row whose row number
was not seen. A row number is not a real identity - inserting a row mid-sheet rewrites the rows below
onto their neighbours' identities - but MIR has no reliably unique column, so this is accepted and
matching simply recomputes. After the transaction, `mir_tax_arithmetic` flags over active rows
(Vapi's GST sum includes `others_with_gst` and its discount is zero). `_FIELDS` differs per plant
exactly as the parsers' column sets do.

### Stock syncs: [sync_stock.py](../apps/core/management/commands/sync_stock.py), [sync_achhad_stock.py](../apps/core/management/commands/sync_achhad_stock.py), [sync_vapi_stock.py](../apps/core/management/commands/sync_vapi_stock.py)

Upsert keyed on `natural_key` from `stock_identity.OccurrenceCounter.key_for(code, description,
vendor)` - HRS uses `sap_item_code` + `party_name`, Achhad `sap_code` + no vendor, Vapi `hsn_code` +
`supplier_name`. `source_row_ref` is still stored, for diagnostics only. Each lot that is upserted
or unchanged gets today's `*RMSnapshot` (`update_or_create` on `(stock_lot, timezone.localdate())`)
unless `--no-snapshot`. Lots not seen are deactivated, never deleted, so snapshot history survives.
Rows with no key are skipped and make the run `PARTIAL`. Then `stock_balance` flags over active
lots. Vapi's code segment is an HSN tariff code that several materials can share, so two Vapi
materials with the same HSN and supplier are told apart only by the `#N` occurrence suffix, i.e. by
sheet order. The identity rules are in [architecture.md](architecture.md#stable-lot-identity).

### Import PO syncs: [sync_hrs_imports_po_csv.py](../apps/core/management/commands/sync_hrs_imports_po_csv.py), [sync_achhad_imports_po_csv.py](../apps/core/management/commands/sync_achhad_imports_po_csv.py), [sync_vapi_imports_po_csv.py](../apps/core/management/commands/sync_vapi_imports_po_csv.py)

Thin wrappers: download `*_IMPORTS_PO_CSV_TITLE` from the shared PO folder, `parse_import_po_csv()`,
hand to `import_sync.sync_orders()`, record a `SyncRun.Source.IMPORT_PO_CSV` row. An empty,
header-only CSV syncs cleanly as 0 rows.

### [apps/core/management/commands/sync_rodtep.py](../apps/core/management/commands/sync_rodtep.py)

Lists `RODTEP_FOLDER_ID` with prefix `RODTEP` (case-insensitive), downloads every file, and parses
each; a file with a `HeaderMismatch` is skipped and named (see
[Sync resilience rules](#sync-resilience-rules)). Rows upsert on `(script_no, sb_number)` with
`unchanged()` over seven fields, recording `source_file_name`. No transaction around the run, no
deactivation. `SyncRun` plant `COMPANY`, source `RODTEP`.

### [apps/core/management/commands/sync_advance_license.py](../apps/core/management/commands/sync_advance_license.py)

Downloads `ADVANCE_LICENSE_FILE_ID` via `download_spreadsheet_bytes_by_id()`, parses, and in one
transaction upserts each licence on `license_number`, skipping when `_license_hash` matches, otherwise
rewriting the licence and deleting/recreating its `AdvanceLicenseMaterial` rows. No deactivation (a
ledger). `SyncRun` plant `COMPANY`, source `ADVANCE_LICENSE`.

### Match commands: [match_hrs.py](../apps/core/management/commands/match_hrs.py), [match_achhad.py](../apps/core/management/commands/match_achhad.py), [match_vapi.py](../apps/core/management/commands/match_vapi.py)

Call the plant module's `run_full_match()` (`matching.py` / `matching_achhad.py` /
`matching_vapi.py`), print a summary, and record a `SyncRun.Source.MATCH` row with `rows_seen` =
`rows_changed` = domestic + import line items matched + MIR entries stock-matched. Before 2026-09-04
they wrote no `SyncRun` at all, so a matching crash left every badge green. The matching itself is in
[matching-engine.md](matching-engine.md).

### Consumption commands: [compute_hrs_consumption.py](../apps/core/management/commands/compute_hrs_consumption.py), [compute_achhad_consumption.py](../apps/core/management/commands/compute_achhad_consumption.py), [compute_vapi_consumption.py](../apps/core/management/commands/compute_vapi_consumption.py)

Call `consumption_ledger.rebuild_plant_consumption(plant_key, since=...)` - `--all` for the whole
history, `--since YYYY-MM-DD`, default `timezone.localdate()` minus 45 days - and record a
`SyncRun.Source.CONSUMPTION` row (`rows_seen` = lots read, `rows_changed` = material-days written).
No Drive access. Identical except the plant key; Achhad's also folds in any existing
`RTPAchhadRMDailyMovement` rows. Everything about the ledger is in [consumption.md](consumption.md).

### [apps/core/management/commands/ensure_schedules.py](../apps/core/management/commands/ensure_schedules.py)

`get_or_create()`s the `daily-sync-all-plants` `Schedule` (`func` =
`apps.services.sync_trigger.run_daily_sync_all_plants`, `Schedule.CRON`, `0 9-20 * * *`,
`next_run` = now on creation). On an existing row it corrects only drifted `func`/`schedule_type`/
`cron` and clears `minutes`; it never touches `next_run`. Run by `release.sh` on every deploy.

### [apps/core/management/commands/report_retired_pos.py](../apps/core/management/commands/report_retired_pos.py)

Read-only. Default: for each plant (or `--plant hrs|achhad|vapi`) and each of domestic/import,
fetch and parse the master CSV and list *active* orders it no longer contains - what the next sync
will retire. `--already-retired`: list `is_active=False` orders straight from the DB, no Drive
needed. Annotated numbers are marked as almost certainly renames.

### [apps/core/management/commands/backfill_achhad_po_numbers.py](../apps/core/management/commands/backfill_achhad_po_numbers.py)

One-off, read-only helper (2026-09-10) that proposes PO numbers for blank `Purchase Order. No.`
cells in a local Achhad MIR xlsx. It fetches the Achhad PO master CSV from Drive (or
`--po-csv-file`) rather than the DB, identifies candidates with the production gates
(`_vendor_matches()` plus `_material_matches()` at Achhad's threshold), and fills a cell only when
that resolves to exactly one PO, optionally after narrowing by rate within 2% (unit-adjusted). It
writes a **new** workbook with `Backfill Status`/`Backfill Note` in AH/AI and never touches the
database or the input file. Its vendor-plus-material rule is not the matcher's current 2-of-3
identification.

### [apps/core/management/commands/load_material_category_reference.py](../apps/core/management/commands/load_material_category_reference.py)

Manual, not scheduled: `--file` is required (read as `utf-8-sig`, so an Excel BOM is fine). Upserts
`MaterialCategoryReference` on `normalized_description` with `unchanged()`, in one transaction; a
repeated description in the file keeps its first occurrence and is reported. No `SyncRun` row.

### [apps/core/management/commands/create_pt_user.py](../apps/core/management/commands/create_pt_user.py)

Creates or updates a `PTUser` (`update_or_create` on the lower-cased email, `is_active=True`),
refusing an email outside `ALLOWED_EMAIL_DOMAIN` and enforcing the same password policy as the Users
panel (10+ characters, not all digits, not the email local-part). bcrypt with `rounds=12`. Re-running
it on an existing account revokes that account's tokens. The only way to create the first admin; see
[auth-security-email.md](auth-security-email.md).

### [apps/core/management/commands/prune_revoked_tokens.py](../apps/core/management/commands/prune_revoked_tokens.py)

Calls `token_revocation.prune_expired_revoked_tokens()` and prints the count. The same function sits
behind `/api/internal/prune-revoked-tokens`, which has no cadence configured.

### [apps/core/management/commands/report_match_accuracy.py](../apps/core/management/commands/report_match_accuracy.py)

A terminal renderer over `match_accuracy.build_report()`: precision/recall/F1 overall and by plant,
match type, plant x match type, tier and field-coverage band, flagging cells under `MIN_SAMPLE` and
counting stale verdicts. The scoring lives in `match_accuracy.py` so this and the review page's
Accuracy tab cannot drift; see [matching-engine.md](matching-engine.md).
