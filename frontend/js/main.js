// Purchase Tracker Dashboard - HRS, RTP-Achhad, and RTP-Vapi.
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

const root = document.getElementById('root');
// pageCharts = charts owned by the page behind the modal (PO trend/status,
// materials drill-down) - only ever destroyed right before that same view
// re-renders and recreates its own charts (see renderPoList()/
// renderMaterialsChart()). modalCharts = charts owned by whatever's
// currently open in #modalBody (material stock/price trend) - destroyed on
// every modal close/reopen. Two separate arrays on purpose: closeModal()
// used to call the single destroyPageCharts(), which also silently wiped
// the dashboard's own PO trend/status charts (and the materials drill-down
// chart) every time a PO/material modal closed, since they shared one
// registry - the canvases went blank and nothing ever redrew them, because
// closing a modal doesn't re-run renderPoList()/renderMaterialsView(). Bug
// found and fixed 2026-09-04 - don't merge these back into one array.
let pageCharts = [];
let modalCharts = [];
function destroyPageCharts() { pageCharts.forEach(c => { try { c.destroy(); } catch (e) { console.warn('Chart.destroy() failed:', e); } }); pageCharts = []; }
function destroyModalCharts() { modalCharts.forEach(c => { try { c.destroy(); } catch (e) { console.warn('Chart.destroy() failed:', e); } }); modalCharts = []; }

// Bumped by every openImportPoModal()/openMaterialModal() call, and captured
// as each call's own `myModalRequestId`. Both functions re-check this after
// every await before touching #modalBody - without it, clicking row A then
// quickly row B before A's fetch resolves can let A's response land after
// B's and silently overwrite the modal (now showing B's title/backdrop)
// with A's stale data. Bug found and fixed 2026-09-04.
let modalRequestId = 0;

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
  // Starts on a real plant rather than 'all' so the very first paint has a
  // single plant's worth of data to fetch, not all three at once.
  plant: PLANT_KEYS[0],
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
  colFilters: { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, valueMin: null, valueMax: null, progress: '' },
  // Data Quality Flags category filter (see DISCREPANCY_LEGEND) - a single
  // category label, or null for "All Categories". Rendered above the chart
  // row, and unlike chartMonthFilter/colFilters this IS a "global" filter
  // like from/to: it narrows `filtered` itself, so the KPI counts and both
  // charts reflect only POs carrying that category, not just the table.
  categoryFilter: null,
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
  matColFilters: { material: '', stockMin: null, stockMax: null, valueMin: null, valueMax: null, rateMin: null, rateMax: null },
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
  importChartMonthFilter: null, importShowAllPOs: false, importTablePage: 1,
  importColFilters: { poNumber: '', vendor: '', country: '', valueMin: null, valueMax: null, stage: '' },
};

function resetImportFilters() {
  state.importFrom = null; state.importTo = null; state.importStatusFilter = null;
  state.importChartMonthFilter = null; state.importShowAllPOs = false; state.importTablePage = 1;
  state.importColFilters = { poNumber: '', vendor: '', country: '', valueMin: null, valueMax: null, stage: '' };
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
  state.chartMonthFilter = null; state.categoryFilter = null; state.tablePage = 1;
  state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, valueMin: null, valueMax: null, progress: '' };
  state.matCategoryFilter = null; state.matSubCategoryFilter = null; state.matStatusFilter = null;
  state.showAllMaterials = false; state.matTablePage = 1;
  state.matColFilters = { material: '', stockMin: null, stockMax: null, valueMin: null, valueMax: null, rateMin: null, rateMax: null };
  state.matChartLevel = 'category'; state.matChartCategory = null; state.matChartSubcategory = null;
  resetImportFilters();
}

// FLAG_PCT mirrors apps/services/matching*.py's FLAG_DIFF_PCT - kept in
// sync manually since the frontend only receives the already-computed diff
// percentages, not the threshold itself. Zero tolerance (2026-09-04,
// project owner, superseding the earlier "5%, deliberately not the
// artifact's 10%" decision) - even a 1kg-out-of-1000kg (0.1%) qty diff, or
// the equivalent for rate/value, now counts as a discrepancy. Since every
// comparison below uses strict `>`, an exact match (diff === 0) still never
// flags - only a real, nonzero difference does.
const FLAG_PCT = 0;

// Colored flag icon for the KPI row - a small monochrome SVG (Material
// Design's "flag" glyph) recolored via `fill` so every KPI card carries a
// color-coded symbol instead of plain text. Per the project owner's
// 2026-09-04 request every status card gets one (not just the discrepancy
// cards), each matching that status's own established color elsewhere in
// the app (status pills, .kpi-card.<status> border colors) - and per the
// 2026-09-04 follow-up, Overdue moved out of the amber "delivery-timing"
// group into the same red as the qty/rate discrepancy cards, since an
// overdue PO is treated as urgent/critical, not just a scheduling note.
const KPI_FLAG_COLORS = {
  critical: '#dc2626', // qty/rate discrepancies + Overdue
  received: '#16a34a',
  partial: '#2563eb',
  pending: '#d97706',
  unknown: '#64748b',
  quality: '#7c3aed', // Data Quality Flags
};
// `cls` defaults to 'kpi-flag-icon' (absolutely positioned in a KPI card's
// top-right corner). Row-level flags (see rowFlags() in renderPoList) pass
// 'row-flag-icon' instead - a plain inline icon, not absolutely positioned,
// since it sits inline next to a status pill rather than alone in a card.
function flagIconHtml(hexColor, cls) {
  return '<svg class="' + (cls || 'kpi-flag-icon') + '" width="15" height="15" viewBox="0 0 24 24" fill="' + hexColor + '" aria-hidden="true">' +
    '<path d="M14.4 6L14 4H5v17h2v-7h5.6l.4 2h7V6h-4.6z"/></svg>';
}

// ── Chart helpers (PO Value Trend / Status Breakdown) ──────────────────
// 'YYYY-MM' -> 'Mon YYYY' for chart axis labels - the raw ISO-ish month key
// used for grouping/sorting (monthTotals) is unreadable as an axis label.
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function formatMonthLabel(m) {
  const parts = m.split('-');
  const idx = parseInt(parts[1], 10) - 1;
  return (MONTH_NAMES[idx] || parts[1]) + ' ' + parts[0];
}
// Chart.js plugin (scoped per-chart via options.plugins array, not globally
// registered) that draws the slice total in the doughnut's own cutout hole -
// a "how many POs total" readout the legend/slices alone don't give at a
// glance. Self-contained: reads whatever dataset the chart it's attached to
// actually has, no dependency on the module-level `state`.
const centerTextPlugin = {
  id: 'poStatusCenterText',
  afterDraw(chart) {
    const area = chart.chartArea;
    if (!area) return;
    const data = chart.data.datasets[0].data;
    const total = data.reduce((a, b) => a + b, 0);
    const ctx = chart.ctx;
    const cx = (area.left + area.right) / 2;
    const cy = (area.top + area.bottom) / 2;
    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#0f1b2d';
    ctx.fillText(String(total), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#94a3b8';
    ctx.fillText('TOTAL POs', cx, cy + 11);
    ctx.restore();
  },
};

// Separate qty/rate/value badges (previously one blended "review" flag) so a
// normal partial delivery (qty differs, rate/value line up) doesn't read the
// same as a genuine price/value discrepancy. Value is suppressed when qty is
// already flagged, since value = qty x rate - a value gap fully explained by
// a partial-delivery qty gap isn't a separate problem worth a second badge.
// Matches are algorithmic (PO-number exact match or a weighted score, see
// CLAUDE.md) - the confidence badge and these diff badges are there so a
// human still manually verifies anything that isn't a plain PO-number match,
// not as a replacement for that review.
function matchStatusHtml(it, plantKey) {
  if (!it.matched) return '-';
  let out = '✓' + (it.matchedMirNo ? ' (' + escapeHtml(it.matchedMirNo) + ')' : '');

  const conf = it.matchTier === 'po_number' ? 'high' : (it.matchScore != null && it.matchScore >= 0.75 ? 'medium' : 'low');
  const confTitle = { high: 'High confidence: exact PO number match', medium: 'Medium confidence: weighted score ≥ 0.75', low: 'Low confidence: weighted score below 0.75 - verify manually' }[conf]
    + (it.matchScore != null ? ' (score ' + it.matchScore.toFixed(2) + ')' : '');
  out += ' <span class="conf-badge conf-' + conf + '" title="' + escapeHtml(confTitle) + '">' + conf + '</span>';

  const qtyFlag = it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT;
  const rateFlag = it.rateDiffPct != null && it.rateDiffPct > FLAG_PCT;
  const valueFlag = it.valueDiffPct != null && it.valueDiffPct > FLAG_PCT && !qtyFlag;
  const anyFlag = qtyFlag || rateFlag || valueFlag;
  // Dismissed (apps/services/match_dismiss.py) keeps the badges visible but
  // muted, rather than hiding them - a reviewer who dismissed a flag should
  // still be able to see what was dismissed and why, not lose the record.
  const dismissedCls = it.dismissedByOverride ? ' dismissed' : '';
  // Badge text uses 1 decimal place, not toFixed(0) - with zero tolerance
  // (see FLAG_PCT's own comment) a genuinely flagged 0.1% diff would
  // otherwise round to "Δ0%", which reads as "no difference" and
  // contradicts the badge existing at all.
  if (qtyFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Qty received differs from PO qty by ' + it.qtyDiffPct.toFixed(2) + '% - likely a partial/over delivery, verify manually">qty Δ' + it.qtyDiffPct.toFixed(1) + '%</span>';
  if (rateFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Rate differs from PO rate by ' + it.rateDiffPct.toFixed(2) + '% - verify manually">rate Δ' + it.rateDiffPct.toFixed(1) + '%</span>';
  if (valueFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Value differs from PO value by ' + it.valueDiffPct.toFixed(2) + '%, not explained by qty - verify manually">value Δ' + it.valueDiffPct.toFixed(1) + '%</span>';

  if (anyFlag && it.dismissedByOverride) {
    out += ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (it.dismissedBy ? ' by ' + it.dismissedBy : '') + (it.dismissedReason ? ': ' + it.dismissedReason : '')) + '">dismissed</span>';
    if (canEditField(plantKey)) {
      out += ' <span class="dismiss-link" data-match-id="' + it.matchId + '" data-match-type="po-mir" data-dismiss="false">reinstate</span>';
    }
  } else if (anyFlag && canEditField(plantKey)) {
    out += ' <span class="dismiss-link" data-match-id="' + it.matchId + '" data-match-type="po-mir" data-dismiss="true">dismiss</span>';
  }
  return out;
}

// Import-PO equivalent of matchStatusHtml() above, for an import line
// item's nested `mirMatch` object (apps/api/routers/imports_views.py's
// _mir_match_dict()) instead of the flat matched/matchTier/... fields
// domestic line items carry - same badge shapes/colors, just reading from
// a nested object since imports_views.py is one cross-plant router rather
// than three per-plant ones (see that module's docstring) and couldn't
// reuse the exact flat shape without colliding with its own PO-level
// fields (dataQualityFlags, etc.) that already use similar names.
function importMatchStatusHtml(it, plantKey) {
  const m = it.mirMatch;
  if (!m) return '-';
  let out = '✓';
  const conf = m.tier === 'po_number' ? 'high' : (m.matchScore != null && m.matchScore >= 0.75 ? 'medium' : 'low');
  const confTitle = { high: 'High confidence: exact PO number match', medium: 'Medium confidence: weighted score ≥ 0.75', low: 'Low confidence: weighted score below 0.75 - verify manually' }[conf]
    + (m.matchScore != null ? ' (score ' + m.matchScore.toFixed(2) + ')' : '');
  out += ' <span class="conf-badge conf-' + conf + '" title="' + escapeHtml(confTitle) + '">' + conf + '</span>';

  const qtyFlag = m.qtyDiffPct != null && m.qtyDiffPct > FLAG_PCT;
  const rateFlag = m.rateDiffPct != null && m.rateDiffPct > FLAG_PCT;
  const valueFlag = m.valueDiffPct != null && m.valueDiffPct > FLAG_PCT && !qtyFlag;
  const anyFlag = qtyFlag || rateFlag || valueFlag;
  const dismissedCls = m.dismissedByOverride ? ' dismissed' : '';
  if (qtyFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Qty (as per BOE) differs from MIR qty by ' + m.qtyDiffPct.toFixed(2) + '% - verify manually">qty Δ' + m.qtyDiffPct.toFixed(1) + '%</span>';
  if (rateFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Rate (converted to INR) differs from MIR rate by ' + m.rateDiffPct.toFixed(2) + '% - verify manually">rate Δ' + m.rateDiffPct.toFixed(1) + '%</span>';
  if (valueFlag) out += ' <span class="flag-badge' + dismissedCls + '" title="Value differs from MIR value by ' + m.valueDiffPct.toFixed(2) + '%, not explained by qty - verify manually">value Δ' + m.valueDiffPct.toFixed(1) + '%</span>';

  if (anyFlag && m.dismissedByOverride) {
    out += ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (m.dismissedReason ? ': ' + m.dismissedReason : '')) + '">dismissed</span>';
    if (canEditField(plantKey)) out += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="import-po-mir" data-plant="' + plantKey + '" data-dismiss="false">reinstate</span>';
  } else if (anyFlag && canEditField(plantKey)) {
    out += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="import-po-mir" data-plant="' + plantKey + '" data-dismiss="true">dismiss</span>';
  }
  if (m.stockMatched) out += ' <span class="conf-badge conf-high" title="The MIR entry this item matched to also has a Stock match">stocked</span>';
  return out;
}

// MIR<->Stock equivalent of matchStatusHtml() above, for a Stock lot's
// `mirStockMatches` (apps/api/routers/*_views.py's _lot_dict()) - a lot can
// carry more than one MIR<->Stock pairing, so this renders one badge per
// flagged match rather than a single blended one.
function mirStockMatchHtml(lot, plantKey) {
  const matches = lot.mirStockMatches || [];
  const flagged = matches.filter(m => m.isFlagged);
  if (!flagged.length) return matches.length ? '<span class="conf-badge conf-high">matched</span>' : '-';
  return flagged.map(m => {
    const dismissedCls = m.dismissedByOverride ? ' dismissed' : '';
    const parts = [];
    if (m.rateDiffPct != null) parts.push('rate Δ' + m.rateDiffPct.toFixed(1) + '%');
    if (m.qtyDiffPct != null) parts.push('qty Δ' + m.qtyDiffPct.toFixed(1) + '%');
    let badge = '<span class="flag-badge' + dismissedCls + '">' + escapeHtml(parts.join(', ') || 'flagged') + '</span>';
    if (m.dismissedByOverride) {
      badge += ' <span class="dismissed-tag">dismissed</span>';
      if (canEditField(plantKey)) badge += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="mir-stock" data-plant="' + plantKey + '" data-dismiss="false">reinstate</span>';
    } else if (canEditField(plantKey)) {
      badge += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="mir-stock" data-plant="' + plantKey + '" data-dismiss="true">dismiss</span>';
    }
    return badge;
  }).join(' ');
}

// Wires every .dismiss-link under `container` (rendered by matchStatusHtml()
// above, or the equivalent MIR<->Stock badge in the materials modal) to
// PATCH .../matches/<type>/<id>/dismiss and then re-run `onDone` to refresh
// whatever view is showing the match. A plain confirm()/prompt() for the
// optional reason - this app has no reusable modal-with-textarea component,
// and a full one felt like overkill for a single optional text field.
function wireDismissLinks(container, plantKey, onDone) {
  container.querySelectorAll('.dismiss-link').forEach(el => {
    el.onclick = async () => {
      const matchId = el.dataset.matchId;
      const matchType = el.dataset.matchType;
      // data-plant overrides the passed-in plantKey for a cross-plant table
      // (the material modal's Stock by Plant rows, each possibly a
      // different plant) - the PO modal's single-plant table never sets it,
      // so it falls back to the plantKey argument there.
      const rowPlantKey = el.dataset.plant || plantKey;
      const dismissing = el.dataset.dismiss === 'true';
      let reason = '';
      if (dismissing) {
        reason = window.prompt('Optional note for dismissing this flag (why is it fine to ignore?):', '') || '';
        if (reason === null) return;
      } else if (!window.confirm('Reinstate this flag? It will show as flagged again.')) {
        return;
      }
      const original = el.textContent;
      el.textContent = '…';
      try {
        await dismissMatch(rowPlantKey, matchType, matchId, dismissing, reason);
        if (onDone) await onDone();
      } catch (e) {
        alert('Could not update this flag: ' + e.message);
        el.textContent = original;
      }
    };
  });
}

// ── Data Quality Flags: categorized from each PO's own `remarks` text ──
// Ported from the original "Purchase Tracker" Claude Artifact prototype's
// CATEGORY_RULES/categorizeFlag() (confirmed by reading its real source via
// the Artifact tool - see CLAUDE.md's "Artifact-parity decisions"), run
// against the same `remarks` field this app already parses verbatim from
// the source CSV (apps/services/parsers/po_csv.py) - no new data needed.
const FLAG_CATEGORY_RULES = [
  { label: 'Misfiled: wrong plant or company', test: t => /misfiled/i.test(t) },
  { label: 'Duplicate file or PO', test: t => /duplicate/i.test(t) },
  { label: 'Revision or superseded PO conflict', test: t => /(changed purchase order|rev\s?0?1|revision|superced|supersede)/i.test(t) },
  { label: 'Tax calculation or labeling mismatch', test: t => /(ugst|igst|cgst|sgst|tax rate|tax mislabel|tax section|inconsistent tax)/i.test(t) },
  { label: 'Header or template data error', test: t => /(header|letterhead|template)/i.test(t) },
  { label: 'Vendor GSTIN anomaly', test: t => /gstin/i.test(t) },
  { label: 'Missing or blank field', test: t => /(blank|not captured|no tax breakdown|no incoterms|missing)/i.test(t) },
  { label: 'Import PO filed in Domestic folder', test: t => /(import po|is an import|belongs in.*import)/i.test(t) },
  { label: 'Delivery date anomaly', test: t => /(delivery date|equals created date|predates|passed)/i.test(t) },
  { label: 'Vendor code scheme inconsistency', test: t => /vendor code/i.test(t) },
  { label: 'Non raw material or different category', test: t => /(not raw material|different category|capital equipment|tooling)/i.test(t) },
];
function categorizeFlag(text) {
  for (const rule of FLAG_CATEGORY_RULES) if (rule.test(text)) return { label: rule.label, severity: 'info' };
  return { label: 'Other data quality issue', severity: 'info' };
}

// Full glossary for the collapsible legend panel - one source of truth
// alongside FLAG_CATEGORY_RULES above. Zero tolerance (2026-09-04, see
// FLAG_PCT's own comment) - the two critical categories' wording says so
// explicitly rather than interpolating FLAG_PCT ("by more than 0 percent"
// is technically correct but reads oddly; "any difference" is clearer).
const DISCREPANCY_LEGEND = [
  { label: 'Quantity Discrepancy', severity: 'critical', meaning: 'A line item’s received quantity (from the matched MIR entry) does not exactly match the PO’s ordered quantity - any difference at all counts, there is no tolerance (e.g. 999kg received against a 1000kg order still flags). Can mean a short shipment, an over shipment, or a receipt logged against the wrong PO.' },
  { label: 'Rate / Value Discrepancy', severity: 'critical', meaning: 'A line item’s received rate or value (from the matched MIR entry) does not exactly match the PO’s rate or value - any difference at all counts, there is no tolerance. Can mean a price change was not reflected on the PO, a tax calculation difference, or a billing error.' },
  { label: 'Misfiled: wrong plant or company', severity: 'info', meaning: 'The PO document was found filed under the wrong plant or company folder in Drive.' },
  { label: 'Duplicate file or PO', severity: 'info', meaning: 'The same PO appears to have been saved or extracted more than once.' },
  { label: 'Revision or superseded PO conflict', severity: 'info', meaning: 'A later revision of the PO exists, or PO numbering suggests it replaced an earlier one, and both versions are present.' },
  { label: 'Tax calculation or labeling mismatch', severity: 'info', meaning: 'The tax type, rate, or amount on the PO looks inconsistent, for example IGST used where CGST plus SGST was expected, or the numbers do not add up.' },
  { label: 'Header or template data error', severity: 'info', meaning: 'The PO letterhead or template fields, such as company name or address, look wrong or inconsistent with the vendor or plant.' },
  { label: 'Vendor GSTIN anomaly', severity: 'info', meaning: 'The vendor GSTIN on the PO looks malformed or does not match known records.' },
  { label: 'Missing or blank field', severity: 'info', meaning: 'A field expected on the PO, such as incoterms or a tax breakdown, is blank or was not captured during extraction.' },
  { label: 'Import PO filed in Domestic folder', severity: 'info', meaning: 'A PO that is actually an international or import order was found in the Domestic purchases folder.' },
  { label: 'Delivery date anomaly', severity: 'info', meaning: 'The delivery date is the same as the created date, which usually means immediate delivery and is unusual, or otherwise looks off.' },
  { label: 'Vendor code scheme inconsistency', severity: 'info', meaning: 'The vendor code format does not match the scheme used elsewhere.' },
  { label: 'Non raw material or different category', severity: 'info', meaning: 'The PO is for something that is not raw material, such as capital equipment or tooling, and may not belong in this tracker.' },
  { label: 'Other data quality issue', severity: 'info', meaning: 'A note recorded in the PO’s Remarks field during extraction that did not match any of the specific patterns above.' },
];
// Per-category flag color - "the flag color should match the error it
// represents" (project owner, 2026-09-04): previously every info category
// shared one flat purple, so "Duplicate file or PO" and "Tax calculation
// mismatch" looked identical at a glance and only the hover tooltip told
// them apart. Grouped into a handful of thematically distinct colors rather
// than either one flat color (too little signal) or 14 unique hues (too
// noisy to read) - money/qty problems (red), tax/compliance (amber),
// filing/process issues - wrong plant, duplicate, superseded, wrong
// domestic/import folder (indigo), data-completeness gaps - template,
// missing fields, vendor code scheme, wrong category (slate), delivery
// timing (blue), and an "other" catch-all (purple). Keys must match
// DISCREPANCY_LEGEND's/FLAG_CATEGORY_RULES' `label` strings exactly - if
// you add a new category there, add its color here too, or it silently
// falls back to DEFAULT_CATEGORY_COLOR.
const CATEGORY_COLORS = {
  'Quantity Discrepancy': '#dc2626',
  'Rate / Value Discrepancy': '#dc2626',
  'Tax calculation or labeling mismatch': '#d97706',
  'Vendor GSTIN anomaly': '#d97706',
  'Misfiled: wrong plant or company': '#6366f1',
  'Duplicate file or PO': '#6366f1',
  'Revision or superseded PO conflict': '#6366f1',
  'Import PO filed in Domestic folder': '#6366f1',
  'Header or template data error': '#64748b',
  'Missing or blank field': '#64748b',
  'Vendor code scheme inconsistency': '#64748b',
  'Non raw material or different category': '#64748b',
  'Delivery date anomaly': '#2563eb',
};
const DEFAULT_CATEGORY_COLOR = '#7c3aed'; // 'Other data quality issue' + any unmapped label
function categoryColor(label) { return CATEGORY_COLORS[label] || DEFAULT_CATEGORY_COLOR; }

function renderLegendHtml() {
  const rows = DISCREPANCY_LEGEND.map(d => '<li class="legend-item"><span class="lg-dot" style="background:' + categoryColor(d.label) + '"></span><span class="lg-label ' + d.severity + '">' + (d.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span><b>' + escapeHtml(d.label) + '</b>: ' + escapeHtml(d.meaning) + '</li>').join('');
  const infoCount = DISCREPANCY_LEGEND.length - 2;
  return '<div class="legend-box" id="legendBox"' + (state.legendOpen ? '' : ' hidden') + '>' +
    '<h4>What each flag means (' + DISCREPANCY_LEGEND.length + ' categories total: 2 critical, ' + infoCount + ' informational)</h4>' +
    '<div class="legend-sub">Critical flags are computed directly from PO versus actual goods receipt data and need action first. Informational flags come from data quality notes recorded when each PO was extracted, and are process or paperwork issues rather than money or quantity problems.</div>' +
    '<ul class="legend-list">' + rows + '</ul>' +
  '</div>';
}

// Mirrors the artifact's rec._deliveryDate: the earliest delivery date among
// this PO's still-unmatched line items (what a viewer actually needs to
// know - "when is the outstanding part due"), falling back to the earliest
// delivery date overall only once every line item has already matched.
function computePoDeliveryDate(po) {
  const items = po.items || [];
  const unmatchedDates = items.filter(it => !it.matched).map(it => it.deliveryDate).filter(Boolean).sort();
  if (unmatchedDates.length) return unmatchedDates[0];
  const allDates = items.map(it => it.deliveryDate).filter(Boolean).sort();
  return allDates.length ? allDates[0] : null;
}

// Computed once per PO alongside computeStatus() - drives the Quantity/
// Rate-Value Discrepancy KPI cards, the per-row flag badges, and the
// legend panel above. Uses this app's own matching-engine threshold
// (FLAG_PCT = apps/services/matching*.py's FLAG_DIFF_PCT) - zero tolerance
// as of 2026-09-04, see FLAG_PCT's own comment for why.
function computePoFlags(po) {
  const items = po.items || [];
  po._qtyFlag = items.some(it => it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT);
  po._rateFlag = items.some(it => (it.rateDiffPct != null && it.rateDiffPct > FLAG_PCT) || (it.valueDiffPct != null && it.valueDiffPct > FLAG_PCT));
  const cats = new Map();
  if (po._qtyFlag) cats.set('Quantity Discrepancy', { label: 'Quantity Discrepancy', severity: 'critical' });
  if (po._rateFlag) cats.set('Rate / Value Discrepancy', { label: 'Rate / Value Discrepancy', severity: 'critical' });
  if (po.remarks) { const c = categorizeFlag(po.remarks); cats.set(c.label, c); }
  po._categories = Array.from(cats.values());
  po._hasInfoFlag = po._categories.some(c => c.severity === 'info');
}

async function init() {
  const user = await requireAuth();  // frontend/js/auth.js - redirects to /login.html on failure
  if (!user) return;

  // Brand/nav/user identity now live in the static topnav in index.html
  // (shared with home.html/search-po.html/admin.html - see js/auth.js's
  // renderNavTabs()) - #root only holds this page's own dashboard content.
  renderNavTabs(document.getElementById('navTabs'), 'dashboard');
  renderUserBadge(document.getElementById('navUser'));

  root.innerHTML =
    '<div class="sync-bar">' +
      '<div class="who" id="syncBadges"></div>' +
      '<button type="button" id="refreshDataBtn" class="refresh-btn">Refresh Data</button>' +
    '</div>' +
    '<div class="validation-note">⚠️ <div>Matches shown here are computed automatically (exact PO-number match, or a weighted score on vendor/material/qty/rate/value - see the confidence badge on each line item). They are not guaranteed correct, especially anything below "high" confidence or carrying a qty/rate/value flag. <strong>Manually verify before treating a match as ground truth for reconciliation decisions.</strong></div>' +
    '</div>' +
    '<div class="view-tabs" id="viewTabs"></div>' +
    '<div class="plant-tabs" id="plantTabs"></div>' +
    '<div class="sub-tabs" id="purchaseTypeTabs"></div>' +
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
      console.error('Refresh Data failed:', e);
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
      stillRunning = results.some(r => r.syncInProgress);
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
    '<div class="view-tab ' + (state.view === 'po' ? 'active' : '') + '" data-view="po">Purchase Orders</div>' +
    '<div class="view-tab ' + (state.view === 'materials' ? 'active' : '') + '" data-view="materials">Raw Material Analysis</div>';
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
    '<div class="plant-tab ' + (state.plant === t.key ? 'active' : '') + '" data-plant="' + t.key + '">' + escapeHtml(t.label) + '</div>'
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
    '<div class="sub-tab ' + (state.purchaseType === pt.key ? 'active' : '') + '" data-ptype="' + pt.key + '">' + escapeHtml(pt.label) + '</div>'
  ).join('');
  el.querySelectorAll('[data-ptype]').forEach(t => t.onclick = async () => {
    if (t.dataset.ptype === state.purchaseType) return;
    state.purchaseType = t.dataset.ptype;
    state.statusFilter = null; state.showAllPOs = false; state.chartMonthFilter = null;
    state.categoryFilter = null; state.tablePage = 1;
    state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, valueMin: null, valueMax: null, progress: '' };
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
        return {
          label: PLANTS[key].label,
          latest: times.length ? times[times.length - 1] : null,
          anyFailed: Object.values(data.sync).some(r => r.status !== 'success'),
          inProgress: !!data.syncInProgress,
        };
      }));
      el.innerHTML = perPlant.map(p => {
        // syncInProgress (see apps/services/sync_trigger.py) reflects a
        // real Drive sync currently running for that plant - shown even if
        // triggered from another browser/tab/session, since it's read from
        // the shared DB-backed lock, not client-side state.
        const syncingBadge = p.inProgress ? ' <span class="badge syncing">syncing&hellip;</span>' : '';
        if (!p.latest) return '<span class="badge stale">' + escapeHtml(p.label) + ': never synced</span>' + syncingBadge;
        const when = new Date(p.latest).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        return '<span class="badge ' + (p.anyFailed ? 'failed' : '') + '">' + escapeHtml(p.label) + ': ' + when + '</span>' + syncingBadge;
      }).join('');
    } else {
      const data = await apiForPlant(state.plant, '/sync-status');
      const labels = { po_csv: 'PO Updated', mir: 'MIR', stock: 'RM' };
      const syncingBadge = data.syncInProgress ? ' <span class="badge syncing">syncing&hellip;</span>' : '';
      el.innerHTML = Object.keys(labels).map(src => {
        const run = data.sync[src];
        if (!run) return '<span class="badge stale">' + labels[src] + ': never synced</span>';
        const cls = run.status === 'success' ? '' : 'failed';
        const when = new Date(run.startedAt).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        return '<span class="badge ' + cls + '">' + labels[src] + ': ' + when + '</span>';
      }).join('') + syncingBadge;
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
  const data = await res.json();
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

// Same 5-status model as the source artifact (received / partial / pending
// / overdue / unknown), driven by the real PO<->MIR match, not a
// placeholder. A PO is only "received"/"partial" once matching has
// actually linked a line item to a live MIR row.
// Display label only - the internal status key stays 'pending' everywhere
// (state.statusFilter, computeStatus()'s return value, the 'pending' CSS
// class on .status-pill/.kpi-card, etc.) so nothing else needs to change to
// pick this up; STATUS_LABELS.pending is the single place the visible text
// lives. Renamed 'Pending' -> 'On Order' (2026-09-04, project owner).
const STATUS_LABELS = { received: 'Received', partial: 'Partial Delivered', pending: 'On Order', overdue: 'Overdue', unknown: 'Delivery Date Unknown' };
function computeStatus(po) {
  const items = po.items || [];
  if (!items.length) return 'pending';
  const matchedCount = items.filter(it => it.matched).length;
  if (matchedCount === items.length) return 'received';
  if (matchedCount > 0) return 'partial';
  const dates = items.map(it => it.deliveryDate).filter(Boolean).sort();
  if (!dates.length) return 'unknown';
  const dd = new Date(dates[0] + 'T00:00:00');
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return (!isNaN(dd.getTime()) && dd < today) ? 'overdue' : 'pending';
}

// 3-step stepper (Ordered / Material Inwarded / Received in Inventory) -
// the 3rd step reads each matched line item's `stockMatched` (see
// hrs_views.py/achhad_views.py/vapi_views.py's _line_item_dict, added
// 2026-09-04) - true only when the MIR entry a line item matched to *also*
// has a real MIR<->Stock match, i.e. the material is currently sitting in
// the Stock file, not just MIR-received. This is a coarse, honest signal
// (a real PO->MIR->Stock chain per line item, not "does this material show
// up anywhere in stock" the way the old artifact prototype approximated it
// - see CLAUDE.md's roadmap note this replaces) - expect it to rarely show
// "done" for HRS specifically, since HRS's Stock sheet's own `received`
// column already reads 0 for nearly every real lot (documented elsewhere in
// CLAUDE.md), which limits how often a fresh MirStockMatch forms at all.
function miniStepperHtml(po) {
  const items = po.items || [];
  const anyMatched = items.some(it => it.matched);
  const anyStocked = items.some(it => it.stockMatched);
  const title = 'Ordered' +
    (anyMatched ? ' → Material Inwarded' : ' (awaiting MIR match)') +
    (anyStocked ? ' → Received in Inventory' : (anyMatched ? ' (awaiting Stock match)' : ''));
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(title) + '">' +
    '<div class="mini-stepper">' +
      '<span class="step-dot done"></span><span class="step-line ' + (anyMatched ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (anyMatched ? 'done' : '') + '"></span><span class="step-line ' + (anyStocked ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (anyStocked ? 'done' : '') + '"></span>' +
    '</div>' +
    '<div class="mini-stepper-labels">Ordered / Inwarded / Stocked</div>' +
  '</div>';
}

// Materials' own 2-step progress indicator - same markup/CSS classes as
// miniStepperHtml() above, but the two steps are "MIR" (has this material
// been received against at least one linked PO line item - i.e. matched to
// a real MIR entry, same `matched` flag PO rows use) and "Stocked" (is it
// actually sitting in current warehouse stock right now, qty > 0) - per the
// project owner's 2026-09-04 request, deliberately not PO's "Ordered"
// framing (a material has no universal "always true" first state the way a
// PO's own existence means "Ordered" always happened).
function materialStepperHtml(m) {
  // `m.mirMatched` is a real signal from the backend (HRSMirStockMatch/etc,
  // the actual computed Stock<->MIR match - see apps/api/routers/*_views.py's
  // _lot_dict()), not inferred from whether this app's own fuzzy PO<->
  // material text matching happened to also find a linked, MIR-matched PO
  // line item. That fuzzy-link-based proxy is what this used to read here -
  // wrong by construction, since a material can be truly MIR-received with
  // zero POs ever fuzzy-linked to it (or the fuzzy match simply missing),
  // so it could show "Stocked" done with "MIR" not done, which the project
  // owner correctly flagged as logically backwards (2026-09-04): stock can
  // only ever have arrived via an MIR receipt in the first place.
  const mirMatched = !!m.mirMatched;
  const stocked = (m.qty || 0) > 0;
  const title = (mirMatched ? 'MIR matched' : 'Awaiting MIR match') + ' → ' + (stocked ? 'Stocked' : 'Not currently in stock');
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(title) + '">' +
    '<div class="mini-stepper">' +
      '<span class="step-dot ' + (mirMatched ? 'done' : '') + '"></span><span class="step-line ' + (mirMatched ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (stocked ? 'done' : '') + '"></span>' +
    '</div>' +
    '<div class="mini-stepper-labels">MIR / Stocked</div>' +
  '</div>';
}

// Per-column header filters on the "View all" PO table - see the header
// filter row rendered inside renderPoList() and state.colFilters' own
// comment. Table-only (like chartMonthFilter/statusFilter above it in the
// filter chain), so these never change the KPI counts or chart data, only
// which rows the list shows. Status itself isn't here - the header's
// Status <select> reads/writes state.statusFilter directly (see
// statusChartData's `key` comment) so there's only ever one source of
// truth for "which status is selected", not two that could disagree.
function applyColFilters(recs) {
  const f = state.colFilters;
  return recs.filter(po => {
    if (f.poNumber && !(po.poNumber || '').toLowerCase().includes(f.poNumber.toLowerCase())) return false;
    if (f.vendor && !(po.vendorName || '').toLowerCase().includes(f.vendor.toLowerCase())) return false;
    if (f.deliveryFrom && (!po._deliveryDate || po._deliveryDate < f.deliveryFrom)) return false;
    if (f.deliveryTo && (!po._deliveryDate || po._deliveryDate > f.deliveryTo)) return false;
    if (f.valueMin != null && (po.totalInclTax == null || po.totalInclTax < f.valueMin)) return false;
    if (f.valueMax != null && (po.totalInclTax == null || po.totalInclTax > f.valueMax)) return false;
    if (f.progress === 'inwarded' && !(po.items || []).some(it => it.matched)) return false;
    if (f.progress === 'not' && (po.items || []).some(it => it.matched)) return false;
    return true;
  });
}

// Refocuses (and restores cursor position on) whatever [data-cf] header
// filter input had focus before a full innerHTML re-render blew it away -
// renderPoList() rebuilds the whole subtree on every keystroke of a text
// filter, which would otherwise kick focus out after the first character
// typed. Wrap any state-mutating handler that triggers a re-render while a
// filter input might be focused: preserveFocus(el, () => { ...; renderPoList(el); }).
function preserveFocus(container, renderFn) {
  const active = document.activeElement;
  const cf = active && active.dataset ? active.dataset.cf : null;
  const selStart = cf && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  const selEnd = cf && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;
  renderFn();
  if (!cf) return;
  const restored = container.querySelector('[data-cf="' + cf + '"]');
  if (!restored) return;
  restored.focus();
  if (selStart != null && restored.setSelectionRange) {
    try { restored.setSelectionRange(selStart, selEnd); } catch (e) { /* not a text-selectable input (date/number/select) */ }
  }
}

function renderPoList(el) {
  // Import Purchases render through the exact same KPI-row -> chart-row ->
  // list -> drill-down-modal scaffold as Domestic (this function), just
  // fed from apps/api/routers/imports_views.py's combined cross-plant data
  // instead of currentPOs() - see renderImportPoList() below. Kept as a
  // separate function rather than branching this one line-by-line: the two
  // datasets share the same *shape* of UI but genuinely different fields
  // (shipment stage/BOE/BL/country-of-origin vs PO<->MIR match status), and
  // interleaving both field sets into one function would be harder to
  // follow than two functions that are each internally consistent.
  if (state.purchaseType === 'import') {
    renderImportPoList(el);
    return;
  }

  const all = currentPOs();
  all.forEach(po => {
    po._status = computeStatus(po);
    po._deliveryDate = computePoDeliveryDate(po);
    computePoFlags(po);
  });

  if (!all.length) {
    const msg = isAllPlants()
      ? 'No purchase orders synced yet for any plant.'
      : 'No purchase orders synced yet - run <code>' + PLANTS[state.plant].syncCmdPoCsv + '</code> to load them.';
    el.innerHTML = '<div class="empty-state">' + msg + '</div>';
    return;
  }

  const inRange = po => {
    if (state.from && (!po.createdDate || po.createdDate < state.from)) return false;
    if (state.to && (!po.createdDate || po.createdDate > state.to)) return false;
    return true;
  };
  let filtered = all.filter(inRange);

  // Category counts computed BEFORE state.categoryFilter narrows `filtered`
  // further, so the dropdown keeps showing every category's count (not just
  // the selected one's) while a category is selected - same pattern as
  // Raw Material Analysis's own category filter. Global filter, like
  // from/to above it: narrows `filtered` itself, so the KPI counts and both
  // charts below reflect only POs carrying the selected category, not just
  // the table (see the category filter's own comment on state.categoryFilter).
  const categoryCounts = {};
  filtered.forEach(po => (po._categories || []).forEach(c => { categoryCounts[c.label] = (categoryCounts[c.label] || 0) + 1; }));
  const dateRangeCount = filtered.length; // "All Categories (N)" option's count - before category narrowing
  if (state.categoryFilter) filtered = filtered.filter(po => (po._categories || []).some(c => c.label === state.categoryFilter));

  const counts = { received: 0, partial: 0, pending: 0, overdue: 0, unknown: 0 };
  filtered.forEach(po => counts[po._status]++);
  const total = filtered.length;
  const qtyDiscCount = filtered.filter(po => po._qtyFlag).length;
  const rateDiscCount = filtered.filter(po => po._rateFlag).length;
  const flagsCount = filtered.filter(po => po._hasInfoFlag).length;

  // Order requested by the project owner (2026-09-04): Total -> Material
  // Inwarded -> Partial Delivered -> the two "critical" (money/quantity)
  // discrepancy cards -> Overdue -> Pending -> Date Unknown -> Data Quality
  // Flags last. Follow-up request (same day): every status card now carries
  // a colored flag icon, not just the discrepancy/attention ones - each
  // matching that status's own established color (status pills,
  // .kpi-card.<status> border colors) - and Overdue uses the same red as
  // the qty/rate discrepancy cards (moved out of the amber group it used to
  // share with Pending/Date Unknown), since an overdue PO is treated as
  // urgent, not merely a scheduling note. Total is the only card with no
  // flag icon - it's a plain aggregate, not a status.
  const cardDef = [
    { key: 'total', cls: '', label: "Total PO's Created", val: total },
    { key: 'received', cls: 'received', label: 'Material Inwarded', val: counts.received, flag: KPI_FLAG_COLORS.received },
    { key: 'partial', cls: 'partial', label: STATUS_LABELS.partial, val: counts.partial, flag: KPI_FLAG_COLORS.partial },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Discrepancies', val: qtyDiscCount, flag: KPI_FLAG_COLORS.critical },
    { key: 'ratedisc', cls: 'critical', label: 'Rate / Value Discrepancies', val: rateDiscCount, flag: KPI_FLAG_COLORS.critical },
    { key: 'overdue', cls: 'overdue', label: 'Overdue', val: counts.overdue, flag: KPI_FLAG_COLORS.critical },
    { key: 'pending', cls: 'pending', label: STATUS_LABELS.pending, val: counts.pending, flag: KPI_FLAG_COLORS.pending },
    { key: 'unknown', cls: 'unknown', label: STATUS_LABELS.unknown, val: counts.unknown, flag: KPI_FLAG_COLORS.unknown },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: flagsCount, flag: KPI_FLAG_COLORS.quality },
  ];
  const kpiHtml = cardDef.map(c => '<div class="kpi-card ' + c.cls + ' ' + (state.statusFilter === c.key ? 'active' : '') + '" data-kpi="' + c.key + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="val">' + c.val + '</div><div class="label">' + escapeHtml(c.label) + '</div></div>').join('');

  let tableRecs = filtered;
  if (state.statusFilter === 'qtydisc') tableRecs = filtered.filter(po => po._qtyFlag);
  else if (state.statusFilter === 'ratedisc') tableRecs = filtered.filter(po => po._rateFlag);
  else if (state.statusFilter === 'flags') tableRecs = filtered.filter(po => po._hasInfoFlag);
  else if (state.statusFilter && state.statusFilter !== 'total') tableRecs = filtered.filter(po => po._status === state.statusFilter);
  // Table-only filters (never touch the KPI counts/charts above, which stay
  // scoped to `filtered` = the from/to date range + category filter only) -
  // clicking a bar in the trend chart narrows the list to that month; the
  // "View all" table's header filters (poNumber/vendor/delivery/value/
  // progress) narrow further. See applyColFilters() and the chart onClick
  // handlers below.
  if (state.chartMonthFilter) tableRecs = tableRecs.filter(po => (po.createdDate || '').slice(0, 7) === state.chartMonthFilter);
  tableRecs = applyColFilters(tableRecs);
  tableRecs = tableRecs.slice().sort((a, b) => (b.createdDate || '').localeCompare(a.createdDate || ''));

  // `key` drives both the doughnut's onClick (maps a clicked slice back to
  // the same statusFilter value a KPI-card click would set) and the header
  // Status <select> (see the "View all" table below) - one status value,
  // three ways to set it, never out of sync with each other.
  const statusChartData = [
    { key: 'received', label: STATUS_LABELS.received, val: counts.received, color: '#16a34a' },
    { key: 'partial', label: STATUS_LABELS.partial, val: counts.partial, color: '#2563eb' },
    { key: 'pending', label: STATUS_LABELS.pending, val: counts.pending, color: '#d97706' },
    { key: 'overdue', label: STATUS_LABELS.overdue, val: counts.overdue, color: '#dc2626' },
    { key: 'unknown', label: STATUS_LABELS.unknown, val: counts.unknown, color: '#64748b' },
  ].filter(s => s.val > 0);

  const monthTotals = {};
  filtered.forEach(po => {
    const m = (po.createdDate || '').slice(0, 7);
    const v = po.totalInclTax != null ? po.totalInclTax : po.totalValue;
    if (!m || v == null) return;
    monthTotals[m] = (monthTotals[m] || 0) + v;
  });
  const months = Object.keys(monthTotals).sort();

  const showingAll = state.showAllPOs;
  const totalForList = tableRecs.length;
  // "View all" is paginated 10/page instead of dumping every matching row
  // at once (project owner, 2026-09-04) - the compact top-5 view is
  // unaffected, it's always just the first 5 of tableRecs, a preview, not
  // a paginated browse. state.tablePage is clamped here (not just where
  // it's set) so a filter change that shrinks the result set below the
  // previously-viewed page can never stick on a blank page.
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(totalForList / PAGE_SIZE));
  const tablePage = Math.min(Math.max(1, state.tablePage), totalPages);
  const listRecs = showingAll ? tableRecs.slice((tablePage - 1) * PAGE_SIZE, tablePage * PAGE_SIZE) : tableRecs.slice(0, 5);

  // Drives the "N filter(s) active - Clear" chip next to the list heading -
  // every table-only filter that could be narrowing the list below what the
  // KPI cards/charts show (which stay scoped to `filtered`, the date-range
  // bar only). The date range counts once here even though it's exposed in
  // two places (the bar above and the "Created On" header filter below -
  // same state.from/state.to, not two separate fields).
  const cf = state.colFilters;
  const activeFilterCount =
    (state.chartMonthFilter ? 1 : 0) +
    (state.statusFilter && state.statusFilter !== 'total' ? 1 : 0) +
    (state.from || state.to ? 1 : 0) +
    (state.categoryFilter ? 1 : 0) +
    (cf.poNumber ? 1 : 0) +
    (cf.vendor ? 1 : 0) +
    (cf.deliveryFrom || cf.deliveryTo ? 1 : 0) +
    (cf.valueMin != null || cf.valueMax != null ? 1 : 0) +
    (cf.progress ? 1 : 0);
  const statusOptionsHtml = Object.keys(STATUS_LABELS).map(k =>
    '<option value="' + k + '"' + (state.statusFilter === k ? ' selected' : '') + '>' + escapeHtml(STATUS_LABELS[k]) + '</option>'
  ).join('');
  // Category filter dropdown, rendered just above the chart row (see
  // el.innerHTML below) - sorted by count descending, same pattern as Raw
  // Material Analysis's own category filter. Only categories actually
  // present in the current date range are listed (categoryCounts is built
  // from real data, not the full DISCREPANCY_LEGEND glossary), so the list
  // never shows a category with nothing behind it.
  const categoryOptionsHtml = Object.entries(categoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => '<option value="' + escapeHtml(label) + '"' + (state.categoryFilter === label ? ' selected' : '') + '>' + escapeHtml(label) + ' (' + n + ')</option>')
    .join('');
  // Per-column header filter content - "as per their data": text (contains)
  // for PO Number/Vendor, date-range for Created On (bound directly to the
  // same state.from/state.to the top filter-row uses, not a duplicate
  // field) and Delivery Date, min/max number for Value, and a <select> each
  // for Status (bound to state.statusFilter - see statusChartData's `key`
  // comment) and Progress. Details has no data of its own (just a link), so
  // its cell is empty. One shared array of inner-cell HTML so both the
  // "View all" table's <thead> AND the compact top-5 grid's header row
  // render the identical controls - filtering works from either view, not
  // just the expanded table.
  const filterCells = [
    '<input type="text" class="col-filter-input" data-cf="poNumber" placeholder="Search..." value="' + escapeHtml(state.colFilters.poNumber) + '">',
    '<input type="text" class="col-filter-input" data-cf="vendor" placeholder="Search..." value="' + escapeHtml(state.colFilters.vendor) + '">',
    '<div class="col-filter-range"><input type="date" data-cf="createdFrom" value="' + (state.from || '') + '"><input type="date" data-cf="createdTo" value="' + (state.to || '') + '"></div>',
    '<div class="col-filter-range"><input type="date" data-cf="deliveryFrom" value="' + (state.colFilters.deliveryFrom || '') + '"><input type="date" data-cf="deliveryTo" value="' + (state.colFilters.deliveryTo || '') + '"></div>',
    '<div class="col-filter-range"><input type="number" class="col-filter-num" data-cf="valueMin" placeholder="Min" value="' + (state.colFilters.valueMin != null ? state.colFilters.valueMin : '') + '"><input type="number" class="col-filter-num" data-cf="valueMax" placeholder="Max" value="' + (state.colFilters.valueMax != null ? state.colFilters.valueMax : '') + '"></div>',
    '<select class="col-filter-input" data-cf="status"><option value="">All</option>' + statusOptionsHtml + '</select>',
    '<select class="col-filter-input" data-cf="progress"><option value="">All</option><option value="inwarded"' + (state.colFilters.progress === 'inwarded' ? ' selected' : '') + '>Inwarded</option><option value="not"' + (state.colFilters.progress === 'not' ? ' selected' : '') + '>Not Inwarded</option></select>',
    '',
  ];

  el.innerHTML =
    '<div class="filter-row">' +
      '<label>Date filter (Created on)</label>' +
      '<input type="date" id="fromDate" value="' + (state.from || '') + '">' +
      '<span style="color:#9ca3af;font-size:12px;">to</span>' +
      '<input type="date" id="toDate" value="' + (state.to || '') + '">' +
      '<button class="primary" id="applyFilter">Apply</button>' +
      '<button id="clearFilter">Clear</button>' +
    '</div>' +
    '<div class="kpi-grid">' + kpiHtml + '</div>' +
    '<span class="legend-toggle" id="legendToggle">What do these flags mean?</span>' +
    renderLegendHtml() +
    // Category filter, placed right before the charts per the project
    // owner's 2026-09-04 request - a "global" filter like the date-range
    // bar above (narrows `filtered`, so it drives the KPI counts and both
    // charts below it too, not just the table - see state.categoryFilter's
    // own comment). Only rendered when at least one category is present in
    // the current date range (an empty dropdown with just "All Categories"
    // would be dead weight).
    (Object.keys(categoryCounts).length ?
      '<div class="filter-row">' +
        '<label>Filter by Category</label>' +
        '<select id="categoryFilterSelect" style="min-width:280px;">' +
          '<option value="">All Categories (' + dateRangeCount + ')</option>' +
          categoryOptionsHtml +
        '</select>' +
        (state.categoryFilter ? '<button id="clearCategoryFilter">Clear</button>' : '') +
      '</div>' : '') +
    ((statusChartData.length || months.length) ?
      '<div class="chart-row">' +
        '<div class="chart-panel"><h4>PO Value Trend by Month Created</h4>' +
          (months.length ? '<div class="chart-box"><canvas id="poTrendChart"></canvas></div>' : '<div class="no-data-note">No dated POs in range to plot.</div>') +
        '</div>' +
        '<div class="chart-panel"><h4>Status Breakdown</h4>' +
          (statusChartData.length ? '<div class="chart-box"><canvas id="poStatusChart"></canvas></div>' : '<div class="no-data-note">No POs in range.</div>') +
        '</div>' +
      '</div>' : '') +
    '<div class="list-toggle-row"><div class="section-title" style="margin:0;">Purchase Orders (Latest first)</div>' +
      '<div style="display:flex;align-items:center;gap:10px;">' +
        (activeFilterCount ? '<span class="clear-list-filters" id="clearListFilters">' + activeFilterCount + ' filter' + (activeFilterCount > 1 ? 's' : '') + ' active &middot; Clear &times;</span>' : '') +
        (totalForList > 5 ? '<button class="view-all-btn" id="toggleAllBtn">' + (showingAll ? 'Show top 5' : 'View all') + '</button>' : '') +
      '</div>' +
    '</div>' +
    (() => {
      // One colored flag icon per category on this PO (not a "2 critical /
      // 1 flag" text count) - see categoryColor()/CATEGORY_COLORS for the
      // color-per-category scheme and DISCREPANCY_LEGEND for what each one
      // means.
      const rowFlags = po => {
        const cats = po._categories || [];
        return cats.map(c =>
          // data-tooltip + CSS (.row-flag-wrap::after, see style.css) instead
          // of a native title attribute - title tooltips have a ~1s hover
          // delay and are easy to dismiss with the slightest mouse movement,
          // which read as "hovering isn't working" per the project owner's
          // 2026-09-04 report. The CSS tooltip shows immediately and
          // reliably instead.
          ' <span class="row-flag-wrap" data-tooltip="' + escapeHtml(c.label) + '">' + flagIconHtml(categoryColor(c.label), 'row-flag-icon') + '</span>'
        ).join('');
      };
      if (showingAll) {
        const colFilterRow = '<tr class="col-filter-row">' + filterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';
        // 10 rows/page instead of dumping the whole filtered result set at
        // once (project owner, 2026-09-04) - direct page-number buttons up
        // to 10 pages (comfortably covers real data sizes today); beyond
        // that, falls back to a plain "Page X of Y" indicator rather than
        // rendering 11+ buttons in a row.
        const pageButtons = totalPages <= 10
          ? Array.from({ length: totalPages }, (_, i) => i + 1)
              .map(p => '<button class="page-btn page-num' + (p === tablePage ? ' active' : '') + '" data-page="' + p + '">' + p + '</button>')
              .join('')
          : '<span class="page-info">Page ' + tablePage + ' of ' + totalPages + '</span>';
        const paginationHtml = totalPages > 1
          ? '<div class="pagination-row">' +
              '<button id="prevPageBtn" class="page-btn"' + (tablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
              pageButtons +
              '<button id="nextPageBtn" class="page-btn"' + (tablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
            '</div>'
          : '';
        return '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Created On</th><th>Delivery Date</th><th>Value (incl. tax)</th><th>Status</th><th>Progress</th><th>Details</th></tr>' + colFilterRow + '</thead>' +
          '<tbody>' + listRecs.map(po => {
            const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
            return '<tr><td><b>' + escapeHtml(po.poNumber) + '</b></td>' +
              '<td>' + escapeHtml(po.vendorName || '-') + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po.createdDate)) + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po._deliveryDate)) + '</td>' +
              '<td>' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : '-') + '</td>' +
              '<td><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</td>' +
              '<td>' + miniStepperHtml(po) + '</td>' +
              '<td><span class="row-link" data-po="' + key + '">View details</span></td></tr>';
          }).join('') + '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row po-cols"><div>PO Number</div><div>Vendor</div><div>Created On</div><div>Delivery Date</div><div>Value (incl. tax)</div><div>Status</div><div>Progress</div><div>Details</div></div>' +
        '<div class="list-header-row po-cols col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="top5List">' + listRecs.map(po => {
          const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
          return '<div class="top5-row">' +
            '<div><span class="po-num">' + escapeHtml(po.poNumber) + '</span></div>' +
            '<div>' + escapeHtml(po.vendorName || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po.createdDate) || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po._deliveryDate) || 'Not available') + '</div>' +
            '<div>' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : '-') + '</div>' +
            '<div><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</div>' +
            '<div>' + miniStepperHtml(po) + '</div>' +
            '<div><span class="row-link" data-po="' + key + '">View details</span></div></div>';
        }).join('') + '</div>';
    })();

  document.querySelectorAll('[data-kpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.kpi;
    state.statusFilter = (state.statusFilter === key || key === 'total') ? null : key;
    state.tablePage = 1;
    renderPoList(el);
  });
  document.getElementById('legendToggle').onclick = () => { state.legendOpen = !state.legendOpen; renderPoList(el); };
  document.getElementById('applyFilter').onclick = () => {
    state.from = document.getElementById('fromDate').value || null;
    state.to = document.getElementById('toDate').value || null;
    state.tablePage = 1;
    renderPoList(el);
  };
  document.getElementById('clearFilter').onclick = () => { state.from = null; state.to = null; state.tablePage = 1; renderPoList(el); };
  const toggleBtn = document.getElementById('toggleAllBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllPOs = !state.showAllPOs; state.tablePage = 1; renderPoList(el); };
  document.querySelectorAll('[data-po]').forEach(el2 => el2.onclick = () => openPoModal(el2.dataset.po));

  const prevPageBtn = document.getElementById('prevPageBtn');
  if (prevPageBtn) prevPageBtn.onclick = () => { state.tablePage = Math.max(1, state.tablePage - 1); renderPoList(el); };
  const nextPageBtn = document.getElementById('nextPageBtn');
  if (nextPageBtn) nextPageBtn.onclick = () => { state.tablePage = state.tablePage + 1; renderPoList(el); }; // clamped to totalPages on next render
  document.querySelectorAll('.page-num').forEach(btn => btn.onclick = () => { state.tablePage = Number(btn.dataset.page); renderPoList(el); });

  const categorySelect = document.getElementById('categoryFilterSelect');
  if (categorySelect) categorySelect.onchange = () => { state.categoryFilter = categorySelect.value || null; state.tablePage = 1; renderPoList(el); };
  const clearCategoryBtn = document.getElementById('clearCategoryFilter');
  if (clearCategoryBtn) clearCategoryBtn.onclick = () => { state.categoryFilter = null; state.tablePage = 1; renderPoList(el); };

  const clearListFiltersBtn = document.getElementById('clearListFilters');
  if (clearListFiltersBtn) clearListFiltersBtn.onclick = () => {
    state.statusFilter = null; state.chartMonthFilter = null; state.from = null; state.to = null;
    state.categoryFilter = null; state.tablePage = 1;
    state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, valueMin: null, valueMax: null, progress: '' };
    renderPoList(el);
  };
  // Header filter row (only present when showingAll - see the colFilterRow
  // markup above). Text inputs re-render on every keystroke ('input') for a
  // live-filter feel; date/number/select commit on 'change' instead, since
  // re-rendering mid-typing a number or mid-picking a date is jarring and
  // unnecessary. preserveFocus() re-focuses the same input (and restores
  // its cursor position) after the innerHTML rebuild a text-input keystroke
  // triggers - without it, typing a second character would be impossible.
  document.querySelectorAll('[data-cf]').forEach(inp => {
    const key = inp.dataset.cf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'date' || inp.type === 'number') ? 'change' : 'input';
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'createdFrom') state.from = raw || null;
      else if (key === 'createdTo') state.to = raw || null;
      else if (key === 'status') state.statusFilter = raw || null;
      else if (key === 'valueMin' || key === 'valueMax') state.colFilters[key] = raw !== '' ? Number(raw) : null;
      else if (key === 'deliveryFrom' || key === 'deliveryTo') state.colFilters[key] = raw || null;
      else state.colFilters[key] = raw;
      state.tablePage = 1;
      preserveFocus(el, () => renderPoList(el));
    });
  });

  destroyPageCharts();
  // Charts are a secondary view on top of the KPIs/table already rendered
  // above - if Chart.js failed to load (e.g. its CDN script is unreachable
  // or CSP-blocked) or the draw call otherwise throws, that must not take
  // down the whole dashboard the way an uncaught error here would (it
  // would propagate to loadDashboard()'s catch and replace the entire,
  // already-correct page with the generic "couldn't load" message - a real
  // failure mode hit in this app on 2026-09-03, see security_headers.py).
  // Each chart fails on its own, visibly, with the rest of the page intact.
  if (months.length) {
    try {
      const ctx = document.getElementById('poTrendChart');
      const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, 'rgba(37,99,235,0.9)');
      gradient.addColorStop(1, 'rgba(37,99,235,0.25)');
      // Clicking a bar sets state.chartMonthFilter to that month - see the
      // "table-only filter" comment on state.chartMonthFilter. Clicking the
      // already-selected bar again clears it (same toggle pattern as a KPI
      // card). The selected bar is drawn solid navy so it's visually clear
      // which month the list below is currently narrowed to - other bars
      // keep the gradient.
      pageCharts.push(new Chart(ctx, {
        type: 'bar',
        data: {
          labels: months.map(formatMonthLabel),
          datasets: [{
            data: months.map(m => monthTotals[m]),
            backgroundColor: months.map(m => m === state.chartMonthFilter ? '#0f1b2d' : gradient),
            hoverBackgroundColor: '#2563eb',
            borderRadius: 6,
            borderSkipped: false,
            maxBarThickness: 46,
          }],
        },
        options: {
          maintainAspectRatio: false,
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const clickedMonth = months[elements[0].index];
            state.chartMonthFilter = state.chartMonthFilter === clickedMonth ? null : clickedMonth;
            state.tablePage = 1;
            renderPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#0f1b2d',
              padding: 10,
              cornerRadius: 8,
              titleFont: { size: 12, weight: '700' },
              bodyFont: { size: 12 },
              displayColors: false,
              callbacks: {
                label: c => formatInr(c.parsed.y),
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
            y: {
              grid: { color: '#eef1f5' },
              border: { display: false },
              ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) },
            },
          },
        },
      }));
    } catch (e) {
      console.error('PO trend chart failed to render:', e);
      const box = document.getElementById('poTrendChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
  if (statusChartData.length) {
    try {
      const ctx = document.getElementById('poStatusChart');
      // Clicking a slice sets statusFilter to that status - the exact same
      // state field (and toggle-off-on-repeat behavior) a KPI card click
      // already uses, so the corresponding KPI card lights up (.active
      // outline) as the same signal a click on that card would give. The
      // selected slice also pops out persistently (not just on hover) via a
      // per-index `offset`, so the selection is visible even after the
      // mouse moves away.
      pageCharts.push(new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: statusChartData.map(s => s.label),
          datasets: [{
            data: statusChartData.map(s => s.val),
            backgroundColor: statusChartData.map(s => s.color),
            borderWidth: 0,
            spacing: 3,
            borderRadius: 4,
            hoverOffset: 8,
            offset: statusChartData.map(s => state.statusFilter === s.key ? 14 : 0),
          }],
        },
        options: {
          maintainAspectRatio: false,
          cutout: '70%',
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const key = statusChartData[elements[0].index].key;
            state.statusFilter = state.statusFilter === key ? null : key;
            state.tablePage = 1;
            renderPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                boxWidth: 9,
                boxHeight: 9,
                padding: 12,
                font: { size: 10.5 },
                generateLabels: chart => {
                  const vals = chart.data.datasets[0].data;
                  const total = vals.reduce((a, b) => a + b, 0);
                  return chart.data.labels.map((label, i) => ({
                    text: label + '  ' + vals[i] + ' (' + (total ? Math.round(vals[i] / total * 100) : 0) + '%)',
                    fillStyle: chart.data.datasets[0].backgroundColor[i],
                    strokeStyle: chart.data.datasets[0].backgroundColor[i],
                    index: i,
                  }));
                },
              },
            },
            tooltip: {
              backgroundColor: '#0f1b2d',
              padding: 10,
              cornerRadius: 8,
              callbacks: {
                label: c => {
                  const total = c.dataset.data.reduce((a, b) => a + b, 0);
                  return ' ' + c.label + ': ' + c.parsed + (total ? ' (' + Math.round(c.parsed / total * 100) + '%)' : '');
                },
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
        },
        plugins: [centerTextPlugin],
      }));
    } catch (e) {
      console.error('PO status chart failed to render:', e);
      const box = document.getElementById('poStatusChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
}

// compositeKey is "<plantKey>::<poNumber>" (see plantKeyFor()) - always
// looked up in that specific plant's own cache, never the merged "All
// Plants" array, since PO numbers aren't guaranteed unique across plants.
// Two tabs, per the project owner's 2026-09-04 request (reference: the
// original "Purchase Tracker" artifact's PO detail screenshot): "Overview"
// (PO/vendor/commercial fields, card-grid layout matching that reference)
// and "Item & Stock" (the line-item table, already carrying each item's MIR
// match status - the closest this app's real data gets to the reference's
// separate "stock" concept, since there's no per-line PO<->Stock chain yet,
// see CLAUDE.md's known gaps). Deliberately doesn't invent fields the
// reference image shows but this app's data model doesn't have (ship-to/
// billing address, vendor email/contact/code, HSN, tax breakdown) - showing
// a fake "Not available" for a field that was never even attempted to be
// captured would misrepresent what this app actually extracts, vs. a field
// that was captured but happens to be blank on this PO. Only real fields
// from _po_dict() (apps/api/routers/*_views.py) are shown.
let poModalTab = 'overview';

// Re-fetches plantKey's PO list after a successful inline-edit save (the
// list endpoint is Domestic's only PO read endpoint - see hrs_views.py's
// module docstring - so a full-list invalidate+reload is how a correction's
// downstream effects (recomputed PO<->MIR match, KPI counts, flag badges)
// reach both the modal and the list behind it), then re-renders both.
async function onDomesticFieldSaved(plantKey, poNumber) {
  PURCHASE_ORDERS_BY_PLANT[plantKey] = null;
  await ensurePOsLoaded([plantKey]);
  await openPoModal(plantKey + '::' + poNumber);
  const content = document.getElementById('content');
  if (content) renderPoList(content);
}

async function openPoModal(compositeKey) {
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const poNumber = compositeKey.slice(sep + 2);
  const po = (PURCHASE_ORDERS_BY_PLANT[plantKey] || []).find(p => p.poNumber === poNumber);
  if (!po) return;
  computePoFlags(po);
  // This modal has no charts of its own, but clear any left over from a
  // previous material modal open just in case one was left dangling -
  // cheap defensive cleanup, see modalCharts' own comment.
  destroyModalCharts();
  const apiBase = PLANTS[plantKey].apiPrefix;
  const edit = (label, value, field, itemId, fieldType, options) => editableLine(plantKey, label, value, field, itemId, fieldType, options);

  const itemsHtml = (po.items || []).length
    ? '<table class="items-table"><thead><tr><th>Description</th><th>Qty</th><th>UOM</th><th>Net Price</th><th>Delivery Date</th><th>MIR Matched</th></tr></thead><tbody>' +
        po.items.map(it => {
          // Domestic line items have no stable item_id in a meaningful
          // number of real rows (see DomesticPOCorrection's docstring) -
          // itemId isn't part of _line_item_dict() at all today, so item-
          // level edits aren't wired up yet; only PO-level fields are
          // editable for now (Overview tab below).
          return '<tr><td>' + escapeHtml(it.description || '') + '</td><td>' + (it.qty != null ? it.qty : '-') +
          '</td><td>' + escapeHtml(it.uom || '') + '</td><td>' + (it.netPrice != null ? formatInr(it.netPrice) : '-') +
          '</td><td>' + escapeHtml(formatDateIN(it.deliveryDate)) + '</td><td>' +
          matchStatusHtml(it, plantKey) +
          '</td></tr>';
        }).join('') + '</tbody></table>'
    : '<div style="font-size:12.5px;color:var(--slate-soft);">No line items recorded.</div>';

  const currencyOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.currency);
  const incotermsOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.incoterms);
  const taxTypeOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.taxType);

  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Purchase Order</h4>' +
        '<div class="line">PO No: ' + escapeHtml(po.poNumber) + '</div>' +
        edit('Created on', po.createdDate, 'po_created_date', null, 'date') +
        '<div class="line">Plant: ' + escapeHtml(PLANTS[plantKey].label) + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>Vendor</h4>' +
        edit('Vendor Name', po.vendorName, 'vendor_name') +
        edit('Vendor Address', po.vendorAddress, 'vendor_address') +
        edit('Vendor GSTIN', po.vendorGstin, 'vendor_gstin') +
        edit('Vendor Email', po.vendorEmail, 'vendor_email') +
        edit('Vendor Code', po.vendorCode, 'vendor_code') +
      '</div>' +
      '<div class="field-block"><h4>Billing and Ship To</h4>' +
        edit('Billing Address', po.billingAddress, 'billing_address') +
        edit('Ship To', po.shipTo, 'ship_to') +
      '</div>' +
      '<div class="field-block"><h4>Packing and Incoterms</h4>' +
        edit('Payment Terms', po.paymentTerms, 'payment_terms') +
        edit('Incoterms', po.incoterms, 'incoterms', null, 'select', incotermsOptions) +
        edit('Currency', po.currency, 'currency', null, 'select', currencyOptions) +
      '</div>' +
      '<div class="field-block"><h4>Value</h4>' +
        edit('Total Value', po.totalValue, 'total_value', null, 'number') +
        edit('Tax Type', po.taxType, 'tax_type', null, 'select', taxTypeOptions) +
        edit('Total Inclusive Value', po.totalInclTax, 'total_inclusive_value', null, 'number') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width" style="margin-top:14px;"><h4>Remarks</h4>' +
      edit('Remarks', po.remarks, 'remarks') + '</div>';

  const itemStockHtml =
    '<div style="margin:0 0 16px;">' + miniStepperHtml(po) + '</div>' +
    '<div class="section-title" style="margin-top:0;">Material / Product Details</div>' + itemsHtml;

  const catsHtml = (po._categories || []).length
    ? po._categories.map(c =>
        '<div class="field-block" style="margin-bottom:8px;">' +
          flagIconHtml(categoryColor(c.label), 'row-flag-icon') +
          ' <span class="lg-label ' + c.severity + '">' + (c.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span> ' +
          '<b>' + escapeHtml(c.label) + '</b>' +
        '</div>'
      ).join('')
    : '<div style="text-align:center;color:var(--slate-soft);padding:14px;">No data quality flags on this PO.</div>';
  const correctionsHtml = (po.corrections || []).length
    ? '<div class="section-title" style="margin-top:18px;">Correction History</div>' +
      po.corrections.map(c =>
        '<div class="field-block" style="margin-bottom:8px;">' +
          '<div style="font-size:12.5px;"><b>' + escapeHtml(c.fieldName) + (c.itemId ? ' (item ' + escapeHtml(c.itemId) + ')' : '') + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') + '</div>' +
          '<div style="margin-top:4px;font-size:11px;color:var(--slate-soft);">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? c.correctedAt.slice(0, 10) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const flagsTabHtml = catsHtml + correctionsHtml;

  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };
  poModalTab = poModalTab || 'overview';
  body.innerHTML =
    '<span class="close-btn" onclick="closeModal()">&times;</span>' +
    '<h2>' + escapeHtml(po.poNumber) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(po.vendorName || 'Unknown vendor') + ' &middot; ' + escapeHtml(PLANTS[plantKey].label) + ' &middot; ' + (po.createdDate ? escapeHtml(formatDateIN(po.createdDate)) : 'no date') + '</div>' +
    '<div class="modal-tabs" id="poModalTabs">' +
      '<div class="modal-tab' + (poModalTab === 'overview' ? ' active' : '') + '" data-tab="overview">Overview</div>' +
      '<div class="modal-tab' + (poModalTab === 'itemstock' ? ' active' : '') + '" data-tab="itemstock">Item &amp; Stock</div>' +
      '<div class="modal-tab' + (poModalTab === 'flags' ? ' active' : '') + '" data-tab="flags">Flags &amp; Corrections</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="poModalOverview"' + (poModalTab !== 'overview' ? ' hidden' : '') + '>' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="poModalItemStock"' + (poModalTab !== 'itemstock' ? ' hidden' : '') + '>' + itemStockHtml + '</div>' +
    '<div class="modal-tab-panel" id="poModalFlags"' + (poModalTab !== 'flags' ? ' hidden' : '') + '>' + flagsTabHtml + '</div>';

  body.querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    poModalTab = tab.dataset.tab;
    body.querySelectorAll('[data-tab]').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('poModalOverview').hidden = poModalTab !== 'overview';
    document.getElementById('poModalItemStock').hidden = poModalTab !== 'itemstock';
    document.getElementById('poModalFlags').hidden = poModalTab !== 'flags';
  });

  const fieldsUrl = apiBase + '/purchase-orders/' + encodeURIComponent(poNumber) + '/fields';
  wireEditableLines(body, fieldsUrl, (fieldName, itemId) => onDomesticFieldSaved(plantKey, poNumber));
  wireDismissLinks(body, plantKey, () => onDomesticFieldSaved(plantKey, poNumber));
}

function closeModal() { document.getElementById('modalBackdrop').classList.remove('open'); destroyModalCharts(); }

// ------------------------------------------------------------------
// Import Purchases (purchaseType 'import') - same KPI-row / chart-row /
// list / drill-down-modal scaffold as Domestic (renderPoList()/openPoModal()
// above), fed from apps/api/routers/imports_views.py instead of PO<->MIR
// match data. Differs from Domestic in the few places the underlying data
// genuinely differs: shipment stage (Placed/Shipped(BL)/Cleared(BOE)) instead
// of match status, BOE/BL/country-of-origin fields, and inline-edit
// corrections (a real mutating endpoint - see ImportPOCorrection in
// apps/core/models.py) since Domestic has no equivalent "fix a bad value in
// place" workflow yet.

const IMPORT_STAGES = ['Placed', 'Shipped (BL)', 'Cleared (BOE)'];
const IMPORT_STAGE_LABELS = { 'Placed': 'Awaiting Bill of Lading', 'Shipped (BL)': 'In Transit', 'Cleared (BOE)': 'Customs Cleared (BOE)' };
const IMPORT_STAGE_PILL_CLASS = { 'Placed': 'status-pending', 'Shipped (BL)': 'status-partial', 'Cleared (BOE)': 'status-received' };

// Same markup/CSS classes as miniStepperHtml()/materialStepperHtml() (3
// dots/2 lines instead of 2 dots/1 line - .mini-stepper's flex layout
// doesn't care how many of each it's given).
function shipmentStepperHtml(po) {
  const idx = IMPORT_STAGES.indexOf(po.shipmentStage);
  const dots = IMPORT_STAGES.map((s, i) =>
    (i > 0 ? '<span class="step-line ' + (i <= idx ? 'done' : '') + '"></span>' : '') +
    '<span class="step-dot ' + (i <= idx ? 'done' : '') + '"></span>'
  ).join('');
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(po.shipmentStage) + '"><div class="mini-stepper">' + dots + '</div>' +
    '<div class="mini-stepper-labels">Placed / Shipped / Cleared</div></div>';
}

// Same idea as renderPoList()'s rowFlags(), but reading dataQualityFlags
// (apps/services/import_flags.py's F1-F7) instead of the domestic
// remarks-regex categorization - one purple icon per flag code present.
function importRowFlags(po) {
  return (po.dataQualityFlags || []).map(f =>
    ' <span class="row-flag-wrap" data-tooltip="' + escapeHtml(f.code + ': ' + f.message) + '">' + flagIconHtml(KPI_FLAG_COLORS.quality, 'row-flag-icon') + '</span>'
  ).join('');
}

function applyImportColFilters(recs) {
  const f = state.importColFilters;
  return recs.filter(po => {
    if (f.poNumber && !(po.poNumber || '').toLowerCase().includes(f.poNumber.toLowerCase())) return false;
    if (f.vendor && !(po.vendorName || '').toLowerCase().includes(f.vendor.toLowerCase())) return false;
    if (f.country && !(po.countryOfOrigin || '').toLowerCase().includes(f.country.toLowerCase())) return false;
    if (f.valueMin != null && (po.totalInclusiveValue || 0) < f.valueMin) return false;
    if (f.valueMax != null && (po.totalInclusiveValue || 0) > f.valueMax) return false;
    if (f.stage && po.shipmentStage !== f.stage) return false;
    return true;
  });
}

function renderImportPoList(el) {
  const all = currentImportPOs();
  if (!all.length) {
    const msg = isAllPlants()
      ? 'No import purchase orders synced yet for any plant.'
      : 'No import purchase orders synced yet for ' + PLANTS[state.plant].label + ' - run <code>' +
        { hrs: 'sync_hrs_imports_po_csv', achhad: 'sync_achhad_imports_po_csv', vapi: 'sync_vapi_imports_po_csv' }[state.plant] +
        '</code> to load them.';
    el.innerHTML = '<div class="empty-state">' + msg + '</div>';
    return;
  }

  const inRange = po => {
    if (state.importFrom && (!po.createdDate || po.createdDate < state.importFrom)) return false;
    if (state.importTo && (!po.createdDate || po.createdDate > state.importTo)) return false;
    return true;
  };
  const filtered = all.filter(inRange);
  const total = filtered.length;

  // A PO "has" a MIR condition if ANY of its line items does - same
  // any-item-triggers-the-PO-level-flag convention Domestic's own
  // po_has_qty_discrepancy() uses server-side; there's no server-computed
  // PO-level MIR aggregate for imports (only per-item mirMatch), so this is
  // done client-side here instead.
  const poInwarded = p => (p.items || []).some(i => i.mirMatch);
  const poQtyDiscMir = p => (p.items || []).some(i => i.mirMatch && i.mirMatch.qtyDiffPct > 0);
  const poRateDiscMir = p => (p.items || []).some(i => i.mirMatch && i.mirMatch.rateDiffPct > 0);

  const counts = {
    inwarded: filtered.filter(poInwarded).length,
    partial: filtered.filter(p => p.partialDelivery).length,
    qtyDisc: filtered.filter(p => p.qtyDiscrepancy).length,
    qtyDiscMir: filtered.filter(poQtyDiscMir).length,
    rateDiscMir: filtered.filter(poRateDiscMir).length,
    overdue: filtered.filter(p => p.deliveryDateStatus === 'Overdue').length,
    onOrder: filtered.filter(p => p.deliveryDateStatus === 'On Order').length,
    unknownDate: filtered.filter(p => p.deliveryDateStatus === 'Unknown').length,
    flags: filtered.filter(p => p.dataQualityFlags && p.dataQualityFlags.length).length,
    placed: filtered.filter(p => p.shipmentStage === 'Placed').length,
    shipped: filtered.filter(p => p.shipmentStage === 'Shipped (BL)').length,
    cleared: filtered.filter(p => p.shipmentStage === 'Cleared (BOE)').length,
  };

  // Same card shape/order philosophy as Domestic's cardDef (file/comment
  // above renderPoList()'s own cardDef): overall total, then the MIR-backed
  // cards (enabled 2026-09-04 - MIR/Stock are shared Drive files across
  // domestic and import purchases for a plant, see
  // apps/services/matching.py's match_import_po_mir_line_item() docstring;
  // these were "Awaiting MIR" placeholders before that was confirmed), then
  // the discrepancy/timing/flag cards, then the import-specific
  // shipment-stage trio Domestic has no equivalent of.
  const cardDef = [
    { key: 'total', cls: '', label: 'Total Import POs', val: total },
    { key: 'inwarded', cls: 'received', label: 'Material Inwarded', val: counts.inwarded, flag: KPI_FLAG_COLORS.received },
    { key: 'partial', cls: 'partial', label: 'Partial Delivered', val: counts.partial, flag: KPI_FLAG_COLORS.partial },
    { key: 'qtydisc', cls: 'critical', label: 'Qty Discrepancies (PO vs BOE)', val: counts.qtyDisc, flag: KPI_FLAG_COLORS.critical },
    { key: 'qtydiscmir', cls: 'critical', label: 'Qty Discrepancies (BOE vs MIR)', val: counts.qtyDiscMir, flag: KPI_FLAG_COLORS.critical },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Discrepancies', val: counts.rateDiscMir, flag: KPI_FLAG_COLORS.critical },
    { key: 'overdue', cls: 'overdue', label: 'Overdue', val: counts.overdue, flag: KPI_FLAG_COLORS.critical },
    { key: 'onorder', cls: 'pending', label: 'Pending Deliveries / On Order', val: counts.onOrder, flag: KPI_FLAG_COLORS.pending },
    { key: 'unknowndate', cls: 'unknown', label: 'Delivery Date Unknown', val: counts.unknownDate, flag: KPI_FLAG_COLORS.unknown },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: counts.flags, flag: KPI_FLAG_COLORS.quality },
    { key: 'placed', cls: 'pending', label: 'Awaiting Bill of Lading', val: counts.placed, flag: KPI_FLAG_COLORS.pending },
    { key: 'shipped', cls: 'partial', label: 'In Transit', val: counts.shipped, flag: KPI_FLAG_COLORS.partial },
    { key: 'cleared', cls: 'received', label: 'Customs Cleared (BOE)', val: counts.cleared, flag: KPI_FLAG_COLORS.received },
  ];
  const kpiHtml = cardDef.map(c => {
    const active = !c.disabled && state.importStatusFilter === c.key;
    return '<div class="kpi-card ' + c.cls + (active ? ' active' : '') + (c.disabled ? ' kpi-disabled' : '') + '" data-kpi="' + (c.disabled ? '' : c.key) + '">' +
      (c.flag ? flagIconHtml(c.flag) : '') +
      '<div class="val' + (typeof c.val === 'string' ? ' val-text' : '') + '">' + (typeof c.val === 'string' ? escapeHtml(c.val) : c.val) + '</div>' +
      '<div class="label">' + escapeHtml(c.label) + '</div></div>';
  }).join('');

  let tableRecs = filtered;
  const sf = state.importStatusFilter;
  if (sf === 'inwarded') tableRecs = filtered.filter(poInwarded);
  else if (sf === 'partial') tableRecs = filtered.filter(p => p.partialDelivery);
  else if (sf === 'qtydisc') tableRecs = filtered.filter(p => p.qtyDiscrepancy);
  else if (sf === 'qtydiscmir') tableRecs = filtered.filter(poQtyDiscMir);
  else if (sf === 'ratedisc') tableRecs = filtered.filter(poRateDiscMir);
  else if (sf === 'overdue') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'Overdue');
  else if (sf === 'onorder') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'On Order');
  else if (sf === 'unknowndate') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'Unknown');
  else if (sf === 'flags') tableRecs = filtered.filter(p => p.dataQualityFlags && p.dataQualityFlags.length);
  else if (sf === 'placed') tableRecs = filtered.filter(p => p.shipmentStage === 'Placed');
  else if (sf === 'shipped') tableRecs = filtered.filter(p => p.shipmentStage === 'Shipped (BL)');
  else if (sf === 'cleared') tableRecs = filtered.filter(p => p.shipmentStage === 'Cleared (BOE)');
  if (state.importChartMonthFilter) tableRecs = tableRecs.filter(po => (po.createdDate || '').slice(0, 7) === state.importChartMonthFilter);
  tableRecs = applyImportColFilters(tableRecs);
  tableRecs = tableRecs.slice().sort((a, b) => (b.createdDate || '').localeCompare(a.createdDate || ''));

  const stageChartData = [
    { key: 'placed', label: IMPORT_STAGE_LABELS['Placed'], val: counts.placed, color: '#d97706' },
    { key: 'shipped', label: IMPORT_STAGE_LABELS['Shipped (BL)'], val: counts.shipped, color: '#2563eb' },
    { key: 'cleared', label: IMPORT_STAGE_LABELS['Cleared (BOE)'], val: counts.cleared, color: '#16a34a' },
  ].filter(s => s.val > 0);

  const monthTotals = {};
  filtered.forEach(po => {
    const m = (po.createdDate || '').slice(0, 7);
    if (!m) return;
    monthTotals[m] = (monthTotals[m] || 0) + (po.totalInclusiveValue || 0);
  });
  const months = Object.keys(monthTotals).sort();

  const showingAll = state.importShowAllPOs;
  const totalForList = tableRecs.length;
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(totalForList / PAGE_SIZE));
  const tablePage = Math.min(Math.max(1, state.importTablePage), totalPages);
  const listRecs = showingAll ? tableRecs.slice((tablePage - 1) * PAGE_SIZE, tablePage * PAGE_SIZE) : tableRecs.slice(0, 5);

  const cf = state.importColFilters;
  const activeFilterCount =
    (state.importChartMonthFilter ? 1 : 0) +
    (state.importStatusFilter ? 1 : 0) +
    (state.importFrom || state.importTo ? 1 : 0) +
    (cf.poNumber ? 1 : 0) + (cf.vendor ? 1 : 0) + (cf.country ? 1 : 0) +
    (cf.valueMin != null || cf.valueMax != null ? 1 : 0) + (cf.stage ? 1 : 0);

  const filterCells = [
    '<input type="text" class="col-filter-input" data-icf="poNumber" placeholder="Search..." value="' + escapeHtml(cf.poNumber) + '">',
    '<input type="text" class="col-filter-input" data-icf="vendor" placeholder="Search..." value="' + escapeHtml(cf.vendor) + '">',
    '<input type="text" class="col-filter-input" data-icf="country" placeholder="Search..." value="' + escapeHtml(cf.country) + '">',
    '<div class="col-filter-range"><input type="number" class="col-filter-num" data-icf="valueMin" placeholder="Min" value="' + (cf.valueMin != null ? cf.valueMin : '') + '"><input type="number" class="col-filter-num" data-icf="valueMax" placeholder="Max" value="' + (cf.valueMax != null ? cf.valueMax : '') + '"></div>',
    '',
    '<select class="col-filter-input" data-icf="stage"><option value="">All</option>' +
      IMPORT_STAGES.map(s => '<option value="' + s + '"' + (cf.stage === s ? ' selected' : '') + '>' + escapeHtml(IMPORT_STAGE_LABELS[s]) + '</option>').join('') +
    '</select>',
    '',
  ];

  el.innerHTML =
    '<div class="filter-row">' +
      '<label>Date filter (Created on)</label>' +
      '<input type="date" id="importFromDate" value="' + (state.importFrom || '') + '">' +
      '<span style="color:#9ca3af;font-size:12px;">to</span>' +
      '<input type="date" id="importToDate" value="' + (state.importTo || '') + '">' +
      '<button class="primary" id="importApplyFilter">Apply</button>' +
      '<button id="importClearFilter">Clear</button>' +
    '</div>' +
    '<div class="kpi-grid">' + kpiHtml + '</div>' +
    ((stageChartData.length || months.length) ?
      '<div class="chart-row">' +
        '<div class="chart-panel"><h4>Import Value Trend by Month Created</h4>' +
          (months.length ? '<div class="chart-box"><canvas id="importTrendChart"></canvas></div>' : '<div class="no-data-note">No dated POs in range to plot.</div>') +
        '</div>' +
        '<div class="chart-panel"><h4>Shipment Stage Breakdown</h4>' +
          (stageChartData.length ? '<div class="chart-box"><canvas id="importStageChart"></canvas></div>' : '<div class="no-data-note">No POs in range.</div>') +
        '</div>' +
      '</div>' : '') +
    '<div class="list-toggle-row"><div class="section-title" style="margin:0;">Import Purchase Orders (Latest first)</div>' +
      '<div style="display:flex;align-items:center;gap:10px;">' +
        (activeFilterCount ? '<span class="clear-list-filters" id="importClearListFilters">' + activeFilterCount + ' filter' + (activeFilterCount > 1 ? 's' : '') + ' active &middot; Clear &times;</span>' : '') +
        (totalForList > 5 ? '<button class="view-all-btn" id="importToggleAllBtn">' + (showingAll ? 'Show top 5' : 'View all') + '</button>' : '') +
      '</div>' +
    '</div>' +
    (() => {
      if (showingAll) {
        const colFilterRow = '<tr class="col-filter-row">' + filterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';
        const pageButtons = totalPages <= 10
          ? Array.from({ length: totalPages }, (_, i) => i + 1)
              .map(p => '<button class="page-btn page-num' + (p === tablePage ? ' active' : '') + '" data-impage="' + p + '">' + p + '</button>')
              .join('')
          : '<span class="page-info">Page ' + tablePage + ' of ' + totalPages + '</span>';
        const paginationHtml = totalPages > 1
          ? '<div class="pagination-row">' +
              '<button id="importPrevPageBtn" class="page-btn"' + (tablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
              pageButtons +
              '<button id="importNextPageBtn" class="page-btn"' + (tablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
            '</div>'
          : '';
        return '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Country of Origin</th><th>Value (Incl.)</th><th>BL Number</th><th>Shipment Stage</th><th>Details</th></tr>' + colFilterRow + '</thead>' +
          '<tbody>' + listRecs.map(po => {
            const key = escapeHtml(po.plant + '::' + po.poNumber);
            return '<tr><td><b>' + escapeHtml(po.poNumber) + '</b></td>' +
              '<td>' + escapeHtml(po.vendorName || '-') + '</td>' +
              '<td>' + escapeHtml(po.countryOfOrigin || '-') + '</td>' +
              '<td>' + (po.totalInclusiveValue != null ? formatInr(po.totalInclusiveValue) : '-') + '</td>' +
              '<td>' + (po.billOfLadingNumber ? escapeHtml(po.billOfLadingNumber) : '-') + '</td>' +
              '<td><span class="status-pill ' + IMPORT_STAGE_PILL_CLASS[po.shipmentStage] + '">' + escapeHtml(po.shipmentStage) + '</span>' + importRowFlags(po) + '</td>' +
              '<td><span class="row-link" data-impo="' + key + '">View details</span></td></tr>';
          }).join('') + '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row po-cols"><div>PO Number</div><div>Vendor</div><div>Country of Origin</div><div>Value (Incl.)</div><div>BL Number</div><div>Shipment Stage</div><div>Details</div></div>' +
        '<div class="list-header-row po-cols col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="importTop5List">' + listRecs.map(po => {
          const key = escapeHtml(po.plant + '::' + po.poNumber);
          return '<div class="top5-row">' +
            '<div><span class="po-num">' + escapeHtml(po.poNumber) + '</span></div>' +
            '<div>' + escapeHtml(po.vendorName || 'Not available') + '</div>' +
            '<div>' + escapeHtml(po.countryOfOrigin || 'Not available') + '</div>' +
            '<div>' + (po.totalInclusiveValue != null ? formatInr(po.totalInclusiveValue) : 'Not available') + '</div>' +
            '<div>' + (po.billOfLadingNumber ? escapeHtml(po.billOfLadingNumber) : 'Not available') + '</div>' +
            '<div><span class="status-pill ' + IMPORT_STAGE_PILL_CLASS[po.shipmentStage] + '">' + escapeHtml(po.shipmentStage) + '</span>' + importRowFlags(po) + '</div>' +
            '<div><span class="row-link" data-impo="' + key + '">View details</span></div></div>';
        }).join('') + '</div>';
    })();

  document.querySelectorAll('[data-kpi]').forEach(c => {
    if (!c.dataset.kpi) return; // disabled (MIR-placeholder) card - no filter to set
    c.onclick = () => {
      const key = c.dataset.kpi;
      state.importStatusFilter = (state.importStatusFilter === key || key === 'total') ? null : key;
      state.importTablePage = 1;
      renderImportPoList(el);
    };
  });
  document.getElementById('importApplyFilter').onclick = () => {
    state.importFrom = document.getElementById('importFromDate').value || null;
    state.importTo = document.getElementById('importToDate').value || null;
    state.importTablePage = 1;
    renderImportPoList(el);
  };
  document.getElementById('importClearFilter').onclick = () => { state.importFrom = null; state.importTo = null; state.importTablePage = 1; renderImportPoList(el); };
  const toggleBtn = document.getElementById('importToggleAllBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.importShowAllPOs = !state.importShowAllPOs; state.importTablePage = 1; renderImportPoList(el); };
  document.querySelectorAll('[data-impo]').forEach(el2 => el2.onclick = () => openImportPoModal(el2.dataset.impo));

  const prevPageBtn = document.getElementById('importPrevPageBtn');
  if (prevPageBtn) prevPageBtn.onclick = () => { state.importTablePage = Math.max(1, state.importTablePage - 1); renderImportPoList(el); };
  const nextPageBtn = document.getElementById('importNextPageBtn');
  if (nextPageBtn) nextPageBtn.onclick = () => { state.importTablePage = state.importTablePage + 1; renderImportPoList(el); };
  document.querySelectorAll('[data-impage]').forEach(btn => btn.onclick = () => { state.importTablePage = Number(btn.dataset.impage); renderImportPoList(el); });

  const clearListFiltersBtn = document.getElementById('importClearListFilters');
  if (clearListFiltersBtn) clearListFiltersBtn.onclick = () => {
    state.importStatusFilter = null; state.importChartMonthFilter = null; state.importFrom = null; state.importTo = null;
    state.importTablePage = 1;
    state.importColFilters = { poNumber: '', vendor: '', country: '', valueMin: null, valueMax: null, stage: '' };
    renderImportPoList(el);
  };
  document.querySelectorAll('[data-icf]').forEach(inp => {
    const key = inp.dataset.icf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'number') ? 'change' : 'input';
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      state.importColFilters[key] = (key === 'valueMin' || key === 'valueMax') ? (raw !== '' ? Number(raw) : null) : raw;
      state.importTablePage = 1;
      preserveFocus(el, () => renderImportPoList(el));
    });
  });

  destroyPageCharts();
  if (months.length) {
    try {
      const ctx = document.getElementById('importTrendChart');
      const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, 'rgba(29,138,150,0.9)');
      gradient.addColorStop(1, 'rgba(29,138,150,0.25)');
      pageCharts.push(new Chart(ctx, {
        type: 'bar',
        data: {
          labels: months.map(formatMonthLabel),
          datasets: [{
            data: months.map(m => monthTotals[m]),
            backgroundColor: months.map(m => m === state.importChartMonthFilter ? '#0f1b2d' : gradient),
            hoverBackgroundColor: '#1d8a96',
            borderRadius: 6, borderSkipped: false, maxBarThickness: 46,
          }],
        },
        options: {
          maintainAspectRatio: false,
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const clickedMonth = months[elements[0].index];
            state.importChartMonthFilter = state.importChartMonthFilter === clickedMonth ? null : clickedMonth;
            state.importTablePage = 1;
            renderImportPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8,
              titleFont: { size: 12, weight: '700' }, bodyFont: { size: 12 }, displayColors: false,
              callbacks: { label: c => formatInr(c.parsed.y), afterLabel: () => 'Click to filter the list below' },
            },
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
            y: { grid: { color: '#eef1f5' }, border: { display: false }, ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) } },
          },
        },
      }));
    } catch (e) {
      console.error('Import trend chart failed to render:', e);
      const box = document.getElementById('importTrendChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
  if (stageChartData.length) {
    try {
      const ctx = document.getElementById('importStageChart');
      pageCharts.push(new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: stageChartData.map(s => s.label),
          datasets: [{
            data: stageChartData.map(s => s.val),
            backgroundColor: stageChartData.map(s => s.color),
            borderWidth: 0, spacing: 3, borderRadius: 4, hoverOffset: 8,
            offset: stageChartData.map(s => state.importStatusFilter === s.key ? 14 : 0),
          }],
        },
        options: {
          maintainAspectRatio: false, cutout: '70%',
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const key = stageChartData[elements[0].index].key;
            state.importStatusFilter = state.importStatusFilter === key ? null : key;
            state.importTablePage = 1;
            renderImportPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                boxWidth: 9, boxHeight: 9, padding: 12, font: { size: 10.5 },
                generateLabels: chart => {
                  const vals = chart.data.datasets[0].data;
                  const totalV = vals.reduce((a, b) => a + b, 0);
                  return chart.data.labels.map((label, i) => ({
                    text: label + '  ' + vals[i] + ' (' + (totalV ? Math.round(vals[i] / totalV * 100) : 0) + '%)',
                    fillStyle: chart.data.datasets[0].backgroundColor[i],
                    strokeStyle: chart.data.datasets[0].backgroundColor[i],
                    index: i,
                  }));
                },
              },
            },
            tooltip: {
              backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8,
              callbacks: {
                label: c => {
                  const totalV = c.dataset.data.reduce((a, b) => a + b, 0);
                  return ' ' + c.label + ': ' + c.parsed + (totalV ? ' (' + Math.round(c.parsed / totalV * 100) + '%)' : '');
                },
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
        },
        plugins: [centerImportTextPlugin],
      }));
    } catch (e) {
      console.error('Import stage chart failed to render:', e);
      const box = document.getElementById('importStageChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
}

// Same idea as centerTextPlugin (PO status donut) - "TOTAL POs" -> "IMPORT
// POs" center label, everything else identical.
const centerImportTextPlugin = {
  id: 'importStageCenterText',
  afterDraw(chart) {
    const area = chart.chartArea;
    if (!area) return;
    const data = chart.data.datasets[0].data;
    const totalV = data.reduce((a, b) => a + b, 0);
    const ctx = chart.ctx;
    const cx = (area.left + area.right) / 2;
    const cy = (area.top + area.bottom) / 2;
    ctx.save();
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#0f1b2d';
    ctx.fillText(String(totalV), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#94a3b8';
    ctx.fillText('IMPORT POs', cx, cy + 11);
    ctx.restore();
  },
};

// compositeKey is "<plant>::<poNumber>" (po.plant is already the imports
// API's plant key - see currentImportPOs()'s comment). Unlike openPoModal()
// this needs a real fetch: the list payload already carries everything the
// KPIs/charts/table need, but not the Overview tab's vendor address/GSTIN/
// email/code/billing/ship-to/currency/remarks (detail-only fields, see
// imports_views.py's _po_dict()) - fetched once per PO and cached.
async function openImportPoModal(compositeKey) {
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const poNumber = compositeKey.slice(sep + 2);

  destroyModalCharts();
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };
  body.innerHTML = '<span class="close-btn" onclick="closeModal()">&times;</span><div class="load-banner"><div class="spinner"></div><div>Loading&hellip;</div></div>';

  let po;
  try {
    po = await ensureImportPoDetailLoaded(plantKey, poNumber);
  } catch (e) {
    console.error('Failed to load import PO detail:', e);
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    body.innerHTML = '<span class="close-btn" onclick="closeModal()">&times;</span><div class="noaccess">Couldn\'t load this purchase order. Please close and try again.</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
  renderImportPoModalBody(plantKey, poNumber, po);
}

async function ensureImportPoDetailLoaded(plantKey, poNumber, force) {
  const cacheKey = plantKey + '|' + poNumber;
  if (IMPORT_PO_DETAIL_CACHE[cacheKey] && !force) return IMPORT_PO_DETAIL_CACHE[cacheKey];
  const data = await apiImports('/purchase-orders/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(poNumber));
  IMPORT_PO_DETAIL_CACHE[cacheKey] = data;
  return data;
}

let importModalTab = 'overview';

// editableLine/plainLine/wireEditableLines/savePoField are shared with
// Domestic's openPoModal() - see frontend/js/shared.js. itemId is omitted
// for a PO-level field, present for a line-item field (matches
// correct_field's own PO-level-vs-item-level branch on both the Domestic
// and Import backends).
function renderImportPoModalBody(plantKey, poNumber, po) {
  importModalTab = importModalTab || 'overview';
  const body = document.getElementById('modalBody');
  const apiBase = '/api/imports';
  const edit = (label, value, field, itemId, fieldType, options) => editableLine(plantKey, label, value, field, itemId, fieldType, options);

  const currencyOptions = distinctFieldValues(IMPORT_PO_CACHE || [], p => p.currency);
  const taxTypeOptions = Array.from(new Set(
    (IMPORT_PO_CACHE || []).flatMap(p => (p.items || []).map(i => i.taxType)).filter(Boolean)
  )).sort();

  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Purchase Order</h4>' +
        plainLine('PO No', po.poNumber) +
        edit('Created on', po.createdDate, 'po_created_date', null, 'date') +
        plainLine('Plant', po.plantLabel + ' (Imports)') +
      '</div>' +
      '<div class="field-block"><h4>Vendor</h4>' +
        edit('Vendor Name', po.vendorName, 'vendor_name') +
        edit('Vendor Address', po.vendorAddress, 'vendor_address') +
        edit('Vendor GSTIN', po.vendorGstin, 'vendor_gstin') +
        edit('Vendor Email', po.vendorEmail, 'vendor_email') +
        edit('Vendor Code', po.vendorCode, 'vendor_code') +
      '</div>' +
      '<div class="field-block"><h4>Billing and Ship To</h4>' +
        edit('Billing Address', po.billingAddress, 'billing_address') +
        edit('Ship To', po.shipTo, 'ship_to') +
      '</div>' +
      '<div class="field-block"><h4>Payment and Incoterms</h4>' +
        edit('Payment Terms', po.paymentTerms, 'payment_terms') +
        edit('Incoterms', po.incoterms, 'incoterms') +
        edit('Currency (as per PO)', po.currency, 'currency', null, 'select', currencyOptions) +
      '</div>' +
      '<div class="field-block"><h4>Value</h4>' +
        edit('Total Value (as per PO, in ' + (po.currency || 'PO currency') + ')', po.totalValue, 'total_value', null, 'number') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width" style="margin-top:14px;"><h4>Remarks</h4>' + edit('Remarks', po.remarks, 'remarks') + '</div>';

  const itemsHtml = (po.items || []).length
    ? '<table class="items-table"><thead><tr><th>Item Id</th><th>Description</th><th>HSN</th><th>Qty (As Per PO)</th><th>Qty (As Per BOE)</th><th>Variance</th><th>Net Price</th><th>Net Value</th><th>MIR Match</th></tr></thead><tbody>' +
        po.items.map(it => {
          const variance = it.qtyDiscrepancyPct != null ? it.qtyDiscrepancyPct.toFixed(1) + '%' : '-';
          const color = it.qtyDiscrepancy ? (it.qtyDiscrepancyPct >= 0 ? 'var(--green)' : 'var(--red)') : 'inherit';
          return '<tr><td>' + escapeHtml(it.itemId || '-') + '</td><td>' + escapeHtml(it.description || '') + '</td><td>' + escapeHtml(it.hsn || '-') +
            '</td><td>' + (it.qtyAsPerPo != null ? it.qtyAsPerPo : '-') + ' ' + escapeHtml(it.uom || '') +
            '</td><td>' + (it.qtyAsPerBoe != null ? it.qtyAsPerBoe : '-') + ' ' + escapeHtml(it.uom || '') +
            '</td><td style="color:' + color + ';font-weight:700;">' + variance + '</td>' +
            '<td>' + (it.netPrice != null ? it.netPrice : '-') + '</td><td>' + (it.netValue != null ? formatInr(it.netValue) : '-') + '</td>' +
            '<td>' + importMatchStatusHtml(it, plantKey) + '</td></tr>';
        }).join('') + '</tbody></table>'
    : '<div style="font-size:12.5px;color:var(--slate-soft);">No line items recorded.</div>';
  const itemsTabHtml = '<div class="section-title" style="margin-top:0;">Material / Product Details</div>' + itemsHtml;

  const shipmentTabHtml = '<div style="margin:0 0 16px;">' + shipmentStepperHtml(po) + '</div>' +
    (po.items || []).map(it =>
      '<div class="field-grid">' +
        '<div class="field-block"><h4>Shipment and Customs' + (it.itemId ? ' - Item ' + escapeHtml(it.itemId) : '') + '</h4>' +
          edit('Bill of Lading No.', it.billOfLadingNumber, 'bill_of_lading_number', it.itemId) +
          edit('Laden on Board date', it.ladenOnBoardDate, 'laden_on_board_date', it.itemId, 'date') +
          edit('BOE (Bill of Entry) No.', it.boeNumber, 'boe_number', it.itemId) +
          edit('Exchange Rate', it.exchangeRate, 'exchange_rate', it.itemId, 'number') +
          edit('Tax Type', it.taxType, 'tax_type', it.itemId, 'select', taxTypeOptions) +
          plainLine('Total Inclusive Value', it.totalInclusiveValue != null ? formatInr(it.totalInclusiveValue) : null) +
        '</div>' +
        '<div class="field-block"><h4>Export Incentive / License Scheme' + (it.itemId ? ' - Item ' + escapeHtml(it.itemId) : '') + '</h4>' +
          edit('License Type', it.licenseType, 'license_type', it.itemId) +
          edit('License / Scrip Number(s)', it.licenseNumber, 'license_number', it.itemId) +
        '</div>' +
      '</div>'
    ).join('');

  const FLAG_DESCRIPTIONS_NOTE = 'Each flag below is a real data-quality check against this PO\'s own synced fields (apps/services/import_flags.py) - not a placeholder.';
  const flagsTabHtml = (po.dataQualityFlags || []).length
    ? '<div class="no-data-note" style="margin-bottom:12px;">' + FLAG_DESCRIPTIONS_NOTE + '</div>' +
      po.dataQualityFlags.map(f =>
        '<div class="field-block" style="margin-bottom:10px;">' +
          '<span class="status-pill status-overdue">' + escapeHtml(f.code) + '</span>' +
          '<div style="margin-top:8px;font-size:13px;">' + escapeHtml(f.message) + '</div>' +
          '<div style="margin-top:6px;font-size:11px;color:var(--slate-soft);">Fields: ' + escapeHtml((f.fields || []).join(', ')) + '</div>' +
          (f.item_id ? '<div style="margin-top:4px;font-size:11px;color:var(--slate-soft);">Item: ' + escapeHtml(f.item_id) + '</div>' : '') +
        '</div>'
      ).join('')
    : '<div style="text-align:center;color:var(--slate-soft);padding:20px;">No data quality flags on this PO.</div>';
  const correctionsHtml = (po.corrections || []).length
    ? '<div class="section-title" style="margin-top:18px;">Correction History</div>' +
      po.corrections.map(c =>
        '<div class="field-block" style="margin-bottom:8px;">' +
          '<div style="font-size:12.5px;"><b>' + escapeHtml(c.fieldName) + (c.itemId ? ' (item ' + escapeHtml(c.itemId) + ')' : '') + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') + '</div>' +
          '<div style="margin-top:4px;font-size:11px;color:var(--slate-soft);">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? c.correctedAt.slice(0, 10) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';

  body.innerHTML =
    '<span class="close-btn" onclick="closeModal()">&times;</span>' +
    '<h2>' + escapeHtml(po.poNumber) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(po.vendorName || 'Unknown vendor') + ' &middot; ' + escapeHtml(po.plantLabel) + ' (Imports) &middot; Country of Origin: ' + escapeHtml(po.countryOfOrigin || 'Not available') + '</div>' +
    '<div class="modal-tabs" id="importPoModalTabs">' +
      '<div class="modal-tab' + (importModalTab === 'overview' ? ' active' : '') + '" data-itab="overview">Overview</div>' +
      '<div class="modal-tab' + (importModalTab === 'items' ? ' active' : '') + '" data-itab="items">Items</div>' +
      '<div class="modal-tab' + (importModalTab === 'shipment' ? ' active' : '') + '" data-itab="shipment">Shipment &amp; License</div>' +
      '<div class="modal-tab' + (importModalTab === 'flags' ? ' active' : '') + '" data-itab="flags">Flags &amp; Corrections</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="importPoModalOverview"' + (importModalTab !== 'overview' ? ' hidden' : '') + '>' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalItems"' + (importModalTab !== 'items' ? ' hidden' : '') + '>' + itemsTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalShipment"' + (importModalTab !== 'shipment' ? ' hidden' : '') + '>' + shipmentTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalFlags"' + (importModalTab !== 'flags' ? ' hidden' : '') + '>' + flagsTabHtml + correctionsHtml + '</div>';

  body.querySelectorAll('[data-itab]').forEach(tab => tab.onclick = () => {
    importModalTab = tab.dataset.itab;
    body.querySelectorAll('[data-itab]').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    ['overview', 'items', 'shipment', 'flags'].forEach(k => {
      document.getElementById('importPoModal' + k.charAt(0).toUpperCase() + k.slice(1)).hidden = k !== importModalTab;
    });
  });

  const fieldsUrl = apiBase + '/purchase-orders/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(poNumber) + '/fields';
  wireEditableLines(body, fieldsUrl, () => onImportFieldSaved(plantKey, poNumber));
  wireDismissLinks(body, plantKey, () => onImportFieldSaved(plantKey, poNumber));
}

// A corrected field can move a PO in/out of a KPI bucket (e.g. fixing a bad
// Tax Type can clear an F2 flag) - invalidate both caches so the list,
// KPIs, charts and this modal all reflect the new value rather than a stale
// one until the next unrelated refresh.
async function onImportFieldSaved(plantKey, poNumber) {
  IMPORT_PO_CACHE = null;
  await ensureImportPOsLoaded();
  const fresh = await ensureImportPoDetailLoaded(plantKey, poNumber, true);
  renderImportPoModalBody(plantKey, poNumber, fresh);
  const content = document.getElementById('content');
  if (content && state.purchaseType === 'import') renderImportPoList(content);
}

// ------------------------------------------------------------------
// Raw Material Analysis - one row per Stock lot (see file header). No
// purchase-type split here (see the module docstring at the top of this
// file for why) - only the plant/All-Plants axis applies.
// ------------------------------------------------------------------
function currentMaterials() {
  const keys = selectedPlantKeys();
  if (keys.length === 1) return MATERIALS_BY_PLANT[keys[0]] || [];
  const merged = [];
  keys.forEach(key => {
    (MATERIALS_BY_PLANT[key] || []).forEach(m => {
      merged.push(Object.assign({}, m, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });
  return merged;
}

async function ensureMaterialsLoaded(plantKeys) {
  await Promise.all(plantKeys.map(async key => {
    if (!MATERIALS_BY_PLANT[key]) {
      const data = await apiForPlant(key, '/materials');
      MATERIALS_BY_PLANT[key] = data.materials;
    }
  }));
}

async function loadAndRenderMaterials() {
  const el = document.getElementById('viewContent');
  el.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading materials&hellip;</div></div>';
  try {
    // Also loads Purchase Orders for the same plant(s) - the KPI row and
    // drill-down chart below need PO line items to compute the "in transit"/
    // discrepancy/data-quality numbers (see materialLinksToItem()), not just
    // Stock data. Cheap: ensurePOsLoaded/ensureMaterialsLoaded both cache per
    // plant, so this is a no-op re-fetch if the Purchase Orders tab was
    // already visited for the same plant(s).
    await Promise.all([ensureMaterialsLoaded(selectedPlantKeys()), ensurePOsLoaded(selectedPlantKeys())]);
    el.innerHTML = '<div id="materialsContent"></div>';
    renderMaterialsView();
  } catch (e) {
    console.error('loadAndRenderMaterials failed:', e);
    el.innerHTML = '<div class="noaccess">Couldn\'t load material data right now. Please refresh, or contact IT if this keeps happening.</div>';
  }
}

// ── Material <-> PO line item linkage (best-effort, not ground truth) ──────
// There's no persisted DB join between a Stock lot and a PO line item (the
// only real chain is Stock<->MIR<->PO via *MirStockMatch/*POMirMatch, which
// by construction only ever covers already-delivered/matched goods - never
// an open, undelivered PO). So "which POs/vendors belong to this material"
// is computed the same best-effort way this app's own backend already
// computes PO<->MIR matches (apps/services/matching.py): normalized
// description equality (always linked), or token-overlap above
// MATERIAL_LINK_THRESHOLD gated on vendor when the material has one (Achhad
// Stock rows have no vendor column at all - description-only there, same
// precedent as matching_achhad.py's weaker material-only gate). NOT
// guaranteed-correct identity resolution - same "verify manually" caveat
// this app already gives PO<->MIR matches (see matchStatusHtml()).
const MATERIAL_LINK_THRESHOLD = 0.3;
function materialLinksToItem(material, po, item) {
  const normMat = normalizeMaterial(material.description);
  const normItem = normalizeMaterial(item.description);
  if (!normMat || !normItem) return false;
  if (normMat === normItem) return true;
  const a = new Set(tokenizeMaterial(material.description));
  const b = new Set(tokenizeMaterial(item.description));
  if (!a.size || !b.size) return false;
  let overlapCount = 0;
  a.forEach(t => { if (b.has(t)) overlapCount++; });
  const union = new Set([...a, ...b]).size;
  if (overlapCount / union < MATERIAL_LINK_THRESHOLD) return false;
  // `material` is either a single Stock lot (real `.vendor` string) or an
  // aggregateMaterialsByName() group (real `.vendors` array, one per
  // distinct contributing lot's vendor) - gate on whichever is present, any
  // one of a group's vendors clearing the gate is enough. Neither present
  // (Achhad has no vendor column at all) - description-only, same weaker-
  // gate precedent as matching_achhad.py.
  const vendorCandidates = (material.vendors && material.vendors.length) ? material.vendors : (material.vendor ? [material.vendor] : []);
  if (vendorCandidates.length) {
    const poVendor = normalizeVendor(po.vendorName);
    return vendorCandidates.some(v => vendorContains(normalizeVendor(v), poVendor));
  }
  return true;
}

// One row per unique material NAME within the current plant scope (sums
// qty/value across every vendor lot contributing to it, and across all 3
// plants when "All Plants" is selected) - per the project owner's
// 2026-09-04 request, matching CLAUDE.md's long-planned "Materials
// consolidated cross-plant view" roadmap item. Scoped to selectedPlantKeys()
// like the rest of this view (not always cross-plant) - a single plant tab
// sums only that plant's own vendor lots, "All Plants" sums across all
// three. Never silently loses the underlying per-vendor/per-plant detail
// CLAUDE.md warned about losing (`lots` keeps every contributing row,
// `vendors` a deduped list) - the row-click modal and materialLinksToItem()
// above both use these, not just the summed totals.
function aggregateMaterialsByName(lots) {
  const groups = new Map();
  lots.forEach(m => {
    const key = normalizeMaterial(m.description);
    if (!key) return;
    if (!groups.has(key)) {
      groups.set(key, { description: m.description, materialCode: m.materialCode, category: '', subCategory: '', qty: 0, value: 0, rates: new Set(), vendorSeen: new Set(), vendors: [], lots: [] });
    }
    const g = groups.get(key);
    if (!g.category && m.category) g.category = m.category;
    if (!g.subCategory && m.subCategory) g.subCategory = m.subCategory;
    g.qty += (m.qty || 0);
    g.value += (m.value || 0);
    if (m.rate != null) g.rates.add(m.rate);
    if (m.vendor) {
      const nv = normalizeVendor(m.vendor);
      if (nv && !g.vendorSeen.has(nv)) { g.vendorSeen.add(nv); g.vendors.push(m.vendor); }
    }
    g.lots.push(m);
  });
  // Rate is only shown when every contributing lot agrees on it - once
  // summed across vendors/plants a single "rate" is otherwise misleading
  // (which lot's rate would it even be?), same reasoning as the reference
  // design's own "Not available" cells for multi-lot materials.
  return Array.from(groups.values()).map(g => ({
    description: g.description, materialCode: g.materialCode,
    category: g.category, subCategory: g.subCategory,
    qty: g.qty, value: g.value,
    rate: g.rates.size === 1 ? Array.from(g.rates)[0] : null,
    // True if ANY contributing lot has a real Stock<->MIR match (see the
    // backend's `mirMatched` field, apps/api/routers/*_views.py) - not a
    // frontend-computed proxy.
    mirMatched: g.lots.some(l => l.mirMatched),
    vendors: g.vendors, lots: g.lots, anchorLot: g.lots[0],
  }));
}

// All (po, item) pairs across the given plant keys' cached PO data that link
// to `material` - each pair tagged with its plant key/label for display.
// Callers must ensure PURCHASE_ORDERS_BY_PLANT is already populated for
// every key in plantKeys (ensurePOsLoaded()) before calling this.
function linkedPoItemsForMaterial(material, plantKeys) {
  const out = [];
  plantKeys.forEach(key => {
    (PURCHASE_ORDERS_BY_PLANT[key] || []).forEach(po => {
      (po.items || []).forEach(item => {
        if (materialLinksToItem(material, po, item)) {
          out.push({ po, item, plantKey: key, plantLabel: PLANTS[key].label });
        }
      });
    });
  });
  return out;
}

// Every material with >=1 linked open (non-received) PO line item, for the
// KPI row below - computed once per render and reused across cards 3/4/5/6/7
// rather than re-scanning PURCHASE_ORDERS_BY_PLANT per card.
function computeMaterialPoLinkage(materials, plantKeys) {
  return materials.map(m => {
    const links = linkedPoItemsForMaterial(m, plantKeys);
    links.forEach(l => { l.po._status = l.po._status || computeStatus(l.po); if (!l.po._categories) computePoFlags(l.po); });
    const openLinks = links.filter(l => l.po._status !== 'received');
    // Same categories a PO row's own rowFlags() shows (see renderPoList()),
    // but scoped to just this material's own linked line item(s) - qty/rate
    // discrepancy is checked against `l.item`'s own diff%, not the parent
    // PO's blanket _qtyFlag/_rateFlag, so a flag on a different line item in
    // a multi-item PO never gets misattributed to this material. Remarks-
    // based info categories stay PO-level (remarks are a whole-PO field, no
    // finer-grained source exists) - same as computePoFlags() itself.
    const catMap = new Map();
    links.forEach(l => {
      if (l.item.qtyDiffPct != null && l.item.qtyDiffPct > FLAG_PCT) catMap.set('Quantity Discrepancy', { label: 'Quantity Discrepancy', severity: 'critical' });
      if ((l.item.rateDiffPct != null && l.item.rateDiffPct > FLAG_PCT) || (l.item.valueDiffPct != null && l.item.valueDiffPct > FLAG_PCT)) catMap.set('Rate / Value Discrepancy', { label: 'Rate / Value Discrepancy', severity: 'critical' });
      if (l.po.remarks) { const c = categorizeFlag(l.po.remarks); catMap.set(c.label, c); }
    });
    const categories = Array.from(catMap.values());
    return {
      material: m,
      links,
      openLinks,
      categories,
      qtyFlag: categories.some(c => c.label === 'Quantity Discrepancy'),
      rateFlag: categories.some(c => c.label === 'Rate / Value Discrepancy'),
      hasInfoFlag: categories.some(c => c.severity === 'info'),
    };
  });
}

// Materials' own status pill, same visual language as PO's Received/
// Partial Delivered/Pending/Overdue/Delivery Date Unknown pills (reuses the
// identical .status-pill.status-<key> CSS classes - no new styling needed).
// Not a delivery status like a PO has - a "business state" for the material
// itself, derived from its linked POs' own statuses + current stock:
//   overdue   - at least one linked, still-open PO is itself overdue
//   partial   - some qty already in stock AND a linked PO is still open
//               (mid-replenishment - part on hand, more incoming)
//   onorder   - a linked PO is open, nothing in stock yet
//   received  - every linked PO has been fully received, nothing open
//   instock   - no linked PO history at all, just sitting in stock (or,
//               rarer, no stock and no PO history either)
const MAT_STATUS_LABELS = { received: 'Received', partial: 'Partial', onorder: 'On Order', overdue: 'Overdue', instock: 'In Stock' };
const MAT_STATUS_PILL_CLASS = { received: 'status-received', partial: 'status-partial', onorder: 'status-pending', overdue: 'status-overdue', instock: 'status-unknown' };
function computeMaterialStatus(m, entry) {
  const links = entry ? entry.links : [];
  const openLinks = entry ? entry.openLinks : [];
  const stocked = (m.qty || 0) > 0;
  if (openLinks.some(l => l.po._status === 'overdue')) return 'overdue';
  if (openLinks.length && stocked) return 'partial';
  if (openLinks.length) return 'onorder';
  // "Received" once there's real evidence of an MIR receipt (m.mirMatched -
  // the actual Stock<->MIR match, see materialStepperHtml()'s comment) or,
  // failing that, a fuzzy-matched PO that's already fully received - never
  // inferred from stock qty alone, since qty>0 with neither signal just
  // means the material is sitting in stock with no traceable receipt found.
  if (m.mirMatched || links.length) return 'received';
  return 'instock';
}

// Table-only header filters (Material text search + Stock/Value/Rate
// ranges) - never touch the KPI row/chart, only which rows the table shows.
// Same role/shape as PO's applyColFilters()/state.colFilters. Status isn't
// filtered here - it's applied separately in renderMaterialsView() itself
// since it needs the `linkage` lookup, not just a plain field on `m`.
function applyMatColFilters(recs) {
  const f = state.matColFilters;
  return recs.filter(m => {
    if (f.material && !(m.description || '').toLowerCase().includes(f.material.toLowerCase())) return false;
    if (f.stockMin != null && (m.qty == null || m.qty < f.stockMin)) return false;
    if (f.stockMax != null && (m.qty == null || m.qty > f.stockMax)) return false;
    if (f.valueMin != null && (m.value == null || m.value < f.valueMin)) return false;
    if (f.valueMax != null && (m.value == null || m.value > f.valueMax)) return false;
    if (f.rateMin != null && (m.rate == null || m.rate < f.rateMin)) return false;
    if (f.rateMax != null && (m.rate == null || m.rate > f.rateMax)) return false;
    return true;
  });
}

function renderMaterialsView() {
  const el = document.getElementById('materialsContent');
  destroyPageCharts();
  // One row per unique material NAME within the current plant scope, not
  // per Stock lot (see aggregateMaterialsByName()'s own header comment for
  // why/scope) - "All Plants" sums across all 3 plants, a single plant tab
  // sums only that plant's own vendor lots.
  const allLots = currentMaterials();
  const all = aggregateMaterialsByName(allLots);

  const catCounts = {};
  all.forEach(m => { catCounts[m.category || 'Uncategorized'] = (catCounts[m.category || 'Uncategorized'] || 0) + 1; });
  const catOptions = Object.entries(catCounts).sort((a, b) => b[1] - a[1]);

  // Sub-category options are scoped to the currently selected category (if
  // any) - a cascading dropdown, same UX convention as most category/
  // sub-category filter pairs, so it never offers a sub-category that can't
  // possibly match anything under the chosen category.
  const inSelectedCategory = state.matCategoryFilter ? all.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter) : all;
  const subCatCounts = {};
  inSelectedCategory.forEach(m => { subCatCounts[m.subCategory || 'Uncategorized'] = (subCatCounts[m.subCategory || 'Uncategorized'] || 0) + 1; });
  const subCatOptions = Object.entries(subCatCounts).sort((a, b) => b[1] - a[1]);

  // "Global" filters - narrow `filtered` itself, so the KPI row and
  // drill-down chart reflect only the selected category/sub-category, not
  // just the table (see state.matCategoryFilter's own comment).
  let filtered = all;
  if (state.matCategoryFilter) filtered = filtered.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter);
  if (state.matSubCategoryFilter) filtered = filtered.filter(m => (m.subCategory || 'Uncategorized') === state.matSubCategoryFilter);

  if (!all.length) {
    const msg = isAllPlants()
      ? 'No Raw Material Stock synced yet for any plant.'
      : 'No RM Stock synced yet - run <code>' + PLANTS[state.plant].syncCmdStock + '</code> to load it.';
    el.innerHTML = '<div class="empty-state">' + msg + '</div>';
    return;
  }

  const plantKeys = selectedPlantKeys();
  const linkage = computeMaterialPoLinkage(filtered, plantKeys);
  // O(1) lookup from a (category/sub-category-filtered) material back to
  // its linkage entry (flags/categories/open-PO links) while rendering the
  // table below - built from the exact same `filtered` array `linkage` was
  // computed from, so every material in `filtered` has an entry.
  const linkageByKey = new Map(linkage.map(l => [normalizeMaterial(l.material.description), l]));

  const totalMaterials = filtered.length;
  const totalValue = filtered.reduce((s, m) => s + (m.value || 0), 0);
  const inTransitValue = linkage.reduce((s, l) => s + l.openLinks.reduce((s2, x) => s2 + (x.item.netPrice != null && x.item.qty != null ? x.item.netPrice * x.item.qty : 0), 0), 0);
  const qtyOrderedOpen = linkage.reduce((s, l) => s + l.openLinks.reduce((s2, x) => s2 + (x.item.qty || 0), 0), 0);
  const qtyDiscMats = linkage.filter(l => l.qtyFlag);
  const rateDiscMats = linkage.filter(l => l.rateFlag);
  const flaggedMats = linkage.filter(l => l.hasInfoFlag);

  // Same visual language as Purchase Orders' KPI row (renderPoList()) -
  // colored left border + flag icon for discrepancy/quality cards, plain
  // counts for the rest. Order: plain counts -> in-transit pair -> the two
  // "critical" (money/quantity) discrepancy cards -> Data Quality Flags last.
  const cardDef = [
    { key: 'total', cls: '', label: 'Materials Tracked', val: totalMaterials },
    { key: 'value', cls: '', label: 'Total Inventory Value (Warehouse)', val: formatInr(totalValue) },
    { key: 'transit', cls: 'partial', label: 'Inventory Value in Transit (Open POs)', val: formatInr(inTransitValue) },
    { key: 'qtyordered', cls: 'partial', label: 'Quantity Ordered (Open POs)', val: qtyOrderedOpen.toLocaleString('en-IN') },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Discrepancies', val: qtyDiscMats.length, flag: KPI_FLAG_COLORS.critical },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Discrepancies', val: rateDiscMats.length, flag: KPI_FLAG_COLORS.critical },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: flaggedMats.length, flag: KPI_FLAG_COLORS.quality },
  ];
  const kpiHtml = cardDef.map(c => '<div class="kpi-card ' + c.cls + ' ' + (state.matStatusFilter === c.key ? 'active' : '') + '" data-matkpi="' + c.key + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="val">' + c.val + '</div><div class="label">' + escapeHtml(c.label) + '</div></div>').join('');

  // matStatusFilter is table-only-in-effect here (like PO's statusFilter on
  // its own table) even though it's also a KPI-card click target - narrows
  // `tableRecs`, not `filtered`, so the KPI counts above always show the
  // full category/sub-category-filtered picture regardless of which status
  // chip is selected, exactly like PO's Quantity/Rate Discrepancy cards.
  let tableRecs = filtered;
  if (state.matStatusFilter === 'qtydisc') tableRecs = qtyDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'ratedisc') tableRecs = rateDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'flags') tableRecs = flaggedMats.map(l => l.material);
  tableRecs = applyMatColFilters(tableRecs);
  // "Materials by Stock Quantity" - sorted by stock qty descending, not
  // value (per the reference design).
  const sorted = tableRecs.slice().sort((a, b) => (b.qty || 0) - (a.qty || 0));

  const showingAll = state.showAllMaterials;
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const matTablePage = Math.min(Math.max(1, state.matTablePage), totalPages);
  const listRecs = showingAll ? sorted.slice((matTablePage - 1) * PAGE_SIZE, matTablePage * PAGE_SIZE) : sorted.slice(0, 5);

  const stockAllPlantsSuffix = isAllPlants() ? ' (All Plants)' : '';

  // Colored flag-icon cluster per material row, identical pattern to PO's
  // own rowFlags() in renderPoList() (categoryColor()/CATEGORY_COLORS +
  // .row-flag-wrap's CSS hover tooltip) - built from computeMaterialPoLinkage()'s
  // `categories` list. Rendered next to the status pill below (see
  // computeMaterialStatus()), same "pill + flags" combo as PO's own Status
  // column.
  const rowFlags = entry => {
    const cats = entry ? entry.categories : [];
    if (!cats.length) return '';
    return cats.map(c =>
      '<span class="row-flag-wrap" data-tooltip="' + escapeHtml(c.label) + '">' + flagIconHtml(categoryColor(c.label), 'row-flag-icon') + '</span>'
    ).join('');
  };

  // Header filter row inside the table, same pattern as PO's filterCells -
  // text search for Material, min/max ranges for Stock/Value/Rate, a
  // <select> for Status (shared with matStatusFilter, same single-source-
  // of-truth reasoning as PO's own Status header select). Category/
  // Sub Category have their own "Filter by" bar above the chart instead
  // (see below) - not repeated here to avoid two controls for one filter.
  // One entry per <th> in the table header (Material, Category, Sub
  // Category, Stock, Inventory Value, Latest Rate, Status, Details) - even
  // the columns with no header-row control of their own (Category/Sub
  // Category use the "Filter by" bar above instead, Details has no data)
  // need an empty placeholder entry, or every later cell silently shifts
  // one column left under the wrong header.
  const matFilterCells = [
    '<input type="text" class="col-filter-input" data-mcf="material" placeholder="Search..." value="' + escapeHtml(state.matColFilters.material) + '">',
    '',
    '',
    '<div class="col-filter-range"><input type="number" class="col-filter-num" data-mcf="stockMin" placeholder="Min" value="' + (state.matColFilters.stockMin != null ? state.matColFilters.stockMin : '') + '"><input type="number" class="col-filter-num" data-mcf="stockMax" placeholder="Max" value="' + (state.matColFilters.stockMax != null ? state.matColFilters.stockMax : '') + '"></div>',
    '<div class="col-filter-range"><input type="number" class="col-filter-num" data-mcf="valueMin" placeholder="Min" value="' + (state.matColFilters.valueMin != null ? state.matColFilters.valueMin : '') + '"><input type="number" class="col-filter-num" data-mcf="valueMax" placeholder="Max" value="' + (state.matColFilters.valueMax != null ? state.matColFilters.valueMax : '') + '"></div>',
    '<div class="col-filter-range"><input type="number" class="col-filter-num" data-mcf="rateMin" placeholder="Min" value="' + (state.matColFilters.rateMin != null ? state.matColFilters.rateMin : '') + '"><input type="number" class="col-filter-num" data-mcf="rateMax" placeholder="Max" value="' + (state.matColFilters.rateMax != null ? state.matColFilters.rateMax : '') + '"></div>',
    '<select class="col-filter-input" data-mcf="status"><option value="">All</option>' +
      '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Discrepancy</option>' +
      '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Discrepancy</option>' +
      '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag</option>' +
    '</select>',
    '',
    '',
  ];
  const colFilterRow = '<tr class="col-filter-row">' + matFilterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';

  const pageButtons = totalPages <= 10
    ? Array.from({ length: totalPages }, (_, i) => i + 1)
        .map(p => '<button class="page-btn page-num' + (p === matTablePage ? ' active' : '') + '" data-matpage="' + p + '">' + p + '</button>')
        .join('')
    : '<span class="page-info">Page ' + matTablePage + ' of ' + totalPages + '</span>';
  const paginationHtml = showingAll && totalPages > 1
    ? '<div class="pagination-row">' +
        '<button id="matPrevPageBtn" class="page-btn"' + (matTablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
        pageButtons +
        '<button id="matNextPageBtn" class="page-btn"' + (matTablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
      '</div>'
    : '';

  el.innerHTML =
    '<div class="section-title">Raw Material and Inventory Analysis: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">One row per unique material' + (isAllPlants() ? ', summed across every vendor lot and all 3 plants' : ', summed across every vendor lot at this plant') + '. Click a row for its full cross-plant analysis.</div>' +
    '<div class="validation-note">⚠️ <div>"Inventory Value in Transit", "Quantity Ordered", and the discrepancy/flag columns below are computed by automatically matching each material to purchase order line items by description (and vendor, where known) - the same best-effort approach this app already uses for PO&harr;MIR matching. <strong>Not guaranteed-correct identity resolution - verify manually before relying on it.</strong></div></div>' +
    '<div class="mat-cards">' + kpiHtml + '</div>' +
    // "Filter by Category" / "Filter by Sub Category" / "Filter by Flags"
    // bar - moved above the chart (project owner, 2026-09-04) so the chart
    // itself already reflects the selection. All three are "global"
    // filters, same as PO's own category filter bar: Category/Sub Category
    // narrow `filtered` (KPI row + chart + table all reflect them); Flags
    // is just this same bar's control over matStatusFilter (also settable
    // from a KPI-card click or the table's own Status header select - one
    // shared field, three ways to set it, same pattern as PO's Status).
    '<div class="filter-row">' +
      '<label>Filter by Category</label>' +
      '<select id="matCatSelect" style="min-width:220px;">' +
        '<option value="">All categories (' + all.length + ')</option>' +
        catOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('') +
      '</select>' +
      '<label>Filter by Sub Category</label>' +
      '<select id="matSubCatSelect" style="min-width:220px;">' +
        '<option value="">All sub-categories (' + inSelectedCategory.length + ')</option>' +
        subCatOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matSubCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('') +
      '</select>' +
      '<label>Filter by Flags</label>' +
      '<select id="matFlagsSelect" style="min-width:200px;">' +
        '<option value="">All flags</option>' +
        '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Discrepancy (' + qtyDiscMats.length + ')</option>' +
        '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Discrepancy (' + rateDiscMats.length + ')</option>' +
        '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + flaggedMats.length + ')</option>' +
      '</select>' +
      ((state.matCategoryFilter || state.matSubCategoryFilter || state.matStatusFilter) ? '<button id="matClearCategoryFilter">Clear</button>' : '') +
    '</div>' +
    renderMaterialsChart(filtered) +
    '<div class="list-toggle-row"><div class="section-title" style="margin:0;">Materials by Stock Quantity - showing ' + listRecs.length + ' of ' + sorted.length + '</div>' +
      (sorted.length > 5 ? '<button class="view-all-btn" id="toggleMatBtn">' + (showingAll ? 'Show top 5' : 'View all ' + sorted.length + ' materials') + '</button>' : '') +
    '</div>' +
    '<div class="table-wrap"><table><thead><tr><th>Material</th><th>Category</th><th>Sub Category</th><th>Stock' + stockAllPlantsSuffix + '</th><th>Inventory Value' + stockAllPlantsSuffix + '</th><th>Latest Rate</th><th>Status</th><th>Progress</th><th>Details</th></tr>' +
      // Header filter row shown in both the compact top-5 preview and the
      // full "View all" table - same as PO's own filter row, which appears
      // above its compact top5-list too, not just the expanded table (see
      // renderPoList()'s own list-header-row/col-filter-row-grid).
      colFilterRow +
    '</thead><tbody>' +
    listRecs.map(m => {
      const anchor = m.anchorLot;
      const key = escapeHtml(plantKeyFor(anchor) + '::' + anchor.lotId);
      const entry = linkageByKey.get(normalizeMaterial(m.description));
      return '<tr><td><span class="row-link" data-lot="' + key + '">' + escapeHtml(m.description || m.materialCode) + '</span></td>' +
      '<td>' + escapeHtml(m.category || '-') + '</td>' +
      '<td>' + escapeHtml(m.subCategory || '-') + '</td>' +
      '<td>' + (m.qty ? m.qty.toLocaleString('en-IN') : '0') + '</td>' +
      '<td>' + formatInr(m.value || 0) + '</td>' +
      '<td>' + (m.rate != null ? formatInr(m.rate) : 'Not available') + '</td>' +
      (() => { const st = computeMaterialStatus(m, entry); return '<td><span class="status-pill ' + MAT_STATUS_PILL_CLASS[st] + '">' + escapeHtml(MAT_STATUS_LABELS[st]) + '</span>' + rowFlags(entry) + '</td>'; })() +
      '<td>' + materialStepperHtml(m) + '</td>' +
      '<td><span class="row-link" data-lot="' + key + '">View analysis</span></td></tr>';
    }).join('') +
    '</tbody></table></div>' + paginationHtml;

  document.querySelectorAll('[data-matkpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.matkpi;
    state.matStatusFilter = (state.matStatusFilter === key || key === 'total' || key === 'value' || key === 'transit' || key === 'qtyordered') ? null : key;
    state.matTablePage = 1;
    renderMaterialsView();
  });
  const matCatSelect = document.getElementById('matCatSelect');
  if (matCatSelect) matCatSelect.onchange = () => { state.matCategoryFilter = matCatSelect.value || null; state.matSubCategoryFilter = null; state.matTablePage = 1; renderMaterialsView(); };
  const matSubCatSelect = document.getElementById('matSubCatSelect');
  if (matSubCatSelect) matSubCatSelect.onchange = () => { state.matSubCategoryFilter = matSubCatSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matFlagsSelect = document.getElementById('matFlagsSelect');
  if (matFlagsSelect) matFlagsSelect.onchange = () => { state.matStatusFilter = matFlagsSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matClearCategoryBtn = document.getElementById('matClearCategoryFilter');
  if (matClearCategoryBtn) matClearCategoryBtn.onclick = () => { state.matCategoryFilter = null; state.matSubCategoryFilter = null; state.matStatusFilter = null; state.matTablePage = 1; renderMaterialsView(); };
  const toggleBtn = document.getElementById('toggleMatBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllMaterials = !state.showAllMaterials; state.matTablePage = 1; renderMaterialsView(); };
  document.querySelectorAll('[data-lot]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.lot));

  const matPrevPageBtn = document.getElementById('matPrevPageBtn');
  if (matPrevPageBtn) matPrevPageBtn.onclick = () => { state.matTablePage = Math.max(1, state.matTablePage - 1); renderMaterialsView(); };
  const matNextPageBtn = document.getElementById('matNextPageBtn');
  if (matNextPageBtn) matNextPageBtn.onclick = () => { state.matTablePage = state.matTablePage + 1; renderMaterialsView(); };
  document.querySelectorAll('[data-matpage]').forEach(btn => btn.onclick = () => { state.matTablePage = Number(btn.dataset.matpage); renderMaterialsView(); });

  // Header filter row (only present when showingAll). Text input re-renders
  // live on every keystroke via preserveFocus() (same as PO's own [data-cf]
  // handling - see that function's comment); number/select inputs commit on
  // 'change' instead, since re-rendering mid-typing a number is jarring.
  document.querySelectorAll('[data-mcf]').forEach(inp => {
    const key = inp.dataset.mcf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'number') ? 'change' : 'input';
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'status') { state.matStatusFilter = raw || null; }
      else if (key === 'material') { state.matColFilters.material = raw; }
      else { state.matColFilters[key] = raw !== '' ? Number(raw) : null; }
      state.matTablePage = 1;
      preserveFocus(el, () => renderMaterialsView());
    });
  });

  wireMaterialsChart(filtered);
}

// ── Drill-down horizontal bar chart: Inventory Value by Category ->
// Subcategory -> Material. Bars/breadcrumb markup built here; the actual
// Chart.js instance + click handling is wired in wireMaterialsChart() right
// after this HTML lands in the DOM (mirrors renderPoList()'s own
// render-HTML-then-wire-charts split).
const MAT_CHART_TOP_N = 12;
function materialsChartLevelData(materials) {
  if (state.matChartLevel === 'category') {
    const totals = {};
    materials.forEach(m => { const k = m.category || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  const inCategory = materials.filter(m => (m.category || 'Uncategorized') === state.matChartCategory);
  if (state.matChartLevel === 'subcategory') {
    const totals = {};
    inCategory.forEach(m => { const k = m.subCategory || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  // 'material' level
  const inSub = inCategory.filter(m => (m.subCategory || 'Uncategorized') === state.matChartSubcategory);
  return inSub.map(m => ({ label: m.description || m.materialCode, value: m.value || 0, material: m }));
}
function renderMaterialsChart(materials) {
  let bars = materialsChartLevelData(materials).sort((a, b) => (b.value || 0) - (a.value || 0));
  if (bars.length > MAT_CHART_TOP_N) {
    const kept = bars.slice(0, MAT_CHART_TOP_N);
    const rest = bars.slice(MAT_CHART_TOP_N);
    const otherTotal = rest.reduce((s, b) => s + (b.value || 0), 0);
    kept.push({ label: 'Other (' + rest.length + ' more)', value: otherTotal, isOther: true });
    bars = kept;
  }
  const crumbs = [{ label: 'All Categories', level: 'category' }];
  if (state.matChartCategory) crumbs.push({ label: state.matChartCategory, level: 'subcategory' });
  if (state.matChartSubcategory) crumbs.push({ label: state.matChartSubcategory, level: 'material' });
  const crumbHtml = crumbs.map((c, i) => {
    const isLast = i === crumbs.length - 1;
    return '<span class="crumb ' + (isLast ? 'active' : '') + '"' + (isLast ? '' : ' data-crumb-level="' + c.level + '"') + '>' + escapeHtml(c.label) + '</span>';
  }).join('<span class="crumb-sep">&rsaquo;</span>');

  if (!bars.length) {
    return '<div class="chart-panel" style="margin-bottom:20px;"><h4>Inventory Value by Category</h4>' +
      '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
      '<div class="no-data-note">No materials in this ' + (state.matChartLevel === 'category' ? 'view' : state.matChartLevel) + ' to chart.</div></div>';
  }
  const chartHeight = Math.max(180, bars.length * 34);
  return '<div class="chart-panel" style="margin-bottom:20px;"><h4>Inventory Value by Category' + (state.matChartLevel !== 'category' ? ' &rsaquo; Subcategory' : '') + (state.matChartLevel === 'material' ? ' &rsaquo; Material' : '') + '</h4>' +
    '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
    '<div class="chart-box" style="height:' + chartHeight + 'px;"><canvas id="matDrillChart"></canvas></div>' +
    (state.matChartLevel !== 'material' ? '<div class="no-data-note" style="margin-top:8px;">Click a bar to drill down.</div>' : '') +
  '</div>';
}
function wireMaterialsChart(materials) {
  document.querySelectorAll('[data-crumb-level]').forEach(c => c.onclick = () => {
    const level = c.dataset.crumbLevel;
    state.matChartLevel = level;
    if (level === 'category') { state.matChartCategory = null; state.matChartSubcategory = null; }
    if (level === 'subcategory') state.matChartSubcategory = null;
    renderMaterialsView();
  });
  const canvas = document.getElementById('matDrillChart');
  if (!canvas) return;
  let bars = materialsChartLevelData(materials).sort((a, b) => (b.value || 0) - (a.value || 0));
  if (bars.length > MAT_CHART_TOP_N) {
    const kept = bars.slice(0, MAT_CHART_TOP_N);
    const rest = bars.slice(MAT_CHART_TOP_N);
    kept.push({ label: 'Other (' + rest.length + ' more)', value: rest.reduce((s, b) => s + (b.value || 0), 0), isOther: true });
    bars = kept;
  }
  try {
    const chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bars.map(b => b.label),
        datasets: [{
          data: bars.map(b => b.value),
          backgroundColor: bars.map(b => b.isOther ? '#cbd5e1' : '#2563eb'),
          borderRadius: 5,
          borderSkipped: false,
          maxBarThickness: 26,
        }],
      },
      options: {
        indexAxis: 'y',
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8, displayColors: false, callbacks: { label: c => formatInr(c.parsed.x) } },
        },
        scales: {
          x: { grid: { color: '#eef1f5' }, border: { display: false }, ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) } },
          y: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
        },
        onClick: (evt, elements) => {
          if (!elements.length) return;
          const bar = bars[elements[0].index];
          if (bar.isOther) return;
          if (state.matChartLevel === 'category') { state.matChartCategory = bar.label; state.matChartLevel = 'subcategory'; renderMaterialsView(); }
          else if (state.matChartLevel === 'subcategory') { state.matChartSubcategory = bar.label; state.matChartLevel = 'material'; renderMaterialsView(); }
          // `bar.material` is an aggregateMaterialsByName() group now (no
          // top-level lotId of its own) - open on its anchor lot, same as
          // the table's own row click (openMaterialModal re-derives the
          // full cross-plant picture from the description regardless of
          // which specific contributing lot it's handed).
          else if (bar.material) { const anchor = bar.material.anchorLot; openMaterialModal(plantKeyFor(anchor) + '::' + anchor.lotId); }
        },
      },
    });
    pageCharts.push(chart);
  } catch (e) {
    console.error('Materials drill-down chart failed to render:', e);
    if (canvas.parentNode) canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
  }
}

// Trailing calendar-day moving average of `points` (each {date:'YYYY-MM-DD',
// price}) as of points[i]'s own date - averages every point whose date falls
// in [date - days, date], not a fixed-size window over the sorted array,
// since POs happen irregularly rather than daily (mirrors the reference
// design's own caption for this - see renderMaterialPriceTrendTab()).
function trailingPriceAvg(points, i, days) {
  const d = new Date(points[i].date + 'T00:00:00');
  const start = new Date(d);
  start.setDate(start.getDate() - days);
  const inWindow = points.filter(p => {
    const pd = new Date(p.date + 'T00:00:00');
    return pd >= start && pd <= d;
  });
  return inWindow.length ? inWindow.reduce((s, p) => s + p.price, 0) / inWindow.length : null;
}

// compositeKey is "<plantKey>::<lotId>" (see plantKeyFor()) - resolves the
// anchor lot against that specific plant (Stock-lot ids are per-plant
// autoincrement PKs, NOT unique across plants), then rolls up to every
// plant regardless of which plant tab was open when this was clicked - see
// the file header/CLAUDE.md for why this modal alone, unlike the rest of
// this view, always aggregates across all three plants.
async function openMaterialModal(compositeKey) {
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const lotId = Number(compositeKey.slice(sep + 2));
  const anchor = (MATERIALS_BY_PLANT[plantKey] || []).find(m => m.lotId === lotId);
  if (!anchor) return;

  // Reopening on a different lot without closing first (e.g. clicking
  // another drill-down chart bar) would otherwise leak the previous open's
  // Chart.js instances - clear them before this open creates its own.
  destroyModalCharts();

  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };
  body.innerHTML = '<span class="close-btn" onclick="closeModal()">&times;</span><div class="load-banner"><div class="spinner"></div><div>Loading material analysis&hellip;</div></div>';

  try {
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS), ensurePOsLoaded(PLANT_KEYS)]);
  } catch (e) {
    console.error('openMaterialModal cross-plant load failed:', e);
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    body.innerHTML = '<span class="close-btn" onclick="closeModal()">&times;</span><div class="noaccess">Couldn\'t load the full material analysis right now. Please refresh, or contact IT if this keeps happening.</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one

  // Sibling lots: the same material (exact normalized-description match,
  // deliberately stricter than the PO linkage below - this asserts "the
  // same material", not just "related") across all 3 plants.
  const normAnchor = normalizeMaterial(anchor.description);
  const siblingLots = [];
  PLANT_KEYS.forEach(key => {
    (MATERIALS_BY_PLANT[key] || []).forEach(lot => {
      if (normalizeMaterial(lot.description) === normAnchor) siblingLots.push(Object.assign({}, lot, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });

  const linked = linkedPoItemsForMaterial(anchor, PLANT_KEYS);
  linked.forEach(l => {
    l.po._status = l.po._status || computeStatus(l.po);
    l.po._deliveryDate = l.po._deliveryDate || computePoDeliveryDate(l.po);
    if (!l.po._categories) computePoFlags(l.po);
  });
  const openLinked = linked.filter(l => l.po._status !== 'received')
    .sort((a, b) => (a.po._deliveryDate || '').localeCompare(b.po._deliveryDate || ''));

  const vendorSeen = new Set();
  const vendors = [];
  siblingLots.forEach(lot => { if (lot.vendor) { const n = normalizeVendor(lot.vendor); if (n && !vendorSeen.has(n)) { vendorSeen.add(n); vendors.push(lot.vendor); } } });
  linked.forEach(l => { if (l.po.vendorName) { const n = normalizeVendor(l.po.vendorName); if (n && !vendorSeen.has(n)) { vendorSeen.add(n); vendors.push(l.po.vendorName); } } });

  const knownDescs = new Set([normAnchor, ...siblingLots.map(l => normalizeMaterial(l.description))]);
  const aliasSeen = new Set();
  const aliases = [];
  linked.forEach(l => {
    const n = normalizeMaterial(l.item.description);
    if (n && !knownDescs.has(n) && !aliasSeen.has(n)) { aliasSeen.add(n); aliases.push(l.item.description); }
  });

  const category = siblingLots.map(l => l.category).find(c => c) || '';
  const subCategory = siblingLots.map(l => l.subCategory).find(c => c) || '';
  const qtyAllPlants = siblingLots.reduce((s, l) => s + (l.qty || 0), 0);
  const valueAllPlants = siblingLots.reduce((s, l) => s + (l.value || 0), 0);
  const openValue = openLinked.reduce((s, l) => s + (l.item.netPrice != null && l.item.qty != null ? l.item.netPrice * l.item.qty : 0), 0);

  const atAGlanceHtml =
    '<div class="line">In stock, all plants: ' + qtyAllPlants + '</div>' +
    '<div class="line">Total inventory value: ' + formatInr(valueAllPlants) + '</div>' +
    '<div class="line">Ordered, not yet delivered: ' + formatInr(openValue) + ' across ' + openLinked.length + ' open PO(s)</div>';

  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Classification</h4>' +
        '<div class="line">Category: ' + escapeHtml(category || 'Not available') + '</div>' +
        '<div class="line">Sub Category: ' + escapeHtml(subCategory || 'Not available') + '</div>' +
        '<div class="line">Also known as: ' + (aliases.length ? escapeHtml(aliases.slice(0, 5).join('; ')) + (aliases.length > 5 ? ' (+' + (aliases.length - 5) + ' more)' : '') : 'Not available') + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>Vendors (from POs and Stock Supplier History)</h4>' +
        (vendors.length ? vendors.map(v => '<span class="vendor-pill">' + escapeHtml(v) + '</span>').join('') : '<div class="line">Not available</div>') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width" style="margin-top:14px;"><h4>At a Glance</h4>' + atAGlanceHtml + '</div>';

  const stockByPlantHtml = siblingLots.length
    ? '<div class="table-wrap"><table><thead><tr><th>Plant</th><th>Qty</th><th>Rate</th><th>Value</th><th>MIR↔Stock Match</th></tr></thead><tbody>' +
        siblingLots.map(l => '<tr><td>' + escapeHtml(l._plantLabel) + '</td><td>' + (l.qty != null ? l.qty : '-') +
          '</td><td>' + (l.rate != null ? formatInr(l.rate) : '-') + '</td><td>' + (l.value != null ? formatInr(l.value) : '-') +
          '</td><td>' + mirStockMatchHtml(l, l._plantKey) + '</td></tr>').join('') +
        '<tr style="font-weight:700;"><td>Total</td><td>' + qtyAllPlants + '</td><td>-</td><td>' + formatInr(valueAllPlants) + '</td><td></td></tr>' +
      '</tbody></table></div>'
    : '<div class="no-data-note">No stock found for this material at any plant.</div>';

  const purchaseActivityHtml =
    '<div class="field-block"><h4>Quantity</h4>' + atAGlanceHtml + '</div>' +
    '<div class="section-title">Open Purchase Orders</div>' +
    (openLinked.length
      ? '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Plant</th><th>Qty</th><th>Status</th></tr></thead><tbody>' +
          openLinked.map(l => '<tr><td><b>' + escapeHtml(l.po.poNumber) + '</b></td><td>' + escapeHtml(l.plantLabel) +
            '</td><td>' + (l.item.qty != null ? l.item.qty : '-') + ' ' + escapeHtml(l.item.uom || '') +
            '</td><td><span class="status-pill status-' + l.po._status + '">' + escapeHtml(STATUS_LABELS[l.po._status]) + '</span></td></tr>').join('') +
        '</tbody></table></div>'
      : '<div class="no-data-note">No open purchase orders currently linked to this material by automated matching.</div>');

  const priceTrendHtml =
    '<div class="chart-box" id="matPriceTrendBox"><div class="no-data-note">Loading&hellip;</div></div>' +
    '<div class="no-data-note" style="margin-top:8px;">Moving averages are a trailing calendar-day average of actual PO prices (21/50/100 days back from each PO date) - since POs happen irregularly rather than daily, this smooths the line without inventing prices on days with no PO.</div>';

  body.innerHTML =
    '<span class="close-btn" onclick="closeModal()">&times;</span>' +
    '<h2>' + escapeHtml(anchor.description || anchor.materialCode) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(category || 'Uncategorized') + ' &middot; rolled up across ' + siblingLots.length + ' plant location' + (siblingLots.length === 1 ? '' : 's') + '</div>' +
    '<div class="validation-note">⚠️ <div>Purchase Activity, Price Trend, Vendors, and "Also known as" are computed by automatically matching this material to purchase order line items by description (and vendor, where known) - not guaranteed-correct identity resolution. <strong>Verify manually before treating a match as ground truth.</strong></div></div>' +
    '<div class="modal-tabs" id="matModalTabs">' +
      '<div class="modal-tab active" data-tab="overview">Overview</div>' +
      '<div class="modal-tab" data-tab="stockplant">Stock by Plant</div>' +
      '<div class="modal-tab" data-tab="poactivity">Purchase Activity' + (openLinked.length ? ' (' + openLinked.length + ' open)' : '') + '</div>' +
      '<div class="modal-tab" data-tab="pricetrend">Price Trend</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="matModalOverview">' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalStockPlant" hidden>' + stockByPlantHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalPoActivity" hidden>' + purchaseActivityHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalPriceTrend" hidden>' + priceTrendHtml + '</div>';

  // wireDismissLinks reads each link's own data-plant (set per-row above,
  // since siblingLots spans multiple plants) - the plantKey argument here
  // is only a fallback, never actually used by this table's own links.
  wireDismissLinks(body, plantKey, async () => {
    PLANT_KEYS.forEach(key => { MATERIALS_BY_PLANT[key] = null; });
    await openMaterialModal(compositeKey);
  });

  const panelIds = { overview: 'matModalOverview', stockplant: 'matModalStockPlant', poactivity: 'matModalPoActivity', pricetrend: 'matModalPriceTrend' };
  body.querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    body.querySelectorAll('[data-tab]').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    Object.entries(panelIds).forEach(([key, id]) => { document.getElementById(id).hidden = tab.dataset.tab !== key; });
  });

  // Price Trend: net price of every linked PO line item (all statuses, not
  // just open ones) plotted over the PO's own createdDate, with 21/50/100-
  // day trailing moving averages (see trailingPriceAvg()).
  const points = linked
    .filter(l => l.item.netPrice != null && l.po.createdDate)
    .map(l => ({ date: l.po.createdDate, price: l.item.netPrice, poNumber: l.po.poNumber }))
    .sort((a, b) => a.date.localeCompare(b.date));
  const trendBox = document.getElementById('matPriceTrendBox');
  if (!points.length) {
    trendBox.innerHTML = '<div class="no-data-note">No purchase order price history found for this material by automated matching.</div>';
  } else {
    trendBox.innerHTML = '<canvas id="matPriceTrendChart"></canvas>';
    try {
      modalCharts.push(new Chart(document.getElementById('matPriceTrendChart'), {
        type: 'line',
        data: {
          labels: points.map(p => formatDateIN(p.date)),
          datasets: [
            { label: 'Net price per PO', data: points.map(p => p.price), borderColor: '#2563eb', backgroundColor: '#2563eb', tension: 0.15, pointRadius: 3 },
            { label: '21-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 21)), borderColor: '#16a34a', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
            { label: '50-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 50)), borderColor: '#d97706', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
            { label: '100-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 100)), borderColor: '#7c3aed', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
          ],
        },
        options: {
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 9, boxHeight: 9, font: { size: 10.5 } } },
            tooltip: { backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8, callbacks: { label: c => c.dataset.label + ': ' + formatInr(c.parsed.y) } } },
          scales: { y: { ticks: { callback: v => formatInr(v) } } },
        },
      }));
    } catch (e) {
      console.error('Price trend chart failed to render:', e);
      trendBox.innerHTML = '<div class="no-data-note">Chart unavailable right now.</div>';
    }
  }

  // Anchor lot's own qty-over-time stock trend (per-lot, not cross-plant -
  // the stock-trend endpoint only knows about one specific lot id), appended
  // under the Stock by Plant table.
  try {
    const data = await apiForPlant(plantKey, '/materials/' + lotId + '/stock-trend');
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    const stockPlantPanel = document.getElementById('matModalStockPlant');
    if (!stockPlantPanel) return; // modal closed/replaced while this was in flight
    const trendSection = document.createElement('div');
    if (!data.snapshots.length) {
      trendSection.innerHTML = '<div class="section-title">Stock Trend (' + escapeHtml(PLANTS[plantKey].label) + ')</div><div class="no-data-note">Not enough daily snapshots yet to plot a trend.</div>';
    } else {
      trendSection.innerHTML = '<div class="section-title">Stock Trend (' + escapeHtml(PLANTS[plantKey].label) + ')</div><div class="chart-box"><canvas id="matTrendChart"></canvas></div>';
    }
    stockPlantPanel.appendChild(trendSection);
    if (data.snapshots.length) {
      modalCharts.push(new Chart(document.getElementById('matTrendChart'), {
        type: 'line',
        data: { labels: data.snapshots.map(s => formatDateIN(s.date)), datasets: [{ label: 'Qty', data: data.snapshots.map(s => s.qty), borderColor: '#2563eb', tension: 0.2 }] },
        options: { plugins: { legend: { display: false } } },
      }));
    }
  } catch (e) {
    console.error('openMaterialModal stock-trend fetch failed:', e);
    const stockPlantPanel = document.getElementById('matModalStockPlant');
    if (stockPlantPanel) stockPlantPanel.insertAdjacentHTML('beforeend', '<div class="no-data-note">Couldn\'t load the stock trend right now.</div>');
  }
}

init();
