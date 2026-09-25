# Matching engine

The matching engine reconciles three things per plant: **PO line item ↔ MIR row** (domestic and
import POs, against one shared MIR table) and **MIR row ↔ RM Stock lot**. There is one
implementation, [matching_core.py](../apps/services/matching_core.py), driven by a per-plant
`_MatchConfig` built in [matching.py](../apps/services/matching.py) (HRS),
[matching_achhad.py](../apps/services/matching_achhad.py) and
[matching_vapi.py](../apps/services/matching_vapi.py). Each plant module exposes
`run_full_match()`, wired to `manage.py match_hrs` / `match_achhad` / `match_vapi`. It is
idempotent (`update_or_create` upserts plus explicit stale-row deletes) and safe to re-run any time;
it also runs synchronously after an "Edit Everywhere" save that touches a matching field (see
[api-and-features.md](api-and-features.md)).

Every match is a **suggestion, not a fact** - there is no labelled ground truth behind any
threshold here. See the Match accuracy section in [api-and-features.md](api-and-features.md) before
trusting a count.

## Overview: what `run_full_match()` does

1. **Load** active domestic and import line items (`purchase_order__is_active=True`), their
   per-PO item counts, and this plant's manual pins (`_load_pins()`).
2. **Build once per pass**: the IDF material scorer (`_material_scorer()`), the set of known PO
   numbers (`known_po_numbers()`), and the MIR candidate index (`_MirCandidateIndex`, which drops
   registered no-PO vendors unless the row names an order we hold).
3. **Collect** per line item: candidate MIR rows (vendor-gated, widened by PO number under 2-of-3),
   filtered by `_identification_pool()` (contradiction gate, date gate, 2-of-3 identification),
   each surviving pair carrying an evidence tier and a `_pair_weight()`. Also record the rate-based
   shipment group (`_shipment_group()`) and the rows citing this PO (`_po_number_group_rows()`).
4. **Settle in order**, all against one `claimed_mir_ids` set shared by domestic and import:
   manual pins first, then PO-number groups (one row per line by `_assign_pairs()`, extras to the
   best-matching line), then rate groups (a group that lost a member is demoted to per-row edges),
   then the optimal assignment `_assign_pairs()` over everything left.
5. **Write**: delete matches of retired orders; upsert or delete one `*POMirMatch` /
   `*ImportPOMirMatch` row per line with `_diffs_and_flag()`'s figures; rebuild every
   `group_entries` link in bulk (`_rebuild_group_links()`).
6. **MIR ↔ Stock**: delete matches of inactive MIR rows, then run `match_mir_entry_stock()` for
   every active MIR row against one shared `_StockLotPool`, and bulk-delete stale match rows.
7. **Return** counts (`po_line_items_matched`, `import_po_line_items_matched`,
   `mir_entries_stock_matched`), `manual_pins_applied`, `manual_pins_stale`, `manual_pins_unfilled`
   (pins whose MIR document had no free row, so the line was left unmatched) and `ran_at`.

**`run_full_match()` is one transaction** (`@transaction.atomic` on the function itself). The pass
deletes stale rows and upserts new ones across three match tables, so a failure part-way rolls the
whole pass back rather than leaving the plant half re-matched. Until 2026-09-24 the decorator had
drifted above a comment block and was decorating `line_item_ref()` instead;
`test_run_full_match_atomic.py` pins the rollback by failing the stock pool after the retired-PO
delete has already run.

---

### Vendor name is a hard gate on MIR↔Stock, one of three votes on PO↔MIR everywhere now

Two records for different vendors are never candidates for each other on MIR↔Stock, however well
material/qty/rate/value line up (Achhad excepted - its Stock sheet has no vendor column at all,
`stock_vendor_field=None`). Names are normalised by `parsers/common.py`'s
`normalize_vendor_for_matching()` (legal suffixes stripped, lowercased, punctuation stripped, then
the `VENDOR_ALIASES` fold) and compared by `_vendor_matches()`, which passes on any of three arms:

- **Exact equality at any length** - `"SRF Ltd"` and `"SRF Limited"` both normalise to `"srf"`, and
  a length floor alone once rejected every SRF delivery.
- **Containment, both sides at least 4 characters** - HRS's Stock sheet appends a city suffix its
  MIR/PO data doesn't carry (`"Rubamin Private Limited"` vs `"Rubamin Private Limited - Vadodara"`),
  and exact matching produced **zero** MIR↔Stock matches until switched to `shorter in longer`.
- **`SequenceMatcher` ratio ≥ `_VENDOR_SIMILARITY_THRESHOLD` (0.90)** - absorbs single-character
  MIR typos ("JMF Perfomance", "M.K.Markating"). 0.90 is fitted, not picked: the lowest true pair
  scores 0.909, the highest false pair ("Bp Chemicals" vs "LBG Chemicals") 0.857. It cannot go
  lower.

**All three plants run PO↔MIR identification as 2-of-3 now - Achhad and HRS since 2026-09-18, Vapi
since 2026-09-19.** `_MatchConfig.identification_two_of_three=True` in all three plant modules means
identification requires any **two of {PO number, vendor, material}** rather than vendor plus one of
the other two. Vendor keeps all of its weight for the rows that have nothing else (423 of Achhad's
663 MIR rows carry no PO number at all, and those still identify on vendor plus material) but it can
now be **outvoted** by a PO number that agrees with the material.

This became safe only because the project owner filled in the PO-number column of Achhad's MIR for
every vendor an order is raised against. Against that file the change was one-sided: **166 → 175
matched line items, zero lost, zero re-pointed**. Four of the gains are rows where the PO number is
right and the **party name is wrong** - MIR 96/05 books Barytes Powder (10000 @ 8.5 = 85000 on both
sides) against Sunjay International's order under the name "Prestige Industries"; MIR 122/08's party
column reads the literal placeholder `Seller / Consigner`; MIR 20/09 types "Kadr Metals" for "Kedar
Metals" (0.842, under the 0.90 threshold). Those matches are made **and flagged**:
`vendor_matched=False` surfaces as the `Vendor Name Mismatch in MIR` data-quality flag, because a
disagreeing name is always an error somebody should fix at source.

**HRS enabled the same flag the same day for a different reason** - `matching.py`'s own comment has
the numbers. Its MIR PO coverage is better than Achhad's (236 of 498 rows carry a usable PO
reference, 232 naming an order the master CSV holds), but **no HRS row is blocked by the vendor gate
at all**: every PO-confirmed row also passes `_vendor_matches()` against its own order's vendor, so
the 2-of-3 rule proper cannot fire and `candidates_for()`'s union widening adds nothing. What HRS
gets is the flag's *other* half - the narrowed no-PO-vendor exclusion below. Tinna Rubber is a
registered `NO_PO_SUPPLIER` that the master CSV now raises four real line items against (3000001081
×3, 3000001098), and those four MIR rows were being dropped before any gate ran. The flag also needs
a `vendor_matched` column on the match models (migrations `0049` for HRS/Achhad, `0052` for Vapi);
`_vendor_matched_field()` only passes the keyword when the flag is on, so enabling it on a model
without the column raises `FieldError`.

**Why 2-of-3 and not a weighted score**, which was considered: no set of weights with one threshold
can express this data. `vendor+material` **must** identify (it is the only evidence 423 rows have)
while `PO-number alone` **must not** - MIR 74/06 cites a mistyped number belonging to another
supplier's order for a completely different material, and PO-alone identification would bind it.
2-of-3 states that rule directly instead of hiding it in constants. Weight still decides *which*
identified candidate wins; that is what the evidence tiers do.

**Vapi enabled the same flag 2026-09-19**, once its `'PURCHASE ORDER'` MIR column's coverage was
measured rather than assumed blank: 29.0% of 1,489 MIR rows name an order the master CSV holds.
Measured with a real `run_full_match()` against a throwaway DB copy: **588 → 599 domestic matches,
strictly additive - 0 lost, 0 re-pointed.** 4 are the 2-of-3 rule proper (all on Madura Technical
Textiles' PO `1000001433`, whose receipts are written under `'MADURA INDL TEXTILES LTD'` /
`'MADURA TECHNICAL FABRICS LTD.'`), 7 are the narrowed no-PO-vendor exclusion (Tinna Rubber, Eternia
Trading). See `matching_vapi.py`'s own comment for the full measurement.

**Vapi's PO column also introduced a shape neither HRS nor Achhad has: one MIR row naming several
open orders at once**, joined by hyphens (`'1000001552-1000001630'`). See
[PO ↔ MIR](#po--mir) for how `_MULTI_PO_HYPHEN_RE` handles it and why the net effect on Vapi was a
loss of 11 matches that is expected, not a regression.

### Legacy slashed PO numbers drift between the two files

The 10-digit SAP numbers reconcile on their own; the legacy slashed form does not. Achhad's MIR
writes `Eng/0007/2026-27` where the master CSV writes `RTP2/HO/26-27/ENGG-0007` - the same order,
and five of them. `parsers/common.py`'s `legacy_po_matches()` folds that shape on **(fiscal year,
serial) plus a shared series name by prefix**, tried only after `_po_number_matches()`'s exact
token test, and only when **both** sides contain `/`. The series condition is the safety argument,
not decoration: Achhad's MIR carries both `0014/2026-27` (Triambakam Impex) and `14/26-27` (Polyols
& Polymers), which reduce to the identical `('26-27', 14)` and would otherwise both claim the same
order. Refusing those digits-only shapes costs nothing - they still identify on vendor plus material.

### Some vendors never have a PO - that is registered, not inferred

`parsers/common.py`'s **`NO_PO_VENDORS`** registry lists the vendors this company never raises a
purchase order against. `_MirCandidateIndex` drops their MIR rows from the PO↔MIR candidate pool at
build time - **except**, on a plant running `identification_two_of_three` (all three today), a row
whose own PO column names an order we actually hold (`_names_known_po()`). That exception exists
because the registry has already gone stale once and nothing said so: Achhad's master CSV now raises
real orders against JMF Performance Materials (1100000792/799/875) and Eternia Trading (3000001072),
and ten MIR rows carrying those PO numbers were being dropped before any gate ran, indistinguishable
from rows the matcher had simply failed on. Narrowing the exclusion is self-correcting; relying on
someone remembering to edit this list is not. Keeping a row does **not** make it match - it still
has to pass identification. Roughly **890 of ~2,460 active MIR rows** across the three plants fall
in this bucket.

**One list, shared by all three plants.** The first version was scoped per plant; the project
owner's list says these are no-PO everywhere, so per-plant scoping would only give three copies a way
to drift apart. Reintroduce scoping if a genuine per-plant exception appears, not before.

Two categories, kept separate because they have **opposite futures**:

- **`INTERNAL_TRANSFER`** - the company's own plants and sister units (Ravasco Vapi/Achhad,
  Hindustan Rubbers Silvassa/Achhad). An inter-plant movement is not a purchase and will never
  generate a PO. Permanent.
- **`NO_PO_SUPPLIER`** - real third-party suppliers bought from without a PO today (Gangamani,
  Eternia, Harsha Impex, K-Flex, 2M Elastomers, Gurvinder Singh HUF, Forech, Star Polymers, Sumitra,
  DS Industries, Tinna, JMF). A **process gap, not a fact about the data model**. If one starts
  being PO'd, remove its entry (the 2-of-3 exception only rescues rows that cite a PO number).

**The registry suppresses matching, never the row.** The rows stay in the MIR table, still
reconcile against Stock via `_StockLotPool` (an internal transfer really does land in stock), and
are counted and labelled by [no_po_vendors.py](../apps/services/no_po_vendors.py), surfaced as
`noPoVendors` on each plant's `sync-status`. A silent exclusion would recreate the exact confusion
the registry exists to end.

**This registry is about PO↔MIR only.** Its MIR↔Stock counterpart is a separate list - see
[What MIR↔Stock deliberately skips](#what-mirstock-deliberately-skips---two-registries-neither-shared-with-the-po-side).

**Lookup is exact normalized equality - deliberately NOT `_vendor_matches()`.** No containment, no
similarity arm, and normalised through `_normalize_vendor_for_matching_base()` - **without** the
`VENDOR_ALIASES` fold, so adding an alias can never widen what this registry excludes. A false
positive here removes a real supplier's receipts from reconciliation; a false negative merely leaves
a row unmatched. Containment makes the false positive easy with a short name (`"mit"` is a substring
of `"limited"`), and the similarity arm scores genuinely different companies as high as 0.857.

The cost is that **a genuinely different word needs its own line**. Normalization already folds
casing, punctuation, `&`/`and`, legal suffixes and trailing plurals (`"STAR POLYMER"`/`"STAR POLYMERS
INC."`, `"K-Flex"`/`"Kflex"`). Only three entries exist purely as spellings: `"Packaging"` vs
`"Packing"` for Ravasco, Vapi's `"... Pvt Ltd ACHHAD"` suffix, and Achhad's misspelled `"JMF
Perfomance"`. `test_no_po_vendors.py` pins each of them.

It is also a real false-positive guard on the matcher itself: `_vendor_matches()` scores
`'Ravasco Transmission And Packing Pvt Ltd ACHHAD'` against `'Ravasco Transmission & Packing Pvt
Ltd'` at **0.897** - 0.003 under the threshold is all that stops one plant's internal transfers from
being claimed by another plant's POs.

### PO ↔ MIR

Per line item, two separate passes: **identification** decides which MIR rows *can* be this line's
row; the **financial check** then measures the chosen pair. There is no score threshold that rejects
an identified candidate - "PO Not Found" means no candidate passed identification, and is stored as
the absence of a match row.

**Identification** (`_identification_pool()`), in order:

1. **Contradiction gate** - `_po_number_contradicts()`: the MIR row names a *different* order we
   hold (`known_po_numbers()`, active orders only, stored and annotation-stripped forms). Killed
   outright. An unrecognized or non-PO-shaped reference (`is_usable_po_reference()`) is treated as
   no evidence, never as evidence against. This closed swapped pairs (Achhad 1100000820 holding
   1100000785's receipt and vice versa).
2. **Date gate** - `_date_verdict()`: a receipt more than `date_grace_days` (7) before the order or
   more than `date_horizon_days` (270) after it is `DATE_IMPOSSIBLE` and killed - unless the PO
   number agrees, in which case the date is the suspect field and only its weight drops to 0. A
   receipt up to 7 days early weighs 0.6; after the order the weight decays linearly to a floor of
   0.15. A missing date is 0 (no information).
3. **2-of-3** of PO number (`_names_this_po()`), vendor (`_vendor_matches()`) and material
   (`_MaterialScorer.similarity()` ≥ `material_match_threshold`: 0.3 at HRS/Achhad, 0.2 at Vapi -
   see `matching_vapi.py` for why Vapi's is lower and was re-measured, not carried over).

**Ranking** among identified candidates is lexicographic, not blended (`_pair_weight()`):
evidence tier × 1000, then a within-tier weight of `0.55 × financial score + 0.20 × date weight +
0.25 × material score` (× 100, so never more than one tier step). Tiers: 4 PO number **and** vendor
agree, 3 PO number, 2 material with financial score ≥ 0.9, 1 material with ≥ 0.5, 0 material only. A
material-only candidate scoring 1.0 financially can never outrank a PO-confirmed one. Material
similarity is inside the within-tier weight because a multi-line order whose lines share a rate is
otherwise decided by nothing (Achhad PO 1000001471's three EE-080 widths were each shifted one place).

The **financial score** (`_score()`) is a weighted closeness over qty **0.29**, rate **0.29** and
value **0.42** (the old 2:2:3 ratio renormalised once material stopped being a scored factor),
each `_closeness()` decaying linearly to 0 at a 50% relative difference. A factor missing on either
side is excluded from the average rather than scored 0, and `field_coverage` records how much weight
was present. `MATCH_THRESHOLD = 0.55` still exists per plant and is served to the frontend as a
confidence signal, but it **does not gate** whether a match is created.

**Material similarity is IDF-weighted with explicit grade codes** (`_MaterialScorer`), because plain
Jaccard scored "ALUMINIUM TRIHYDRATE 4600N" against "...4200N" at 0.6. A grade code is a number of 2+
digits, or a number re-joined with a 1-2 letter suffix (`_grade_codes()` - `tokenize()` splits
"180P" into two tokens, and taking only the digits made "Aksil 180 G" and "Aksil 180 P" agree). A
shared code adds 0.25; codes on both sides sharing none multiply the score by 0.45.

**The value comparison must use pre-tax figures on both sides.** PO's `net_value` is compared with
`config.mir_value`: MIR's own `net` column at HRS and Achhad (the project owner's explicit PO Net
Value ↔ MIR Net mapping), `taxable_value` at Vapi, which has no `net` column. It is **not**
`invoice_final_value`/`total_amount`, both post-GST/TCS. Comparing pre-tax to post-tax produced a
bogus ~18% "value discrepancy" on line items that matched exactly on qty and rate - 18% being roughly
the GST rate, not a real discrepancy. MIR's taxable and final values are compared separately, as
data-mismatch checks, against the PO's `total_value` / `total_inclusive_value` - and **only for a
single-line PO**, because those CSV columns are a whole-PO total repeated on every row.

**PO Item Id is not a trustworthy join key** and the matcher never relies on it: one code
(`11287940`) is reused across three chemically unrelated materials from two vendors.

**PO-number matching is whole-token, not substring** (`_po_number_matches()`): both sides are split
on `, & / ;` and whitespace and the PO's tokens must appear as a contiguous run, so
`'HRS/HO/26-27/003'` no longer matches `'...0031'` but still matches `'HRS/HO/26-27/003 & 004'`. A
trailing `.0` from openpyxl float cells is stripped. `_names_this_po()` also tries the
annotation-stripped form of our own number (see [One PO, many receipts](#one-po-many-receipts---the-po-number-is-the-join-key-2026-09-24)).

**A MIR row can name more than one open order at once - Vapi only, `_MULTI_PO_HYPHEN_RE`
(2026-09-19).** Confirmed real, not a data-entry slip: Madura Industrial Textiles alone carries up to
21 concurrently open orders for the same fabric code (EE250), so one delivery is allocated across
several, written as `'1000001552-1000001630'`. 334 of Vapi's 1,489 MIR rows write this shape (never
wider than 3; 319 have every number recognized). `_po_tokens()` splits it into separate tokens -
guarded to digits-only segments of at least 8 digits (`is_usable_po_reference()`'s own floor), so it
never fires on a cell that merely *contains* a hyphen: HRS's/Achhad's legacy form uses one for its
fiscal-year segment (`'HRS/HO/26-27/003'`), and so do Vapi's own legacy forms (`'RTP1/HO/26-27/008'`,
`'DDO-0095/26-27'`) - both excluded by the slash/letters. Splitting lets `_po_number_matches()` treat
naming *either* order as positive evidence and `_po_number_contradicts()` treat a candidate naming
*neither* as contradicted, leaving ordinary scoring to decide which order wins.

**This one is not strictly additive, and that is expected.** Measured together with 2-of-3: Vapi's
domestic matches moved 588 → 577, a net loss of 11, with 295 re-pointings - almost all inside Madura
Industrial Textiles, whose ~100 concurrent orders are priced nearly identically (₹230-250 across most
EE-series codes), so qty/rate tie-breaking there was always weak. Most of what this removed was
already flagged `severity=material` and bound to a MIR row whose own PO column named a *specific,
different* order; most re-pointed items land on the row that names their own PO number. Shipped
without a labelled-ground-truth check - review a sample of Madura's re-pointed pairs through
`review.html` before trusting the new count over the old one. **Don't quote a single Vapi
before/after without saying which of the two 2026-09-19 changes it includes** - they move the same
count in opposite directions.

**Financial check** (`_diffs_and_flag()`), after units are normalised (`_uom_adjust()`): only qty and
rate produce a hard error (`qty_mismatched` / `rate_mismatched`; `is_flagged` means exactly "qty or
rate mismatched"). Everything else - unit family clash, net value, taxable value, final value, GST
type (`_tax_type_mismatch()`: IGST vs CGST+SGST) - folds into `data_mismatch`, with the three value
checks also stored individually (`net_value_mismatched` etc.). `qty_over_delivered` is three-state
(True / False / None when no qty comparison was possible). `severity` buckets the largest diff at
5%/20% (`rounding`/`minor`/`material`), matching `flags.js`'s `rowTintClass()`; a unit clash is
always `material`. Every `*_diff_pct` is clamped to `9999.99` (`_MAX_DIFF_PCT`) because the columns
are `max_digits=6`.

### One PO, many receipts - the PO number is the join key (2026-09-24)

Reported against HRS PO 3000001174 (Carbon Black N330, 200,000 KG): four MIR receipts each name it in
their PO column; the modal showed one MIR number (33/09) at "67.8% short". **67.8% was already the
four receipts summed** - `_shipment_group()` had grouped them - but `*POMirMatch` is one row per line
(`mir_entry` = the primary) and the group's membership was never saved. The other three read as
unmatched to everything: the modal, `mir_without_po`'s "PO on file, not yet matched", and the MIR
picker's `claimedBy`.

The project owner's rule, which the matcher now follows: *"one PO can match with multiple MIR number
based on PO numbers only."*

- **PO-number groups settle right after manual pins**, before rate groups and the optimal
  assignment. Every candidate that is `po_number_matched` and whose PO column names **exactly one**
  order we hold (`_cited_po_numbers()`, counted on the `clean_po_number()` form so an annotated
  order is not two) counts toward that order - **no rate tolerance and no 150% cap**. A disagreeing
  rate or quantity is flagged, never grounds to drop a delivery. Identification still applies
  (2-of-3), so a mistyped number from another supplier's order for another material cannot attach.
- **Several lines on one order share its receipts in two steps:** one receipt per line by
  `_assign_pairs()` first, then every extra receipt to the line it matches best (material matched,
  then material score, then pair weight, then line id). The first version skipped step one and left
  one line of each duplicate-line order empty (2 Achhad, 2 Vapi).
- **Vapi's hyphen cells (a row naming several orders) stay on the old per-row path** - one row
  cannot be held against all of them.
- **Every counted row is saved** in `*POMirMatch.group_entries` (M2M, migration `0059`, all six PO
  match models), rebuilt whole each run by `_rebuild_group_links()` - one delete and one bulk insert
  per table. Empty for a one-row match; readers fall back to `mir_entry`. The API serves them as
  `matchedMirs` (`_domestic_base._counted_mirs()`); `mir_without_po._matched_mir_ids()` and both
  `claimedBy` lookups (`_held_mir_numbers()`) read them. See [frontend.md](frontend.md) for
  `matchedMirsLabelHtml()`.
- **Groups also sum taxable and final value** (`_ShipmentGroup.taxable/final`). Comparing one
  delivery's taxable value against the whole order raised a false "Taxable Value Mismatch" on every
  grouped single-line PO. A member whose unit does not convert stays a member but marks the group
  `uom_clash`, so the check reports a unit mismatch rather than a fabricated percentage.
- **Deliberately NOT done: re-forming a rate group from its remaining rows** when it loses one.
  Measured: it cost Vapi 12 matched lines, because a rate group keys on vendor + material + rate
  alone and a rebuilt Madura EE-200 group at the flat Rs 230 swallowed VERBAL receipts of five widths
  that other lines held one to one. The case that prompted it (Silica 3000001085) is fixed by
  PO-number groups instead.

Measured against the local copy, full `run_full_match()` before and after, in a rolled-back
transaction:

| Plant | Lines matched | Lost | MIR receipts linked to a line | Lines with >1 receipt |
| --- | --- | --- | --- | --- |
| HRS | 179 -> 183 | 0 | 179 -> 302 | 45 |
| RTP-Achhad | 179 -> 181 | 0 | 179 -> 221 | 25 |
| RTP-Vapi | 603 -> 608 | 0 | 603 -> 796 | 66 |

Run time unchanged. Receipts whose PO column names exactly one order, and linked to it: HRS 235 of
235, Achhad 218 of 219, Vapi 407 of 440. The remainder is MIR naming the wrong order (Achhad MIR 74/06,
2M Elastomers against an APP order; 28 at Vapi, e.g. MIR109/05, Jayam's reclaim rubber against
Dycon's hydrocarbon resin PO). They stay in `mir_without_po`'s "PO on file, not yet matched".

**An annotated order is matched on its bare number too** (`_names_this_po()`, used by
identification, the candidate index and pins). The master CSV can write "1000001462 (Changed Purchase
Order)" while MIR writes 1000001462; a token match against the annotated form never fired, so 17 Vapi
receipts linked to nothing. `known_po_numbers()` already held both forms, so the contradiction gate
knew the receipt named one of our orders, just never which. `test_po_number_groups.py` pins the rule.

#### The PO modal reconciles in real figures (2026-09-24)

Project owner: *"show all the MIR's row matched and instead of delta in red or green show the real
values too"*. `frontend/js/po-reconcile.js` renders one card per line with an **Ordered / Received /
Difference** table and every matched receipt; the rendering details are in
[frontend.md](frontend.md). The parts that belong to the engine:

- **The received side is computed server-side**, by `matching_core.received_against_line()` - the
  matcher's own unit conversion, in the PO line's unit - and served as `received` plus per-row
  `matchedMirs` (`qtyInPoUnit` etc.). Summing in the browser would let a PO in MT and receipts in KG
  disagree with the flags beside them. A row whose unit cannot convert makes `qty`/`rate` null
  rather than a partial sum; a row with no value makes `value` null.
- **Imports compare what the matcher compares** - BOE quantity, net price x exchange rate, and net
  value x exchange rate. Both the card and the matcher compare value pre-duty; see
  [Import PO ↔ MIR](#import-po--mir-convert-currency-first) for why the landed figure is not used.
- **Each receipt shows its MIR Excel row** (`sheetRow`, the stored `source_row_ref`). It is a
  pointer for a human, never an identity - see [architecture.md](architecture.md#stable-lot-identity).

**Still open:** fabric ordered in ROLLS against MIR in Kgs is compared raw (PO 1077: "9999.99%"),
because `normalize_uom()` does not treat ROLLS vs KG as a known family clash.

### MIR ↔ Stock

Gates differently per plant, reflecting the real schema difference: HRS and Vapi gate on
**(material description, vendor)** - HRS via `HRSRMLot.party_name`, Vapi via
`RTPVapiRMLot.supplier_name`. Achhad gates on **material description alone** since its Stock sheet
has no vendor column - weaker and more false-positive-prone. This pairing is **many-to-many** by
design (one material arrives into several lots over time); there is no exclusive claiming, with one
exception: a lot identified on date + rate alone yields to a tier-1 describer (see the date + rate
section below).

**No plant uses stock quantity to identify.** HRS's `received` field (the sheet's "REC" column) reads
`0` for nearly every real lot. On pairs that are near-certainly the same delivery (descriptions and
dates identical), **rate agrees 97-100% and `received` qty agrees ~0%**. Rate is a usable identifier;
stock qty is not, and must not become one. (Once a pair is identified *and* its dates agree, qty and
value are still *checked* where `received` is nonzero - see `match_mir_entry_stock()` below.)

#### Three tiers, not one equality test (2026-09-21)

Until this date identification was `normalize_material()` on both sides compared letter for letter,
and coverage was **HRS 26.8%, Achhad 19.9%, Vapi 1.6%** - because the two files write the same
material differently: `'8MPA RECLAIM RUBBER'` vs `'RECLAIM RUBBER 8MPA'`, `'Precipitated Silica'` vs
`'PRECIPITATD SILICA'`.

Tried in order per (MIR row, lot), best evidence winning:

- **Tier 3 - identical names.** The original rule.
- **Tier 2 - close enough.** `_MaterialScorer` with **per-token edit distance** (`fuzzy_tokens=True`,
  `SequenceMatcher` ≥ 0.85 on tokens of 4+ characters), over a corpus of this pairing's own two
  vocabularies. This is where nearly all the gain is. **`fuzzy_tokens` defaults to `False` and PO↔MIR
  does not get it** - that pairing's figures were measured against exact tokens.
  `stock_material_threshold = 0.45` at all three plants, swept at 0.35/0.45/0.55/0.65; it is
  **separate from `material_match_threshold`** (PO↔MIR's 0.2/0.3) because a Stock sheet is a
  warehouse's shorthand, not order paperwork. Vapi's lowered 0.2 does **not** apply here.
- **Tier 1 - the descriptions agree on nothing, but the receipt date and the rate both do.** See
  below.

**Descriptions are cleaned first**, and at Vapi this is worth more than any scoring change:
`clean_mir_material_for_stock()` strips the SAP code Vapi's MIR prefixes (`'RM00011014 ZINC OXIDE'`,
173 of 1,489 rows) and `clean_stock_material()` strips a trailing plant tag (`'RECLAIM RUBBER 6MPA
HRS'`). Both are rare vocabulary, so IDF weighted them *most heavily*. Worth +27 matched rows on its
own, and why Vapi's exact-name matches go 24 → 71. **Deliberately outside `normalize_material()`** -
that output is a persisted join key (`MaterialCategoryReference.normalized_description`,
`stock_identity.lot_natural_key()`), same confinement as `tokenize()`'s letter/digit split.

#### Date + rate is this pairing's PO number - behind two guards

There is no shared key: MIR has **no item-code column**, HRS's `sap_item_code` has nothing to join
to, and `MaterialCategoryReference` resolves only 30-47% of lots and 5-32% of MIR rows. But rec-date
and rate are highly selective on their own. Average lots passing **one** signal, out of the whole
active stock table: **rec-date 0.6/0.5/0.3, rate-within-2% 2.4/2.7/1.5, vendor 4.2/-/1.2, category
6.5/2.5/1.9** (HRS/Achhad/Vapi). Rec-date is *more* selective than vendor.

So `stock_date_rate_path=True` admits a pair on identical date **and** rate within
`stock_rate_identity_tolerance_pct` (2%, unit-adjusted, `_rates_agree()`) when the descriptions
disagree - `'Kanatol-8A (DOA)'` ↔ `'DOA Oil'`, `'JC Magnesium Hydroxide'` ↔ `'JH Magnesium Hydroxide
MDH'`, `'RMP001105002 EVA BAG'` ↔ `'BATA BAG/EVA BAG 20"X26"X180G'`.

**Raw, this path is unsafe** - a vendor delivering several SKUs on one day at one price cross-matches
them. Real pairs it bound: `'NBR 2675'` ↔ `'NBR 3345'` (both ₹226.50), `'AUROBOND 825'` ↔ `'AUROAID AR
262'` (both ₹345, **both ways round**), `'Eva Bag 20"X20"'` ↔ `'24 x 36 Eva Bag'` (swapped), `'Nordel
4770'` ↔ `'Nordel 4570'`.

Two guards make it safe, both verified necessary:

- **`_grade_codes_contradict()`** - codes present on both sides and sharing none ⇒ reject. The RM
  analogue of `_po_number_contradicts()`. It removes every pair above and **costs nothing on the
  material paths** (the scorer's grade penalty already pushes a disagreement under threshold there);
  it is also applied to tier 2.
- **Tier-1 exclusivity** (`_StockLotPool.best_describer`) - a lot claimed on date+rate alone yields to
  the MIR row sharing that date that best describes it (ties on lowest id, so the winner is stable).
  This is what un-swaps the Eva Bags. **Deliberately tier-1 only**: a lot really does receive two
  MIR lines of the same material on one day (two invoices).

**Only the best evidence survives per MIR row, ties included** - `(tier, material score)`. Ties keep
the genuine many-to-many case (several lots of the same material at identical evidence). What it
removes is one MIR row spreading across several *different* materials each merely over threshold.
Measured on Achhad (no vendor gate, worst affected): **without it, 350 matched rows produce 713 match
rows (2.04 each) and same-date rate agreement drops to 88%; with it, same coverage, 405 rows (1.15
each), agreement back to 98%.**

Measured on live data, PO↔MIR unchanged throughout (179/179/577):

| Plant | MIR rows matched, before → after | Tier 3 / Tier 2 / Tier 1 | Stock lots reconciled |
| --- | --- | --- | --- |
| HRS | 135 → **269** (26.8% → 53.5%) | 135 / 133 / 2 | 69 of 210 |
| RTP-Achhad | 132 → **350** (19.9% → 52.7%) | 132 / 210 / 10 | 130 of 325 |
| RTP-Vapi | 24 → **304** (1.6% → 20.4%) | 71 / 230 / 3 | 68 of 168 |

(The plant modules' own comments record slightly different same-day figures - HRS 270, Achhad 352 -
from a run before the not-stocked registry landed. Re-measure before quoting either.)

**Read the per-plant split, not just the totals:**

- **HRS** gains almost entirely from Tier 2: it is the only plant with vendor, date, rate *and* a
  reliable category all populated. Of its 53 remaining reachable misses, most are the **vendor gate
  working** (Balaji Rubbers and GPC International both supply SBR). One real miss worth an alias:
  `'Singh Plasticisers And Resins (India)'` vs `'Singh Plasticisers & Resin (I) Pvt Ltd'`, 7 rows.
- **Achhad** gets the most out of Tier 1, because its Stock sheet has **no vendor column** - date and
  rate are the only independent evidence. It leaves **zero reachable rows behind**. Its constraint is
  data: only **211 of 325 lots (65%)** carry a receipt date, against HRS's 100% and Vapi's 95%, and
  Tier 1 cannot fire without one.
- **Vapi**'s gain is mostly the description cleaning. Its `category` column is **useless here** (17%
  agreement on known-good pairs vs HRS 95%, Achhad 80%) and is not wired in anywhere.

**Do not compare Vapi's 20.4% to the other two, or to PO↔MIR.** See below.

#### The RM Stock sheets do not hold everything MIR logs

The hard ceiling on this pairing is **not** the matcher. The RM sheets hold chemicals and raw rubber;
MIR logs everything that comes through the gate. **Conveyor belting, conveyor fabric (EE/NN/EP
series), rubber compound and MS crates appear in no plant's RM sheet** - 0 hits for
`belt`/`belting`/`fabric`/EE/NN codes across all three, while Vapi's MIR alone has 216 rows saying
"belt" and 117 saying "fabric".

That is **36% of HRS's active MIR rows, 47% of Achhad's, and 76% of Vapi's**. Against the rows whose
material is actually in the sheet, the three plants match **84% / 100% / 85%** - the fair comparison
to PO↔MIR's ~90%. These rows are not lost: they still reconcile against their purchase orders.

**Open question for the plants, not a code fix:** whether conveyor fabric and belting are
deliberately outside RM stock tracking, or tracked in a separate file this app does not sync. If such
a file exists it is a new pipeline, not something to force into MIR↔Stock.

### What MIR↔Stock deliberately skips - two registries, neither shared with the PO side

`parsers/common.py`'s **`NO_RM_STOCK_VENDORS`** is the MIR↔Stock counterpart of `NO_PO_VENDORS` and
answers a different question: not "no purchase order exists" but "these goods are not tracked in the
RM Stock sheet". A vendor can be in either list, both, or neither - **Madura is properly PO'd** (the
single biggest source of PO↔MIR matches at Vapi) and never appears in a stock file; Tinna Rubber is
the mirror image. **Don't merge them.**

Measured 2026-09-21 across all three plants:

| Plant | MIR rows from Madura | Share of that plant's MIR | Value | RM stock lots |
| --- | --- | --- | --- | --- |
| RTP-Vapi | 703 | 47.2% | ₹26.24 cr | **0** |
| HRS | 112 | 22.3% | ₹4.07 cr | **0** |
| RTP-Achhad | 15 | 2.3% | ₹0.39 cr | **0** |

830 rows and ₹30.7 crore against **zero** stock lots anywhere, deliveries 2026-04-01 → 2026-09-18.
Vapi's Madura rows are 703 of the 1,133 MIR rows that plant has no stock counterpart for - **62% of
its MIR↔Stock gap**.

Same rule as `NO_PO_VENDORS`: **suppress the match, never the row.** `match_mir_entry_stock()` checks
this registry first, then `NOT_STOCKED_MATERIALS`, and exits through `_no_matches()`.
[rm_untracked.py](../apps/services/rm_untracked.py) counts and labels both, surfaced as
**`rmUntracked`** (its `byVendor` half) on each plant's `sync-status`. Lookup is exact normalized
equality (base normalisation, no aliases), same reasoning as `no_po_vendor_entry()` - so each of the
four real Madura spellings needs its own line, and `test_mir_stock_identification.py` pins every one.

#### `NOT_STOCKED_MATERIALS` - the material-keyed half

The project owner's scope list, 2026-09-21. `parsers/common.py`'s **`NOT_STOCKED_MATERIALS`** lists
classes booked inward and never stocked, matched by regex against the raw description (first match
wins). Same rule: suppress the match, never the row.

**Every entry passed two tests against live data, and the patterns are worded the way they are
because of what failed.** Re-run both before adding anything:

- **A. No stock lot at any plant matches the pattern.** If a lot exists, the class *is* tracked and
  an unmatched row is a **matching** gap - excluding it would hide the thing worth fixing.
- **B. No currently-matched MIR row matches it**, i.e. it destroys no existing reconciliation.

**Scoped per plant** (`NOT_STOCKED_MATERIALS` is a dict keyed on `_MatchConfig.plant_key`:
`"hrs"`/`"achhad"`/`"vapi"`), unlike the two vendor registries: **what a plant stocks is a fact
about that plant's warehouse**, whereas whether a vendor is PO'd is a fact about the company. Four
classes are shared; spares/services is per plant (with or without grease), and printing is absent at
Achhad:

| | HRS | RTP-Achhad | RTP-Vapi |
| --- | --- | --- | --- |
| Conveyor fabric | excluded | excluded | excluded |
| Conveyor belting | excluded | excluded | excluded |
| Rubber compound (bare phrase) | excluded | excluded | excluded |
| Crates | excluded | excluded | excluded |
| **Grease** (inside spares) | **stocked** - not excluded | absent - excluded | **stocked** - not excluded |
| **Printing / labels / logo** | absent - excluded | **stocked** - not excluded | absent - excluded |

The evidence: `'GREASE EP 1'` is a real lot at HRS and Vapi and absent from Achhad; `'Lamor Logo
160mm X 70Mic (12290)'` is a real lot at Achhad and absent from the other two.

**An unknown plant key excludes nothing**, deliberately - a new plant must state its own scope.
`not_stocked_material_entry()` takes `plant_key` as a required argument with no shared default; an
omitted plant silently falling back to another plant's scope is the failure this split prevents.

Live counts (**1,304 rows, ₹80.1 cr**): HRS 118, Achhad 213, Vapi 973.

**Four things were rejected or narrowed, all by real data:**

- **"Printing / labels / logo work" - excluded at HRS and Vapi, never at Achhad (fails A there).**
  Achhad's Stock sheet carries `'Lamor Logo 160mm X 70Mic (12290)'`, and its MIR's 9 `'Lamor Logo
  Print'` rows are that same product. **It is blocked twice over**, worth knowing before anyone
  "fixes" it by widening a threshold:
  - *The scorer scores it 0.243 against 0.45.* The two naming tokens (`lamor`, `logo`) are shared,
    but the lot also carries `160 mm x 70 mic 12290` - six tokens of dimensions and code the MIR row
    has no reason to repeat. Symmetric weighted Jaccard counts them all in the denominator, and IDF
    weights the rare ones heaviest (`12290` alone scores 7.2). **One side being more specific is
    penalised as if it disagreed.** An asymmetric containment score fixes this pair and was measured:
    it costs Achhad's precision badly (same-date rate agreement 58% → 30%), so it is not the answer.
  - *The date+rate path cannot fire either*, because that lot has **`received_date = None`** - one of
    Achhad's 114 undated lots. Rate is an exact ₹100.00 on all nine rows; simulating a date matches
    it instantly (verified in a rolled-back transaction).

  So this is **a data gap, not a matcher bug**: filling Achhad's receipt-date column recovers it for
  free.
- **"Grease" - dropped from the spares class (fails A twice).** HRS and Vapi hold `'GREASE EP 1'`.
- **Rubber compound - narrowed to the bare phrase.** Achhad stocks *named* compounds (`'Rubber
  Compound-EAR 11560'`) and HRS stocks `'SILSHEET RUBBER'`. A blanket `/rubber comp|silsheet/`
  **destroyed 43 real matches**. The anchored pattern matches only `'RUBBER COMPOUND'` with an
  optional unit suffix, plus `'COMPOUNDED RUBBER'`.
- **Packing - narrowed from bags/drums/wooden to CRATES only.** All three stock EVA/LD/BATA bags; HRS
  stocks `'WOODEN STOPPER 12"'`. Only MS crates are untracked.

**One loss got past both tests and is the reason to re-run the whole matcher, not just the two
checks.** The belting pattern was first a bare `\bbelts?\b`, which caught Achhad's `'Rubber Compound
Cushion Belts'` - a stocked compound naming a belt only as its application. Test B missed it because
that row happened to be unmatched at the time. It surfaced by **re-running `run_full_match()` with
the registry disabled and diffing the matched set** (Achhad 350 → 349). Requiring
`conveyor`/`belting`/`transmission belt` closes it. Do that diff for any new entry;
`test_mir_stock_identification.py` pins this case by name.

Net effect: **zero matches lost at all three plants**. What it buys is the same thing
`purchasesWithoutPo` buys on the PO side: an out-of-scope row stops reading as a matcher failure.

### "No purchase order behind it" is three questions, not one (2026-09-21)

Project owner: *"we have orders without a PO in MIR, which might be true or waiting for a PO to be
matched with them - can we show info about them too?"* That sentence is the design.

`purchasesWithoutPo` (`no_po_vendors.purchases_without_po_summary()`) is a count; a receipt with no
order behind it is three situations wearing one number, each owned by a different person:

| Bucket | What it means | Whose fix |
| --- | --- | --- |
| `no_po` | MIR names no order at all - blank, or a sentinel like `VERBAL`/`NIL` | Purchasing. Nothing is pending. |
| `po_unknown` | MIR names an order **we have never received** | Upstream - the PO master CSV generator. The one genuinely "waiting for a PO". |
| `po_known_unmatched` | MIR names an order **we do hold** and the matcher has not linked it | Ours - a qty/description/vendor-spelling drift. |

Counts on the local copy of the data, 2026-09-25, after short legacy PO numbers began counting as
POs (HRS / Achhad / Vapi): `no_po` **69 / 37 / 247**, `po_unknown` **4 / 19 / 134**,
`po_known_unmatched` **57 / 1 / 101**. HRS's `no_po` was 193 before that change.

[mir_without_po.py](../apps/services/mir_without_po.py) classifies; `_domestic_base.make_mir_without_po()`
serves `GET /api/[<plant>/]mir-without-po` (rows + summary; `?bucket=` narrows, `?download=csv`
downloads); the panel and badges are in [frontend.md](frontend.md). Five things are load-bearing:

- **The bucket is decided by the MATCHER's own PO-number comparison** (`known_po_numbers()` /
  `_po_number_matches()`, through `cites_a_po()` and `names_a_held_po()`), not a string compare
  against the PO table. **"Has a PO" is PO-shaped OR names an order we hold** (`cites_a_po()`):
  HRS's legacy series is 1-4 digits (`1074`, read from MIR as `1074.0`), under
  `is_usable_po_reference()`'s 8-digit floor, and on 2026-09-25 that put 124 of HRS's 193 `no_po`
  rows there although each named one of our own orders (67 already matched to it). The floor still
  guards the contradiction gate, where a short number colliding across financial years would veto a
  real match; for a report, "cites PO 1074, which we hold" risks nothing. HRS writes its legacy series five
  ways (see [Legacy slashed PO numbers](#legacy-slashed-po-numbers-drift-between-the-two-files)), so
  plain equality put **~120 HRS rows** in `po_unknown` that the matcher considers known. Measured:
  121 → 4 at HRS once the matcher's own test is used.
- **`?format=csv` does not work and must not be reintroduced.** `format` is reserved by DRF's content
  negotiation, which **404s** on an unknown renderer. It is `?download=csv`.
- **A `no_po` row the matcher matched anyway stays in the list**, flagged `matched` (identification
  needs no PO number). It is still a purchase made without an order. It is also what makes this
  module's `no_po` total reconcile **exactly** with `purchases_without_po_summary()`'s badge, which
  takes the plant's `known_po_numbers()` and applies the same `cites_a_po()`;
  `test_mir_without_po.py` pins the two together, including a short legacy number.
- **Two badges, never one combined number.** "How many did we buy without an order" is a figure
  somebody is driving down; a matching backlog is not purchasing's work.
- **An unknown `?bucket=` is a 400, not an empty list.** An empty list reads as "nothing to fix".

`INTERNAL_TRANSFER` parties are excluded from every bucket and reported separately as
`internalTransfer`.

**`sync-status` serves its copy of the counts from a 60-second per-plant cache**
(`_domestic_base._cached_mir_without_po_summary()`). Measured 135/53/163 ms (HRS/Achhad/Vapi) against a
`/sync-status` that otherwise answers in ~20-30 ms, on the endpoint the freshness watcher polls every
60 s. The cost is `_names_known_po()` scanning every known PO number per row - the same shape as
`_po_number_contradicts()`, and **not** something to work around by re-deriving a faster second idea
of what a known PO number is. What is cached is plant-derived data, not a response, and the
permission check still runs per request - this is not the
[`cache_page` trap](auth-security-email.md#non-negotiables). `/mir-without-po` itself is uncached.

#### Reported as one thing, stored as two

`rm_untracked.rm_untracked_summary()` returns both MIR↔Stock halves - `byClass` and `byVendor`, plus
`total` and `value` - on each plant's `sync-status` as **`rmUntracked`**. One summary because to a
reader they are one thing ("receipts this pairing isn't expected to reconcile"); two registries
because they **go stale for different reasons** - a vendor entry when that vendor's goods start being
stocked, a class when a plant starts stocking that class.

**A row counted under a vendor is never also counted under a class**, since the matcher checks the
vendor registry first - otherwise `total` would disagree with the rows actually excluded.

The dashboard renders it as **"N not tracked in RM"**, only under Raw Material Analysis (see
[frontend.md](frontend.md); the view-tab handler calls `loadSyncStatus()` so the badge appears on
switching). Live counts: HRS 118 rows (₹4.23 cr), Achhad 212 (₹14.17 cr), Vapi 973 (₹61.74 cr).

### Units are normalised before comparing - on both pairings

`_uom_adjust()` runs before every qty/rate comparison - `_score_components()` and `_diffs_and_flag()`
on PO↔MIR, `_rates_agree()` and the financial checks in `match_mir_entry_stock()` on MIR↔Stock. Same
family (KG vs MT) converts to the family's base unit, rate inverted; an unrecognised unit on either
side passes through unconverted (guessing is worse); two recognised but different families return
`None`s and `uom_mismatch=True`. This was once missing on the MIR↔Stock side: MIR in MT against a lot
in KG reported a ~1000x "rate mismatch" that was purely a unit artifact.

Under a clash, qty/rate diffs are recorded as `None` and `uom_mismatch` is set - **not** a nonsense
percentage (migration `0044` added the field to all three `*MirStockMatch` models). On PO↔MIR the
financial score counts a clashing qty/rate as an explicit 0 (still a real signal), unlike missing
data, which is excluded. Value is a currency amount and stays unit-uncompared. It surfaces as
`uomMismatch` in `_domestic_base.py`'s `mirStockMatches`.

**It is rendered as its own amber "units differ" badge (2026-09-23), not as "matched".** A unit clash
leaves `is_flagged=False` (nothing was compared, so nothing mismatched), and `mirStockMatchHtml()`
used to fall through to the green `matched` badge. See [frontend.md](frontend.md).

### Flag thresholds

`FLAG_DIFF_PCT = 0` in all three plant modules, mirrored by `FLAG_PCT` in `flags.js` - **zero
tolerance**, a deliberate policy choice (the project owner asked for none, down to 1 kg in 1000 kg).
Comparisons use strict `>`, so an exact match never flags. (`5.00` was used earlier; picked, not
measured, and superseded.)

If this policy changes, change it in one place per side: `FLAG_PCT` in `flags.js` (KPI cards, row
flags, line-item badges, Raw Material Analysis cards via `computeMaterialPoLinkage()`) and
`FLAG_DIFF_PCT` in each of the three `matching*.py` files (stored `is_flagged` on both pairings; keep
all three identical). Also re-word `DISCREPANCY_LEGEND`'s two critical entries, which say "no
tolerance".

`VALUE_FLAG_EPSILON = Decimal("1.00")` is a ₹1.00 **absolute** tolerance on the value checks (net,
taxable, final), defined in each plant module and passed as `_MatchConfig.value_flag_epsilon`;
`arithmetic_checks.py` uses the same figure. Rounding from multiplying and summing already-rounded
currency fields is not a data-quality problem.

Other thresholds that are deliberately **not** zero, because they are identification tests, not
discrepancy reports: `_SHIPMENT_RATE_TOLERANCE_PCT` (2%, rate groups), `stock_rate_identity_tolerance_pct`
(2%, MIR↔Stock date+rate path). A pair admitted by either is still checked at zero tolerance.

### Import PO ↔ MIR: convert currency first

Import line items are priced in the PO's own currency (every real Vapi import PO today is USD); MIR's
`rate`/`taxable_value`/`net` are always INR. The first version compared them raw and scored every
real pair near zero - a ~94x gap.

`matching_core._import_rate_value_inr()` (one copy, shared by all three plants) converts before
scoring and diffing: rate = `net_price × exchange_rate` (bare `net_price` when `exchange_rate` is
null - 2 of 37 real rows); value = `net_value × exchange_rate` (bare `net_value` without a rate).
**Value is pre-duty, like every other value comparison.** MIR's value (`net` / `taxable_value`)
excludes customs duty, while `total_inclusive_value` is the landed figure including it. The matcher
used the landed figure until 2026-09-25, on an early measurement that it sat closer to MIR; re-measured
on Vapi's 25 matched pairs it did not (median gap 18.0% landed against 8.8% net x rate), and it read
Vapi 1000001569 "15% short" on a line whose qty matched. Because value also breaks ties between
candidates, the switch moved 7 of 26 Vapi pairings, all checked: both SBR 1502 lines now take the MIR
row whose rate and value agree exactly, two Chloroprene lines move from a MIR 20% off on rate to one
within 1-3%, and the one line left unmatched had been holding a receipt that belongs to another
order. The landed figure keeps its own comparison, the final-value check. Vapi went from 0/37 to 25/37 import line items matched. With
`import_extended_fields=True` (all three plants now), imports also get the tax-type, taxable-value
(`_import_total_value_inr()`, single-line POs only) and final-value checks, and the extended columns
are written.

**Which import quantity is compared:** `qty_as_per_boe`, not `qty_as_per_po`. The Bill of Entry
quantity is what customs recorded as clearing - the equivalent of what MIR logs as received.
`qty_as_per_po` is the ordered amount and can legitimately differ; that PO-vs-BOE check is a separate
read-time flag in [import_flags.py](../apps/services/import_flags.py).

No separate MIR↔Stock table exists for imports: `*MirStockMatch` is keyed on `mir_entry` alone, so it
already covers the Stock leg for both.

### Performance: the Raw Material Analysis render was quadratic too

Reported 2026-09-21 as the Raw Material tab "gone slower". **The endpoint was never the problem** -
`/materials` answers in 33-51 ms over 7 queries. The cost was client-side: `materials.js`'s
`computeMaterialPoLinkage()` calls `materialLinksToItem()` once per **(material x PO line item)**
pair - 703 × 1,112 = **781,736 calls per render** on "All Plants" over ~1,800 distinct strings.
Memoizing the per-string work took it from ~2,100 ms to ~90 ms with byte-identical results; the caches
are cleared at the top of every pass on purpose. Full detail in [frontend.md](frontend.md).

**Why it belongs here:** the 2026-09-21 matching release made it noticeable - MIR↔Stock match rows
went 342 → 1,053 (x3.1), so each lot carries more `mirStockMatches`, and the consumption step made
the freshness watcher's data stamp advance more often. **If you add anything to the Raw Material
render path, check it is not per-pair.**

### Open orders with no stock lot get a row of their own (2026-09-24)

Raw Material Analysis was built one row per Stock lot, so **386 of 478 open line items linked to no
lot** and were invisible. `materials.js`'s `orderOnlyMaterials()` now gives every unlinked open line a
row of its own; "open" is judged per line (`isOpenPoLine()`). Details in [frontend.md](frontend.md).

Two causes were measured and deliberately **not** changed on the linkage side: wording drift (~23
lines, e.g. `AUROAID AR 262` vs `AUROAID AR262`) and the vendor gate (~16). **Dropping the gate binds
a generic "Reclaim Rubber" order to six different grades and "Carbon Black N220" to "Carbon Black
134".** Those lines show on an order-only row instead.

### Open import orders count too, and the list reads latest first (2026-09-24)

Raw Material Analysis read domestic orders only, so every open import order was missing (HRS PO
3000001141, 201,600 KG of Kumho SBR, among them). `materials.js`'s `materialOrders()` now appends
import orders reshaped by `importPoAsMaterialOrder()`, **converted to INR exactly as
`matching_core._import_rate_value_inr()` does** - left raw, a USD line counts at ~1/90th. **It does
not join a generic order to a grade**: Kumho's "Synthetic Rubber SBR" is kept off the SBR 1502 row by
the vendor gate and gets an order-only row. The list sort, modal columns and
`test_raw_material_order_payload.py` are described in [frontend.md](frontend.md).

### Performance: API responses are compressed, except where that is a security risk

`config/middleware.py`'s `SelectiveGZipMiddleware` (2026-09-23) compresses API JSON 91-94% (Vapi's
`/purchase-orders` 787 KB → 51 KB) except under `/api/auth/` and `/admin/`, where a secret sits next
to reflected input (BREACH). **If you add an endpoint that returns a token or OTP in its body, put it
under `/api/auth/` or add its prefix to `UNCOMPRESSED_PATH_PREFIXES`.** Not a matching concern; the
full rationale lives in [auth-security-email.md](auth-security-email.md) and
[architecture.md](architecture.md).

### Performance: the engine was quadratic

Two full-table SELECTs used to sit inside per-row loops - `_candidate_mir_entries()` loaded the whole
MIR table once per PO line item, and `match_mir_entry_stock()` the whole Stock table once per active
MIR row - plus a stale-row DELETE once per MIR row. On HRS, the *smallest* plant (120 domestic line
items, 474 active MIR rows, 195 lots):

```
before:  2,035 queries,  9.36 s
after:     972 queries,  0.81 s
```

The shape mattered more than the numbers: cost grew with the **product** of two growing tables, in a
pipeline that runs 12× a day across 3 plants.

Fixed with `_MirCandidateIndex` and `_StockLotPool` (fetch once per pass, memoize the vendor gate per
distinct normalized vendor, pre-normalize each row's vendor once) threaded through
`run_full_match()`, plus a batched cleanup (`kept_ids`). **Pure caching - no scoring, gating or
ordering logic changed**, verified by diffing every match row before and after: byte-identical. Both
caches preserve queryset row order (tie-breaking can depend on it), hand out copies rather than the
cached list, and live for **one pass only** - never module-level, or they would outlive a sync. The
single-item entry points build a throwaway index/pool, which is the one query they always issued.

`test_matching_query_scaling.py` deliberately does **not** assert a magic query count - it runs the
pipeline at two dataset sizes and asserts the count barely moves, so a reintroduced N+1 fails by
construction.

### `_assign_pairs()` needs both its termination guards

The optimal-assignment step is max-weight bipartite matching by successive maximum-gain augmenting
paths, found by SPFA (queue-driven Bellman-Ford) over a graph whose displacement edges carry negative
weight. Where a **positive-gain cycle** exists it has two independent ways to never terminate, and
both were live until 2026-09-18, when `match_vapi` stopped returning:

1. **The relaxation loop re-queues forever.** Bounded by `dequeue_budget = 64 + 8 × len(adjacency)`
   (total work per source).
2. **The path flip then walks a cyclic path forever.** `came_from_left`/`came_from_right` describe a
   path only if the search ran to completion; a search that stops early can leave them describing a
   cycle. The flip walked it *while mutating*, leaving the assignment half-applied. The path is now
   collected and validated **before** anything is written, and a cyclic one abandons that source
   rather than corrupting `match_left`/`match_right` into disagreement.

Fixing only the first moves the hang from one loop to the other. **Keep both.**

**The graph size was never the cause**, and assuming it was cost two wrong fixes. Vapi's graph was
**869 candidate pairs across 278 line items, a median of 2 each**; 233 of 234 source searches used
**79 dequeues between them**, and exactly one ran away. A per-node re-queue bound of V is too loose -
V² × 234 sources is still hundreds of millions of iterations.

Vapi hit it because so much of its MIR matches on material alone, producing sets of same-vendor,
same-material candidates at near-identical weight - exactly the ties that create cycles. It went
critical when Vapi's order book roughly doubled (131 → 222 POs) in one sync.

Other properties worth knowing: weights must be positive (`_pair_weight()` guarantees it), which lets
augmentation stop at the first non-improving path, so a line is left unmatched rather than forced
onto a row worth more elsewhere; a path may end on a free row **or** on a displaced holder that stays
unmatched; and the source order is deterministic (`-max weight, str(key)`) so identical data gives an
identical assignment. `TestAssignPairsTerminates` in `test_matching.py` pins termination with graphs
that hang the unguarded code.

### A shipment group that loses its rows must fall back, not lose its match

`run_full_match()` settles multi-shipment groups **before** the optimal assignment, because a grouped
edge stands for several MIR rows at once and plain bipartite matching cannot express that. A rate
group (`_shipment_group()`: 2+ identified rows within 2% of the line's rate, summed qty not over 150%
of ordered) whose members are already claimed has to give up - but until 2026-09-18 the line item was
then **dropped from the assignment entirely**, because `ungrouped_edges` was built from `key not in
groups_by_key`, leaving it unmatched with free rows in its own pool.

Grouping is an optimization on **how** an item takes its rows, not a claim that single-row matching
is wrong for it. A losing group is now **demoted** - it rejoins the ordinary assignment with its
per-row edges (`row_edges`, kept for every item) and competes for what is still free. It is **not**
re-formed from its remaining rows (see
[One PO, many receipts](#one-po-many-receipts---the-po-number-is-the-join-key-2026-09-24)).

Measured: **HRS 170 → 180 line items (75.6% → 80.0%)**, Achhad 178 → 179, Vapi +4. HRS PO
`3000001046` was the clearest case - one line item, two unclaimed MIR rows both naming that PO, same
vendor, identical material, matched instantly by `match_po_mir_line_item()` alone and left unmatched
by the full run.

### Two hot paths in matching are cached or short-circuited for a reason

`_po_number_contradicts()` scans **every known PO number** for **every candidate** of **every line
item**, so anything it calls is on a cubic-ish path. Profiled on one Vapi run: `_po_number_matches`
at 851,590 calls and `_po_tokens` at 1,691,328 - **12.5 s of a 15.6 s run**.

- `_po_tokens` is `lru_cache`d (8192). It is a pure function of a short string from a corpus of well
  under 2,000 values, and returns a **tuple** so a cached value cannot be mutated by one caller and
  handed back corrupted to the next. `_cited_po_numbers()` and `_vendor_similarity()` are cached for
  the same reason.
- `_po_number_matches` skips the `legacy_po_matches()` fallback unless **both** sides contain `/`.
  Without that guard it ran 844,956 times in one Vapi run, none of which could match.

`_names_known_po()` has the same shape and is why `sync-status` caches `mir_without_po`'s summary.

### The five core helpers live once, in `matching_core.py`

`_closeness()`, `_diff_pct()`, `_token_overlap()`, `_vendor_matches()`, `_po_number_matches()` - and
everything else in the algorithm - are defined **exactly once**, in `matching_core.py`, and all three
plants run that copy through their `_MatchConfig`. (They were once byte-for-byte copies per plant;
the Match Accuracy Programme's Phase 0 consolidated them - see `test_matching.py`'s module docstring.)
**Do not re-introduce per-plant copies** - a plant difference belongs in a `_MatchConfig` field.

**`matching_core.py` was deliberately NOT split into smaller files in the 2026-09-23 modularity
pass.** It was feasible (low test coupling), but this is the module every matching investigation and
memory note names functions in *by file*, it is one algorithm read top to bottom, and the gain would
have been navigation only, in the code with the least tolerance for risk.

Dismissing a match, by contrast, is identical per plant, so it lives once in `match_dismiss.py`, and
every matcher's `update_or_create` defaults deliberately never carry `dismissed_*`, so a dismissal
survives every re-match **while the line still points at the same MIR row**. Every PO↔MIR write goes
through `_save_po_mir_match()`, which clears the dismissal when a run re-points the line to a
different receipt - a dismissal judged one pairing, and keeping it would hide the new receipt's
flags, which nobody has looked at (see [api-and-features.md](api-and-features.md)).

---

## File reference

### apps/services/matching_core.py

The whole algorithm: identification, ranking, grouping, assignment, financial checks, persistence and
MIR↔Stock, parameterised by `_MatchConfig`. It imports no models - every model class, the manual-pin
model and the plant's `SyncRun.Plant` value arrive through the config, which is also what lets tests
build a config by hand. Several docstrings inside it describe superseded states (see the notes
below); trust the code.

**Configuration**

- `_MatchConfig` (frozen dataclass) - model classes (`po_item_model`, `import_item_model`,
  `mir_model`, three match models, `stock_lot_model`); `match_threshold`, `flag_diff_pct`,
  `value_flag_epsilon`; financial weights; `material_match_threshold`; `mir_value` /
  `mir_taxable_value` / `mir_final_value` accessors; `plant_key`; `manual_match_model` /
  `syncrun_plant` (empty means no pins); `stock_rate_field`, `stock_vendor_field` (None = no vendor
  gate), `stock_code_field` / `stock_uom_field` (display only, for `review_views.py`);
  `stock_material_threshold` (0 = equality only), `stock_date_rate_path`,
  `stock_rate_identity_tolerance_pct`; `import_extended_fields`, `stock_extended_fields` (write the
  extended columns - a model without them raises `FieldError`); `date_grace_days` (7) /
  `date_horizon_days` (270); `identification_two_of_three`. Its inline comments still say Vapi keeps
  2-of-3 off and that the extended-field flags are HRS-only; all three plants set all three True.
- Constants: `TIER_PO_NUMBER` / `TIER_MATERIAL` (stored `tier` label), `_MAX_DIFF_PCT` (9999.99),
  `TIER_RANK_*` (4..0), `_STRONG_FINANCIAL_SCORE` 0.9 / `_OK_FINANCIAL_SCORE` 0.5, `_TIER_STEP` 1000,
  `DATE_IMPOSSIBLE` (-1), `_SEVERITY_MINOR_PCT` 5 / `_SEVERITY_MATERIAL_PCT` 20,
  `_SHIPMENT_RATE_TOLERANCE_PCT` 2, `_SHIPMENT_GROUP_MAX_OVERSHOOT_PCT` 50,
  `_VENDOR_SIMILARITY_THRESHOLD` 0.90, `PIN_KIND_BY_MATCH_KIND` (`po`→`domestic`, `import`→`import`).

**Primitive helpers (pure)**

- `_closeness(a, b)` - 1.0 at equality, linear to 0 at 50% relative difference; None if a side is
  missing.
- `_diff_pct(a, b)` - % difference of b relative to a, None if a side is missing, clamped to
  `_MAX_DIFF_PCT`; a == 0 with b ≠ 0 returns the clamp.
- `_token_overlap(a, b)` - plain Jaccard over `tokenize()`; now only the fallback when no scorer is
  passed.
- `_vendor_similarity(a, b)` (cached) and `_vendor_matches(a, b)` - the three-arm vendor test
  (exact, containment ≥ 4 chars, ratio ≥ 0.90). Callers pass already-normalised names.
- `_po_tokens(value)` (cached, returns a tuple) - upper-cased, hyphen cells split by
  `_MULTI_PO_HYPHEN_RE`, split on `_PO_TOKEN_SPLIT`, `.0` suffix stripped.
- `_po_number_matches(po_number, raw)` - contiguous whole-token run, then the `/`-guarded
  `legacy_po_matches()` fallback.
- `_names_this_po(po_number, raw)` - `_po_number_matches` on the stored number or its
  `clean_po_number()` form.
- `_names_known_po(raw, known_pos)` - raw is PO-shaped and names any known number. Used by the
  candidate index.
- `cites_a_po(raw, known_pos)` / `names_a_held_po(raw, known_pos)` - reporting only, never matching:
  "has a PO" is PO-shaped or names a held order; the second is the shape-free "names a held order".
  Used by `mir_without_po.py` and `no_po_vendors.purchases_without_po_summary()`.
- `_grade_codes(tokens)` - number + short suffix, or 2+ digit number.
- `_grade_codes_contradict(a, b)` - both non-empty, disjoint.

**Material scoring**

- `_MaterialScorer(corpus, fuzzy_tokens=False)` - smoothed IDF over a corpus; `similarity(a, b)` is
  weighted Jaccard, plus (fuzzy only) greedy heaviest-first near-token credit at the mean of the two
  weights, then the grade bonus (+0.25, capped at 1) or penalty (×0.45). `_near()` memoizes per
  instance.
- `_material_scorer(config)` - builds the PO↔MIR scorer over active MIR + active domestic and import
  PO descriptions, once per pass.
- `_material_similarity()` / `_material_matches()` - number and yes/no against
  `material_match_threshold`.

**Candidate pools**

- `_MirCandidateIndex(config, known_pos=None)` - active MIR rows with a party name, minus no-PO
  vendors (kept if the row names a known PO under 2-of-3), with pre-normalised vendors.
  `candidates_for(vendor, po_number)` returns the vendor-gated set (memoised per vendor), unioned in
  row order with rows naming this PO under 2-of-3. Returns a copy.
- `_candidate_mir_entries(config, vendor, index=None, po_number="")` - thin wrapper; builds a
  throwaway index when none is passed.
- `_StockLotPool(config)` - active lots with a description (and vendor, where gated), pre-computed
  cleaned descriptions, normalised materials, grade codes, a fuzzy `_MaterialScorer` over lots +
  cleaned MIR descriptions (only when `stock_material_threshold > 0`), and `best_describer` (lot id →
  best same-date MIR id, only with `stock_date_rate_path`).
- `known_po_numbers(config)` - frozenset of active domestic and import PO numbers, raw and cleaned.

**Gates and ranking**

- `_po_number_contradicts(po_number, raw, known)` - raw is PO-shaped, doesn't name this order, and
  names another known one.
- `_date_verdict(config, po_date, mir_date)` - 0 (unknown), `DATE_IMPOSSIBLE`, 0.6 (early within
  grace) or linear decay to 0.15.
- `_identification_pool(...)` - applies the gates and 2-of-3 (or vendor + one), scores each
  survivor, assigns a tier, returns `(pool, {mir_id: _Candidate})`. Its docstring's opening
  ("Vendor is already satisfied") describes the non-2-of-3 path.
- `_Candidate` (NamedTuple) - mir, tier_rank, score, coverage, date_weight, material_matched,
  po_number_matched, material_score, vendor_matched.
- `_pair_weight(candidate)` - `tier × 1000 + (0.55 score + 0.20 date + 0.25 material) × 100`.
- `_best_by_evidence(found, entries)` / `_best_candidate(config, pool, item)` - highest pair weight;
  highest financial score (the latter is legacy, unused by the pipeline).

**Scoring and units**

- `_Matchable` (NamedTuple) - description, qty, uom, rate, value, and optional tax_type /
  total_value / total_inclusive_value for the data-mismatch checks.
- `_po_matchable(line, is_single_item_po)` / `_import_matchable(config, line, is_single_item_po)` -
  build the above; totals only for single-line POs; imports use `qty_as_per_boe` and INR figures.
- `_uom_adjust(qty_a, uom_a, qty_b, uom_b, rate_a, rate_b)` - see
  [Units](#units-are-normalised-before-comparing---on-both-pairings).
- `_score_components()` / `_score()` - weighted financial closeness and `field_coverage`.
- `_import_rate_value_inr(line)` / `_import_total_value_inr(total, fx)` - currency conversion; value
  is `net_value x exchange_rate`, pre-duty.
- `_save_po_mir_match(model, line, previous_mir_id, defaults)` - every PO↔MIR `update_or_create`;
  clears `dismissed_*` when the line's MIR row changed. `run_full_match()` reads every line's previous
  MIR in one query per model; the single-item paths pass `_LOOK_UP`.
- `received_against_line(config, po_uom, mir_rows)` - public; the modal's "Received" totals in the
  PO line's unit, None on any non-convertible row.

**Financial check**

- `_diffs_and_flag(config, item, mir, *, group=None, ...overrides)` - returns the 16-tuple (qty/rate/
  value diffs, is_flagged, uom_mismatch, severity, qty/rate mismatched, data_mismatch,
  tax_type_mismatch, taxable/final diffs, three value-mismatch booleans, qty_over_delivered). A group
  supplies qty/rate/value and summed taxable/final; a `uom_clash` group reports a unit mismatch. (Its
  docstring's closing paragraph still says taxable/final always compare against the single row;
  since 2026-09-24 a group's sums are used.)
- `_tax_type_mismatch(tax_type, mir)` - IGST vs CGST+SGST structure; reads `igst` or Vapi's
  `igst_amt`; never a mismatch when either side has nothing.
- `_severity(uom_mismatch, *diffs)` - rounding / minor / material / None.
- `_vendor_matched_field(config, vendor_matched)` - `{"vendor_matched": ...}` only under 2-of-3.

**Shipment groups**

- `_ShipmentGroup` (NamedTuple) - entries, qty, rate (value-weighted), value, taxable, final,
  uom_clash, by_po_number.
- `_aggregate_rows(config, item, rows, *, by_po_number)` - sums in the line's unit; a non-convertible
  member stays in and sets `uom_clash`.
- `_cited_po_numbers(raw, known_pos)` (cached) - distinct cleaned known orders a row names.
- `_po_number_group_rows(found, known_pos)` - PO-confirmed candidates citing exactly one order.
- `_shipment_group(config, item, pool)` - the rate group (2% tolerance, 150% cap) or None.
- `_pick_match(...)` - single-item path: cited rows first (on a multi-line PO only those whose
  material also matches), else rate group, else best by evidence.

**Assignment**

- `_assign_pairs(edges)` - `{left: [(right, weight)]}` → `{left: right}`; see the termination section.

**Manual pins** (the design is in [api-and-features.md](api-and-features.md#editing-which-mir-a-po-line-matched-2026-09-21))

- `line_item_ref(position)` - the string position.
- `line_item_positions(po_items)` - `{item id: (po_number, item_ref, description)}` numbered per PO in
  pk order; imported by `_domestic_base.py` and `imports_views.py` so API and matcher agree.
- `_load_pins(config, positions_by_kind)` - resolves pins for both kinds in one pass, newest decision
  first, and splits off stale pins (description at that position changed). Returns
  `(ordered pins, stale)`.
- `_forced_candidate(...)` - a pinned pair's `_Candidate`, skipping identification but measuring
  everything; an impossible date is zeroed, not a veto; ranked at tier 4.
- A pin whose MIR document has no free row (a newer pin took it, or the number is gone from MIR)
  leaves its line unmatched - never an automatic fallback - and is reported in
  `manual_pins_unfilled`, not counted in `manual_pins_applied`.

**Persistence and entry points**

- `match_po_mir_line_item(config, line)` / `match_import_po_mir_line_item(config, line)` - single-item,
  no cross-item exclusivity (used by tests and callers that need one line); upsert or delete, set
  `group_entries`. They do not consult pins.
- `match_mir_entry_stock(config, mir_entry, pool=None, kept_ids=None)` - registries first, then the
  three-tier identification with vendor gate where configured, best-evidence filter, then per-lot
  financial checks when `stock_extended_fields`: rate, and (if `received` is nonzero) qty and derived
  value (`received × rate` vs `mir_value`), all **only when the dates are equal** - comparing against
  a lot received months apart measured price drift (~97-98% of old rate flags had no date match).
  Every early exit goes through `_no_matches()`, which deletes the entry's rows on the standalone
  path. Its long docstring still describes material equality as the sole identification factor; the
  inline tier comments are current.
- `_rates_agree(config, mir, lot)` - unit-adjusted rate within the identity tolerance; missing or
  clashing is "no".
- `_rebuild_group_links(model, links)` - wipes and bulk-inserts one match model's `group_entries`
  through table.
- `run_full_match(config)` - the pipeline in [Overview](#overview-what-run_full_match-does). Its
  docstring still describes greedy claiming by score; the code uses pins → PO-number groups → rate
  groups → `_assign_pairs()`.

### apps/services/matching.py

HRS's `MATCH_CONFIG` plus the four re-exported entry points (`run_full_match`,
`match_po_mir_line_item`, `match_import_po_mir_line_item`, `match_mir_entry_stock`), which is how
`match_hrs`, `_domestic_base.py`, `imports_views.py` and `review_views.py` (via `MATCH_CONFIG`) reach
the engine. Values: `MATCH_THRESHOLD` 0.55, `FLAG_DIFF_PCT` 0, `VALUE_FLAG_EPSILON` 1.00, weights
0.29/0.29/0.42, `MATERIAL_MATCH_THRESHOLD` 0.3, `mir_value = mir.net`, `plant_key="hrs"`, lot
`basic_rate` / `party_name` / `sap_item_code` / `uom`, stock threshold 0.45, date+rate path on, all
extended flags and 2-of-3 on. The module docstring's "falls back to `net` when `taxable_value` is
blank" is stale - it compares `net` only.

### apps/services/matching_achhad.py

Achhad's config: same numbers as HRS except `plant_key="achhad"`, lot `rate` field, **no stock vendor
field** (material-only MIR↔Stock gate), `stock_code_field="sap_code"`, no stock UOM column. Its
comment on `import_extended_fields` ("Vapi excluded for now") is stale.

### apps/services/matching_vapi.py

Vapi's config: `MATERIAL_MATCH_THRESHOLD` **0.2** (re-measured 2026-09-19: raising it to 0.3 cost 7
import matches), `mir_value = mir.taxable_value` (Vapi's MIR has no `net`), lot `basic_rate` /
`supplier_name`, no item-code column, `plant_key="vapi"`. The comments carry the 2-of-3 measurement
(583 / 588 / 599) and the MIR↔Stock denominator argument (304 of 356 in-scope rows, 85%).

### apps/services/stock_identity.py

Dependency-free lot identity used by the stock syncs (not by the matcher directly).
`lot_natural_key(code, description, vendor, location="", occurrence=0)` returns
`<code or normalize_material(description)>|<normalize_vendor(vendor)>`, with `#N` for a genuine
duplicate; `""` when nothing identifies the row (callers must skip it); `location` is accepted but
never keyed. `OccurrenceCounter.key_for()` numbers duplicates in sheet order within one sync run. This
key is why `normalize_material()` must not change: its output is persisted. It is **not** the
consumption key (see [consumption.md](consumption.md)). Rationale:
[architecture.md](architecture.md#stable-lot-identity).

### apps/services/no_po_vendors.py

DB layer over `NO_PO_VENDORS`. `no_po_vendor_summary(mir_model)` - one grouped query by `party_name`,
classified in Python; returns `total`, `internalTransfer`, `noPoSupplier` and a `vendors` list,
served as `noPoVendors`. `purchases_without_po_summary(mir_model, known_pos=frozenset())` - a
different question: every active non-internal-transfer row with no PO behind it
(`matching_core.cites_a_po(raw, known_pos)` False; the router passes the plant's
`known_po_numbers()`), registered or not (`registered` /
`unregistered`), per vendor and `byMonth` via `TruncMonth`; served as `purchasesWithoutPo` and the
source of the "purchased without a PO" badge that `mir_without_po`'s `no_po` bucket must equal.

### apps/services/mir_without_po.py

The three-bucket drill-down. `classify_mir_row(raw, party, matched, known_pos)` - pure rule:
internal transfer → None; `cites_a_po()` False → `no_po` (matched or not); matched → None; else
`po_known_unmatched` if `names_a_held_po()`, else `po_unknown`. `_matched_mir_ids(config)` - primary
`mir_entry` **and** `group_entries` of both domestic and import match models.
`mir_without_po_rows(mir_model, config)` - one pass, newest first within `BUCKET_ORDER`; value from
`net`, `taxable_value` or `total_amount`, whichever the model has (`_value_fields()`).
`mir_without_po_summary(..., rows=None)` - per-bucket counts, value, vendors, `openTotal` and
`internalTransfer`. Bucket ids are wire strings (`no_po`, `po_unknown`, `po_known_unmatched`).

### apps/services/rm_untracked.py

`rm_untracked_summary(mir_model, plant_key)` - `byVendor` (`NO_RM_STOCK_VENDORS`, rows and taxable
value) and `byClass` (`NOT_STOCKED_MATERIALS` for this plant, grouped by description + party so the
regex runs per distinct description), a vendor-excluded row never counted again under a class, plus
`total` and `value`. Served as `rmUntracked`. There is no separate `no_rm_stock_vendors.py`.

### apps/services/import_flags.py

Read-time, dependency-free import PO derivations used by `imports_views.py`, independent of MIR
matching: `shipment_stage()` / `po_shipment_stage()` (Placed → Shipped (BL) → Cleared (BOE), PO
takes the least advanced), `qty_discrepancy()` / `po_has_qty_discrepancy()` (PO vs BOE; missing BOE
qty is not a discrepancy), `delivery_date_status()` / `po_delivery_date_status()` (cleared is always
Delivered; callers must pass `timezone.localdate()`), `partial_delivery()` (BOE < ordered only), and
data-quality flags F1-F7 (`item_flags()`, plus F3 in `po_flags()` for mixed landed-cost completeness
within one BOE). Its flag `code`s feed Import flag dismissal keys (`<code>:<item_id>`). The module
docstring's "no MIR-equivalent data source for imports yet" predates import↔MIR matching; BOE↔MIR is
done by `match_import_po_mir_line_item()`.

### apps/core/management/commands/match_hrs.py

`manage.py match_hrs`: calls `matching.run_full_match()`, prints the three counts and elapsed time,
and always writes a `SyncRun` (`source=MATCH`, `rows_seen = rows_changed =` the sum of the three
counts, `error_detail` on failure), exiting 1 on failure. The `SyncRun` exists because matching used
to fail silently while every sync badge stayed green. Its docstring's "Tier-1 vs Tier-2 weighted
score" summary is stale.

### apps/core/management/commands/match_achhad.py

Same shape for Achhad (`SyncRun.Plant.RTP_ACHHAD`). Its docstring's "deliberately duplicated rather
than parameterized" is stale - the logic is shared in `matching_core.py`.

### apps/core/management/commands/match_vapi.py

Same shape for Vapi (`SyncRun.Plant.RTP_VAPI`). Its docstring's "po_number_raw 100% blank, no usable
Tier-1 shortcut" is stale - the column has been populated since 2026-09-11.
