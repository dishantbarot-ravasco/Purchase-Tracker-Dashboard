// ── Plant stock tabs: sort configuration (2026-09-29) ────────────────────
// The columns, built-in sorts and row values for the three plant tabs that
// plant-stock.js renders - Inventory (INV_SORT), On Order (ORD_SORT) and
// Stock Planner (PLAN_SORT) - on list-sort.js's shared sort engine, so they
// sort, pick values and save presets exactly like every other list.
//
// Every row these sorters see is a plant-stock.js row object whose figures
// are already worked out (psInventoryRows() / psOrderRows() / psPlannerRows()),
// so rowValue() only reads fields - no figure is derived twice.
//
// Every *_SORT_COLUMNS list's keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW for its view, and its `pick` columns PICK_KEYS_BY_VIEW;
// test_sort_presets.py checks.

// ── Inventory ──
const INV_SORT_COLUMNS = [
  { key: 'status', label: 'Stock status', kind: 'rank', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'stock', label: 'In Stock', kind: 'num', dir: 'desc' },
  { key: 'value', label: 'Stock Value', kind: 'num', dir: 'desc' },
  { key: 'dailyUse', label: 'Daily Use', kind: 'num', dir: 'desc' },
  { key: 'daysLeft', label: 'Days Left', kind: 'num', dir: 'asc' },
  { key: 'received', label: 'Last Received', kind: 'date', dir: 'desc' },
];

// The first is the default: what needs attention first, then what runs out
// soonest.
const INV_BUILTIN_SORTS = [
  { id: 'builtin:urgent', name: 'Needs attention first (default)', levels: [{ key: 'status', dir: 'asc' }, { key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material (A to Z)', levels: [{ key: 'material', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Stock Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:received', name: 'Latest received first', levels: [{ key: 'received', dir: 'desc' }] },
];

function invSortValue(key, r) {
  switch (key) {
    case 'status': return r.rank;
    case 'material': return r.m.description || null;
    case 'category': return r.category;
    case 'subCategory': return r.subCategory;
    case 'stock': return r.m.qty != null ? r.m.qty : null;
    case 'value': return r.m.value != null ? r.m.value : null;
    case 'dailyUse': return r.dailyUse;
    case 'daysLeft': return r.daysLeft;
    case 'received': return r.lastReceived;
    default: return null;
  }
}

const INV_SORT = createListSort({
  view: 'plant_inventory',
  idPrefix: 'inv',
  columns: INV_SORT_COLUMNS,
  builtins: INV_BUILTIN_SORTS,
  rowValue: invSortValue,
  tieBreak: (a, b) => (a.m.description || '').localeCompare(b.m.description || ''),
  onChange: () => { PS_STATE.inventory.page = 1; },
  rerender: () => renderPlantListRegion(),
});

// ── On Order ──
const ORD_SORT_COLUMNS = [
  { key: 'due', label: 'Due Date', kind: 'date', dir: 'asc' },
  { key: 'status', label: 'Delivery status', kind: 'rank', dir: 'asc' },
  { key: 'created', label: 'PO Date', kind: 'date', dir: 'desc' },
  { key: 'poNumber', label: 'PO Number', kind: 'text', dir: 'asc' },
  { key: 'vendor', label: 'Vendor', kind: 'text', dir: 'asc', pick: true },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'plant', label: 'Plant', kind: 'text', dir: 'asc', pick: true },
  { key: 'toComeValue', label: 'Value Still to Come', kind: 'num', dir: 'desc' },
];

// Soonest due first by default - an overdue line is the oldest date, so it
// leads, and a line with no due date goes last.
const ORD_BUILTIN_SORTS = [
  { id: 'builtin:due', name: 'Due date, soonest first (default)', levels: [{ key: 'due', dir: 'asc' }] },
  { id: 'builtin:urgent', name: 'Most urgent first', levels: [{ key: 'status', dir: 'asc' }, { key: 'due', dir: 'asc' }] },
  { id: 'builtin:vendor', name: 'Vendor, then due date', levels: [{ key: 'vendor', dir: 'asc' }, { key: 'due', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material, then due date', levels: [{ key: 'material', dir: 'asc' }, { key: 'due', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest value still to come first', levels: [{ key: 'toComeValue', dir: 'desc' }] },
  { id: 'builtin:created', name: 'Newest PO first', levels: [{ key: 'created', dir: 'desc' }] },
];

function ordSortValue(key, r) {
  switch (key) {
    case 'due': return r.due;
    case 'status': return r.rank;
    case 'created': return r.po.createdDate || null;
    case 'poNumber': return r.po.poNumber || null;
    case 'vendor': return r.po.vendorName || null;
    case 'material': return r.item.description || null;
    case 'category': return r.category;
    case 'plant': return r.plantLabel;
    case 'toComeValue': return r.toComeValue;
    default: return null;
  }
}

const ORD_SORT = createListSort({
  view: 'plant_on_order',
  idPrefix: 'ord',
  columns: ORD_SORT_COLUMNS,
  builtins: ORD_BUILTIN_SORTS,
  rowValue: ordSortValue,
  tieBreak: (a, b) => (a.po.poNumber || '').localeCompare(b.po.poNumber || '') || (a.lineIndex - b.lineIndex),
  onChange: () => { PS_STATE.onorder.page = 1; },
  rerender: () => renderPlantListRegion(),
});

// ── Stock Planner ──
const PLAN_SORT_COLUMNS = [
  { key: 'status', label: 'Action', kind: 'rank', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'daysLeft', label: 'Days Left', kind: 'num', dir: 'asc' },
  { key: 'nextDelivery', label: 'Next Delivery', kind: 'date', dir: 'asc' },
  { key: 'toComeValue', label: 'Value Still to Come', kind: 'num', dir: 'desc' },
];

const PLAN_BUILTIN_SORTS = [
  { id: 'builtin:urgent', name: 'Most urgent first (default)', levels: [{ key: 'status', dir: 'asc' }, { key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:nextDelivery', name: 'Next delivery, soonest first', levels: [{ key: 'nextDelivery', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest value still to come first', levels: [{ key: 'toComeValue', dir: 'desc' }] },
];

function planSortValue(key, r) {
  switch (key) {
    case 'status': return r.rank;
    case 'material': return r.m.description || null;
    case 'category': return r.category;
    case 'subCategory': return r.subCategory;
    case 'daysLeft': return r.daysLeft;
    case 'nextDelivery': return r.nextDue;
    case 'toComeValue': return r.toComeValue;
    default: return null;
  }
}

const PLAN_SORT = createListSort({
  view: 'stock_planner',
  idPrefix: 'plan',
  columns: PLAN_SORT_COLUMNS,
  builtins: PLAN_BUILTIN_SORTS,
  rowValue: planSortValue,
  tieBreak: (a, b) => (a.m.description || '').localeCompare(b.m.description || ''),
  onChange: () => { PS_STATE.planner.page = 1; },
  rerender: () => renderPlantListRegion(),
});
