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
| GET | `[<p>/]purchase-orders/<po>/mir-candidates` | `mir_candidates` | Auth + plant (403) | MIR documents a line could be pinned to, each with who holds it (`claimedBy`) |
| PATCH | `[<p>/]purchase-orders/<po>/mir-match` | `set_mir_match` | IsEditor + plant | Set / clear a manual MIR pin, then run `run_full_match()` synchronously |
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
| PATCH | `imports/purchase-orders/<plant>/<po>/mir-match` | `set_mir_match` | IsEditor + plant (403) | Set / clear an import pin (`po_kind=import`) |
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
- **Re-matching is synchronous.** Any correction to a matching-relevant field, and every pin change,
  calls that plant's `run_full_match()` inside the request. Idempotent and cheap at these volumes.
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
- **A verdict whose match row no longer exists is dropped and counted as `staleVerdicts`** - a later
  `match_*` run deletes and re-points pairs, so it is a statement about a pair that no longer exists.
- **Tiers resolve with one query per (plant, match_type) group**, not one per review (200+ queries at
  the programme's own target).
- **`precision` ignores "unsure" and `recall` counts it against the total.** `null` (nothing judged)
  renders as an en dash, never 0%.

**Throughput polish, same date.** Keyboard verdicts (`1` / `2` / `3`), `U` to undo, `N` for the next
batch, and a progress bar against the target counted in **distinct matches** (`_progress()`:
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

Five protected pages share one header (`brand.css`, modelled on the TDS Automation App's nav). The JS
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

**Note on "PO Not Found in MIR" density**: Import's and Materials' linked-item universe includes every
still-open order by construction, so this fires for most on-order items. Expected, not a bug.

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
MIR" is suppressed from the red bucket while a row is ON ORDER** (it fires on every not-yet-due order by
construction, and would have made red mean nothing), but still counts on overdue and partial rows;
**overdue is not a fifth bucket** (the status pill already turns red); and **`partial` wins over
`onOrder`** when Import can report both. Details in [frontend.md](frontend.md).

### Inline "Edit Everywhere"

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

**Endpoints.** `GET .../mir-candidates?q=` returns up to 80 active MIR rows, newest first, collapsed to
**one entry per MIR number** with `rowCount`, `sheetRows` (every Excel row of that document) and
`claimedBy`. With no `q` it returns rows whose PO column mentions this PO plus recent ones; `q` filters
MIR number, party or material. `PATCH .../mir-match` takes `{itemRef, mirNo, reason, clear}`: a
non-empty `mirNo` pins, an empty `mirNo` pins the line as deliberately unmatched, `clear: true` removes
the pin. An unknown `mirNo` is a 400; a retired PO or unknown `itemRef` a 404. The response carries
`manualPinsApplied`, `stalePins` and `unfilledPins` from the synchronous `run_full_match()`.
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
matches count. The popup says what happens to the holder: *"That line will be re-matched automatically
and may end up with no MIR at all"* - true, the assignment runs again from scratch.

`manually_pinned` on the six `*POMirMatch` models is **derived, not preserved** - rewritten every run, so
removing a pin clears the badge (`manuallyPinned` in the API). `matching_core.py` keeps its "no model
imports" shape: `manual_match_model` and `syncrun_plant` are injected by the three `matching*.py`
modules.

**One picker, two routers.** `po-modal.js`'s `wireMirPicker()` takes an `api` object
(`candidates(q)` / `save(body)`), so Domestic (`apiForPlant()`) and Import (`apiImports()`) share one
panel and collision popup. It is a picker, not a free-text box: typing a number blind is how you pin a
line to a document that does not exist.

Covered by `apps/services/tests/test_manual_mir_match.py`, `test_manual_mir_match_imports.py` (pipeline)
and `apps/api/tests/test_manual_mir_match_api.py`, `test_manual_mir_match_imports_api.py` (HTTP:
permissions, plant scoping, validation, upsert-not-duplicate, retired PO, `claimedBy` / `itemRef` shape,
import `clear` deleting only the import pin). No test covers the domestic endpoint against a same-numbered
import pin.

### Dismiss / override a flagged match or flag

Two mechanisms, because the two things are stored differently.

**Match dismissal.** Every `*POMirMatch` / `*MirStockMatch` has `dismissed_by_override` plus
`dismissed_by` / `dismissed_at` / `dismissed_reason` (migration `0011`).
`match_dismiss.dismiss_match(model_cls, match_id, user, dismissed, reason)` is the one implementation
for every plant and both PO kinds; each router passes its own model class. Endpoints:
`PATCH [<p>/]matches/po-mir/<id>/dismiss`, `[<p>/]matches/mir-stock/<id>/dismiss`, and
`PATCH imports/matches/po-mir/<plant>/<id>/dismiss`. Body `{"dismissed": true|false, "reason": "..."}`;
response `{status, matchId, dismissedByOverride, dismissedReason}`, 404 for an unknown id. **Clearing a
dismissal also clears the three audit columns** - an undone decision should not keep stale provenance.
**Every matcher's `update_or_create` `defaults` never carries `dismissed_*`, so a dismissal survives every
re-match that keeps the same pairing** (`test_dismiss_match.py::test_editor_can_dismiss_with_reason_and_it_survives_rematch`).
A PO↔MIR match row is keyed on its line item, so a run that pairs the line with a **different** MIR row
updates the same row in place - and `matching_core._save_po_mir_match()` clears the dismissal then,
since it judged the old receipt and would otherwise hide the new one's flags
(`test_manual_mir_match.py::test_a_dismissal_survives_a_rematch_but_not_a_repointed_match`). A match
that is deleted and recreated by a later run is a new row and loses it too. MIR↔Stock rows are keyed
on the (MIR row, lot) pair, so they cannot be re-pointed.
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

Frontend: a dismissed flag **stays visible but muted** (struck through, tooltip with who / when / why),
wired by one shared `wireDismissLinks()`; the optional reason is a plain `window.prompt()`.

### Import purchases

Import POs for all three plants are parsed by the shared `parsers/import_po_csv.py`, with a read-time
layer in `import_flags.py`: shipment-stage rollup, PO-vs-BOE qty discrepancy, delivery-date status,
partial-delivery detection, and 7 data-quality flags (`po_flags()`). That layer predates import↔MIR
matching and is still used.

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

There is **no server-computed PO-level MIR rollup for imports**: `import-po.js`'s `renderImportPoList()`
computes `poInwarded` / `poQtyDiscMir` / `poRateDiscMir` client-side (a PO has a condition if any line
does). The server's PO-level `qtyDiscrepancy` (`import_flags.po_has_qty_discrepancy()`) is the PO-vs-BOE
check, not a MIR one.

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
  diff percentages, `qtyOverDelivered` tri-state, split mismatch booleans, `severity`, `matchedMirNo`,
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
- **`make_mir_candidates` / `make_set_mir_match`**: the domestic pin endpoints. `set_mir_match`
  scopes its clear and upsert to `po_kind=domestic` (see
  [Editing which MIR a PO line matched](#editing-which-mir-a-po-line-matched-2026-09-21)).
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
  (from `import_flags`, with `timezone.localdate()`). `_po_dict(detail=True)` adds vendor / billing
  fields, corrections and flag dismissals.
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
- **Pins**: **`mir_candidates`** (404 for unknown or out-of-scope plant; merges domestic claims via
  `_domestic_cfg_for()` with `_import_claims_for_mir_numbers()`), **`set_mir_match`** (writes and deletes
  with `po_kind=import`). `_domestic_cfg_for()` imports the plant view modules lazily to avoid a circular
  import.

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
  `{"done": true}` only when every group is empty. **`submit_review`**: validates plant (400), scope
  (403), match type, verdict, integer id, match existence (404); creates the row; 201.
  **`undo_review`**: deletes only the caller's own row. **`_progress()`**: distinct reviewed matches
  against `REVIEW_TARGET`.
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
`REVIEW_TARGET = 200`. `model_for(config, match_type)` picks the match model. `latest_verdicts()` keeps
the most recent verdict per (plant, match type, match id). `scores()`: precision = correct / (correct +
incorrect), recall = correct / (all three), F1, each `None` when undefined. `_match_rows()` resolves tier
and `coverage_band()` (`n/a` until a `field_coverage` column exists) with one query per group and drops
verdicts whose row is gone. `build_report(note_limit=25)` returns overall, `byPlant`, `byMatchType`,
`byPlantAndType` (all 9 cells), `byTier`, `byCoverage`, `staleVerdicts`, reviewer counts and recent
notes. Only reviews of existing matches are ever scored, so this is not recall over missed matches.

### apps/services/match_dismiss.py

`dismiss_match(model_cls, match_id, user, dismissed, reason)`: sets or clears `dismissed_by_override`
and its three audit columns on any `*POMirMatch` / `*MirStockMatch`, returning the row or `None`.
Clearing wipes the audit columns. Does no plant check itself; callers do.

### apps/services/flag_dismiss.py

`dismiss_po_flag(plant, po_number, flag_key, user, dismissed, reason)`: upserts the `FlagDismissal` row
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
