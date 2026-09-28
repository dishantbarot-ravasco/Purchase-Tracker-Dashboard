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
