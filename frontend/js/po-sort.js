// ── Purchase Orders (Domestic): sort configuration (2026-09-28) ──────────
// Project owner: "add the same sorting and presets to the Purchase Orders
// tab". The columns, built-in sorts and row values for list-sort.js's shared
// sort engine (see that file for how sorting, picking values and presets
// work). PO_SORT is the sorter po-list.js calls: PO_SORT.sortRows() in
// poListRegionHtml(), PO_SORT.headerHtml()/barHtml() for the markup,
// PO_SORT.wire() in wirePoListRegion(), and PO_SORT.ensurePresetsLoaded() in
// main.js's loadDashboard().
//
// Category and Sub Category are not table columns here (a PO can order
// materials of several categories), so they sort from the Sort by select and
// Custom sort only. A PO's category for sorting is its categories A to Z,
// joined - "Chemicals, Fillers" sorts after "Chemicals" alone. Picking a
// category, sub category or material (poPickValues()) matches a PO holding
// it on any line, so Category -> Sub Category -> Material narrows to the
// orders for one material.
//
// PO_SORT_COLUMNS' keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW["purchase_orders"], and its `pick` columns
// PICK_KEYS_BY_VIEW; test_sort_presets.py checks.

const PO_SORT_COLUMNS = [
  { key: 'created', label: 'Created On', kind: 'date', dir: 'desc' },
  { key: 'poNumber', label: 'PO Number', kind: 'text', dir: 'asc' },
  { key: 'vendor', label: 'Vendor', kind: 'text', dir: 'asc', pick: true },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'delivery', label: 'Delivery Date', kind: 'date', dir: 'asc' },
  { key: 'value', label: 'Value', kind: 'num', dir: 'desc' },
  { key: 'status', label: 'Status', kind: 'rank', dir: 'asc' },
];

// The first is the default: latest first, the order this list always had.
const PO_BUILTIN_SORTS = [
  { id: 'builtin:latest', name: 'Latest first (default)', levels: [{ key: 'created', dir: 'desc' }] },
  { id: 'builtin:category', name: 'Category (A to Z)', levels: [{ key: 'category', dir: 'asc' }] },
  { id: 'builtin:subCategory', name: 'Sub Category (A to Z)', levels: [{ key: 'subCategory', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }, { key: 'created', dir: 'desc' }] },
  { id: 'builtin:vendor', name: 'Vendor (A to Z)', levels: [{ key: 'vendor', dir: 'asc' }, { key: 'created', dir: 'desc' }] },
  { id: 'builtin:delivery', name: 'Delivery Date (soonest first)', levels: [{ key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:status', name: 'Most urgent status first', levels: [{ key: 'status', dir: 'asc' }, { key: 'delivery', dir: 'asc' }] },
];

// Overdue first, received last. `_overdue` is an overlay on a partly
// delivered order too (see flags.js's computeStatus()), so it wins over
// `_status`.
const PO_STATUS_RANK = { overdue: 0, pending: 1, partial: 2, unknown: 3, received: 4 };

// A PO's distinct categories (or sub categories), A to Z, joined; null when
// it has none. "Uncategorized" and blanks count as none, so they sort last.
function poCategoryText(po, field) {
  const values = Array.from(new Set((po.materialCategories || [])
    .map(c => c[field])
    .filter(v => v && v !== 'Uncategorized')));
  values.sort((a, b) => a.localeCompare(b, 'en', { sensitivity: 'base' }));
  return values.length ? values.join(', ') : null;
}

// The values an order holds for a `pick` column: every line's material, and
// every category and sub category it orders ("Uncategorized" counts as
// none). null for the other columns, which hold one value each. Shared with
// import-sort.js.
function poPickValues(key, po) {
  const clean = v => v && v !== 'Uncategorized';
  if (key === 'material') return Array.from(new Set((po.items || []).map(i => i.description).filter(Boolean)));
  if (key === 'category') return Array.from(new Set((po.materialCategories || []).map(c => c.category).filter(clean)));
  if (key === 'subCategory') return Array.from(new Set((po.materialCategories || []).map(c => c.subCategory).filter(clean)));
  return null;
}

// The Custom sort editor's choices for a Sub Category or Material level
// under picked categories / sub categories: only the ones this order holds
// on a line (or category pair) that sits under those picks. Each line item
// carries its own category (_domestic_base._categorized_line_item_dict(),
// the import router likewise). Shared with import-sort.js.
function poScopedPickValues(key, po, picks) {
  const clean = v => v && v !== 'Uncategorized';
  const under = (category, subCategory) => (!picks.category || picks.category.includes(category))
    && (!picks.subCategory || picks.subCategory.includes(subCategory));
  if (key === 'subCategory') {
    return Array.from(new Set((po.materialCategories || [])
      .filter(c => !picks.category || picks.category.includes(c.category))
      .map(c => c.subCategory).filter(clean)));
  }
  if (key === 'material') {
    return Array.from(new Set((po.items || []).filter(i => i.description && under(i.category, i.subCategory)).map(i => i.description)));
  }
  return null;
}

// The value a PO row sorts on; null sorts last in either direction.
function poSortValue(key, po) {
  switch (key) {
    case 'created': return po.createdDate || null;
    case 'poNumber': return po.poNumber || null;
    case 'vendor': return po.vendorName || null;
    case 'material': { const item = (po.items || []).find(i => i.description); return item ? item.description : null; }
    case 'category': return poCategoryText(po, 'category');
    case 'subCategory': return poCategoryText(po, 'subCategory');
    case 'delivery': return po._deliveryDate || null;
    case 'value': return po.totalInclTax != null ? po.totalInclTax : (po.totalValue != null ? po.totalValue : null);
    case 'status': { const rank = PO_STATUS_RANK[po._overdue ? 'overdue' : po._status]; return rank != null ? rank : null; }
    default: return null;
  }
}

const PO_SORT = createListSort({
  view: 'purchase_orders',
  idPrefix: 'po',
  columns: PO_SORT_COLUMNS,
  builtins: PO_BUILTIN_SORTS,
  rowValue: poSortValue,
  pickValues: poPickValues,
  scopedPickValues: poScopedPickValues,
  // Rows every level ties on keep the default order: latest first, then PO
  // number.
  tieBreak: (a, b) => (b.createdDate || '').localeCompare(a.createdDate || '')
    || String(a.poNumber || '').localeCompare(String(b.poNumber || ''), 'en', { numeric: true }),
  onChange: () => { state.tablePage = 1; },
  rerender: () => renderPoListRegion(),
});
