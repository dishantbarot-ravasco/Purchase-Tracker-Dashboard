# Raw Material matching - a plain-language explainer

For the business owner. How the app links goods received (MIR) to the RM Stock sheets at HRS,
RTP-Achhad and RTP-Vapi, and what the Raw Material page shows. Consumption and days-left are
covered separately in [consumption.md](../consumption.md). Technical detail lives in
[matching-engine.md](../matching-engine.md) and [data-sync.md](../data-sync.md).

## Where the stock data comes from

- Each plant keeps one RM Stock workbook on Google Drive, in the same folder as its MIR file:
  - HRS: `HRS RAW MATERIAL STOCK.xlsx`, sheet `Stock`
  - Achhad: `RAVASCO ACHHAD RM STOCK FILE.xlsx`, the first tab (renamed every month)
  - Vapi: `RAVASCO VAPI RM STOCK FILE.xlsx`, sheet `Stock`
- The app copies the sheet into its database on every scheduled sync (hourly, 9 am to 8 pm IST).
- The sheet only ever shows today's position. It has no history of its own.
- If a column heading moves or is renamed, the sync stops with an error rather than guessing.

## What a "lot" is

- A lot is one row of the stock sheet: one material, usually from one supplier.
- Each lot gets a permanent ID called its **natural key**: `material code (or cleaned name) | supplier`.
  - HRS: SAP item code + party name
  - Achhad: SAP code only (the sheet has no supplier column)
  - Vapi: HSN code + supplier name
- If two rows share the same key, the second gets `#2`, the third `#3`, in sheet order.
- The row number is never used as identity. Inserting a row mid-sheet would otherwise hand one
  material's history to another.
- A lot missing from today's sheet is marked inactive, never deleted, so its history survives.

## How a MIR row is matched to a stock lot

Every active MIR row is checked against every active lot at its plant, on each sync.

### Step 1 - rows the RM sheet is not expected to hold are skipped

- Two registries (see below) remove a row from this pairing before any comparison runs.

### Step 2 - supplier must agree (hard gate)

- At HRS and Vapi, a lot from a different supplier is never a candidate, however well the rest fits.
- Names are cleaned first (Ltd/Pvt/punctuation removed, known aliases folded). They agree if:
  - they are identical after cleaning, or
  - one contains the other (HRS's stock sheet adds a city, e.g. "... - Vadodara"), or
  - they are at least 90% similar (absorbs single-letter typos).
- **Achhad has no supplier column, so it has no supplier gate.** It relies on material alone,
  which is weaker.

### Step 3 - material, in three tiers (best evidence wins)

- Descriptions are cleaned first: Vapi's SAP code prefix on MIR ("RM00011014 ZINC OXIDE") and a
  trailing plant tag on stock ("... 6MPA HRS") are stripped.
- **Tier 3 - same name.** The two descriptions are identical after cleaning.
- **Tier 2 - close enough.** A word-weighted similarity score of at least 0.45, tolerant of word
  order and small typos ("PRECIPITATD SILICA").
- **Tier 1 - same day, same price.** The names share nothing, but the MIR date equals the lot's
  receipt date **and** the rates agree within 2%. This is the stock side's stand-in for a PO number
  (e.g. "Kanatol-8A (DOA)" to "DOA Oil").
- Only the strongest tier found for a MIR row is kept. Ties are all kept (one material can sit in
  several lots).

### Guards that keep tier 1 honest

- **Grade-code gate.** If both sides name a grade code and none agree, the pair is rejected
  ("NBR 2675" vs "NBR 3345", same supplier, same day, same price). Also applied to tier 2.
- **Tier-1 exclusivity.** A lot claimed on date + rate alone goes only to the MIR row of that date
  that best describes it. This stops two same-day, same-price bags swapping lots.
- **Stock quantity is never used to identify.** The sheet's received column is a running total and
  agrees with MIR almost never; rate agrees 97-100% on true pairs.

### Step 4 - figures are checked (only when the dates agree)

- Rate, and (when the lot's received qty is not zero) quantity and value are compared at **zero
  tolerance**, value with a Rs 1 rounding allowance.
- Dates must match first: a lot's rate is its latest receipt, so comparing it to a months-old MIR
  row would only measure price drift.
- **Units are converted first** (MT vs KG, litres vs KL). If the units cannot be converted, nothing
  is compared and the pair shows an amber "units differ" badge, not "matched".

## The two "don't expect a match" registries

| | No-PO vendors | RM untracked |
| --- | --- | --- |
| Question | Is there a purchase order? | Does the RM sheet hold these goods? |
| Affects | PO <-> MIR only | MIR <-> Stock only |
| Keyed on | Vendor name | Vendor (`NO_RM_STOCK_VENDORS`) and material class (`NOT_STOCKED_MATERIALS`) |
| Scope | One list, all plants | Vendor list shared; material list **per plant** |

- `NO_RM_STOCK_VENDORS` today lists only Madura (four spellings). Madura is properly PO'd, so it is
  not a no-PO vendor. The two lists must stay separate.
- `NOT_STOCKED_MATERIALS`, per plant:
  - All plants: conveyor fabric, conveyor belting, bare "rubber compound", MS crates.
  - Spares/services: all plants, but grease is excluded only at Achhad (HRS and Vapi stock grease).
  - Printing/labels/logo: excluded at HRS and Vapi, **not** at Achhad (Achhad stocks Lamor logo film).
- **They suppress the match, never the row.** The rows stay in MIR, still reconcile against their
  POs, and are counted on the Raw Material page as "N not tracked in RM".

## What the Raw Material page shows

- One row per material (lots of the same name grouped), plus a row for any open order line that
  links to no stock lot, so open orders are never invisible. Import orders are included, in INR.
- **KPI cards** (each is a filter): Materials Tracked, Total Inventory Value, Inventory Value in
  Transit (open POs), Quantity Ordered (open POs), Quantity Mismatches, Rate Mismatches, Low Stock,
  Data Quality Flags.
- **How the chain is built - two separate bridges, not one join:**
  - Stock <-> PO line: worked out in the browser by name similarity (and supplier where the lot has
    one). A best-effort link, not a proof.
  - PO line <-> MIR: the server's purchase-order match.
  - MIR <-> Stock: the server's match described above. It drives the "MIR / Stocked" progress dots.
- **Statuses**: Overdue (a linked open PO is late), Partial (open PO and some stock), On Order
  (open PO, no stock), Received (a MIR match exists, or a linked PO is fully received), In Stock
  (in stock, no receipt traced).
- **Flags**: the mismatch KPIs and flag filter read the linked PO lines' PO <-> MIR figures. The
  MIR <-> Stock result (matched / rate or qty difference / units differ) appears per lot in the
  material pop-up's Stock by Plant tab, where an editor can dismiss it.

## Daily stock snapshots

- Each stock sync also saves a snapshot per lot for today: opening, received, issued, today's
  stock, rate and value. A re-sync on the same day overwrites that day's snapshot.
- This is the only stock history the app has. Drive only holds today's figures.
- **A day with no sync has no snapshot, and it cannot be recovered later.** The scheduler worker
  must be running. The status bar shows a snapshot-gap badge when days are missing.
- The history feeds the per-lot stock trend chart and a CSV export (editors).

## How the three plants differ

| | HRS | RTP-Achhad | RTP-Vapi |
| --- | --- | --- | --- |
| Supplier column in stock sheet | Yes (with city suffix) | **No** | Yes |
| Supplier gate on matching | Yes | **No** | Yes |
| Code in the lot key | SAP item code | SAP code | HSN code (shared by materials) |
| Lots with a receipt date | ~100% | **~65%** | ~95% |
| Grease excluded as not stocked | No | Yes | No |
| Printing/logo excluded | Yes | No | Yes |
| Other quirks | Location tag column | Category divider rows | `PLANT` column: RTP-1 (Vapi's own, 145 rows), HRS (20) and RTP-2 (3); SAP code prefix on MIR |
| MIR rows matched (2026-09-21) | 53.5% | 52.7% | 20.4% |
| Matched, of rows the sheet can hold | ~84% | ~100% | ~85% |

## Known limits

- **Madura fabric never reaches RM.** 830 MIR rows, about Rs 30.7 crore, zero stock lots at any
  plant. It is 62% of Vapi's gap. Registered as untracked, not a matching failure.
- **Fabric and belting are outside every RM sheet.** Conveyor fabric, belting, bare compound and
  crates are 36% of HRS's MIR rows, 47% of Achhad's and 76% of Vapi's. Open question for the
  plants: are these tracked in a file the app does not sync?
- **Vapi has the worst raw coverage** (about 20%) almost entirely because of the above. Against
  the goods its sheet actually holds, it matches about 85%, in line with the others.
- **Achhad has no supplier gate** and a third of its lots have no receipt date, so tier 1 cannot
  help those lots.
- **The scorer penalises a more specific description.** Achhad's "Lamor Logo Print" misses its lot
  "Lamor Logo 160mm X 70Mic (12290)" (score 0.24 vs 0.45) because the extra size/code words count
  against it. A looser score was tested and hurt Achhad badly. Filling Achhad's receipt dates would
  let tier 1 recover it at no cost.
- Vapi's HSN code is shared by several materials, so two same-HSN, same-supplier lots are told
  apart only by sheet order.
- **Vapi's HRS and RTP-2 rows are real Vapi stock, not double counts.** Checked 2026-09-25: they
  are job-work material physically held at Vapi (billing "JOBWORK", names like "RECLAIM RUBBER 6MPA
  HRS"), and none of the 23 appears in HRS's or Achhad's own sheet. Counting them as Vapi lots is
  correct.
- Every match is a suggestion. Nothing is actioned automatically.

## The value check on MIR <-> Stock

- It is computed and stored, but not shown as its own badge; only rate and quantity differences
  raise one. Deliberate for now: value is quantity x rate, so a value gap almost always shows up as
  a quantity or rate badge already.
