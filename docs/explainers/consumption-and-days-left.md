# Consumption and Days Left - how the numbers are made

A plain-language guide for the business owner. The technical reference is
[docs/consumption.md](../consumption.md); where it and the code differ, this page follows the code.

## 1. What goes in

- **One stock snapshot per lot per day.** Each time the RM Stock sheet is synced, the app saves that
  day's row for every lot (a same-day re-sync overwrites it). Nothing else feeds consumption.
- Each snapshot carries four figures: **opening, received, issued, closing**.
- **All four are "period-to-date" at all three plants.** Opening is the period's opening (not
  yesterday's closing); received and issued are running totals since the period began.
- The sheets are exact: `opening + received - issued = closing` held on every one of 4,327 rows tested.
- **Achhad extra:** its sheet also has a day-by-day issue matrix. For the days it covers, the matrix
  is used instead of snapshots, because its dates are the real issue dates.

## 2. How one day's consumption is worked out

- For each lot, compare two consecutive snapshots: **consumed = issued today - issued yesterday**.
- Because issued is a running total, the difference is exactly what was issued in between.
- **Example:**

  | Date | Opening | Received | Issued | Closing |
  | --- | --- | --- | --- | --- |
  | 10 Sep | 3,000 | 2,000 | 1,200 | 3,800 |
  | 11 Sep | 3,000 | 2,000 | 1,450 | 3,550 |

  Consumed on 11 Sep = 1,450 - 1,200 = **250**. (Check: closing fell 250 and nothing was received.)
- **Snapshot gap:** if the next snapshot is 14 Sep with issued 1,750, the 300 is real but cannot be
  pinned to a day. It is split evenly, 100 each on 12, 13 and 14 Sep, and marked "spread"
  (estimated). Month totals stay exact; any rounding remainder goes on the last day.

## 3. Things that look like consumption but are not

Each is detected and logged as an event, never silently averaged in:

- **Restatement** - someone corrects opening and closing, but issued does not move. Counted as the
  issue-book figure (usually 0). Real case: HRS "SACK CARBON" went 450,500 -> 17,850 with nothing
  issued; the old method called that 432,650 units consumed.
- **Period roll (reset)** - issued drops (e.g. 9,800 -> 150) because the sheet started a new period.
  The true figure is unknowable from two rows, so the interval is **left out** of the rate.
- **Closeout** - a lot that had not moved suddenly goes to zero in one step, with no new receipt and
  at least 90% of its balance "issued". Treated as a write-off or transfer and **left out**.
- **Books disagree** - the sheet's own arithmetic fails. Never seen in real data; kept as an alarm.
  Counted at the issue figure but logged.

## 4. Grouping by material

- Lots are grouped by a **material key**: the description, lower-cased, punctuation removed
  (e.g. "SBR-1502 " and "sbr 1502" are the same key).
- The vendor is **not** part of the key, so one material bought from three suppliers is one line.
- Consequence: if a description is reworded in the sheet, it starts a new material history.

## 5. What is stored

- **Daily ledger** (`MaterialConsumptionDaily`): one row per plant, material and day with the
  quantity consumed. Only days with positive consumption get a row.
- **Coverage** (`ConsumptionCoverage`): one row per plant per day that fell inside a snapshot
  interval, i.e. a day the app was actually watching. "Observed" = seen on a one-day interval
  rather than estimated across a gap.
- **Events** (`ConsumptionEvent`): the restatements, resets and closeouts above, with sizes.
- Every month, quarter or year total is a plain sum of daily rows.

## 6. The daily rate

- **Window:** the last 30 days, today included.
- **Rate = total consumed in the window / number of covered days** (not 30).
- Why: a day with no row might be a quiet day or a day nobody looked. Coverage tells them apart.
  Quiet but watched days still count in the divisor; unwatched days do not.
- If the plant has **no** covered days, the divisor falls back to 30.
- The rate is per plant. In "All Plants" view, each plant's rate is added once (not once per lot).

## 7. Days Left - the exact formula

- **Days Left = current stock / daily rate.**
- **Current stock** = the latest synced closing stock (`todays_stock`) summed over the material's
  **active** lots on screen (all three plants' lots when "All Plants" is selected).
- **Rounded to the nearest whole day** everywhere: "13 d" on screen, "13" in the daily and monthly
  emails, or "N/A" with no rate.
- **Worked example** (window 27 Aug - 25 Sep):
  - Ledger total 3,400; plant covered 17 of 30 days, 12 of them observed directly.
  - Rate = 3,400 / 17 = **200 per day**. Stock on hand 2,500. Days Left = 2,500 / 200 = **12.5 -> "13 d"**.
  - Under 15, so it counts as **Low Stock (Reorder Soon)**.
  - Dividing by 30 instead would give 113/day and 22 days: not flagged, and wrong.
- **Edge cases:**
  - **No consumption in 30 days**, at a plant that was watched (band low or better): the cell
    shows **"No movement"**, with the plant's coverage in the tooltip.
  - **Not enough watching** (band none): the cell shows "-" with "Not enough snapshot history yet".
    With that little coverage, "did not move" and "was not watched" cannot be told apart.
  - **Negative stock** in the sheet is a data error: no Days Left is worked out. The screen shows
    **"Stock < 0"** (tooltip: fix it in the sheet), the emails show "N/A (stock below zero in
    sheet)", and it does not count as Low Stock on Days Left.
  - **Achhad reorder level (MSL):** if stock is at or below MSL, the lot is Low Stock at once,
    even with no rate. HRS and Vapi have no MSL column.

## 8. Colours and flags on screen

- **Confidence dot** beside the number, based on how much of the 30 days the plant covered:

  | Dot | Band | Needs (share of 30 days) |
  | --- | --- | --- |
  | Green | high | 80% covered and 50% observed |
  | Amber | medium | 50% covered and 20% observed |
  | Grey | low | 5% covered |
  | "-" shown, grey dot | none | less than that |

- The example above (17 covered, 12 observed) is **medium / amber**.
- The band is plant-wide: every material at a plant gets the same band on a given day.
- A material combining lots takes the **weakest** band among them.
- Tooltip reads e.g. "17 of 30 days covered, 12 observed directly, the rest averaged across snapshot gaps".
- **Low Stock** = Days Left under 15 **while the number is shown** (band not none), or any Achhad
  lot at or below MSL. A row never counts as Low Stock on a Days Left figure the reader cannot see.
  There is no separate red colour for Days Left itself; it feeds the Low Stock KPI card and status
  filter.

## 9. When it is recomputed

- The rebuild is the **last step** of each plant's pipeline: sync PO, sync MIR, sync stock, match,
  then compute consumption. It fetches nothing from Drive.
- Runs on every scheduled sync (hourly 09:00 - 20:00 IST) and on every manual "Sync" click.
- Each run rebuilds the **last 45 days** (reading 30 more days back for anchors), replacing old rows
  in one transaction. `--all` rebuilds the full history.
- **Skipped if that plant's stock sync failed** in the same run. It is recorded as a failed step
  ("Skipped - sync_stock failed..."), so the dashboard never shows a stale ledger as freshly computed.
- **Needs the qcluster worker.** If it is not running, no sync happens, so no snapshot is taken.
- **A missed snapshot day cannot be recovered** later; the sheet only shows today. The gap becomes a
  "spread" estimate, and the dashboard shows a snapshot-gap badge.

## 10. Consumption emails

- **Daily report** (issued today) and **monthly report** (issued last month), one per plant, sent
  to active admins plus purchase@ravasco.com.
- Both read the same daily ledger; Days Left in them is always "as of today".
- "(est.)" marks a figure that includes a spread (gap-averaged) day.
- An empty report says which kind of empty: "no material was issued" if the day was covered, or a
  capital-letters **NO STOCK SNAPSHOT WAS CAPTURED** warning if it was not.
- Triggered by an external scheduler (cron-job.org), not the qcluster; moving them is deferred.

## 11. Known limitations

- **Thin history reads low confidence** until the worker runs every day; that is by design.
- **qcluster gaps** leave permanent holes; the estimate across them is flagged, not hidden.
- **All Plants view takes the weakest band.** If one plant holding the material has band none,
  the whole row shows "-" and is not counted as Low Stock on Days Left, even if another plant's
  rate is solid. Open that plant's own view to see its figure.
- **Reworded descriptions** split a material's history in two.
- **Dead code:** `apps/services/stock_consumption.py` is the old method (it added the running
  received total and overstated consumption 1.3x - 1.85x). Nothing uses it except its own test.
