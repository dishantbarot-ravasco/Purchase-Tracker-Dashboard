// ── Plant stock tabs: sort configuration (2026-09-29) ────────────────────
// The columns, built-in sorts and row values for the three plant tabs that
// plant-stock.js renders - Inventory (INV_SORT), On Order (ORD_SORT) and
// Stock & Orders (COMB_SORT) - on list-sort.js's shared sort engine, so they
// sort, pick values and save presets exactly like every other list.
//
// Every row carries `sv`, its sort values already worked out by
// plant-stock.js's row builders, so psSortValue() only reads them. The
// per-plant columns (hrsStock, achhadToCome, ...) are listed for every plant;
// a plant not in view simply has no value, and a row with no value sorts
// last.
//
// Every *_SORT_COLUMNS list's keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW for its view, and its `pick` columns PICK_KEYS_BY_VIEW;
// test_sort_presets.py checks.

function psSortValue(key, r) {
  const v = r.sv[key];
  return v === undefined ? null : v;
}

function psTieBreak(a, b) {
  return (a.m.description || '').localeCompare(b.m.description || '');
}

// ── Inventory ──
const INV_SORT_COLUMNS = [
  { key: 'status', label: 'Stock status', kind: 'rank', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'stock', label: 'Total stock', kind: 'num', dir: 'desc' },
  { key: 'hrsStock', label: 'Stock at HRS', kind: 'num', dir: 'desc' },
  { key: 'achhadStock', label: 'Stock at RTP-Achhad', kind: 'num', dir: 'desc' },
  { key: 'vapiStock', label: 'Stock at RTP-Vapi', kind: 'num', dir: 'desc' },
  { key: 'value', label: 'Stock Value', kind: 'num', dir: 'desc' },
  { key: 'dailyUse', label: 'Daily Use', kind: 'num', dir: 'desc' },
  { key: 'daysLeft', label: 'Days Left', kind: 'num', dir: 'asc' },
  { key: 'received', label: 'Last received', kind: 'date', dir: 'desc' },
];

// The first is the default: what needs attention first, then what runs out
// soonest.
const INV_BUILTIN_SORTS = [
  { id: 'builtin:urgent', name: 'Needs attention first (default)', levels: [{ key: 'status', dir: 'asc' }, { key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material (A to Z)', levels: [{ key: 'material', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Stock Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:received', name: 'Latest received first', levels: [{ key: 'received', dir: 'desc' }] },
];

const INV_SORT = createListSort({
  view: 'plant_inventory',
  idPrefix: 'inv',
  columns: INV_SORT_COLUMNS,
  builtins: INV_BUILTIN_SORTS,
  rowValue: psSortValue,
  tieBreak: psTieBreak,
  onChange: () => { PS_STATE.inventory.page = 1; },
  rerender: () => renderPlantListRegion(),
});

// ── On Order ──
const ORD_SORT_COLUMNS = [
  { key: 'status', label: 'Status', kind: 'rank', dir: 'asc' },
  { key: 'nextDue', label: 'Next due', kind: 'date', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'toComeValue', label: 'Value still to come', kind: 'num', dir: 'desc' },
  { key: 'hrsToCome', label: 'To come at HRS (value)', kind: 'num', dir: 'desc' },
  { key: 'achhadToCome', label: 'To come at RTP-Achhad (value)', kind: 'num', dir: 'desc' },
  { key: 'vapiToCome', label: 'To come at RTP-Vapi (value)', kind: 'num', dir: 'desc' },
];

// Most urgent first: overdue, then reorder soon, then by next due date.
const ORD_BUILTIN_SORTS = [
  { id: 'builtin:urgent', name: 'Most urgent first (default)', levels: [{ key: 'status', dir: 'asc' }, { key: 'nextDue', dir: 'asc' }] },
  { id: 'builtin:nextDue', name: 'Next due, soonest first', levels: [{ key: 'nextDue', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest value still to come first', levels: [{ key: 'toComeValue', dir: 'desc' }] },
  { id: 'builtin:catMat', name: 'Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material (A to Z)', levels: [{ key: 'material', dir: 'asc' }] },
];

const ORD_SORT = createListSort({
  view: 'plant_on_order',
  idPrefix: 'ord',
  columns: ORD_SORT_COLUMNS,
  builtins: ORD_BUILTIN_SORTS,
  rowValue: psSortValue,
  tieBreak: psTieBreak,
  onChange: () => { PS_STATE.onorder.page = 1; },
  rerender: () => renderPlantListRegion(),
});

// ── Stock & Orders ──
const COMB_SORT_COLUMNS = [
  { key: 'status', label: 'Status', kind: 'rank', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'stock', label: 'In stock', kind: 'num', dir: 'desc' },
  { key: 'value', label: 'Stock Value', kind: 'num', dir: 'desc' },
  { key: 'daysLeft', label: 'Days Left', kind: 'num', dir: 'asc' },
  { key: 'toComeValue', label: 'Value still to come', kind: 'num', dir: 'desc' },
  { key: 'nextDue', label: 'Next due', kind: 'date', dir: 'asc' },
];

const COMB_BUILTIN_SORTS = [
  { id: 'builtin:urgent', name: 'Needs attention first (default)', levels: [{ key: 'status', dir: 'asc' }, { key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material (A to Z)', levels: [{ key: 'material', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Stock Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:toCome', name: 'Highest value still to come first', levels: [{ key: 'toComeValue', dir: 'desc' }] },
];

const COMB_SORT = createListSort({
  view: 'plant_combined',
  idPrefix: 'comb',
  columns: COMB_SORT_COLUMNS,
  builtins: COMB_BUILTIN_SORTS,
  rowValue: psSortValue,
  tieBreak: psTieBreak,
  onChange: () => { PS_STATE.combined.page = 1; },
  rerender: () => renderPlantListRegion(),
});
