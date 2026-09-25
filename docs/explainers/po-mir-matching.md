# How PO <-> MIR matching works

A plain-language guide for the business owner. It covers domestic and import purchase orders at
HRS, RTP-Achhad and RTP-Vapi. Engineering detail lives in [matching-engine.md](../matching-engine.md).
Every match is a suggestion for a person to check, never an automatic decision.

## The two sides

- **PO side (domestic):** each plant's PO master CSV on Drive, one row per PO line item.
- **PO side (import):** each plant's Imports PO CSV, with BOE, bill of lading, exchange rate and licence columns.
- **MIR side:** each plant's MIR workbook (Material Inward Register), one row per receipt line.
- Domestic and import lines compete for the **same** MIR table; one MIR row can serve only one line.
- Only active POs are matched. A PO dropped from the CSV is retired and its matches deleted.

## Step 1: who is even a candidate?

- A MIR row must have a party (vendor) name to be considered at all.
- **Contradiction gate:** if the MIR row names a *different* PO we hold, it is ruled out.
- A mistyped or unknown PO number is treated as "no evidence", never as evidence against.
- **Date gate:** a receipt more than 7 days before the PO date, or more than 270 days after, is ruled out.
- Exception: if the MIR row names this PO, the date is assumed wrong and only loses its weight.
- **2-of-3 rule (all three plants):** at least two of these must agree:
  - **PO number** written in MIR names this order (whole-token match, not a substring).
  - **Vendor** name agrees (after cleaning: exact, one name contained in the other, or 90% similar).
  - **Material** description is similar enough (0.3 at HRS and Achhad, 0.2 at Vapi).
- So vendor + material works for rows with no PO number; PO number alone never does.
- A match made with a wrong vendor name is kept but flagged "Vendor Name Mismatch in MIR".

## Step 2: ranking the candidates

- Candidates are sorted by **evidence tier** first; money only breaks ties inside a tier.
- Tier 4: PO number and vendor both agree.
- Tier 3: PO number agrees.
- Tier 2: material agrees and figures agree closely (score >= 0.9).
- Tier 1: material agrees and figures are plausible (score >= 0.5).
- Tier 0: material agrees, nothing else supports it.
- Inside a tier: 55% financial closeness, 20% date plausibility, 25% material similarity.
- Financial closeness weighs value 42%, qty 29%, rate 29%; a figure missing on either side is skipped.
- Date plausibility: best on the PO date, fading to a floor after that; up to 7 days early scores 0.6.
- A "confidence" threshold (0.55) is shown in the UI but does **not** block a match.

## Step 3: settling who gets which receipt

Done in this order, each step only using MIR rows still free:

1. **Manual pins** (a person said "this line came in under MIR X").
2. **PO-number groups:** one PO, many receipts (see below).
3. **Rate groups:** 2+ receipts within 2% of the PO rate, total qty not over 150% of ordered.
4. **Best overall assignment** for everything left, so no row is claimed twice.

- A rate group that loses a member to an earlier step falls back to single-receipt matching.
- "PO Not Found" simply means no candidate passed Step 1.

## One PO, many receipts

- Every receipt that passes Step 1 and cites **exactly one** of our POs is counted against that PO.
- No rate tolerance and no 150% cap here: a disagreeing figure is flagged, not dropped.
- Several lines on one PO: each line first gets one receipt, then extras go to the best-fitting line.
- Every counted receipt is saved on the match (`group_entries`) and shown in the PO modal.
- The PO modal shows Ordered / Received / Difference in real figures, summed in the PO line's unit.
- A Vapi cell naming several POs at once stays on the single-receipt path.

## Comparing qty, rate and value

- **Qty and rate** are the only hard errors ("Quantity Mismatch" / "Rate Mismatch").
- **Value is pre-tax on both sides for domestic:** PO net value vs MIR `net` (HRS, Achhad) or `taxable_value` (Vapi).
- MIR invoice/final values (post-GST) are never compared against PO net value.
- Taxable and final value checks against PO totals run **only for single-line POs** (the CSV repeats the PO total on every row).
- GST type (IGST vs CGST+SGST) is also checked.
- Value, taxable, final, GST type and unit problems roll up into one "data mismatch" flag.
- Over- vs under-delivery is recorded separately (over / under / could not tell).

## Units

- Units are converted before comparing: KG vs MT, etc. convert within the same family.
- An unrecognised unit is compared as-is (guessing is worse).
- Two different families (e.g. KG vs NOS) are a **unit mismatch**: no qty/rate % is invented.
- A unit mismatch is always severity "material" and shows as its own badge, not as green.

## Flag thresholds and results

- **Zero tolerance** on qty and rate: any difference at all flags (owner's decision).
- Kept identical in the three plant modules and `flags.js`'s `FLAG_PCT`.
- Value checks allow Rs 1.00 absolute, to absorb rounding.
- Severity by largest difference: under 5% "rounding", 5-20% "minor", 20%+ "material".
- Results per PO line: matched clean, matched with flags, matched with data mismatch, or PO Not Found.
- Row flags: red = mismatch, blue = partial delivery, yellow = on order, purple = data quality.
- PO Not Found is kept out of red while an order is still open, or red would mean nothing.

## Receipts with no PO behind them

- **No-PO vendor registry:** vendors the company never raises a PO against, one list for all plants.
  - Internal transfers (own plants and sister units): permanent.
  - No-PO suppliers (e.g. Tinna, Eternia, JMF, Gangamani): a process gap, remove when they get POs.
- Their receipts are skipped for PO matching **unless** the row names a PO we hold.
- The row is never hidden: it still reconciles against RM stock and is counted on the dashboard.
- Registry lookup is exact (after cleaning); no fuzzy matching, so it cannot swallow real suppliers.
- Unmatched receipts fall into **three buckets**, each with a different owner:

| Bucket | Meaning | Who fixes it |
| --- | --- | --- |
| `no_po` | MIR cites no PO (blank, VERBAL, NIL...), or a short number that is not one of our POs | Purchasing |
| `po_unknown` | MIR cites a PO we never received in the CSV | Upstream PO generator |
| `po_known_unmatched` | MIR cites a PO we hold but it did not match | Us (data drift) |

- Internal transfers are excluded from all three and counted separately.
- A `no_po` receipt that matched anyway stays in its list (it was still bought without a PO).
- A receipt citing one of our POs is never `no_po`, even a short HRS number like `1074` (see below).
- Two badges, never one combined number. CSV download is `?download=csv`.

## Manual pins and dismissals

- **Pin:** a person picks the MIR number for a PO line (domestic or import).
- A pin names a MIR **number**, not a row; the matcher picks the best row under that number.
- An empty pin means "no MIR matches this line, leave it unmatched".
- Pinned pairs skip Step 1 but are still measured, so flags stay honest.
- A pin goes **stale** (ignored and reported, not deleted) if the PO line's description changes.
- If the pinned MIR number is missing or already taken, the line stays unmatched (no fallback), and
  the app says so: the pin is reported as "unfilled" and the person who saved it gets a warning.
- **Dismiss:** a person marks a flagged match as accepted; it survives every re-match that keeps the
  same MIR receipt.
- If a later run pairs the line with a **different** receipt, the dismissal is cleared, so the new
  receipt's flags are seen.
- PO-level flags can be dismissed too, keyed on plant + PO number + flag.

## Per-plant differences

| | HRS | RTP-Achhad | RTP-Vapi |
| --- | --- | --- | --- |
| MIR value column used | `net` | `net` | `taxable_value` (no `net` column) |
| Material threshold | 0.3 | 0.3 | 0.2 (re-measured) |
| MIR date repair | Day/month swaps fixed from the MIR number's month | None (date stored as text) | Same repair as HRS |
| PO number shapes | 10-digit SAP plus a legacy short series written 5 ways | SAP plus legacy slashed form (`Eng/0007/2026-27`) | SAP; some cells name 2-3 POs joined by hyphens |
| Hyphen multi-PO cells | n/a | n/a | Split into separate POs (8+ digit parts only) |
| No-PO registry, 2-of-3, flags | Same | Same | Same |

- **HRS short series:** numbers like `11` or `1074` (under 8 digits) are not trusted as PO references for matching.
  - They can still confirm a match, but cannot contradict one or form a PO-number group.
  - For the "no PO" report they count as a PO when they name one we hold: 124 HRS receipts moved out of
    `no_po` (193 -> 69), 67 of them already matched.
  - Short numbers also collide across financial years; canonical form proposed: `HRS/HO/26-27/NNN`.
- **Legacy slashed numbers** fold on financial year + serial + a shared series name, only when both sides contain `/`.
- **Vapi hyphen cells:** naming either PO counts as a match; naming neither counts as a contradiction.

## Import differences

- **Currency first:** rate = net price x exchange rate (bare price if no exchange rate).
- Import value = net value x exchange rate (pre-duty), the same basis as MIR's pre-tax value.
- The landed figure (with customs duty) has its own check, the final-value comparison.
- Re-measured 2026-09-25 on Vapi: median value gap 8.8% this way vs 18.0% using the landed figure.
- **Quantity compared is `qty_as_per_boe`** (what customs cleared), not the ordered qty.
- PO qty vs BOE qty is a separate read-time flag (not part of MIR matching).
- **Partial delivery** = BOE qty below ordered qty; shown blue.
- Each import line carries one BOE number; several MIR receipts can still group under one import PO.
- Shipment stage: Placed -> Shipped (BL) -> Cleared (BOE), PO shows its least advanced line.
- Same 2-of-3 rule, gates, ranking, pins and flags as domestic.

## When matching runs

- Per plant, in order: sync PO CSV -> sync MIR -> sync RM stock -> **match** -> consumption.
- Imports: sync Imports PO CSV -> match again (one run covers domestic and import).
- Scheduled hourly, **09:00 to 20:00 IST** (cron `0 9-20 * * *`, 12 runs a day).
- **Nothing scheduled runs unless the `qcluster` worker is running.**
- Also runs straight after an inline edit that touches a matching field.
- Each run is one all-or-nothing transaction and records a sync log row.
- Manual: `match_hrs`, `match_achhad`, `match_vapi`.

## Known limitations

- No labelled ground truth: match accuracy has not been measured on a large enough sample.
- HRS short PO numbers still never group receipts or contradict a match; fix at source with the canonical form.
- Fabric ordered in ROLLS vs received in KG is compared raw (e.g. "9999.99%").
- Vapi's Madura orders are near-identically priced, so which order gets which receipt is weak there.
- The no-PO registry goes stale when a supplier starts getting POs; only rows citing a PO self-correct.
- Rows with a blank party name, or POs with a blank vendor (HRS 1074-1082), lose a vote.
- Achhad MIR dates are not repaired; HRS invoice and PO dates carry the same swap with nothing to fix them from.
- Every correction made in the app is overwritten by the next sync unless fixed in the source file.
