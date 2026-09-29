# API layer and dashboard features

Every HTTP endpoint under `/api/` except the auth flow, the server-side rules behind each dashboard
feature, and the match-accuracy programme. The domestic plants share one implementation
(`_domestic_base.py`, parametrised by a `_PlantConfig` per plant); Imports, Review and Reports are
cross-plant routers with the plant as a path segment or not at all. The JavaScript that renders each
feature is named here but documented in [frontend.md](frontend.md); matching itself is in
[matching-engine.md](matching-engine.md), consumption / Days-Left in [consumption.md](consumption.md),
roles, plant scoping, sessions and email in [auth-security-email.md](auth-security-email.md).

## Endpoints

All paths are under `/api/`. "Auth" means the project default (`IsAuthenticated`, any role). "Plant"
means the view also calls `user_can_access_plant()` (reads) or `user_can_edit_plant()` (writes).
Domestic endpoints exist three times: HRS has no prefix, RTP-Achhad is under `achhad/`, RTP-Vapi under
`vapi/`, written below as `[<p>/]`. Every `/api/` response defaults to `Cache-Control: no-store`
(`ApiNoStoreMiddleware`, see [architecture.md](architecture.md)).

**Auth-flow endpoints are documented in [auth-security-email.md](auth-security-email.md):**
`auth/login`, `auth/token/refresh`, `auth/token/verify`, `auth/me`, `auth/change-password/request|confirm`,
and everything in `device_urls.py` / `device_views.py` and `google_oauth_urls.py` / `google_oauth_views.py`
(`password_views.py` too). `health` and `health/ready` are in [testing-deployment.md](testing-deployment.md).

### Domestic, per plant (`hrs_views` / `achhad_views` / `vapi_views` over `_domestic_base`)

| Method | Path | View | Permission | Purpose |
| --- | --- | --- | --- | --- |
| GET | `[<p>/]purchase-orders` | `purchase_orders` | Auth + plant (403) | Every active PO with line items, match fields, corrections, flag dismissals, data-quality flags |
| PATCH | `[<p>/]purchase-orders/<po>/fields` | `correct_field` | IsEditor + plant | Inline correction of one PO or line-item field, audit row, re-match if the field feeds matching |
| GET | `[<p>/]purchase-orders/<po>/mir-candidates` | `mir_candidates` | Auth + plant (403) | MIR documents a line could be given, each with who holds it (`claimedBy`); with `itemRef`, grouped with `why` |
| PATCH | `[<p>/]purchase-orders/<po>/mir-match` | `set_mir_match` | IsEditor + plant | One manual change to a line (add / remove a receipt, not received, back to automatic, undo, or the older pin); re-match queued on the worker |
| POST | `[<p>/]purchase-orders/<po>/mir-match/preview` | `preview_mir_match` | IsEditor + plant | Queue a dry run of that change; returns `previewId` |
| GET | `[<p>/]mir-match-previews/<id>` | `preview_mir_match_status` | IsEditor + plant | The preview's state and the lines it moves |
| GET | `[<p>/]purchase-orders/<po>/manual-changes` | `manual_changes` | Auth + plant (403) | The order's manual receipt decisions, who and when, plus its receipts placed on other orders |
| PATCH | `[<p>/]purchase-orders/<po>/flags/dismiss` | `dismiss_flag` | IsEditor + plant | Dismiss / reinstate a PO-level flag (`FlagDismissal`) |
| GET | `[<p>/]materials` | `materials` | Auth + plant (403) | Every active Stock lot with consumption, MSL, MIR↔Stock matches, flags |
| PATCH | `[<p>/]materials/<lot_id>/fields` | `correct_material_field` | IsEditor + plant | Inline correction of one lot field |
| GET | `[<p>/]materials/<lot_id>/stock-trend` | `stock_trend` | Auth + plant (403) | One lot's snapshot history |
| GET | `[<p>/]stock-snapshots/dates` | `stock_snapshot_dates` | Auth + plant (403) | Distinct snapshot dates with lot counts (no frontend caller today) |
| GET | `[<p>/]stock-snapshots?date=` | `stock_snapshots_for_date` | Auth + plant (403) | Whole plant's stock on one date; 404 with nearest dates when absent (no frontend caller today) |
| GET | `[<p>/]stock-snapshots/export?from=&to=` | `export_stock_snapshots` | IsEditor + plant | Streaming CSV of the full snapshot history |
| GET | `[<p>/]mir-without-po?bucket=&download=csv` | `mir_without_po` | Auth + plant (403) | Receipts with no order behind them, bucketed; CSV with `?download=csv` |
| GET | `[<p>/]sync-status` | `sync_status` | Auth + plant (403) | Latest `SyncRun` per source plus registry counts, retired-PO count, snapshot gap |
| POST | `[<p>/]sync-trigger` | `sync_trigger` | IsAdmin, `SyncTriggerThrottle` | Queue the plant's Drive sync pipeline; 202 or 409 `already_running` |
| PATCH | `[<p>/]matches/po-mir/<id>/dismiss` | `dismiss_po_mir_match` | IsEditor + plant | Dismiss / reinstate a PO↔MIR match |
| PATCH | `[<p>/]matches/mir-stock/<id>/dismiss` | `dismiss_mir_stock_match` | IsEditor + plant | Dismiss / reinstate a MIR↔Stock match |

### Imports, cross-plant (`imports_views`)

| Method | Path | View | Permission | Purpose |
| --- | --- | --- | --- | --- |
| GET | `imports/purchase-orders` | `purchase_orders` | Auth, plants silently narrowed | All three plants' active import POs, newest first |
| GET | `imports/purchase-orders/<plant>/<po>` | `purchase_order_detail` | Auth + plant (404) | One PO in detail shape (corrections, flag dismissals, vendor fields) |
| PATCH | `imports/purchase-orders/<plant>/<po>/fields` | `correct_field` | IsEditor + plant (403) | Inline correction, `ImportPOCorrection` audit row, re-match if relevant |
| GET | `imports/purchase-orders/<plant>/<po>/mir-candidates` | `mir_candidates` | Auth + plant (404) | Picker candidates; `claimedBy` lists domestic AND import holders |
| PATCH | `imports/purchase-orders/<plant>/<po>/mir-match` | `set_mir_match` | IsEditor + plant (403) | One manual change to an import line (`po_kind=import`) |
| POST | `imports/purchase-orders/<plant>/<po>/mir-match/preview` | `preview_mir_match` | IsEditor + plant (403) | Queue a dry run of that change |
| GET | `imports/mir-match-previews/<plant>/<id>` | `preview_mir_match_status` | IsEditor + plant (403) | The preview's state and result |
| GET | `imports/purchase-orders/<plant>/<po>/manual-changes` | `manual_changes_view` | Auth + plant (404) | The import order's manual receipt decisions |
| PATCH | `imports/purchase-orders/<plant>/<po>/flags/dismiss` | `dismiss_flag` | IsEditor + plant | Dismiss an `import_flags.py` flag (`<code>:<item_id>` key) |
| PATCH | `imports/matches/po-mir/<plant>/<id>/dismiss` | `dismiss_import_po_mir_match` | IsEditor + plant | Dismiss / reinstate an import PO↔MIR match |
| GET | `imports/sync-status` | `sync_status` | Auth, plants silently narrowed | Latest `IMPORT_PO_CSV` run per plant, plus `rodtepInProgress` / `advanceLicenseInProgress` |
| POST | `imports/sync-trigger/<plant>` | `sync_trigger` | IsAdmin, `SyncTriggerThrottle` | Queue that plant's import CSV sync + match |
| GET | `imports/track-bl?bl=` | `track_bl` | Auth | Live SafeCube container-tracking passthrough; 502 on any failure |
| GET | `imports/rodtep` | `rodtep_ledger` | Auth | RoDTEP scrip ledger joined to import citations |
| POST | `imports/rodtep/sync-trigger` | `rodtep_sync_trigger` | IsAdmin | Run `sync_rodtep` synchronously; 200 or 409 |
| GET | `imports/rodtep/<script_no>` | `rodtep_script_detail` | Auth | Export side, import side and legacy usage rows for one scrip |
| GET | `imports/advance-license` | `advance_license_ledger` | Auth | Advance Licence ledger with utilisation, validity and BOE cross-check |
| POST | `imports/advance-license/sync-trigger` | `advance_license_sync_trigger` | IsAdmin | Run `sync_advance_license` synchronously; 200 or 409 |

### MIR entry, cross-plant (`mir_views`)

| Method | Path | View | Permission | Purpose |
| --- | --- | --- | --- | --- |
| GET | `mir/meta` | `meta` | Auth | Plants with `canRead` / `canReceive`, reason codes, tax types, GST slabs, today |
| GET | `mir/open-pos?q=` | `open_pos` | IsEditor, every plant on purpose | Active POs with an open line whose number, vendor name or GSTIN matches |
| GET | `mir/purchase-orders/<id>` | `purchase_order` | IsEditor, every plant on purpose | A normalized PO with each line's received-so-far, open quantity and whether it can take a receipt |
| GET | `mir/vendors?q=` | `vendors` | IsEditor | Vendor picker for a PO that names no vendor |
| POST | `mir/preview` | `preview` | IsEditor + receiving plant (403) | Check and price the form; saves nothing |
| POST | `mir/entries/new` | `post_entry` | IsEditor + receiving plant (403) | Save a MIR; 201, or 400 with `errors: [{field, message}]` |
| GET | `mir/entries?plant=&status=&q=&from=&to=` | `entries` | Auth, readable plants only | The MIR register, newest first |
| GET | `mir/entries/<id>` | `entry` | Auth, readable plant (404) | One MIR with lines and mismatches |
| POST | `mir/entries/<id>/cancel` | `cancel_entry` | IsEditor + MIR's plant (403) | Cancel with a reason |
| GET | `mir/mismatches?status=&plant=` | `mismatches` | Auth, readable plants only | Mismatches, `OPEN` by default |
| POST | `mir/mismatches/<id>/resolve` | `resolve` | IsEditor + MIR's or PO's plant (403) | Resolve with a note |
| POST | `mir/po-lines/<id>/close` / `reopen` / `review` | `close_line` / `reopen_line` / `review_line` | IsEditor + PO's plant (403) | Short-close a line, reopen it, or clear a `needs_review` flag |

### Review, admin, reports

| Method | Path | View | Permission | Purpose |
| --- | --- | --- | --- | --- |
| GET | `review/next` | `review_views.next_review` | Auth, draws only from readable plants | Up to 5 random unreviewed matches, round-robin across groups |
| POST | `review` | `review_views.submit_review` | Auth + plant (403) | Record a `MatchReview` verdict; 201 |
| DELETE | `review/<id>` | `review_views.undo_review` | Auth, own rows only (404 otherwise) | Undo: delete the caller's own verdict |
| GET | `review/stats` | `review_views.review_stats` | Auth, cross-plant on purpose | Precision / recall / F1 report from `match_accuracy.build_report()` |
| GET | `review/stats/export` | `review_views.export_review_stats` | Auth, cross-plant on purpose | Same tables as CSV via `SafeCsvWriter` |
| GET | `auth/users` | `users_views.list_users` | IsAdmin | All users with `correctionsCount` |
| POST | `auth/users/create` | `users_views.create_user` | IsAdmin, `AdminWriteThrottle` | Create a user; 409 on duplicate email |
| PATCH / DELETE | `auth/users/<id>` | `users_views.update_user` | IsAdmin, `AdminWriteThrottle`; DELETE also needs `DELETE_USER_ALLOWED_EMAIL` | Update fields / reset password, or permanently delete |
| GET | `auth/users/<id>/devices` | `users_views.list_user_devices` | IsAdmin | The account's trusted devices |
| DELETE | `auth/users/<id>/devices/<device_id>` | `users_views.revoke_user_device` | IsAdmin | Revoke one trusted device |
| POST | `auth/users/<id>/logout-everywhere` | `users_views.admin_logout_everywhere` | IsAdmin, `AdminWriteThrottle` | `revoke_all_sessions()` for another account |
| GET | `sort-presets?view=<view>` | `preferences_views.presets` | Auth (any role), own rows only | The caller's saved sort presets for a view (`materials`, `material_lots`, `material_open_pos`, `purchase_orders`, `import_purchases`, `po_lines`, `po_receipts`, `search_po`, `search_po_items`); 400 on an unknown view |
| POST | `sort-presets` | `preferences_views.presets` | Auth (any role) | Save `{view, name, levels}`; 201 new, 200 when it saves over the caller's preset of the same name (case-insensitive) |
| PATCH / DELETE | `sort-presets/<id>` | `preferences_views.preset` | Auth (any role), own rows only (404 otherwise) | Rename / replace levels, or delete |
| GET | `auth/admin-overview` | `admin_overview_views.admin_overview` | IsAdmin | Top correctors, top vendors, recent corrections |
| GET / POST | `internal/send-daily-report` | `reports_views.trigger_daily_report` | AllowAny + `REPORT_CRON_SECRET` | Daily RM consumption emails; 502 `partial` if a plant failed |
| GET / POST | `internal/send-monthly-report?year=&month=` | `reports_views.trigger_monthly_report` | AllowAny + secret | Monthly consumption emails; 502 `partial` if a plant failed |
| GET / POST | `internal/send-mismatch-report?test_recipient=` | `reports_views.trigger_mismatch_report` | AllowAny + secret | Plant Data Correction emails (gated by `MISMATCH_REPORT_PLANT_HEADS_ENABLED`); always 200 |
| GET / POST | `internal/prune-revoked-tokens` | `reports_views.trigger_prune_revoked_tokens` | AllowAny + secret | Delete expired `RevokedRefreshToken` rows; safe for validating the secret |
| GET / POST | `internal/send-advance-license-expiry-report` | `reports_views.trigger_advance_license_expiry_report` | AllowAny + secret | Advance Licence import/export validity alerts; always 200 |

How an out-of-scope plant is refused differs by endpoint shape on purpose (domestic reads 403, the
combined import list narrows silently, import single-PO reads 404); the reasoning lives in
[auth-security-email.md](auth-security-email.md). Note the import **write** endpoints (`correct_field`,
`set_mir_match`) answer 403, not 404: only the import reads hide the PO's existence.

### Endpoint conventions worth knowing before adding one

- **Gate both role and plant.** `apps/api/tests/test_endpoint_permission_guard.py` fails on a write
  endpoint without `@permission_classes`, or a plant-handling endpoint that never calls a scoping helper.
- **Every CSV export goes through `SafeCsvWriter`** (formula-injection escaping by construction). See
  [Data export](#data-export).
- **Never name a query parameter `format`.** DRF reserves it for content negotiation and 404s on an
  unknown renderer; that is why the no-PO CSV is `?download=csv`.
- **Body flags go through `_request_bool()`** (`_domestic_base.py`), never bare `bool(...)`, because
  `bool("false")` is `True`. A JSON boolean is used as-is; a string reads by meaning (`"false"`, `"0"`,
  `"no"`, `"off"`, `""` are false). `dismissed` defaults to `true` when omitted. The pin endpoints'
  `clear` still uses `bool(...)`; the frontend sends real JSON booleans there.
- **Re-matching runs on the background worker, never in the request** (2026-09-25). A correction to
  a matching-relevant field, and every pin change, saves at once and calls
  `apps/services/rematch.py`'s `request_rematch(plant)`, which queues one `run_rematch()` per plant
  on the qcluster; the response carries `rematch` (its `status()`). It used to run
  `run_full_match()` inside the request, which scales with the data and runs on one CPU thread:
  Vapi took 24.8 s on production against gunicorn's 30 s kill, and past it the pin had committed while
  the re-match rolled back. A bigger Render plan would only have postponed that. The page shows
  "Saved - re-matching" and follows `sync-status`'s `rematch` block (`shared.js`'s
  `waitForRematch()`) until the run finishes, then reloads; pins it could not apply come back in
  that block's `unfilledPins`. Under pytest the run is inline, so tests see its result.
- **`timezone.localdate()`, never `date.today()`** for anything date-relative (delivery status, licence
  countdowns, snapshot gap). Render runs UTC; `TIME_ZONE` is IST.

---

## Match accuracy and review

### Match accuracy: manual validation is required, not optional

There is no automated precision/recall measurement over a sample big enough to act on.
`MATCH_THRESHOLD = Decimal("0.55")` (all three of `matching.py` / `matching_achhad.py` /
`matching_vapi.py`) was **picked, not measured** against labelled ground truth. Real rates from a full
sync against live Drive data:

| Plant | PO line items matched to MIR (2026-09-18, latest Drive files) |
| --- | --- |
| HRS | **179 of 225 (79.6%)** |
| RTP-Achhad | **179 of 197 (90.9%)** |
| RTP-Vapi | **567 of 690 (82.2%)** |

Later measurements (the PO-number groups of 2026-09-24, see
[matching-engine.md](matching-engine.md)) moved these to 183 / 181 / 608 lines matched on the local
copy. **Re-measure before comparing anything to either set.**

**HRS's remaining gap is mostly not the matcher.** 21 of its 45 unmatched line items were on POs raised
in the last 30 days, where the goods had not been received or booked into MIR (Achhad's equivalent was
5 of 19); most of the older ones are POs no MIR row mentions at all. HRS's MIR carried **177 of 497
rows with day/month transposed dates**, 38 of them in the future; `parsers/mir.py` repairs them at
parse time the way `parsers/vapi_mir.py` does. **It cost one match** (180 → 179) and that is the
point: the date gate now runs on true dates. Achhad needs no repair - its date column is plain text.

The earlier table read HRS 68.6% / Achhad 86.0% / Vapi 50.5% and is superseded. Vapi moved because
three things changed at once: its MIR PO column went from ~100% blank to populated, its order book grew
131 → 222 POs, and `match_vapi` had been **hanging outright** (see the termination guards in
[matching-engine.md](matching-engine.md)), so the database predated all of it.

**2026-09-19: `identification_two_of_three` and `_MULTI_PO_HYPHEN_RE` both landed the same day** - 588
→ 599 from the flag alone, 588 → 577 with both together. **Don't quote a single before/after Vapi
percentage without saying which of the two changes it includes** - they move the same count in
opposite directions, by design.

MIR↔Stock rates were much lower (1.6-24%) and are **53.5% / 52.7% / 20.4%** after the 2026-09-21
three-tier pass. What remains is mostly structural: Stock is a current snapshot while MIR is a full
historical log, and **the RM sheets do not track conveyor belting, conveyor fabric, rubber compound or
MS crates at all** (36% / 47% / 76% of the three plants' MIR rows). Against the rows whose material is
actually in the sheet the figures are **84% / 100% / 85%**. **Quote that second set when comparing this
pairing to PO↔MIR** - the headline percentages have different denominators.

**Until a sample-based accuracy check exists, treat every match as a suggestion, not a fact.** This is a
process instruction, not just a UI note. Do not wire any downstream action - auto-approving a PO,
auto-updating stock - off a match without a human in the loop. The dashboard prompts for it through
`shared.js`'s `matchingDisclaimerHtml()` (one sentence plus a "How matching works" disclosure) and the
per-line confidence badge.

**Triage signals.** `_line_item_dict()` exposes `matchScore`, `matchTier`, `qtyDiffPct`, `rateDiffPct`,
`valueDiffPct`, the split mismatch booleans and `severity` per line item. The confidence badge is
rendered by `po-reconcile.js`'s `reconControlsHtml()`: **high** = tier `po_number`, **medium** =
weighted score ≥ 0.75, **low** = below that. The reconcile card shows Ordered / Received / Difference
in real figures rather than per-flag delta badges, so a partial delivery (qty short, rate in line) is
visibly different from a price discrepancy.

**The review queue.** `review.html` (`review-page.js`) + `review_views.py` back a deliberately minimal
queue: a batch of 5 random unreviewed matches (`_BATCH_SIZE`), both sides side by side, recording
Correct / Incorrect / Unsure as a `MatchReview` row. No filtering, no search, no bulk actions - the goal
is throughput (`REVIEW_TARGET = 200` distinct matches), and filtering would bias the random sample the
accuracy figure depends on. It is cross-plant (one router, like `imports_views.py`) and reuses each
plant's `MATCH_CONFIG` via `match_accuracy.CONFIGS` for model classes and lot field names rather than a
second mapping. Any authenticated role can review - data collection, not a privileged write.

**Role is open; plant is not (2026-09-23).** A card is real per-plant business data, and until this date
`next_review` drew from every plant regardless of `PTUser.plants` while `submit_review` accepted a
verdict on any plant. `next_review` now draws only from readable plants and `submit_review` 403s on any
other, via `user_can_access_plant()`. `review_views._ACCESS_KEY` maps `SyncRun.Plant` values (what
`MatchReview.plant` stores) to the lowercase access keys; `test_review_plant_scoping.py` fails if it
disagrees with the three `_PlantConfig`s. `review_stats` and its export stay cross-plant on purpose:
they report the accuracy of the app's own output, not plant data.

**A card carries both rows' identifiers and the matcher's own evidence** (2026-09-22). The first version
showed description / qty / rate / value / vendor only: a Tier-1 match could not be checked against the
PO number it was made on, `500 KG` against `500 MTR` read as agreement, and there was no key to find the
row in the source sheet. Each side now carries ordered `refs` (PO number and date, line, UOM, HSN,
delivery date / MIR number and date, the PO the MIR itself cites, invoice number and date / the lot's
item code, received date, UOM, category), and `signals` render the matcher's identification booleans
(`po_number_matched` / `vendor_matched` / `material_matched` / `manually_pinned`, and MIR↔Stock's
`material_matched` / `date_matched` / `uom_mismatch`) as plain-English pills. The lot's item-code and
UOM columns come from `MATCH_CONFIG.stock_code_field` / `stock_uom_field` (HRS `sap_item_code` / `uom`,
Achhad `sap_code` / none, Vapi none / `uom`); an empty field name omits the row rather than showing a
blank.

Two display rules came with it. **Import cards show INR, not the PO's own currency**: the card uses
`matching_core._import_rate_value_inr()`, the same conversion the matcher scores on; the raw USD
`net_price` made a correct match look like a ~90x rate discrepancy and invited a wrong "Incorrect"
verdict. The original-currency rate and exchange rate stay as ref fields. And the card **deliberately
does not use `formatInr()`**, which rounds to whole rupees and abbreviates at a lakh, rounding away the
exact comparison the verdict depends on. `apps/api/tests/test_review_card_payload.py` pins this.

**The accuracy figures are in the browser, not only a terminal** (2026-09-22). `review.html` has two
views behind one nav tab, **Review queue** and **Accuracy**. `GET /api/review/stats` serves
precision / recall / F1 overall and by plant, match type, plant × match type, tier and coverage band,
plus reviewer counts and the latest reviewer notes; `/api/review/stats/export` is the same tables as a
CSV.

**The scoring lives in `apps/services/match_accuracy.py`** and `manage.py report_match_accuracy` is a
renderer over it. Two implementations of precision/recall that could drift is not affordable for the
only measured accuracy statement this app makes. Four things a re-derivation would likely get wrong:

- **`byPlantAndType` materialises all 9 cells, including `n=0` ones.** An unsampled cell is a hole in the
  evidence; the panel renders it as "not sampled yet", never 0%.
- **A verdict is about a pair, not a match row** (2026-09-29). `MatchReview` stores `left_id` /
  `right_id` (the PO line and primary MIR row, or the MIR row and lot - `match_pairs.py`) beside the
  `match_id` it was recorded on. The matcher deletes a row when its line matches nothing and recreates it
  with a new id, so a verdict keyed on the row id went stale and the pair was served for review again.
  Verdicts now group, resolve and count by pair everywhere (`latest_verdicts()`, `_match_rows()`,
  `next_review`, `_progress()`).
- **A verdict on a pair no match row holds any more is dropped and counted as `staleVerdicts`** - the
  matcher no longer makes that pairing, so scoring it would be scoring the past. A verdict whose row was
  already gone when migration `0075` backfilled the pair columns keeps them null and is always stale.
- **Tiers resolve with one query per (plant, match_type) group**, not one per review (200+ queries at
  the programme's own target).
- **`precision` ignores "unsure" and `recall` counts it against the total.** `null` (nothing judged)
  renders as an en dash, never 0%.

**Throughput polish, same date.** Keyboard verdicts (`1` / `2` / `3`), `U` to undo, `N` for the next
batch, and a progress bar against the target counted in **distinct pairs** (`_progress()`:
re-reviewing one is a correction, not progress). Three things are load-bearing:

- **Undo deletes the row, and only ever the caller's own** (`DELETE /api/review/<id>` filters on
  `reviewer=request.user`; anything else is a 404). The model always allowed a correction (no unique
  constraint, latest verdict wins); the screen used to disable the buttons permanently. Not even an
  admin can delete someone else's verdict.
- **The batch does not auto-advance** when the fifth verdict lands; that made Undo unreachable for the
  card most likely to be a misclick. An explicit "Next 5" costs one keypress per five reviews.
- **`_pending` is set synchronously before the POST** (`review-page.js`), and `firstUnreviewedIdx()`
  skips a pending card. Without it, holding `1` down landed every keypress on the same card.

Keyboard handling is inert while the note textarea has focus and while the Accuracy view is open.
`apps/api/tests/test_review_stats_and_undo.py` pins scoring, stale/unsampled handling and both undo
permissions.

**Getting plant staff to reliably fill MIR's own PO-number field was considered and rejected as a
lever** - a training/process fix, not this app's to solve. Don't assume Tier-1 coverage will improve on
its own.

`apps/services/arithmetic_checks.py` is a separate, matching-independent layer: it self-validates the
source sheets' own arithmetic (a transposed digit, a rate typed as a fraction, a missing tax component,
a broken stock formula). `data_quality.py` turns the results into `DataQualityFlag` rows, upserting
current mismatches and **deleting flags that no longer mismatch**, so the table is current reality, not
a growing log. The API serves them per PO line item and per matched MIR entry (`_po_dict()`), and per
lot (`_lot_dict()`), as `dataQualityFlags`. Measured against real data: all three checks reconcile 100%
for HRS/Achhad and ~96-99.5% for Vapi, the remainder genuine data issues.

---

## Feature areas

### Frontend pages and the shared top nav

Six protected pages share one header (`brand.css`, modelled on the TDS Automation App's nav). The JS
side is in [frontend.md](frontend.md); the server-relevant facts:

- **`index.html`** (`/`) - the PO↔MIR↔Stock reconciliation dashboard. Reads every domestic and import
  endpoint above.
- **`home.html`** (`home-page.js`) - landing page. Its KPI row (Total PO's, Suppliers, This Month, This
  Week) is computed client-side by `loadKpis()` from all three plants' `purchase-orders` fetched in
  parallel. A plant that fails to load degrades the count **with a visible warning** rather than reading
  as zero. There is deliberately no server-side KPI endpoint.
- **`search-po.html`** (`search-po-page.js`) - PO lookup across all three plants (substring,
  case-insensitive, ≥ 2 characters). **Deliberately self-contained**: it fetches its own copy of each
  plant's PO list and deep-links into `/?plant=<key>&po=<number>` for full detail.
- **`review.html`** (`review-page.js`) - the match-accuracy queue and Accuracy view (above).
- **`mir.html`** (`mir-page.js`) - MIR entry, the register and open mismatches ([MIR entry](#mir-entry-2026-09-28)).
  Any role reads the register; entering needs Editor or Admin at the receiving plant.
- **`admin.html`** (`admin-page.js`) - admin only: per-plant sync status, Overview tab, Users panel.
  The client-side access-denied panel is defence in depth; **the endpoints enforce `IsAdmin`
  server-side**.

**Sync status labels**: the dashboard's badges and `admin.html`'s per-plant cards read **"PO Updated" /
"MIR" / "RM"**. Display labels only - the `sync` dict keys returned by `sync-status` are the
`SyncRun.source` values (`po_csv` / `mir` / `stock` / `match` / `consumption`) and are unchanged.
Don't go looking for a `source='rm'` value.

### In-app user management

`apps/api/routers/users_views.py` + `users_urls.py`, ported from TDS's own `users_views.py`. It replaced
an earlier `admin.html` that linked out to Django Admin, which was explicitly rejected ("don't django
admin, use our builded as we did in tds_app") - **no Django Admin links remain on that page**.

- `GET /api/auth/users` - list, each user with `correctionsCount` (from
  `admin_overview_views.correction_counts_by_email()`).
- `POST /api/auth/users/create` - body `email`, `password`, `fullName` (required), `designation`,
  `role` (default viewer), `plants`. Validates the email domain, rejects a duplicate with a real `409`,
  enforces `_validate_password_strength()` (≥ 10 chars, not all digits, not the email local-part).
- `PATCH /api/auth/users/<id>` - any of `role` / `isActive` / `fullName` / `designation` / `plants` /
  `password`. The password field is the in-app reset path, and a reset calls `revoke_all_tokens()` so
  the account's live sessions end (device trust is kept).
- `DELETE /api/auth/users/<id>` - same view; only the account in `DELETE_USER_ALLOWED_EMAIL` may delete.
- `GET .../devices`, `DELETE .../devices/<device_id>`, `POST .../logout-everywhere` - trusted-device
  management and the admin-side panic button.
- **An admin's `plants` is always forced to `[]`** on create and update: the scoping helpers do not
  special-case role, so a non-empty list would lock an admin out of an unscoped plant.
- `manage.py create_pt_user` remains the only way to create the **very first** account.

URL naming deliberately differs from TDS's, which disambiguates `GET /users` from `POST /users/` by
trailing slash alone - fragile. This app uses distinct path segments (`/auth/users` vs
`/auth/users/create`). The last-active-admin lock and the delete gate are explained in
[auth-security-email.md](auth-security-email.md).

`admin_overview_views.py` backs the Overview tab. TDS's concepts don't map literally (a TDS document is
*created* by a user; a PO is synced from a CSV and nobody authors one), so they were adapted: **Top
Correctors** (inline corrections per user across the three correction tables), **Top Vendors** (PO count
per vendor across the three **domestic** PO tables), and **Recent Activity** (last 10 corrections across
all three correction tables). The KPI row reuses `home.html`'s client-side `loadKpis()` rather than
duplicating it server-side; this endpoint covers only the two things with no client-side source.

### Dashboard structure (Artifact parity)

The nav structure and PO-list KPI row were rebuilt to match the original Claude Artifact prototype, whose
actual source was read directly before anything changed. Three structurally different tab components,
one per level: `.view-tab` (Purchase Orders / Raw Material Analysis), `.plant-tab` (plant selector,
including "All Plants" first on the project owner's request), `.sub-tab` (Domestic / Import, no
"Combined"). Details in [frontend.md](frontend.md). **Lesson recorded for parity work:
sample-checking a reference's CSS is not enough** - read the complete stylesheet and markup.

**"(Changed Purchase Order)" needs no code.** The artifact has no amendment-detection logic
(`_isNewPo` is hardcoded `true`). That text is literal content in some real `po_number` values (e.g.
`'3000001104 (Changed Purchase Order)'`), written by the upstream CSV extraction. `po_csv.py` captures
it verbatim and the API returns it verbatim. **If you see this and think "I should build amendment
detection" - don't.** The matcher separately matches an annotated order on its bare number too, and a
rename upstream retires the old spelling (see [data-sync.md](data-sync.md)).

### KPI cards are filter buttons - all of them, or none of them

Every card in both KPI rows renders as a button and narrows the list below to the rows behind that
number; `state.statusFilter` / `state.matStatusFilter` is the single source of truth, shared with the
"Filter by Flags" bar and the table's Status header select. Entirely client-side
(`po-list.js`, `import-po.js`, `materials.js`, `shared.js`'s `revealFilteredList()`); no endpoint is
involved and the KPI numbers are computed from the same list payloads. The rules:

- **`total` and `value` CLEAR** the filter (they count the whole category-filtered set). Purchase Orders'
  own `total` is the only non-narrowing card on that side.
- **`transit` and `qtyordered` narrow to the same `openpo` row set** and share one `filterKey`, so
  selecting either lights up both. Until 2026-09-21 they did nothing when clicked.
- **If you add a KPI card, decide which of the three it is** - narrows, clears, or shares a `filterKey` -
  and wire it. A button that does nothing is invisible in review because the markup is identical.
- A narrowing click scrolls the list into view, but not when it is already visible, not on clearing, and
  with a jump instead of a glide under `prefers-reduced-motion`; it also `announce()`s the list heading.

Full reasoning and the verification harness in [frontend.md](frontend.md).

### Data Quality Flags

Flag categories are computed **client-side** from fields the API already returns; the server's job is to
serve every computed match-quality signal, not to categorise. `flags.js`'s
`FLAG_CATEGORY_RULES` / `categorizeFlag()` / `DISCREPANCY_LEGEND` started as a port of the artifact's
11 regexes run against each PO's `remarks`.

**The category list covers every computed match-quality signal**, not just qty/rate/remarks.
`matching_core._diffs_and_flag()` always computed taxable/final-value flags, and the models had
`tax_type_mismatch` / `uom_mismatch`, but only qty/rate used to reach the frontend. The API now serves
all of them per line item (`_line_item_dict()` and imports' `_mir_match_dict()`): `qtyMismatched`,
`rateMismatched`, `dataMismatch`, `taxTypeMismatch`, `uomMismatch`, `netValueMismatched`,
`taxableValueMismatched`, `finalValueMismatched`, `vendorMatched`, `severity`, and the diff percentages.
`computePoFlags()` turns them into **PO Not Found in MIR** (critical), **Tax Type / Net Value / Taxable
Value / Final Amount / UOM Mismatch in MIR**, and **Vendor Name Mismatch in MIR** (from
`vendorMatched=False` under 2-of-3 identification). **The frontend must not re-derive a value flag from
`valueDiffPct` alone** - that ignores `VALUE_FLAG_EPSILON`; read the stored booleans. `vendorMatched`
defaults to `true` for a row or plant without the column, so the flag never fires where the concept does
not apply. Backend value-mismatch columns came in migration `0040` across all six match models.

**"Filter by Flags" has no fixed option list** - one option per category actually present in the current
selection. The same treatment covers **Import Purchases** (`import-po.js`'s `importCategoriesFor()`,
`flags.js`'s `importCriticalFlagsFor()`, reading the nested `mirMatch`) and **Raw Material Analysis**
(`materials.js`, `material-modal.js`), each view computing its own categories.

**"PO Not Found in MIR" is not raised on an order that is not due yet** (2026-09-25) - nothing has
arrived and the delivery date has not passed, so there is nothing for MIR to hold. It fired on every
such order by construction, which put every open order into the Data Quality Flags count on all three
views. The test is the row flags' own "on order and not partial": domestic `po._status === 'pending'`
(`computePoFlags()`, and per line in `materials.js`), import `flags.js`'s `importOrderNotDueYet()`
(`deliveryDateStatus === 'On Order' && !partialDelivery`, used by both `importCategoriesFor()` and
`importCriticalFlagsFor()`). An overdue, part-arrived or undated order still raises it.

**The regex categorisation is intentionally imprecise, same as the original.** A remark mentioning "no
Item ID/Vendor Code printed" matches "Vendor code scheme inconsistency" via a bare `/vendor code/i`.
Known, accepted, carried over.

### Row flags are four buckets, one icon each (2026-09-22)

Project owner: *"keeping 1-4 flags aside of status seem too much - I was thinking of keeping only red for
any mismatches, blue for partial delivery, yellow for on order and purple for all data quality issues."*
`flags.js`'s `rowFlagsHtml({partial, onOrder, categories})` is the one implementation, called by
`po-list.js`, `import-po.js` and `materials.js`: blue = partial delivery, yellow = on order, red = any
`critical` category, purple = any other category, with the category names in the tooltip.

Server-relevant rules: the "on order" and "partial" inputs come from the API's own fields for Import
(`deliveryDateStatus === 'On Order'`, `partialDelivery`, both from `import_flags.py`); **"PO Not Found in
MIR" is suppressed from the red bucket while a row is ON ORDER** (it would have made red mean nothing)
but still counts on overdue and partial rows - the category itself is no longer raised for such an
order (see above), so this suppression is now a second guard rather than the only one;
**overdue is not a fifth bucket** (the status pill already turns red); and **`partial` wins over
`onOrder`** when Import can report both. Details in [frontend.md](frontend.md).

### Inline "Edit Everywhere"

**Import lines are addressed by position (2026-09-25).** The Import modal sends `itemId: "#<itemRef>"`;
`_resolve_import_line()` resolves it by position (the same ref pins use) and records it on the
correction (`ImportPOCorrection.item_ref`, migration `0062`), so revert hits the same line. A bare
item_id is still accepted when it names exactly one line, and refused with a 400 when it names
several: 12 Vapi orders repeat an item_id across shipment lines, and `.first()` wrote line 2's
exchange rate onto line 1. `exchange_rate`, `boe_number`, `bill_of_lading_number` and
`total_inclusive_value` are re-match triggers too - the matcher converts, pairs, gates and checks the
landed rate with them.

Every PO detail modal (Domestic and Import) and the Raw Material Analysis modal have per-field
pencil-to-edit corrections. **This mutates the real row and writes an append-only audit row - it is not
a resolve-at-read overrides table.** A spec described the latter, but Import's edit feature had already
shipped with mutate+audit, and the decision was to extend that rather than rebuild both. **A correction
lasts only until the next sync of that source rewrites the row** - the success message says so ("fix
the source file too").

**Backend.** Each `correct_field` / `correct_material_field` view allow-lists field names (split PO-level
vs item-level), coerces dates / decimals (`_coerce_value()`; `decimal.InvalidOperation` is re-raised as
`ValueError` so bad input is a 400, not a 500), and does the mutate + audit row in one
`transaction.atomic()`. Audit models: `DomesticPOCorrection`, `ImportPOCorrection`, `MaterialCorrection` -
separate models, same per-type convention as everything else. `validation.py`'s `is_valid_gstin` /
`is_valid_email` **warn, never block** (`_field_warning()` adds a `warning` key to the 200).

Body: `{"field": "<model field>", "value": ..., "reason": "...", "itemId": "<optional>"}`. A present
`itemId` selects a line item (`filter(item_id=...).first()`), otherwise the PO. Allow-lists:

| Router | PO-level | Item-level |
| --- | --- | --- |
| Domestic (`_domestic_base`) | `po_created_date`, `vendor_name`, `vendor_address`, `vendor_gstin`, `vendor_email`, `vendor_code`, `billing_address`, `ship_to`, `payment_terms`, `incoterms`, `currency`, `total_value`, `tax_type`, `total_inclusive_value`, `remarks` | `description`, `hsn`, `qty`, `uom`, `delivery_date`, `net_price`, `net_value` |
| Import (`imports_views`) | same minus `tax_type` / `total_inclusive_value` | `description`, `hsn`, `qty_as_per_po`, `qty_as_per_boe`, `uom`, `delivery_date`, `net_price`, `net_value`, `tax_type`, `currency_after_taxes`, `exchange_rate`, `total_inclusive_value`, `boe_number`, `bill_of_lading_number`, `laden_on_board_date`, `country_of_origin`, `license_type`, `license_number` |

Editing a field that feeds matching **synchronously re-runs that plant's `run_full_match()`** so badges
update immediately - safe because it is idempotent and volumes are small. Trigger sets: domestic
`vendor_name`, `vendor_gstin`, `description`, `qty`, `net_price`, `net_value`; import the same with
`qty_as_per_boe` in place of `qty`; materials per plant (below). `imports_views._coerce_value` is its own
copy on purpose: its date/decimal field sets are a superset of the domestic ones.

**Domestic line items have no stable natural key.** `item_id` isn't unique or required, and a sync
deletes and recreates every line item on any change. Item-level corrections key on
`(plant, po_number, item_id)`, same as `ImportPOCorrection`; a blank-`item_id` domestic item cannot be
individually corrected. (Manual MIR pins use a position-based `itemRef` instead, see below.)

**Material edits are per lot**, on the "Stock by Plant" tab's lot rows, **not** the Overview tab's
rolled-up Classification block, which shows one "first found" value across sibling lots and would be
ambiguous. `MaterialCorrection` keys on `(plant, lot_id)`. The allow-lists genuinely differ per plant:

| Plant | Editable | Decimal | Re-match triggers |
| --- | --- | --- | --- |
| HRS | `description`, `category`, `sub_category`, `uom`, `basic_rate`, `party_name` | `basic_rate` | `description`, `party_name`, `basic_rate` |
| RTP-Achhad | `description`, `category`, `rate`, `msl` | `rate`, `msl` | `description`, `rate` (no vendor column; `msl` never feeds matching) |
| RTP-Vapi | `description`, `category`, `batch_no`, `uom`, `basic_rate`, `supplier_name` | `basic_rate` | `description`, `supplier_name`, `basic_rate` |

Vapi offers `batch_no` where HRS has `sub_category`: the Vapi Stock sheet renamed that column in place on
2026-09-12, so `sub_category` is no longer sourced. The API's displayed `category` / `subCategory` for a
lot is the canonical `MaterialCategoryReference` lookup, not the raw column - see `_lot_dict()`.

Domestic's PO modal has a **Flags & Corrections** tab; Import's same-named tab renders correction history
too. **Domestic has no Shipment & License tab** - BOE / BL / licence fields don't exist on domestic
models. Don't add one without a real schema change first.

#### The correction box comes to the field (2026-09-21)

Clicking a pencil used to switch to the Flags & Corrections tab, where the field being corrected was no
longer on screen. `shared.js` now moves the correction box directly under the clicked line
(`selectFieldForCorrection()` / `_moveOverrideBoxTo()`), with Cancel, a dirty-check before discarding a
half-written correction, Escape backing out of the correction before closing the modal, and a
keyboard-reachable pencil. Frontend-only; see [frontend.md](frontend.md).

#### Validation runs BEFORE the write, not after

`_field_warning()` (GSTIN / email) runs *after* `target.save()`, so an invalid GSTIN was committed and
then reported. `shared.js`'s `validateOverrideValue()` now runs first and returns `{error}` (blocks) or
`{warn}` (confirm, then save). Its regexes are a **deliberate mirror** of `validation.py`'s, not a
stricter client rule: the backend is warn-never-block by design, so a stricter client would make the two
disagree about what is savable. The server still treats an empty `value` as NULL (`_coerce_value()`),
which is why the client adds a **clear-confirm** whenever a field that had a value is about to be saved
blank: a `<input type="number">` holding typed garbage reports `''`, and that silently erased real
figures.

#### Revert, and what the success message says

`wireRevertLinks()` PATCHes `oldValue` back through the same endpoint, which **writes its own audit row**,
so the history records both the change and the change back. Domestic offers revert for PO-level fields
only (item-level corrections key on an unreliable `item_id`); Import for both; Material gates each row on
that row's own plant. The sync-overwrite caveat rides on the success message, because in small grey type
it was reliably missed.

### Editing which MIR a PO line matched (2026-09-21)

Project owner: *"the edit option - I think in purchase order it would be great if we can edit the MIR
number too, and if that was assigned to some other PO then a pop up would appear telling that matching
with this would break so and so."*

`ManualMirMatch` (migrations `0055` / `0056`) is one shared table with a `plant` column - the
`FlagDismissal` case, not the per-plant MIR/Stock case, since nothing in it comes from a spreadsheet.
Unique on `(plant, po_kind, po_number, item_ref)`. **Domestic AND Import**: `_domestic_base.py`'s
`make_mir_candidates()` / `make_set_mir_match()` generate the per-plant Domestic pair, and
`imports_views.py` has its own `mir_candidates` / `set_mir_match` for the cross-plant router. The shared
pieces - `_mir_row_dict()`, `_claims_for_mir_numbers()`, `_held_mir_numbers()`, `_sheet_row()` - are
imported by the imports router rather than copied; the views themselves are not factored together (one
is a `_PlantConfig` factory producing three views, the other a single view for all three plants).

**Endpoints.** `GET .../mir-candidates?q=` returns up to 80 active MIR rows (`_candidate_rows()`,
shared by both routers). With no `q` the order's OWN receipts come first - rows whose PO column names
it and every document its lines are matched to - then the newest rows fill the list; a plain "names
this PO or has a date" cut at 80 left the current MIR out of the default list on 162 of 190 HRS lines.
Results are collapsed to
**one entry per MIR number** with `rowCount`, `sheetRows` (every Excel row of that document) and
`claimedBy`. With no `q` it returns rows whose PO column mentions this PO plus recent ones; `q` filters
MIR number, party or material. The Import picker also sends `itemRef`, so the endpoint can mark a
holder on the same order and Bill of Entry as the edited line, for a receipt booked under that BOE,
`sharesReceipt: true` - that line keeps its share whatever is chosen (see "Pins on a shared Bill of
Entry" below), so the picker does not warn about it. With an `itemRef` both pickers also group and
explain each receipt - see [Editing a line's receipts one at a time](#editing-a-lines-receipts-one-at-a-time-2026-09-29).
`PATCH .../mir-match` takes `{itemRef, action, mirNo, reason, share, undoType, undoId}`
(`_domestic_base.change_from_request()`, shared by both routers). The body with no `action` still
works as it did: a non-empty `mirNo` pins (`set`), an empty `mirNo` pins the line as deliberately
unmatched (`notReceived`), `clear: true` removes the pin (`auto`), and `share: true` (read with
`_request_bool()`, stored as `ManualMirMatch.shared`, migration `0061`) makes it a "Keep both" pin.
An unknown `mirNo` or action is a 400; a retired PO or unknown `itemRef` a 404. The change is
written first and the re-match queued on the worker (`rematch.request_rematch()`); the response
carries `manualPinsApplied`, `stalePins`, `unfilledPins` and `unfilledEdits` once that run has
finished (always under pytest).
`unfilledPins` lists pins whose MIR document had no free row left (a newer pin holds it, or the number
is gone from MIR): the line is left unmatched, never auto-matched, and the picker tells the user
instead of saying "Matched".

**It names a MIR NUMBER, not a MIR row, and that is the central decision.** One MIR document routinely
covers several material lines, so `mir_no` is not unique. The unique column is `source_row_ref` - the
openpyxl row index, which shifts the moment a row is inserted above it. Pinning a row number would
silently re-point itself at a different material on the next sync. Naming the number says what a person
knows ("this line came in under MIR 96/05") and leaves the matcher's qty/rate/material scoring to pick
among that document's rows. A test pins a two-row document and asserts the right row wins.

**An empty `mir_no` is a real instruction, not a missing value:** "no MIR matches this line, leave it
unmatched." Without it there is no way to correct a confidently-wrong match except by pointing it at some
other wrong row.

**A pin OUTRANKS identification** - the realistic reason to reach for one is that the automatic rule got
the row wrong, and the commonest cause is precisely the evidence identification runs on.
`_forced_candidate()` skips `_identification_pool()` entirely but measures everything else honestly, so
**arithmetic still flags**: a manual match whose qty and rate disagree still raises Quantity / Rate
Mismatch. A pin overrides identification, never the financial check.

**`item_ref` is the line's zero-based position within its PO**, ordered by pk - the master CSV's own row
order. Domestic line items have no stable natural key (`item_id` is neither required nor unique), and a
sync deletes and recreates every line, so a pk is no good either. `item_description` is stored alongside
as a **staleness tripwire**: when the description at that position no longer matches, the pin is ignored
and reported in `manual_pins_stale`, never applied to whatever now occupies the slot, and never deleted.
`matching_core.line_item_positions()` is the one implementation, imported by both routers, and the API
serves it as each line's `itemRef` - **the only thing the frontend may use to address a line**. Import
lines use the same position-based ref even though they carry a real `item_id`: the import sync
recreates lines too, and one rule is better than two.

**Pins settle before groups and before the optimal assignment**, so a row a human named cannot be taken
by a better-scoring automatic pair, and pinned items are excluded from groups and ungrouped edges. Two
pins naming the same single-row document resolve **newest-decision-first** - load-bearing, since the
first version computed that order and then iterated the wrong list.

**`po_kind` is part of the unique key, not a bare tag.** Domestic and Import POs live in separate tables,
so one number can exist as both; without it a domestic pin would address an import line or the reverse.
Each endpoint filters and writes its own kind on both its clear and its upsert: the import endpoint
`po_kind=import`, the domestic one `po_kind=domestic`. Until 2026-09-24 the domestic endpoint passed no
`po_kind`, so where a PO number existed as both kinds with the same `itemRef`, a domestic clear deleted
the import pin and a domestic set could overwrite it. `test_manual_mir_match_api.py` pins both cases
with an import pin on the same number. **Any new pin query must name its `po_kind`.**

**Both kinds of pin load in ONE pass and settle against ONE `claimed_mir_ids` set**, because they
compete for the same MIR table, exactly as their automatic matches do. `pinned_keys` holds
`(kind, item id)` pairs so a domestic and an import line sharing a database id are never confused.

The same reasoning drives the Import picker's `claimedBy`: it reports **domestic holders as well as
import ones** (`_claims_for_mir_numbers()` on the plant's domestic `_PlantConfig`, via
`_domestic_cfg_for()`, plus `_import_claims_for_mir_numbers()`, whose entries carry `isImport: true`).
Showing only half the holders would let a reader take a row believing nothing was using it. The domestic
picker reports domestic holders only.

**`claimedBy` is read from the match table, not from the pins**, and counts a document held through a
multi-shipment group (`group_entries`) as well as a primary `mir_entry` - a row held by an ordinary
automatic match is just as worth warning about as one held by someone else's pin. Only active POs'
matches count.

**When the document is already held, the picker asks: Keep both, Move it here, or Cancel** (project
owner, 2026-09-25: *"do you want to keep both; remove previous one or cancel"*). Each option says what
it costs:

- **Move it here** (`share` false) - the row is this line's alone. The holder is re-matched from
  scratch and *"may end up with no MIR"*, which is true.
- **Keep both** (`share` true) - the pin takes its document's best row **whoever holds it, and claims
  nothing**, so the holder keeps the row through whichever stage gave it before. Once everything is
  assigned, `_split_shared_rows()` has every line holding that row count its share by ordered quantity
  (`receipt_share` on the match; the domestic models gained the field in `0061`), so a 1,000 KG line
  and a 5,000 KG line on one 1,000 KG receipt count 1/6 and 5/6 of it rather than each reading the
  whole. The split happens only when it is honest: every holder counts that one row alone and in
  full, and all quantities are known and in one unit. Otherwise the row stays unsplit and the flags
  show the full figure. The reconciliation card says the receipt is shared (`po-reconcile.js`'s
  `receiptShareNote()`).

**Pins on a shared Bill of Entry (2026-09-25).** Vapi 1000001317 has three lines on one BOE, booked in
MIR as one 24,000 KG row (MIR43/05), and BOE settlement shares that row across all three. A pin used
to claim it exclusively, so pinning any line to MIR43/05 unmatched the other two, and pinning all
three left two "unfilled". A pin on an import line naming a document whose every row cites that line's
own BOE is now handed to BOE settlement (`_pin_defers_to_boe()`), which offers the pinned line only
that document and treats the pin as its evidence. The line is still marked pinned, and the siblings
keep their shares. If settlement cannot place it (different Bills of Lading, quantities that cannot be
split), the pin falls back to an ordinary exclusive claim, never to the automatic pick.

**A pin survives refreshes and syncs.** It is stored apart from both spreadsheets and re-applied on every
match run, ahead of every automatic stage. It stops applying only when it goes stale (the line's
description changed) or unfilled (the document has no free row, or left MIR). Both are reported,
never silently re-matched.

`manually_pinned` on the six `*POMirMatch` models is **derived, not preserved** - rewritten every run, so
removing a pin clears the badge (`manuallyPinned` in the API). `matching_core.py` keeps its "no model
imports" shape: `manual_match_model` and `syncrun_plant` are injected by the three `matching*.py`
modules.

**One picker, two routers.** `po-modal.js`'s `wireMirPicker()` takes an `api` object
(`candidates(q, itemRef)`, `save(body)`, `preview(body)`, `previewStatus(id)`, `changes()`), so Domestic
(`apiForPlant()`) and Import (`apiImports()`) share one panel and collision popup. It is a picker, not a
free-text box: typing a number blind is how you pin a line to a document that does not exist.

Covered by `apps/services/tests/test_manual_mir_match.py`, `test_manual_mir_match_imports.py` (pipeline)
and `apps/api/tests/test_manual_mir_match_api.py`, `test_manual_mir_match_imports_api.py` (HTTP:
permissions, plant scoping, validation, upsert-not-duplicate, retired PO, `claimedBy` / `itemRef` shape,
import `clear` deleting only the import pin). No test covers the domestic endpoint against a same-numbered
import pin.

### Editing a line's receipts one at a time (2026-09-29)

HRS 3000001167 (6MPA and 7MPA reclaim rubber, 50 t each): 7MPA counted 20/09 and 66/09, and 59/09 -
also 7MPA, PO typed `30000001167` - sat on no line. It identifies on vendor and material, but the line
was already settled by the receipts citing its PO number, and such a line takes nothing more. The only
tool was a pin, which **replaces** a line's receipts with one document, so adding 59/09 cost the line
its other two. The panel ("edit receipts" on each line card) now changes one receipt at a time.

**`ManualReceiptEdit`** (`review.py`, migration `0074`): per line and MIR number, `action` **add** or
**remove**, plus `shared`, `reason`, `created_by`. Addressed exactly like a pin (plant, `po_kind`, PO
number, `item_ref`, `item_description` as the staleness tripwire), unique on
`(plant, po_kind, po_number, item_ref, mir_no)`.

- **Add**: the line counts the document's best row **on top of** what it matches automatically. The row
  is claimed right after the pins (so no automatic stage takes it first), the line itself still takes
  part in every stage, and the rows are merged into its match at the end, compared as one total.
  `shared` is "Keep both" and claims nothing. A document with no free row is reported in
  `manual_edits_unfilled`.
- **Remove**: the document is dropped from that line's candidates, so no stage offers it there; its
  rows go to whichever line fits next. A removal promises only that: the line may still be given
  another receipt citing the order (the one-row-per-line step), which the reader can remove too or
  answer with "not received".

**`manual_receipts.apply_change()`** is the only writer of pins and edits, for both routers, and keeps
them consistent: add clears a "not received" pin and a removal of the same document; remove clears an
add of it and a pin naming it; `notReceived` clears the line's adds; `auto` ("Back to automatic")
clears every pin and edit on the line; `undo` deletes one pin or edit by id, only on that line.

**Who did it** - every run writes `receipt_notes` on the six `*POMirMatch` models (derived, like
`manually_pinned`): one `{mirNo, how, by, byName, at, reason}` per receipt a person placed (`pinned`,
`added`), and `moved` (with `fromPoNumber`, `fromLine`, `fromKind`) on the line that took a document
someone removed elsewhere. `byName` is the account's `full_name`. Served per receipt as `manualNote`
(`_counted_mirs()`), shown under the receipt on the card and in the panel.

**`GET .../manual-changes`** (both routers, `manual_receipts.manual_changes()`): each line's pins and
edits with `type`, `id`, `action`, who, when, reason and `stale`, plus `elsewhere` - pins and adds on
**other** orders' lines naming a receipt that cites this one. The Flags & Corrections tab lists them
with an Undo each (the tab's "revert" links undo field corrections only, and used to read as if they
would undo a pin).

**Preview** (`receipt_preview.py`): `POST .../mir-match/preview` with the PATCH's body queues a dry run
and returns `previewId`; `GET [<p>/]mir-match-previews/<id>` (Import:
`imports/mir-match-previews/<plant>/<id>`) returns `{state, lines, unfilled}`. The worker runs the
plant's `run_full_match(dry_run=True)` twice inside rolled-back transactions - as things stand, then
with the change applied through `apply_change()` - and lists every line whose receipts or received
quantity moved, plus every line of the edited order. As-things-stand is a real run, not the match
tables, because those hold the last run's result, which can predate a deploy or a queued change. A
preview id belongs to its plant (another plant's endpoint answers 404). The panel's Save works while
the preview runs and when it fails: the preview informs, it does not gate.

**Candidates** (`order_candidates()`, both routers): with an `itemRef`, entries are grouped `cites` /
`vendorMaterial` / `vendor` / `other`, newest first in each, with `onThisLine` and `why` - the
matcher's own evidence (`_forced_candidate()`, the contradiction gate, the date verdict) as sentences:
"Its PO column says 30000001167, which is not one of our orders - one digit away from 3000001167, so
probably a typo. Correct it in the MIR sheet and it will match on its own", and "This line already
counts the receipts that cite its PO number...".

Covered by `apps/services/tests/test_manual_receipt_edits.py` (the 3000001167 shape: add keeps the
line's receipts, contrast with a pin, move vs keep both, unfilled, stale, removal and its moved note,
preview rolls back, dry run skips MIR<->Stock) and `apps/api/tests/test_manual_receipts_api.py`
(every action's writes, undo scoping, permissions, listing, preview endpoints, candidate groups and
reasons, the Import router).

### Dismiss / override a flagged match or flag

Two mechanisms, because the two things are stored differently.

**Match dismissal.** Every `*POMirMatch` / `*MirStockMatch` has `dismissed_by_override` plus
`dismissed_by` / `dismissed_at` / `dismissed_reason` (migration `0011`).
`match_dismiss.dismiss_match(model_cls, match_id, user, dismissed, reason, *, plant, match_type)` is the
one implementation for every plant and both PO kinds; each router passes its own model class, its
`SyncRun.Plant` value and the match type. Endpoints:
`PATCH [<p>/]matches/po-mir/<id>/dismiss`, `[<p>/]matches/mir-stock/<id>/dismiss`, and
`PATCH imports/matches/po-mir/<plant>/<id>/dismiss`. Body `{"dismissed": true|false, "reason": "..."}`;
response `{status, matchId, dismissedByOverride, dismissedReason}`, 404 for an unknown id. **Clearing a
dismissal also clears the three audit columns** - an undone decision should not keep stale provenance.
**Every matcher's `update_or_create` `defaults` never carries `dismissed_*`, so a dismissal survives every
re-match that keeps the same pairing** (`test_dismiss_match.py::test_editor_can_dismiss_with_reason_and_it_survives_rematch`).
A PO↔MIR match row is keyed on its line item, so a run that pairs the line with a **different** MIR row
updates the same row in place - and `matching_core._save_po_mir_match()` clears the dismissal then,
since it judged the old receipt and would otherwise hide the new one's flags
(`test_manual_mir_match.py::test_a_dismissal_survives_a_rematch_but_not_a_repointed_match`). MIR↔Stock
rows are keyed on the (MIR row, lot) pair, so they cannot be re-pointed.

**The decision is also stored on the pair, in `MatchDismissal`** (2026-09-29). A run deletes a match row
when its line matches nothing and creates a new row (new id) when the same pair matches again, and the
new row used to come back undismissed. `dismiss_match()` now writes the row's columns and a
`MatchDismissal` keyed on (plant, match type, left id, right id) in one transaction - left/right are the
PO line and primary MIR row, or the MIR row and lot (`match_pairs.py`) - and undismissing deletes it.
`run_full_match()` calls `match_pairs.restore_dismissals()` after writing the PO↔MIR rows and again
after MIR↔Stock, copying each record back onto whichever row holds its pair. A re-pointed line still
loses the dismissal (different pair); if a later run pairs it with the original receipt, it comes back.
Readers still filter on `dismissed_by_override`; nothing reads `MatchDismissal` except the restore.
`test_match_decisions_follow_pair.py` pins all of this.
Dismissal also removes the match from the Plant Data Correction email.

**PO-level flag dismissal.** The Quantity / Rate-Value critical flags and Data Quality Flag categories are
computed at read time, so there was no column to carry a dismissal. `FlagDismissal` (migration `0014`) is
unique on `(plant, po_number, flag_key)`, upserted by `flag_dismiss.dismiss_po_flag()` with the same audit
fields and the same clear-on-undo rule. Endpoints: `PATCH [<p>/]purchase-orders/<po>/flags/dismiss` and
`imports/purchase-orders/<plant>/<po>/flags/dismiss`; body `{flagKey, dismissed, reason}` (a missing
`flagKey` is a 400). `flag_key` is opaque to the server: Domestic sends its own flag label (safe -
`computePoFlags()` holds one entry per label), Import sends `<code>:<item_id>` (e.g. `"F7:ITEM3"`, since
`import_flags.py`'s flags are per item and carry a stable code). The rows ride back on each PO as
`flagDismissals` (domestic list payload, import detail payload).

Frontend: a dismissed flag **stays visible but muted** in the PO's Flags & Corrections tab (struck
through, tooltip with who / when / why), wired by one shared `wireDismissLinks()`; the optional reason is
a plain `window.prompt()`. **It stops counting everywhere else** (2026-09-25): the KPI cards, "Filter by
Flags", the row flags and row tint all read it through `flags.js`'s `poFlagDismissed()`. Until then only
the modal honoured it, so a dismissed "Rate Mismatch in MIR" still counted on the Rate Mismatches card.
An Import F1-F7 code stops counting only once every one of its per-line flags is dismissed. The Plant
Data Correction email reads match-level dismissals only (`dismissed_by_override`), not these.

### Import purchases

Import POs for all three plants are parsed by the shared `parsers/import_po_csv.py`, with a read-time
layer in `import_flags.py`: shipment-stage rollup, PO-vs-BOE qty discrepancy, the receipt-based
Material Inwarded / Partial Delivered / delivery-date status, and 7 data-quality flags (`po_flags()`).
The receipt-based three read each line's MIR match with the same rules as Domestic's status buckets
(2026-09-25); before that they read customs clearance and BOE quantities, so a cleared shipment never
received at the plant read Delivered, and the cards' tooltips described something the code did not count.

`imports_views.py` is cross-plant by design (plant is a path segment; all three import CSVs are
byte-identical, so there is no per-plant divergence to protect). `_PLANTS` maps each plant key to its PO
model, line-item model, `SyncRun.Plant`, label and import match model. `_item_dict()` carries a nested
`mirMatch` (null when the item never crossed `MATCH_THRESHOLD`) with `matchId` / `tier` / `matchScore`,
`matchedMirs` / `received` (every counted receipt, via `_domestic_base._counted_mirs()`),
`orderedRateInr` / `orderedValueInr`, diff percentages, the split mismatch booleans, `dismissed*` and
`stockMatched` (read from the `mir_entry.stock_matches` prefetch, not a query per item). The mismatch
booleans are read with `getattr` defaults because not every plant's import match model has every column.
Each line also carries its canonical `category` / `subCategory`, so Raw Material Analysis can file an
import-only material row correctly.

PO-level receipt rollups are server-computed: `materialInwarded`, `partialDelivery` and
`deliveryDateStatus` on each list row (`import_flags.py`). The MIR *mismatch* rollups are not:
`import-po.js`'s `renderImportPoList()` computes `poQtyDiscMir` / `poRateDiscMir` client-side (a PO has a
mismatch if any live line does). The server's PO-level `qtyDiscrepancy`
(`import_flags.po_has_qty_discrepancy()`) is the PO-vs-BOE check, not a MIR one.

**The import rate check accepts the landed basis too** (2026-09-25): a cleared line agrees if MIR's
final rate matches landed value / BOE qty within 0.01%, or its rate matches the pre-duty rate exactly -
see [matching-engine.md](matching-engine.md#import-po--mir-convert-currency-first). `mirMatch` carries
the pair as `landedRateInr` / `receivedFinalRate` (null until cleared), from
`matching_core.import_landed_rates()`, and the PO modal's card shows it as a "Landed rate" row. It also
carries `mirExchangeRate` / `exchangeRateMismatched` (a rate gap that is a different customs exchange
rate - an info "Exchange Rate Differs from MIR" category, not a Rate Mismatch) and `receiptShare` (a
line's share of one receipt covering several lines of its Bill of Entry; `_counted_mirs()` scales each
row's `qtyInPoUnit` / `value` and the `received` totals by it, leaving the row's own `qty`). Tier
`boe_number` means MIR cites the line's BOE number and reads as high confidence. See
[matching-engine.md](matching-engine.md#import-po--mir-convert-currency-first).

**Every import endpoint filters `is_active=True`** - the list, `purchase_order_detail`,
`correct_field`, `mir_candidates` and `set_mir_match` - so a retired import PO 404s by direct URL the
same as it is missing from the list, matching the domestic routers.

**BL tracking**: `bl_tracking.py` is a thin, single-call passthrough to SafeCube's (Sinay's) Container
Tracking API v2, behind `GET imports/track-bl?bl=`. **Nothing is stored** - every call hits the API live,
with no caching or rate-limit tracking. If the trial key's quota becomes a problem, add a short-TTL cache
keyed on `bl_number` then; caching before would be guessing.

**RoDTEP** (`RodtepScrollEntry`, `RodtepUsage`) and **Advance Licence** (`AdvanceLicense`,
`AdvanceLicenseMaterial`) are company-wide ledgers surfaced by `rodtep-panel.js` /
`advance-license-panel.js` under the Imports view. Both are synced by the dashboard's "Refresh Data"
button **once per click, not once per selected plant** (one shared lock each), and `pollSyncUntilDone()`
waits on `rodtepInProgress` / `advanceLicenseInProgress` from `GET imports/sync-status`. The two triggers
fire in **parallel**: unlike every plant trigger, both run their sync **synchronously** inside the request
(`sync_trigger.trigger_rodtep_sync()` / `trigger_advance_license_sync()`: one or two small files, a few
seconds, well under gunicorn's 30s timeout), so awaiting them in sequence made the click wait for one
download before starting the other. Their response is `{"status": "ok"}` (200), not 202.

### Licences: the import side was in the CSV all along (2026-09-22)

**Citations are plant-scoped (2026-09-25).** The RoDTEP and Advance Licence ledgers are company-wide,
but each citation names an import PO, its plant and its landed value, so `_license_citations()` keeps
only the plants the caller may read (`user_can_access_plant()`).

Both panels were built (2026-09-09) believing the import side of a licence was not knowable from any
synced source. `RodtepScrollEntry`'s docstring still says *"Drive has no structured link between a script
and which import it was later used against"*, and `RodtepUsage` - a hand-entered table behind a "Log
Usage" form - existed to carry that link.

**That premise was wrong, for both schemes.** Each plant's Imports Purchase Data CSV has carried
`License Type` and `License Number` per line item since the first import sync. Nothing ever joined on
them. Measured against real data: `license_type` is `''` on 43 rows, `'ADVANCE'` on 6, `'RODTEP'` on 3,
and **every RoDTEP number an import cites (`2603043916`, `2603044111`) is a Script No `RodtepScrollEntry`
already holds - a 100% join on an exact identifier**, far stronger than anything else in this app. The
hand-entry form had **never been used once** (0 rows).

`apps/services/license_links.py` is the one place that join lives - read its header before changing
either panel. Scheme classification is by substring (`classify_scheme()`: contains `RODTEP` / `ADVANCE`,
else unknown), so plausible future spellings still classify. Two normalisation rules, both forced by real
data:

- **Multi-licence cells split, behind a guard.** One line can name several licences slash-joined
  (`'0311051817/0311055303'`, and one real cell naming six). Separators are `/ , ; |` and newlines; the
  split only happens when **every** token is 7-12 digits, otherwise the cell stays whole, unmatched but
  intact. Unguarded splitting is how the multi-PO hyphen work went wrong before it was guarded - a cell of
  an unexpected shape shreds into tokens matching nothing, invisibly. **The hyphen is deliberately not a
  separator**, since it appears inside other identifier series in this data.
- **Zero-pad to 10 digits** (`LICENSE_NUMBER_WIDTH`). The CSV writes the same authorisation as
  `'311051817'` on one line and `'0311051817'` in a slash-joined cell on another; unpadded, the live data
  reads as 10 distinct authorisations where there are 9. Harmless for RoDTEP scrips.

`license_number_raw` on every citation is the verbatim cell. Citations come from **active POs only**.

**What the CSV gives and does not give.** It gives WHICH licence was applied to WHICH line (plant, PO,
BOE, material, landed value). It does **not** give the AMOUNT of credit debited, on either scheme. So:

- **RoDTEP shows no remaining balance.** `totalUsed` / `balance` read the legacy `RodtepUsage` table only,
  and the panel renders those columns **only when `summary.hasLoggedUsage`** is true. With the table empty
  they showed Balance == Sanctioned on every row, which reads as "none of this scrip has been spent" while
  the CSV says several imports were cleared under it.
- **Advance Licence utilisation is real**, because the owner's workbook carries `Value Imported` per usage
  row (`usage.valueImported`, `cifRemaining`, `cifUtilisedPct`). That number comes from the workbook,
  never from the CSV join.

**A line's landed value is never apportioned between the licences it names.** Each citation carries
`sharedWith` (the other licences on that line) and every rollup carries `sharedLines`, so the panel can
say the landed-value column is not additive. Inventing a split would put a made-up number next to real
ones.

**The insight each panel leads with is where a licence is NOT being used**: a scrip or authorisation we
hold that no import cites (`scripsNeverCited` / `neverCited`), a number an import cites that we hold no
file for (`unknownScrips` / `unknownLicenses` - the upstream fix, same shape as `mir_without_po`'s
`po_unknown` bucket, deliberately **not** folded in among real ledger rows), and a line naming a licence
with `License Type` left blank (`unclassifiedCitations`, returned by **both** ledgers since either reader
can act on it). Advance Licence adds `boeCrossCheck` (`workbookOnly` / `csvOnly`): a BOE one side records
and the other does not - a bookkeeping gap nothing else here could see. `_material_rollup()` takes each
material's **authorised** figures once (first non-null) and sums only the imported ones; the workbook
repeats the authorisation on every usage row, so summing it would multiply it by the number of imports.
`exportExpiringSoon` uses `_LICENSE_EXPIRY_SOON_DAYS = 90`, a stated review horizon, not a measurement.

**Both panels are read-only** (project owner: *"remove log usage and sync now from both the license tabs
and make them sync simultaneously like we have for csv's and refresh data"*). `POST
/api/imports/rodtep/usage` and its route are gone; **the `RodtepUsage` model and its read path stay** -
those rows record a human decision, and dropping a table is irreversible in a way removing a button is
not. The two `sync-trigger` endpoints stay: "Refresh Data" and `run_daily_sync_all_plants()` call them.

Validity countdowns use `timezone.localdate()`, never `date.today()`. The 30-day expiry **emails** are
`advance_license_report.py` (below and [auth-security-email.md](auth-security-email.md)).

### Days-Left and consumption

The consumption rate, days-of-cover and `daysToMsl` fields on each lot in `GET [<p>/]materials` come from
the consumption ledger via `_domestic_base._consumption_by_material()` (one query per plant). The
engine, the ledger, why the old Days-Left arithmetic was wrong, and the report emails are all in
[consumption.md](consumption.md).

### MIR entry (2026-09-28)

The project owner's direction: MIR moves into the app, one form for every plant, entered against the
POs the CSV already gives, and without the fuzzy matching and tolerances the Drive files forced - the
receipt is linked to the PO line the clerk picked, so every figure either agrees exactly or is a
mismatch with a reason. PO extraction and PDF upload are on hold; the PO master CSV stays the only PO
source. The Drive MIR files and the existing matching keep running untouched: MIRs entered here are
separate records (`Mir` / `MirLine`, [architecture.md](architecture.md#appscoremodelsprocurementpy)),
so nothing is counted twice while both exist.

**The flow.** The clerk picks the receiving plant and searches open POs by **PO number only** (owner,
2026-09-29 - a vendor-name search offered every open order of that vendor, which is how a receipt lands
on the wrong one). **Every
plant's open POs are offered** (owner rule: any plant's store may receive any plant's PO), then lines
of one or several POs of **one vendor** (one MIR is one vendor's invoice). Opening a PO shows its header
as the purchase team raised it (PO date, payment terms, incoterms, currency, tax type, GST %, values,
Bill To, Ship To, vendor address, remarks) above its lines. Per MIR the clerk types the invoice number,
date and total, TCS, the tax type, an optional SAP GRN number and optional transport details; per line
the quantity received and rejected (in the PO line's own unit), the invoice rate (in the PO's currency
per that unit), discount, GST %, the **material category** (required) and sub-category (optional) from
`MaterialCategoryReference`'s list - prefilled by `mir_service.suggested_categories()`, the PO line's
item code against the list's SAP item code first, then its description - and department use. Required
fields carry a red asterisk, each figure has a one-line hint, optional fields (SAP GRN, transport,
remarks) are folded into one section, and a sticky save bar lists what is still missing. Freight/packing, rolls and batch are not on the form (owner, 2026-09-29);
their `MirLine` columns stay, at 0 / blank. The page sends the form to `preview` on every change and
paints what comes back; it never computes a figure itself. Saving runs the same check again inside the
transaction.

**The invoice number is required but not unique** (owner, 2026-09-29). One invoice can arrive as
several deliveries, each its own MIR - the Drive MIR files do this (one BST Elastomers invoice on five
HRS MIRs). So earlier POSTED MIRs of the same vendor invoice in its financial year, at any plant, come
back from `evaluate()` as `notices` ("Invoice X is already on HRS/26-27/0003 ... Save only if this is
another delivery"), shown in blue above the lines, never as an error. Invoice numbers are compared
after upper-casing, removing spaces and leading zeros (`invoice_key()`); there is no database
constraint on them (migration `0076` dropped it).

**What is computed** (`procurement_rules.line_amounts()`): gross = qty x rate, taxable = gross -
discount (+ `other_charges`, always 0 from the form), GST on taxable - IGST, or half CGST and half SGST/UGST - each tax rounded
on its own as invoices print them. The tax type follows the states: the vendor's GSTIN state against
the RECEIVING plant's (HRS 26, a union territory, so UGST; Achhad 27; Vapi 24), falling back to the
PO's tax type when the vendor has no GSTIN. The GST field defaults to the rate the PO's two totals
imply when it lands on a slab.

**What needs a reason** (a `MirReasonCode` of the matching kind, plus a note where the reason says so):

| Difference | Compared | Kind | Effect of the reason |
| --- | --- | --- | --- |
| Accepted quantity below the open quantity | accepted = received - rejected, exactly | `QTY_SHORT` | "balance to come" keeps the line open; "close the line" short-closes it |
| Accepted quantity above the open quantity | exactly - there is no tolerance, weighbridge included | `QTY_OVER` | recorded; the line reads received |
| Any rejected quantity | above zero | `QTY_REJECTED` (reasons of kind `REJECTION`) | recorded, for the return / replacement / debit note |
| Invoice rate different from the PO rate | at 4 decimals | `RATE_HIGH` / `RATE_LOW` | recorded |
| GST % different from the one the PO's totals imply | only when the PO's two totals land on one slab (`po_gst_rate()`) | `GST_RATE` | recorded |
| Invoice total more than Rs 1 from the computed total | the rupee an invoice rounds to | `INVOICE_TOTAL` | recorded |
| Tax type other than the states imply | | `TAX_TYPE` | recorded |

Each becomes a `MirMismatch` that stays `OPEN` until a purchase manager resolves it with a note. A
rejection with a shortfall asks two questions, so it carries two differences: why the goods were
rejected (`QTY_REJECTED`) and whether the balance is still coming (`QTY_SHORT`). The reason list lives
in one place, migration `0068`'s `REASONS` (41 reasons across the seven kinds); migration `0076`
re-runs its `seed()` so a database that already applied `0068` gets the new ones, and a reason is never
deleted (a posted mismatch points at it with `PROTECT`).

**Open mismatches is the purchase team's follow-up list.** The store never has to wait on it: a MIR
with differences is saved with the reasons chosen, and each difference sits in the Open mismatches
tab (count on the tab) until someone chases the balance, raises the debit note, amends the PO or
returns the material, then marks it resolved with a note saying what was done.

**What is refused outright**: a receiving plant the account may not edit (403); a MIR date in the
future or before a line's PO date; an invoice date after the MIR date; a PO line that is retired,
dropped from the sheet, closed, waiting for review, without a quantity or rate, or already received in
full; the same line twice; two vendors on one MIR; a PO with no vendor and none chosen; a GST rate off
the slabs; a discount above the line's value; a line with no material category, or a category or
sub-category not on the reference list (when the list has any rows).

**Received so far is never stored.** It is summed from posted MIR lines, so cancelling a MIR returns
its quantity at once, voids its open mismatches and reopens any line it closed. Posting locks the PO
lines first (`select_for_update`, id order), so two receipts of one line cannot both count the same
open quantity. MIR numbers are per plant and financial year (`HRS/26-27/0001`), gapless, and a
cancelled MIR keeps its number. Who entered, cancelled or resolved is stored as a user link and the
email, so the record survives the user's deletion.

**Not yet done, on purpose:** MIRs entered here do not feed the reconciliation dashboard's matching
(that would count a receipt twice while the Drive MIR files still carry it); a PO missing from the
master CSV cannot be received against (the search says to ask purchase to add it); RM stock is next.

### Sort presets (2026-09-28)

Raw Material Analysis (and its modal's Stock by Plant and Open Purchase Orders tables), Domestic
Purchase Orders, Import Purchases, the PO modals' line cards and MIR receipts, and the Search PO
page's results and line items sort by any column,
in up to 5 levels ("Category, then Sub Category, then Material"), and a user can save a sort as a
named preset **to their account**, kept per list (`view`: `materials`, `material_lots`, `material_open_pos`, `purchase_orders`, `import_purchases`, `po_lines`, `po_receipts`, `search_po`, `search_po_items`), so it follows them to
any device (owner's choice over browser-only or shared presets). A level on a pickable column
(Category, Sub Category, Material, Vendor, Plant, Country) can also **pick values** - those rows
first, in the picked order - and **"Show only these"**, which narrows the list to them; each level's
choices are scoped to the picks above it, so Category -> Sub Category -> Material pins down one
material's orders (2026-09-28, project owner: "go to least granularity"). Built-in sorts (Category
alone, Sub Category alone, Category then Sub Category then Material, and a few more per list) live
in the frontend, not the database. Every role may save presets, viewers included: a preset is the caller's own display
preference, not a write to business data. Every query filters on the caller, so another user's id
reads 404. Nothing in a preset is trusted - `sort_presets.clean_levels()` accepts only the view's
known column keys, each once, asc or desc, at most 5, and picked values only on the view's pickable
columns (`PICK_KEYS_BY_VIEW`), as distinct non-empty strings of at most 200 characters, at most 50
per level, with a boolean `only` (an empty pick list is dropped with its `only`); a name is 1-60
characters; at most 25 presets per user per view. Saving under an existing name (ignoring case and repeated spaces) saves over it,
Excel-style; the page asks first. The server's key lists must match the frontend's
(`material-sort.js`'s `MAT_SORT_COLUMNS`, `MAT_LOTS_SORT_COLUMNS` and `MAT_OPEN_PO_SORT_COLUMNS`,
`po-sort.js`'s `PO_SORT_COLUMNS`, `import-sort.js`'s `IMPORT_SORT_COLUMNS`, `po-reconcile.js`'s
`PO_LINES_SORT_COLUMNS` and `PO_RECEIPTS_SORT_COLUMNS`, `search-po-page.js`'s `SEARCH_SORT_COLUMNS` and
`SEARCH_ITEMS_SORT_COLUMNS`), keys and `pick: true` columns both;
`test_sort_presets.py` checks each pair. The frontend side is in [frontend.md](frontend.md#frontendjslist-sortjs).

### Data export

An "Export Data" button next to "Refresh Data" opens a panel (`export-panel.js`) that downloads the full
daily RM stock snapshot history as CSV via `GET [<p>/]stock-snapshots/export?from=&to=`, both dates
optional.

**Deliberately scoped to this one dataset.** POs and Import POs are fully present in their own source
CSVs, but the Stock xlsx files only ever hold *today's* position, so the `*RMSnapshot` table is the
**only** place day-by-day history exists - it cannot be reconstructed afterwards. Extending Export to
other datasets is a deliberate follow-up if asked for, not assumed.

- **CSV, not Excel** - per "whichever is less compute and faster". A `.xlsx` needs a library for no real
  benefit.
- **`IsEditor` + `user_can_access_plant()`-gated**, unlike every other GET in `_domestic_base.py` (plain
  `IsAuthenticated`, any role). An explicit, deliberate exception, not an oversight.
- **It streams.** `StreamingHttpResponse` over a generator (`_Echo` + `SafeCsvWriter`, queryset
  `.iterator(chunk_size=2000)`), so peak memory is one row - on a table that grows by one row per active
  lot per day forever, where "export everything" is the default. *Behavioural note:* no `Content-Length`
  (indeterminate progress bar), and **any test or client reading an export must use
  `.streaming_content`**, not `.content`. An exception mid-stream cannot become a 500 (headers are
  already sent), so the generator only formats rows.
- **Every string cell is escaped against CSV formula injection.** Descriptions, categories and vendor
  names come from hand-edited Drive sheets; a cell starting `=`, `+`, `-`, `@` (or tab / CR, which Excel
  strips first) executes as a formula when opened, and opening in Excel is the whole point. `csv_safe()`
  prefixes a single quote and is applied through the `SafeCsvWriter` **wrapper rather than per-column
  calls**, so a new column is protected by construction. **Use `SafeCsvWriter` for any new export** (the
  no-PO CSV and the review-stats CSV already do). Non-string values pass through untouched - quoting a
  Decimal turns quantities into text Excel won't sum.
- **Download is a plain `window.open()`, not `fetch()` + blob** - the httpOnly auth cookie rides a
  same-origin navigation and `Content-Disposition` does the rest; a new tab means a stale-session error
  shows there instead of blowing away the dashboard.
- The button renders only when `PLANT_KEYS.some(canEditField)`, and each plant's row is gated
  individually.

Columns: Plant, Snapshot Date, Material Description, Material Code, Category, Sub Category, Vendor, UOM,
Opening Stock, Received, Issued, Today's Stock, Rate, Value, Lot Currently Active. Filename
`<plant>_rm_stock_snapshots[_<from|start>_to_<to|latest>].csv`. A bad date is a 400.

---

## File reference

### apps/api/urls.py

Wires every `/api/` path to its view (table above). Domestic plants each get their own URL prefix rather
than a `?plant=` parameter, so URLs stay greppable and bookmarkable; Imports and Review are cross-plant.
Includes `device_urls`, `google_oauth_urls` and `users_urls` at the root. `review/stats` is declared
before `review` for readability only; ordering is not load-bearing.

### apps/api/routers/_domestic_base.py

The one implementation behind the three domestic routers. Each `make_*` factory takes a `_PlantConfig`
and returns a DRF view; the plant files call them once at import time. Per-plant differences are
expressed only as config values, never as branches inside this module.

- **`_PlantConfig`** (frozen dataclass): `key` (`hrs` / `achhad` / `vapi`, the access and sync-lock
  key), `syncrun_plant`, the seven model classes (PO, line item, MIR, PO↔MIR match, MIR↔Stock match,
  lot, snapshot), `run_full_match`, `match_config` (the plant's `MATCH_CONFIG`, used by
  `mir_without_po` so the drill-down uses the matcher's own idea of a known PO number),
  `material_editable_fields` / `material_decimal_fields` / `material_rematch_trigger_fields`,
  `lot_rate_field`, `lot_code_field`, `lot_vendor_field` (None at Achhad), `daily_movement_model` (Achhad
  only; not read by the request path any more).
- **`csv_safe()` / `SafeCsvWriter` / `_Echo`**: formula-injection escaping for exported cells, a
  `csv.writer` wrapper that applies it to every cell, and the file-like object whose `write()` returns
  the line so a generator can stream it. Imported by `review_views.py` too.
- **`_coerce_value()` / `_coerce_material_value()`**: blank → None, ISO dates, `Decimal` with
  `InvalidOperation` re-raised as `ValueError`. `_field_warning()`: GSTIN / email warnings after save.
  `_serialize()`: dates to ISO, Decimals to float. Both shared with `imports_views.py`.
- **`_counted_mirs(match, po_uom)`**: every MIR row a PO↔MIR match counted (its `group_entries`, else its
  `mir_entry`), oldest first, with qty in the PO line's unit, and a `received` total from
  `matching_core.received_against_line()` - the matcher's own unit conversion, so the card and the flags
  agree. Reads the prefetched `group_entries`. `_match_config_for()` finds the right `MATCH_CONFIG` from
  the match's model class (lazy import).
- **`_line_item_dict(item, item_ref)`**: the per-line payload (`itemRef`, match ids / tier / score,
  diff percentages, `qtyOverDelivered` tri-state, `qtyWithinTolerance` (weighed material accepted up
  to 10% over - [flag thresholds](matching-engine.md#flag-thresholds); the import payload carries it
  too), `rollsOrdered` / `rollsReceived` (Madura roll counts, null when not stated), `poolLineRefs`
  (identical lines it is pooled with, "" when none; both payloads), split mismatch booleans, `severity`, `matchedMirNo`,
  `matchedMirs`, `received`, `stockMatched`, dismissal fields). `vendorMatched` defaults to true where the
  column does not exist. `_categorized_line_item_dict()` adds the line's canonical category.
- **`_po_dict()`**: the PO payload; line items numbered in pk order (the matcher's numbering). Takes
  pre-batched maps for corrections, flag dismissals and data-quality flags (line-item flags plus flags on
  each matched MIR entry); falls back to per-PO queries only when called standalone.
- **`_lot_dict()`**: the lot payload. Category / sub-category are the canonical
  `MaterialCategoryReference` lookup (the raw column is too noisy to group by). Consumption is per
  material (shared by sibling lots, **never summed across them**); `daysLeft` is this lot's own cover.
  `daysToMsl` (Achhad `msl` only) is 0 the moment stock is at or below MSL, independent of any rate.
  `locationTag` is HRS `location_tag` or Vapi `plant_tag`. `mirStockMatches` lists each match's flags
  and `uomMismatch`.
- **`make_purchase_orders`**: active POs with one prefetch set and 4 batched side queries total (not ~4
  per PO) - the N+1 fix.
- **`make_correct_field` / `make_correct_material_field`**: allow-listed inline correction, atomic
  mutate + audit row, synchronous re-match for trigger fields.
- **`make_materials`**: active lots ordered by value, one consumption query, one category-reference
  query, two batched side queries.
- **`make_stock_trend` / `make_stock_snapshot_dates` / `make_stock_snapshots_for_date`**: snapshot reads.
  The by-date view defaults to the latest date and returns **404 with `availableDates`** rather than an
  empty 200, which a client would render as "zero stock everywhere".
- **`make_export_stock_snapshots`**: the streaming CSV export ([Data export](#data-export)).
- **`make_mir_without_po`**: rows from `services/mir_without_po.py`, bucketed. Unknown `?bucket=` is a
  **400** (an empty list reads as "nothing to fix"). `?download=csv` builds the CSV eagerly (bounded by
  the MIR table), including the MIR sheet row. The summary is always built from the unfiltered rows so
  every tab shows its count. Deliberately uncached.
- **`_cached_mir_without_po_summary()`**: the same summary behind a 60-second per-plant cache, for
  `sync_status` only (135 / 53 / 163 ms against a ~20-30 ms endpoint polled every 60 s). The cached value
  is plant data, not a response; the permission check still runs per request.
- **`make_sync_status`**: latest `SyncRun` per source, `mirEntryCount`, `noPoVendors`,
  `purchasesWithoutPo`, `mirWithoutPo`, `rmUntracked`, `retiredPoCount`, `syncInProgress`,
  `lastSnapshotDate`, `snapshotGapDays`. Sets `Cache-Control: no-store` itself (a heuristically cached
  response once showed "syncing" after a sync had finished). The registries behind these counts are in
  [matching-engine.md](matching-engine.md).
- **`make_sync_trigger`**: `trigger_plant_sync(cfg.key)`; 202 `started` or 409 `already_running`.
- **`make_dismiss_po_mir_match` / `make_dismiss_mir_stock_match` / `make_dismiss_flag`**: thin wrappers
  over `match_dismiss` / `flag_dismiss`.
- **`_sheet_row()`, `_mir_row_dict()`, `_claims_for_mir_numbers()`, `_held_mir_numbers()`,
  `_item_refs_for_pos()`**: the manual-pin picker's building blocks, shared with imports.
  `_held_mir_numbers()` yields each MIR document a match holds once, primary or grouped.
- **`candidate_entries()` / `order_candidates()`**: one picker entry per MIR number, then grouped
  most-likely first with `group` / `onThisLine` / `why` from `manual_receipts.candidate_insights()`;
  shared with imports.
- **`change_from_request()` / `resolve_line()` / `_pin_response()`**: a manual-change body read into
  `apply_change()`'s terms (the older no-`action` body mapped to `set` / `notReceived` / `auto`), the
  PO line at an `itemRef`, and the save response; shared with imports.
- **`make_mir_candidates` / `make_set_mir_match`**: the domestic picker and save endpoints. The save
  goes through `manual_receipts.apply_change()` with `po_kind=domestic` (see
  [Editing which MIR a PO line matched](#editing-which-mir-a-po-line-matched-2026-09-21) and
  [Editing a line's receipts one at a time](#editing-a-lines-receipts-one-at-a-time-2026-09-29)).
- **`make_manual_changes` / `make_preview_mir_match` / `make_preview_status`**: the line decisions
  listing, and the preview's queue and status endpoints (editor-only; a preview id answers only on
  its own plant).
- **`_request_bool(value, default)`**: reads a request-body flag as a real bool (a string by its
  meaning, missing or null as `default`). Used for every `dismissed` flag, domestic and import.

### apps/api/routers/hrs_views.py

Builds HRS's `_PlantConfig` and re-exports the factory-built views under the names `urls.py` uses.
Differences: `SyncRun.Plant.HRS`, `apps.services.matching`, `lot_rate_field="basic_rate"`,
`lot_code_field="sap_item_code"`, `lot_vendor_field="party_name"`; editable lot fields include
`sub_category`, `uom` and `party_name`. No URL prefix.

### apps/api/routers/achhad_views.py

RTP-Achhad's config (prefix `achhad/`). The most divergent plant: no vendor column
(`lot_vendor_field=None`), no `sub_category` / `uom`, a real `msl` column (editable, never a re-match
trigger), `lot_rate_field="rate"`, `lot_code_field="sap_code"`, and `daily_movement_model` set to
`RTPAchhadRMDailyMovement` (read by the consumption build, not by these views). Matcher:
`matching_achhad`.

### apps/api/routers/vapi_views.py

RTP-Vapi's config (prefix `vapi/`). HRS-shaped with its own vendor column
(`lot_vendor_field="supplier_name"`), `lot_code_field="hsn_code"`, and `batch_no` editable in place of
`sub_category` (the sheet renamed that column on 2026-09-12). Matcher: `matching_vapi`.

### apps/api/routers/imports_views.py

The cross-plant Import Purchase router, plus the RoDTEP and Advance Licence ledgers and the import pin
endpoints.

- **`_PLANTS`, `_RUN_FULL_MATCH`, `_MIR_MODEL`**: plant key → models / label / match model, → that
  plant's `run_full_match`, → that plant's MIR model (imports match against the plant's domestic MIR).
- **Allow-lists**: `_PO_EDITABLE_FIELDS`, `_ITEM_EDITABLE_FIELDS`, `_DATE_FIELDS`, `_DECIMAL_FIELDS`,
  `_REMATCH_TRIGGER_FIELDS` (BOE qty in place of domestic qty). Its own `_coerce_value()` because the
  field sets are a superset.
- **`_mir_match_dict()` / `_item_dict()` / `_po_dict()`**: the payload. `_mir_match_dict()` adds
  `orderedRateInr` / `orderedValueInr` from `_import_rate_value_inr()` and reads optional columns with
  `getattr` defaults. `_item_dict()` adds `shipmentStage`, `qtyDiscrepancy[Pct]`, `deliveryDateStatus`
  (from `import_flags`, with `timezone.localdate()`). `_po_dict()` adds the PO-level
  `shipmentStage`, `deliveryDateStatus`, `partialDelivery`, `materialInwarded` and `qtyDiscrepancy`,
  plus `currency` ("Currency (As Per PO)", the currency every line's `netPrice` is in - the PO list's
  Rate column and the modal's currency dropdown read it from the list row);
  `_po_dict(detail=True)` adds vendor / billing fields, corrections and flag dismissals.
- **`purchase_orders`**: all readable plants' active POs, sorted newest first; out-of-scope plants are
  skipped silently. **`purchase_order_detail`**: 404 for unknown or out-of-scope plant; does not filter
  `is_active`.
- **`correct_field`**: import inline correction; 404 unknown plant, 403 out of scope; does not filter
  `is_active`.
- **`sync_status`** (no-store), **`sync_trigger`** (queued, 202 / 409), **`track_bl`** (400 without
  `bl`, 502 on any SafeCube failure, otherwise SafeCube's payload as-is).
- **`dismiss_import_po_mir_match` / `dismiss_flag`**: as the domestic ones, parametrised by plant.
- **RoDTEP**: `_boe_exists()` (does any plant's import line carry this BOE - `boeVerified`),
  `_license_citations(scheme)` (one pass returning grouped citations plus unclassified ones),
  `_unrecognised_license_rows()`, **`rodtep_ledger`** (one aggregate over `RodtepScrollEntry` per script,
  `RodtepUsage` totals, citations, `unknownScrips`, `unclassifiedCitations`, `lastSync` with
  `errorDetail`), **`rodtep_script_detail`** (resolves a scrip the CSV cites even with no ledger file,
  404 only when nothing at all is known), **`rodtep_sync_trigger`** (synchronous, IsAdmin).
- **Advance Licence**: `_advance_license_material_dict()`, `_material_rollup()` (authorisation taken once,
  imports summed), `_advance_license_dict()` (usage, validity countdowns, citations, `boeCrossCheck`),
  **`advance_license_ledger`**, **`advance_license_sync_trigger`**.
- **Pins and receipt edits**: **`mir_candidates`** (404 for unknown or out-of-scope plant; merges
  domestic claims via `_domestic_cfg_for()` with `_import_claims_for_mir_numbers()`, then
  `order_candidates()` against the plant's `match_config`), **`set_mir_match`** (`apply_change()` with
  `po_kind=import`), **`manual_changes_view`**, **`preview_mir_match`**, **`preview_mir_match_status`**.
  `_domestic_cfg_for()` imports the plant view modules lazily to avoid a circular import.

### apps/api/routers/review_views.py

The match-accuracy review queue and accuracy report endpoints.

- **`_GROUPS`**: every (match type, plant) pair. **`_ACCESS_KEY`**: `SyncRun.Plant` → access key;
  `_can_review()` wraps `user_can_access_plant()`.
- **`_ref()` / `_signal()`**: one identifying field (`kind` `text` / `date` / `num`) and one matcher
  evidence pill (`yes` / `no` / `warn`). **`_po_mir_sides()`** builds both sides of a PO↔MIR or import
  card (import figures converted to INR, BOE qty); **`_mir_stock_sides()`** builds a MIR↔Stock card (lot
  qty and value deliberately `None`, since stock qty is not comparable).
- **`_pick_one()`**: a random (`order_by("?")`) match from one group excluding reviewed and already-picked
  ids.
- **`next_review`**: shuffles the caller's readable groups and picks round-robin until 5 or exhausted;
  `{"done": true}` only when every group is empty; `_reviewed_match_ids()` excludes rows holding an
  already-reviewed pair. **`submit_review`**: validates plant (400), scope (403), match type, verdict,
  integer id, match existence (404); creates the row with the match's pair (`match_pairs.pair_of()`);
  201. **`undo_review`**: deletes only the caller's own row. **`_progress()`**: distinct reviewed pairs
  (keyed by `verdict_key()`, like the report) against `REVIEW_TARGET`.
- **`review_stats`** / **`export_review_stats`**: `build_report()` as JSON, and its tables as one CSV
  with a `section` column and a `small_sample` flag (`match-accuracy-<date>.csv`).

### apps/api/routers/users_views.py

Admin user management (see [In-app user management](#in-app-user-management)).
`_validate_password_strength()`, `_clean_plants()` (only `hrs` / `achhad` / `vapi`), `_hash_password()`
(bcrypt, `rounds=12`), `_user_out()`. `create_user` forces admin `plants=[]` and audit-logs;
`update_user` handles PATCH and DELETE on one URL, runs `_assert_not_last_active_admin()` (locks **every**
active admin row, so two concurrent demotions always contend) inside `transaction.atomic()`, and calls
`revoke_all_tokens()` after a password reset. `list_user_devices` never returns the device token hash;
`revoke_user_device` and `admin_logout_everywhere` audit-log. Security rationale in
[auth-security-email.md](auth-security-email.md).

### apps/api/routers/users_urls.py

Maps the six `auth/users...` paths to `users_views`. Path segments, not trailing slashes, distinguish
list from create.

### apps/api/routers/admin_overview_views.py

`GET auth/admin-overview` (IsAdmin). `correction_counts_by_email()` (public, also used by `list_users`)
tallies the three correction tables; `_top_correctors()` / `_top_vendors()` (top 5, domestic POs only) /
`_recent_activity()` (last 10 corrections across all three tables, merged and re-sorted).

### apps/api/routers/reports_views.py

Shared-secret endpoints for the external scheduler (cron-job.org), since the caller has no session.
`_check_report_secret()` accepts `X-Report-Secret` (preferred), `?secret=` or a body `secret`, compares
with `hmac.compare_digest`, and answers 503 when `REPORT_CRON_SECRET` is unset, 403 otherwise.
`_report_response()` turns any `failures` into a **502 `partial`** so the scheduler flags a partly failed
run (daily and monthly only; the mismatch and licence endpoints always 200). `trigger_monthly_report`
requires `year` and `month` together, as integers, month 1-12. Schedules, dedup and the "never use a
test run" rule are in [auth-security-email.md](auth-security-email.md) and
[testing-deployment.md](testing-deployment.md).

### Not in this document

`device_urls.py`, `device_views.py`, `google_oauth_urls.py`, `google_oauth_views.py`, `password_views.py`
are documented in [auth-security-email.md](auth-security-email.md).

### apps/services/match_accuracy.py

The single scoring implementation behind `review/stats`, its CSV and `report_match_accuracy`.
`CONFIGS` (plant → `MATCH_CONFIG`), `PLANT_LABELS`, `MATCH_TYPE_LABELS`, `MIN_SAMPLE = 5`,
`REVIEW_TARGET = 200`. `model_for(config, match_type)` picks the match model. `verdict_key(review)` is
(plant, match type, left id, right id), or (plant, match type, None, match id) for a verdict with no
recorded pair; `latest_verdicts()` keeps the most recent verdict per key. `scores()`: precision = correct / (correct +
incorrect), recall = correct / (all three), F1, each `None` when undefined. `_match_rows()` resolves tier
and `coverage_band()` (`n/a` until a `field_coverage` column exists) with one query per group, via
`match_pairs.rows_for_pairs()`, and drops verdicts whose pair no row holds. `build_report(note_limit=25)` returns overall, `byPlant`, `byMatchType`,
`byPlantAndType` (all 9 cells), `byTier`, `byCoverage`, `staleVerdicts`, reviewer counts and recent
notes. Only reviews of existing matches are ever scored, so this is not recall over missed matches.

### apps/services/match_dismiss.py

`dismiss_match(model_cls, match_id, user, dismissed, reason, *, plant, match_type)`: sets or clears
`dismissed_by_override` and its three audit columns on any `*POMirMatch` / `*ImportPOMirMatch` /
`*MirStockMatch`, and in the same transaction writes or deletes the pair's `MatchDismissal`. Returns the
row or `None`. Clearing wipes the audit columns. Does no plant check itself; callers do.

### apps/services/match_pairs.py

The identity of a match for the decisions people record about it. `PAIR_FIELDS` maps each match type to
its (left, right) id fields: `po_line_item_id` / `mir_entry_id` for both PO kinds, `mir_entry_id` /
`stock_lot_id` for MIR↔Stock. `pair_of(match_type, match)`; `rows_for_pairs(model, match_type, pairs,
queryset=None)` returns {pair: row} in one query; `restore_dismissals(config, match_types)` copies this
plant's `MatchDismissal` records onto undismissed rows holding their pair, called from
`run_full_match()` inside its transaction.

### Minor API rules from the 2026-09-25 audit

- `GET <prefix>/purchase-orders/summary` (`make_purchase_order_summary()`) - each active PO's vendor
  and created date only, for Home's and Admin's KPI cards; plant-scoped like the full list.
- The pin endpoints read `clear` with `_request_bool()`, like every other body flag.
- The import match payload carries `dismissedBy` (prefetched with the match).
- The import flag-dismiss endpoint returns 404 for a PO that does not exist.
- The RoDTEP and Advance Licence ledgers load the BOE-number set once (`_known_boe_numbers()`) and pass
  it to `_boe_exists()`: per citation it was up to three one-row lookups (48 queries per Advance
  Licence load, now 16).

### apps/api/routers/mir_views.py

The MIR endpoints above. Gates, parses and serializes only; every rule is in `mir_service`. Money and
quantities are returned as strings, never floats. `_po_line()` / `_po_summary()` / `_mir_detail()` /
`_mismatch()` are the payload shapes; `_readable_plants()` filters reads; `_receiving_plant_allowed()`
gates preview and post. The three PO lookups are cross-plant by owner rule and listed as such in
`test_endpoint_permission_guard.py`. `entries` orders explicitly: Django ignores `Meta.ordering` on
its aggregate query.

### apps/api/routers/preferences_views.py

The sort-preset endpoints above: `presets` (GET list / POST save) and `preset` (PATCH / DELETE), both
`IsAuthenticated` declared explicitly (every role; see [sort presets](#sort-presets-2026-09-28)),
both filtered on `user=request.user`. Validation is in `apps/services/sort_presets.py`.

### apps/services/sort_presets.py

`SORT_KEYS_BY_VIEW` (the columns each view sorts on - keep each equal to its frontend list: `materials`
to `material-sort.js`'s `MAT_SORT_COLUMNS`, `material_lots` / `material_open_pos` to its
`MAT_LOTS_SORT_COLUMNS` / `MAT_OPEN_PO_SORT_COLUMNS`, `purchase_orders` to `po-sort.js`'s
`PO_SORT_COLUMNS`, `import_purchases` to `import-sort.js`'s `IMPORT_SORT_COLUMNS`, `po_lines` /
`po_receipts` to `po-reconcile.js`'s `PO_LINES_SORT_COLUMNS` / `PO_RECEIPTS_SORT_COLUMNS`, `search_po` /
`search_po_items` to `search-po-page.js`'s `SEARCH_SORT_COLUMNS` / `SEARCH_ITEMS_SORT_COLUMNS`) and
`PICK_KEYS_BY_VIEW` (the `pick: true` columns), `MAX_PICKED_VALUES` / `MAX_PICKED_VALUE_LENGTH`
(mirrored in list-sort.js), `clean_view()` / `clean_name()` / `clean_levels()` / `_clean_picks()`
(raise `ValueError`, which becomes a 400), `list_presets()`, `save_preset()` (create or save over the same name; locks the
user's row so two tabs cannot both pass the 25-preset cap) and `update_preset()` (rename refuses a
name the user already has).

### apps/services/mir_service.py

`evaluate(payload, lock=False)` - the one check-and-price of a MIR (header, vendor, earlier MIRs of the
same invoice as `notices`, tax type, lines, categories, mismatches, invoice total); returns errors,
notices and figures, never saves. `category_options()` - {category: [sub-categories]} from
`MaterialCategoryReference`; `suggested_categories(po_lines)` - each line's (category, sub-category) by
item code, then by `normalize_material(description)`.
`post_mir(payload, user)` - `evaluate(lock=True)` then saves `Mir`, `MirLine`s, `MirMismatch`es and any
line closure in one transaction; the MIR number comes from `_next_seq()` (row-locked `MirSequence`), so
two MIRs posted at once at one plant get consecutive numbers. `cancel_mir()`,
`resolve_mismatch()`, `close_po_line()`, `reopen_po_line()`, `clear_line_review()` - the other writes,
each row-locked and requiring a reason or note. `accepted_by_line()` / `line_state()` - received so far
and whether a line can take a receipt. `search_open_pos()` - open POs whose PO number contains the query.
`MirValidationError.errors` is `[{field, message}]`, the field in the payload's own terms
(`lines.0.qty_reason`).

### apps/services/procurement_rules.py

Pure rules, no Django imports: `canonical_uom()` (spellings of one unit folded, KG and MT kept apart,
an unknown unit kept and flagged), `clean_gstin()` / `gstin_state()`, `canonical_tax_type()`,
`expected_tax_type()`, `po_gst_rate()`, `GST_SLABS` / `is_gst_slab()`, `financial_year()`,
`mir_number()`, `invoice_key()`, `vendor_name_key()`, `line_amounts()`, `rate_differs()`, `pct_of()`,
`INVOICE_ROUNDING_TOLERANCE` (Rs 1).

### apps/services/procurement_sync.py

`project_plant_orders(plant_code)` - see [data-sync.md](data-sync.md#po-csv-into-the-procurement-tables-2026-09-28).
`upsert_vendor()` - one `Vendor` per GSTIN (or cleaned name without one); the newest PO's details win,
a blank never erases. `LEGACY_PO_MODELS` names each plant's CSV mirror model.

### apps/services/rematch.py

`request_rematch(plant_key)` queues `run_rematch()` on the qcluster (`async_task`), or joins the run
already queued (`cache.add` on a pending key); `run_rematch()` clears that key FIRST, so a save made
while a run is under way queues the next one rather than being folded into a run that may have read
the old data; it records `{state, startedAt, finishedAt, unfilledPins, stalePins, manualPinsApplied,
unfilledEdits, staleEdits}`
(or `error`) for `status(plant_key)`, which `sync-status` serves as `rematch`. `status()` also carries
`queuedAt` and `stalled`: True once a queued run has waited more than `_STALL_SECONDS` (120) without
the worker starting it - the qcluster is down - so the page stops waiting and says so. `_inline()` runs it in
process under pytest. Two runs of one plant never overlap: `matching_core.run_full_match()` takes a
per-plant `pg_advisory_xact_lock` (`_lock_plant_match()`), so a queued re-match and the hourly sync's
match of the same plant run one after the other.

### apps/services/manual_receipts.py

The one writer of manual receipt decisions (pins and `ManualReceiptEdit`s) for both routers.
`apply_change()` applies one `add` / `remove` / `notReceived` / `auto` / `undo` / `set` to one line,
keeping pins and edits consistent, and raises `ValueError` with the reader's message.
`manual_changes()` lists a PO's decisions plus `elsewhere` (decisions on other orders naming a receipt
that cites this one, confirmed with `_names_this_po()`). `candidate_insights()` groups each offered
MIR document and explains, from `_forced_candidate()` evidence, why the matcher is not counting it on
the line; `_one_edit_apart()` spots a PO number one digit off. See
[Editing a line's receipts one at a time](#editing-a-lines-receipts-one-at-a-time-2026-09-29).

### apps/services/receipt_preview.py

The preview of a manual change. `request_preview()` stores a `queued` state under a fresh id and
queues `run_preview()` on the qcluster (inline under pytest, via `rematch._inline()`); `status()` adds
`stalled` past `rematch._STALL_SECONDS`. `compute()` runs `run_full_match(dry_run=True)` twice inside
rolled-back transactions (as things stand, then with `apply_change()`), `_snapshot()`s every active
line's receipts and received quantity (import lines on BOE qty, share-scaled), and returns the lines
that moved plus the edited order's, with `unfilled` when the run reported the change unfilled. A
`ValueError` from `apply_change()` becomes `failed` with its message.

### apps/services/data_stamp.py

`touch(plant)` / `read(plant)` - when a plant's data last changed outside a sync, in the default cache
(the shared DatabaseCache). Touched by every correction, dismissal and pin endpoint and at the end of
`run_full_match()`; served as `sync-status`'s `dataChangedAt`, which the freshness watcher folds into
its stamp. Before it, a colleague's correction or pin wrote no SyncRun row and so reached no other open
dashboard until the next hourly sync. Losing the key only means one missed change.

### apps/services/flag_dismiss.py

`dismiss_po_flag(plant, po_number, flag_key, user, dismissed, reason)`: touches `data_stamp`, then upserts the `FlagDismissal` row
for that key with the same audit fields and clear-on-undo rule. `flag_key` is opaque here.

### apps/services/license_links.py

The one join between import line items' `license_type` / `license_number` and the two ledgers. Read-only,
recomputed per request, active POs only. `classify_scheme()` (substring), `normalize_license_number()`
(zero-pad digits to 10, else trim + upper), `split_license_numbers()` (guarded split, de-duplicated in
source order), `LicenseCitation` (frozen dataclass: one line × one licence, with `shared_with`),
`collect_citations(scheme=None)` (three queries, one per plant), `citations_by_license()`,
`citation_totals()` (`lineCount`, `poCount`, `plants`, `boeNumbers`, un-apportioned `landedValue`,
`sharedLines`). Imports no API code, so it is callable from anywhere.

### apps/services/advance_license_report.py

The Advance Licence validity-expiry emails behind `internal/send-advance-license-expiry-report`.
`_claim_expiring_licenses(kind, today)` selects licences whose import or export validity date is in
`[today, today + 30]` and claims a `ReportSendLog` row keyed `"<license_number>@<validity date>"` per
licence, skipping any already claimed - so each deadline alerts once, and an **extended** validity
re-arms. Materials join into one sorted, semicolon-separated cell. `_send_one()` sends to every active
admin plus `imports@ravasco.com` with `fail_silently=False` (load-bearing: a failure must raise so every
claim from that run is released for retry). `send_advance_license_expiry_reports()` runs import and
export independently and returns `importSent` / `importLicenses` / `exportSent` / `exportLicenses`.

### apps/services/plant_mismatch_report.py

The Plant Data Correction email behind `internal/send-mismatch-report`. `_PLANT_HEADS` is a fixed dict of
plant → label, recipient and match models (not derived from `PTUser`). `build_plant_mismatch_report()`
lists non-dismissed PO↔MIR and MIR↔Stock matches with `qty_mismatched` or `rate_mismatched`, showing a
percentage only for the mismatch actually true ("whichever applicable"), with MIR number and month.
`send_plant_mismatch_reports(test_recipient=None)` sends one `EmailMultiAlternatives` per plant with
something flagged, CC'ing every admin, **with no "do not reply" footer** (it asks for a reply). Real
delivery is off unless `MISMATCH_REPORT_PLANT_HEADS_ENABLED`; then it returns `plant_heads_disabled:
true`. `test_recipient` redirects all three emails there, prefixes `[TEST]`, drops the CC and is never
gated. Best-effort per plant; no `ReportSendLog` dedup, by decision.

### apps/core/management/commands/report_match_accuracy.py

Terminal renderer over `match_accuracy.build_report(note_limit=0)`: overall, then by plant, match type,
plant + match type, match type + tier, match type + coverage band, marking small samples and reporting
stale verdicts. Holds no scoring logic of its own.

### apps/core/management/commands/report_retired_pos.py

Read-only companion to PO retirement. Default mode fetches each plant's domestic and import master CSV
(same settings and parsers as the syncs) and lists active stored orders absent from it - what the next
sync would deactivate. `--already-retired` lists `is_active=False` orders straight from the DB, no Drive
access. `--plant` narrows. An annotated number (containing `(`) is marked "almost certainly a rename".
Retirement itself is in [data-sync.md](data-sync.md).
