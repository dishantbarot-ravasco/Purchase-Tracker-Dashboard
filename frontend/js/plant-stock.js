// ── Plant stock tabs: Inventory, On Order, Stock Planner (2026-09-29) ────
// Project owner: three raw-material tabs for plant staff - one for what is
// in the store, one for what is on order, one that puts the two together -
// with Raw Material Analysis kept for admins. main.js shows these tabs to
// every role and Raw Material Analysis to admins only (isAdminUser()).
//
//   Inventory     - one row per material in the store: how much, its value,
//                   how fast it is used, days left, when it last came in.
//   On Order      - one row per open PO line (domestic and import): what is
//                   still to come, from whom, and when it is due.
//   Stock Planner - one row per material, stock and orders together: what
//                   to reorder now, what is late, what is on the way.
//
// SAME FIGURES AS RAW MATERIAL ANALYSIS, NOT A SECOND COPY. Every number here
// comes from the helpers that tab already uses - materialScope() for the
// materials (one row per name, summed across vendor lots), openQtyOfLine() /
// openValueOfLine() for what is still to come on a line, isOpenPoLine() for
// whether it is open at all, isMaterialLowStock() for low stock and the
// consumption ledger's Days Left. So the two views can never disagree about a
// figure; they only differ in what they show. What these tabs leave out on
// purpose is the reconciliation layer - match confidence, mismatch flags,
// corrections - which is admin work.
//
// THE PLANNER COMPARES DATES, NOT QUANTITIES. "Will the order arrive before
// the stock runs out?" is answered as run-out date (today + Days Left)
// against the next open line's due date. Adding stock to what is on order
// would need both in one unit, and Achhad's stock sheet records no unit at
// all - a sum there would be a guess, and this app never guesses a unit.
//
// A material's link to its PO lines is the same best-effort description (and
// vendor) link Raw Material Analysis uses - see materials.js's
// buildLineLinks(). The On Order tab does not depend on it: it lists the PO
// lines themselves.

const PLANT_STOCK_TABS = [
  { key: 'inventory', label: 'Inventory' },
  { key: 'onorder', label: 'On Order' },
  { key: 'planner', label: 'Stock Planner' },
];
const PLANT_STOCK_VIEW_KEYS = PLANT_STOCK_TABS.map(t => t.key);

function isAdminUser() {
  return !!(CURRENT_USER && CURRENT_USER.role === 'admin');
}

// Filters per tab. `status` is the KPI card / status select value, `showAll`
// the "include out of stock / nothing held" box, `kind` On Order's
// Domestic/Import select.
function psDefaultState() {
  const base = () => ({ status: null, category: null, search: '', showAll: false, page: 1 });
  return { inventory: base(), onorder: Object.assign(base(), { kind: '' }), planner: base() };
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
  if (view === 'planner') return PLAN_SORT;
  return INV_SORT;
}

// ── Small helpers ──
function psToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

// Whole days from today to an ISO date: negative when it has passed, null
// when there is no date.
function psDaysFromToday(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
  if (isNaN(d.getTime())) return null;
  return Math.round((d - psToday()) / 86400000);
}

// Today plus `days`, as a local ISO date.
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

// The one unit a material's lots share, or '' when they name none or differ.
function psMaterialUnit(m) {
  const units = new Set((m.lots || []).map(l => String(l.uom || '').trim().toUpperCase()).filter(Boolean));
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

// The open line due soonest, from a material's linkage entry. Undated lines
// never win; null when no open line has a date.
function psNextDelivery(entry) {
  let best = null;
  (entry ? entry.openLinks : []).forEach(l => {
    const due = l.item.deliveryDate;
    if (due && (!best || due < best.due)) best = { due: due, link: l };
  });
  return best;
}

function psPill(def) {
  return '<span class="status-pill ' + def.pill + '">' + escapeHtml(def.label) + '</span>';
}

// A clickable name. A real <button> so it is reachable by keyboard; the
// ps-link class strips the button chrome. data-* only - CSP blocks inline
// handlers, see wirePlantListRegion().
function psMaterialLinkHtml(m) {
  const norm = normalizeMaterial(m.description);
  return '<button type="button" class="row-link ps-link mat-name" data-ps-material="' + escapeHtml(norm) + '">' + escapeHtml(m.description || m.materialCode || '-') + '</button>';
}

// ── What each tab reads ──
// Every material row for the plants in view, and each one's PO linkage -
// the same two calls Raw Material Analysis makes (materials.js's
// materialScope() / computeMaterialPoLinkage()), over the unfiltered scope.
function psBuildScope() {
  const keys = selectedPlantKeys();
  const all = materialScope(keys);
  const linkage = computeMaterialPoLinkage(all, keys, all);
  const entryByNorm = new Map(linkage.map(l => [normalizeMaterial(l.material.description), l]));
  return { keys, all, entryByNorm };
}

// ── Inventory ──
// Ranked most urgent first: running out, then a sheet error, then empty.
const INV_STATUS = {
  low: { rank: 0, label: 'Low stock', pill: 'status-overdue' },
  negative: { rank: 1, label: 'Below zero', pill: 'status-overdue' },
  out: { rank: 2, label: 'Out of stock', pill: 'status-pending' },
  ok: { rank: 3, label: 'In stock', pill: 'status-received' },
  idle: { rank: 4, label: 'Not used lately', pill: 'status-unknown' },
};

function psInventoryStatus(m) {
  if ((m.qty || 0) < 0) return 'negative';
  if (!((m.qty || 0) > 0)) return 'out';
  if (isMaterialLowStock(m)) return 'low';
  // Watched and nothing issued in the window - stock sitting idle. Without
  // enough history there is nothing to say either way, so it reads In stock.
  const c = m.consumption;
  if (c && c.confidence !== 'none' && !c.avgDaily) return 'idle';
  return 'ok';
}

function psInventoryRows(scope) {
  return scope.all.filter(m => !m.orderOnly).map(m => {
    const status = psInventoryStatus(m);
    return {
      m: m,
      status: status,
      rank: INV_STATUS[status].rank,
      category: psCategory(m.category),
      subCategory: psCategory(m.subCategory),
      dailyUse: psDailyUse(m),
      daysLeft: psDaysLeft(m),
      lastReceived: psLastReceived(m),
    };
  });
}

// ── On Order ──
const ORD_STATUS = {
  overdue: { rank: 0, label: 'Overdue', pill: 'status-overdue' },
  soon: { rank: 1, label: 'Due this week', pill: 'status-pending' },
  onorder: { rank: 2, label: 'On order', pill: 'status-partial' },
  nodate: { rank: 3, label: 'No due date', pill: 'status-unknown' },
};
const ORD_DUE_SOON_DAYS = 7;

// One row per open PO line, domestic and import (materials.js's
// materialOrders()), judged open by the same isOpenPoLine() Raw Material
// Analysis uses. Due status is the LINE's own delivery date: a PO's status
// takes its earliest line, which would call a later line late.
function psOrderRows(keys) {
  const rows = [];
  keys.forEach(key => materialOrders(key).forEach(po => (po.items || []).forEach((item, idx) => {
    if (!isOpenPoLine(po, item)) return;
    const due = item.deliveryDate || null;
    const dueIn = psDaysFromToday(due);
    const status = dueIn == null ? 'nodate' : dueIn < 0 ? 'overdue' : dueIn <= ORD_DUE_SOON_DAYS ? 'soon' : 'onorder';
    const arrived = lineItemArrived(item);
    const rec = item.received;
    rows.push({
      plantKey: key,
      plantLabel: PLANTS[key].label,
      po: po,
      item: item,
      lineIndex: idx,
      due: due,
      dueIn: dueIn,
      status: status,
      rank: ORD_STATUS[status].rank,
      category: psCategory(item.category),
      // Something has arrived against the line and more is still to come.
      partial: arrived,
      // null when it arrived in a unit the server could not convert.
      receivedQty: arrived ? (rec && rec.comparable && rec.qty != null ? rec.qty : null) : 0,
      toComeQty: openQtyOfLine(item),
      toComeValue: openValueOfLine(item),
    });
  })));
  return rows;
}

// ── Stock Planner ──
const PLAN_STATUS = {
  reorder: { rank: 0, label: 'Reorder now', pill: 'status-overdue' },
  late: { rank: 1, label: 'Delivery late', pill: 'status-overdue' },
  coming: { rank: 2, label: 'On the way', pill: 'status-partial' },
  firstorder: { rank: 3, label: 'On order, none in stock', pill: 'status-pending' },
  check: { rank: 4, label: 'Check stock sheet', pill: 'status-pending' },
  nodata: { rank: 5, label: 'No usage data yet', pill: 'status-unknown' },
  ok: { rank: 6, label: 'Enough stock', pill: 'status-received' },
  idle: { rank: 7, label: 'Not used lately', pill: 'status-unknown' },
  dormant: { rank: 8, label: 'Nothing held', pill: 'status-unknown' },
};

// What to do about a material, with the reason shown under the pill.
// "Low" is Raw Material Analysis's Low Stock (under 15 days of cover, or at
// Achhad's minimum stock level), plus a material in use that has run out.
// A low material with an order is late when that order is past due, or due
// after the day the stock is expected to run out.
function psPlanDecision(m, entry, next, daysLeft) {
  const hasOrder = !!(entry && entry.openLinks.length);
  const nextIn = next ? psDaysFromToday(next.due) : null;
  const nextLate = nextIn != null && nextIn < 0;
  const nextText = next ? 'Next delivery ' + formatDateIN(next.due) : 'On order, no due date';
  // On order with no stock lot anywhere in view. Worded as a fact, not a
  // promise: much of it (conveyor fabric, belting) is never stocked in RM.
  if (m.orderOnly) {
    return nextLate
      ? { status: 'late', why: 'None in stock; order due ' + formatDateIN(next.due) + ', ' + psDueText(nextIn) }
      : { status: 'firstorder', why: 'None in stock. ' + nextText };
  }
  if ((m.qty || 0) < 0) return { status: 'check', why: 'The stock sheet shows less than zero' };
  const c = m.consumption;
  const measured = !!(c && c.confidence !== 'none');
  const inUse = measured && !!c.avgDaily;
  const atMsl = (m.lots || []).some(l => l.daysToMsl === 0);
  const empty = !((m.qty || 0) > 0);
  const low = isMaterialLowStock(m) || (empty && inUse);
  if (low) {
    // Say which rule made it low - "in use" only when daily use says so.
    const lowWhy = empty && inUse ? 'Out of stock and in use'
      : atMsl ? (empty ? 'Out of stock, below its minimum stock level' : 'At or below the minimum stock level')
        : 'Under 15 days of stock left';
    if (!hasOrder) return { status: 'reorder', why: lowWhy + ', nothing on order' };
    if (nextLate) return { status: 'late', why: 'Order due ' + formatDateIN(next.due) + ', ' + psDueText(nextIn) };
    if (nextIn != null && daysLeft != null && nextIn > daysLeft) {
      return { status: 'late', why: 'Order due ' + formatDateIN(next.due) + ', after stock runs out (' + formatDateIN(psIsoInDays(daysLeft)) + ')' };
    }
    return { status: 'coming', why: lowWhy + '. ' + nextText };
  }
  if (!((m.qty || 0) > 0)) {
    if (!hasOrder) return { status: 'dormant', why: 'None in stock, not in use, nothing on order' };
    return nextLate
      ? { status: 'late', why: 'None in stock; order due ' + formatDateIN(next.due) + ', ' + psDueText(nextIn) }
      : { status: 'coming', why: 'None in stock. ' + nextText };
  }
  if (!measured) return { status: 'nodata', why: 'Not enough stock history to work out daily use' };
  if (!c.avgDaily) return { status: 'idle', why: 'Nothing issued lately' };
  return { status: 'ok', why: daysLeft != null ? 'About ' + Math.round(daysLeft) + ' days of stock' : 'Stock on hand' };
}

function psPlannerRows(scope) {
  return scope.all.map(m => {
    const entry = scope.entryByNorm.get(normalizeMaterial(m.description)) || null;
    const next = psNextDelivery(entry);
    const daysLeft = psDaysLeft(m);
    const decision = psPlanDecision(m, entry, next, daysLeft);
    const figures = entry && entry.orderFigures;
    return {
      m: m,
      entry: entry,
      next: next,
      nextDue: next ? next.due : null,
      daysLeft: daysLeft,
      status: decision.status,
      why: decision.why,
      rank: PLAN_STATUS[decision.status].rank,
      category: psCategory(m.category),
      subCategory: psCategory(m.subCategory),
      toComeValue: figures && figures.lineCount ? figures.pendingValue : null,
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
    // Stock and both order books for every tab: the On Order tab needs no
    // stock and Inventory no orders, but the Planner needs both and the three
    // share the caches, so switching tabs never waits a second time.
    const keys = selectedPlantKeys();
    await Promise.all([ensureMaterialsLoaded(keys), ensurePOsLoaded(keys), ensureImportPOsLoaded(), psSorter(view).ensurePresetsLoaded()]);
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

// ── Rendering ──
// Everything a list-region re-render needs, set by renderPlantView(). A
// search keystroke or a sort change rebuilds the list region alone, never
// the KPI row (same split as materials.js's MAT_LIST_CTX).
let PS_LIST_CTX = null;

function renderPlantView() {
  const el = document.getElementById('plantStockContent');
  if (!el) return;
  const view = psView();
  const scope = view === 'onorder' ? null : psBuildScope();
  if (view === 'inventory') renderInventoryView(el, scope);
  else if (view === 'onorder') renderOnOrderView(el);
  else renderPlannerView(el, scope);
}

// A row of KPI cards. A card with `filter` narrows the list to that status;
// one without clears the filter (it counts the whole list).
function psKpiRowHtml(cards, active) {
  return '<div class="kpi-grid mat-kpi-grid ps-kpi-grid">' + cards.map(c => {
    const on = !!c.filter && active === c.filter;
    return '<div class="kpi-card ' + (c.cls || '') + (on ? ' active' : '') + '" data-ps-kpi="' + escapeHtml(c.filter || '') + '" tabindex="0" role="button" aria-pressed="' + on + '">' +
      (c.flag ? flagIconHtml(c.flag) : '') +
      '<div class="mat-kpi-valrow"><span class="val" data-count-target="' + c.raw + '" data-count-fmt="' + (c.fmt || 'int') + '">0</span></div>' +
      '<div class="label">' + escapeHtml(c.label) + infoTooltipHtml(c.tip) + '</div></div>';
  }).join('') + '</div>';
}

// Category select options from the rows in view, most common first.
function psCategoryOptionsHtml(rows, selected) {
  const counts = new Map();
  rows.forEach(r => { const k = r.category || 'Uncategorized'; counts.set(k, (counts.get(k) || 0) + 1); });
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).map(([c, n]) =>
    '<option value="' + escapeHtml(c) + '"' + (selected === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('');
}

function psStatusOptionsHtml(defs, counts, selected, extra) {
  return (extra || []).concat(Object.keys(defs).map(k => ({ key: k, label: defs[k].label })))
    .map(o => '<option value="' + o.key + '"' + (selected === o.key ? ' selected' : '') + '>' + escapeHtml(o.label) + (counts[o.key] != null ? ' (' + counts[o.key] + ')' : '') + '</option>').join('');
}

function psCountBy(rows, field) {
  const counts = {};
  rows.forEach(r => { counts[r[field]] = (counts[r[field]] || 0) + 1; });
  return counts;
}

// The filter bar: search, category, status, an optional extra control and
// the show-all box. The search box sits OUTSIDE the list region, so typing
// never rebuilds the box under the cursor. Clear is always there: typing a
// search re-renders only the list, so a Clear drawn on demand would never
// appear for a search.
function psFilterBarHtml(f, opts) {
  return '<div class="filter-row ps-filter-row">' +
    '<div class="filter-group"><label for="psSearch">Search</label>' +
      '<input type="text" id="psSearch" class="col-filter-input ps-search" placeholder="' + escapeHtml(opts.searchPlaceholder) + '" value="' + escapeHtml(f.search) + '"></div>' +
    '<div class="filter-group"><label for="psCategory">Category</label>' +
      '<select id="psCategory" class="select-w220"><option value="">All categories</option>' + opts.categoryOptions + '</select></div>' +
    '<div class="filter-group"><label for="psStatus">' + escapeHtml(opts.statusLabel) + '</label>' +
      '<select id="psStatus" class="select-w200"><option value="">All</option>' + opts.statusOptions + '</select></div>' +
    (opts.extraHtml || '') +
    (opts.showAllLabel
      ? '<label class="ps-check"><input type="checkbox" id="psShowAll"' + (f.showAll ? ' checked' : '') + '> ' + escapeHtml(opts.showAllLabel) + '</label>'
      : '') +
    '<button type="button" id="psClear">Clear filters</button>' +
  '</div>';
}

function renderInventoryView(el, scope) {
  const f = PS_STATE.inventory;
  const rows = psInventoryRows(scope);
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml(isAllPlants() ? 'No stock synced yet for any plant.' : 'No stock synced yet for ' + escapeHtml(PLANTS[state.plant].label) + '.') + '</div>';
    PS_LIST_CTX = null;
    return;
  }
  const inCategory = f.category ? rows.filter(r => (r.category || 'Uncategorized') === f.category) : rows;
  const counts = psCountBy(inCategory, 'status');
  const held = inCategory.filter(r => (r.m.qty || 0) > 0);
  const value = inCategory.reduce((s, r) => s + (r.m.value || 0), 0);
  const idleValue = inCategory.filter(r => r.status === 'idle').reduce((s, r) => s + (r.m.value || 0), 0);
  const n = v => v.toLocaleString('en-IN');
  const cards = [
    { label: 'Materials in Stock', raw: held.length, tip: 'Materials with a positive quantity on the stock sheet' + (isAllPlants() ? ' at any plant' : '') + '. Every vendor lot of one material counts as one material.' },
    { label: 'Stock Value', raw: value, fmt: 'inr', tip: 'The stock sheet\'s own Value column, summed. A lot the sheet shows below zero subtracts - fix it in the sheet.' },
    { filter: 'low', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Low Stock', raw: counts.low || 0, tip: 'Under 15 days of stock at the recent rate of use, or at or below Achhad\'s minimum stock level. Check the Stock Planner for whether an order is coming.' },
    { filter: 'out', cls: 'partial', label: 'Out of Stock', raw: counts.out || 0, tip: 'Listed on the stock sheet with nothing left. Hidden from the list unless you pick this card or tick "Include out of stock".' },
    { filter: 'idle', label: 'Not Used Lately', raw: counts.idle || 0, tip: 'In stock, but nothing issued in the recent window: ' + formatInr(idleValue) + ' of stock sitting idle.' },
    { filter: 'negative', cls: 'critical', label: 'Below Zero', raw: counts.negative || 0, tip: 'The stock sheet shows less than zero - a data error to correct in the sheet.' },
  ];
  el.innerHTML =
    '<div class="section-title">Inventory: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">What is in the store now, from the RM stock sheet' + (isAllPlants() ? ', summed across all plants you can see' : '') + '. One row per material, however many vendor lots it has. Click a material for its lots and orders.' +
      (isAllPlants() ? ' Days Left here is the plant that runs out first.' : '') + '</div>' +
    psKpiRowHtml(cards, f.status) +
    psFilterBarHtml(f, {
      searchPlaceholder: 'Material name...',
      categoryOptions: psCategoryOptionsHtml(rows, f.category),
      statusLabel: 'Stock status',
      statusOptions: psStatusOptionsHtml(INV_STATUS, counts, f.status),
      showAllLabel: 'Include out of stock',
    }) +
    '<div id="psListRegion"></div>';
  PS_LIST_CTX = { view: 'inventory', rows: inCategory };
  wirePlantViewChrome(el);
  renderPlantListRegion();
}

function renderOnOrderView(el) {
  const f = PS_STATE.onorder;
  const all = psOrderRows(selectedPlantKeys());
  if (!all.length) {
    el.innerHTML = '<div class="section-title">On Order: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
      '<div class="empty-state">' + emptyStateHtml('Nothing is on order right now - every purchase order line has been received.') + '</div>';
    PS_LIST_CTX = null;
    return;
  }
  let scoped = f.category ? all.filter(r => (r.category || 'Uncategorized') === f.category) : all;
  if (f.kind) scoped = scoped.filter(r => (f.kind === 'import') === !!r.po.isImport);
  const counts = psCountBy(scoped, 'status');
  counts.partial = scoped.filter(r => r.partial).length;
  const toCome = scoped.reduce((s, r) => s + (r.toComeValue || 0), 0);
  const overdueValue = scoped.filter(r => r.status === 'overdue').reduce((s, r) => s + (r.toComeValue || 0), 0);
  const poCount = new Set(scoped.map(r => r.plantKey + '::' + r.po.poNumber + (r.po.isImport ? '::i' : ''))).size;
  const cards = [
    { label: 'Open Lines', raw: scoped.length, tip: 'Purchase order lines with something still to arrive, on ' + poCount.toLocaleString('en-IN') + ' orders (domestic and import).' },
    { label: 'Value Still to Come', raw: toCome, fmt: 'inr', tip: 'Quantity still to arrive on each open line, at its PO rate in INR, before tax. Import prices are converted at the PO\'s exchange rate.' },
    { filter: 'overdue', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Overdue', raw: counts.overdue || 0, tip: 'Lines whose delivery date has passed with material still to come: ' + formatInr(overdueValue) + '. Chase the vendor.' },
    { filter: 'soon', cls: 'partial', label: 'Due This Week', raw: counts.soon || 0, tip: 'Lines due today or in the next ' + ORD_DUE_SOON_DAYS + ' days - expect these at the gate.' },
    { filter: 'partial', label: 'Part Received', raw: counts.partial || 0, tip: 'Lines where some of the order has come in and the rest is still to come.' },
    { filter: 'nodate', label: 'No Due Date', raw: counts.nodate || 0, tip: 'Open lines the purchase order gives no delivery date for.' },
  ];
  el.innerHTML =
    '<div class="section-title">On Order: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">Every purchase order line still to arrive' + (isAllPlants() ? ' at the plants you can see' : '') + ', domestic and import, due soonest first. What has arrived comes from the MIR receipts matched to each line. Click a PO number for the order, or a material for its stock.</div>' +
    psKpiRowHtml(cards, f.status) +
    psFilterBarHtml(f, {
      searchPlaceholder: 'PO number, vendor or material...',
      categoryOptions: psCategoryOptionsHtml(all, f.category),
      statusLabel: 'Delivery status',
      statusOptions: psStatusOptionsHtml(ORD_STATUS, counts, f.status, [{ key: 'partial', label: 'Part received' }]),
      extraHtml: '<div class="filter-group"><label for="psKind">Purchase type</label>' +
        '<select id="psKind" class="select-w200"><option value="">Domestic and import</option>' +
        '<option value="domestic"' + (f.kind === 'domestic' ? ' selected' : '') + '>Domestic</option>' +
        '<option value="import"' + (f.kind === 'import' ? ' selected' : '') + '>Import</option></select></div>',
    }) +
    '<div id="psListRegion"></div>';
  PS_LIST_CTX = { view: 'onorder', rows: scoped };
  wirePlantViewChrome(el);
  renderPlantListRegion();
}

function renderPlannerView(el, scope) {
  const f = PS_STATE.planner;
  const rows = psPlannerRows(scope);
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml('No stock or open orders to plan from yet.') + '</div>';
    PS_LIST_CTX = null;
    return;
  }
  const inCategory = f.category ? rows.filter(r => (r.category || 'Uncategorized') === f.category) : rows;
  const counts = psCountBy(inCategory, 'status');
  const cards = [
    { filter: 'reorder', cls: 'critical', flag: KPI_FLAG_COLORS.critical, label: 'Reorder Now', raw: counts.reorder || 0, tip: 'Low on stock (under 15 days left, at the minimum stock level, or out while still in use) with nothing on order.' },
    { filter: 'late', cls: 'critical', label: 'Delivery Late', raw: counts.late || 0, tip: 'Needed, and the order for it is past its due date, or due after the stock is expected to run out.' },
    { filter: 'coming', cls: 'partial', label: 'On the Way', raw: counts.coming || 0, tip: 'Low or out of stock, with an order due before the stock runs out.' },
    { filter: 'firstorder', label: 'On Order, None in Stock', raw: counts.firstorder || 0, tip: 'On order, with no stock lot of it at all: a new material, one bought again after running out, or something the RM stock sheet does not track (conveyor fabric and belting are the bulk of these at Vapi).' },
    { filter: 'ok', label: 'Enough Stock', raw: counts.ok || 0, tip: 'At least 15 days of stock at the recent rate of use.' },
    { filter: 'idle', label: 'Not Used Lately', raw: counts.idle || 0, tip: 'In stock, with nothing issued in the recent window.' },
  ];
  el.innerHTML =
    '<div class="section-title">Stock Planner: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">Stock and open orders together, one row per material, most urgent first. "Runs out" is today plus Days Left at the recent rate of use; an order is late when it is past due or due after that day.' +
      (isAllPlants() ? ' On All Plants, Days Left is the plant that runs out first, so check the plant split under In Stock before ordering - another plant may hold enough.' : '') + '</div>' +
    matchingDisclaimerHtml(
      'Orders are tied to materials by name, the same way Raw Material Analysis does it. Days Left is an estimate.',
      '<p>A PO line counts for the stock material whose name it matches best, and by vendor where the stock sheet names one. A differently worded order can be missed, so check the On Order tab before placing a new order.</p>' +
      '<p>Days Left comes from recent stock-sheet history, not from the sheet itself. The dot beside it shows how much history it rests on.</p>'
    ) +
    psKpiRowHtml(cards, f.status) +
    psFilterBarHtml(f, {
      searchPlaceholder: 'Material name...',
      categoryOptions: psCategoryOptionsHtml(rows, f.category),
      statusLabel: 'Action',
      statusOptions: psStatusOptionsHtml(PLAN_STATUS, counts, f.status),
      showAllLabel: 'Include materials with nothing held',
    }) +
    '<div id="psListRegion"></div>';
  PS_LIST_CTX = { view: 'planner', rows: inCategory };
  wirePlantViewChrome(el);
  renderPlantListRegion();
}

// ── The list region (heading, sort bar, table, pages) ──
function psFilteredRows(ctx) {
  const f = PS_STATE[ctx.view];
  const q = (f.search || '').trim().toLowerCase();
  return ctx.rows.filter(r => {
    if (ctx.view === 'onorder') {
      if (f.status === 'partial') { if (!r.partial) return false; } else if (f.status && r.status !== f.status) return false;
      if (q && ![r.po.poNumber, r.po.vendorName, r.item.description].some(v => String(v || '').toLowerCase().includes(q))) return false;
      return true;
    }
    if (f.status) { if (r.status !== f.status) return false; }
    // Out of stock (Inventory) and nothing held (Planner) stay out of the
    // list unless asked for - the stock sheets keep used-up lots listed.
    else if (!f.showAll && (r.status === 'out' || r.status === 'dormant')) return false;
    if (q && !String(r.m.description || '').toLowerCase().includes(q)) return false;
    return true;
  });
}

const PS_COLUMNS = {
  inventory: [
    { label: 'Material', sort: 'material' },
    { label: 'Category', sort: 'category' },
    { label: 'In Stock', sort: 'stock', tip: 'Quantity on hand on the stock sheet.' },
    { label: 'Stock Value', sort: 'value', tip: 'The stock sheet\'s own Value column, with the latest rate under it.' },
    { label: 'Daily Use', sort: 'dailyUse', tip: 'Average issued per day over the recent window, from the stock sheet\'s own issue figures.' },
    { label: 'Days Left', sort: 'daysLeft', tip: 'Stock divided by daily use. The dot shows how much history it rests on.' },
    { label: 'Last Received', sort: 'received' },
    { label: 'Status', sort: 'status' },
  ],
  onorder: [
    { label: 'PO', sort: 'poNumber' },
    { label: 'Vendor', sort: 'vendor' },
    { label: 'Material', sort: 'material' },
    { label: 'Ordered' },
    { label: 'Received', tip: 'Received so far on this line, from its matched MIR receipts.' },
    { label: 'Still to Come', sort: 'toComeValue', tip: 'Ordered less received, and its value at the PO rate (INR, before tax).' },
    { label: 'Due', sort: 'due' },
    { label: 'Status', sort: 'status' },
  ],
  planner: [
    { label: 'Material', sort: 'material' },
    { label: 'In Stock' },
    { label: 'Daily Use' },
    { label: 'Days Left', sort: 'daysLeft', tip: 'Stock divided by daily use, and the day it is expected to run out.' },
    { label: 'Still to Come', sort: 'toComeValue', tip: 'Still to arrive on this material\'s open PO lines, and its value at the PO rate.' },
    { label: 'Next Delivery', sort: 'nextDelivery', tip: 'The open line due soonest.' },
    { label: 'Action', sort: 'status' },
  ],
};

function psDailyUseCellHtml(r) {
  const m = r.m;
  if (m.orderOnly) return '<span class="mat-cell-none">-</span>';
  const c = m.consumption;
  if (!c || c.confidence === 'none') return '<span class="mat-cell-none">Not enough history</span>';
  if (!c.avgDaily) return '<span class="mat-cell-none">None lately</span>';
  return '<div class="mat-cell-main">' + escapeHtml(psQty(c.avgDaily, psMaterialUnit(m), 1)) + '</div><div class="mat-cell-sub">a day</div>';
}

function psDaysLeftCellHtml(r) {
  const cell = daysLeftCellHtml(r.m);
  if (r.daysLeft == null || r.daysLeft <= 0) return cell;
  return cell + '<div class="mat-cell-sub">runs out ' + escapeHtml(formatDateIN(psIsoInDays(r.daysLeft))) + '</div>';
}

function psInventoryCells(r) {
  const m = r.m;
  const lotCount = (m.lots || []).length;
  const received = r.lastReceived;
  const ago = received != null ? psDaysFromToday(received) : null;
  return [
    psMaterialLinkHtml(m) + '<div class="mat-cell-sub">' + lotCount + (lotCount === 1 ? ' lot' : ' lots') + '</div>',
    '<div class="mat-cell-main mat-cell-text">' + escapeHtml(categoryLabel(r.category || 'Uncategorized')) + '</div>' +
      (r.subCategory ? '<div class="mat-cell-sub">' + escapeHtml(r.subCategory) + '</div>' : ''),
    stockCellHtml(m),
    stockValueCellHtml(m),
    psDailyUseCellHtml(r),
    psDaysLeftCellHtml(r),
    // A date after today is a sheet error (often day and month swapped) -
    // shown as written, and said so, rather than hidden.
    received ? '<div class="mat-cell-main">' + escapeHtml(formatDateIN(received)) + '</div>' +
      (ago == null ? '' : ago > 0 ? '<div class="mat-cell-sub imp-overdue">date is in the future</div>'
        : '<div class="mat-cell-sub">' + (ago === 0 ? 'today' : (-ago).toLocaleString('en-IN') + ' days ago') + '</div>')
      : '<span class="mat-cell-none">No date on the sheet</span>',
    psPill(INV_STATUS[r.status]),
  ];
}

function psOrderCells(r) {
  const po = r.po;
  const item = r.item;
  const uom = item.uom || '';
  const poKey = r.plantKey + '::' + po.poNumber;
  let received;
  if (!r.partial) received = '<span class="mat-cell-none">Nothing yet</span>';
  else if (r.receivedQty == null) received = '<span class="mat-cell-none">Part received (another unit)</span>';
  else received = '<div class="mat-cell-main">' + escapeHtml(psQty(r.receivedQty, uom)) + '</div>';
  const dueSub = r.dueIn != null
    ? '<div class="mat-cell-sub' + (r.dueIn < 0 ? ' imp-overdue' : '') + '">' + escapeHtml(psDueText(r.dueIn)) + '</div>'
    : '';
  return [
    '<button type="button" class="row-link ps-link" data-ps-po="' + escapeHtml(poKey) + '" data-ps-kind="' + (po.isImport ? 'import' : 'domestic') + '">' + escapeHtml(po.poNumber) + '</button>' +
      '<div class="mat-cell-sub">' + (po.createdDate ? 'PO date ' + escapeHtml(formatDateIN(po.createdDate)) : 'No PO date') + '</div>' +
      (isAllPlants() ? '<div class="mat-cell-sub">' + escapeHtml(r.plantLabel) + '</div>' : '') +
      (po.isImport ? '<span class="mat-tag">Import</span>' : ''),
    '<div class="mat-cell-main mat-cell-text">' + escapeHtml(po.vendorName || '-') + '</div>',
    '<button type="button" class="row-link ps-link mat-name" data-ps-material="' + escapeHtml(normalizeMaterial(item.description)) + '">' + escapeHtml(item.description || '-') + '</button>' +
      (r.category ? '<div class="mat-cell-sub">' + escapeHtml(r.category) + '</div>' : ''),
    '<div class="mat-cell-main">' + escapeHtml(psQty(item.qty, uom)) + '</div>',
    received,
    '<div class="mat-cell-main">' + escapeHtml(psQty(r.toComeQty, uom)) + '</div>' +
      '<div class="mat-cell-sub"' + (po.isImport ? ' title="' + escapeHtml(importPriceTitle(item)) + '"' : '') + '>' + formatInr(r.toComeValue || 0) + '</div>',
    r.due ? '<div class="mat-cell-main">' + escapeHtml(formatDateIN(r.due)) + '</div>' + dueSub : '<span class="mat-cell-none">Not given</span>',
    psPill(ORD_STATUS[r.status]) + (r.partial ? '<div><span class="mat-tag">Part received</span></div>' : ''),
  ];
}

function psPlannerCells(r) {
  const m = r.m;
  const figures = r.entry && r.entry.orderFigures;
  const toCome = figures && figures.lineCount
    ? '<div class="mat-cell-main">' + escapeHtml(qtySummaryText(figures.pendingQty) || '-') + '</div>' +
      '<div class="mat-cell-sub">' + formatInr(figures.pendingValue) + ' &middot; ' + figures.poCount + (figures.poCount === 1 ? ' PO' : ' POs') + '</div>'
    : '<span class="mat-cell-none">Nothing on order</span>';
  let next;
  if (r.next) {
    const nextIn = psDaysFromToday(r.next.due);
    next = '<div class="mat-cell-main">' + escapeHtml(formatDateIN(r.next.due)) + '</div>' +
      '<div class="mat-cell-sub' + (nextIn < 0 ? ' imp-overdue' : '') + '">' + escapeHtml(psDueText(nextIn)) + '</div>' +
      '<div class="mat-cell-sub">PO ' + escapeHtml(r.next.link.po.poNumber) + '</div>';
  } else {
    next = '<span class="mat-cell-none">' + (figures && figures.lineCount ? 'No due date' : '-') + '</span>';
  }
  return [
    psMaterialLinkHtml(m) + (m.orderOnly ? '<span class="mat-tag">On order only</span>' : '') +
      '<div class="mat-cell-sub">' + escapeHtml(categoryLabel(r.category || 'Uncategorized')) + '</div>',
    m.orderOnly ? '<span class="mat-cell-none">None yet</span>' : stockCellHtml(m),
    psDailyUseCellHtml(r),
    m.orderOnly ? '<span class="mat-cell-none">-</span>' : psDaysLeftCellHtml(r),
    toCome,
    next,
    psPill(PLAN_STATUS[r.status]) + '<div class="mat-cell-sub ps-why">' + escapeHtml(r.why) + '</div>',
  ];
}

const PS_CELLS = { inventory: psInventoryCells, onorder: psOrderCells, planner: psPlannerCells };
const PS_NOUNS = { inventory: ['material', 'materials'], onorder: ['open line', 'open lines'], planner: ['material', 'materials'] };

function plantListRegionHtml() {
  const ctx = PS_LIST_CTX;
  const view = ctx.view;
  const f = PS_STATE[view];
  const sorter = psSorter(view);
  const sorted = sorter.sortRows(psFilteredRows(ctx));
  const pageSize = LIST_PAGE_SIZE;
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const page = Math.min(Math.max(1, f.page), totalPages);
  f.page = page;
  ctx.totalPages = totalPages;
  const pageRows = sorted.slice((page - 1) * pageSize, page * pageSize);
  const nouns = PS_NOUNS[view];
  const cols = PS_COLUMNS[view];
  const cells = PS_CELLS[view];
  const pageButtons = totalPages <= 10
    ? Array.from({ length: totalPages }, (_, i) => i + 1).map(p =>
      '<button type="button" class="page-btn page-num' + (p === page ? ' active' : '') + '" data-ps-page="' + p + '">' + p + '</button>').join('')
    : '<span class="page-info">Page ' + page + ' of ' + totalPages + '</span>';
  const pagination = totalPages > 1
    ? '<div class="pagination-row">' +
        '<button type="button" id="psPrevPage" class="page-btn"' + (page <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
        pageButtons +
        '<button type="button" id="psNextPage" class="page-btn"' + (page >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
        jumpToPageHtml('ps', totalPages) +
      '</div>'
    : '';
  const body = pageRows.length
    ? pageRows.map(r => '<tr>' + cells(r).map(c => '<td>' + c + '</td>').join('') + '</tr>').join('')
    : '<tr><td colspan="' + cols.length + '" class="ps-empty">Nothing matches these filters.</td></tr>';
  return '<div class="list-toggle-row"><div class="section-title m-0">' +
      (sorted.length === 1 ? '1 ' + nouns[0] : sorted.length.toLocaleString('en-IN') + ' ' + nouns[1]) +
      (totalPages > 1 ? ' - showing ' + ((page - 1) * pageSize + 1) + ' to ' + ((page - 1) * pageSize + pageRows.length) : '') +
    '</div></div>' +
    sorter.barHtml() +
    '<div class="table-wrap"><table class="mat-table ps-table ps-table-' + view + '"><thead><tr>' +
      cols.map(c => '<th' + (c.tip ? ' title="' + escapeHtml(c.tip) + '"' : '') + '>' + sorter.headerHtml(c.label, c.sort || null) + '</th>').join('') +
    '</tr></thead><tbody>' + body + '</tbody></table></div>' + pagination;
}

function renderPlantListRegion() {
  const region = document.getElementById('psListRegion');
  if (!region || !PS_LIST_CTX) return;
  preserveFocus(region, () => { region.innerHTML = plantListRegionHtml(); });
  applyDynamicStyles(region);
  wirePlantListRegion(region);
}

function wirePlantListRegion(region) {
  const f = PS_STATE[PS_LIST_CTX.view];
  const totalPages = PS_LIST_CTX.totalPages || 1;
  const goto = n => { f.page = n; renderPlantListRegion(); };
  region.querySelectorAll('[data-ps-page]').forEach(b => { b.onclick = () => goto(Number(b.dataset.psPage)); });
  const prev = document.getElementById('psPrevPage');
  if (prev) prev.onclick = () => goto(Math.max(1, f.page - 1));
  const next = document.getElementById('psNextPage');
  if (next) next.onclick = () => goto(Math.min(totalPages, f.page + 1));
  wireJumpToPage('ps', totalPages, goto);
  psSorter(PS_LIST_CTX.view).wire(region);
  region.querySelectorAll('[data-ps-material]').forEach(b => { b.onclick = () => openPlantMaterialPanel(b.dataset.psMaterial); });
  region.querySelectorAll('[data-ps-po]').forEach(b => {
    b.onclick = () => (b.dataset.psKind === 'import' ? openImportPoModal(b.dataset.psPo) : openPoModal(b.dataset.psPo));
  });
}

// The KPI row and filter bar. A KPI card, a select or the box re-renders the
// whole view (they change the counts); the search box re-renders the list
// region only, debounced.
function wirePlantViewChrome(el) {
  const view = psView();
  const f = PS_STATE[view];
  applyDynamicStyles(el);
  wireKpiCountUps(el);
  const rerender = () => { f.page = 1; renderPlantView(); };
  el.querySelectorAll('[data-ps-kpi]').forEach(card => {
    const act = () => {
      const key = card.dataset.psKpi || null;
      f.status = key && f.status !== key ? key : null;
      rerender();
      if (f.status) revealFilteredList('psListRegion');
    };
    card.onclick = act;
    card.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(); } };
  });
  const search = document.getElementById('psSearch');
  if (search) {
    const later = debounceRender(() => { f.page = 1; renderPlantListRegion(); });
    search.addEventListener('input', () => { f.search = search.value; later(); });
  }
  const category = document.getElementById('psCategory');
  if (category) category.onchange = () => { f.category = category.value || null; rerender(); };
  const status = document.getElementById('psStatus');
  if (status) status.onchange = () => { f.status = status.value || null; rerender(); };
  const kind = document.getElementById('psKind');
  if (kind) kind.onchange = () => { f.kind = kind.value; rerender(); };
  const showAll = document.getElementById('psShowAll');
  if (showAll) showAll.onchange = () => { f.showAll = showAll.checked; rerender(); };
  const clear = document.getElementById('psClear');
  if (clear) clear.onclick = () => { PS_STATE[view] = psDefaultState()[view]; renderPlantView(); };
}

// ── Material panel (the plant tabs' material detail) ──
// What plant staff see when they open a material: its stock lots at every
// plant they can read, and its open orders. It deliberately leaves out Raw
// Material Analysis's modal (match confidence, flags, corrections, price
// trend) - that stays with admins, who get that modal from the same links
// (openMaterialLink()).
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
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS), ensurePOsLoaded(PLANT_KEYS), ensureImportPOsLoaded()]);
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
  const openLinks = entry.openLinks.slice().sort((a, b) => (a.item.deliveryDate || '9999').localeCompare(b.item.deliveryDate || '9999'));
  const plantOf = l => l._plantKey || PLANT_KEYS[0];
  const lots = (m.lots || []).slice().sort((a, b) =>
    PLANT_KEYS.indexOf(plantOf(a)) - PLANT_KEYS.indexOf(plantOf(b))
    || ((b.qty || 0) > 0) - ((a.qty || 0) > 0)
    || (b.receivedDate || '').localeCompare(a.receivedDate || ''));
  const daysLeft = psDaysLeft(m);
  const figures = entry.orderFigures;

  const glance =
    '<div class="line">In stock: ' + escapeHtml(m.orderOnly ? 'none yet' : lotsQtyText(lots)) + '</div>' +
    '<div class="line">Stock value: ' + formatInr(m.value || 0) + '</div>' +
    '<div class="line">Daily use: ' + escapeHtml(psDailyUse(m) != null ? psQty(psDailyUse(m), psMaterialUnit(m), 1) : 'not known') + '</div>' +
    '<div class="line">Days left: ' + escapeHtml(daysLeft != null ? Math.round(daysLeft) + ' (runs out ' + formatDateIN(psIsoInDays(daysLeft)) + ')' : 'not known') + '</div>' +
    '<div class="line">Still to come: ' + escapeHtml(figures.lineCount ? (qtySummaryText(figures.pendingQty) || '-') + ', ' + formatInr(figures.pendingValue) : 'nothing on order') + '</div>';

  const lotRows = lots.length
    ? lots.map(l => {
      const usedUp = !((l.qty || 0) > 0);
      return '<tr' + (usedUp ? ' class="lot-used-up"' : '') + '>' +
        '<td>' + escapeHtml(PLANTS[plantOf(l)].label) + '</td>' +
        '<td>' + escapeHtml(l.vendor || '-') + '</td>' +
        '<td class="nowrap">' + escapeHtml(l.receivedDate ? formatDateIN(l.receivedDate) : '-') + '</td>' +
        '<td>' + escapeHtml(psQty(l.qty, l.uom || '')) + (usedUp ? '<div class="fs-11 text-slate-soft">used up</div>' : '') + '</td>' +
        '<td>' + (l.rate != null ? formatInr(l.rate) : '-') + '</td>' +
        '<td>' + (l.value != null ? formatInr(l.value) : '-') + '</td></tr>';
    }).join('')
    : '<tr><td colspan="6" class="ps-empty">No stock lot at any plant you can see.</td></tr>';

  const orderRows = openLinks.length
    ? openLinks.map(l => {
      const dueIn = psDaysFromToday(l.item.deliveryDate);
      return '<tr>' +
        '<td><button type="button" class="row-link ps-link" data-ps-po="' + escapeHtml(l.plantKey + '::' + l.po.poNumber) + '" data-ps-kind="' + (l.po.isImport ? 'import' : 'domestic') + '">' + escapeHtml(l.po.poNumber) + '</button>' +
          (l.po.isImport ? ' <span class="mat-tag">Import</span>' : '') + '</td>' +
        '<td>' + escapeHtml(l.plantLabel) + '</td>' +
        '<td>' + escapeHtml(l.po.vendorName || '-') + '</td>' +
        '<td class="nowrap">' + escapeHtml(l.item.deliveryDate ? formatDateIN(l.item.deliveryDate) : 'Not given') +
          (dueIn != null ? '<div class="fs-11' + (dueIn < 0 ? ' text-red' : ' text-slate-soft') + '">' + escapeHtml(psDueText(dueIn)) + '</div>' : '') + '</td>' +
        '<td>' + escapeHtml(psQty(openQtyOfLine(l.item), l.item.uom || '')) + '</td>' +
        '<td>' + formatInr(openValueOfLine(l.item)) + '</td></tr>';
    }).join('')
    : '<tr><td colspan="6" class="ps-empty">Nothing on order.</td></tr>';

  const plantCount = new Set(lots.map(plantOf)).size;
  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(m.description || m.materialCode || '-') + '</h2>' +
      '<div class="modal-meta">' + escapeHtml(categoryLabel(m.category || 'Uncategorized')) + (m.subCategory && m.subCategory !== 'Uncategorized' ? ' &middot; ' + escapeHtml(m.subCategory) : '') +
      ' &middot; ' + lots.length + ' stock lot' + (lots.length === 1 ? '' : 's') + (lots.length ? ' at ' + plantCount + ' plant' + (plantCount === 1 ? '' : 's') : '') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="field-grid"><div class="field-block full-width"><h4>At a glance</h4>' + glance + '</div></div>' +
    '<h4 class="ps-panel-heading">Stock lots</h4>' +
    '<div class="table-wrap"><table><thead><tr><th>Plant</th><th>Vendor</th><th>Received</th><th>Qty</th><th>Rate</th><th>Value</th></tr></thead><tbody>' + lotRows + '</tbody></table></div>' +
    '<h4 class="ps-panel-heading">Open orders</h4>' +
    (openLinks.length ? '<div class="no-data-note">Tied to this material by name, and by vendor where the stock sheet names one - check the On Order tab for anything worded differently.</div>' : '') +
    '<div class="table-wrap"><table><thead><tr><th>PO</th><th>Plant</th><th>Vendor</th><th>Due</th><th>Still to come</th><th>Value</th></tr></thead><tbody>' + orderRows + '</tbody></table></div>';
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
