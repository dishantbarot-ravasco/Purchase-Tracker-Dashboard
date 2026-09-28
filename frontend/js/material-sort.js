// ── Raw Material Analysis: sort configuration (2026-09-28) ───────────────
// The columns, built-in sorts and row values for list-sort.js's shared sort
// engine (see that file for how sorting and presets work). MAT_SORT is the
// sorter materials.js calls: MAT_SORT.sortRows() in
// materialsListRegionHtml(), MAT_SORT.headerHtml()/barHtml() for the markup,
// MAT_SORT.wire() in wireMaterialsListRegion(), and
// MAT_SORT.ensurePresetsLoaded() in loadAndRenderMaterials().
//
// MAT_SORT_COLUMNS' keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW["materials"]; test_sort_presets.py checks the two agree.

const MAT_SORT_COLUMNS = [
  { key: 'latest', label: 'Last received / ordered', kind: 'date', dir: 'desc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc' },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc' },
  { key: 'stock', label: 'Stock', kind: 'num', dir: 'desc' },
  { key: 'value', label: 'Inventory Value', kind: 'num', dir: 'desc' },
  { key: 'rate', label: 'Latest Rate', kind: 'num', dir: 'desc' },
  { key: 'daysLeft', label: 'Days Left', kind: 'num', dir: 'asc' },
  { key: 'pending', label: 'Pending Delivery (value)', kind: 'num', dir: 'desc' },
  { key: 'pipeline', label: 'Open PO Pipeline (value)', kind: 'num', dir: 'desc' },
];

// The first is the default: latest first (2026-09-24, project owner: "stock
// lot added first or latest, just like it's for PO latest first").
const MAT_BUILTIN_SORTS = [
  { id: 'builtin:latest', name: 'Latest first (default)', levels: [{ key: 'latest', dir: 'desc' }] },
  { id: 'builtin:category', name: 'Category (A to Z)', levels: [{ key: 'category', dir: 'asc' }] },
  { id: 'builtin:subCategory', name: 'Sub Category (A to Z)', levels: [{ key: 'subCategory', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Inventory Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:pending', name: 'Most Pending Delivery first', levels: [{ key: 'pending', dir: 'desc' }] },
];

// The value a material row sorts on. `extra` is { entryOf, latestOf } from
// materialsListRegionHtml(): the row's PO-linkage entry and its
// materialLatestDate(). null sorts last in either direction: no category
// (the API's literal "Uncategorized" counts as none), no open PO, no Days
// Left figure, an order-only row's stock and value.
function matSortValue(key, m, extra) {
  const entry = extra.entryOf(m);
  const figures = entry && entry.orderFigures;
  const c = m.consumption;
  switch (key) {
    case 'latest': return extra.latestOf(m) || null;
    case 'material': return m.description || m.materialCode || null;
    case 'category': return m.category && m.category !== 'Uncategorized' ? m.category : null;
    case 'subCategory': return m.subCategory && m.subCategory !== 'Uncategorized' ? m.subCategory : null;
    case 'stock': return m.orderOnly ? null : (m.qty || 0);
    case 'value': return m.orderOnly ? null : (m.value || 0);
    case 'rate': return m.rate != null ? m.rate : null;
    // Only where the cell shows a number - see daysLeftCellHtml().
    case 'daysLeft': return (!m.orderOnly && c && c.confidence !== 'none' && !c.negativeStock && c.daysLeft != null) ? c.daysLeft : null;
    case 'pending': return figures && figures.lineCount ? figures.pendingValue : null;
    case 'pipeline': return figures && figures.lineCount ? figures.orderedValue : null;
    default: return null;
  }
}

const MAT_SORT = createListSort({
  view: 'materials',
  idPrefix: 'mat',
  columns: MAT_SORT_COLUMNS,
  builtins: MAT_BUILTIN_SORTS,
  rowValue: matSortValue,
  // Rows every level ties on keep the default order: latest first, then
  // stock qty, then value still on order.
  tieBreak: (a, b, extra) => {
    const ea = extra.entryOf(a);
    const eb = extra.entryOf(b);
    return (extra.latestOf(b) || '').localeCompare(extra.latestOf(a) || '')
      || ((b.qty || 0) - (a.qty || 0))
      || ((eb ? eb.openValue : 0) - (ea ? ea.openValue : 0));
  },
  onChange: () => { state.matTablePage = 1; },
  rerender: () => renderMaterialsListRegion(),
});

// ── The Raw Material modal's two tables (2026-09-28) ──
// Project owner: "add the same sorting to the Raw Material modal tables".
// Stock by Plant (MAT_LOTS_SORT, one row per stock lot at any plant) and
// Purchase Activity's Open Purchase Orders (MAT_OPEN_PO_SORT, one row per
// open PO line). Their rows carry inline-edit and dismiss controls, so a
// sort change re-orders the existing rows in place (material-modal.js's
// resortMatModalTable()) rather than rebuilding them - rebuilding would
// have to re-wire those controls, and wireEditIcons() resets a correction
// the reader may be half-way through.
//
// Keys must equal SORT_KEYS_BY_VIEW["material_lots"] and
// ["material_open_pos"]; test_sort_presets.py checks.

const MAT_LOTS_SORT_COLUMNS = [
  { key: 'plant', label: 'Plant', kind: 'text', dir: 'asc' },
  { key: 'vendor', label: 'Vendor', kind: 'text', dir: 'asc' },
  { key: 'received', label: 'Received', kind: 'date', dir: 'desc' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc' },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc' },
  { key: 'qty', label: 'Qty', kind: 'num', dir: 'desc' },
  { key: 'rate', label: 'Rate', kind: 'num', dir: 'asc' },
  { key: 'value', label: 'Value', kind: 'num', dir: 'desc' },
];

// The default is the order this table always had: by plant, lots still in
// stock before used-up ones, newest first (the tie-break below does the
// last two).
const MAT_LOTS_BUILTIN_SORTS = [
  { id: 'builtin:plant', name: 'By plant (default)', levels: [{ key: 'plant', dir: 'asc' }] },
  { id: 'builtin:received', name: 'Newest received first', levels: [{ key: 'received', dir: 'desc' }] },
  { id: 'builtin:qty', name: 'Largest quantity first', levels: [{ key: 'qty', dir: 'desc' }] },
  { id: 'builtin:value', name: 'Highest value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:rate', name: 'Lowest rate first', levels: [{ key: 'rate', dir: 'asc' }] },
  { id: 'builtin:vendor', name: 'Vendor (A to Z)', levels: [{ key: 'vendor', dir: 'asc' }, { key: 'received', dir: 'desc' }] },
  { id: 'builtin:category', name: 'Category, then Sub Category', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }] },
];

function matLotSortValue(key, l) {
  switch (key) {
    case 'plant': return l._plantLabel || null;
    case 'vendor': return l.vendor || null;
    case 'received': return l.receivedDate || null;
    case 'category': return l.category && l.category !== 'Uncategorized' ? l.category : null;
    case 'subCategory': return l.subCategory && l.subCategory !== 'Uncategorized' ? l.subCategory : null;
    case 'qty': return l.qty != null ? l.qty : null;
    case 'rate': return l.rate != null ? l.rate : null;
    case 'value': return l.value != null ? l.value : null;
    default: return null;
  }
}

const MAT_LOTS_SORT = createListSort({
  view: 'material_lots',
  idPrefix: 'matLots',
  columns: MAT_LOTS_SORT_COLUMNS,
  builtins: MAT_LOTS_BUILTIN_SORTS,
  rowValue: matLotSortValue,
  tieBreak: (a, b) => (((b.qty || 0) > 0) - ((a.qty || 0) > 0))
    || (b.receivedDate || '').localeCompare(a.receivedDate || ''),
  rerender: () => resortMatModalTable('lots'),
});

const MAT_OPEN_PO_SORT_COLUMNS = [
  { key: 'delivery', label: 'Delivery Date', kind: 'date', dir: 'asc' },
  { key: 'created', label: 'PO Created On', kind: 'date', dir: 'desc' },
  { key: 'poNumber', label: 'PO Number', kind: 'text', dir: 'asc' },
  { key: 'vendor', label: 'Vendor', kind: 'text', dir: 'asc' },
  { key: 'plant', label: 'Plant', kind: 'text', dir: 'asc' },
  { key: 'qtyToCome', label: 'Qty to come', kind: 'num', dir: 'desc' },
  { key: 'valueToCome', label: 'Value to come', kind: 'num', dir: 'desc' },
  { key: 'status', label: 'Status', kind: 'rank', dir: 'asc' },
];

// The default is the order this table always had: soonest delivery first.
const MAT_OPEN_PO_BUILTIN_SORTS = [
  { id: 'builtin:delivery', name: 'Delivery Date (soonest first)', levels: [{ key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Most value to come first', levels: [{ key: 'valueToCome', dir: 'desc' }] },
  { id: 'builtin:qty', name: 'Most quantity to come first', levels: [{ key: 'qtyToCome', dir: 'desc' }] },
  { id: 'builtin:status', name: 'Most urgent status first', levels: [{ key: 'status', dir: 'asc' }, { key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:vendor', name: 'Vendor (A to Z)', levels: [{ key: 'vendor', dir: 'asc' }, { key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:created', name: 'Newest PO first', levels: [{ key: 'created', dir: 'desc' }] },
];

// `l` is a linkedPoItemsForMaterial() link: {po, item, plantKey, plantLabel}.
function matOpenPoSortValue(key, l) {
  switch (key) {
    case 'delivery': return l.po._deliveryDate || null;
    case 'created': return l.po.createdDate || null;
    case 'poNumber': return l.po.poNumber || null;
    case 'vendor': return l.po.vendorName || null;
    case 'plant': return l.plantLabel || null;
    case 'qtyToCome': return openQtyOfLine(l.item);
    // The cell shows "-" without a rate, so that sorts with the blanks.
    case 'valueToCome': return l.item.netPrice != null && l.item.qty != null ? openValueOfLine(l.item) : null;
    case 'status': { const rank = PO_STATUS_RANK[l.po._overdue ? 'overdue' : l.po._status]; return rank != null ? rank : null; }
    default: return null;
  }
}

const MAT_OPEN_PO_SORT = createListSort({
  view: 'material_open_pos',
  idPrefix: 'matOpenPos',
  columns: MAT_OPEN_PO_SORT_COLUMNS,
  builtins: MAT_OPEN_PO_BUILTIN_SORTS,
  rowValue: matOpenPoSortValue,
  tieBreak: (a, b) => (a.po._deliveryDate || '').localeCompare(b.po._deliveryDate || '')
    || String(a.po.poNumber || '').localeCompare(String(b.po.poNumber || ''), 'en', { numeric: true }),
  rerender: () => resortMatModalTable('openPos'),
});
