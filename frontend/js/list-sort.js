// ── Shared list sorting and sort presets (2026-09-28) ────────────────────
// Project owner: sort by Category and Sub Category, plus "presets like we
// have in Excel and Sheets", saved to the user's account - first on Raw
// Material Analysis, then Purchase Orders, Import Purchases, and the tables
// inside the Raw Material and PO modals. One engine, one configuration per
// list: material-sort.js (MAT_SORT, and the Raw Material modal's
// MAT_LOTS_SORT and MAT_OPEN_PO_SORT), po-sort.js (PO_SORT), import-sort.js
// (IMPORT_SORT) and po-reconcile.js (the PO modals' PO_LINES_SORT and
// PO_RECEIPTS_SORT) each call createListSort() with their own columns,
// built-in sorts and row values.
//
// Three ways in, one sort state (sorter.state.levels):
//   - the "Sort by" select: built-in sorts and the user's own presets;
//   - a click on a sortable column header (sorter.headerHtml()): that one
//     column, clicking again flips its direction;
//   - "Custom sort...": an Excel-style editor that stacks up to
//     LIST_SORT_MAX_LEVELS levels ("Sort by X, then by Y"), applied or saved
//     as a named preset.
//
// PICKING VALUES - down to the finest grain (2026-09-28, project owner: "go
// to least granularity... select the category too and then subcategory
// too", left to us to make it useful for pinning down a PO, material or
// vendor). A level on a column marked `pick` (Category, Sub Category,
// Vendor, Material, Plant...) can also name values: those rows come first,
// in the picked order, and the rest follow in the level's direction. Ticking
// "Show only these" also narrows the list to rows holding a picked value. A
// column with a `parent` (Sub Category under Category) only offers values
// found in the rows the parent level's picks select, so the choice narrows
// Category -> Sub Category -> Material like drilling into a tree. A level is
// { key, dir, values?: [...], only?: true }, and presets save all of it.
//
// A sort is TABLE-ONLY, like a header text search: it never changes a KPI,
// filter or chart, so every change calls the list's own region re-render
// (cfg.rerender), never the whole view. "Show only these" is table-only too.
//
// Saved presets live on the server per user (GET/POST/PATCH/DELETE
// /api/sort-presets?view=<cfg.view>, apps/api/routers/preferences_views.py),
// so they follow the user to any device. The sort in use is remembered in
// this browser only (localStorage) - a per-viewer convenience that may come
// back empty.
//
// Each list's column keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW[cfg.view], and its `pick` columns PICK_KEYS_BY_VIEW;
// test_sort_presets.py checks they agree.

const LIST_SORT_MAX_LEVELS = 5;
// Mirrors sort_presets.py's MAX_PICKED_VALUES / MAX_PICKED_VALUE_LENGTH.
const LIST_SORT_MAX_PICKS = 50;
const LIST_SORT_MAX_PICK_LENGTH = 200;

// Direction wording per column kind. `rank` is an ordered code (e.g. PO
// status, most urgent = 0); `stage` too, in the order a shipment moves
// through (import shipment stage).
const LIST_SORT_DIR_LABELS = {
  text: { asc: 'A to Z', desc: 'Z to A' },
  date: { asc: 'Oldest first', desc: 'Newest first' },
  num: { asc: 'Smallest first', desc: 'Largest first' },
  rank: { asc: 'Most urgent first', desc: 'Least urgent first' },
  stage: { asc: 'Earliest stage first', desc: 'Latest stage first' },
};

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

function sameSortLevels(a, b) {
  return a.length === b.length && a.every((l, i) => l.key === b[i].key && l.dir === b[i].dir
    && !!l.only === !!b[i].only
    && (l.values || []).length === (b[i].values || []).length
    && (l.values || []).every((v, j) => v === b[i].values[j]));
}

// A copy with only the fields a level may carry: `values` and `only` only
// when values were picked, so an empty pick never tells two equal sorts
// apart.
function cloneSortLevels(levels) {
  return levels.map(l => {
    const out = { key: l.key, dir: l.dir };
    if (l.values && l.values.length) {
      out.values = l.values.slice();
      if (l.only) out.only = true;
    }
    return out;
  });
}

/**
 * cfg: {
 *   view       - the server's view name ('materials', 'purchase_orders', ...)
 *   idPrefix   - prefix for this list's element ids ('mat', 'po', ...)
 *   columns    - [{ key, label, kind: 'text'|'num'|'date'|'rank'|'stage', dir,
 *                pick?, parent? }]: `dir` is the direction a header click
 *                starts with; `pick` lets a level name values; `parent` is
 *                the column whose picks scope this one's options
 *   builtins   - [{ id: 'builtin:*', name, levels }], the first is the default
 *   rowValue   - (key, row, extra) => the value a row sorts on; null sorts
 *                LAST in either direction, the way a spreadsheet keeps
 *                blanks at the bottom
 *   pickValues - optional (key, row, extra) => the values a row holds for a
 *                `pick` column, as an array (a PO can hold several
 *                categories), or null to use [rowValue]
 *   scopedPickValues - optional (key, row, ancestorPicks, extra) => the
 *                row's values for `key` that sit under the ancestor levels'
 *                picks ({ category: [...], ... }), for the editor's choices;
 *                null to fall back to pickValues
 *   tieBreak   - (a, b, extra) => number, for rows every level ties on
 *   onChange   - optional, called on every sort change (reset the page)
 *   rerender   - () => void, the list's region re-render (or, for a table
 *                whose rows carry their own listeners, a re-order in place)
 *   barLabel   - optional label for the select, default "Sort by"
 * }
 */
function createListSort(cfg) {
  const columnByKey = new Map(cfg.columns.map(c => [c.key, c]));
  const storageKey = 'pt.sort.' + cfg.view + '.v1';
  const defaultSort = cfg.builtins[0];
  const id = name => cfg.idPrefix + name;
  // Every control this sorter renders carries data-sorter="<idPrefix>", and
  // wire() binds only those, so two sorters on one page (the Raw Material
  // modal's two tables, a PO modal's lines and receipts) never bind each
  // other's buttons, and wire() can be handed any ancestor.
  const own = ' data-sorter="' + cfg.idPrefix + '"';
  const pickValuesOf = (key, row, extra) => {
    if (cfg.pickValues) {
      const vals = cfg.pickValues(key, row, extra);
      if (vals) return vals;
    }
    const v = cfg.rowValue(key, row, extra);
    return v == null ? [] : [String(v)];
  };

  // presets: null until loaded; [] when the user has none. presetsError: the
  // load failed - the list still works, only the user's own presets are
  // missing, and the select says so. levels/presetId: the sort in use
  // ('custom' when it matches no preset). editorOpen/draft/draftName/msg:
  // the Custom sort editor, kept here so a region re-render never loses a
  // half-built sort. rows/extra: what the last sortRows() call was handed,
  // before "Show only these" - the editor lists pickable values from it.
  const state = {
    levels: cloneSortLevels(defaultSort.levels), presetId: defaultSort.id,
    presets: null, presetsError: false,
    editorOpen: false, draft: null, draftName: '', msg: '',
    rows: [], extra: undefined,
  };

  function validLevels(levels) {
    if (!Array.isArray(levels) || !levels.length || levels.length > LIST_SORT_MAX_LEVELS) return false;
    const seen = new Set();
    return levels.every(l => {
      if (!l || !columnByKey.has(l.key) || (l.dir !== 'asc' && l.dir !== 'desc') || seen.has(l.key)) return false;
      seen.add(l.key);
      if (l.values === undefined) return !l.only;
      return !!columnByKey.get(l.key).pick && Array.isArray(l.values) && l.values.length <= LIST_SORT_MAX_PICKS
        && l.values.every(v => typeof v === 'string' && v && v.length <= LIST_SORT_MAX_PICK_LENGTH)
        && new Set(l.values).size === l.values.length;
    });
  }

  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (stored && validLevels(stored.levels)) {
      state.levels = cloneSortLevels(stored.levels);
      state.presetId = typeof stored.presetId === 'string' || typeof stored.presetId === 'number' ? stored.presetId : 'custom';
    }
  } catch (e) { /* storage blocked or corrupt: the default sort stands */ }

  function store() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({ levels: state.levels, presetId: state.presetId }));
    } catch (e) { /* storage blocked: the sort still applies for this visit */ }
  }

  function allPresets() {
    return cfg.builtins.concat(state.presets || []);
  }

  // The preset (built-in or saved) whose levels equal `levels`, preferring
  // the one already selected, so a header click that lands on "Category (A
  // to Z)" shows that name rather than "Custom".
  function presetIdForLevels(levels) {
    const current = allPresets().find(p => String(p.id) === String(state.presetId));
    if (current && sameSortLevels(current.levels, levels)) return current.id;
    const found = allPresets().find(p => sameSortLevels(p.levels, levels));
    return found ? found.id : 'custom';
  }

  function currentUserPreset() {
    return (state.presets || []).find(p => p.id === state.presetId) || null;
  }

  function sortUserPresets() {
    state.presets.sort((a, b) => a.name.localeCompare(b.name, 'en', { sensitivity: 'base' }));
  }

  // Every sort change goes through here: back to page 1, remembered.
  function set(levels, presetId) {
    state.levels = cloneSortLevels(levels);
    state.presetId = presetId != null ? presetId : presetIdForLevels(state.levels);
    if (cfg.onChange) cfg.onChange();
    store();
  }

  // Loaded once per page visit; never throws - a failed load leaves the
  // built-in sorts working and says so in the select.
  async function ensurePresetsLoaded() {
    if (state.presets) return;
    try {
      const data = await sortPresetApi('?view=' + encodeURIComponent(cfg.view));
      state.presets = (data.presets || []).filter(p => validLevels(p.levels));
      state.presetsError = false;
      sortUserPresets();
      // A remembered preset id deleted on another device reads as Custom.
      if (!allPresets().some(p => p.id === state.presetId)) state.presetId = presetIdForLevels(state.levels);
    } catch (e) {
      console.error('Loading sort presets failed (' + cfg.view + '):', e);
      state.presets = null;
      state.presetsError = true;
    }
  }

  // Does `row` hold one of `level`'s picked values?
  function rowHasPick(level, row, extra) {
    const wanted = new Set(level.values);
    return pickValuesOf(level.key, row, extra).some(v => wanted.has(v));
  }

  // `rows` narrowed by every level's "Show only these", then sorted by the
  // levels in use. Values are computed once per row, not once per
  // comparison. A level with picked values puts rows holding one first, in
  // the picked order (a row holding several ranks by its earliest), and
  // sorts the rest by value. Text compares case-insensitively and
  // numerically ("PO 9" before "PO 10"); a row with no value for a level
  // sorts after every row that has one, in either direction.
  function sortRows(rows, extra) {
    state.rows = rows;
    state.extra = extra;
    let kept = rows;
    state.levels.filter(l => l.only && l.values && l.values.length).forEach(l => {
      kept = kept.filter(row => rowHasPick(l, row, extra));
    });
    const cols = state.levels.map(l => {
      const kind = columnByKey.get(l.key).kind;
      return {
        key: l.key, sign: l.dir === 'desc' ? -1 : 1, text: kind === 'text' || kind === 'date',
        order: l.values && l.values.length ? new Map(l.values.map((v, i) => [v, i])) : null,
      };
    });
    const keyed = kept.map(row => ({
      row,
      vals: cols.map(c => cfg.rowValue(c.key, row, extra)),
      ranks: cols.map(c => {
        if (!c.order) return 0;
        const hits = pickValuesOf(c.key, row, extra).map(v => c.order.get(v)).filter(i => i != null);
        return hits.length ? Math.min(...hits) : c.order.size;
      }),
    }));
    keyed.sort((a, b) => {
      for (let i = 0; i < cols.length; i++) {
        if (a.ranks[i] !== b.ranks[i]) return a.ranks[i] - b.ranks[i];
        const x = a.vals[i];
        const y = b.vals[i];
        if (x == null && y == null) continue;
        if (x == null) return 1;
        if (y == null) return -1;
        const d = cols[i].text ? String(x).localeCompare(String(y), 'en', { sensitivity: 'base', numeric: true }) : x - y;
        if (d) return d * cols[i].sign;
      }
      return cfg.tieBreak(a.row, b.row, extra);
    });
    return keyed.map(k => k.row);
  }

  // Is any "Show only these" narrowing the list right now?
  function isNarrowing() {
    return state.levels.some(l => l.only && l.values && l.values.length);
  }

  // The values a draft level could pick, most common first, with a row count
  // each. A column with a `parent` counts only rows its ancestors' picks
  // select (Material under Sub Category under Category), so each level
  // offers only what sits under the choices above it.
  function pickOptions(draft, level) {
    let rows = state.rows;
    const ancestorPicks = {};
    const seen = new Set([level.key]);
    for (let parent = columnByKey.get(level.key).parent; parent && !seen.has(parent); parent = (columnByKey.get(parent) || {}).parent) {
      seen.add(parent);
      const parentLevel = draft.find(l => l.key === parent && l.values && l.values.length);
      if (!parentLevel) continue;
      ancestorPicks[parent] = parentLevel.values;
      rows = rows.filter(row => rowHasPick(parentLevel, row, state.extra));
    }
    // A row holding several values (a PO's lines) can say which of them sit
    // under the ancestors' picks (cfg.scopedPickValues), so a PO ordering a
    // polymer and an oil offers only the polymer's sub category under
    // "Polymers".
    const valuesOf = row => {
      const scoped = cfg.scopedPickValues && Object.keys(ancestorPicks).length ? cfg.scopedPickValues(level.key, row, ancestorPicks, state.extra) : null;
      return scoped || pickValuesOf(level.key, row, state.extra);
    };
    const counts = new Map();
    rows.forEach(row => new Set(valuesOf(row)).forEach(v => counts.set(v, (counts.get(v) || 0) + 1)));
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'en', { sensitivity: 'base', numeric: true }));
  }

  function dirLabel(key, dir) {
    return LIST_SORT_DIR_LABELS[columnByKey.get(key).kind][dir];
  }

  function levelText(l) {
    const label = columnByKey.get(l.key).label;
    if (!(l.values && l.values.length)) return label + ' (' + dirLabel(l.key, l.dir) + ')';
    const shown = l.values.slice(0, 3).join(', ') + (l.values.length > 3 ? ' +' + (l.values.length - 3) + ' more' : '');
    return label + ': ' + shown + (l.only ? ' only' : ' first, then ' + dirLabel(l.key, l.dir));
  }

  function summaryText(levels) {
    return levels.map((l, i) => (i ? 'then ' : '') + levelText(l)).join(', ');
  }

  // A table header's label, clickable when the column sorts: an arrow shows
  // the direction and, in a multi-level sort, which level it is.
  function headerHtml(label, key) {
    if (!key) return escapeHtml(label);
    const idx = state.levels.findIndex(l => l.key === key);
    const level = idx >= 0 ? state.levels[idx] : null;
    const arrow = level ? '<span class="sort-arrow">' + (level.dir === 'asc' ? '&uarr;' : '&darr;') + (state.levels.length > 1 ? '<sup>' + (idx + 1) + '</sup>' : '') + '</span>' : '';
    const tip = 'Sort by ' + label + (level && state.levels.length === 1 ? ' - click again to reverse' : '');
    return '<button type="button" class="sort-header-btn' + (level ? ' active' : '') + '"' + own + ' data-listsort="' + escapeHtml(key) + '" title="' + escapeHtml(tip) + '">' + escapeHtml(label) + arrow + '</button>';
  }

  function selectHtml() {
    const selected = String(state.presetId);
    const opt = p => '<option value="' + escapeHtml(String(p.id)) + '"' + (String(p.id) === selected ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>';
    let mine;
    if (state.presetsError) mine = '<optgroup label="My presets"><option disabled>Could not load your presets - refresh to retry</option></optgroup>';
    else if (!state.presets || !state.presets.length) mine = '<optgroup label="My presets"><option disabled>None saved yet - use Custom sort to save one</option></optgroup>';
    else mine = '<optgroup label="My presets">' + state.presets.map(opt).join('') + '</optgroup>';
    const custom = selected === 'custom' ? '<option value="custom" selected>Custom: ' + escapeHtml(summaryText(state.levels)) + '</option>' : '';
    return '<select id="' + id('SortSelect') + '" class="list-sort-select" aria-label="' + escapeHtml(cfg.barLabel || 'Sort by') + '">' + custom +
      '<optgroup label="Built-in">' + cfg.builtins.map(opt).join('') + '</optgroup>' + mine + '</select>';
  }

  // The picked values of draft level `i`: chips in order (move earlier /
  // later, remove), an "add" select of what is left, and "Show only these".
  function picksHtml(draft, i) {
    const l = draft[i];
    const col = columnByKey.get(l.key);
    const values = l.values || [];
    const chips = values.map((v, j) => '<span class="list-sort-chip">' + escapeHtml(v) +
      '<button type="button"' + own + ' data-pick-move="' + i + ':' + j + ':-1"' + (j === 0 ? ' disabled' : '') + ' aria-label="Move ' + escapeHtml(v) + ' earlier">&larr;</button>' +
      '<button type="button"' + own + ' data-pick-move="' + i + ':' + j + ':1"' + (j === values.length - 1 ? ' disabled' : '') + ' aria-label="Move ' + escapeHtml(v) + ' later">&rarr;</button>' +
      '<button type="button"' + own + ' data-pick-remove="' + i + ':' + j + '" aria-label="Remove ' + escapeHtml(v) + '">&times;</button></span>').join('');
    const taken = new Set(values);
    const options = pickOptions(draft, l).filter(([v]) => !taken.has(v));
    const parent = col.parent && draft.some(x => x.key === col.parent && x.values && x.values.length) ? ' in the picked ' + columnByKey.get(col.parent).label.toLowerCase() : '';
    const add = values.length >= LIST_SORT_MAX_PICKS ? '' :
      '<select' + own + ' data-pick-add="' + i + '" aria-label="Pick a ' + escapeHtml(col.label) + ' to put first">' +
        '<option value="">' + (values.length ? '+ Then' : '+ Pick') + ' a ' + escapeHtml(col.label.toLowerCase()) + parent + '... (' + options.length + ')</option>' +
        options.map(([v, n]) => '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + ' (' + n + ')</option>').join('') +
      '</select>';
    const only = values.length
      ? '<label class="list-sort-only"><input type="checkbox"' + own + ' data-pick-only="' + i + '"' + (l.only ? ' checked' : '') + '> Show only these</label>'
      : '<span class="list-sort-hint">Picked values come first, in the order picked.</span>';
    return '<div class="list-sort-picks">' + chips + add + only + '</div>';
  }

  function editorHtml() {
    const draft = state.draft;
    const levelRows = draft.map((l, i) => {
      const used = new Set(draft.filter((_, j) => j !== i).map(x => x.key));
      const colOpts = cfg.columns.filter(c => !used.has(c.key) || c.key === l.key)
        .map(c => '<option value="' + c.key + '"' + (c.key === l.key ? ' selected' : '') + '>' + escapeHtml(c.label) + '</option>').join('');
      const hasPicks = l.values && l.values.length;
      const dirOpts = ['asc', 'desc'].map(d => '<option value="' + d + '"' + (d === l.dir ? ' selected' : '') + '>' + (hasPicks ? 'then ' : '') + escapeHtml(dirLabel(l.key, d)) + '</option>').join('');
      return '<div class="list-sort-level-wrap"><div class="list-sort-level">' +
        '<span class="list-sort-level-label">' + (i ? 'Then by' : 'Sort by') + '</span>' +
        '<select' + own + ' data-sortlevel-key="' + i + '" aria-label="Sort level ' + (i + 1) + ' column">' + colOpts + '</select>' +
        '<select' + own + ' data-sortlevel-dir="' + i + '" aria-label="Sort level ' + (i + 1) + ' direction">' + dirOpts + '</select>' +
        '<button type="button" class="page-btn"' + own + ' data-sortlevel-up="' + i + '"' + (i === 0 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' up">&uarr;</button>' +
        '<button type="button" class="page-btn"' + own + ' data-sortlevel-down="' + i + '"' + (i === draft.length - 1 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' down">&darr;</button>' +
        '<button type="button" class="page-btn"' + own + ' data-sortlevel-remove="' + i + '"' + (draft.length === 1 ? ' disabled' : '') + ' aria-label="Remove level ' + (i + 1) + '">Remove</button>' +
      '</div>' + (columnByKey.get(l.key).pick ? picksHtml(draft, i) : '') + '</div>';
    }).join('');
    const mine = currentUserPreset();
    return '<div class="list-sort-editor" role="group" aria-label="Custom sort">' +
      levelRows +
      '<button type="button" class="page-btn" id="' + id('SortAddLevel') + '"' + (draft.length >= Math.min(LIST_SORT_MAX_LEVELS, cfg.columns.length) ? ' disabled' : '') + '>+ Add level</button>' +
      '<div class="list-sort-actions">' +
        '<button type="button" class="view-all-btn" id="' + id('SortApply') + '">Apply</button>' +
        '<input type="text" id="' + id('SortPresetName') + '" maxlength="60" placeholder="Preset name" aria-label="Preset name" value="' + escapeHtml(state.draftName) + '">' +
        '<button type="button" class="page-btn" id="' + id('SortSave') + '">Save as preset</button>' +
        (mine ? '<button type="button" class="page-btn" id="' + id('SortDelete') + '">Delete "' + escapeHtml(mine.name) + '"</button>' : '') +
        '<button type="button" class="page-btn" id="' + id('SortClose') + '">Close</button>' +
      '</div>' +
      '<div class="list-sort-msg" id="' + id('SortMsg') + '" role="status">' + escapeHtml(state.msg) + '</div>' +
    '</div>';
  }

  function barHtml() {
    const narrowing = isNarrowing();
    return '<div class="list-sort-bar">' +
      '<label class="list-sort-label" for="' + id('SortSelect') + '">' + escapeHtml(cfg.barLabel || 'Sort by') + '</label>' + selectHtml() +
      '<button type="button" class="page-btn" id="' + id('SortEditBtn') + '" aria-expanded="' + state.editorOpen + '">' + (state.editorOpen ? 'Hide custom sort' : 'Custom sort...') + '</button>' +
      '<span class="list-sort-summary">' + escapeHtml(summaryText(state.levels)) + '</span>' +
      (narrowing ? '<button type="button" class="list-sort-narrowing" id="' + id('SortShowAll') + '" title="Rows without a picked value are hidden by this sort">Showing picked values only &middot; Show all &times;</button>' : '') +
    '</div>' + (state.editorOpen ? editorHtml() : '');
  }

  // A re-render replaces the sort controls, so the one that had keyboard
  // focus is found again by its id or data attribute and focused, rather
  // than dropping the reader back to the top of the page.
  const FOCUS_ATTRS = ['data-listsort', 'data-sortlevel-key', 'data-sortlevel-dir', 'data-sortlevel-up', 'data-sortlevel-down', 'data-sortlevel-remove', 'data-pick-add', 'data-pick-only'];
  function rerenderKeepingFocus() {
    const active = document.activeElement;
    let selector = null;
    if (active && active.id) selector = '#' + active.id;
    else if (active && active.getAttribute) {
      const attr = FOCUS_ATTRS.find(a => active.hasAttribute(a));
      if (attr) selector = '[data-sorter="' + cfg.idPrefix + '"][' + attr + '="' + active.getAttribute(attr) + '"]';
    }
    cfg.rerender();
    if (!selector) return;
    const again = document.querySelector(selector);
    if (again && again !== document.activeElement && !again.disabled) again.focus();
  }

  // Called by the list's own wiring after every region render.
  function wire(region) {
    const rerender = rerenderKeepingFocus;
    const q = sel => region.querySelectorAll('[data-sorter="' + cfg.idPrefix + '"]' + sel);
    const byId = name => document.getElementById(id(name));
    const select = byId('SortSelect');
    if (select) select.addEventListener('change', () => {
      const preset = allPresets().find(p => String(p.id) === select.value);
      if (!preset) return;
      set(preset.levels, preset.id);
      if (state.editorOpen) { state.draft = cloneSortLevels(preset.levels); state.draftName = currentUserPreset() ? preset.name : ''; state.msg = ''; }
      rerender();
    });

    q('[data-listsort]').forEach(btn => btn.addEventListener('click', () => {
      const key = btn.dataset.listsort;
      const col = columnByKey.get(key);
      if (!col) return;
      const only = state.levels.length === 1 && state.levels[0].key === key && !(state.levels[0].values || []).length;
      const dir = only ? (state.levels[0].dir === 'asc' ? 'desc' : 'asc') : col.dir;
      set([{ key, dir }]);
      rerender();
    }));

    // "Show all": drops every "Show only these", keeps the order.
    const showAll = byId('SortShowAll');
    if (showAll) showAll.addEventListener('click', () => {
      set(state.levels.map(l => Object.assign({}, l, { only: false })));
      if (state.editorOpen && state.draft) state.draft.forEach(l => { delete l.only; });
      rerender();
    });

    const editBtn = byId('SortEditBtn');
    if (editBtn) editBtn.addEventListener('click', () => {
      state.editorOpen = !state.editorOpen;
      if (state.editorOpen) {
        state.draft = cloneSortLevels(state.levels);
        const mine = currentUserPreset();
        state.draftName = mine ? mine.name : '';
        state.msg = '';
      }
      rerender();
    });
    if (!state.editorOpen) return;

    const draft = state.draft;
    const say = text => { state.msg = text; const el = byId('SortMsg'); if (el) el.textContent = text; };
    q('[data-sortlevel-key]').forEach(sel => sel.addEventListener('change', () => {
      const i = Number(sel.dataset.sortlevelKey);
      draft[i] = { key: sel.value, dir: columnByKey.get(sel.value).dir };
      rerender(); // direction wording, picks and the other levels' column lists change
    }));
    q('[data-sortlevel-dir]').forEach(sel => sel.addEventListener('change', () => {
      draft[Number(sel.dataset.sortlevelDir)].dir = sel.value;
    }));
    const move = (i, j) => { const t = draft[i]; draft[i] = draft[j]; draft[j] = t; rerender(); };
    q('[data-sortlevel-up]').forEach(b => b.addEventListener('click', () => { const i = Number(b.dataset.sortlevelUp); if (i > 0) move(i, i - 1); }));
    q('[data-sortlevel-down]').forEach(b => b.addEventListener('click', () => { const i = Number(b.dataset.sortlevelDown); if (i < draft.length - 1) move(i, i + 1); }));
    q('[data-sortlevel-remove]').forEach(b => b.addEventListener('click', () => {
      if (draft.length > 1) { draft.splice(Number(b.dataset.sortlevelRemove), 1); rerender(); }
    }));
    const addBtn = byId('SortAddLevel');
    if (addBtn) addBtn.addEventListener('click', () => {
      const used = new Set(draft.map(l => l.key));
      // A column whose parent is already a level comes next, so "+ Add
      // level" after Category offers Sub Category first.
      const next = cfg.columns.find(c => !used.has(c.key) && c.parent && used.has(c.parent)) || cfg.columns.find(c => !used.has(c.key));
      if (next && draft.length < LIST_SORT_MAX_LEVELS) { draft.push({ key: next.key, dir: next.dir }); rerender(); }
    });

    // Picking values on a level.
    q('[data-pick-add]').forEach(sel => sel.addEventListener('change', () => {
      if (!sel.value) return;
      const l = draft[Number(sel.dataset.pickAdd)];
      l.values = (l.values || []).concat([sel.value]).slice(0, LIST_SORT_MAX_PICKS);
      rerender();
    }));
    q('[data-pick-move]').forEach(b => b.addEventListener('click', () => {
      const [i, j, step] = b.dataset.pickMove.split(':').map(Number);
      const vals = draft[i].values;
      const k = j + step;
      if (!vals || k < 0 || k >= vals.length) return;
      const t = vals[j]; vals[j] = vals[k]; vals[k] = t;
      rerender();
    }));
    q('[data-pick-remove]').forEach(b => b.addEventListener('click', () => {
      const [i, j] = b.dataset.pickRemove.split(':').map(Number);
      const l = draft[i];
      l.values.splice(j, 1);
      if (!l.values.length) { delete l.values; delete l.only; }
      rerender();
    }));
    q('[data-pick-only]').forEach(box => box.addEventListener('change', () => {
      const l = draft[Number(box.dataset.pickOnly)];
      if (box.checked) l.only = true; else delete l.only;
    }));

    const nameInput = byId('SortPresetName');
    if (nameInput) nameInput.addEventListener('input', () => { state.draftName = nameInput.value; });

    const applyBtn = byId('SortApply');
    if (applyBtn) applyBtn.addEventListener('click', () => {
      set(draft);
      state.editorOpen = false;
      rerender();
    });
    const closeBtn = byId('SortClose');
    if (closeBtn) closeBtn.addEventListener('click', () => { state.editorOpen = false; rerender(); });

    const saveBtn = byId('SortSave');
    if (saveBtn) saveBtn.addEventListener('click', async () => {
      const name = (state.draftName || '').trim();
      if (!name) { say('Type a name for the preset first.'); if (nameInput) nameInput.focus(); return; }
      const clash = (state.presets || []).find(p => p.name.toLowerCase() === name.replace(/\s+/g, ' ').toLowerCase());
      if (clash && !window.confirm('You already have a preset called "' + clash.name + '". Replace it with this sort?')) return;
      saveBtn.disabled = true;
      say('Saving...');
      try {
        const saved = await sortPresetApi('', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view: cfg.view, name, levels: cloneSortLevels(draft) }) });
        state.presets = (state.presets || []).filter(p => p.id !== saved.id).concat([saved]);
        sortUserPresets();
        set(saved.levels, saved.id);
        state.draft = cloneSortLevels(saved.levels);
        state.draftName = saved.name;
        state.msg = 'Saved "' + saved.name + '" to your account.';
        rerender();
      } catch (e) {
        saveBtn.disabled = false;
        say(e.message || 'Could not save the preset.');
      }
    });

    const deleteBtn = byId('SortDelete');
    if (deleteBtn) deleteBtn.addEventListener('click', async () => {
      const mine = currentUserPreset();
      if (!mine || !window.confirm('Delete the preset "' + mine.name + '"? The table keeps its current order.')) return;
      deleteBtn.disabled = true;
      try {
        await sortPresetApi('/' + encodeURIComponent(mine.id), { method: 'DELETE' });
        state.presets = state.presets.filter(p => p.id !== mine.id);
        set(state.levels);
        state.draftName = '';
        state.msg = 'Deleted "' + mine.name + '".';
        rerender();
      } catch (e) {
        deleteBtn.disabled = false;
        say(e.message || 'Could not delete the preset.');
      }
    });
  }

  return { prefix: cfg.idPrefix, state, validLevels, set, ensurePresetsLoaded, sortRows, isNarrowing, headerHtml, barHtml, wire, summaryText };
}

// ── Sortable tables whose rows keep their own controls (the modals) ──
// The Raw Material and PO modals' tables carry edit pencils, dismiss links,
// "change MIR" and open-PO links on their rows, and re-running
// wireEditIcons() would reset a correction the reader is part-way through.
// So a sort change there MOVES the existing rows into the new order (and
// hides the ones "Show only these" drops) rather than rebuilding them; only
// the sort bars and header rows are redrawn. `registry` is a modal's own
// object, replaced on every modal render: registry[key] = { sorter, rows,
// index, headers }.

function sortedTableHeadHtml(sorter, headers) {
  return headers.map(h => '<th' + (h.attrs || '') + '>' + sorter.headerHtml(h.label, h.key || null) + '</th>').join('');
}

/** A table section: rows in the sorter's order, each tagged data-sort-row
 * with its index in `rows` (rows "Show only these" drops are rendered
 * hidden, so a later sort can bring them back). `opts`: wrapClass,
 * tableClass, tbodyFoot (a total row that stays last), tfoot. `rowHtml(row,
 * index)` must return a string starting "<tr". */
function sortedTableHtml(registry, key, sorter, rows, rowHtml, headers, opts) {
  opts = opts || {};
  const entry = { sorter, rows, headers, index: new Map(rows.map((r, i) => [r, i])) };
  registry[key] = entry;
  const sorted = sorter.sortRows(rows);
  const kept = new Set(sorted);
  const tagged = (r, hidden) => {
    const html = rowHtml(r, entry.index.get(r));
    return hidden ? html.replace(/^<tr/, '<tr hidden') : html;
  };
  return '<div class="' + (opts.wrapClass || 'table-wrap') + '" data-sort-table="' + escapeHtml(key) + '">' +
    '<table' + (opts.tableClass ? ' class="' + opts.tableClass + '"' : '') + '><thead><tr>' + sortedTableHeadHtml(sorter, headers) + '</tr></thead>' +
    '<tbody>' + sorted.map(r => tagged(r, false)).join('') + rows.filter(r => !kept.has(r)).map(r => tagged(r, true)).join('') +
      (opts.tbodyFoot || '') + '</tbody>' + (opts.tfoot || '') + '</table></div>';
}

/** A sorter's bar, placed anywhere in the modal; redrawn with its tables. */
function sortedTableBarHtml(sorter) {
  return '<div data-sort-bar="' + sorter.prefix + '">' + sorter.barHtml() + '</div>';
}

/** A sorter's rerender for these tables: redraw its bars and header rows,
 * move every one of its tables' rows into the new order (a tbody total row,
 * tagged data-sort-foot, stays last), hide what "Show only these" drops,
 * and re-wire the sorter's controls under `root`. */
function resortSortedTables(registry, sorter, root) {
  if (!root) return;
  Object.keys(registry).forEach(key => {
    const entry = registry[key];
    if (entry.sorter !== sorter) return;
    const section = root.querySelector('[data-sort-table="' + CSS.escape(key) + '"]');
    if (!section) return;
    section.querySelector('thead tr').innerHTML = sortedTableHeadHtml(sorter, entry.headers);
    const tbody = section.querySelector('tbody');
    const foot = tbody.querySelector('[data-sort-foot]');
    const byIndex = new Map();
    tbody.querySelectorAll('[data-sort-row]').forEach(tr => byIndex.set(Number(tr.dataset.sortRow), tr));
    const sorted = sorter.sortRows(entry.rows);
    const kept = new Set(sorted);
    sorted.concat(entry.rows.filter(r => !kept.has(r))).forEach(r => {
      const tr = byIndex.get(entry.index.get(r));
      if (!tr) return;
      tr.hidden = !kept.has(r);
      tbody.insertBefore(tr, foot);
    });
  });
  root.querySelectorAll('[data-sort-bar="' + sorter.prefix + '"]').forEach(bar => { bar.innerHTML = sorter.barHtml(); });
  sorter.wire(root);
}
