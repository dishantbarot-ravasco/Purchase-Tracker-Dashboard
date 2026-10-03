// ── Plant stock tabs: Inventory, On Order, Stock & Orders (2026-09-29) ───
// Project owner: three raw-material tabs that replace Raw Material Analysis
// for everyone but admins (main.js shows that tab to admins only):
//
//   Inventory       - the material in the store at each plant: one row per
//                     material, a stock column per plant on All Plants.
//   On Order        - the material on order for each plant, plus what should
//                     be reordered soon (low on stock, nothing on order).
//   Stock & Orders  - both together: what is held and what is coming, per
//                     material.
//
// SAME LAYOUT AS RAW MATERIAL ANALYSIS (project owner: "design each of these
// tabs with our current design schema, theme and fonts"). Every tab is the
// same stack that view renders, from the same classes: KPI cards
// (.kpi-grid.mat-kpi-grid), the "Filter by" bar (.filter-row), a chart row
// (.chart-row of .chart-panel: a category bar chart and a status doughnut
// with a clickable legend), then the list - the top-5 card preview
// (.top5-row) with "View all" opening the fixed-column table (.fixed-table)
// and its header filter row. Only the column track widths are set from JS,
// because the plant columns come and go with the plant tabs.
//
// SAME FIGURES AS RAW MATERIAL ANALYSIS, NOT A SECOND COPY. Materials come
// from materialScope() (one row per name, summed across vendor lots), their
// PO lines from computeMaterialPoLinkage(), what is still to come from
// openQtyOfLine() / openValueOfLine(), whether a line is open from
// isOpenPoLine(), low stock from isMaterialLowStock() and Days Left from the
// consumption ledger. What these tabs leave out is the reconciliation layer -
// match confidence, mismatch flags, corrections - which is admin work.
//
// A material's PO lines are tied to it by the same best-effort description
// (and vendor) link Raw Material Analysis uses (materials.js's
// buildLineLinks()). A line tied to two materials counts under both rows; the KPI
// totals count each line once.

// `perm` is the permission that shows the tab (auth.js's userHasPerm(),
// apps/api/permissions.py's Perm) - each is granted on its own.
const PLANT_STOCK_TABS = [
  { key: 'inventory', label: 'Inventory', perm: 'view_inventory' },
  { key: 'onorder', label: 'On Order', perm: 'view_on_order' },
  { key: 'combined', label: 'Stock & Orders', perm: 'view_stock_orders' },
];
// Who may read the order books - the server's ORDER_VIEWS. An account with
// Inventory alone loads stock only.
const PS_ORDER_PERMS = ['view_dashboard', 'view_on_order', 'view_stock_orders'];
const PLANT_STOCK_VIEW_KEYS = PLANT_STOCK_TABS.map(t => t.key);

function isAdminUser() {
  return !!(CURRENT_USER && CURRENT_USER.role === 'admin');
}

// One colour per plant, in every stacked chart - the palette the PO charts
// already use (blue, amber, green).
const PS_PLANT_COLORS = { hrs: '#2563eb', achhad: '#d97706', vapi: '#16a34a' };
const PS_CHART_TOP_N = 5;
const PS_DUE_SOON_DAYS = 7;

// Filters per tab. category / subCategory are "global" (they narrow the KPI
// cards and charts too); status, search and the page are table-only, the
// same split Raw Material Analysis makes. showAll is "View all".
function psDefaultState() {
  const base = () => ({ status: null, category: null, subCategory: null, search: '', showAll: false, page: 1 });
  return { inventory: base(), onorder: base(), combined: base() };
}
let PS_STATE = psDefaultState();

// Called by main.js's resetFilters() on every view or plant switch.
function resetPlantViewFilters() {
  PS_STATE = psDefaultState();
}

function psView() {
  return PLANT_STOCK_VIEW_KEYS.includes(state.view) ? state.view : 'inventory';
}

function psSorter(view) {
  if (view === 'onorder') return ORD_SORT;
  if (view === 'combined') return COMB_SORT;
  return INV_SORT;
}

// ── Small helpers ──
function psToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

// Whole days from today to an ISO date: negative once it has passed, null
// when there is no date.
function psDaysFromToday(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
  if (isNaN(d.getTime())) return null;
  return Math.round((d - psToday()) / 86400000);
}

function psIsoInDays(days) {
  const d = psToday();
  d.setDate(d.getDate() + Math.floor(days));
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function psQty(n, uom, digits) {
  if (n == null) return '-';
  return n.toLocaleString('en-IN', { maximumFractionDigits: digits == null ? 3 : digits }) + (uom ? ' ' + uom : '');
}

// "3 days late" / "due today" / "in 5 days".
function psDueText(days) {
  if (days == null) return '';
  if (days < 0) return (-days) + (days === -1 ? ' day late' : ' days late');
  if (days === 0) return 'due today';
  return 'in ' + days + (days === 1 ? ' day' : ' days');
}

// The one unit a set of lots shares, or '' when they name none or differ.
function psLotsUnit(lots) {
  const units = new Set((lots || []).map(l => String(l.uom || '').trim().toUpperCase()).filter(Boolean));
  return units.size === 1 ? Array.from(units)[0] : '';
}

// Same rule as material-sort.js's daysLeft: only where daysLeftCellHtml()
// shows a number.
function psDaysLeft(m) {
  const c = m.consumption;
  return (!m.orderOnly && c && c.confidence !== 'none' && !c.negativeStock && c.daysLeft != null) ? c.daysLeft : null;
}

function psDailyUse(m) {
  const c = m.consumption;
  return (!m.orderOnly && c && c.confidence !== 'none' && c.avgDaily) ? c.avgDaily : null;
}

function psLastReceived(m) {
  return (m.lots || []).map(l => l.receivedDate).filter(Boolean).sort().pop() || null;
}

function psCategory(value) {
  return value && value !== 'Uncategorized' ? value : null;
}

// Low enough to reorder: Raw Material Analysis's Low Stock (under 15 days
// left, or at Achhad's minimum stock level), or out of stock while still in
// use.
function psIsLow(m) {
  if (m.orderOnly) return false;
  if (isMaterialLowStock(m)) return true;
  return !((m.qty || 0) > 0) && psDailyUse(m) != null;
}

// The plants that get a column of their own: every plant in view on All
// Plants, none on a single plant's tab.
function psPlantColumnKeys() {
  const keys = selectedPlantKeys();
  return isAllPlants() && keys.length > 1 ? keys : [];
}

function psShortPlant(key) {
  return PLANTS[key].label.split(',')[0];
}

// A lot's plant: tagged on All Plants, the one plant in view otherwise.
function psLotPlant(l) {
  return l._plantKey || selectedPlantKeys()[0];
}

function psLotsByPlant(m) {
  const by = {};
  (m.lots || []).forEach(l => { const k = psLotPlant(l); (by[k] = by[k] || []).push(l); });
  return by;
}

// What a material's open PO lines add up to: ordered, received and still to
// come per unit family, the value still to come, and the same per plant.
function psOrderFigures(open) {
  const perPlant = {};
  open.forEach(l => {
    const p = perPlant[l.plantKey] || (perPlant[l.plantKey] = { links: [], value: 0 });
    p.links.push(l);
    p.value += openValueOfLine(l.item);
  });
  Object.keys(perPlant).forEach(k => { perPlant[k].qty = summariseOpenQty(perPlant[k].links); });
  return {
    ordered: summariseLineQty(open, it => it.qty),
    received: summariseLineQty(open, it => { const q = openQtyOfLine(it); return it.qty != null && q != null ? it.qty - q : null; }),
    toCome: summariseOpenQty(open),
    toComeValue: open.reduce((s, l) => s + openValueOfLine(l.item), 0),
    perPlant: perPlant,
    poCount: new Set(open.map(l => l.plantKey + '::' + l.po.poNumber)).size,
    partial: open.some(l => lineItemArrived(l.item)),
  };
}

// A material's delivery status from its open lines, judged on each LINE's own
// delivery date (a PO's status takes its earliest line, which would call a
// later line late): overdue when any open line is past due, else due within
// a week, on order, or no due date. `next` is the earliest dated open line.
function psDelivery(open) {
  let overdue = false;
  let next = null;
  open.forEach(l => {
    const due = l.item.deliveryDate;
    if (!due) return;
    if (psDaysFromToday(due) < 0) overdue = true;
    if (!next || due < next.due) next = { due: due, link: l };
  });
  const nextIn = next ? psDaysFromToday(next.due) : null;
  const status = overdue ? 'overdue' : !next ? 'nodate' : nextIn <= PS_DUE_SOON_DAYS ? 'soon' : 'onorder';
  return { status: status, next: next };
}

// Every open line in `rows`, once each (a line tied to two materials sits
// under both rows) - what the KPI totals and charts add up.
function psDistinctOpenLinks(rows) {
  const seen = new Map();
  rows.forEach(r => (r.open || []).forEach(l => { if (!seen.has(l.item)) seen.set(l.item, l); }));
  return Array.from(seen.values());
}

function psPill(def) {
  return '<span class="status-pill ' + def.pill + '">' + escapeHtml(def.label) + '</span>';
}

// A clickable name. A real <button> so it is reachable by keyboard; the
// ps-link class strips the button chrome. data-* only - CSP blocks inline
// handlers, see wirePlantListRegion().
function psMaterialLinkHtml(m) {
  return '<button type="button" class="row-link ps-link mat-name" data-ps-material="' + escapeHtml(normalizeMaterial(m.description)) + '">' + escapeHtml(m.description || m.materialCode || '-') + '</button>';
}

function psQtyText(summary) {
  return summary ? qtySummaryText(summary) : '';
}

// ── Statuses ──
// Ranked most urgent first; `pill` reuses the PO list's status colours
// (red overdue, amber on order, blue partial, green received, grey unknown).
const PS_STOCK_STATUS = {
  low: { rank: 0, label: 'Low stock', pill: 'status-overdue', color: '#dc2626' },
  negative: { rank: 1, label: 'Below zero', pill: 'status-overdue', color: '#7c3aed' },
  out: { rank: 2, label: 'Out of stock', pill: 'status-pending', color: '#d97706' },
  ok: { rank: 3, label: 'In stock', pill: 'status-received', color: '#16a34a' },
  idle: { rank: 4, label: 'Not used lately', pill: 'status-unknown', color: '#64748b' },
};

const PS_ORDER_STATUS = {
  overdue: { rank: 0, label: 'Overdue', pill: 'status-overdue', color: '#dc2626' },
  reorder: { rank: 1, label: 'Reorder soon', pill: 'status-overdue', color: '#7c3aed' },
  soon: { rank: 2, label: 'Due this week', pill: 'status-partial', color: '#2563eb' },
  onorder: { rank: 3, label: 'On order', pill: 'status-pending', color: '#d97706' },
  nodate: { rank: 4, label: 'No due date', pill: 'status-unknown', color: '#64748b' },
};

// Where a material stands, stock and orders together.
const PS_POSITION = {
  lownoorder: { rank: 0, label: 'Low, nothing on order', pill: 'status-overdue', color: '#dc2626' },
  both: { rank: 1, label: 'In stock and on order', pill: 'status-partial', color: '#2563eb' },
  orderonly: { rank: 2, label: 'On order, none in stock', pill: 'status-pending', color: '#d97706' },
  stockonly: { rank: 3, label: 'In stock', pill: 'status-received', color: '#16a34a' },
  none: { rank: 4, label: 'Nothing held', pill: 'status-unknown', color: '#64748b' },
};

function psStockStatus(m) {
  if ((m.qty || 0) < 0) return 'negative';
  if (!((m.qty || 0) > 0)) return 'out';
  if (isMaterialLowStock(m)) return 'low';
  // Watched and nothing issued in the window - stock sitting idle. Without
  // enough history there is nothing to say either way, so it reads In stock.
  const c = m.consumption;
  if (c && c.confidence !== 'none' && !c.avgDaily) return 'idle';
  return 'ok';
}

// ── Rows ──
// Every row carries `sv`, the values plant-stock-sort.js sorts on, so the
// sorters only read fields.
function psBaseSortValues(m) {
  return {
    material: m.description || null,
    category: psCategory(m.category),
    subCategory: psCategory(m.subCategory),
    stock: m.orderOnly ? null : (m.qty || 0),
    value: m.orderOnly ? null : (m.value || 0),
    daysLeft: psDaysLeft(m),
  };
}

function psInventoryRows(scope) {
  return scope.all.filter(m => !m.orderOnly).map(m => {
    const status = psStockStatus(m);
    const byPlant = psLotsByPlant(m);
    const sv = Object.assign(psBaseSortValues(m), {
      dailyUse: psDailyUse(m),
      received: psLastReceived(m),
      status: PS_STOCK_STATUS[status].rank,
    });
    PLANT_KEYS.forEach(k => { sv[k + 'Stock'] = byPlant[k] ? byPlant[k].reduce((s, l) => s + (l.qty || 0), 0) : null; });
    return { m: m, status: status, byPlant: byPlant, sv: sv };
  });
}

// Materials with an open PO line, plus the ones to reorder soon: low on
// stock with nothing on order.
function psOrderRows(scope) {
  const rows = [];
  scope.all.forEach(m => {
    const entry = scope.entryByNorm.get(normalizeMaterial(m.description)) || null;
    const open = entry ? entry.openLinks : [];
    const low = psIsLow(m);
    if (!open.length && !low) return;
    const delivery = open.length ? psDelivery(open) : { status: 'reorder', next: null };
    const figures = open.length ? psOrderFigures(open) : null;
    const sv = Object.assign(psBaseSortValues(m), {
      toComeValue: figures ? figures.toComeValue : null,
      nextDue: delivery.next ? delivery.next.due : null,
      status: PS_ORDER_STATUS[delivery.status].rank,
    });
    PLANT_KEYS.forEach(k => { sv[k + 'ToCome'] = figures && figures.perPlant[k] ? figures.perPlant[k].value : null; });
    rows.push({ m: m, entry: entry, open: open, low: low, status: delivery.status, next: delivery.next, figures: figures, sv: sv });
  });
  return rows;
}

function psCombinedRows(scope) {
  return scope.all.map(m => {
    const entry = scope.entryByNorm.get(normalizeMaterial(m.description)) || null;
    const open = entry ? entry.openLinks : [];
    const held = (m.qty || 0) > 0;
    const low = psIsLow(m);
    const position = low && !open.length ? 'lownoorder'
      : held && open.length ? 'both'
        : open.length ? 'orderonly'
          : held ? 'stockonly' : 'none';
    const delivery = open.length ? psDelivery(open) : null;
    const figures = open.length ? psOrderFigures(open) : null;
    const sv = Object.assign(psBaseSortValues(m), {
      toComeValue: figures ? figures.toComeValue : null,
      nextDue: delivery && delivery.next ? delivery.next.due : null,
      status: PS_POSITION[position].rank,
    });
    return {
      m: m, entry: entry, open: open, low: low, status: position,
      overdue: !!delivery && delivery.status === 'overdue',
      next: delivery ? delivery.next : null, figures: figures, sv: sv,
    };
  });
}

// ── Loading ──
async function loadAndRenderPlantView() {
  const view = psView();
  const plantAtStart = state.plant;
  const el = document.getElementById('viewContent');
  el.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading ' + escapeHtml(PLANT_STOCK_TABS.find(t => t.key === view).label.toLowerCase()) + '&hellip;</div></div>';
  try {
    // Stock and both order books for every tab - they share the caches, so
    // switching between the three never waits a second time. The order
    // books only for an account that may read them (Inventory needs none).
    const keys = selectedPlantKeys();
    // Each plant's chosen source (Drive sheets or the in-app RM store and
    // MIRs - main.js's ensurePOsLoaded()); imports have only the Drive one.
    const loads = [ensureMaterialsLoaded(keys, { stockTabs: true }), psSorter(view).ensurePresetsLoaded()];
    if (userHasPerm(...PS_ORDER_PERMS)) loads.push(ensurePOsLoaded(keys, { stockTabs: true }), ensureImportPOsLoaded());
    await Promise.all(loads);
  } catch (e) {
    console.error('loadAndRenderPlantView failed:', e);
    if (psView() !== view || state.plant !== plantAtStart) return true;
    el.innerHTML = '<div class="noaccess">Couldn\'t load stock and order data right now. Please refresh, or contact IT if this keeps happening.</div>';
    return false;
  }
  // The reader moved on while this loaded; their newer click renders.
  if (psView() !== view || state.plant !== plantAtStart) return true;
  el.innerHTML = '<div id="plantStockContent"></div>';
  renderPlantView();
  return true;
}

// ── Page layout (shared by the three tabs) ──
// Everything a list-region re-render needs, set by renderPlantView(): a
// search keystroke or a sort change rebuilds the list alone, never the KPI
// row or the charts (same split as materials.js's MAT_LIST_CTX).
let PS_LIST_CTX = null;

function psBuildScope() {
  const keys = selectedPlantKeys();
  const all = materialScope(keys);
  const linkage = computeMaterialPoLinkage(all, keys, all);
  return { keys, all, entryByNorm: new Map(linkage.map(l => [normalizeMaterial(l.material.description), l])) };
}

const PS_VIEWS = {
  inventory: { rows: psInventoryRows, statuses: PS_STOCK_STATUS, render: psInventoryParts, columns: psInventoryColumns },
  onorder: { rows: psOrderRows, statuses: PS_ORDER_STATUS, render: psOnOrderParts, columns: psOnOrderColumns },
  combined: { rows: psCombinedRows, statuses: PS_POSITION, render: psCombinedParts, columns: psCombinedColumns },
};

// ── Where the figures come from (owner, 2026-10-03) ──
// Each plant reads either its Drive sheets or the in-app RM store and MIRs
// (apps/services/app_stock_source.py), never both added together. The bar
// says which, per selected plant; an admin can switch a plant at once.
const PS_SOURCE_LABEL = { drive: 'Drive sheets', app: 'In-app MIR and RM store' };
const PS_SOURCE_SWITCH = { drive: 'Switch to Drive sheets', app: 'Switch to in-app records' };

function psSourceBarHtml() {
  if (!STOCK_SOURCE) return '';
  const keys = selectedPlantKeys();
  return '<div class="ps-source-bar" role="note"><span class="ps-source-k">Figures from</span>' +
    keys.map(key => {
      const src = stockSourceOf(key);
      const other = src === 'app' ? 'drive' : 'app';
      return '<span class="ps-source-item"><b>' + escapeHtml(PLANTS[key].label) + ':</b> ' + escapeHtml(PS_SOURCE_LABEL[src]) +
        (STOCK_SOURCE.canChange ? ' <button type="button" class="ps-source-switch" data-ps-source="' + escapeHtml(key) + '" data-to="' + other + '">' +
          escapeHtml(PS_SOURCE_SWITCH[other]) + '</button>' : '') + '</span>';
    }).join('') + '</div>';
}

function wirePsSourceBar(el) {
  el.querySelectorAll('[data-ps-source]').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await apiAt('/api/stock-source/set', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plant: btn.dataset.psSource, source: btn.dataset.to }) });
        STOCK_SOURCE.sources[res.plant] = res.source;
        // The loaders see the cache holds the other source and re-fetch it.
        await loadAndRenderPlantView();
      } catch (e) {
        btn.disabled = false;
        alert(e.message);
      }
    };
  });
}

function renderPlantView() {
  const el = document.getElementById('plantStockContent');
  if (!el) return;
  destroyPageCharts();
  const view = psView();
  const cfg = PS_VIEWS[view];
  const f = PS_STATE[view];
  const all = cfg.rows(psBuildScope());
  if (!all.length) {
    const label = PLANT_STOCK_TABS.find(t => t.key === view).label;
    el.innerHTML = '<div class="section-title">' + escapeHtml(label) + ': ' + escapeHtml(plantDisplayLabel()) + '</div>' + psSourceBarHtml() +
      '<div class="empty-state">' + emptyStateHtml(view === 'onorder' ? 'Nothing is on order and nothing needs reordering right now.' : 'No stock synced yet for these plants.') + '</div>';
    PS_LIST_CTX = null;
    wirePsSourceBar(el);
    return;
  }
  // Category and Sub Category narrow everything - KPIs, charts and list.
  const catCounts = psCounts(all, r => r.sv.category || 'Uncategorized');
  const inCategory = f.category ? all.filter(r => (r.sv.category || 'Uncategorized') === f.category) : all;
  const subCounts = psCounts(inCategory, r => r.sv.subCategory || 'Uncategorized');
  const filtered = f.subCategory ? inCategory.filter(r => (r.sv.subCategory || 'Uncategorized') === f.subCategory) : inCategory;
  const parts = cfg.render(filtered);
  const catOptions = Object.entries(catCounts).sort((a, b) => b[1] - a[1]);
  const subOptions = Object.entries(subCounts).sort((a, b) => b[1] - a[1]);

  el.innerHTML =
    '<div class="section-title">' + escapeHtml(parts.title) + ': ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">' + parts.sub + '</div>' + psSourceBarHtml() +
    '<div class="kpi-grid mat-kpi-grid ps-kpi-grid">' + parts.cards.map(c => psKpiCardHtml(c, f.status)).join('') + '</div>' +
    '<div class="filter-row">' +
      '<div class="filter-group"><label for="psCatSelect">Filter by Category</label>' +
        '<select id="psCatSelect" class="select-w220"><option value="">All categories (' + all.length + ')</option>' + psOptionsHtml(catOptions, f.category) + '</select></div>' +
      '<div class="filter-group"><label for="psSubCatSelect">Filter by Sub Category</label>' +
        '<select id="psSubCatSelect" class="select-w220"><option value="">All sub-categories (' + inCategory.length + ')</option>' + psOptionsHtml(subOptions, f.subCategory) + '</select></div>' +
      '<div class="filter-group"><label for="psStatusSelect">Filter by Status</label>' +
        '<select id="psStatusSelect" class="select-w220">' + psStatusOptionsHtml(parts.statusOptions, f.status) + '</select></div>' +
      ((f.category || f.subCategory || f.status) ? '<button type="button" id="psClearFilters">Clear</button>' : '') +
    '</div>' +
    '<div class="chart-row">' + parts.charts.map(c => c.html).join('') + '</div>' +
    '<div id="psListRegion"></div>';

  PS_LIST_CTX = { view: view, rows: filtered, catOptions: catOptions, subOptions: subOptions, statusOptions: parts.statusOptions, defaultHidden: parts.defaultHidden || null };
  applyDynamicStyles(el);
  wireKpiCountUps(el);
  wirePlantViewChrome(el);
  wirePsSourceBar(el);
  parts.charts.forEach(c => { if (c.wire) c.wire(); });
  renderPlantListRegion();
}

function psCounts(rows, keyOf) {
  const counts = {};
  rows.forEach(r => { const k = keyOf(r); counts[k] = (counts[k] || 0) + 1; });
  return counts;
}

function psOptionsHtml(entries, selected) {
  return entries.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (selected === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('');
}

// [{value, label, count?}] - the first is the "no filter" choice ('').
function psStatusOptionsHtml(options, selected) {
  return options.map(o => '<option value="' + escapeHtml(o.value) + '"' + ((selected || '') === o.value ? ' selected' : '') + '>' +
    escapeHtml(o.label) + (o.count != null ? ' (' + o.count + ')' : '') + '</option>').join('');
}

function psStatusChoices(defs, counts, first, extra) {
  return first.concat(extra || []).concat(Object.keys(defs).map(k => ({ value: k, label: defs[k].label, count: counts[k] || 0 })));
}

// Same card as Raw Material Analysis's KPI row. A card with `filter` narrows
// the list to that status (a second click clears it); one without clears
// the filter, since it counts the whole list.
function psKpiCardHtml(c, active) {
  const on = !!c.filter && active === c.filter;
  return '<div class="kpi-card ' + (c.cls || '') + (on ? ' active' : '') + '" data-ps-kpi="' + escapeHtml(c.filter || '') + '" tabindex="0" role="button" aria-pressed="' + on + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="mat-kpi-valrow"><span class="val" data-count-target="' + c.raw + '" data-count-fmt="' + (c.fmt || 'int') + '">0</span></div>' +
    '<div class="label">' + escapeHtml(c.label) + infoTooltipHtml(c.tip) + '</div></div>';
}

// A doughnut panel: slices of a status set, the legend beside it filtering
// the list exactly like the matching KPI card.
function psStatusDoughnut(id, title, sub, defs, counts, centerLabel) {
  const slices = Object.keys(defs).map(k => ({ key: k, label: defs[k].label, val: counts[k] || 0, color: defs[k].color })).filter(s => s.val > 0);
  const total = slices.reduce((s, x) => s + x.val, 0);
  const legend = chartLegendHtml([{ items: slices.map(s => ({ key: s.key, label: s.label, color: s.color, valText: String(s.val), pctText: total ? sharePct(s.val, total) : null })) }],
    PS_STATE[psView()].status, true);
  return {
    html: '<div class="chart-panel">' + chartHeadHtml(title, sub) +
      (total ? '<div class="doughnut-layout"><div class="chart-box chart-box-doughnut"><canvas id="' + id + '"></canvas></div>' + legend + '</div>'
        : '<div class="no-data-note">Nothing to chart.</div>') + '</div>',
    wire: () => {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const panel = canvas.closest('.chart-panel');
      wireChartLegend(panel, key => psSetStatus(key));
      try {
        const ringGap = (getComputedStyle(document.documentElement).getPropertyValue('--card') || '').trim() || '#ffffff';
        pageCharts.push(new Chart(canvas, {
          type: 'doughnut',
          data: { labels: slices.map(s => s.label), datasets: [{ data: slices.map(s => s.val), backgroundColor: slices.map(s => s.color), borderWidth: 2, borderColor: ringGap, borderRadius: 2,
            offset: slices.map(s => (s.key === PS_STATE[psView()].status ? 10 : 0)) }] },
          options: {
            maintainAspectRatio: false,
            cutout: '62%',
            onClick: (evt, els) => { if (els.length) psSetStatus(slices[els[0].index].key); },
            onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
            plugins: { legend: { display: false }, tooltip: { callbacks: {
              label: c => ' ' + c.label + ': ' + c.parsed + ' (' + sharePct(c.parsed, total) + ')',
              afterLabel: () => 'Click to filter the list below',
            } } },
          },
          plugins: [psCenterText(centerLabel)],
        }));
      } catch (e) {
        console.error('Status chart failed to render:', e);
        canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
      }
    },
  };
}

// The doughnut's centre readout - charts.js's centerTextPlugin, with this
// chart's own label instead of "TOTAL POs".
function psCenterText(label) {
  return {
    id: 'psCenterText',
    afterDatasetsDraw(chart) {
      const area = chart.chartArea;
      if (!area) return;
      const total = chart.data.datasets[0].data.reduce((a, b) => a + b, 0);
      const ctx = chart.ctx;
      const cx = (area.left + area.right) / 2;
      const cy = (area.top + area.bottom) / 2;
      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = CHART_STRONG;
      ctx.fillText(total.toLocaleString('en-IN'), cx, cy - 9);
      ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = CHART_MUTED;
      ctx.fillText(label, cx, cy + 11);
      ctx.restore();
    },
  };
}

// A horizontal bar panel over categories: the top PS_CHART_TOP_N by total,
// one dataset per series (plants, or stock vs on order), clicking a bar
// filters the page to that category.
function psCategoryBars(id, title, sub, rows, series, stacked) {
  const totals = new Map();
  rows.forEach(r => {
    const cat = r.sv.category || 'Uncategorized';
    const t = totals.get(cat) || series.map(() => 0);
    series.forEach((s, i) => { t[i] += s.valueOf(r) || 0; });
    totals.set(cat, t);
  });
  const ranked = Array.from(totals.entries()).map(([cat, vals]) => ({ cat, vals, sum: vals.reduce((a, b) => a + b, 0) }))
    .filter(x => x.sum > 0).sort((a, b) => b.sum - a.sum);
  const bars = ranked.slice(0, PS_CHART_TOP_N);
  const rest = ranked.slice(PS_CHART_TOP_N);
  const grand = ranked.reduce((a, x) => a + x.sum, 0);
  const seriesTotals = series.map((s, i) => ranked.reduce((a, x) => a + x.vals[i], 0));
  const legend = series.length > 1
    ? chartLegendHtml([{ items: series.map((s, i) => ({ key: null, label: s.label, color: s.color, valText: formatInr(seriesTotals[i]) })) }])
    : '';
  return {
    html: '<div class="chart-panel">' + chartHeadHtml(title, sub, 'Total in view', bars.length ? formatInr(grand) : null) +
      (bars.length ? '<div class="chart-box"><canvas id="' + id + '"></canvas></div>' + legend +
        (rest.length ? '<div class="chart-foot"><span class="no-data-note">Top ' + PS_CHART_TOP_N + ' by value. ' + rest.length + ' more ' + (rest.length === 1 ? 'category holds ' : 'categories hold ') + escapeHtml(formatInr(rest.reduce((a, x) => a + x.sum, 0))) + '.</span></div>' : '')
        : '<div class="no-data-note">Nothing to chart.</div>') + '</div>',
    wire: () => {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      try {
        pageCharts.push(new Chart(canvas, {
          type: 'bar',
          data: {
            labels: bars.map(b => categoryLabel(b.cat)),
            datasets: series.map((s, i) => ({ label: s.label, data: bars.map(b => b.vals[i]), backgroundColor: s.color, borderRadius: 4, borderSkipped: false, maxBarThickness: 26 })),
          },
          options: {
            indexAxis: 'y',
            maintainAspectRatio: false,
            onClick: (evt, els) => {
              if (!els.length) return;
              const f = PS_STATE[psView()];
              const cat = bars[els[0].index].cat;
              f.category = f.category === cat ? null : cat;
              f.subCategory = null;
              f.page = 1;
              renderPlantView();
            },
            onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: { label: c => c.dataset.label + ': ' + formatInr(c.parsed.x), footer: () => 'Click to filter by this category' } },
            },
            scales: {
              x: { stacked: !!stacked, grid: { color: CHART_GRID }, border: { display: false }, ticks: { maxTicksLimit: 6, callback: v => formatInr(v) } },
              y: { stacked: !!stacked, grid: { display: false }, border: { color: CHART_GRID }, ticks: { callback: function (v) { const t = this.getLabelForValue(v) || ''; return t.length > 26 ? t.slice(0, 25) + '...' : t; } } },
            },
          },
        }));
      } catch (e) {
        console.error('Category chart failed to render:', e);
        canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
      }
    },
  };
}

// The series a category chart stacks: one per plant on All Plants, else one.
function psPlantSeries(valueAtPlant, singleLabel) {
  const keys = psPlantColumnKeys();
  if (!keys.length) return [{ label: singleLabel, color: '#2563eb', valueOf: r => valueAtPlant(r, null) }];
  return keys.map(k => ({ label: psShortPlant(k), color: PS_PLANT_COLORS[k] || '#64748b', valueOf: r => valueAtPlant(r, k) }));
}

function psSetStatus(key) {
  const f = PS_STATE[psView()];
  f.status = f.status === key ? null : key;
  f.page = 1;
  renderPlantView();
  if (f.status) revealFilteredList('psListRegion');
}

// ── Inventory ──
function psInventoryParts(rows) {
  const counts = psCounts(rows, r => r.status);
  const held = rows.filter(r => (r.m.qty || 0) > 0);
  const value = rows.reduce((s, r) => s + (r.m.value || 0), 0);
  const idleValue = rows.filter(r => r.status === 'idle').reduce((s, r) => s + (r.m.value || 0), 0);
  const plantKeys = psPlantColumnKeys();
  const lotValueAt = (r, k) => (k ? (r.byPlant[k] || []) : (r.m.lots || [])).reduce((s, l) => s + (l.value || 0), 0);
  const perPlantValue = plantKeys.map(k => psShortPlant(k) + ' ' + formatInr(rows.reduce((s, r) => s + lotValueAt(r, k), 0)));
  return {
    title: 'Inventory',
    sub: 'The material in the store' + (plantKeys.length ? ' at each plant, side by side' : '') + ', from the RM stock sheet. One row per material, however many vendor lots it has; materials at zero are left out unless you pick Out of Stock.' +
      (plantKeys.length ? ' Days Left is the plant that runs out first.' : '') + ' Click a material for its lots and orders.',
    cards: [
      { label: 'Materials in Stock', raw: held.length, tip: 'Materials with a positive quantity on the stock sheet. Every vendor lot of one material counts as one material.' },
      { label: 'Stock Value', raw: value, fmt: 'inr', tip: 'The stock sheet\'s own Value column, summed' + (perPlantValue.length ? ': ' + perPlantValue.join(', ') : '') + '. A lot below zero subtracts - fix it in the sheet.' },
      { filter: 'low', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Low Stock', raw: counts.low || 0, tip: 'Under 15 days of stock at the recent rate of use, or at or below Achhad\'s minimum stock level. The On Order tab lists which of these have nothing on order.' },
      { filter: 'out', cls: 'partial', label: 'Out of Stock', raw: counts.out || 0, tip: 'Listed on the stock sheet with nothing left.' },
      { filter: 'idle', label: 'Not Used Lately', raw: counts.idle || 0, tip: 'In stock, but nothing issued in the recent window: ' + formatInr(idleValue) + ' of stock sitting idle.' },
      { filter: 'negative', cls: 'flags', label: 'Below Zero', raw: counts.negative || 0, tip: 'The stock sheet shows less than zero - a data error to correct in the sheet.' },
    ],
    statusOptions: psStatusChoices(PS_STOCK_STATUS, counts, [{ value: '', label: 'All in stock' }], [{ value: 'everything', label: 'Everything, including out of stock' }]),
    defaultHidden: r => r.status === 'out',
    charts: [
      psCategoryBars('psInvCatChart', 'Stock Value by Category', plantKeys.length ? 'What each plant holds in each category. Click a bar to filter.' : 'Stock value on hand, largest first. Click a bar to filter.',
        rows, psPlantSeries(lotValueAt, 'Stock value'), true),
      psStatusDoughnut('psInvStatusChart', 'Stock Status', 'Each slice matches its card above. Click a slice or a label to filter the list.', PS_STOCK_STATUS, counts, 'MATERIALS'),
    ],
  };
}

function psInventoryColumns() {
  const plantCols = psPlantColumnKeys().map(k => ({
    label: psShortPlant(k), sort: k + 'Stock', w: [95, 0.9],
    tip: 'In the store at ' + PLANTS[k].label + ', and its value.',
    cell: r => {
      const lots = r.byPlant[k];
      if (!lots) return '<span class="mat-cell-none">-</span>';
      return '<div class="mat-cell-main">' + escapeHtml(lotsQtyText(lots)) + '</div>' +
        '<div class="mat-cell-sub">' + formatInr(lots.reduce((s, l) => s + (l.value || 0), 0)) + '</div>';
    },
  }));
  return [
    { label: 'Material', sort: 'material', filter: 'search', w: [170, 1.7], cell: r => {
      const n = (r.m.lots || []).length;
      const rec = r.sv.received;
      const ahead = rec && psDaysFromToday(rec) > 0;
      return psMaterialLinkHtml(r.m) + '<div class="mat-cell-sub">' + n + (n === 1 ? ' lot' : ' lots') +
        (rec ? ' &middot; <span' + (ahead ? ' class="imp-overdue" title="A date after today is a sheet error, often day and month swapped"' : '') + '>last received ' + escapeHtml(formatDateIN(rec)) + '</span>' : '') + '</div>';
    } },
    { label: 'Category', sort: 'category', filter: 'category', w: [110, 1], cell: r => categoryCellHtml(r.m) },
    { label: 'Sub Category', sort: 'subCategory', filter: 'subCategory', w: [110, 1], cell: r => subCategoryCellHtml(r.m) },
  ].concat(plantCols).concat([
    { label: plantCols.length ? 'Total Stock' : 'In Stock', sort: 'stock', w: [105, 1], tip: 'Quantity on hand on the stock sheet' + (plantCols.length ? ', all plants together' : '') + '.',
      cell: r => '<div class="mat-cell-main">' + escapeHtml(lotsQtyText(r.m.lots || [])) + '</div>' },
    { label: 'Stock Value', sort: 'value', w: [105, 1], tip: 'The stock sheet\'s own Value column, with the latest rate under it.', cell: r => stockValueCellHtml(r.m) },
    { label: 'Daily Use', sort: 'dailyUse', w: [90, 0.8], tip: 'Average issued per day over the recent window.', cell: r => psDailyUseCellHtml(r.m) },
    { label: 'Days Left', sort: 'daysLeft', w: [95, 0.85], tip: 'Stock divided by daily use. The dot shows how much history it rests on.', cell: r => psDaysLeftCellHtml(r.m) },
    { label: 'Status', sort: 'status', filter: 'status', w: [105, 0.9], cell: r => psPill(PS_STOCK_STATUS[r.status]) },
  ]);
}

// ── On Order ──
function psOnOrderParts(rows) {
  const counts = psCounts(rows, r => r.status);
  const onOrder = rows.filter(r => r.open.length);
  const lines = psDistinctOpenLinks(onOrder);
  const toCome = lines.reduce((s, l) => s + openValueOfLine(l.item), 0);
  const overdueValue = psDistinctOpenLinks(rows.filter(r => r.status === 'overdue')).reduce((s, l) => s + openValueOfLine(l.item), 0);
  const partial = onOrder.filter(r => r.figures.partial).length;
  const plantKeys = psPlantColumnKeys();
  return {
    title: 'On Order',
    sub: 'The material on order' + (plantKeys.length ? ' for each plant, side by side' : '') + ' (domestic and import POs), and what to reorder soon: materials low on stock with nothing on order. What has arrived comes from the MIR receipts matched to each line. Click a material for its PO lines.',
    cards: [
      { label: 'Materials on Order', raw: onOrder.length, tip: lines.length.toLocaleString('en-IN') + ' open PO lines across ' + onOrder.length.toLocaleString('en-IN') + ' materials, domestic and import.' },
      { label: 'Value Still to Come', raw: toCome, fmt: 'inr', tip: 'What is still to arrive on the open lines, at each line\'s PO rate in INR, before tax. Each line counted once.' },
      { filter: 'overdue', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Overdue', raw: counts.overdue || 0, tip: 'Materials with an open line past its delivery date: ' + formatInr(overdueValue) + ' still to come on them. Chase the vendor.' },
      { filter: 'soon', cls: 'partial', label: 'Due This Week', raw: counts.soon || 0, tip: 'Next delivery due today or in the next ' + PS_DUE_SOON_DAYS + ' days - expect these at the gate.' },
      { filter: 'partial', label: 'Part Received', raw: partial, tip: 'Some of an open line has come in and the rest is still to come.' },
      { filter: 'reorder', cls: 'flags', flag: KPI_FLAG_COLORS.quality, label: 'Reorder Soon', raw: counts.reorder || 0, tip: 'Low on stock (under 15 days left, at the minimum stock level, or out while still in use) with nothing on order.' },
    ],
    statusOptions: psStatusChoices(PS_ORDER_STATUS, counts, [{ value: '', label: 'All' }], [{ value: 'partial', label: 'Part received', count: partial }]),
    charts: [
      psDueChart(onOrder),
      psStatusDoughnut('psOrdStatusChart', 'Status Breakdown', 'Each slice matches its card above. Click a slice or a label to filter the list.', PS_ORDER_STATUS, counts, 'MATERIALS'),
    ],
  };
}

// Value still to come by when it is due: Overdue, then each month from this
// one, then no due date - stacked by plant on All Plants. Each open line
// counted once.
function psDueChart(rows) {
  const lines = psDistinctOpenLinks(rows);
  const thisMonth = psIsoInDays(0).slice(0, 7);
  const bucketOf = l => {
    const due = l.item.deliveryDate;
    if (!due) return 'nodate';
    if (psDaysFromToday(due) < 0) return 'overdue';
    return due.slice(0, 7) < thisMonth ? thisMonth : due.slice(0, 7);
  };
  const months = Array.from(new Set(lines.map(bucketOf).filter(b => b !== 'overdue' && b !== 'nodate'))).sort();
  const buckets = ['overdue'].concat(months).concat(['nodate']);
  const labelOf = b => (b === 'overdue' ? 'Overdue' : b === 'nodate' ? 'No date' : shortMonthLabel(b));
  const plantKeys = psPlantColumnKeys();
  const series = plantKeys.length
    ? plantKeys.map(k => ({ label: psShortPlant(k), color: PS_PLANT_COLORS[k] || '#64748b', match: l => l.plantKey === k }))
    : [{ label: 'Still to come', color: '#2563eb', match: () => true }];
  const data = series.map(s => buckets.map(b => lines.filter(l => s.match(l) && bucketOf(l) === b).reduce((a, l) => a + openValueOfLine(l.item), 0)));
  const total = lines.reduce((a, l) => a + openValueOfLine(l.item), 0);
  const legend = series.length > 1
    ? chartLegendHtml([{ items: series.map((s, i) => ({ key: null, label: s.label, color: s.color, valText: formatInr(data[i].reduce((a, b) => a + b, 0)) })) }])
    : '';
  return {
    html: '<div class="chart-panel">' + chartHeadHtml('Still to Come by Due Date', 'Value still to arrive, by the month it is due. Click Overdue or No date to list those materials.', 'Total in view', lines.length ? formatInr(total) : null) +
      (lines.length ? '<div class="chart-box"><canvas id="psOrdDueChart"></canvas></div>' + legend : '<div class="no-data-note">Nothing on order.</div>') + '</div>',
    wire: () => {
      const canvas = document.getElementById('psOrdDueChart');
      if (!canvas) return;
      try {
        pageCharts.push(new Chart(canvas, {
          type: 'bar',
          data: {
            labels: buckets.map(labelOf),
            datasets: series.map((s, i) => ({
              label: s.label, data: data[i],
              backgroundColor: buckets.map(b => (b === 'overdue' && series.length === 1 ? '#dc2626' : s.color)),
              borderRadius: i === series.length - 1 ? { topLeft: 4, topRight: 4 } : 0, borderSkipped: false, maxBarThickness: 42, categoryPercentage: 0.7,
            })),
          },
          options: {
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            onClick: (evt, els) => {
              if (!els.length) return;
              const b = buckets[els[0].index];
              if (b === 'overdue' || b === 'nodate') psSetStatus(b);
            },
            onHover: (evt, els) => { const b = els.length ? buckets[els[0].index] : null; evt.native.target.style.cursor = (b === 'overdue' || b === 'nodate') ? 'pointer' : 'default'; },
            plugins: {
              legend: { display: false },
              tooltip: { filter: c => c.parsed.y > 0, callbacks: {
                label: c => c.dataset.label + ': ' + formatInr(c.parsed.y),
                footer: items => 'Total: ' + formatInr(items.reduce((a, c) => a + c.parsed.y, 0)),
              } },
            },
            scales: {
              x: { stacked: true, grid: { display: false }, border: { color: CHART_GRID }, ticks: { maxRotation: 0, autoSkipPadding: 8 } },
              y: { stacked: true, grid: { color: CHART_GRID }, border: { display: false }, ticks: { maxTicksLimit: 6, callback: v => formatInr(v) } },
            },
          },
        }));
      } catch (e) {
        console.error('Due-date chart failed to render:', e);
        canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
      }
    },
  };
}

function psNextDueCellHtml(r) {
  if (!r.open || !r.open.length) return '<span class="mat-cell-none">Nothing on order</span>';
  if (!r.next) return '<span class="mat-cell-none">No due date</span>';
  const inDays = psDaysFromToday(r.next.due);
  return '<div class="mat-cell-main">' + escapeHtml(formatDateIN(r.next.due)) + '</div>' +
    '<div class="mat-cell-sub' + (inDays < 0 ? ' imp-overdue' : '') + '">' + escapeHtml(psDueText(inDays)) + '</div>' +
    '<div class="mat-cell-sub">PO ' + escapeHtml(r.next.link.po.poNumber) + '</div>';
}

function psToComeCellHtml(r, plantSplit) {
  if (!r.figures) return '<span class="mat-cell-none">Nothing on order</span>';
  const f = r.figures;
  const split = plantSplit ? Object.keys(PLANTS).filter(k => f.perPlant[k]) : [];
  return '<div class="mat-cell-main">' + escapeHtml(psQtyText(f.toCome) || '-') + '</div>' +
    '<div class="mat-cell-sub">' + formatInr(f.toComeValue) + ' &middot; ' + f.poCount + (f.poCount === 1 ? ' PO' : ' POs') + '</div>' +
    (split.length > 1 ? split.map(k => '<div class="mat-cell-sub mat-plant-split"><span>' + escapeHtml(psShortPlant(k)) + '</span><span>' + escapeHtml(psQtyText(f.perPlant[k].qty) || '-') + '</span></div>').join('') : '');
}

function psOnOrderColumns() {
  const plantCols = psPlantColumnKeys().map(k => ({
    label: psShortPlant(k), sort: k + 'ToCome', w: [100, 0.95],
    tip: 'Still to come for ' + PLANTS[k].label + ', and its value.',
    cell: r => {
      const p = r.figures && r.figures.perPlant[k];
      if (!p) return '<span class="mat-cell-none">-</span>';
      return '<div class="mat-cell-main">' + escapeHtml(psQtyText(p.qty) || '-') + '</div><div class="mat-cell-sub">' + formatInr(p.value) + '</div>';
    },
  }));
  const vendorsOf = r => Array.from(new Set(r.open.map(l => l.po.vendorName).filter(Boolean)));
  return [
    { label: 'Material', sort: 'material', filter: 'search', w: [170, 1.6], cell: r => {
      const vendors = vendorsOf(r);
      return psMaterialLinkHtml(r.m) + (r.m.orderOnly ? '<span class="mat-tag">Not in stock</span>' : '') +
        (vendors.length ? '<div class="mat-cell-sub">' + escapeHtml(vendors[0]) + (vendors.length > 1 ? ' +' + (vendors.length - 1) + ' more' : '') + '</div>' : '');
    } },
    { label: 'Category', sort: 'category', filter: 'category', w: [110, 1], cell: r => categoryCellHtml(r.m) },
  ].concat(plantCols).concat([
    { label: 'Ordered', w: [100, 0.9], tip: 'Ordered on the open lines, including what has already arrived.',
      cell: r => (r.figures ? '<div class="mat-cell-main">' + escapeHtml(psQtyText(r.figures.ordered) || '-') + '</div>' : '<span class="mat-cell-none">-</span>') },
    { label: 'Received', w: [100, 0.9], tip: 'Received so far on those lines, from their matched MIR receipts.',
      cell: r => (r.figures ? '<div class="mat-cell-main">' + escapeHtml(psQtyText(r.figures.received) || '-') + '</div>' + (r.figures.partial ? '<span class="mat-tag">Part received</span>' : '') : '<span class="mat-cell-none">-</span>') },
    { label: plantCols.length ? 'Total to Come' : 'Still to Come', sort: 'toComeValue', w: [120, 1.1], tip: 'Ordered less received, its value at the PO rate (INR, before tax) and how many POs.',
      cell: r => psToComeCellHtml(r, false) },
    { label: 'Next Due', sort: 'nextDue', w: [105, 0.95], tip: 'The earliest delivery date among the open lines - a passed date is a late line.', cell: psNextDueCellHtml },
    { label: 'In Stock', w: [105, 0.95], tip: 'Held now, and days of stock left.', cell: r => (r.m.orderOnly ? '<span class="mat-cell-none">None</span>'
      : '<div class="mat-cell-main">' + escapeHtml(lotsQtyText(r.m.lots || [])) + '</div>' + (psDaysLeft(r.m) != null ? '<div class="mat-cell-sub' + (r.low ? ' imp-overdue' : '') + '">' + Math.round(psDaysLeft(r.m)).toLocaleString('en-IN') + ' days left</div>' : '')) },
    { label: 'Status', sort: 'status', filter: 'status', w: [110, 0.95], cell: r => psPill(PS_ORDER_STATUS[r.status]) + (r.low && r.status !== 'reorder' ? '<div><span class="mat-tag">Low stock</span></div>' : '') },
  ]);
}

// ── Stock & Orders ──
function psCombinedParts(rows) {
  const counts = psCounts(rows, r => r.status);
  const value = rows.reduce((s, r) => s + (r.m.value || 0), 0);
  const toCome = psDistinctOpenLinks(rows).reduce((s, l) => s + openValueOfLine(l.item), 0);
  const overdue = rows.filter(r => r.overdue).length;
  const plantKeys = psPlantColumnKeys();
  return {
    title: 'Stock & Orders',
    sub: 'What each material has in the store and what is on order for it, side by side' + (plantKeys.length ? ', with each plant\'s share under the totals' : '') + '. Materials with nothing held and nothing on order are left out unless you pick Nothing Held. Click a material for its lots and PO lines.',
    cards: [
      { label: 'Materials', raw: rows.filter(r => r.status !== 'none').length, tip: 'Materials held or on order.' },
      { label: 'Stock Value', raw: value, fmt: 'inr', tip: 'The stock sheet\'s own Value column, summed.' },
      { label: 'Value Still to Come', raw: toCome, fmt: 'inr', tip: 'Still to arrive on open PO lines at the PO rate in INR, before tax. Each line counted once.' },
      { filter: 'lownoorder', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Low, Nothing on Order', raw: counts.lownoorder || 0, tip: 'Low on stock (under 15 days left, at the minimum stock level, or out while still in use) with nothing on order.' },
      { filter: 'orderonly', cls: 'partial', label: 'On Order, None in Stock', raw: counts.orderonly || 0, tip: 'On order with nothing of it in the store - a new material, one bought again after running out, or one the RM stock sheet does not track (conveyor fabric and belting, mostly at Vapi).' },
      { filter: 'overdue', cls: 'critical', label: 'Orders Overdue', raw: overdue, tip: 'Materials with an open PO line past its delivery date.' },
    ],
    statusOptions: psStatusChoices(PS_POSITION, counts, [{ value: '', label: 'All held or on order' }], [{ value: 'overdue', label: 'Order overdue', count: overdue }]),
    defaultHidden: r => r.status === 'none',
    charts: [
      psCategoryBars('psCombCatChart', 'Stock and Orders by Category', 'Value in the store beside value still to come. Click a bar to filter.', rows, [
        { label: 'In stock', color: '#2563eb', valueOf: r => r.m.value || 0 },
        { label: 'Still to come', color: '#d97706', valueOf: r => (r.figures ? r.figures.toComeValue : 0) },
      ], false),
      psStatusDoughnut('psCombStatusChart', 'Where Materials Stand', 'Held, on order, both or neither. Click a slice or a label to filter the list.', PS_POSITION, counts, 'MATERIALS'),
    ],
  };
}

function psCombinedColumns() {
  const split = psPlantColumnKeys().length > 0;
  return [
    { label: 'Material', sort: 'material', filter: 'search', w: [170, 1.6], cell: r => psMaterialLinkHtml(r.m) + (r.m.orderOnly ? '<span class="mat-tag">On order only</span>' : '') },
    { label: 'Category', sort: 'category', filter: 'category', w: [110, 1], cell: r => categoryCellHtml(r.m) },
    { label: 'Sub Category', sort: 'subCategory', filter: 'subCategory', w: [110, 0.95], cell: r => subCategoryCellHtml(r.m) },
    { label: 'In Stock', sort: 'stock', w: [120, 1.05], tip: 'Quantity on hand' + (split ? ', split by plant' : '') + '.', cell: r => stockCellHtml(r.m) },
    { label: 'Stock Value', sort: 'value', w: [105, 0.95], tip: 'The stock sheet\'s own Value column.', cell: r => stockValueCellHtml(r.m) },
    { label: 'Days Left', sort: 'daysLeft', w: [95, 0.8], tip: 'Stock divided by daily use.', cell: r => (r.m.orderOnly ? '<span class="mat-cell-none">-</span>' : psDaysLeftCellHtml(r.m)) },
    { label: 'Still to Come', sort: 'toComeValue', w: [125, 1.1], tip: 'Still to arrive on open PO lines, its value and how many POs' + (split ? ', split by plant' : '') + '.', cell: r => psToComeCellHtml(r, split) },
    { label: 'Next Due', sort: 'nextDue', w: [105, 0.95], cell: psNextDueCellHtml },
    { label: 'Status', sort: 'status', filter: 'status', w: [120, 1], cell: r => psPill(PS_POSITION[r.status]) + (r.overdue ? '<div><span class="mat-tag ps-tag-late">Order overdue</span></div>' : '') },
  ];
}

// ── Cells shared by the tabs ──
function psDailyUseCellHtml(m) {
  if (m.orderOnly) return '<span class="mat-cell-none">-</span>';
  const c = m.consumption;
  if (!c || c.confidence === 'none') return '<span class="mat-cell-none">Not enough history</span>';
  if (!c.avgDaily) return '<span class="mat-cell-none">None lately</span>';
  return '<div class="mat-cell-main">' + escapeHtml(psQty(c.avgDaily, psLotsUnit(m.lots), 1)) + '</div><div class="mat-cell-sub">a day</div>';
}

function psDaysLeftCellHtml(m) {
  const cell = daysLeftCellHtml(m);
  const d = psDaysLeft(m);
  if (d == null || d <= 0) return cell;
  return cell + '<div class="mat-cell-sub">runs out ' + escapeHtml(formatDateIN(psIsoInDays(d))) + '</div>';
}

// ── The list region: heading, sort bar, top-5 cards or the full table ──
function psFilteredRows(ctx) {
  const f = PS_STATE[ctx.view];
  const q = (f.search || '').trim().toLowerCase();
  return ctx.rows.filter(r => {
    if (f.status === 'everything') { /* nothing hidden */ }
    else if (f.status === 'partial') { if (!(r.figures && r.figures.partial)) return false; }
    else if (f.status === 'overdue' && ctx.view === 'combined') { if (!r.overdue) return false; }
    else if (f.status) { if (r.status !== f.status) return false; }
    else if (ctx.defaultHidden && ctx.defaultHidden(r)) return false;
    if (q && !String(r.m.description || '').toLowerCase().includes(q)) return false;
    return true;
  });
}

// Column track widths: minmax(<min>px, <n>fr) per column, the same kind of
// template the Raw Material list uses, built here because the plant columns
// vary. The table's <col>s get the same shares as percentages.
function psGridTemplate(cols) {
  return cols.map(c => 'minmax(' + c.w[0] + 'px, ' + c.w[1] + 'fr)').join(' ');
}
function psMinWidth(cols) {
  return cols.reduce((s, c) => s + c.w[0], 0) + 12 * (cols.length - 1) + 30;
}

function psFilterCellHtml(c, ctx) {
  const f = PS_STATE[ctx.view];
  if (c.filter === 'search') return '<input type="text" class="col-filter-input" data-psf="search" placeholder="Search..." value="' + escapeHtml(f.search) + '">';
  if (c.filter === 'category') return '<select class="col-filter-input" data-psf="category"><option value="">All categories</option>' + psOptionsHtml(ctx.catOptions, f.category) + '</select>';
  if (c.filter === 'subCategory') return '<select class="col-filter-input" data-psf="subCategory"><option value="">All sub-categories</option>' + psOptionsHtml(ctx.subOptions, f.subCategory) + '</select>';
  if (c.filter === 'status') return '<select class="col-filter-input" data-psf="status">' + psStatusOptionsHtml(ctx.statusOptions.map(o => ({ value: o.value, label: o.label })), f.status) + '</select>';
  return '';
}

function plantListRegionHtml() {
  const ctx = PS_LIST_CTX;
  const f = PS_STATE[ctx.view];
  const cols = PS_VIEWS[ctx.view].columns();
  ctx.cols = cols;
  const sorter = psSorter(ctx.view);
  const sorted = sorter.sortRows(psFilteredRows(ctx));
  const pageSize = LIST_PAGE_SIZE;
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const page = Math.min(Math.max(1, f.page), totalPages);
  f.page = page;
  ctx.totalPages = totalPages;
  const shown = f.showAll ? sorted.slice((page - 1) * pageSize, page * pageSize) : sorted.slice(0, 5);
  const headerCell = (c, tag) => '<' + tag + (c.tip ? ' title="' + escapeHtml(c.tip) + '"' : '') + '>' + sorter.headerHtml(c.label, c.sort || null) + '</' + tag + '>';
  const filterCells = cols.map(c => psFilterCellHtml(c, ctx));
  const cellsOf = r => cols.map(c => c.cell(r));
  let list;
  if (f.showAll) {
    const frTotal = cols.reduce((s, c) => s + c.w[1], 0);
    const pageButtons = totalPages <= 10
      ? Array.from({ length: totalPages }, (_, i) => i + 1).map(p =>
        '<button type="button" class="page-btn page-num' + (p === page ? ' active' : '') + '" data-ps-page="' + p + '">' + p + '</button>').join('')
      : '<span class="page-info">Page ' + page + ' of ' + totalPages + '</span>';
    list = '<div class="table-wrap"><table class="fixed-table mat-table ps-table" data-ps-minw="' + psMinWidth(cols) + '"><colgroup>' +
        cols.map(c => '<col data-ps-colw="' + (c.w[1] / frTotal * 100).toFixed(2) + '">').join('') + '</colgroup>' +
      '<thead><tr>' + cols.map(c => headerCell(c, 'th')).join('') + '</tr>' +
        '<tr class="col-filter-row">' + filterCells.map(c => '<th>' + c + '</th>').join('') + '</tr></thead>' +
      '<tbody>' + (shown.length
        ? shown.map(r => '<tr>' + cellsOf(r).map(c => '<td>' + c + '</td>').join('') + '</tr>').join('')
        : '<tr><td colspan="' + cols.length + '" class="ps-empty">Nothing matches these filters.</td></tr>') +
      '</tbody></table></div>' +
      (totalPages > 1
        ? '<div class="pagination-row">' +
            '<button type="button" id="psPrevPage" class="page-btn"' + (page <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' + pageButtons +
            '<button type="button" id="psNextPage" class="page-btn"' + (page >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
            jumpToPageHtml('ps', totalPages) +
          '</div>'
        : '');
  } else {
    // The top-5 preview: card rows on one grid, like every other list's.
    list = '<div class="mat-grid-scroll"><div class="list-header-row ps-grid">' + cols.map(c => headerCell(c, 'div')).join('') + '</div>' +
      '<div class="list-header-row ps-grid col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
      '<div class="top5-list">' + (shown.length
        ? shown.map(r => '<div class="top5-row ps-grid">' + cellsOf(r).map(c => '<div>' + c + '</div>').join('') + '</div>').join('')
        : '<div class="top5-row ps-empty">Nothing matches these filters.</div>') + '</div></div>';
  }
  const noun = ctx.view === 'onorder' ? 'Materials on order' : 'Materials';
  return '<div class="list-toggle-row"><div class="section-title m-0">' + noun + ' - showing ' + shown.length + ' of ' + sorted.length + '</div>' +
      (sorted.length > 5 ? '<button type="button" class="view-all-btn" id="psToggleAll">' + (f.showAll ? 'Show top 5' : 'View all ' + sorted.length + ' materials') + '</button>' : '') +
    '</div>' +
    sorter.barHtml() + list;
}

function renderPlantListRegion() {
  const region = document.getElementById('psListRegion');
  if (!region || !PS_LIST_CTX) return;
  preserveFocus(region, () => { region.innerHTML = plantListRegionHtml(); });
  applyDynamicStyles(region);
  wirePlantListRegion(region);
}

function wirePlantListRegion(region) {
  const ctx = PS_LIST_CTX;
  const f = PS_STATE[ctx.view];
  const totalPages = ctx.totalPages || 1;
  // Track widths (CSP allows styles set from JS, never a style="" in markup).
  const tpl = psGridTemplate(ctx.cols);
  const minW = psMinWidth(ctx.cols) + 'px';
  region.querySelectorAll('.ps-grid').forEach(e => { e.style.gridTemplateColumns = tpl; e.style.minWidth = minW; });
  region.querySelectorAll('.mat-grid-scroll > .top5-list').forEach(e => { e.style.minWidth = minW; });
  region.querySelectorAll('[data-ps-minw]').forEach(t => { t.style.minWidth = t.dataset.psMinw + 'px'; });
  region.querySelectorAll('[data-ps-colw]').forEach(c => { c.style.width = c.dataset.psColw + '%'; });

  const goto = n => { f.page = n; renderPlantListRegion(); };
  region.querySelectorAll('[data-ps-page]').forEach(b => { b.onclick = () => goto(Number(b.dataset.psPage)); });
  const prev = document.getElementById('psPrevPage');
  if (prev) prev.onclick = () => goto(Math.max(1, f.page - 1));
  const next = document.getElementById('psNextPage');
  if (next) next.onclick = () => goto(Math.min(totalPages, f.page + 1));
  wireJumpToPage('ps', totalPages, goto);
  const toggle = document.getElementById('psToggleAll');
  if (toggle) toggle.onclick = () => { f.showAll = !f.showAll; f.page = 1; renderPlantListRegion(); };
  psSorter(ctx.view).wire(region);
  region.querySelectorAll('[data-ps-material]').forEach(b => { b.onclick = () => openPlantMaterialPanel(b.dataset.psMaterial); });

  // Header filter row: the search box re-renders this region only,
  // debounced; Category / Sub Category / Status are the page's global
  // filters, so they re-render the whole view, as on Raw Material Analysis.
  region.querySelectorAll('[data-psf]').forEach(inp => {
    const key = inp.dataset.psf;
    if (key === 'search') {
      const later = debounceRender(() => { f.page = 1; renderPlantListRegion(); });
      inp.addEventListener('input', () => { f.search = inp.value; later(); });
      return;
    }
    inp.addEventListener('change', () => {
      if (key === 'category') { f.category = inp.value || null; f.subCategory = null; }
      else if (key === 'subCategory') f.subCategory = inp.value || null;
      else f.status = inp.value || null;
      f.page = 1;
      renderPlantView();
    });
  });
}

// The KPI cards and the "Filter by" bar.
function wirePlantViewChrome(el) {
  const f = PS_STATE[psView()];
  const rerender = () => { f.page = 1; renderPlantView(); };
  el.querySelectorAll('[data-ps-kpi]').forEach(card => {
    const act = () => {
      const key = card.dataset.psKpi || null;
      if (!key) { f.status = null; rerender(); return; }
      psSetStatus(key);
    };
    card.onclick = act;
    card.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(); } };
  });
  const cat = document.getElementById('psCatSelect');
  if (cat) cat.onchange = () => { f.category = cat.value || null; f.subCategory = null; rerender(); };
  const sub = document.getElementById('psSubCatSelect');
  if (sub) sub.onchange = () => { f.subCategory = sub.value || null; rerender(); };
  const status = document.getElementById('psStatusSelect');
  if (status) status.onchange = () => { f.status = status.value || null; rerender(); };
  const clear = document.getElementById('psClearFilters');
  if (clear) clear.onclick = () => { f.category = null; f.subCategory = null; f.status = null; rerender(); };
}

// ── Material panel (the plant tabs' material detail) ──
// The same modal shell and tab strip as Raw Material Analysis's material
// modal - Overview, Stock by Plant, Open Orders - without its match
// confidence, flags, corrections and price trend, which stay with admins
// (openMaterialLink() sends an admin to that modal instead).
async function openPlantMaterialPanel(norm) {
  const myModalRequestId = ++modalRequestId;
  destroyModalCharts();
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  openModalA11y(backdrop);
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };
  body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="load-banner"><div class="spinner"></div><div>Loading material&hellip;</div></div>';
  try {
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS, { stockTabs: true }), ensurePOsLoaded(PLANT_KEYS, { stockTabs: true }), ensureImportPOsLoaded()]);
  } catch (e) {
    console.error('openPlantMaterialPanel failed:', e);
    if (myModalRequestId !== modalRequestId) return;
    body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="noaccess">Couldn\'t load this material right now. Please refresh, or contact IT if this keeps happening.</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return;

  const all = materialScope(PLANT_KEYS);
  const m = all.find(x => normalizeMaterial(x.description) === norm);
  if (!m) {
    body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="noaccess">This material is not in stock or on an open order at any plant you can see.</div>';
    return;
  }
  const entry = computeMaterialPoLinkage([m], PLANT_KEYS, all)[0];
  const open = entry.openLinks.slice().sort((a, b) => (a.item.deliveryDate || '9999').localeCompare(b.item.deliveryDate || '9999'));
  const figures = open.length ? psOrderFigures(open) : null;
  const plantOf = l => l._plantKey || PLANT_KEYS[0];
  const lots = (m.lots || []).slice().sort((a, b) =>
    PLANT_KEYS.indexOf(plantOf(a)) - PLANT_KEYS.indexOf(plantOf(b))
    || ((b.qty || 0) > 0) - ((a.qty || 0) > 0)
    || (b.receivedDate || '').localeCompare(a.receivedDate || ''));
  const daysLeft = psDaysLeft(m);
  const dailyUse = psDailyUse(m);
  const plantCount = new Set(lots.map(plantOf)).size;

  const vendorSeen = new Set();
  const vendors = [];
  lots.map(l => l.vendor).concat(open.map(l => l.po.vendorName)).forEach(v => {
    const n = v ? normalizeVendor(v) : '';
    if (n && !vendorSeen.has(n)) { vendorSeen.add(n); vendors.push(v); }
  });

  const byPlant = {};
  lots.forEach(l => { (byPlant[plantOf(l)] = byPlant[plantOf(l)] || []).push(l); });
  const plantLines = Object.keys(byPlant).map(k => '<div class="line">' + escapeHtml(psShortPlant(k)) + ': ' + escapeHtml(lotsQtyText(byPlant[k])) + '</div>').join('');
  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>In the store</h4>' +
        (lots.length ? plantLines : '<div class="line">None at any plant you can see</div>') +
        '<div class="line">Stock value: ' + formatInr(m.value || 0) + '</div>' +
        '<div class="line">Daily use: ' + escapeHtml(dailyUse != null ? psQty(dailyUse, psLotsUnit(lots), 1) : 'not known') + '</div>' +
        '<div class="line">Days left: ' + escapeHtml(daysLeft != null ? Math.round(daysLeft) + ' (runs out ' + formatDateIN(psIsoInDays(daysLeft)) + ')' : 'not known') + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>On order</h4>' +
        (figures
          ? '<div class="line">Still to come: ' + escapeHtml(psQtyText(figures.toCome) || '-') + '</div>' +
            '<div class="line">Value to come: ' + formatInr(figures.toComeValue) + '</div>' +
            '<div class="line">Open POs: ' + figures.poCount + '</div>'
          : '<div class="line">Nothing on order</div>') +
      '</div>' +
      '<div class="field-block full-width"><h4>Vendors (stock lots and open orders)</h4>' +
        (vendors.length ? vendors.map(v => '<span class="vendor-pill">' + escapeHtml(v) + '</span>').join('') : '<div class="line">Not available</div>') +
      '</div>' +
    '</div>';

  const lotRows = lots.map(l => {
    const usedUp = !((l.qty || 0) > 0);
    return '<tr' + (usedUp ? ' class="lot-used-up" title="Used up - still listed in the stock sheet"' : '') + '>' +
      '<td>' + escapeHtml(PLANTS[plantOf(l)].label) + (l.locationTag ? '<div class="fs-11 text-slate-soft nowrap">Location: ' + escapeHtml(l.locationTag) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(l.vendor || '-') + '</td>' +
      '<td class="nowrap">' + escapeHtml(l.receivedDate ? formatDateIN(l.receivedDate) : '-') + '</td>' +
      '<td>' + escapeHtml(psQty(l.qty, l.uom || '')) + (usedUp ? '<div class="fs-11 text-slate-soft">used up</div>' : '') + '</td>' +
      '<td>' + (l.rate != null ? formatInr(l.rate) : '-') + '</td>' +
      '<td>' + (l.value != null ? formatInr(l.value) : '-') + '</td></tr>';
  }).join('');
  const stockHtml = lots.length
    ? '<div class="table-wrap"><table><thead><tr><th>Plant</th><th>Vendor</th><th>Received</th><th>Qty</th><th>Rate</th><th>Value</th></tr></thead><tbody>' + lotRows +
      '<tr class="ps-total-row"><td>Total</td><td></td><td></td><td>' + escapeHtml(lotsQtyText(lots)) + '</td><td></td><td>' + formatInr(m.value || 0) + '</td></tr></tbody></table></div>'
    : '<div class="no-data-note">No stock lot at any plant you can see.</div>';

  const orderRows = open.map(l => {
    const dueIn = psDaysFromToday(l.item.deliveryDate);
    const uom = l.item.uom || '';
    return '<tr>' +
      '<td><button type="button" class="row-link ps-link" data-ps-po="' + escapeHtml(l.plantKey + '::' + l.po.poNumber) + '" data-ps-kind="' + (l.po.isImport ? 'import' : 'domestic') + '">' + escapeHtml(l.po.poNumber) + '</button>' +
        (l.po.isImport ? ' <span class="mat-tag">Import</span>' : '') + '</td>' +
      '<td>' + escapeHtml(l.plantLabel) + '</td>' +
      '<td>' + escapeHtml(l.po.vendorName || '-') + '</td>' +
      '<td class="nowrap">' + escapeHtml(l.item.deliveryDate ? formatDateIN(l.item.deliveryDate) : 'Not given') +
        (dueIn != null ? '<div class="fs-11 ' + (dueIn < 0 ? 'text-red' : 'text-slate-soft') + '">' + escapeHtml(psDueText(dueIn)) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(psQty(l.item.qty, uom)) + '</td>' +
      '<td>' + escapeHtml(psQty(openQtyOfLine(l.item), uom)) + (lineItemArrived(l.item) ? '<div class="fs-11 text-slate-soft">part received</div>' : '') + '</td>' +
      '<td>' + formatInr(openValueOfLine(l.item)) + '</td></tr>';
  }).join('');
  const ordersHtml = open.length
    ? '<div class="no-data-note mb-8">Tied to this material by name, and by vendor where the stock sheet names one - a differently worded order may be listed under another material.</div>' +
      '<div class="table-wrap"><table><thead><tr><th>PO</th><th>Plant</th><th>Vendor</th><th>Due</th><th>Ordered</th><th>Still to come</th><th>Value to come</th></tr></thead><tbody>' + orderRows + '</tbody></table></div>'
    : '<div class="no-data-note">Nothing on order.</div>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(m.description || m.materialCode || '-') + '</h2>' +
      '<div class="modal-meta">' + escapeHtml(categoryLabel(m.category || 'Uncategorized')) + (psCategory(m.subCategory) ? ' &middot; ' + escapeHtml(m.subCategory) : '') +
      ' &middot; ' + lots.length + ' stock lot' + (lots.length === 1 ? '' : 's') + (lots.length ? ' at ' + plantCount + ' plant' + (plantCount === 1 ? '' : 's') : '') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="modal-tabs" role="tablist">' +
      '<div class="modal-tab active" data-tab="overview" tabindex="0" role="tab" aria-selected="true">Overview</div>' +
      '<div class="modal-tab" data-tab="stock" tabindex="0" role="tab" aria-selected="false">Stock by Plant (' + lots.length + ')</div>' +
      '<div class="modal-tab" data-tab="orders" tabindex="0" role="tab" aria-selected="false">Open Orders (' + open.length + ')</div>' +
    '</div>' +
    '<div class="modal-tab-panel" data-panel="overview">' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" data-panel="stock" hidden>' + stockHtml + '</div>' +
    '<div class="modal-tab-panel" data-panel="orders" hidden>' + ordersHtml + '</div>';
  body.querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    body.querySelectorAll('[data-tab]').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    body.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
  });
  body.querySelectorAll('[data-ps-po]').forEach(b => {
    b.onclick = () => (b.dataset.psKind === 'import' ? openImportPoModal(b.dataset.psPo) : openPoModal(b.dataset.psPo));
  });
}

// Where a material link goes (the PO modals' material names, Search PO's
// "?material=" deep link): Raw Material Analysis's modal for an admin, the
// plant material panel for everyone else. `key` is that modal's own
// "<plant>::<lotId>" or "order::<normalized name>" form.
function openMaterialLink(key) {
  if (isAdminUser()) return openMaterialModal(key);
  const sep = key.indexOf('::');
  const head = key.slice(0, sep);
  const rest = key.slice(sep + 2);
  if (head === 'order') return openPlantMaterialPanel(rest);
  const lot = (MATERIALS_BY_PLANT[head] || []).find(l => String(l.lotId) === rest);
  return openPlantMaterialPanel(normalizeMaterial(lot ? lot.description : ''));
}
