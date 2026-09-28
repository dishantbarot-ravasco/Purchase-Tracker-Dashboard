// ── Shared list sorting and sort presets (2026-09-28) ────────────────────
// Project owner: sort by Category and Sub Category, plus "presets like we
// have in Excel and Sheets", saved to the user's account - first on Raw
// Material Analysis, then the same on Purchase Orders. One engine, one
// configuration per list: material-sort.js (MAT_SORT) and po-sort.js
// (PO_SORT) and import-sort.js (IMPORT_SORT) each call createListSort() with their own columns, built-in
// sorts and row values.
//
// Three ways in, one sort state (sorter.state.levels):
//   - the "Sort by" select: built-in sorts and the user's own presets;
//   - a click on a sortable column header (sorter.headerHtml()): that one
//     column, clicking again flips its direction;
//   - "Custom sort...": an Excel-style editor that stacks up to
//     LIST_SORT_MAX_LEVELS levels ("Sort by X, then by Y"), applied or saved
//     as a named preset.
//
// A sort is TABLE-ONLY, like a header text search: it never changes a KPI,
// filter or chart, so every change calls the list's own region re-render
// (cfg.rerender), never the whole view.
//
// Saved presets live on the server per user (GET/POST/PATCH/DELETE
// /api/sort-presets?view=<cfg.view>, apps/api/routers/preferences_views.py),
// so they follow the user to any device. The sort in use is remembered in
// this browser only (localStorage) - a per-viewer convenience that may come
// back empty.
//
// Each list's column keys must equal apps/services/sort_presets.py's
// SORT_KEYS_BY_VIEW[cfg.view]; test_sort_presets.py checks they agree.

const LIST_SORT_MAX_LEVELS = 5;

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
  return a.length === b.length && a.every((l, i) => l.key === b[i].key && l.dir === b[i].dir);
}

function cloneSortLevels(levels) {
  return levels.map(l => ({ key: l.key, dir: l.dir }));
}

/**
 * cfg: {
 *   view      - the server's view name ('materials', 'purchase_orders',
 *               'import_purchases')
 *   idPrefix  - prefix for this list's element ids ('mat', 'po')
 *   columns   - [{ key, label, kind: 'text'|'num'|'date'|'rank'|'stage', dir }], `dir`
 *               being the direction a header click starts with
 *   builtins  - [{ id: 'builtin:*', name, levels }], the first is the default
 *   rowValue  - (key, row, extra) => the value a row sorts on; null sorts
 *               LAST in either direction, the way a spreadsheet keeps blanks
 *               at the bottom
 *   tieBreak  - (a, b) => number over two rows, for rows every level ties on
 *   rerender  - () => void, the list's region re-render
 * }
 */
function createListSort(cfg) {
  const columnByKey = new Map(cfg.columns.map(c => [c.key, c]));
  const storageKey = 'pt.sort.' + cfg.view + '.v1';
  const defaultSort = cfg.builtins[0];
  const id = name => cfg.idPrefix + name;

  // presets: null until loaded; [] when the user has none. presetsError: the
  // load failed - the list still works, only the user's own presets are
  // missing, and the select says so. levels/presetId: the sort in use
  // ('custom' when it matches no preset). editorOpen/draft/draftName/msg:
  // the Custom sort editor, kept here so a region re-render never loses a
  // half-built sort.
  const state = {
    levels: cloneSortLevels(defaultSort.levels), presetId: defaultSort.id,
    presets: null, presetsError: false,
    editorOpen: false, draft: null, draftName: '', msg: '',
  };

  function validLevels(levels) {
    if (!Array.isArray(levels) || !levels.length || levels.length > LIST_SORT_MAX_LEVELS) return false;
    const seen = new Set();
    return levels.every(l => l && columnByKey.has(l.key) && (l.dir === 'asc' || l.dir === 'desc') && !seen.has(l.key) && seen.add(l.key));
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
    state.presetId = presetId != null ? presetId : presetIdForLevels(levels);
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

  // `rows` sorted by the levels in use. Values are computed once per row,
  // not once per comparison. Text compares case-insensitively and
  // numerically ("PO 9" before "PO 10"); a row with no value for a level
  // sorts after every row that has one, in either direction.
  function sortRows(rows, extra) {
    const cols = state.levels.map(l => ({ key: l.key, sign: l.dir === 'desc' ? -1 : 1, text: columnByKey.get(l.key).kind === 'text' || columnByKey.get(l.key).kind === 'date' }));
    const keyed = rows.map(row => ({ row, vals: cols.map(c => cfg.rowValue(c.key, row, extra)) }));
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
      return cfg.tieBreak(a.row, b.row, extra);
    });
    return keyed.map(k => k.row);
  }

  function dirLabel(key, dir) {
    return LIST_SORT_DIR_LABELS[columnByKey.get(key).kind][dir];
  }

  function summaryText(levels) {
    return levels.map((l, i) => (i ? 'then ' : '') + columnByKey.get(l.key).label + ' (' + dirLabel(l.key, l.dir) + ')').join(', ');
  }

  // A table header's label, clickable when the column sorts: an arrow shows
  // the direction and, in a multi-level sort, which level it is.
  function headerHtml(label, key) {
    if (!key) return escapeHtml(label);
    const idx = state.levels.findIndex(l => l.key === key);
    const level = idx >= 0 ? state.levels[idx] : null;
    const arrow = level ? '<span class="sort-arrow">' + (level.dir === 'asc' ? '&uarr;' : '&darr;') + (state.levels.length > 1 ? '<sup>' + (idx + 1) + '</sup>' : '') + '</span>' : '';
    const tip = 'Sort by ' + label + (level && state.levels.length === 1 ? ' - click again to reverse' : '');
    return '<button type="button" class="sort-header-btn' + (level ? ' active' : '') + '" data-listsort="' + escapeHtml(key) + '" title="' + escapeHtml(tip) + '">' + escapeHtml(label) + arrow + '</button>';
  }

  function selectHtml() {
    const selected = String(state.presetId);
    const opt = p => '<option value="' + escapeHtml(String(p.id)) + '"' + (String(p.id) === selected ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>';
    let mine;
    if (state.presetsError) mine = '<optgroup label="My presets"><option disabled>Could not load your presets - refresh to retry</option></optgroup>';
    else if (!state.presets || !state.presets.length) mine = '<optgroup label="My presets"><option disabled>None saved yet - use Custom sort to save one</option></optgroup>';
    else mine = '<optgroup label="My presets">' + state.presets.map(opt).join('') + '</optgroup>';
    const custom = selected === 'custom' ? '<option value="custom" selected>Custom: ' + escapeHtml(summaryText(state.levels)) + '</option>' : '';
    return '<select id="' + id('SortSelect') + '" class="list-sort-select" aria-label="Sort by">' + custom +
      '<optgroup label="Built-in">' + cfg.builtins.map(opt).join('') + '</optgroup>' + mine + '</select>';
  }

  function editorHtml() {
    const draft = state.draft;
    const levelRows = draft.map((l, i) => {
      const used = new Set(draft.filter((_, j) => j !== i).map(x => x.key));
      const colOpts = cfg.columns.filter(c => !used.has(c.key) || c.key === l.key)
        .map(c => '<option value="' + c.key + '"' + (c.key === l.key ? ' selected' : '') + '>' + escapeHtml(c.label) + '</option>').join('');
      const dirOpts = ['asc', 'desc'].map(d => '<option value="' + d + '"' + (d === l.dir ? ' selected' : '') + '>' + escapeHtml(dirLabel(l.key, d)) + '</option>').join('');
      return '<div class="list-sort-level">' +
        '<span class="list-sort-level-label">' + (i ? 'Then by' : 'Sort by') + '</span>' +
        '<select data-sortlevel-key="' + i + '" aria-label="Sort level ' + (i + 1) + ' column">' + colOpts + '</select>' +
        '<select data-sortlevel-dir="' + i + '" aria-label="Sort level ' + (i + 1) + ' direction">' + dirOpts + '</select>' +
        '<button type="button" class="page-btn" data-sortlevel-up="' + i + '"' + (i === 0 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' up">&uarr;</button>' +
        '<button type="button" class="page-btn" data-sortlevel-down="' + i + '"' + (i === draft.length - 1 ? ' disabled' : '') + ' aria-label="Move level ' + (i + 1) + ' down">&darr;</button>' +
        '<button type="button" class="page-btn" data-sortlevel-remove="' + i + '"' + (draft.length === 1 ? ' disabled' : '') + ' aria-label="Remove level ' + (i + 1) + '">Remove</button>' +
      '</div>';
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
    return '<div class="list-sort-bar">' +
      '<label class="list-sort-label" for="' + id('SortSelect') + '">Sort by</label>' + selectHtml() +
      '<button type="button" class="page-btn" id="' + id('SortEditBtn') + '" aria-expanded="' + state.editorOpen + '">' + (state.editorOpen ? 'Hide custom sort' : 'Custom sort...') + '</button>' +
      '<span class="list-sort-summary">' + escapeHtml(summaryText(state.levels)) + '</span>' +
    '</div>' + (state.editorOpen ? editorHtml() : '');
  }

  // Called by the list's own wiring after every region render.
  function wire(region) {
    const rerender = cfg.rerender;
    const byId = name => document.getElementById(id(name));
    const select = byId('SortSelect');
    if (select) select.addEventListener('change', () => {
      const preset = allPresets().find(p => String(p.id) === select.value);
      if (!preset) return;
      set(preset.levels, preset.id);
      if (state.editorOpen) { state.draft = cloneSortLevels(preset.levels); state.draftName = currentUserPreset() ? preset.name : ''; state.msg = ''; }
      rerender();
    });

    region.querySelectorAll('[data-listsort]').forEach(btn => btn.addEventListener('click', () => {
      const key = btn.dataset.listsort;
      const col = columnByKey.get(key);
      if (!col) return;
      const only = state.levels.length === 1 && state.levels[0].key === key;
      const dir = only ? (state.levels[0].dir === 'asc' ? 'desc' : 'asc') : col.dir;
      set([{ key, dir }]);
      rerender();
    }));

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
    region.querySelectorAll('[data-sortlevel-key]').forEach(sel => sel.addEventListener('change', () => {
      const l = draft[Number(sel.dataset.sortlevelKey)];
      l.key = sel.value;
      l.dir = columnByKey.get(sel.value).dir;
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
    const addBtn = byId('SortAddLevel');
    if (addBtn) addBtn.addEventListener('click', () => {
      const used = new Set(draft.map(l => l.key));
      const next = cfg.columns.find(c => !used.has(c.key));
      if (next && draft.length < LIST_SORT_MAX_LEVELS) { draft.push({ key: next.key, dir: next.dir }); rerender(); }
    });

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
        const saved = await sortPresetApi('', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view: cfg.view, name, levels: draft }) });
        state.presets = (state.presets || []).filter(p => p.id !== saved.id).concat([saved]);
        sortUserPresets();
        set(saved.levels, saved.id);
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

  return { state, validLevels, set, ensurePresetsLoaded, sortRows, headerHtml, barHtml, wire, summaryText };
}
