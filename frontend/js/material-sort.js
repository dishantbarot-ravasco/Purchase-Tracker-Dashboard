// ── Raw Material Analysis: sorting and sort presets (2026-09-28) ─────────
// Project owner: "option to sort by Category and Subcategory, also provide
// option to set presets too like we have sorting preset in Excel and Sheets",
// presets "saved to user's account".
//
// Three ways in, one sort state (MAT_SORT_STATE.levels):
//   - the "Sort by" select: built-in sorts (Category alone, Sub Category
//     alone, Category then Sub Category, ...) and the user's own presets;
//   - a click on a sortable column header: that one column, clicking again
//     flips its direction;
//   - "Custom sort...": an Excel-style editor that stacks up to
//     MAT_SORT_MAX_LEVELS levels ("Sort by X, then by Y"), applied or saved
//     as a named preset.
//
// A sort is TABLE-ONLY, like the Material search: it never changes the KPI
// row, the filters or the chart, so every change re-renders the list region
// alone (renderMaterialsListRegion()), never the whole view.
//
// Saved presets live on the server per user (GET/POST/PATCH/DELETE
// /api/sort-presets, apps/api/routers/preferences_views.py), so they follow
// the user to any device. The sort in use is remembered in this browser only
// (localStorage) - a per-viewer convenience that may come back empty.
//
// MAT_SORT_COLUMNS' keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW["materials"]; test_sort_presets.py checks the two agree.

// `kind` picks the direction wording and how a value compares: 'text'
// (A to Z), 'num' (smallest/largest), 'date' (oldest/newest). `dir` is the
// direction a header click starts with.
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
const MAT_SORT_COLUMN_BY_KEY = new Map(MAT_SORT_COLUMNS.map(c => [c.key, c]));
const MAT_SORT_MAX_LEVELS = 5;
const MAT_SORT_STORAGE_KEY = 'pt.matSort.v1';

// Ready-made sorts every user has. Ids are 'builtin:*' so they never collide
// with a saved preset's numeric id.
const MAT_BUILTIN_SORTS = [
  { id: 'builtin:latest', name: 'Latest first (default)', levels: [{ key: 'latest', dir: 'desc' }] },
  { id: 'builtin:category', name: 'Category (A to Z)', levels: [{ key: 'category', dir: 'asc' }] },
  { id: 'builtin:subCategory', name: 'Sub Category (A to Z)', levels: [{ key: 'subCategory', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:daysLeft', name: 'Fewest Days Left first', levels: [{ key: 'daysLeft', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest Inventory Value first', levels: [{ key: 'value', dir: 'desc' }] },
  { id: 'builtin:pending', name: 'Most Pending Delivery first', levels: [{ key: 'pending', dir: 'desc' }] },
];
const MAT_DEFAULT_SORT = MAT_BUILTIN_SORTS[0];

// null until loaded; [] when the user has none. MAT_USER_PRESETS_ERROR is
// set when the load failed - the page still works, only the user's own
// presets are missing, and the select says so.
let MAT_USER_PRESETS = null;
let MAT_USER_PRESETS_ERROR = false;

// levels/presetId: the sort in use ('custom' when it matches no preset).
// editorOpen/draft/draftName: the Custom sort editor, kept here so a region
// re-render never loses a half-built sort. msg: the editor's status line.
const MAT_SORT_STATE = { levels: MAT_DEFAULT_SORT.levels.slice(), presetId: MAT_DEFAULT_SORT.id, editorOpen: false, draft: null, draftName: '', msg: '' };

function validSortLevels(levels) {
  if (!Array.isArray(levels) || !levels.length || levels.length > MAT_SORT_MAX_LEVELS) return false;
  const seen = new Set();
  return levels.every(l => l && MAT_SORT_COLUMN_BY_KEY.has(l.key) && (l.dir === 'asc' || l.dir === 'desc') && !seen.has(l.key) && seen.add(l.key));
}

function sameSortLevels(a, b) {
  return a.length === b.length && a.every((l, i) => l.key === b[i].key && l.dir === b[i].dir);
}

function cloneSortLevels(levels) {
  return levels.map(l => ({ key: l.key, dir: l.dir }));
}

(function restoreStoredMatSort() {
  try {
    const stored = JSON.parse(localStorage.getItem(MAT_SORT_STORAGE_KEY) || 'null');
    if (stored && validSortLevels(stored.levels)) {
      MAT_SORT_STATE.levels = cloneSortLevels(stored.levels);
      MAT_SORT_STATE.presetId = typeof stored.presetId === 'string' || typeof stored.presetId === 'number' ? stored.presetId : 'custom';
    }
  } catch (e) { /* storage blocked or corrupt: the default sort stands */ }
})();

function storeMatSort() {
  try {
    localStorage.setItem(MAT_SORT_STORAGE_KEY, JSON.stringify({ levels: MAT_SORT_STATE.levels, presetId: MAT_SORT_STATE.presetId }));
  } catch (e) { /* storage blocked: the sort still applies for this visit */ }
}

// The preset (built-in or saved) whose levels equal `levels`, preferring the
// one already selected, so a header click that lands on "Category (A to Z)"
// shows that name rather than "Custom".
function presetIdForLevels(levels) {
  const all = MAT_BUILTIN_SORTS.concat(MAT_USER_PRESETS || []);
  const current = all.find(p => String(p.id) === String(MAT_SORT_STATE.presetId));
  if (current && sameSortLevels(current.levels, levels)) return current.id;
  const found = all.find(p => sameSortLevels(p.levels, levels));
  return found ? found.id : 'custom';
}

function setMatSort(levels, presetId) {
  MAT_SORT_STATE.levels = cloneSortLevels(levels);
  MAT_SORT_STATE.presetId = presetId != null ? presetId : presetIdForLevels(levels);
  state.matTablePage = 1;
  storeMatSort();
}

// Plain JSON fetch for /api/sort-presets - not under a plant prefix. Same
// 401 and error contract as shared.js's apiImports().
async function sortPresetApi(path, opts) {
  const res = await authFetch('/api/sort-presets' + path, Object.assign({ credentials: 'same-origin' }, opts || {}));
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  if (res.status === 204) return null;
  let data;
  try { data = await res.json(); } catch (e) { throw unexpectedResponseError(res.status); }
  if (!res.ok) {
    const err = new Error(data.error || data.detail || 'Something went wrong. Please try again.');
    err.status = res.status;
    throw err;
  }
  return data;
}

function jsonBody(method, body) {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

function sortUserPresets() {
  MAT_USER_PRESETS.sort((a, b) => a.name.localeCompare(b.name, 'en', { sensitivity: 'base' }));
}

// Loaded once per page visit; never throws - a failed load leaves the
// built-in sorts working and says so in the select.
async function ensureSortPresetsLoaded() {
  if (MAT_USER_PRESETS) return;
  try {
    const data = await sortPresetApi('?view=materials');
    MAT_USER_PRESETS = (data.presets || []).filter(p => validSortLevels(p.levels));
    MAT_USER_PRESETS_ERROR = false;
    sortUserPresets();
    // A remembered preset id that was deleted on another device reads as Custom.
    if (!MAT_BUILTIN_SORTS.some(p => p.id === MAT_SORT_STATE.presetId) && !MAT_USER_PRESETS.some(p => p.id === MAT_SORT_STATE.presetId)) {
      MAT_SORT_STATE.presetId = presetIdForLevels(MAT_SORT_STATE.levels);
    }
  } catch (e) {
    console.error('Loading sort presets failed:', e);
    MAT_USER_PRESETS = null;
    MAT_USER_PRESETS_ERROR = true;
  }
}

// ── Sorting ──
// The value a row sorts on for one column; null sorts LAST in either
// direction (a blank category, no open PO, no Days Left figure), the way a
// spreadsheet keeps blanks at the bottom.
function matSortValue(key, m, entry, latest) {
  const figures = entry && entry.orderFigures;
  const c = m.consumption;
  switch (key) {
    case 'latest': return latest || null;
    case 'material': return m.description || m.materialCode || null;
    // The API sends "Uncategorized" for a material with no reference row;
    // it sorts with the blanks, not under U.
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

// `rows` sorted by `levels`; ties after the last level fall back to the
// default order (latest first, then stock qty, then value still on order),
// so the result is stable and never depends on load order.
function sortMaterialRows(rows, levels, entryOf, latestOf) {
  const cols = levels.map(l => ({ key: l.key, sign: l.dir === 'desc' ? -1 : 1, text: MAT_SORT_COLUMN_BY_KEY.get(l.key).kind !== 'num' }));
  const keyed = rows.map(m => {
    const entry = entryOf(m);
    const latest = latestOf(m);
    return { m, latest, openValue: entry ? entry.openValue : 0, vals: cols.map(c => matSortValue(c.key, m, entry, latest)) };
  });
  keyed.sort((a, b) => {
    for (let i = 0; i < cols.length; i++) {
      const x = a.vals[i];
      const y = b.vals[i];
      if (x == null && y == null) continue;
      if (x == null) return 1;
      if (y == null) return -1;
      const d = cols[i].text ? String(x).localeCompare(String(y), 'en', { sensitivity: 'base', numeric: true }) : x - y;
      if (d) return d * cols[i].sign;
    }
    return (b.latest || '').localeCompare(a.latest || '') || ((b.m.qty || 0) - (a.m.qty || 0)) || (b.openValue - a.openValue);
  });
  return keyed.map(k => k.m);
}

// ── Markup ──
function sortDirLabel(key, dir) {
  const kind = MAT_SORT_COLUMN_BY_KEY.get(key).kind;
  if (kind === 'text') return dir === 'asc' ? 'A to Z' : 'Z to A';
  if (kind === 'date') return dir === 'asc' ? 'Oldest first' : 'Newest first';
  return dir === 'asc' ? 'Smallest first' : 'Largest first';
}

function sortSummaryText(levels) {
  return levels.map((l, i) => (i ? 'then ' : '') + MAT_SORT_COLUMN_BY_KEY.get(l.key).label + ' (' + sortDirLabel(l.key, l.dir) + ')').join(', ');
}

function currentUserPreset() {
  return (MAT_USER_PRESETS || []).find(p => p.id === MAT_SORT_STATE.presetId) || null;
}

// A table header's label, clickable when the column sorts: an arrow shows
// the direction and, in a multi-level sort, which level it is.
function matSortHeaderHtml(label, key) {
  if (!key) return escapeHtml(label);
  const idx = MAT_SORT_STATE.levels.findIndex(l => l.key === key);
  const level = idx >= 0 ? MAT_SORT_STATE.levels[idx] : null;
  const arrow = level ? '<span class="sort-arrow">' + (level.dir === 'asc' ? '&uarr;' : '&darr;') + (MAT_SORT_STATE.levels.length > 1 ? '<sup>' + (idx + 1) + '</sup>' : '') + '</span>' : '';
  const tip = 'Sort by ' + label + (level && MAT_SORT_STATE.levels.length === 1 ? ' - click again to reverse' : '');
  return '<button type="button" class="sort-header-btn' + (level ? ' active' : '') + '" data-matsort="' + escapeHtml(key) + '" title="' + escapeHtml(tip) + '">' + escapeHtml(label) + arrow + '</button>';
}

function matSortSelectHtml() {
  const selected = String(MAT_SORT_STATE.presetId);
  const opt = p => '<option value="' + escapeHtml(String(p.id)) + '"' + (String(p.id) === selected ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>';
  let mine;
  if (MAT_USER_PRESETS_ERROR) mine = '<optgroup label="My presets"><option disabled>Could not load your presets - refresh to retry</option></optgroup>';
  else if (!MAT_USER_PRESETS || !MAT_USER_PRESETS.length) mine = '<optgroup label="My presets"><option disabled>None saved yet - use Custom sort to save one</option></optgroup>';
  else mine = '<optgroup label="My presets">' + MAT_USER_PRESETS.map(opt).join('') + '</optgroup>';
  const custom = selected === 'custom' ? '<option value="custom" selected>Custom: ' + escapeHtml(sortSummaryText(MAT_SORT_STATE.levels)) + '</option>' : '';
  return '<select id="matSortSelect" class="mat-sort-select" aria-label="Sort materials by">' + custom +
    '<optgroup label="Built-in">' + MAT_BUILTIN_SORTS.map(opt).join('') + '</optgroup>' + mine + '</select>';
}

function matSortEditorHtml() {
  const draft = MAT_SORT_STATE.draft;
  const levelRows = draft.map((l, i) => {
    const used = new Set(draft.filter((_, j) => j !== i).map(x => x.key));
    const colOpts = MAT_SORT_COLUMNS.filter(c => !used.has(c.key) || c.key === l.key)
      .map(c => '<option value="' + c.key + '"' + (c.key === l.key ? ' selected' : '') + '>' + escapeHtml(c.label) + '</option>').join('');
    const dirOpts = ['asc', 'desc'].map(d => '<option value="' + d + '"' + (d === l.dir ? ' selected' : '') + '>' + escapeHtml(sortDirLabel(l.key, d)) + '</option>').join('');
    return '<div class="mat-sort-level">' +
      '<span class="mat-sort-level-label">' + (i ? 'Then by' : 'Sort by') + '</span>' +
      '<select data-sortlevel-key="' + i + '" aria-label="Sort level ' + (i + 1) + ' column">' + colOpts + '</select>' +
      '<select data-sortlevel-dir="' + i + '" aria-label="Sort level ' + (i + 1) + ' direction">' + dirOpts + '</select>' +
      '<button type="button" class="page-btn" data-sortlevel-up="' + i + '"' + (i === 0 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' up">&uarr;</button>' +
      '<button type="button" class="page-btn" data-sortlevel-down="' + i + '"' + (i === draft.length - 1 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' down">&darr;</button>' +
      '<button type="button" class="page-btn" data-sortlevel-remove="' + i + '"' + (draft.length === 1 ? ' disabled' : '') + ' aria-label="Remove level ' + (i + 1) + '">Remove</button>' +
    '</div>';
  }).join('');
  const mine = currentUserPreset();
  return '<div class="mat-sort-editor" role="group" aria-label="Custom sort">' +
    levelRows +
    '<button type="button" class="page-btn" id="matSortAddLevel"' + (draft.length >= Math.min(MAT_SORT_MAX_LEVELS, MAT_SORT_COLUMNS.length) ? ' disabled' : '') + '>+ Add level</button>' +
    '<div class="mat-sort-actions">' +
      '<button type="button" class="view-all-btn" id="matSortApply">Apply</button>' +
      '<input type="text" id="matSortPresetName" maxlength="60" placeholder="Preset name" aria-label="Preset name" value="' + escapeHtml(MAT_SORT_STATE.draftName) + '">' +
      '<button type="button" class="page-btn" id="matSortSave">Save as preset</button>' +
      (mine ? '<button type="button" class="page-btn" id="matSortDelete">Delete "' + escapeHtml(mine.name) + '"</button>' : '') +
      '<button type="button" class="page-btn" id="matSortClose">Close</button>' +
    '</div>' +
    '<div class="mat-sort-msg" id="matSortMsg" role="status">' + escapeHtml(MAT_SORT_STATE.msg) + '</div>' +
  '</div>';
}

function matSortBarHtml() {
  return '<div class="mat-sort-bar">' +
    '<label class="mat-sort-label" for="matSortSelect">Sort by</label>' + matSortSelectHtml() +
    '<button type="button" class="page-btn" id="matSortEditBtn" aria-expanded="' + MAT_SORT_STATE.editorOpen + '">' + (MAT_SORT_STATE.editorOpen ? 'Hide custom sort' : 'Custom sort...') + '</button>' +
    '<span class="mat-sort-summary">' + escapeHtml(sortSummaryText(MAT_SORT_STATE.levels)) + '</span>' +
  '</div>' + (MAT_SORT_STATE.editorOpen ? matSortEditorHtml() : '');
}

// ── Wiring ── (called by wireMaterialsListRegion() after every region render)
function wireMaterialSort(region) {
  const rerender = () => renderMaterialsListRegion();
  const select = document.getElementById('matSortSelect');
  if (select) select.addEventListener('change', () => {
    const all = MAT_BUILTIN_SORTS.concat(MAT_USER_PRESETS || []);
    const preset = all.find(p => String(p.id) === select.value);
    if (!preset) return;
    setMatSort(preset.levels, preset.id);
    if (MAT_SORT_STATE.editorOpen) { MAT_SORT_STATE.draft = cloneSortLevels(preset.levels); MAT_SORT_STATE.draftName = currentUserPreset() ? preset.name : ''; MAT_SORT_STATE.msg = ''; }
    rerender();
  });

  region.querySelectorAll('[data-matsort]').forEach(btn => btn.addEventListener('click', () => {
    const key = btn.dataset.matsort;
    const col = MAT_SORT_COLUMN_BY_KEY.get(key);
    if (!col) return;
    const only = MAT_SORT_STATE.levels.length === 1 && MAT_SORT_STATE.levels[0].key === key;
    const dir = only ? (MAT_SORT_STATE.levels[0].dir === 'asc' ? 'desc' : 'asc') : col.dir;
    setMatSort([{ key, dir }]);
    rerender();
  }));

  const editBtn = document.getElementById('matSortEditBtn');
  if (editBtn) editBtn.addEventListener('click', () => {
    MAT_SORT_STATE.editorOpen = !MAT_SORT_STATE.editorOpen;
    if (MAT_SORT_STATE.editorOpen) {
      MAT_SORT_STATE.draft = cloneSortLevels(MAT_SORT_STATE.levels);
      const mine = currentUserPreset();
      MAT_SORT_STATE.draftName = mine ? mine.name : '';
      MAT_SORT_STATE.msg = '';
    }
    rerender();
  });
  if (!MAT_SORT_STATE.editorOpen) return;

  const draft = MAT_SORT_STATE.draft;
  const say = text => { MAT_SORT_STATE.msg = text; const el = document.getElementById('matSortMsg'); if (el) el.textContent = text; };
  region.querySelectorAll('[data-sortlevel-key]').forEach(sel => sel.addEventListener('change', () => {
    const l = draft[Number(sel.dataset.sortlevelKey)];
    l.key = sel.value;
    l.dir = MAT_SORT_COLUMN_BY_KEY.get(sel.value).dir;
    rerender(); // direction wording and the other levels' column lists change
  }));
  region.querySelectorAll('[data-sortlevel-dir]').forEach(sel => sel.addEventListener('change', () => {
    draft[Number(sel.dataset.sortlevelDir)].dir = sel.value;
  }));
  const move = (i, j) => { const t = draft[i]; draft[i] = draft[j]; draft[j] = t; rerender(); };
  region.querySelectorAll('[data-sortlevel-up]').forEach(b => b.addEventListener('click', () => { const i = Number(b.dataset.sortlevelUp); if (i > 0) move(i, i - 1); }));
  region.querySelectorAll('[data-sortlevel-down]').forEach(b => b.addEventListener('click', () => { const i = Number(b.dataset.sortlevelDown); if (i < draft.length - 1) move(i, i + 1); }));
  region.querySelectorAll('[data-sortlevel-remove]').forEach(b => b.addEventListener('click', () => {
    if (draft.length > 1) { draft.splice(Number(b.dataset.sortlevelRemove), 1); rerender(); }
  }));
  const addBtn = document.getElementById('matSortAddLevel');
  if (addBtn) addBtn.addEventListener('click', () => {
    const used = new Set(draft.map(l => l.key));
    const next = MAT_SORT_COLUMNS.find(c => !used.has(c.key));
    if (next && draft.length < MAT_SORT_MAX_LEVELS) { draft.push({ key: next.key, dir: next.dir }); rerender(); }
  });

  const nameInput = document.getElementById('matSortPresetName');
  if (nameInput) nameInput.addEventListener('input', () => { MAT_SORT_STATE.draftName = nameInput.value; });

  const applyBtn = document.getElementById('matSortApply');
  if (applyBtn) applyBtn.addEventListener('click', () => {
    setMatSort(draft);
    MAT_SORT_STATE.editorOpen = false;
    rerender();
  });
  const closeBtn = document.getElementById('matSortClose');
  if (closeBtn) closeBtn.addEventListener('click', () => { MAT_SORT_STATE.editorOpen = false; rerender(); });

  const saveBtn = document.getElementById('matSortSave');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const name = (MAT_SORT_STATE.draftName || '').trim();
    if (!name) { say('Type a name for the preset first.'); nameInput && nameInput.focus(); return; }
    const clash = (MAT_USER_PRESETS || []).find(p => p.name.toLowerCase() === name.replace(/\s+/g, ' ').toLowerCase());
    if (clash && !window.confirm('You already have a preset called "' + clash.name + '". Replace it with this sort?')) return;
    saveBtn.disabled = true;
    say('Saving...');
    try {
      const saved = await sortPresetApi('', jsonBody('POST', { view: 'materials', name, levels: draft }));
      if (!MAT_USER_PRESETS) MAT_USER_PRESETS = [];
      MAT_USER_PRESETS = MAT_USER_PRESETS.filter(p => p.id !== saved.id).concat([saved]);
      sortUserPresets();
      setMatSort(saved.levels, saved.id);
      MAT_SORT_STATE.draftName = saved.name;
      MAT_SORT_STATE.msg = 'Saved "' + saved.name + '" to your account.';
      rerender();
    } catch (e) {
      saveBtn.disabled = false;
      say(e.message || 'Could not save the preset.');
    }
  });

  const deleteBtn = document.getElementById('matSortDelete');
  if (deleteBtn) deleteBtn.addEventListener('click', async () => {
    const mine = currentUserPreset();
    if (!mine || !window.confirm('Delete the preset "' + mine.name + '"? The table keeps its current order.')) return;
    deleteBtn.disabled = true;
    try {
      await sortPresetApi('/' + encodeURIComponent(mine.id), { method: 'DELETE' });
      MAT_USER_PRESETS = MAT_USER_PRESETS.filter(p => p.id !== mine.id);
      setMatSort(MAT_SORT_STATE.levels);
      MAT_SORT_STATE.draftName = '';
      MAT_SORT_STATE.msg = 'Deleted "' + mine.name + '".';
      rerender();
    } catch (e) {
      deleteBtn.disabled = false;
      say(e.message || 'Could not delete the preset.');
    }
  });
}
