// ── Import Purchases: sort configuration (2026-09-28) ────────────────────
// Project owner: "add the same sorting to Import Purchases too". The
// columns, built-in sorts and row values for list-sort.js's shared sort
// engine (see that file for how sorting and presets work). IMPORT_SORT is
// the sorter import-po.js calls: IMPORT_SORT.sortRows() in
// importListRegionHtml(), IMPORT_SORT.headerHtml()/barHtml() for the markup,
// IMPORT_SORT.wire() in wireImportListRegion(); main.js loads its presets
// with the import orders.
//
// Category and Sub Category are not table columns (an order can hold
// materials of several categories), so they sort from the Sort by select and
// Custom sort only, the same way po-sort.js does it (poCategoryText()).
//
// IMPORT_SORT_COLUMNS' keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW["import_purchases"]; test_sort_presets.py checks.

const IMPORT_SORT_COLUMNS = [
  { key: 'created', label: 'Created On', kind: 'date', dir: 'desc' },
  { key: 'poNumber', label: 'PO Number', kind: 'text', dir: 'asc' },
  { key: 'vendor', label: 'Vendor', kind: 'text', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc' },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc' },
  { key: 'plant', label: 'Plant', kind: 'text', dir: 'asc' },
  { key: 'country', label: 'Country of Origin', kind: 'text', dir: 'asc' },
  { key: 'delivery', label: 'Delivery Date', kind: 'date', dir: 'asc' },
  { key: 'value', label: 'Order Value (before duty)', kind: 'num', dir: 'desc' },
  { key: 'blNumber', label: 'BL Number', kind: 'text', dir: 'asc' },
  { key: 'stage', label: 'Shipment Stage', kind: 'stage', dir: 'asc' },
];

// The first is the default: latest first, the order this list always had.
const IMPORT_BUILTIN_SORTS = [
  { id: 'builtin:latest', name: 'Latest first (default)', levels: [{ key: 'created', dir: 'desc' }] },
  { id: 'builtin:category', name: 'Category (A to Z)', levels: [{ key: 'category', dir: 'asc' }] },
  { id: 'builtin:subCategory', name: 'Sub Category (A to Z)', levels: [{ key: 'subCategory', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'created', dir: 'desc' }] },
  { id: 'builtin:vendor', name: 'Vendor (A to Z)', levels: [{ key: 'vendor', dir: 'asc' }, { key: 'created', dir: 'desc' }] },
  { id: 'builtin:country', name: 'Country of Origin (A to Z)', levels: [{ key: 'country', dir: 'asc' }, { key: 'created', dir: 'desc' }] },
  { id: 'builtin:stage', name: 'Shipment Stage (earliest first)', levels: [{ key: 'stage', dir: 'asc' }, { key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Value first', levels: [{ key: 'value', dir: 'desc' }] },
];

// Order placed, then shipped on a Bill of Lading, then cleared through
// customs on a Bill of Entry - import-po.js's IMPORT_STAGES order.
const IMPORT_STAGE_RANK = { Placed: 0, 'Shipped (BL)': 1, 'Cleared (BOE)': 2 };

// The value an import order sorts on; null sorts last in either direction.
function importSortValue(key, po) {
  switch (key) {
    case 'created': return po.createdDate || null;
    case 'poNumber': return po.poNumber || null;
    case 'vendor': return po.vendorName || null;
    case 'material': { const item = (po.items || []).find(i => i.description); return item ? item.description : null; }
    case 'category': return poCategoryText(po, 'category');
    case 'subCategory': return poCategoryText(po, 'subCategory');
    case 'plant': return po.plantLabel || po.plant || null;
    case 'country': return po.countryOfOrigin || null;
    // What the list's Delivery and Order Value columns show (import-po.js's
    // importPoDeliveryDate() and importPoInrValue(): the next open delivery
    // date, and the order value in INR before duty - null, sorting with the
    // blanks, when a line lacks its value or exchange rate).
    case 'delivery': return importPoDeliveryDate(po);
    case 'value': return importPoInrValue(po);
    case 'blNumber': return po.billOfLadingNumber || null;
    case 'stage': { const rank = IMPORT_STAGE_RANK[po.shipmentStage]; return rank != null ? rank : null; }
    default: return null;
  }
}

const IMPORT_SORT = createListSort({
  view: 'import_purchases',
  idPrefix: 'import',
  columns: IMPORT_SORT_COLUMNS,
  builtins: IMPORT_BUILTIN_SORTS,
  rowValue: importSortValue,
  // Rows every level ties on keep the default order: latest first, then PO
  // number.
  tieBreak: (a, b) => (b.createdDate || '').localeCompare(a.createdDate || '')
    || String(a.poNumber || '').localeCompare(String(b.poNumber || ''), 'en', { numeric: true }),
  onChange: () => { state.importTablePage = 1; },
  rerender: () => renderImportListRegion(),
});
