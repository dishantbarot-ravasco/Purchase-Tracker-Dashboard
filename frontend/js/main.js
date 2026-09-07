/**
 * frontend/js/main.js — the PO<->MIR<->Stock reconciliation dashboard
 * ("/"), for HRS, RTP-Achhad, and RTP-Vapi.
 */
//
// Navigation hierarchy (top to bottom): Purchase Orders / Raw Material
// Analysis (level 1) -> plant (level 2) -> Domestic / Import Purchases
// (level 3, Purchase Orders only). Originally ported to match the "Purchase
// Tracker" Claude Artifact prototype's structure exactly (see CLAUDE.md's
// "Artifact-parity decisions" section for that history), with one
// deliberate deviation from the artifact requested directly by the project
// owner on 2026-09-04: Purchase Orders' plant selector now DOES include an
// "All Plants" option (listed first, before HRS), same as Raw Material
// Analysis already had - see plantTabOptions() below. This supersedes the
// earlier "no All Plants for Purchase Orders" parity decision; that
// decision is now stale, don't revert to it without re-checking with the
// owner first.
//   - Purchase Orders' level 3 is still Domestic Purchases / Import
//     Purchases only - NO "Combined" option (unchanged from the artifact).
// Level 3 doesn't apply to Raw Material Analysis at all - Stock/MIR data
// has no domestic-vs-import distinction in the database (only Purchase
// Orders do), so showing that split there would imply a distinction the
// data can't actually support - confirmed with the project owner rather
// than guessed.
//
// UI/interaction model within each (view, plant, purchase-type) combination
// (KPI cards that filter the list -> chart row -> table -> drill-down
// modal) is ported from the original "Purchase Tracker" Claude artifact
// this app is modeled on. What's different from that artifact, deliberately:
//   - All three plants share this one rendering path - only the API path
//     prefix and a couple of display labels change per plant (see PLANTS
//     below); the backend already normalizes all three plants' data into
//     the identical response shape (apps/api/routers/hrs_views.py /
//     achhad_views.py / vapi_views.py).
//   - "All Plants" (Raw Material Analysis only - see above) is a real
//     client-side aggregation (fetches all three plants' data and merges
//     it, tagging each row with which plant it came from - see
//     currentMaterials()), not a fourth backend endpoint. Stock-lot ids are
//     NOT guaranteed unique across plants (per-plant autoincrement PKs), so
//     every row/modal lookup uses a composite "<plantKey>::<id>" key once
//     aggregation is possible, never a bare id - see plantKeyFor(). This
//     same composite-key plumbing is still used for Purchase Orders' single-
//     plant rows too (harmless and consistent, even though no PO-view
//     aggregation ever happens now).
//   - "Import Purchases" is real, live data (ensureImportPOsLoaded() below
//     fetches /api/imports/purchase-orders), not an empty state - HRS's and
//     RTP-Achhad's Imports CSVs are already synced (imports_views.py). It
//     has no MIR-equivalent reconciliation though - see CLAUDE.md's Known
//     gaps for why the "Material Inwarded"/discrepancy KPI cards there stay
//     disabled ("Awaiting MIR"). RTP-Vapi's own genuinely different BOE/
//     customs-shaped Imports CSV is still unparsed (deferred to v2).
//   - "Data Quality Flags" (Purchase Orders KPI row) are categorized
//     client-side from each PO's own `remarks` text via a ported regex rule
//     set - see FLAG_CATEGORY_RULES/DISCREPANCY_LEGEND below and CLAUDE.md's
//     "Artifact-parity decisions" for where this came from.
//   - Sign-in is required (frontend/js/auth.js's requireAuth(), called at
//     the top of init() below, redirects to /login.html if there's no
//     active session) - see CLAUDE.md's "Auth & security architecture".
//   - Data comes from real PO<->MIR matching (apps/services/matching.py /
//     matching_achhad.py / matching_vapi.py), not placeholder status flags.
//   - The mini-stepper has 3 steps (Ordered / Inwarded / Stocked), see
//     miniStepperHtml() below and CLAUDE.md's roadmap section for what the
//     3rd step actually reads (a real per-line-item PO<->MIR<->Stock chain).
//   - "Materials" lists one row per material NAME, summed across every
//     contributing vendor lot/plant (see aggregateMaterialsByName()), not
//     one row per raw Stock lot - the backend's own per-lot data (HRS's and
//     Vapi's real Stock sheets are lot-shaped, one row per material+vendor;
//     Achhad's is material-shaped with no vendor column at all, see
//     apps/core/models.py) is still kept underneath (`lots`/`vendors` on
//     each aggregated group) for the drill-down modal's per-vendor
//     breakdown, not lost by aggregating.

// PLANTS/PLANT_KEYS/ALL_PLANTS_LABEL/apiForPlant/escapeHtml/formatInr/
// formatDateIN now live in js/shared.js (loaded before this file on every
// protected page) - not redefined here, see that file's header comment.

// This dashboard's own script used to be one 3,460-line main.js; split
// 2026-09-04 into one file per concern, all plain globals sharing this
// page's one script scope (same as shared.js/auth.js already did - no
// bundler, no ES modules). Load order in index.html matters: every file
// below must load before this one, since main.js's init() (bottom of this
// file) is the entry point and reaches into all of them.
//   - js/charts.js    - Chart.js lifecycle (pageCharts/modalCharts,
//                        destroy*Charts, closeModal) + chart plugins/helpers
//                        (centerTextPlugin, centerImportTextPlugin,
//                        formatMonthLabel, trailingPriceAvg).
//   - js/flags.js      - PO status (STATUS_LABELS/computeStatus/
//                        miniStepperHtml/materialStepperHtml), match-
//                        confidence badges (matchStatusHtml and friends),
//                        and Data Quality Flag rendering/categorization -
//                        shared by every list/modal below.
//   - js/po-list.js    - renderPoList() (Domestic Purchases KPI row/chart/
//                        table).
//   - js/po-modal.js   - openPoModal() (Domestic PO detail modal).
//   - js/import-po.js  - renderImportPoList()/openImportPoModal() (Import
//                        Purchases list + detail modal).
//   - js/materials.js  - Raw Material Analysis list rendering + its own
//                        drill-down chart, and the material<->PO linkage
//                        helpers everything else here reads.
//   - js/material-modal.js - openMaterialModal() (material detail modal).
// This file keeps: PURCHASE_TYPES, the shared `state` object and per-plant
// caches, the filter-reset helpers, and the bootstrap/nav/sync-polling code
// (init() and everything init() calls directly) - the app's own central
// state and entry point, not any one view's rendering.

// Level 3 - Purchase Orders only (see file header for why Raw Material
// Analysis doesn't get this split, and why there's no "Combined" option -
// matches the reference design exactly, not an earlier guess). 'domestic'
// shows every domestic PO currently synced; 'import' shows real, live
// Import PO data for HRS/RTP-Achhad (ensureImportPOsLoaded() below) - RTP-
// Vapi's own genuinely different BOE/customs-shaped Imports CSV is still
// unparsed (deferred to v2, see CLAUDE.md's roadmap), so Vapi rows won't
// appear under this tab yet even though HRS/Achhad ones do.
const PURCHASE_TYPES = [
  { key: 'domestic', label: 'Domestic Purchases' },
  { key: 'import', label: 'Import Purchases' },
];

// ── Constants & state ───────────────────────────────────────────────────
const root = document.getElementById('root');

// Purchase orders / materials are cached per REAL plant (never per "all")
// so switching tabs back and forth doesn't re-fetch data already loaded,
// and "All Plants" composes for free from whichever per-plant caches are
// already populated instead of needing a cache entry of its own.
let PURCHASE_ORDERS_BY_PLANT = {};
let MATERIALS_BY_PLANT = {};
// Import POs, unlike domestic, come from ONE combined cross-plant endpoint
// (apps/api/routers/imports_views.py's purchase_orders view already merges
// all three plants and tags each row with `plant`) - so there is exactly
// one cache, not one per plant. selectedPlantKeys() still narrows it for
// display, same idea as currentPOs()'s "All Plants" merge, just starting
// from an already-merged array instead of merging per-plant caches itself.
let IMPORT_PO_CACHE = null;
let IMPORT_PO_DETAIL_CACHE = {}; // "plant|poNumber" -> full detail payload (Items/Shipment/Flags tabs)
let state = {
  view: 'po',            // 'po' | 'materials'
  // 'all' ("All Plants") is valid for both views - see plantTabOptions().
  // Defaults to 'all' so the dashboard lands on Purchase Orders / Domestic /
  // All Plants on first paint (project owner request, 2026-09-04).
  plant: 'all',
  purchaseType: 'domestic', // 'domestic' | 'import' (Purchase Orders only)
  statusFilter: null, from: null, to: null, showAllPOs: false, legendOpen: false,
  // chartMonthFilter is set by clicking a bar in the "PO Value Trend" chart
  // (see the chart's onClick below) - a 'YYYY-MM' string. Table-only filter:
  // unlike from/to, it never touches the KPI counts or the charts
  // themselves, only which rows the list below shows - see renderPoList().
  chartMonthFilter: null,
  // Per-column header filters on the "View all" PO table (poNumber/vendor
  // substring match, delivery-date range, value range, Progress) - see
  // applyColFilters(). Status has its own header <select> but deliberately
  // reuses statusFilter above rather than a separate field, so the KPI
  // cards/pie chart/header dropdown can never disagree about which status
  // is selected.
  colFilters: { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, progress: '' },
  // Data Quality Flags filter, split into 3 dropdowns mirroring the
  // Materials view's Category/Sub Category/Flags bar (see matCategoryFilter
  // etc. below) - categoryFilter is the flag *severity* ('critical' or
  // 'info', PO's closest equivalent of a material's Category field),
  // subCategoryFilter cascades to a specific DISCREPANCY_LEGEND label within
  // that severity, and flagsFilter is a coarse KPI-style shortcut
  // ('qtydisc'/'ratedisc'/'flags') matching Materials' own Flags dropdown
  // options 1:1. All three are "global" filters like from/to: they narrow
  // `filtered` itself, so the KPI counts and both charts reflect the
  // selection, not just the table. Rendered above the chart row.
  categoryFilter: null, subCategoryFilter: null, flagsFilter: null,
  // "View all" table pagination - 1-indexed, 10 rows/page. Reset to 1 by
  // every handler that can change which rows match (see renderPoList()).
  tablePage: 1,
  // matCategoryFilter/matSubCategoryFilter are "global" filters, same as PO's
  // categoryFilter above: they narrow the aggregated material list itself,
  // so the KPI row and drill-down chart reflect only the selected category/
  // sub-category, not just the table - rendered as a "Filter by Category" /
  // "Filter by Sub Category" bar right above the chart (see
  // renderMaterialsView()). matStatusFilter is shared between a KPI-card
  // click and the table's own Status column header <select> - one source of
  // truth for "which flag is selected", same pattern as PO's statusFilter/
  // header Status select never disagreeing with each other.
  matCategoryFilter: null, matSubCategoryFilter: null, matStatusFilter: null,
  showAllMaterials: false, matTablePage: 1,
  // Table-only header filters on the materials table (never touch the KPI
  // row/chart, only which rows the table shows) - same role as PO's
  // colFilters/applyColFilters(). Material name text search lives here too
  // (table-only), not as a "global" filter like matCategoryFilter above -
  // matches PO Number/Vendor's own table-only text search on the PO table.
  // Category/subCategory here mirror state.matCategoryFilter/
  // matSubCategoryFilter (see those fields' own comment) - the table's own
  // header-row selects write into those same global fields directly, not a
  // separate table-only copy, same single-source-of-truth reasoning as
  // Status below. progress is table-only, like PO's own Progress filter.
  matColFilters: { material: '', progress: '' },
  // Drill-down chart nav (Inventory Value by Category -> Subcategory -> Material) -
  // see renderMaterialsChart().
  matChartLevel: 'category', matChartCategory: null, matChartSubcategory: null,

  // Import Purchases (Purchase Orders, purchaseType 'import' only) - kept
  // namespaced separately from the domestic PO fields above (importFrom, not
  // a reused `from`, etc.) even though the two purchase types are never
  // rendered at the same time, so switching tabs can never leave one type's
  // filter bleeding into the other's differently-shaped status/stage model.
  importFrom: null, importTo: null,
  importStatusFilter: null, // one of the KPI-card / donut-slice keys in renderImportPoList()
  // Category/Sub Category filters for Import, same severity/label pair as
  // Domestic's categoryFilter/subCategoryFilter (see that field's own
  // comment) - Flags reuses importStatusFilter above (qtydisc/qtydiscmir/
  // ratedisc/flags, the same KPI-card keys), not a separate field.
  importCategoryFilter: null, importSubCategoryFilter: null,
  importChartMonthFilter: null, importShowAllPOs: false, importTablePage: 1,
  importColFilters: { poNumber: '', vendor: '', country: '', stage: '' },
};

function resetImportFilters() {
  state.importFrom = null; state.importTo = null; state.importStatusFilter = null;
  state.importCategoryFilter = null; state.importSubCategoryFilter = null;
  state.importChartMonthFilter = null; state.importShowAllPOs = false; state.importTablePage = 1;
  state.importColFilters = { poNumber: '', vendor: '', country: '', stage: '' };
}

function isAllPlants() { return state.plant === 'all'; }
function selectedPlantKeys() { return isAllPlants() ? PLANT_KEYS : [state.plant]; }
function plantDisplayLabel() { return isAllPlants() ? ALL_PLANTS_LABEL : PLANTS[state.plant].label; }
// A merged "All Plants" row is tagged with _plantKey at merge time; a
// single-plant row isn't (no need), so it falls back to the one plant
// currently selected. Used anywhere a fetch or lookup needs to target the
// exact plant a given row actually belongs to.
function plantKeyFor(item) { return item._plantKey || state.plant; }

function resetFilters() {
  state.statusFilter = null; state.from = null; state.to = null; state.showAllPOs = false;
  state.chartMonthFilter = null; state.categoryFilter = null; state.subCategoryFilter = null; state.flagsFilter = null; state.tablePage = 1;
  state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, progress: '' };
  state.matCategoryFilter = null; state.matSubCategoryFilter = null; state.matStatusFilter = null;
  state.showAllMaterials = false; state.matTablePage = 1;
  state.matColFilters = { material: '', progress: '' };
  state.matChartLevel = 'category'; state.matChartCategory = null; state.matChartSubcategory = null;
  resetImportFilters();
}

// ── Bootstrap ───────────────────────────────────────────────────────────
/** Entry point, invoked once at the bottom of this file. Gates the whole
 * dashboard behind requireAuth(), then renders the shared nav chrome and
 * kicks off the first data load. */
async function init() {
  const user = await requireAuth();  // frontend/js/auth.js - redirects to /login.html on failure
  if (!user) return;

  // Brand/nav/user identity now live in the static topnav in index.html
  // (shared with home.html/search-po.html/admin.html - see js/auth.js's
  // renderNavTabs()) - #root only holds this page's own dashboard content.
  renderNavTabs(document.getElementById('navTabs'), 'dashboard');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();

  root.innerHTML =
    '<div class="sync-bar">' +
      '<div class="who" id="syncBadges"></div>' +
      '<button type="button" id="refreshDataBtn" class="refresh-btn">Refresh Data</button>' +
    '</div>' +
    '<div class="validation-note"><svg class="validation-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 21h20L12 3Z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r=".6" fill="currentColor" stroke="none"/></svg> <div>Matches shown here are computed automatically (exact PO-number match, or a weighted score on vendor/material/qty/rate/value - see the confidence badge on each line item). They are not guaranteed correct, especially anything below "high" confidence or carrying a qty/rate/value flag. <strong>Manually verify before treating a match as ground truth for reconciliation decisions.</strong></div>' +
    '</div>' +
    '<div class="view-tabs" id="viewTabs" role="tablist"></div>' +
    '<div class="plant-tabs" id="plantTabs" role="tablist"></div>' +
    '<div class="sub-tabs" id="purchaseTypeTabs" role="tablist"></div>' +
    '<div id="viewContent"></div>';
  renderViewTabs();
  renderPlantTabs();
  renderPurchaseTypeTabs();
  loadSyncStatus();
  // Admins get a real Google Drive sync (see apps/services/sync_trigger.py
  // and triggerRealSyncAndRefresh() below) - wired 2026-09-04 after the
  // project owner reported the old behavior ("Refresh Data" only re-read
  // Postgres, never actually talked to Drive) as confusing/"not connected
  // properly". The /sync-trigger endpoint is IsAdmin-only server-side (see
  // apps/api/permissions.py), so non-admins keep the old re-read-only
  // behavior here rather than risk a surprise 403.
  const refreshBtn = document.getElementById('refreshDataBtn');
  refreshBtn.title = user.role === 'admin'
    ? 'Triggers a real Google Drive sync for the selected plant(s), then reloads once it finishes - can take a few minutes.'
    : 'Reloads from the database. Ask an admin to run a live Drive sync for fresher data.';
  refreshBtn.onclick = async () => {
    if (user.role === 'admin') {
      await triggerRealSyncAndRefresh(refreshBtn);
      return;
    }
    const btn = refreshBtn;
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    try {
      PURCHASE_ORDERS_BY_PLANT = {};
      MATERIALS_BY_PLANT = {};
      await loadSyncStatus();
      await loadAndRender();
    } catch (e) {
      // Real bug, found and fixed 2026-09-04: this used to only
      // console.error() and silently reset the button back to "Refresh
      // Data" - a viewer clicking it would see the button briefly say
      // "Refreshing…" then flip right back with nothing to show for it and
      // no indication anything went wrong, indistinguishable from "already
      // up to date". Matches the admin branch's own triggerRealSyncAndRefresh()
      // failure handling just above (alert() - this page has no toast
      // system, unlike admin.html).
      console.error('Refresh Data failed:', e);
      alert('Could not refresh the data right now: ' + (e.message || 'unknown error'));
    } finally {
      btn.disabled = false;
      btn.textContent = 'Refresh Data';
    }
  };
  await loadAndRender();
}

// Admin-only real Drive sync (2026-09-04) - triggers every currently
// selected plant's sync+match pipeline (1 plant, or all 3 under "All
// Plants" - see selectedPlantKeys()), then polls until every triggered
// plant reports done, then reloads exactly like the old re-read-only
// button did. A 409 ("already running", e.g. someone else already clicked
// it, or a previous click's poll timed out while the sync kept going) is
// treated as "good, it's already in flight" and just moves straight to
// polling - not an error.
async function triggerRealSyncAndRefresh(btn) {
  const targetKeys = selectedPlantKeys();
  btn.disabled = true;
  btn.textContent = 'Starting sync…';
  try {
    await Promise.all(targetKeys.map(async key => {
      try {
        await apiForPlant(key, '/sync-trigger', { method: 'POST' });
      } catch (e) {
        if (e.status !== 409) throw e;
      }
      // Also kick off that plant's Import PO CSV sync - a separate
      // pipeline from the domestic one just triggered above (see
      // sync_trigger.py's _run_pipeline vs _run_imports_pipeline). Found
      // 2026-09-07 that "Refresh Data" never triggered this at all, so
      // Import PO data only ever updated via a direct API call, never
      // through this button.
      try {
        await apiImports('/sync-trigger/' + key, { method: 'POST' });
      } catch (e) {
        if (e.status !== 409) throw e;
      }
    }));
  } catch (e) {
    console.error('sync-trigger failed:', e);
    btn.disabled = false;
    btn.textContent = 'Refresh Data';
    alert('Could not start the sync: ' + (e.message || 'unknown error'));
    return;
  }
  await pollSyncUntilDone(btn, targetKeys);
}

// Polls each triggered plant's /sync-status for syncInProgress:false every
// SYNC_POLL_INTERVAL_MS, up to SYNC_POLL_TIMEOUT_MS total - a real sync+
// match pipeline (3 Drive-fetching commands + 1 match command per plant)
// can genuinely take a few minutes, this isn't a quick request. On timeout
// the sync is very likely still running in the background (see
// sync_trigger.py's own worker-recycling caveat for the one case where it
// silently isn't) - polling just stops rather than blocking the UI forever;
// the badges will pick up the finished state next time anything reloads them.
const SYNC_POLL_INTERVAL_MS = 4000;
const SYNC_POLL_TIMEOUT_MS = 5 * 60 * 1000;
async function pollSyncUntilDone(btn, targetKeys) {
  const startedAt = Date.now();
  btn.textContent = 'Syncing…';
  while (Date.now() - startedAt < SYNC_POLL_TIMEOUT_MS) {
    await new Promise(resolve => setTimeout(resolve, SYNC_POLL_INTERVAL_MS));
    let stillRunning;
    try {
      const results = await Promise.all(targetKeys.map(key => apiForPlant(key, '/sync-status')));
      // imports/sync-status is a single cross-plant endpoint (unlike the
      // domestic per-plant one above) - see imports_views.py's sync_status()
      // docstring - so it's fetched once and narrowed to targetKeys here.
      const importsStatus = await apiImports('/sync-status');
      stillRunning = results.some(r => r.syncInProgress) ||
        targetKeys.some(key => importsStatus.sync[key] && importsStatus.sync[key].syncInProgress);
    } catch (e) {
      console.error('Polling sync-status failed:', e);
      continue; // one bad poll shouldn't abandon the wait - try again next tick
    }
    if (!stillRunning) {
      PURCHASE_ORDERS_BY_PLANT = {};
      MATERIALS_BY_PLANT = {};
      await loadSyncStatus();
      await loadAndRender();
      btn.disabled = false;
      btn.textContent = 'Refresh Data';
      return;
    }
  }
  btn.disabled = false;
  btn.textContent = 'Refresh Data';
  await loadSyncStatus();
  alert('The sync is taking longer than expected. It may still be running in the background - refresh in a bit to check.');
}

// ── Level 1: Purchase Orders / Raw Material Analysis ───────────────────
function renderViewTabs() {
  const el = document.getElementById('viewTabs');
  el.innerHTML =
    '<div class="view-tab ' + (state.view === 'po' ? 'active' : '') + '" data-view="po" tabindex="0" role="tab" aria-selected="' + (state.view === 'po') + '">Purchase Orders</div>' +
    '<div class="view-tab ' + (state.view === 'materials' ? 'active' : '') + '" data-view="materials" tabindex="0" role="tab" aria-selected="' + (state.view === 'materials') + '">Raw Material Analysis</div>';
  el.querySelectorAll('[data-view]').forEach(t => t.onclick = async () => {
    if (t.dataset.view === state.view) return;
    state.view = t.dataset.view;
    // "All Plants" is now valid under both views (Purchase Orders and Raw
    // Material Analysis) - no fallback needed when switching between them.
    resetFilters();
    renderViewTabs();
    renderPlantTabs();
    renderPurchaseTypeTabs();
    await loadAndRender();
  });
}

// ── Level 2: plant selector - "All Plants" first, then HRS/RTP-Achhad/
// RTP-Vapi. Same option set for both Purchase Orders and Raw Material
// Analysis (see file header for the 2026-09-04 change on the PO side) ────
function plantTabOptions() {
  const real = PLANT_KEYS.map(k => ({ key: k, label: PLANTS[k].label }));
  return [{ key: 'all', label: ALL_PLANTS_LABEL }].concat(real);
}
function renderPlantTabs() {
  const el = document.getElementById('plantTabs');
  const tabs = plantTabOptions();
  el.innerHTML = tabs.map(t =>
    '<div class="plant-tab ' + (state.plant === t.key ? 'active' : '') + '" data-plant="' + t.key + '" tabindex="0" role="tab" aria-selected="' + (state.plant === t.key) + '">' + escapeHtml(t.label) + '</div>'
  ).join('');
  el.querySelectorAll('[data-plant]').forEach(t => t.onclick = async () => {
    if (t.dataset.plant === state.plant) return;
    state.plant = t.dataset.plant;
    resetFilters();
    renderPlantTabs();
    loadSyncStatus();
    await loadAndRender();
  });
}

// ── Level 3: Domestic / Import Purchases (Purchase Orders only) ──
function renderPurchaseTypeTabs() {
  const el = document.getElementById('purchaseTypeTabs');
  if (state.view !== 'po') { el.innerHTML = ''; el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = PURCHASE_TYPES.map(pt =>
    '<div class="sub-tab ' + (state.purchaseType === pt.key ? 'active' : '') + '" data-ptype="' + pt.key + '" tabindex="0" role="tab" aria-selected="' + (state.purchaseType === pt.key) + '">' + escapeHtml(pt.label) + '</div>'
  ).join('');
  el.querySelectorAll('[data-ptype]').forEach(t => t.onclick = async () => {
    if (t.dataset.ptype === state.purchaseType) return;
    state.purchaseType = t.dataset.ptype;
    state.statusFilter = null; state.showAllPOs = false; state.chartMonthFilter = null;
    state.categoryFilter = null; state.subCategoryFilter = null; state.flagsFilter = null; state.tablePage = 1;
    state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, progress: '' };
    resetImportFilters();
    renderPurchaseTypeTabs();
    const content = document.getElementById('content');
    if (!content) return;
    if (state.purchaseType === 'import' && !IMPORT_PO_CACHE) {
      // Unlike Domestic (already loaded on initial dashboard load), Import
      // PO data is fetched lazily on first visit to this tab - a real fetch,
      // not the "no fetch needed" case the comment used to describe here.
      content.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading import purchase orders&hellip;</div></div>';
      try {
        await ensureImportPOsLoaded();
      } catch (e) {
        console.error('Failed to load import purchase orders:', e);
        content.innerHTML = '<div class="noaccess">Couldn\'t load import purchase orders right now. Please refresh, or contact IT if this keeps happening.</div>';
        return;
      }
    }
    renderPoList(content);
  });
}

async function loadSyncStatus() {
  const el = document.getElementById('syncBadges');
  try {
    if (isAllPlants()) {
      const perPlant = await Promise.all(PLANT_KEYS.map(async key => {
        const data = await apiForPlant(key, '/sync-status');
        const times = Object.values(data.sync).map(r => r.startedAt).filter(Boolean).sort();
        // Collect every failed source's own error_detail (not just the
        // fact that something failed) so a hover on the "failed" badge
        // tells an admin WHY, not just that they need to go dig through
        // server logs/Django Admin to find out - see errorDetail's own
        // comment on the single-plant branch below for the same fix.
        const failedDetails = Object.values(data.sync).filter(r => r.status !== 'success' && r.errorDetail).map(r => r.errorDetail);
        return {
          label: PLANTS[key].label,
          latest: times.length ? times[times.length - 1] : null,
          anyFailed: Object.values(data.sync).some(r => r.status !== 'success'),
          failedTitle: failedDetails.join(' | '),
          inProgress: !!data.syncInProgress,
          snapshotGapDays: data.snapshotGapDays,
        };
      }));
      el.innerHTML = perPlant.map(p => {
        // syncInProgress (see apps/services/sync_trigger.py) reflects a
        // real Drive sync currently running for that plant - shown even if
        // triggered from another browser/tab/session, since it's read from
        // the shared DB-backed lock, not client-side state.
        const syncingBadge = p.inProgress ? ' <span class="badge syncing">syncing&hellip;</span>' : '';
        // snapshotGapDays (Snapshot Pipeline Rebuild, Phase B - see
        // apps/api/routers/_domestic_base.py's make_sync_status()) makes a
        // silently-dead daily snapshot job visible instead of looking
        // identical to a healthy one - reuses the existing .badge.stale
        // style rather than adding new CSS.
        const gapBadge = p.snapshotGapDays > 1
          ? ' <span class="badge stale" title="No stock snapshot in ' + p.snapshotGapDays + ' days">snapshot gap: ' + p.snapshotGapDays + 'd</span>'
          : '';
        if (!p.latest) return '<span class="badge stale">' + escapeHtml(p.label) + ': never synced</span>' + syncingBadge + gapBadge;
        const when = new Date(p.latest).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        const titleAttr = p.anyFailed && p.failedTitle ? ' title="' + escapeHtml(p.failedTitle) + '"' : '';
        return '<span class="badge ' + (p.anyFailed ? 'failed' : '') + '"' + titleAttr + '>' + escapeHtml(p.label) + ': ' + when + '</span>' + syncingBadge + gapBadge;
      }).join('');
    } else {
      const data = await apiForPlant(state.plant, '/sync-status');
      // 'match' added 2026-09-04 - previously the matching pass (which
      // actually produces every discrepancy flag/confidence badge on this
      // dashboard) had no SyncRun tracking at all, so a real failure there
      // was invisible here even with every other source showing green -
      // see SyncRun.Source.MATCH's own comment (apps/core/models.py).
      const labels = { po_csv: 'PO Updated', mir: 'MIR', stock: 'RM', match: 'Matching' };
      const syncingBadge = data.syncInProgress ? ' <span class="badge syncing">syncing&hellip;</span>' : '';
      // See the isAllPlants() branch above for what snapshotGapDays means
      // and why .badge.stale is reused rather than adding new CSS.
      const gapBadge = data.snapshotGapDays > 1
        ? ' <span class="badge stale" title="No stock snapshot in ' + data.snapshotGapDays + ' days">snapshot gap: ' + data.snapshotGapDays + 'd</span>'
        : '';
      el.innerHTML = Object.keys(labels).map(src => {
        const run = data.sync[src];
        if (!run) return '<span class="badge stale">' + labels[src] + ': never synced</span>';
        const cls = run.status === 'success' ? '' : 'failed';
        const when = new Date(run.startedAt).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        // errorDetail (SyncRun.error_detail, apps/core/models.py) was
        // always recorded server-side on a sync failure, but never
        // returned by /sync-status until now - an admin used to see only a
        // red "failed" badge with no way to find out why short of Django
        // Admin/server log access. A plain title tooltip is enough here;
        // this is a diagnostic aid for the (rare) failure case, not
        // something that needs its own dedicated UI.
        const titleAttr = cls === 'failed' && run.errorDetail ? ' title="' + escapeHtml(run.errorDetail) + '"' : '';
        return '<span class="badge ' + cls + '"' + titleAttr + '>' + labels[src] + ': ' + when + '</span>';
      }).join('') + syncingBadge + gapBadge;
    }
  } catch (e) {
    // Logged, and shown as a visible badge rather than silently leaving the
    // sync-status area blank - a blank area looks identical to "nothing
    // synced yet" and "the status check itself failed", which are very
    // different things a viewer needs to be able to tell apart.
    console.error('loadSyncStatus failed:', e);
    el.innerHTML = '<span class="badge failed">Sync status unavailable</span>';
  }
}

async function loadAndRender() {
  if (state.view === 'po') {
    await loadDashboard();
  } else {
    await loadAndRenderMaterials();
  }
}

// ── API fetch helpers & per-plant caches ────────────────────────────────
async function ensurePOsLoaded(plantKeys) {
  await Promise.all(plantKeys.map(async key => {
    if (!PURCHASE_ORDERS_BY_PLANT[key]) {
      const data = await apiForPlant(key, '/purchase-orders');
      PURCHASE_ORDERS_BY_PLANT[key] = data.purchaseOrders;
    }
  }));
}

// Returns cached rows for the current plant selection. In "All Plants"
// mode this is a real client-side merge across all three plants' caches,
// each row tagged with which plant it came from - see plantKeyFor() and
// the file header for why that tag (not the bare id) drives every
// lookup/fetch once a row could have come from more than one plant.
function currentPOs() {
  const keys = selectedPlantKeys();
  if (keys.length === 1) return PURCHASE_ORDERS_BY_PLANT[keys[0]] || [];
  const merged = [];
  keys.forEach(key => {
    (PURCHASE_ORDERS_BY_PLANT[key] || []).forEach(po => {
      merged.push(Object.assign({}, po, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });
  return merged;
}

// GET /api/imports/... isn't under any single plant's apiPrefix (it's the
// combined cross-plant router, not "hrs"/"achhad"/"vapi" prefixed) - a
// small direct fetch wrapper instead of routing it through apiForPlant()
// and picking an arbitrary plant key, same 401-redirect/error-shape
// contract as apiForPlant() in shared.js.
async function apiImports(path, opts) {
  const res = await fetch('/api/imports' + path, opts || {});
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  // See shared.js's apiForPlant() for why res.json() is guarded - same fix
  // applied here for consistency (this is the imports-router equivalent of
  // that same fetch wrapper).
  let data;
  try {
    data = await res.json();
  } catch (e) {
    const err = new Error('The server sent an unexpected response. Please try again, or contact IT if this keeps happening.');
    err.status = res.status;
    throw err;
  }
  if (!res.ok) {
    const err = new Error(data.error || data.detail || 'Something went wrong. Please try again.');
    err.status = res.status;
    throw err;
  }
  return data;
}

async function ensureImportPOsLoaded() {
  if (IMPORT_PO_CACHE) return;
  const data = await apiImports('/purchase-orders');
  IMPORT_PO_CACHE = data.purchaseOrders || [];
}

// Same role as currentPOs(), starting from the already-merged cache instead
// of merging per-plant caches - see IMPORT_PO_CACHE's own comment. `plant`
// on each row is already 'hrs'/'achhad'/'vapi' (imports_views.py's plant
// key, not a display label), so it lines up with PLANT_KEYS/selectedPlantKeys()
// directly, same convention plantKeyFor()/_plantKey uses for domestic rows.
function currentImportPOs() {
  const keys = selectedPlantKeys();
  return (IMPORT_PO_CACHE || []).filter(po => keys.includes(po.plant));
}

async function loadDashboard() {
  const el = document.getElementById('viewContent');
  el.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading purchase orders&hellip;</div></div>';
  try {
    if (state.purchaseType === 'import') await ensureImportPOsLoaded();
    else await ensurePOsLoaded(selectedPlantKeys());
    el.innerHTML = '<div id="content"></div>';
    renderPoList(document.getElementById('content'));
  } catch (e) {
    console.error('loadDashboard failed:', e);
    el.innerHTML = '<div class="noaccess">Couldn\'t load the dashboard right now. Please refresh, or contact IT if this keeps happening.</div>';
  }
}

init();
