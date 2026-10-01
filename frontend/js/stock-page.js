// stock.html's page script - see stock.html's header comment. Self-contained
// like mir-page.js: its own fetch wrapper, no dependency on main.js.
//
// The page never draws stock or values a line itself. Every input change
// sends the whole form to /api/stock/preview (debounced) and paints what
// comes back - the value, what the MIR can still give, the field errors, the
// notices. Saving sends the same body to /api/stock/vouchers/new, which
// re-runs the same check server-side with the MIR receipts locked. Inputs are
// never re-rendered while the storekeeper types (so focus and cursor survive
// a preview); only the computed figures are repainted.
//
// Every top-level name here starts with "st" / "ST_": this page shares one
// global scope with auth.js and shared.js, and a repeated `let`/`const` there
// is a SyntaxError that silently stops the page
// (test_frontend_global_names.py).

let ST_META = null;
const ST_FORMS = {};

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'stock');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
  stInitTabs();
  try {
    ST_META = await apiStock('/meta');
  } catch (e) {
    document.getElementById('main-content').prepend(stBanner(e.message));
    return;
  }
  ST_FORMS.ISSUE = stIssueForm();
  ST_FORMS.RETURN = stReturnForm();
  ST_FORMS.ADJUST = stDiffForm();
  stInitRegister();
  stInitMismatches();
  stPaintPending(ST_META.pendingApprovals);
})();

/** /api/stock/... fetch wrapper - same shape as mir-page.js's apiMir(). */
async function apiStock(path, opts) {
  const o = Object.assign({ credentials: 'same-origin' }, opts || {});
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
  }
  const res = await authFetch('/api/stock' + path, o);
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  let data;
  try {
    data = await res.json();
  } catch (e) {
    const err = new Error('The server sent an unexpected response. Please try again, or contact IT if this keeps happening.');
    err.status = res.status;
    throw err;
  }
  if (!res.ok) {
    const err = new Error(data.error || data.detail || 'Something went wrong. Please try again.');
    err.status = res.status;
    err.errors = data.errors || [];
    throw err;
  }
  return data;
}

// ── Formatting ────────────────────────────────────────────────────────────
// Exact figures, never the dashboard's rounded shorthand: this is a record.
function stQty(s, uom) {
  if (s === null || s === undefined || s === '') return '-';
  return Number(s).toLocaleString('en-IN', { maximumFractionDigits: 3 }) + (uom ? ' ' + uom : '');
}
function stMoney(s) {
  if (s === null || s === undefined || s === '') return '-';
  return Number(s).toLocaleString('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function stRate(s) {
  if (s === null || s === undefined || s === '') return '-';
  return Number(s).toLocaleString('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 4 });
}
function stDate(iso) { return iso ? formatDateIN(iso) : '-'; }
function stDebounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// Each list's newest request: debouncing spaces the requests out but they can
// still overlap, and a slower earlier response must not paint over a newer one.
// stLoadTicket(name) returns a check that is true only while no later load of
// that list has started.
const ST_LOAD_SEQ = {};
function stLoadTicket(name) {
  const n = (ST_LOAD_SEQ[name] = (ST_LOAD_SEQ[name] || 0) + 1);
  return () => ST_LOAD_SEQ[name] === n;
}
function stBanner(text) { const d = document.createElement('div'); d.className = 'mir-banner mir-banner-warn'; d.textContent = text; return d; }
function stPill(text, tone) { return '<span class="status-pill mir-pill-' + tone + '">' + escapeHtml(text) + '</span>'; }
function stWritable() { return ST_META.plants.filter(p => p.canWrite); }
function stReadable() { return ST_META.plants.filter(p => p.canRead); }
function stReasons(kind) { return ST_META.reasons.filter(r => r.kind === kind); }
function stIsoDay(d) {
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}
function stMinDate() {
  const d = new Date(ST_META.today + 'T00:00:00');
  d.setDate(d.getDate() - ST_META.backdateDays);
  return stIsoDay(d);
}
function stPlantOptions(plants, withAll) {
  return (withAll && plants.length > 1 ? '<option value="">All plants</option>' : '') +
    plants.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
}
function stDays(n) { return n === null || n === undefined ? '-' : n + ' day' + (n === 1 ? '' : 's'); }
const ST_STATUS_TONE = { POSTED: 'ok', PENDING: 'warn', REJECTED: 'bad', CANCELLED: 'muted' };
const ST_MOVE_LABEL = { RECEIPT: 'MIR receipt', OPENING: 'Opening balance', ISSUE: 'Issue', RETURN: 'Return', ADJUST: 'Stock difference' };
const ST_DIFF_LABEL = { COUNT: 'Physical count', WRITE_OFF: 'Write-off', GAIN: 'Found more' };

function stToast(text, tone) {
  let stack = document.getElementById('stToasts');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'stToasts';
    stack.className = 'toast-stack';
    stack.setAttribute('role', 'status');
    document.body.appendChild(stack);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + (tone || 'success');
  el.textContent = text;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 8000);
}

function stFacts(rows) {
  const filled = rows.filter(r => r[1] !== '' && r[1] !== null && r[1] !== undefined);
  if (!filled.length) return '';
  return '<table class="mir-facts-table"><tbody>' + filled.map(([k, v]) =>
    '<tr><th scope="row">' + escapeHtml(k) + '</th><td>' + escapeHtml(String(v)) + '</td></tr>').join('') + '</tbody></table>';
}

// "HRS/26-27/0012 line 1" - how a MIR receipt is named everywhere.
function stMirRef(r) { return r.mirNo ? r.mirNo + ' line ' + r.lineNo : r.doc; }

// The facts a MIR line carries, as chips - what "comes from the MIR".
function stReceiptChips(r) {
  const chip = (k, v) => v ? '<span class="mir-po-chip"><span class="mir-kv-k">' + escapeHtml(k) + '</span> ' + escapeHtml(v) + '</span>' : '';
  return '<div class="mir-line-facts">' +
    chip('MIR date', stDate(r.receivedDate)) + chip('Vendor', r.vendor) + chip('PO', r.poNumber) + chip('Invoice', r.invoiceNo) +
    chip('Item code', r.itemCode) + chip('Category', [r.material.category, r.material.subcategory].filter(Boolean).join(' / ')) +
    chip('Rate', r.rate ? stRate(r.rate) + ' / ' + (r.uom || 'unit') + (r.currency !== 'INR' ? ' ' + r.currency : '') : '') +
    chip('Unit', stConverted(r) ? r.uom + ' (MIR in ' + r.mirUom + ': 1 ' + r.mirUom + ' = ' + stQty(r.factor) + ' ' + r.uom + ')' : r.uom) +
    chip('In store', stDays(r.days)) + chip('PO of', r.billToPlant ? r.billToPlant.name : '') + '</div>';
}

// A receipt held in its material's base unit rather than the MIR's (2 MT
// received, held as 2,000 KG).
function stConverted(r) { return !!r.mirUom && r.mirUom !== r.uom && Number(r.factor) > 0; }

// "2,000" plus "(2 MT)" when the MIR had it in another unit.
function stInMirUnit(qty, r) {
  return stConverted(r) && Number(qty) ? ' <span class="mir-muted">(' + stQty(String(Number(qty) / Number(r.factor)), r.mirUom) + ')</span>' : '';
}

// ── View tabs ─────────────────────────────────────────────────────────────
const ST_TABS = [['tabIssue', 'viewIssue'], ['tabRegister', 'viewRegister'], ['tabMismatch', 'viewMismatch']];

function stShowView(viewId) {
  ST_TABS.forEach(([t, v]) => {
    const on = v === viewId;
    document.getElementById(t).classList.toggle('active', on);
    document.getElementById(t).setAttribute('aria-selected', String(on));
    document.getElementById(v).hidden = !on;
  });
  if (viewId === 'viewRegister') stLoadRegisterView();
  if (viewId === 'viewMismatch') stLoadMismatches();
}

function stInitTabs() {
  ST_TABS.forEach(([tabId, viewId]) => { document.getElementById(tabId).onclick = () => stShowView(viewId); });
}

async function stPaintPending(n) {
  if (n === undefined) {
    try { n = (await apiStock('/meta')).pendingApprovals; } catch (e) { return; }
  }
  const el = document.getElementById('pendingCount');
  el.hidden = !n;
  el.textContent = n ? String(n) : '';
  el.title = n ? n + ' difference' + (n === 1 ? '' : 's') + ' waiting for approval' : '';
}

// ── The shared form machinery ─────────────────────────────────────────────
// One controller per form: its lines, the latest preview, whether a save was
// tried (fields turn red only after that), and a stale-response counter.
function stSetupForm(formId, kind, hooks) {
  const form = document.getElementById(formId);
  const host = form.closest('[data-form-host]');
  const ctl = { kind, form, lines: [], preview: null, tried: false, seq: 0, hooks, extra: {} };
  if (!stWritable().length) {
    host.querySelector('[data-no-write]').hidden = false;
    form.hidden = true;
    return ctl;
  }
  const plantSel = form.querySelector('select[data-field="plant"]');
  // "All plants" searches every plant's MIRs; the first MIR picked sets
  // the plant (stTakePlant()).
  if (plantSel) plantSel.innerHTML = stPlantOptions(stWritable(), true);
  const date = form.querySelector('[data-field="voucher_date"]');
  date.value = ST_META.today;
  date.max = ST_META.today;
  date.min = stMinDate();
  form.querySelector('[data-date-hint]').textContent = 'Today, or up to ' + ST_META.backdateDays + ' days back.';
  ctl.schedule = stDebounce(() => stRunPreview(ctl), 350);
  form.addEventListener('input', e => { if (!e.target.closest('[data-nopreview]')) ctl.schedule(); });
  if (plantSel) plantSel.addEventListener('change', () => { if (hooks.onPlant) hooks.onPlant(); ctl.schedule(); });
  form.addEventListener('submit', e => { e.preventDefault(); stPost(ctl); });
  return ctl;
}

function stHeader(ctl) {
  const out = { kind: ctl.kind };
  ctl.form.querySelectorAll('[data-field]').forEach(el => {
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') out[el.dataset.field] = el.value;
  });
  return Object.assign(out, ctl.extra);
}

function stPlantOf(ctl) { return ctl.form.querySelector('[data-field="plant"]').value; }

async function stRunPreview(ctl) {
  const bar = ctl.form.querySelector('[data-savebar]');
  if (!ctl.lines.length) { bar.hidden = true; ctl.preview = null; if (ctl.hooks.progress) ctl.hooks.progress(null); return; }
  bar.hidden = false;
  const seq = ++ctl.seq;
  let p;
  try {
    p = await apiStock('/preview', { method: 'POST', body: ctl.hooks.payload() });
  } catch (e) {
    if (seq !== ctl.seq) return;
    stShowErrors(ctl, [{ field: '', message: e.message }]);
    return;
  }
  if (seq !== ctl.seq) return;
  ctl.preview = p;
  ctl.hooks.paint(p);
  stShowErrors(ctl, p.errors);
  if (ctl.hooks.progress) ctl.hooks.progress(p);
  ctl.form.querySelector('[data-notices]').innerHTML = (p.notices || []).map(n =>
    '<div class="mir-notice"><span class="mir-notice-icon" aria-hidden="true">!</span><span>' + escapeHtml(n) + '</span></div>').join('');
  const value = p.lines.reduce((s, l) => s + Number(l.value || 0), 0);
  ctl.form.querySelector('[data-totals]').innerHTML = '<div class="mir-total-tile ' + (p.ok ? 'is-ok' : '') + '"><span class="mir-kv-k">Value (before GST)</span>' +
    '<span class="mir-total-val">' + escapeHtml(stMoney(String(Math.abs(value)))) + '</span></div>';
}

const ST_FIELD_LABELS = {
  plant: 'the plant', voucher_date: 'the date', lines: 'the lines', return_of: 'the issue', lot_id: 'the MIR',
  qty: 'the quantity', counted: 'the counted quantity', reason: 'the reason', note: 'the note', mode: 'the kind of difference',
  issue_line_id: 'the issue line',
};

function stFieldLabel(ctl, field) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(field || '');
  if (m) {
    const ln = ctl.hooks.lineAt(Number(m[1]));
    return (ST_FIELD_LABELS[m[2]] || m[2]) + ' on line ' + (Number(m[1]) + 1) + (ln && ln.name ? ' (' + ln.name.slice(0, 40) + ')' : '');
  }
  return ST_FIELD_LABELS[field] || field;
}

function stFieldEl(ctl, e) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(e.field || '');
  if (m) {
    const ln = ctl.hooks.lineAt(Number(m[1]));
    return ln ? ctl.form.querySelector('[data-line="' + ln.key + '"][data-key="' + m[2] + '"]') : null;
  }
  return ctl.form.querySelector('[data-field="' + e.field + '"]');
}

function stShowErrors(ctl, errors) {
  ctl.form.querySelectorAll('.is-invalid').forEach(el => el.classList.remove('is-invalid'));
  const box = ctl.form.querySelector('[data-todo]');
  const text = e => {
    const label = stFieldLabel(ctl, e.field);
    if (e.message === 'Required.') return 'Enter ' + label;
    if (e.message === 'Choose a reason.') return 'Pick ' + label;
    if (e.message === 'This reason needs a note.') return 'Write ' + label + ' (the reason chosen needs one)';
    return (e.field ? label.charAt(0).toUpperCase() + label.slice(1) + ': ' : '') + e.message;
  };
  box.innerHTML = errors.length
    ? '<div class="mir-todo-head' + (ctl.tried ? ' is-bad' : '') + '">Still to do before saving (' + errors.length + ')</div><ul>' +
      errors.map((e, n) => '<li><button type="button" class="mir-todo-item" data-todo-i="' + n + '">' + escapeHtml(text(e)) + '</button></li>').join('') + '</ul>'
    : '<div class="mir-todo-head is-ok">Everything needed is filled in. Check the figures and save.</div>';
  box.querySelectorAll('[data-todo-i]').forEach(b => { b.onclick = () => stFocus(ctl, errors[Number(b.dataset.todoI)]); });
  if (ctl.tried) errors.forEach(e => { const el = stFieldEl(ctl, e); if (el) el.classList.add('is-invalid'); });
}

function stFocus(ctl, e) {
  const el = stFieldEl(ctl, e);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => el.focus({ preventScroll: true }), 300);
}

async function stPost(ctl) {
  ctl.tried = true;
  if (!ctl.preview || !ctl.preview.ok) {
    if (ctl.preview) {
      stShowErrors(ctl, ctl.preview.errors);
      if (ctl.preview.errors.length) stFocus(ctl, ctl.preview.errors[0]);
    } else if (!ctl.lines.length) {
      stToast('Add at least one MIR line first.');
    }
    return;
  }
  const btn = ctl.form.querySelector('[data-post]');
  btn.disabled = true;
  try {
    const v = await apiStock('/vouchers/new', { method: 'POST', body: ctl.hooks.payload() });
    stToast(v.voucherNo + ' saved at ' + v.plant.name + (v.status === 'PENDING' ? ' - waiting for an admin to approve it; stock changes once approved.' : '.'));
    ctl.hooks.reset();
    if (ctl.hooks.saved) ctl.hooks.saved(v);
    stPaintPending();
  } catch (e) {
    await stRunPreview(ctl);
    stShowErrors(ctl, e.errors && e.errors.length ? e.errors : [{ field: '', message: e.message }]);
  } finally {
    btn.disabled = false;
  }
}

// A form's lines all come from one plant: the first MIR picked sets the
// form's plant, and a MIR at another plant is refused until this entry is
// saved. Returns false when refused.
function stTakePlant(ctl, r) {
  const current = ctl.lines.length ? ctl.lines[0].r.plant : null;
  if (current && current.code !== r.plant.code) {
    stToast(r.doc + ' is at ' + r.plant.name + ', but this entry is for ' + current.name + '. Save it first, then pick from ' + r.plant.name + '.', 'error');
    return false;
  }
  const sel = ctl.form.querySelector('select[data-field="plant"]');
  if (sel && sel.value !== r.plant.code) {
    sel.value = r.plant.code;
    if (ctl.hooks.plantSet) ctl.hooks.plantSet();
  }
  return true;
}

function stResetCommon(ctl) {
  const plantEl = ctl.form.querySelector('[data-field="plant"]');
  // Back to "All plants" after a save, so the next MIR picked sets the plant
  // again; a form with one plant (or the return form's fixed one) keeps it.
  const plant = plantEl.tagName === 'SELECT' && plantEl.querySelector('option[value=""]') ? '' : plantEl.value;
  ctl.form.reset();
  plantEl.value = plant;
  ctl.form.querySelector('[data-field="voucher_date"]').value = ST_META.today;
  ctl.lines = []; ctl.preview = null; ctl.tried = false; ctl.extra = {};
  ['[data-todo]', '[data-notices]', '[data-totals]'].forEach(s => { ctl.form.querySelector(s).innerHTML = ''; });
  ctl.form.querySelector('[data-savebar]').hidden = true;
}

// ── Picking a MIR ─────────────────────────────────────────────────────────
// The search box, the "Show all open MIRs" button and the hits: the MIR
// receipts with stock left at the form's plant (every plant the user may
// write at, under "All plants"), grouped by MIR, oldest MIR first. Clicking a
// line adds it; "Add all lines" adds every line of that MIR not yet added.
// The list stays open after a pick (the added lines grey out), so several
// MIRs can be picked in a row. Returns {clear()} for a plant change.
function stMirPicker(input, allBtn, box, plantFn, takenFn, onPick) {
  let rows = [];
  let showAll = false;
  const paint = () => {
    const q = input.value.trim();
    if (!rows.length) {
      box.innerHTML = '<div class="mir-empty">' + (q ? 'No MIR at this plant with stock left matches "' + escapeHtml(q) + '".'
        : 'No MIR at this plant has stock left. Stock comes in when a MIR is posted.') + '</div>';
      return;
    }
    const groups = [];
    rows.forEach(r => {
      let g = groups.find(x => x.key === r.doc);
      if (!g) { g = { key: r.doc, head: r, rows: [] }; groups.push(g); }
      g.rows.push(r);
    });
    const open = g => g.rows.filter(r => !takenFn(r.id));
    box.innerHTML = '<div class="st-summary">' + groups.length + ' open MIR' + (groups.length === 1 ? '' : 's') + ', ' + rows.length + ' line' +
      (rows.length === 1 ? '' : 's') + ' with stock left' + (showAll && !q ? '' : ' matching "' + escapeHtml(q) + '"') + '</div>' +
      groups.map((g, gi) => '<div class="st-mir-group"><div class="st-mir-head"><b>' + escapeHtml(g.key) + '</b>' +
        '<span class="st-plant-tag">' + escapeHtml(g.head.plant.name) + '</span>' +
        '<span class="mir-muted">' + escapeHtml([stDate(g.head.receivedDate), g.head.vendor, g.head.invoiceNo ? 'Invoice ' + g.head.invoiceNo : ''].filter(Boolean).join(' - ')) + '</span>' +
        (g.rows.length > 1 ? '<button type="button" class="mir-link st-add-all" data-group="' + gi + '"' + (open(g).length ? '' : ' disabled') + '>Add all ' + g.rows.length + ' lines</button>' : '') +
        '</div>' +
        g.rows.map(r => '<button type="button" class="st-hit" data-hit="' + r.id + '"' + (takenFn(r.id) ? ' disabled' : '') + '>' +
          '<b>' + (r.lineNo ? 'Line ' + r.lineNo + ': ' : '') + escapeHtml(r.material.name) + '</b>' +
          '<span class="mir-muted">' + escapeHtml(stQty(r.balance, r.uom) + ' left - ' + stDays(r.days) + ' in store' +
            (r.material.category ? ' - ' + r.material.category : '') + (takenFn(r.id) ? ' - added' : '')) + '</span></button>').join('') +
        '</div>').join('');
    box.querySelectorAll('[data-hit]').forEach(b => {
      b.onclick = () => { onPick(rows.find(r => r.id === Number(b.dataset.hit)), false); paint(); };
    });
    box.querySelectorAll('[data-group]').forEach(b => {
      b.onclick = () => { open(groups[Number(b.dataset.group)]).forEach(r => onPick(r, true)); paint(); };
    });
  };
  const load = stDebounce(async () => {
    const q = input.value.trim();
    if (q.length < 2 && !showAll) { rows = []; box.innerHTML = ''; return; }
    box.innerHTML = '<div class="mir-muted">Loading...</div>';
    const params = {};
    if (plantFn()) params.plant = plantFn();
    if (q.length >= 2) params.q = q;
    else params.all = '1';
    let found;
    try {
      found = (await apiStock('/receipts?' + new URLSearchParams(params).toString())).receipts;
    } catch (e) { box.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
    if (input.value.trim() !== q) return;
    rows = found.filter(r => stWritable().some(p => p.code === r.plant.code));
    paint();
  }, 250);
  const setAll = on => {
    showAll = on;
    allBtn.textContent = on ? 'Hide the list' : 'Show all open MIRs';
    allBtn.setAttribute('aria-expanded', String(on));
  };
  input.addEventListener('input', load);
  allBtn.onclick = () => {
    setAll(!showAll);
    if (showAll) input.value = '';
    load();
  };
  return { clear: () => { rows = []; box.innerHTML = ''; input.value = ''; setAll(false); } };
}

// ── Issue from MIR ────────────────────────────────────────────────────────
function stIssueForm() {
  let ctl;
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), { lines: ctl.lines.map(l => ({ lot_id: l.r.id, qty: l.qty })) }),
    lineAt: i => ctl.lines[i],
    paint: p => p.lines.forEach(pl => {
      const ln = ctl.lines[pl.index];
      const box = ln && ctl.form.querySelector('[data-figs="' + ln.key + '"]');
      if (!box) return;
      const after = pl.qty !== null && !p.errors.some(e => e.field === 'lines.' + pl.index + '.qty') ? Number(ln.r.balance) - Number(pl.qty) : null;
      box.innerHTML = (pl.value !== null ? '<div class="mir-line-figures">Value <b>' + stMoney(pl.value) + '</b>' +
        (after !== null ? ' &middot; <b>' + stQty(String(after), ln.r.uom) + '</b> left in this MIR after it' : '') + '</div>' : '');
    }),
    progress: p => stIssueProgress(ctl, p),
    plantSet: () => stIssueDepartments(ctl),
    reset: () => { stResetCommon(ctl); stIssueRender(ctl); stIssueProgress(ctl, null); },
    onPlant: () => { ctl.lines = []; stIssueRender(ctl); stIssueDepartments(ctl); if (ctl.picker) ctl.picker.clear(); },
  };
  ctl = stSetupForm('issueForm', 'ISSUE', hooks);
  ctl.nextKey = 1;
  // `quiet`: added from "Add all lines" or the register - no jump to the
  // quantity box, so the list stays where the storekeeper is looking.
  ctl.add = (r, quiet) => {
    if (ctl.lines.some(l => l.r.id === r.id) || !stTakePlant(ctl, r)) return;
    ctl.lines.push({ key: 'i' + (ctl.nextKey++), r, name: r.material.name, qty: '' });
    stIssueRender(ctl);
    ctl.schedule();
    const inputs = ctl.form.querySelectorAll('[data-key="qty"]');
    if (!quiet && inputs.length) inputs[inputs.length - 1].focus({ preventScroll: true });
  };
  if (ctl.form.hidden) return ctl;
  stIssueDepartments(ctl);
  document.querySelectorAll('#issueProgress [data-goto]').forEach(b => {
    b.onclick = () => { const t = document.getElementById(b.dataset.goto); if (t && !t.hidden) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
  });
  ctl.picker = stMirPicker(document.getElementById('isFind'), document.getElementById('isAll'), document.getElementById('isHits'),
    () => stPlantOf(ctl), id => ctl.lines.some(l => l.r.id === id), (r, quiet) => ctl.add(r, quiet));
  stIssueProgress(ctl, null);
  return ctl;
}

function stIssueProgress(ctl, p) {
  const errs = p ? p.errors : [];
  const done = { 1: !errs.some(e => e.field === 'plant' || e.field === 'voucher_date'), 2: ctl.lines.length > 0,
                 3: ctl.lines.length > 0 && !!p && p.ok };
  let currentSet = false;
  document.querySelectorAll('#issueProgress li').forEach(li => {
    const n = Number(li.dataset.step);
    li.classList.toggle('is-done', !!done[n]);
    const current = !done[n] && !currentSet;
    if (current) currentSet = true;
    li.classList.toggle('is-current', current);
  });
}

function stIssueDepartments(ctl) {
  document.getElementById('isDeptList').innerHTML = (ST_META.departments[stPlantOf(ctl)] || []).map(d => '<option value="' + escapeHtml(d) + '">').join('');
}

function stIssueRender(ctl) {
  const area = document.getElementById('isLines');
  document.getElementById('isStep3').hidden = !ctl.lines.length;
  area.innerHTML = ctl.lines.map((l, i) =>
    '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(l.r.material.name) + '</b>' +
      '<div class="mir-muted">' + escapeHtml(stMirRef(l.r) + ' - ' + l.r.plant.name) + ' - ' + stQty(l.r.balance, l.r.uom) + ' left</div></div></div>' +
      '<button type="button" class="mir-link" data-remove="' + l.key + '">Remove</button></div>' +
    stReceiptChips(l.r) +
    '<div class="mir-line-grid"><div class="form-group"><label class="form-label" for="isQty' + l.key + '">Quantity going out (' + escapeHtml(l.r.uom || 'units') + ') <span class="req-mark">*</span></label>' +
      '<input class="form-control" id="isQty' + l.key + '" data-line="' + l.key + '" data-key="qty" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.qty) + '"></div></div>' +
    '<div data-figs="' + l.key + '"></div></div>').join('');
  area.querySelectorAll('[data-key="qty"]').forEach(inp => inp.addEventListener('input', () => { ctl.lines.find(x => x.key === inp.dataset.line).qty = inp.value; }));
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => { ctl.lines = ctl.lines.filter(l => l.key !== b.dataset.remove); stIssueRender(ctl); ctl.schedule(); }; });
}

// ── Return (opened from an issue slip) ────────────────────────────────────
function stReturnForm() {
  let ctl;
  const filled = () => ctl.lines.filter(l => l.qty.trim() !== '');
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), {
      return_of: ctl.issue ? ctl.issue.id : null,
      lines: filled().map(l => ({ issue_line_id: l.lineId, qty: l.qty, reason: l.reason, note: l.note })),
    }),
    // A return's lines on the wire are only the ones with a quantity, so
    // "lines.N" is the Nth of those.
    lineAt: i => filled()[i],
    paint: p => {
      ctl.lines.forEach(l => { const box = ctl.form.querySelector('[data-figs="' + l.key + '"]'); if (box) box.innerHTML = ''; });
      p.lines.forEach(pl => {
        const l = filled()[pl.index];
        const box = l && ctl.form.querySelector('[data-figs="' + l.key + '"]');
        if (box && pl.value) box.innerHTML = '<div class="mir-line-figures">Back into ' + escapeHtml(pl.draws.map(d => d.doc).join(', ')) + ' &middot; value <b>' + stMoney(pl.value) + '</b></div>';
      });
    },
    reset: () => { stResetCommon(ctl); ctl.issue = null; document.getElementById('returnHost').hidden = true; },
    saved: v => { stLoadSlips(); stLoadVoucher(v.returnOf.id, 'slipDetail'); },
  };
  ctl = stSetupForm('returnForm', 'RETURN', hooks);
  ctl.issue = null;
  if (!ctl.form.hidden) document.getElementById('rtClose').onclick = () => hooks.reset();
  return ctl;
}

function stReturnOpen(issue) {
  const ctl = ST_FORMS.RETURN;
  if (ctl.form.hidden) return;
  stResetCommon(ctl);
  ctl.issue = issue;
  document.getElementById('rtPlant').value = issue.plant.code;
  document.getElementById('rtTitle').textContent = issue.voucherNo;
  ctl.lines = issue.returnable.filter(r => Number(r.stillOut) > 0).map(r => ({
    key: 'r' + r.lineId, lineId: r.lineId, name: r.material.name, uom: r.uom, stillOut: r.stillOut, issued: r.issued, qty: '', reason: '', note: '' }));
  const reasons = stReasons('RETURN');
  document.getElementById('rtLines').innerHTML = ctl.lines.map((l, i) =>
    '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(l.name) + '</b>' +
      '<div class="mir-muted">Issued ' + stQty(l.issued, l.uom) + ', still out ' + stQty(l.stillOut, l.uom) + '</div></div></div></div>' +
    '<div class="mir-line-grid">' +
      '<div class="form-group"><label class="form-label" for="rtQty' + i + '">Quantity back (' + escapeHtml(l.uom || 'units') + ')</label>' +
        '<input class="form-control" id="rtQty' + i + '" data-line="' + l.key + '" data-key="qty" inputmode="decimal" autocomplete="off"></div>' +
      '<div class="form-group"><label class="form-label" for="rtReason' + i + '">Reason</label><select class="form-control" id="rtReason' + i + '" data-line="' + l.key + '" data-key="reason">' +
        '<option value="">Choose...</option>' + reasons.map(r => '<option value="' + escapeHtml(r.code) + '">' + escapeHtml(r.label) + '</option>').join('') + '</select></div>' +
      '<div class="form-group"><label class="form-label" for="rtNote' + i + '">Note</label><input class="form-control" id="rtNote' + i + '" data-line="' + l.key + '" data-key="note" maxlength="2000"></div>' +
    '</div><div data-figs="' + l.key + '"></div></div>').join('');
  document.getElementById('rtLines').querySelectorAll('[data-key]').forEach(inp => {
    inp.addEventListener('input', () => { ctl.lines.find(x => x.key === inp.dataset.line)[inp.dataset.key] = inp.value; });
  });
  const host = document.getElementById('returnHost');
  host.hidden = false;
  host.scrollIntoView({ behavior: 'smooth', block: 'start' });
  ctl.schedule();
}

// ── Stock differences (the Open mismatches form) ──────────────────────────
function stDiffForm() {
  let ctl;
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), { lines: ctl.lines.map(l => ({
      mode: l.mode, lot_id: l.r.id, qty: l.qty, counted: l.counted, reason: l.reason, note: l.note })) }),
    lineAt: i => ctl.lines[i],
    paint: p => p.lines.forEach(pl => {
      const l = ctl.lines[pl.index];
      const box = l && ctl.form.querySelector('[data-figs="' + l.key + '"]');
      if (!box) return;
      let html = '';
      if (l.mode === 'count' && pl.book !== null) {
        const diff = Number(pl.counted) - Number(pl.book);
        html += '<div class="mir-line-figures">Register on the day <b>' + stQty(pl.book, l.r.uom) + '</b> &middot; counted <b>' + stQty(pl.counted, l.r.uom) + '</b> &middot; ' +
          (diff === 0 ? 'no difference' : (diff > 0 ? 'found <b>' + stQty(String(diff), l.r.uom) + '</b> more' : '<b>' + stQty(String(-diff), l.r.uom) + '</b> short')) + '</div>';
      }
      if (pl.value) html += '<div class="mir-line-figures">Value <b>' + stMoney(pl.value) + '</b></div>';
      box.innerHTML = html;
    }),
    reset: () => { stResetCommon(ctl); stDiffRender(ctl); },
    onPlant: () => { ctl.lines = []; stDiffRender(ctl); if (ctl.picker) ctl.picker.clear(); },
    saved: () => { document.getElementById('diffHost').hidden = true; stLoadMismatches(); },
  };
  ctl = stSetupForm('diffForm', 'ADJUST', hooks);
  ctl.nextKey = 1;
  ctl.add = r => {
    if (ctl.lines.some(l => l.r.id === r.id) || !stTakePlant(ctl, r)) return;
    ctl.lines.push({ key: 'd' + (ctl.nextKey++), r, name: r.material.name, mode: 'count', qty: '', counted: '', reason: '', note: '' });
    stDiffRender(ctl);
    ctl.schedule();
  };
  if (ctl.form.hidden) return ctl;
  document.getElementById('dfPostHint').textContent = ST_META.isAdmin ? '' : 'It waits for an admin to approve it.';
  ctl.picker = stMirPicker(document.getElementById('dfFind'), document.getElementById('dfAll'), document.getElementById('dfHits'),
    () => stPlantOf(ctl), id => ctl.lines.some(l => l.r.id === id), r => ctl.add(r));
  stDiffRender(ctl);
  return ctl;
}

function stDiffReasonOptions(l) {
  const opts = rs => rs.map(r => '<option value="' + escapeHtml(r.code) + '"' + (l.reason === r.code ? ' selected' : '') + '>' + escapeHtml(r.label) + '</option>').join('');
  if (l.mode === 'remove') return opts(stReasons('ADJUST_OUT'));
  return '<optgroup label="Found more than the register">' + opts(stReasons('ADJUST_IN')) + '</optgroup>' +
    '<optgroup label="Found less than the register">' + opts(stReasons('ADJUST_OUT')) + '</optgroup>';
}

function stDiffRender(ctl) {
  const area = document.getElementById('dfLines');
  area.innerHTML = ctl.lines.length ? ctl.lines.map((l, i) => {
    const qtyField = l.mode === 'count'
      ? '<div class="form-group"><label class="form-label" for="dfCounted' + l.key + '">Counted - left of this MIR (' + escapeHtml(l.r.uom || 'units') + ') <span class="req-mark">*</span></label>' +
        '<input class="form-control" id="dfCounted' + l.key + '" data-line="' + l.key + '" data-key="counted" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.counted) + '"></div>'
      : '<div class="form-group"><label class="form-label" for="dfQty' + l.key + '">Quantity written off (' + escapeHtml(l.r.uom || 'units') + ') <span class="req-mark">*</span></label>' +
        '<input class="form-control" id="dfQty' + l.key + '" data-line="' + l.key + '" data-key="qty" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.qty) + '"></div>';
    return '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(l.r.material.name) + '</b>' +
        '<div class="mir-muted">' + escapeHtml(stMirRef(l.r) + ' - ' + l.r.plant.name) + ' - ' + stQty(l.r.balance, l.r.uom) + ' in the register</div></div></div>' +
        '<button type="button" class="mir-link" data-remove="' + l.key + '">Remove</button></div>' +
      '<div class="mir-line-grid">' +
        '<div class="form-group"><label class="form-label" for="dfMode' + l.key + '">Kind of difference <span class="req-mark">*</span></label><select class="form-control" id="dfMode' + l.key + '" data-line="' + l.key + '" data-key="mode">' +
          '<option value="count"' + (l.mode === 'count' ? ' selected' : '') + '>Physical count</option><option value="remove"' + (l.mode === 'remove' ? ' selected' : '') + '>Write-off</option></select></div>' +
        qtyField +
        '<div class="form-group"><label class="form-label" for="dfReason' + l.key + '">Reason <span class="req-mark">*</span></label><select class="form-control" id="dfReason' + l.key + '" data-line="' + l.key + '" data-key="reason"><option value="">Choose...</option>' + stDiffReasonOptions(l) + '</select></div>' +
        '<div class="form-group"><label class="form-label" for="dfNote' + l.key + '">Note</label><input class="form-control" id="dfNote' + l.key + '" data-line="' + l.key + '" data-key="note" maxlength="2000" value="' + escapeHtml(l.note) + '"></div>' +
      '</div><div data-figs="' + l.key + '"></div></div>';
  }).join('') : '<div class="mir-empty">Find the MIR above to record a difference against it.</div>';
  area.querySelectorAll('[data-line][data-key]').forEach(inp => {
    inp.addEventListener('input', () => {
      const l = ctl.lines.find(x => x.key === inp.dataset.line);
      l[inp.dataset.key] = inp.value;
      if (inp.dataset.key === 'mode') { l.reason = ''; stDiffRender(ctl); }
    });
  });
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => { ctl.lines = ctl.lines.filter(l => l.key !== b.dataset.remove); stDiffRender(ctl); ctl.schedule(); }; });
}

// ── RM register ───────────────────────────────────────────────────────────
function stInitRegister() {
  const readable = stReadable();
  ['rgPlant', 'slPlant', 'mmPlant'].forEach(id => { document.getElementById(id).innerHTML = stPlantOptions(readable, true); });
  document.getElementById('rgCategory').innerHTML = '<option value="">All</option>' +
    (ST_META.categories || []).map(c => '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>').join('');
  document.getElementById('rgFrom').value = ST_META.today.slice(0, 8) + '01';
  document.getElementById('rgTo').value = ST_META.today;
  const reload = stDebounce(stLoadRegister, 300);
  ['rgPlant', 'rgFrom', 'rgTo', 'rgCategory', 'rgSearch', 'rgAll'].forEach(id => document.getElementById(id).addEventListener('input', reload));
  const reloadSlips = stDebounce(stLoadSlips, 300);
  ['slPlant', 'slKind', 'slFrom', 'slTo', 'slSearch'].forEach(id => document.getElementById(id).addEventListener('input', reloadSlips));
  const pick = slips => {
    document.getElementById('rgStock').hidden = slips;
    document.getElementById('rgSlips').hidden = !slips;
    [['rgShowStock', !slips], ['rgShowSlips', slips]].forEach(([id, on]) => {
      const b = document.getElementById(id);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    stLoadRegisterView();
  };
  document.getElementById('rgShowStock').onclick = () => pick(false);
  document.getElementById('rgShowSlips').onclick = () => pick(true);
}

function stLoadRegisterView() {
  if (document.getElementById('rgSlips').hidden) stLoadRegister();
  else stLoadSlips();
}

async function stLoadRegister() {
  const area = document.getElementById('registerArea');
  const params = new URLSearchParams();
  [['plant', 'rgPlant'], ['from', 'rgFrom'], ['to', 'rgTo'], ['category', 'rgCategory'], ['q', 'rgSearch']].forEach(([k, id]) => {
    const v = document.getElementById(id).value.trim();
    if (v) params.set(k, v);
  });
  if (document.getElementById('rgAll').checked) params.set('all', '1');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  const current = stLoadTicket('register');
  let data;
  try { data = await apiStock('/register?' + params.toString()); } catch (e) {
    if (current()) area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (!current()) return;
  const rows = data.rows;
  if (!rows.length) {
    area.innerHTML = '<div class="mir-empty">No stock for these filters. Stock comes into the store when a MIR is posted at the plant.</div>';
    return;
  }
  const total = rows.reduce((s, r) => s + Number(r.value || 0), 0);
  const allPlants = !document.getElementById('rgPlant').value && stReadable().length > 1;
  // Returned and Adjusted only when something in the period moved that way,
  // so the usual register is Opening / Received / Issued / Closing.
  const showReturned = rows.some(r => Number(r.returned));
  const showAdjusted = rows.some(r => Number(r.adjusted));
  const num = v => '<td class="num">' + (Number(v) ? stQty(v) : '<span class="mir-muted">-</span>') + '</td>';
  const catText = m => [m.category, m.subcategory && m.subcategory.toLowerCase() !== (m.category || '').toLowerCase() ? m.subcategory : '']
    .filter(Boolean).join(' - ');
  const qtyCols = 4 + (showReturned ? 1 : 0) + (showAdjusted ? 1 : 0);
  area.innerHTML = '<div class="st-summary">' + rows.length + ' MIR line' + (rows.length === 1 ? '' : 's') + ' &middot; ' + stDate(data.from) + ' to ' + stDate(data.to) + '</div>' +
    '<div class="table-wrap"><table class="mir-click st-register"><thead><tr><th>MIR</th><th>Material</th><th>Vendor</th>' +
    '<th class="num">Opening</th><th class="num">Received</th><th class="num">Issued</th>' +
    (showReturned ? '<th class="num">Returned</th>' : '') + (showAdjusted ? '<th class="num">Adjusted</th>' : '') +
    '<th class="num">Closing</th><th class="num">Rate</th><th class="num">Value</th><th class="num">In store</th></tr></thead><tbody>' +
    rows.map((r, i) => '<tr data-row="' + i + '" tabindex="0">' +
      '<td class="nowrap"><b>' + escapeHtml(r.mirNo || r.doc) + '</b><div class="mir-muted">' +
        escapeHtml([r.lineNo ? 'Line ' + r.lineNo : '', stDate(r.receivedDate), allPlants ? r.plant.code.toUpperCase() : ''].filter(Boolean).join(' - ')) + '</div></td>' +
      '<td class="st-text"><b>' + escapeHtml(r.material.name) + '</b><div class="mir-muted">' +
        escapeHtml([r.itemCode, catText(r.material)].filter(Boolean).join(' - ') || 'No category') + '</div></td>' +
      '<td class="st-text">' + escapeHtml(r.vendor || '-') + (r.billToPlant ? '<div class="mir-muted">PO of ' + escapeHtml(r.billToPlant.name) + '</div>' : '') + '</td>' +
      num(r.opening) + '<td class="num">' + (Number(r.received) ? stQty(r.received) + stInMirUnit(r.received, r) : '<span class="mir-muted">-</span>') + '</td>' + num(r.issued) + (showReturned ? num(r.returned) : '') + (showAdjusted ? num(r.adjusted) : '') +
      '<td class="num st-closing"><b>' + stQty(r.closing) + '</b> <span class="mir-muted">' + escapeHtml(r.uom || '') + '</span></td>' +
      '<td class="num">' + stRate(r.rate) + (r.uom ? '<span class="mir-muted">/' + escapeHtml(r.uom) + '</span>' : '') + '</td>' +
      '<td class="num">' + stMoney(r.value) + '</td>' +
      '<td class="num">' + stDays(r.days) + '</td></tr>').join('') +
    '</tbody><tfoot><tr><td colspan="' + (3 + qtyCols) + '">Closing value (before GST)</td><td></td><td class="num"><b>' + stMoney(String(total)) + '</b></td><td></td></tr></tfoot>' +
    '</table></div>';
  area.querySelectorAll('[data-row]').forEach(tr => {
    const open = () => stLoadReceipt(rows[Number(tr.dataset.row)].id);
    tr.onclick = open;
    tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
  });
}

async function stLoadReceipt(lotId) {
  const area = document.getElementById('receiptDetail');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let d;
  try { d = await apiStock('/receipts/' + lotId); } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const s = d.setting;
  const canAct = d.canWrite && d.stocked && Number(d.balance) > 0 && d.source === 'MIR';
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(d.material.name) + ' <span class="mir-muted">' + escapeHtml(stMirRef(d)) + '</span></h3>' +
      (canAct ? '<button type="button" class="btn btn-primary btn-small" id="rcIssue">Issue from this MIR</button>' +
        '<button type="button" class="btn btn-navy btn-small" id="rcDiff">Record a difference</button>' : '') + '</div>' +
    stReceiptChips(d) +
    stFacts([['Plant', d.plant.name], ['Received (accepted)', stQty(d['in'], d.uom) + (stConverted(d) && Number(d['in']) ? ' (' + stQty(String(Number(d['in']) / Number(d.factor)), d.mirUom) + ' on the MIR)' : '')],
      ['Issued', stQty(d.issued, d.uom)],
      ['Returned', stQty(d.returned, d.uom)], ['Stock differences', Number(d.adjusted) ? stQty(d.adjusted, d.uom) : ''],
      ['Left in store', stQty(d.balance, d.uom)], ['Value (before GST)', stMoney(d.value)], ['Batch', d.batchNo],
      ['Kept in store', d.stocked ? 'Yes' : 'No - went straight to use']]) +
    (d.canWrite ? '<div class="st-settings"><h4 class="mir-subtitle">Store settings for ' + escapeHtml(d.material.name) + ' at ' + escapeHtml(d.plant.name) + '</h4>' +
      '<div class="mir-grid">' +
        '<label class="st-check"><input type="checkbox" id="setStocked"' + (s.isStocked ? ' checked' : '') + '> This plant keeps it in store</label>' +
        '<div class="form-group"><label class="form-label" for="setMin">Minimum level (' + escapeHtml(d.uom || 'units') + ')</label>' +
          '<input class="form-control" id="setMin" inputmode="decimal" value="' + escapeHtml(s.minLevel || '') + '" placeholder="Blank = none"></div>' +
      '</div>' +
      '<p class="mir-hint">Not kept in store: its future MIRs record the receipt but put nothing into stock (for material that goes straight to use). MIRs already made keep what they are.' +
        (s.updatedBy ? ' Last changed by ' + escapeHtml(s.updatedBy) + '.' : '') + '</p>' +
      '<div class="mir-actions"><button type="button" class="btn btn-navy btn-small" id="setSave">Save settings</button><span class="mir-error-text" id="setErr"></span></div></div>' : '') +
    stUnitsPanel(d) +
    '<h4 class="mir-subtitle">Movements</h4><div class="table-wrap"><table><thead><tr><th>Date</th><th>Document</th><th>What</th><th>Detail</th><th class="num">In / out</th><th class="num">Balance</th></tr></thead><tbody>' +
      (d.movements.length ? d.movements.map(m => '<tr><td class="nowrap">' + stDate(m.date) + '</td>' +
        '<td class="nowrap">' + (m.voucherId ? '<button type="button" class="mir-link" data-voucher="' + m.voucherId + '">' + escapeHtml(m.doc) + '</button>' : escapeHtml(m.doc)) + '</td>' +
        '<td>' + escapeHtml(ST_MOVE_LABEL[m.kind] || m.kind) + '</td><td>' + escapeHtml(m.detail || '') + '</td>' +
        '<td class="num">' + (Number(m.qty) > 0 ? '+' : '') + stQty(m.qty) + '</td><td class="num"><b>' + stQty(m.balance) + '</b></td></tr>').join('')
        : '<tr><td colspan="6" class="mir-muted">Nothing counts: the MIR is cancelled or it went straight to use.</td></tr>') +
    '</tbody></table></div></section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  area.querySelectorAll('[data-voucher]').forEach(b => { b.onclick = () => stOpenSlip(Number(b.dataset.voucher)); });
  const issueBtn = document.getElementById('rcIssue');
  if (issueBtn) issueBtn.onclick = () => stSendTo(ST_FORMS.ISSUE, 'viewIssue', d);
  const diffBtn = document.getElementById('rcDiff');
  if (diffBtn) diffBtn.onclick = () => { stShowView('viewMismatch'); stOpenDiffForm(); stSendTo(ST_FORMS.ADJUST, null, d); };
  stWireUnits(d, lotId);
  const save = document.getElementById('setSave');
  if (save) save.onclick = async () => {
    try {
      await apiStock('/settings', { method: 'POST', body: { plant: d.plant.code, materialId: d.material.id, isStocked: document.getElementById('setStocked').checked,
        minLevel: document.getElementById('setMin').value.trim(), minLevelUom: d.uom } });
      stToast('Settings saved for ' + d.material.name + '.');
      stLoadReceipt(lotId);
    } catch (e) { document.getElementById('setErr').textContent = e.message; }
  };
}

// ── Units (company-wide, on the material) ────────────────────────────────
// A material's base unit (KG, L, NOS, M) and pack factors ("1 ROLL = 660 M").
// Exact units convert by themselves; only MIRs posted afterwards convert by a
// change - receipts already in the store keep their unit.
function stIsExact(uom) { return Object.prototype.hasOwnProperty.call(ST_META.exactUnits || {}, uom); }

function stUnitsPanel(d) {
  const u = d.units;
  const baseLabel = u.baseUom || 'base unit';
  const factorText = u.factors.map(f => '1 ' + f.uom + ' = ' + stQty(f.factor) + ' ' + (u.baseUom || '')).join(', ');
  if (!d.canWrite) {
    return stFacts([['Base unit (every plant)', u.baseUom || 'None - each MIR keeps its unit'], ['Pack units', factorText]]);
  }
  const rows = u.factors.map(f => ({ uom: f.uom, factor: f.factor, fixed: true }));
  if (d.mirUom && !stIsExact(d.mirUom) && !rows.some(r => r.uom === d.mirUom)) rows.push({ uom: d.mirUom, factor: '', fixed: true });
  const row = (r, i) => '<div class="st-factor-row" data-factor-row="' + i + '"><span>1</span>' +
    '<input class="form-control st-unit" data-unit aria-label="Pack unit" list="unPackList" maxlength="20" value="' + escapeHtml(r.uom) + '"' + (r.fixed ? ' readonly' : '') + ' placeholder="e.g. ROLL">' +
    '<span>=</span><input class="form-control st-factor" data-factor aria-label="How many base units" inputmode="decimal" value="' + escapeHtml(r.factor || '') + '" placeholder="Blank = none">' +
    '<span data-base-label>' + escapeHtml(baseLabel) + '</span></div>';
  return '<div class="st-settings"><h4 class="mir-subtitle">Units for ' + escapeHtml(d.material.name) + ' (every plant)</h4>' +
    '<div class="mir-grid"><div class="form-group"><label class="form-label" for="unBase">Base unit - stock is kept in it</label><select class="form-control" id="unBase">' +
      '<option value="">None - each MIR keeps its own unit</option>' +
      (ST_META.baseUnits || []).map(b => '<option value="' + escapeHtml(b.code) + '"' + (u.baseUom === b.code ? ' selected' : '') + '>' + escapeHtml(b.label) + '</option>').join('') +
    '</select></div></div>' +
    '<div class="form-label">Pack units - how many base units one of them is</div>' +
    '<div id="unRows">' + rows.map(row).join('') + '</div>' +
    '<button type="button" class="mir-link" id="unAdd">+ Add a pack unit</button>' +
    '<datalist id="unPackList">' + (ST_META.packUnits || []).map(x => '<option value="' + escapeHtml(x) + '">').join('') + '</datalist>' +
    '<div class="form-group"><label class="form-label" for="unReason">Why the change <span class="req-mark">*</span></label><input class="form-control" id="unReason" maxlength="500"></div>' +
    '<p class="mir-hint">MT, G, ML, KL, CM and MM convert into KG, L or M by themselves. A pack unit (ROLL, SET, BAG, M2, ...) converts only with a factor. ' +
      'Only MIRs posted from now on use a change; receipts already in the store keep their unit.</p>' +
    (u.history.length ? '<ul class="st-history">' + u.history.map(h => '<li>' + escapeHtml(h.field.replace('base_uom', 'Base unit') + ': ' + (h.oldValue || 'none') + ' to ' + (h.newValue || 'none') +
      ' - ' + h.reason + ' (' + h.by + ', ' + new Date(h.at).toLocaleDateString('en-IN') + ')') + '</li>').join('') + '</ul>' : '') +
    '<div class="mir-actions"><button type="button" class="btn btn-navy btn-small" id="unSave">Save units</button><span class="mir-error-text" id="unErr"></span></div></div>';
}

function stWireUnits(d, lotId) {
  const save = document.getElementById('unSave');
  if (!save) return;
  const base = document.getElementById('unBase');
  base.addEventListener('change', () => {
    document.querySelectorAll('#unRows [data-base-label]').forEach(el => { el.textContent = base.value || 'base unit'; });
  });
  document.getElementById('unAdd').onclick = () => {
    const box = document.getElementById('unRows');
    const i = box.querySelectorAll('[data-factor-row]').length;
    box.insertAdjacentHTML('beforeend', '<div class="st-factor-row" data-factor-row="' + i + '"><span>1</span>' +
      '<input class="form-control st-unit" data-unit aria-label="Pack unit" list="unPackList" maxlength="20" placeholder="e.g. ROLL">' +
      '<span>=</span><input class="form-control st-factor" data-factor aria-label="How many base units" inputmode="decimal" placeholder="Blank = none">' +
      '<span data-base-label>' + escapeHtml(base.value || 'base unit') + '</span></div>');
    box.lastElementChild.querySelector('[data-unit]').focus();
  };
  save.onclick = async () => {
    const factors = {};
    document.querySelectorAll('#unRows [data-factor-row]').forEach(r => {
      const unit = r.querySelector('[data-unit]').value.trim().toUpperCase();
      if (unit) factors[unit] = r.querySelector('[data-factor]').value.trim();
    });
    try {
      await apiStock('/materials/' + d.material.id + '/units', { method: 'POST', body: {
        baseUom: base.value, factors, reason: document.getElementById('unReason').value.trim() } });
      stToast('Units saved for ' + d.material.name + '. MIRs posted from now on use them.');
      stLoadReceipt(lotId);
    } catch (e) { document.getElementById('unErr').textContent = e.message; }
  };
}

// Put a receipt onto a form (from the register's detail); it sets the
// form's plant, or is refused if the form already holds another plant's.
function stSendTo(ctl, viewId, receipt) {
  if (!ctl || ctl.form.hidden) return;
  if (viewId) stShowView(viewId);
  ctl.add(receipt, true);
  ctl.form.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── Issue slips ───────────────────────────────────────────────────────────
function stOpenSlip(id) {
  stShowView('viewRegister');
  document.getElementById('rgShowSlips').click();
  stLoadVoucher(id, 'slipDetail');
}

async function stLoadSlips() {
  const area = document.getElementById('slipArea');
  const params = new URLSearchParams();
  [['plant', 'slPlant'], ['kind', 'slKind'], ['q', 'slSearch'], ['from', 'slFrom'], ['to', 'slTo']].forEach(([k, id]) => {
    const v = document.getElementById(id).value.trim();
    if (v) params.set(k, v);
  });
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  const current = stLoadTicket('slips');
  try {
    const list = (await apiStock('/vouchers?' + params.toString())).vouchers;
    if (!current()) return;
    if (!list.length) { area.innerHTML = '<div class="mir-empty">Nothing matches these filters.</div>'; return; }
    area.innerHTML = '<div class="table-wrap"><table class="mir-click"><thead><tr><th>Number</th><th>Date</th><th>Kind</th><th>Plant</th><th>Department / why</th><th>Materials</th><th>Status</th><th>Entered by</th></tr></thead><tbody>' +
      list.map(v => '<tr data-voucher="' + v.id + '" tabindex="0"><td class="nowrap"><b>' + escapeHtml(v.voucherNo) + '</b></td><td class="nowrap">' + stDate(v.date) + '</td>' +
        '<td>' + escapeHtml(v.kindLabel) + '</td><td>' + escapeHtml(v.plant.name) + '</td>' +
        '<td>' + escapeHtml(v.kind === 'ISSUE' ? [v.department, v.issuedTo].filter(Boolean).join(', ') || '-' : (v.returnOf ? 'Back from ' + v.returnOf.voucherNo : '')) + '</td>' +
        '<td>' + escapeHtml(v.materials) + '</td><td>' + stPill(v.statusLabel, ST_STATUS_TONE[v.status]) + '</td><td>' + escapeHtml(v.createdBy) + '</td></tr>').join('') +
      '</tbody></table></div>';
    area.querySelectorAll('[data-voucher]').forEach(tr => {
      const open = () => stLoadVoucher(Number(tr.dataset.voucher), 'slipDetail');
      tr.onclick = open;
      tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
    });
  } catch (e) {
    if (current()) area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

async function stLoadVoucher(id, areaId) {
  const area = document.getElementById(areaId);
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let v;
  try { v = await apiStock('/vouchers/' + id); } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const stillOut = v.returnable.some(r => Number(r.stillOut) > 0);
  const canReturn = stillOut && (ST_META.plants.find(p => p.code === v.plant.code) || {}).canWrite && areaId === 'slipDetail';
  const value = v.lines.reduce((s, l) => s + Number(l.value || 0), 0);
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(v.voucherNo) + '</h3>' + stPill(v.statusLabel, ST_STATUS_TONE[v.status]) +
      (canReturn ? '<button type="button" class="btn btn-navy btn-small" id="vReturn">Take material back</button>' : '') + '</div>' +
    stFacts([['Kind', v.kindLabel], ['Plant', v.plant.name], ['Date', stDate(v.date)], ['Department', v.department], ['Issued to', v.issuedTo],
      ['Production order / batch', v.reference], ['Returns material from', v.returnOf ? v.returnOf.voucherNo : ''],
      ['Value (before GST)', v.status === 'PENDING' ? 'Worked out on approval' : stMoney(String(Math.abs(value)))],
      ['Entered by', v.createdBy + ', ' + new Date(v.createdAt).toLocaleString('en-IN')],
      ['Approval', v.decidedBy ? (v.status === 'REJECTED' ? 'Not approved by ' : 'Approved by ') + v.decidedBy + (v.decisionNote ? ': ' + v.decisionNote : '') : ''],
      ['Cancelled', v.cancelReason ? v.cancelledBy + ': ' + v.cancelReason : ''], ['Remarks', v.remarks]]) +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>MIR</th><th>Material</th><th class="num">Quantity</th><th>Reason</th><th class="num">Value</th></tr></thead><tbody>' +
      v.lines.map(l => '<tr><td>' + l.lineNo + '</td>' +
        '<td class="nowrap">' + (l.receipt ? '<button type="button" class="mir-link" data-receipt="' + l.receipt.id + '">' + escapeHtml(stMirRef(l.receipt)) + '</button>'
          : escapeHtml(l.draws.map(d => d.doc).join(', ') || '-')) + '</td>' +
        '<td>' + escapeHtml(l.material.name) + '</td>' +
        '<td class="num nowrap">' + (l.direction > 0 ? '+' : '-') + stQty(l.qty, l.uom) +
          (l.counted !== null ? '<div class="mir-muted">Counted ' + stQty(l.counted, l.uom) + ', register ' + stQty(l.book, l.uom) + '</div>' : '') + '</td>' +
        '<td>' + escapeHtml(l.reason || '-') + (l.note ? '<div class="mir-muted">' + escapeHtml(l.note) + '</div>' : '') + '</td>' +
        '<td class="num">' + (l.value !== null ? stMoney(l.value) : '-') + '</td></tr>').join('') +
    '</tbody></table></div>' +
    (v.returns.length ? '<h4 class="mir-subtitle">Returns against this issue</h4><p>' + v.returns.map(r => '<button type="button" class="mir-link" data-open="' + r.id + '">' + escapeHtml(r.voucherNo) + '</button> (' + escapeHtml(r.status.toLowerCase()) + ')').join(', ') + '</p>' : '') +
    (v.canApprove ? '<div class="mir-cancel"><input class="form-control" id="vNote' + areaId + '" maxlength="500" placeholder="Note (required to turn it down)" aria-label="Approval note">' +
      '<button type="button" class="btn btn-primary" id="vApprove' + areaId + '">Approve</button><button type="button" class="btn btn-navy" id="vReject' + areaId + '">Turn down</button></div>' : '') +
    (v.canCancel ? '<div class="mir-cancel"><input class="form-control" id="vCancelReason' + areaId + '" maxlength="500" placeholder="Why is this being cancelled?" aria-label="Cancel reason">' +
      '<button type="button" class="btn btn-navy" id="vCancel' + areaId + '">' + (v.status === 'PENDING' ? 'Withdraw' : 'Cancel') + ' ' + escapeHtml(v.voucherNo) + '</button></div>' : '') +
    '<div class="mir-error-text" id="vErr' + areaId + '"></div></section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  area.querySelectorAll('[data-open]').forEach(b => { b.onclick = () => stLoadVoucher(Number(b.dataset.open), areaId); });
  area.querySelectorAll('[data-receipt]').forEach(b => { b.onclick = () => stOpenReceipt(Number(b.dataset.receipt)); });
  const err = document.getElementById('vErr' + areaId);
  const act = async (path, body, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await apiStock('/vouchers/' + id + path, { method: 'POST', body });
      stLoadVoucher(id, areaId);
      if (areaId === 'slipDetail') stLoadSlips(); else stLoadMismatches();
      stPaintPending();
    } catch (e) { err.textContent = e.message; }
  };
  const approve = document.getElementById('vApprove' + areaId);
  if (approve) approve.onclick = () => act('/approve', { note: document.getElementById('vNote' + areaId).value.trim() }, 'Approve ' + v.voucherNo + '? Stock changes at once.');
  const reject = document.getElementById('vReject' + areaId);
  if (reject) reject.onclick = () => {
    const note = document.getElementById('vNote' + areaId).value.trim();
    if (!note) { err.textContent = 'Say why it is not approved.'; return; }
    act('/reject', { note });
  };
  const cancel = document.getElementById('vCancel' + areaId);
  if (cancel) cancel.onclick = () => {
    const reason = document.getElementById('vCancelReason' + areaId).value.trim();
    if (!reason) { err.textContent = 'Say why it is being cancelled.'; return; }
    act('/cancel', { reason }, (v.status === 'PENDING' ? 'Withdraw ' : 'Cancel ') + v.voucherNo + '? It stops counting at once.');
  };
  const ret = document.getElementById('vReturn');
  if (ret) ret.onclick = () => stReturnOpen(v);
}

function stOpenReceipt(lotId) {
  stShowView('viewRegister');
  document.getElementById('rgShowStock').click();
  stLoadReceipt(lotId);
}

// ── Open mismatches ───────────────────────────────────────────────────────
function stInitMismatches() {
  document.getElementById('mmExplain').innerHTML = '<b>What this is for.</b> When the stock on the floor is not what the register says - a physical count comes out short or over, ' +
    'or material is damaged, lost or taken as a sample - record it here against the MIR it belongs to. ' +
    (ST_META.isAdmin ? 'Differences you enter post at once; an editor\'s wait here for you to approve or turn down.'
      : 'It waits here, moving nothing, until an admin approves it or turns it down with a note.');
  const reload = stDebounce(stLoadMismatches, 250);
  ['mmStatus', 'mmPlant'].forEach(id => document.getElementById(id).addEventListener('input', reload));
  const btn = document.getElementById('mmNew');
  if (!stWritable().length) btn.hidden = true;
  btn.onclick = () => stOpenDiffForm();
}

function stOpenDiffForm() {
  const host = document.getElementById('diffHost');
  host.hidden = false;
  host.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function stLoadMismatches() {
  const area = document.getElementById('mismatchArea');
  const params = new URLSearchParams({ status: document.getElementById('mmStatus').value });
  const plant = document.getElementById('mmPlant').value;
  if (plant) params.set('plant', plant);
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  const current = stLoadTicket('mismatches');
  let list;
  try { list = (await apiStock('/differences?' + params.toString())).differences; } catch (e) {
    if (current()) area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (!current()) return;
  if (!list.length) { area.innerHTML = '<div class="mir-empty">Nothing here.</div>'; return; }
  area.innerHTML = '<div class="table-wrap"><table class="mir-click"><thead><tr><th>Number</th><th>Date</th><th>MIR</th><th>Material</th><th>Kind</th>' +
    '<th class="num">Register</th><th class="num">Counted</th><th class="num">Difference</th><th class="num">Value</th><th>Reason</th><th>Entered by</th><th>Status</th></tr></thead><tbody>' +
    list.map((d, i) => '<tr data-row="' + i + '" tabindex="0"><td class="nowrap"><b>' + escapeHtml(d.voucherNo) + '</b></td><td class="nowrap">' + stDate(d.date) + '</td>' +
      '<td class="nowrap">' + escapeHtml(d.receipt ? stMirRef(d.receipt) : '-') + '</td><td class="st-text">' + escapeHtml(d.material.name) + '</td>' +
      '<td>' + escapeHtml(ST_DIFF_LABEL[d.kind] || d.kind) + '</td>' +
      '<td class="num nowrap">' + (d.book !== null ? stQty(d.book, d.uom) : '-') + '</td><td class="num nowrap">' + (d.counted !== null ? stQty(d.counted, d.uom) : '-') + '</td>' +
      '<td class="num nowrap"><b>' + (d.direction > 0 ? '+' : '-') + stQty(d.qty, d.uom) + '</b></td><td class="num nowrap">' + (d.value !== null ? stMoney(d.value) : '-') + '</td>' +
      '<td class="st-text">' + escapeHtml(d.reason || '-') + (d.note ? '<div class="mir-muted">' + escapeHtml(d.note) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(d.createdBy) + '</td><td>' + stPill(d.statusLabel, ST_STATUS_TONE[d.status]) + '</td></tr>').join('') +
    '</tbody></table></div>';
  area.querySelectorAll('[data-row]').forEach(tr => {
    const open = () => stLoadVoucher(list[Number(tr.dataset.row)].voucherId, 'mismatchDetail');
    tr.onclick = open;
    tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
  });
}
