# Stock consumption, the consumption ledger, and Days-Left

How much raw material each plant consumes per day, how that rolls up into months/quarters/financial
years, and how the dashboard's Days-Left column and the daily/monthly consumption emails are built
from it. Everything here is **derived**: nothing comes from Drive directly, and the whole ledger can be
dropped and rebuilt from the stock snapshot history at any time.

Data flow:

```
sync_<plant>_stock  ->  *RMSnapshot (one row per lot per sync day)      [+ RTPAchhadRMDailyMovement at Achhad]
        |
compute_<plant>_consumption  (last step of the plant pipeline, no Drive fetch)
        |   consumption_ledger.rebuild_plant_consumption()  ->  consumption_engine (pure arithmetic)
        v
MaterialConsumptionDaily  (plant, material_key, day, qty, quality)   the only atom
ConsumptionEvent          (intervals excluded from the rate, kept visible)
ConsumptionCoverage       (plant, day) observed - the rate denominator
        |
consumption_periods  (SUM rollups, trailing-window rates, confidence bands)
        |
        +-- /materials API (_domestic_base._lot_dict: consumption block, daysLeft, daysToMsl)
        |       -> materials.js aggregateMaterialsByName() / daysLeftCellHtml() / Low Stock
        +-- consumption_report.py (daily + monthly emails, via cron-job.org -> reports_views)
```

Related docs: the stock sync and snapshot history are in [data-sync.md](data-sync.md), the qcluster
schedule and the `SyncRun` health probe in [architecture.md](architecture.md) and
[testing-deployment.md](testing-deployment.md), the email plumbing (dedup, SMTP budget) in
[auth-security-email.md](auth-security-email.md), the Raw Material Analysis UI in
[frontend.md](frontend.md) and [api-and-features.md](api-and-features.md). Lot identity
(`*RMLot.natural_key`) belongs to [matching-engine.md](matching-engine.md).

---

### Days-Left engine

Per-material consumption rate and days-of-cover in Raw Material Analysis.

**The arithmetic this section used to describe is superseded and no code path runs on it.** Read
[Consumption ledger](#consumption-ledger-2026-09-21---supersedes-the-days-left-engines-arithmetic)
for what is current. What the old engine was, and what of it survives:

- **The old engine** was `stock_consumption.consumption_stats()`: per lot, the day-over-day drawdown
  of the closing balance plus the snapshot's `received`, skipping any gap over 7 days, with confidence
  bands keyed on the *span* between first and last snapshot (`high` = 14+ days and 5+ intervals,
  `medium` 7/3, `low` 2/1). It was built on the belief that only Achhad's `issued` is period-to-date and
  that the books reconcile with the balance on only 45%/87%/82% of days (HRS/Achhad/Vapi). **Both
  beliefs were wrong** - see the next section. The 45% figure was an artifact of comparing a
  cumulative column against a one-day balance delta. **Do not restore the balance-drawdown method as
  primary on the strength of that finding.**
- **The trap it contains.** The formula `consumption = (stock_yesterday - stock_today) + received`
  looks right, but `received` is a cumulative period-to-date level at every plant, so adding the level
  (the code's `recv = float(r1) if r1 != r0`) instead of its increment adds the whole running total on
  any interval where `received` grew. That is what overstated consumption 1.31x-1.85x. Its "received
  frozen across snapshots" double-counting guard was a symptom of the same misreading: a cumulative
  column naturally stays flat when nothing new arrives.
- **`stock_consumption.py` is dead code**, imported by nothing except
  `apps/services/tests/test_stock_consumption.py`. It is kept deliberately, with its superseded
  docstring intact, because the reasoning it records is what the ledger overturns. Delete it only
  together with that test. (It is not dependency-free: it imports `django.utils.timezone`.)

What survives unchanged from the old engine, now computed from the ledger:

- **Confidence is rendered, never a hidden filter.** A thin-history figure is shown, with the band
  saying how thin it is. Band `none` renders "-" instead of a number in `materials.js`
  (`daysLeftCellHtml()`), so a two-day estimate never looks like a month-long one.
- **`daysToMsl`** (Achhad only, from `RTPAchhadRMLot.msl`; HRS/Vapi get `None` through the same
  `getattr(lot, "msl", None)` no-branching pattern) is `0` the instant stock is at or below MSL,
  independent of whether any consumption rate is known - being below the reorder point must not wait
  on 30 days of history. Otherwise it is `(stock - msl) / avgDaily`, or `None` with no rate.
- **Aggregation rules** (`materials.js`'s `aggregateMaterialsByName()`): days-left is **never summed
  or averaged across lots** - rates are summed and the already-summed quantity is divided by them.
  Group confidence is the **weakest** contributing band, not the best or a mean. The "Low Stock"
  status fires on `daysLeft < 15` **while the band is not `none`**, or on any contributing lot's
  `daysToMsl === 0` (`isMaterialLowStock()`). The band gate keeps Low Stock to rows whose Days Left
  cell shows the number: under `none` the cell reads "-", and a Low Stock row the reader cannot
  check was possible in All Plants view, where one thinly-covered plant makes the whole group
  `none` while another plant's rate still produced a figure. The MSL half needs no rate and is not
  gated. What changed: rates are now counted **once per plant, not once per lot**
  (see the read path below).
- **One query for the whole plant, not one per lot.** The old `_consumption_by_lot()` is gone; its
  replacement is two constant queries (ledger aggregate plus coverage), pinned by
  `apps/api/tests/test_materials_days_left.py` with `django_assert_num_queries(2)` for HRS and Achhad.

### Consumption ledger (2026-09-21) - supersedes the Days-Left engine's arithmetic

`MaterialConsumptionDaily` + `ConsumptionEvent` + `ConsumptionCoverage`, built by
[consumption_engine.py](../apps/services/consumption_engine.py) (pure, dependency-free) and
[consumption_ledger.py](../apps/services/consumption_ledger.py) (its DB layer), rolled up by
[consumption_periods.py](../apps/services/consumption_periods.py), written by
`manage.py compute_hrs_consumption` / `compute_achhad_consumption` / `compute_vapi_consumption`.

**The premise the Days-Left engine was built on is wrong.** Re-measured on live data 2026-09-21:

- **All three plants' `received`/`issued` are period-to-date cumulative.** `opening + received -
  issued == closing` holds on **4,327 of 4,327** rows. `opening_stock` is frozen across consecutive
  snapshots on 100% (Achhad, Vapi) and 83% (HRS) of pairs - it is the *period* opening, not
  yesterday's closing. `issued` is monotonic per lot on 100%/100%/80% of lots.
- Therefore `(closing_0 - closing_1) + (received_1 - received_0) == issued_1 - issued_0` **by
  construction** within a period - verified on **2,909 of 2,909** same-period intervals, zero
  failures. The engine's primary figure is `issued_1 - issued_0`, the plant's own issue book.

**The defect it caused.** Adding the cumulative `received` level inflated consumption **HRS 1.78x,
Achhad 1.85x, Vapi 1.31x** - from only 5/1/12 intervals, all on large lots. 29 active materials read
2-10x too low on days-left, several banded `high`: SILSHEET RUBBER 4.2 -> 13.0, QUREANTI MMB
7.3 -> 24.7, Imported Coal 2.7 -> 10.4. Exactly the rows that trip `daysLeft < 15`.

**Four things that look like consumption and are not**, separated by
`consumption_engine.classify_interval()` rather than averaged into a rate:

- **`restatement` - the biggest distortion, and not the one first suspected.** The sheet rewrites
  `opening` AND `closing` while `issued` never moves: a corrected figure, nothing issued. 148 of
  HRS's 192 opening-change intervals; the balance method counts **766,459 units** there against the
  issue book's **48,975**. Canonical case HRS lot 119 `SACK CARBON`, 450,500 -> 17,850 on 2026-09-04
  with `received`/`issued` both 0 either side - 432,650 phantom units, roughly a third of that plant's
  whole measured total, from one cell. Reading the issue book gets it right for free (0 - 0 = 0).
  **This is why an `opening` change must NOT fall back to the balance delta** - that is the
  restatement case and the balance is 15x wrong on it. Branch on whether **`issued` reset**, not on
  whether `opening` changed. A restatement **counts toward the rate** at its issue-book quantity.
- **`period_roll`** - `issued` genuinely resets downward. 44 of HRS's 192; none at Achhad/Vapi.
  Excluded, recorded as a `ConsumptionEvent` at its balance-implied size (floored at 0).
- **`closeout`** - a lot with no prior counted movement in the window jumps to closing 0 in one step,
  with `received` unchanged and the issue delta at least 90% of its prior balance. 8/8/4 events,
  **3-7%** of each plant's total. An earlier "at least 90% of the lot wiped in one interval" heuristic
  without the "no prior movement" and "received unchanged" terms put this at 61%/34%/53% and was
  wrong - it swept up genuine high-turnover and gap-spanning consumption. A lot already being drawn
  down that simply finishes is real consumption. Excluded, recorded as an event.
- **`books_disagree`** - the identity fails (tolerance 0.0005) while `opening` is unchanged. **0 of
  2,909** in real data; a canary, not a workaround. Counted at the issue-book figure.

**All four kinds become `ConsumptionEvent` rows.** `period_roll` and `closeout` are excluded from the
rate; `restatement` and `books_disagree` are counted at their issue-book figure AND logged, because
the balance disagreed with the books (`_LOGGED_BUT_COUNTED` in the engine). They are logged even at
zero quantity - the SACK CARBON shape, a 432,650-unit balance rewrite with nothing issued, is the
commonest restatement and would otherwise leave no trace. A counted one-day restatement with a
positive quantity also stores its label as that day's `MaterialConsumptionDaily.quality` (a
multi-day one becomes `spread`).

`spread` is not an exclusion: real consumption the snapshots can't pin to one day (span longer than
`max_dated_gap_days`, default 1). It is divided evenly across the span with the rounding remainder on
the last day, so `sum(days) == interval` and a period total stays exact. The old engine discarded any
gap over 7 days outright, throwing away 18% of HRS's and **63% of Achhad's** measured consumption.

**The daily row is the only atom.** Month/quarter/financial-year totals are a `SUM` over
`MaterialConsumptionDaily`, never a second independent derivation -
`consumption_periods.period_series()/material_totals()/period_summary()`, with financial quarters
(Apr-Jun = Q1) and Apr-Mar years matching the `*_25-26` Drive folder convention. Both consumption
emails now read the same rows (see the read path). Note that the period helpers have **no production
caller yet** - only tests use them; the live readers use `consumption_rates()` and
`coverage_in_window()`.

**`material_key` is `normalize_material(description)` - deliberately NOT `*RMLot.natural_key`.**
That key is `<code>|<vendor>#<occurrence>`, shaped for MIR<->Stock matching. Consumption does not care
who supplied a material, and vendor-keying forks one material across its suppliers (HRS buys SBR 1502
from three). The `#occurrence` suffix is assigned by sheet row order: measured 2026-09-21, 56 HRS lots'
`opening_stock` changed between Sep 2 and Sep 3 and **54% of those picked up the previous lot's
opening** - a reshuffle splicing two materials' histories. A wrong match is visible and reviewable
(`MatchReview`); a spliced consumption series just returns a confident wrong number. A material *code*
would be better if all three plants had one - Vapi's is `hsn_code`, a tariff code where 24 of 73
distinct values cover more than one material, so it would merge unrelated materials outright. The code
is stored as a non-key column (`material_code`) instead. Consequence: a description reworded in the
sheet starts a new material key.

**`MaterialConsumptionDaily`/`ConsumptionEvent`/`ConsumptionCoverage` are shared tables with a `plant`
column** - a deliberate exception to the per-plant models rule (see [architecture.md](architecture.md)),
and the `SyncRun` case rather than the MIR/Stock case. That rule exists because the three plants'
*spreadsheets* differ; nothing in these tables comes from a sheet column. Cross-plant rollups are a
first-class use case (`material_totals()` with `plant=None`), only possible because `material_key` is
uniform. **Don't split these into three models.**

**Achhad's dated movement matrix wins for the days it covers.** `RTPAchhadRMDailyMovement` is already
per-day and its dates are the *real issue dates* - a snapshot lags them by a day (lot
`Isnr - (Svr - 10)`'s 09-03 movement first appears in the 09-04 snapshot). The cutoff is the latest
`movement_date` across the whole matrix, and every Achhad lot's snapshot series is trimmed to after
it. `_points_after()` synthesises an anchor point dated exactly at the cutoff, carrying the matrix's
own issues up to it, so the first snapshot interval measures only what the matrix didn't. **Keeping the
last real snapshot as the anchor instead double-counts**: matrix through Sep 3, snapshots on Sep 2 and
Sep 5, and Sep 3 gets a share from both. In the live data the two sources happened to abut exactly
(matrix through Sep 7, next snapshot Sep 8) so nothing overlapped - which is why this needed a test
rather than an inspection. If the matrix claims more issued by the cutoff than the next snapshot's
running total, the synthetic point makes that interval a `period_roll` event: two sources disagreeing
is a finding, not something to average away.

**A bounded rebuild reads further back than it writes.** An interval needs the snapshot *before* it,
so reading from `since` leaves the first day of the range with no predecessor and it silently
vanishes. `_ANCHOR_LOOKBACK_DAYS = 30` of extra read, `date >= since` filter on the write. Only shows
up on the bounded path, which is the path every scheduled run takes (the commands default to a 45-day
lookback).

**Lots are read regardless of `is_active`.** A lot that has since sold out genuinely consumed material
while it was alive, and its snapshots survive deactivation by design. The Days-Left engine filtered
these out and lost that history. (The *current stock* used for days-left does filter to active lots.)

**Runs on the qcluster, not an external cron.** `compute_<plant>_consumption` is the last step of each
plant's `_PLANT_COMMANDS` pipeline in `sync_trigger.py`, after `match_<plant>`. It must run last - it
derives entirely from what `sync_<plant>_stock` just wrote. It fetches nothing from Drive and is
independent of the match step; the ordering is the only coupling and it is one-way. It records a
`SyncRun.Source.CONSUMPTION` row for the same reason `match_*` records `MATCH`, and more urgently: a
stale consumption ledger looks perfectly healthy from outside, because yesterday's rows are still
there returning plausible numbers. `/api/health/ready` includes this step for that reason. Only the
ledger rebuild moved onto the worker; the report **emails** are still triggered by cron-job.org.

**Verification.** Rebuilt over the full live history and reconciled against an independent
recomputation from raw snapshots: HRS and Vapi agree **exactly** (556,948 and 576,075, diff 0);
Achhad's 235,377.08 = 102,401.88 (matrix) + 132,975.20 (snapshots after the cutoff) with **zero**
ledger rows on or before the cutoff disagreeing with the matrix.

#### The read path (migrated 2026-09-21, same day)

Both readers go through the ledger; **nothing computes consumption per request any more.**

- **`_domestic_base.py`** - `_consumption_by_material()` is a one-line call to
  `consumption_periods.consumption_rates(plant, today=timezone.localdate())`. `make_materials()` calls
  it once per request; `_lot_dict()` looks the lot's material up by `normalize_material(description)`,
  copies the block, and adds `daysLeft = days_of_cover(lot.todays_stock, avgDaily)` (this lot's own quantity at the
  material's rate) plus `daysToMsl`. A material with no ledger rows in the window gets the plant's
  `MaterialRates.no_movement` block - the plant's real coverage and band with `avgDaily: null`,
  which the frontend shows as "No movement" - or `consumption: null` when the plant's band is
  `none`, where "did not move" and "was not watched" still cannot be told apart. Before
  2026-09-25 every quiet material got `null` and read "Not enough snapshot history yet", the same
  as a plant nobody had watched. Achhad is two queries like the others: its matrix is reconciled at build time.
- **`consumption_report.py`** - one `_ledger_rows()` shared by both reports. It imports no snapshot
  model; `*RMLot` is read only for display fields, the live rate and current stock.

**This fixed a second, separate reporting error.** "Issued Today" used to read `*RMSnapshot.issued`
directly for HRS/Vapi, on the belief that only Achhad's column was period-to-date. All three are. So
the daily email printed a running month-to-date total under a column headed "Issued Today", and the
monthly email **summed those running totals across the month**.

**Achhad's period-to-date `(est.)` fallback is gone, and `isEstimate` means something else now.** The
fallback existed because the day-matrix could be blank for the current day while the live cumulative
column had a figure; there is no live cumulative column in the read path any more. The flag now marks
a quantity **interpolated across a snapshot gap** (any `spread` row in the period) rather than
observed on one dated day, and applies at all three plants. A day the ledger has no rows for reports
nothing.

**Rows are per material, not per lot**, in the API payload's rate and both emails. A material split
across vendor lots used to appear once per lot, each holding a fragment of the day's issues.
`materials.js`'s `aggregateMaterialsByName()` therefore **counts each rate once per plant, not once
per lot** (dedup on `_plantKey`) - summing across `g.lots` was right when every lot carried its own
fragment and would now multiply the rate by the lot count. The per-plant dedup is what keeps "All
Plants" correct, since the ledger genuinely is per plant.

**Rates divide by days the plant was OBSERVED, not by the window length** - `ConsumptionCoverage`, one
row per (plant, day) that fell inside some lot's counted snapshot interval, written by the same build.
This is not a refinement; it was found in verification and it was wrong in the shipped write path for
an hour. `MaterialConsumptionDaily` holds only days something moved, so dividing by 30 asserts that
every day without a row was a real zero - **including the days nobody looked**. HRS had 17 covered
days in a 30-day window on 2026-09-21, so that assertion put every rate 43% low. A quiet day *inside*
coverage still dilutes the average, which is correct. Coverage is read from the **intervals**, not
from the quantities: a zero-quantity interval writes no ledger row but its days were still watched.
With zero covered days the denominator falls back to the window length. At Achhad, matrix-period
coverage comes only from matrix days with a positive issue (see the ledger file reference).

**Expect thinner confidence bands than before, and that is the point.** The old bands keyed on the
SPAN between the first and last snapshot, so 7 real days inside a 17-day span read `high`. They now
key on coverage of the 30-day window (`_BANDS` in `consumption_periods.py`: `high` needs 80% covered
and 50% directly observed, `medium` 50%/20%, `low` 5%/0, else `none`). The band is **plant-wide** -
every material at one plant gets the same band on a given day. Against 2026-09-21's data every HRS
material sat at `low` - correctly, because the plants had 6-7 distinct snapshot dates in an 18-day
span. They climb on their own once a qcluster worker actually runs every day (see
[architecture.md](architecture.md) on scheduling).

Real movement on the live data, HRS: SILSHEET RUBBER 4.2 -> 8.2 days, CARBON BLACK N330 BKT
0.7 -> 16.7, QUREANTI MMB (MB2) 7.3 -> 251.6.

**An empty report says WHICH kind of nothing it found (2026-09-21).** Until this was fixed the empty
body always read "No material was issued today", so a plant whose sync had silently died got a calm
all-clear every morning, indistinguishable from a genuinely quiet day - the same conflation this whole
ledger exists to stop, reproduced in the covering sentence. `_empty_message()` asks
`ConsumptionCoverage`: a day inside a snapshot interval was observed, so zero rows there really is "no
material was issued"; a day with no coverage was never looked at, and the email says so in block
capitals, names the last date that does have data, and points at the Drive sync. A period entirely
before the plant's history says that instead of claiming no history exists.

The `(est.)` footnote was reworded in the same pass to describe what the flag means everywhere: a
figure averaged across a gap between snapshots.

---

## File reference

### apps/services/consumption_engine.py

Role: pure, dependency-free (no Django imports) derivation of what was consumed between two
consecutive snapshots of one lot, and the classification of everything that looks like consumption
but is not. Safe to import from a migration; unit-tested as plain Python in
`apps/services/tests/test_consumption_engine.py`.

- **Classification constants** - `COUNTED`, `SPREAD`, `RESTATEMENT`, `PERIOD_ROLL`, `CLOSEOUT`,
  `BOOKS_DISAGREE` (plus internal `_NOT_AN_INTERVAL`). The strings are persisted in
  `MaterialConsumptionDaily.quality` and `ConsumptionEvent.kind`, so renaming one needs a data
  migration. `EXCLUDED_FROM_RATE = {period_roll, closeout, not_an_interval}`; restatement and
  books_disagree are deliberately not in it.
- **`ConsumptionPoint`** - NamedTuple `(date, opening, received, issued, closing)`, all `Decimal`
  straight from the DB, never floats.
- **`ConsumptionInterval`** - `(start, end, span_days, quantity, balance_quantity, classification)`
  with `counts_toward_rate`. `quantity` is always the best figure even when excluded;
  `balance_quantity` is what the old balance method would have said, for comparison.
- **`classify_interval(p0, p1, *, lot_moved_before, max_dated_gap_days)`** - the decision order is
  load-bearing: span <= 0 -> `not_an_interval`; issue delta < 0 -> `period_roll` (checked before any
  opening comparison); dormant lot to closing 0 with `received` unchanged and delta >= 90% of prior
  closing -> `closeout`; identity broken beyond 0.0005 -> `restatement` if `opening` changed else
  `books_disagree`; otherwise `counted` or `spread` by span. Quantity is always `issued_1 - issued_0`
  except `period_roll` (balance estimate, floored at 0).
- **`intervals_for_lot(points, *, max_dated_gap_days=1)`** - sorts the points (so unordered input and
  same-date duplicates are harmless) and classifies each consecutive pair, tracking whether the lot has
  already moved (a positive counted interval) for the closeout rule.
- **`allocate_daily(interval)`** - spreads an interval over the days *after* `start` through `end`.
  One-day intervals keep their own classification as the quality; wider ones split evenly, quantized
  to 0.001, all marked `spread`, remainder on the last day so the sum is exact. Zero or negative
  quantities allocate nothing.
- **`daily_from_points(points, ...)`** - composition for one lot: `({date: (qty, quality)},
  excluded_intervals)`. Only intervals that count toward the rate are allocated; `excluded` holds only
  rate-excluded intervals (period_roll, closeout), which the ledger turns into events.
- **`rate_from_daily(daily, *, window_start, window_end)`** - average over the full calendar window.
  **Not used by any production path** (tests only), and its window-length denominator is the opposite
  of the coverage decision the live readers follow. Do not wire it into a reader.

### apps/services/consumption_ledger.py

Role: the DB layer over the engine. Reads one plant's stock history, runs the engine per lot, and
replaces that plant's rows in `MaterialConsumptionDaily`, `ConsumptionEvent` and
`ConsumptionCoverage`. The only consumption module that writes.

- **`PlantLedgerConfig` / `PLANT_CONFIGS`** - per-plant wiring (`hrs`/`achhad`/`vapi`): lot and
  snapshot models, `code_field` (`sap_item_code`/`sap_code`/`hsn_code`, stored for display, never part
  of the key), `uom_field` (none at Achhad), and `daily_movement_model` (Achhad only). Its own copy
  rather than an import from `apps/api`, per the layering rule.
- **`_lot_meta(cfg)`** - every lot's description/category/code/uom and `normalize_material()` key in
  one query, **not filtered on `is_active`**.
- **`_snapshot_points(cfg, since)`** - every lot's snapshots in one `values_list` query ordered by
  (lot, date) and grouped with `itertools.groupby` (the ordering is what makes `groupby` correct).
- **`_dated_movements(cfg, since)`** - Achhad only: `{lot_id: {date: issued}}` for positive issues
  from `RTPAchhadRMDailyMovement`, plus the matrix's last date (max across all rows, receipts
  included). Returns `({}, None)` for HRS/Vapi.
- **`_points_after(points, cutoff, matrix_days)`** - trims a lot's snapshots to after the matrix
  cutoff and synthesises an anchor dated at the cutoff whose `issued` includes the matrix's issues
  between the last real snapshot and the cutoff (closing reduced by the same amount), so totals are
  preserved and only attribution changes.
- **`rebuild_plant_consumption(plant_key, *, since=None, max_dated_gap_days=1)`** - the entry point.
  Reads from `since - 30 days` (`_ANCHOR_LOOKBACK_DAYS`), writes only `date >= since` (`None` = whole
  history). Per lot: adds matrix days (`counted`, marking coverage observed), then snapshot-derived
  days, then coverage from every rate-counting interval (a day is `observed=True` if any lot had a
  single-day interval ending there), then events for excluded intervals ending on or after `since`.
  Sibling lots of one material are summed per day, `lot_count` records how many contributed, and
  `spread` wins if any contributor was spread. Display fields come from one lot per key, preferring
  one with a material code. Delete-then-`bulk_create` of all three tables inside one
  `transaction.atomic()`, so a crash leaves the previous ledger intact and a re-run is idempotent.
  Returns `{plant, lots_read, material_days, coverage_days, spread_days, events, total_quantity}`.
  Non-obvious:
  - A lot with a blank description is skipped silently. The in-code comment says it is "counted in
    the result dict instead"; it is not counted anywhere.
  - At Achhad, days before the matrix cutoff are covered only if some lot had a positive issue that
    day, so a matrix day with no issues at all does not enter the rate denominator.
  - `lots_read` counts lots with any snapshot in the read window, not lots written.

### apps/services/consumption_periods.py

Role: read-side rollups and rates over the ledger. Every figure is a `SUM` over
`MaterialConsumptionDaily`; nothing re-derives from snapshots. Financial year is April-March.

- **Period bounds** - `month_bounds()`, `quarter_bounds(year, q)` (financial quarters, Q1 = Apr-Jun,
  `year` is the FY start year, so `quarter_bounds(2026, 4)` is Jan-Mar 2027),
  `financial_year_bounds()`, `financial_year_label()` ("2025-26"), `financial_year_of()`,
  and `bounds_for(grain, ...)` as the one entry point from a grain to a date range.
- **`material_totals(start, end, *, plant=None, min_quantity=None)`** - per-material totals, biggest
  first, with `spreadQuantity`/`spreadDays` alongside. `plant=None` rolls all three plants together on
  `material_key`. Display fields come from the latest row per material so a reworded description does
  not split results. `avgDaily` divides by the calendar span.
- **`category_totals()`**, **`period_series(grain, start, end, ...)`** (Python-side bucketing, so the
  financial-quarter offset lives in one place; rejects an unknown grain), **`period_summary()`**
  (headline totals plus `excludedEvents` grouped by kind). A range that cuts through a spread interval
  gets only its share - `spreadQuantity` exists to make that visible.
- **`consumption_rate(material_key, *, plant, window_days, today)`** - single-material trailing rate.
  Divides by `window_days`, **not coverage** - inconsistent with `consumption_rates()`. Tests only.
- **`consumption_rates(plant, *, today, window_days=30)`** - **the live one**, used by both the
  materials API and the emails. One aggregate query plus `coverage_in_window()`; returns a
  `MaterialRates` dict, `{material_key: {avgDaily, quantity, confidence, coverageDays, observedDays,
  materialDays, materialObservedDays, windowDays, windowStart, windowEnd}}`, whose `no_movement`
  attribute is the same block with `avgDaily: None` and zero quantity for a material with no ledger
  row (or `None` when the band is `none`). `avgDaily = total / covered_days`
  (window length if nothing is covered). Confidence comes from `_confidence_band()` on plant-wide
  coverage and observed ratios, so it is the same for every material at the plant.
- **`coverage_in_window(plant, start, end)`** - `(covered days, observed days)` from
  `ConsumptionCoverage`.
- **`days_of_cover(stock, avg_daily)`** - Days Left, the one implementation behind the API and the
  emails: `stock / avg_daily`, or `None` with no rate, no stock figure, or **negative stock**. A sheet
  showing stock below zero is a data error, not an empty store; dividing it gave a negative Days Left
  that counted as Low Stock and read as a real warning.
- **`whole_days(days)`** - the emails' Days Left text: rounded half-up to a whole day, as
  `daysLeftCellHtml()`'s `Math.round` does on screen (Python's `round()` is banker's and would make
  12.5 into 12); `"N/A"` for `None`.
- **`DEFAULT_WINDOW_DAYS = 30`**, `_BANDS`, `plant_keys()`.

With no production caller beyond `consumption_rates()`/`coverage_in_window()`, the period helpers are
tested infrastructure waiting for an endpoint or report; see `apps/services/tests/test_consumption_ledger.py`.

### apps/services/consumption_report.py

Role: builds and sends the Daily and Monthly Raw Material Consumption emails, one per plant, reading
the ledger. Triggered synchronously by `apps/api/routers/reports_views.py`
(`trigger_daily_report`/`trigger_monthly_report`, shared-secret endpoints hit by cron-job.org). The
dedup, SMTP time budget and failure reporting are described in
[auth-security-email.md](auth-security-email.md).

- **`_PLANTS`** - label, `SyncRun` plant, lot model, rate field, vendor field (`party_name` /
  none / `supplier_name`) and, for Achhad, the MIR<->Stock match model used for vendors.
- **`_report_recipients()`** - every active admin plus the fixed `purchase@ravasco.com`, deduplicated.
  Kept separate from `security_alerts._admin_emails()` so the purchasing mailbox never gets security
  alerts.
- **`_material_display(cfg)`** - `material_key -> {material, category, rate}` from the live `*RMLot`
  rows (all lots, not only active); the highest-value lot wins where lots disagree; blank category
  becomes "Uncategorized".
- **`_vendors_by_material(cfg)`** - HRS/Vapi from the Stock sheet vendor column, highest-value lot
  first; Achhad from `party_name` on non-dismissed MIR<->Stock matches, newest receipt first, flagged
  `viaMir`. **`_vendor_display()`** caps at three names plus "+N more" and appends "(per MIR)".
- **`_current_stock(cfg)`** - total `todays_stock` per material over **active** lots only.
- **`_ledger_rows(cfg, start, end, qty_key)`** - the shared row builder: one aggregate over the
  ledger for the period (materials with qty > 0), plus the trailing 30-day rates as of today. Each row:
  material, category, vendor, quantity under `qty_key`, rate, `daysLeft = current stock / avgDaily`,
  confidence, `isEstimate` (any spread row in the period). Sorted by quantity descending.
- **`_empty_message(cfg, start, end, period_phrase)`** - "No material was issued" only when the period
  has coverage; otherwise the block-capitals no-snapshot message naming the nearest date with data.
- **`build_plant_report(plant_key, today=None)`** / **`build_plant_monthly_report(plant_key, year,
  month, today=None)`** - daily (`issuedToday`) and monthly (`issuedThisMonth`, default the last
  completed month). `daysLeft`/confidence are always present-tense, whatever month is summarised.
- **`_ledger_rows()`** sets `daysLeft` through `days_of_cover()` and adds `negativeStock`; the rows
  render Days Left as whole days (`whole_days()`), or "N/A (stock below zero in sheet)".
- **`_render_consumption_rows()` / `_render_consumption_email()`** - shared category-grouped table
  and text builders (categories ordered by their biggest mover, via dict insertion order over the
  quantity-sorted rows); `_render_report_email()` / `_render_monthly_report_email()` supply title,
  column label and `(est.)` footnote. Both bodies render the confidence legend from one
  `_CONFIDENCE_LEGEND` tuple, which must stay in step with `consumption_periods._BANDS`; a test
  checks the two cover the same bands.
- **`_send_plant_reports(...)`**, **`send_daily_consumption_reports()`**,
  **`send_monthly_consumption_reports(year, month)`** - claim a `ReportSendLog` row per plant before
  building, release it on failure, share one SMTP connection, defer plants past the 12s budget, and
  return per-plant outcomes, failures and timings.

Tests: `apps/services/tests/test_consumption_report.py` (seeds snapshots and runs
`rebuild_plant_consumption()` first, so it exercises the real ledger), `apps/api/tests/test_reports_views.py`.

### apps/services/stock_consumption.py

Role: the superseded Days-Left engine, kept as dead code (see
[Days-Left engine](#days-left-engine)). Nothing imports it except
`apps/services/tests/test_stock_consumption.py`.

- **`consumption_stats(points, *, window_days=30, today=None)`** - per-lot balance-drawdown rate over
  `(date, todays_stock, received, issued)` tuples; skips gaps over 7 days; returns the old payload
  shape (`avgDaily`, `daysLeft`, `confidence`, `historyDays`, `intervalsUsed`, `receiptIntervals`,
  `estimatesAgree`, window bounds). Contains the cumulative-`received` defect described above.
- **`_issued_cross_check()`**, **`_confidence_band()`** (span-based `_BANDS`).

### apps/core/management/commands/compute_hrs_consumption.py

Role: thin wrapper (identical shape across the three plants) that calls
`rebuild_plant_consumption("hrs", since=...)`, prints a one-line summary, and records a
`SyncRun(source=CONSUMPTION)` row in a `finally`.

- **Arguments** - default `since = timezone.localdate() - 45 days` (`_DEFAULT_LOOKBACK_DAYS`: wide
  enough for late-arriving snapshots, which change the intervals on both sides of them, and for a
  month boundary when the monthly report runs on the 1st); `--all` rebuilds the whole history;
  `--since YYYY-MM-DD`.
- **SyncRun** - `rows_seen = lots_read`, `rows_changed = material_days`. On any exception the run is
  `FAILED` with the message in `error_detail` and the command exits 1. Excluded events are a correct
  outcome, reported on stdout only.
- Fetches nothing from Drive, so it cannot fail for a network reason.

### apps/core/management/commands/compute_achhad_consumption.py

Role: the same wrapper for `"achhad"` / `SyncRun.Plant.RTP_ACHHAD`. The only behavioural difference
lives in the ledger: Achhad's `RTPAchhadRMDailyMovement` matrix is authoritative up to its last date
and snapshot intervals are used only after it.

### apps/core/management/commands/compute_vapi_consumption.py

Role: the same wrapper for `"vapi"` / `SyncRun.Plant.RTP_VAPI`. Vapi has no daily matrix, so it
reads snapshot intervals only.

### apps/core/models/consumption.py

Role: the three derived tables. All share one schema across plants (a `plant` column with
`SyncRun.Plant` choices); all are safe to drop and rebuild with `compute_<plant>_consumption --all`.
Import them from `apps.core.models`.

- **`MaterialConsumptionDaily`** - unique on `(plant, material_key, consumption_date)`; `quantity`
  `Decimal(16,3)`; `quality` (help text says `counted`/`spread`, but `restatement`/`books_disagree`
  can also appear, see above); denormalised `material_description`, `material_code`, `category`,
  `uom` refreshed on every rebuild and kept off the key; `lot_count`; `computed_at`. Indexed on
  `(plant, consumption_date)` and `(plant, material_key, consumption_date)`, the rollup shapes. Only
  positive quantities are ever written.
- **`ConsumptionEvent`** - an interval excluded from the rate (`period_roll`, `closeout` in
  practice), with `lot_ref` as a plain `"<LotModel>#<pk>"` string (not an FK: three lot models, and it
  must survive lot removal), interval dates, the issue-book `quantity` and the old method's
  `balance_quantity`. Unique on `(plant, lot_ref, interval_start, interval_end, kind)`.
- **`ConsumptionCoverage`** - one row per (plant, day) inside some counted interval; `observed` is
  True when at least one lot had a single-day interval ending there. The rate denominator; plant-wide
  by design, so a material with no row on a covered day genuinely consumed nothing that day.

Also relevant: `RTPAchhadRMDailyMovement` in `apps/core/models/achhad.py` (the matrix source, one row
per lot per activity day) and `SyncRun.Source.CONSUMPTION` in `apps/core/models/sync.py`
(migrations `0053`, `0054`).

### apps/api/routers/_domestic_base.py (consumption read path only)

Role: serves the per-lot `consumption` block on each plant's `/materials` endpoint.

- **`_consumption_by_material(cfg)`** - `consumption_rates(cfg.syncrun_plant,
  today=timezone.localdate())`.
- **`_lot_dict(...)`** - attaches a copy of the material's block, falling back to
  `MaterialRates.no_movement` (or `None`), adds `daysLeft` from
  this lot's own `todays_stock`, and `daysToMsl` from `msl` (Achhad) as described above.
- **`make_materials(cfg)`** - active lots only, one rate lookup per request.

### frontend/js/materials.js (consumption read path only)

- **`aggregateMaterialsByName(lots)`** - groups lots by normalized description; sums one `avgDaily`
  per plant (dedup on `_plantKey`), recomputes `daysLeft = group qty / avgDaily` (null, with
  `negativeStock: true`, when the group's quantity is below zero - `days_of_cover()`'s rule), and takes the
  weakest band (`CONF_RANK`) with its coverage figures.
- **`isMaterialLowStock(m)`** - `daysLeft < 15` with a band other than `none`, or any lot's
  `daysToMsl === 0`; backs the "Low Stock
  (Reorder Soon)" KPI card and the `lowstock` status filter.
- **`daysLeftCellHtml(m)`** - the Days Left cell: "-" for band `none` or no block, "No movement" when
  a block has no `daysLeft` (a watched material that did not move - `no_movement`), otherwise the rounded days plus a confidence dot whose tooltip states
  coverage and observed days out of the window.

`main.js` lists `consumption` among each plant's sync steps (`DOMESTIC_SYNC_STEPS`) but excludes it
from the "rows updated from Drive" count, since it re-derives every run.
