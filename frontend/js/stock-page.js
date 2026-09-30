// stock.html's page script - see stock.html's header comment. Self-contained
// like mir-page.js: its own fetch wrapper, no dependency on main.js.
//
// The page never draws stock or values a line itself. Every input change
// sends the whole form to /api/stock/preview (debounced) and paints what
// comes back - which receipts each line draws from, the value, the field
// errors, the notices. Saving sends the same body to /api/stock/vouchers/new,
// which re-runs the same check server-side with the lots locked. Inputs are
// never re-rendered while the storekeeper types (so focus and cursor
// survive a preview); only the computed figures are repainted.
//
// Every top-level name here starts with "st" / "ST_": this page shares one
// global scope with auth.js and shared.js, and a repeated `let`/`const`
// there is a SyntaxError that silently stops the page
// (test_frontend_global_names.py).

let ST_META = null;
const ST_FORMS = {};
// Stock rows per plant code, for the material pickers - refreshed after
// every save, so a picker never offers stock that has just gone out.
const ST_STOCK = {};

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
  stInitStockView();
  ST_FORMS.ISSUE = stIssueForm();
  ST_FORMS.RETURN = stReturnForm();
  ST_FORMS.ADJUST = stAdjustForm();
  stInitRegister();
  stPaintPending(ST_META.pendingApprovals);
  stLoadStock();
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
function stBanner(text) { const d = document.createElement('div'); d.className = 'mir-banner mir-banner-warn'; d.textContent = text; return d; }
function stPill(text, tone) { return '<span class="status-pill mir-pill-' + tone + '">' + escapeHtml(text) + '</span>'; }
function stPlantName(code) { const p = ST_META.plants.find(x => x.code === code); return p ? p.name : code; }
function stWritable() { return ST_META.plants.filter(p => p.canWrite); }
function stReasons(kind) { return ST_META.reasons.filter(r => r.kind === kind); }
function stDaysAgo(iso) {
  if (!iso) return null;
  const d = new Date(iso + 'T00:00:00');
  const t = new Date(ST_META.today + 'T00:00:00');
  return Math.round((t - d) / 86400000);
}
function stMinDate() {
  const d = new Date(ST_META.today + 'T00:00:00');
  d.setDate(d.getDate() - ST_META.backdateDays);
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}
const ST_STATUS_TONE = { POSTED: 'ok', PENDING: 'warn', REJECTED: 'bad', CANCELLED: 'bad' };
const ST_KIND_LABEL = { ISSUE: 'Issue', RETURN: 'Return', ADJUST: 'Adjustment', RECEIPT: 'MIR receipt' };

function stToast(text) {
  let stack = document.getElementById('stToasts');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'stToasts';
    stack.className = 'toast-stack';
    stack.setAttribute('role', 'status');
    document.body.appendChild(stack);
  }
  const el = document.createElement('div');
  el.className = 'toast success';
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

// ── View tabs ─────────────────────────────────────────────────────────────
const ST_TABS = [['tabStock', 'viewStock'], ['tabIssue', 'viewIssue'], ['tabReturn', 'viewReturn'], ['tabAdjust', 'viewAdjust'], ['tabRegister', 'viewRegister']];

function stShowView(viewId) {
  ST_TABS.forEach(([t, v]) => {
    const on = v === viewId;
    document.getElementById(t).classList.toggle('active', on);
    document.getElementById(t).setAttribute('aria-selected', String(on));
    document.getElementById(v).hidden = !on;
  });
  if (viewId === 'viewStock') stLoadStock();
  if (viewId === 'viewRegister') stLoadRegister();
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
  el.title = n ? n + ' adjustment' + (n === 1 ? '' : 's') + ' waiting for approval' : '';
}

async function stStockFor(plant, fresh) {
  if (!ST_STOCK[plant] || fresh) ST_STOCK[plant] = (await apiStock('/balances?plant=' + encodeURIComponent(plant))).rows;
  return ST_STOCK[plant];
}

// ── Stock on hand ─────────────────────────────────────────────────────────
function stInitStockView() {
  const sel = document.getElementById('stPlant');
  const readable = ST_META.plants.filter(p => p.canRead);
  sel.innerHTML = (readable.length > 1 ? '<option value="">All my plants</option>' : '') +
    readable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
  const reload = stDebounce(stLoadStock, 250);
  ['stPlant', 'stSearch', 'stZero'].forEach(id => document.getElementById(id).addEventListener('input', reload));
}

function stStockStatus(r) {
  if (!r.isStocked) return stPill('Not kept in store', 'muted');
  if (Number(r.qty) <= 0) return stPill('None left', 'muted');
  if (r.belowMin) return stPill('Below minimum', 'bad');
  return stPill('In stock', 'ok');
}

async function stLoadStock() {
  const area = document.getElementById('stockArea');
  const plant = document.getElementById('stPlant').value;
  const q = document.getElementById('stSearch').value.trim();
  const zero = document.getElementById('stZero').checked;
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let rows;
  try {
    rows = (await apiStock('/balances?' + new URLSearchParams(Object.assign(plant ? { plant } : {}, q ? { q } : {})).toString())).rows;
  } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const shown = zero ? rows : rows.filter(r => Number(r.qty) !== 0);
  if (!shown.length) {
    area.innerHTML = '<div class="mir-empty">' + (rows.length ? 'Nothing left in stock for these filters. Tick "Include materials with none left" to see them.'
      : 'No stock yet. Stock comes in when a MIR is posted at the plant, or through an opening balance on the Adjust tab.') + '</div>';
    return;
  }
  const total = shown.reduce((s, r) => s + Number(r.value || 0), 0);
  const allPlants = !plant;
  area.innerHTML = '<div class="st-summary">' + shown.length + ' material' + (shown.length === 1 ? '' : 's') + ' &middot; ' + stMoney(total) + ' in stock (before GST)</div>' +
    '<div class="table-wrap"><table class="mir-click"><thead><tr><th>Material</th>' + (allPlants ? '<th>Plant</th>' : '') +
    '<th class="num">In stock</th><th class="num">Value</th><th class="num">Lots</th><th>Last received</th><th>Oldest stock</th><th>Minimum</th><th>Status</th></tr></thead><tbody>' +
    shown.map((r, i) => {
      const age = stDaysAgo(r.oldestHeld);
      return '<tr data-row="' + i + '" tabindex="0"><td><b>' + escapeHtml(r.material.name) + '</b><div class="mir-muted">' + escapeHtml(r.material.category || 'No category') + '</div></td>' +
        (allPlants ? '<td>' + escapeHtml(r.plant.name) + '</td>' : '') +
        '<td class="num nowrap">' + stQty(r.qty, r.uom) + (Number(r.otherCurrencyQty) ? '<div class="mir-muted">' + stQty(r.otherCurrencyQty, r.uom) + ' billed in another currency</div>' : '') + '</td>' +
        '<td class="num nowrap">' + stMoney(r.value) + '</td><td class="num">' + r.openLots + '</td>' +
        '<td class="nowrap">' + stDate(r.lastReceived) + '</td>' +
        '<td class="nowrap">' + (r.oldestHeld ? stDate(r.oldestHeld) + '<div class="mir-muted">' + age + ' day' + (age === 1 ? '' : 's') + ' in store</div>' : '-') + '</td>' +
        '<td class="nowrap">' + (r.minLevel ? stQty(r.minLevel, r.uom) : '-') + '</td><td>' + stStockStatus(r) + '</td></tr>';
    }).join('') + '</tbody></table></div>';
  area.querySelectorAll('[data-row]').forEach(tr => {
    const r = shown[Number(tr.dataset.row)];
    const open = () => stLoadMaterial(r.material.id, r.plant.code, r.uom);
    tr.onclick = open;
    tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
  });
}

async function stLoadMaterial(materialId, plant, uom) {
  const area = document.getElementById('stockDetail');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let d;
  try {
    d = await apiStock('/materials/' + materialId + '?' + new URLSearchParams({ plant, uom }).toString());
  } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const held = d.lots.filter(l => Number(l.balance) > 0);
  const onHand = held.reduce((s, l) => s + Number(l.balance), 0);
  const value = held.reduce((s, l) => s + Number(l.value || 0), 0);
  const s = d.setting;
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(d.material.name) + '</h3>' + escapeHtml(d.plant.name) + '</div>' +
    stFacts([['In stock', stQty(String(onHand), d.uom)], ['Value (before GST)', stMoney(String(value))],
      ['Category', [d.material.category, d.material.subcategory].filter(Boolean).join(' / ')],
      ['Minimum level', s.minLevel ? stQty(s.minLevel, s.minLevelUom || d.uom) : 'Not set'],
      ['Kept in store', s.isStocked ? 'Yes' : 'No - receipts go straight to use']]) +
    (d.canWrite ? '<div class="st-settings"><h4 class="mir-subtitle">Store settings</h4>' +
      '<div class="mir-grid">' +
        '<label class="st-check"><input type="checkbox" id="setStocked"' + (s.isStocked ? ' checked' : '') + '> This plant keeps it in store</label>' +
        '<div class="form-group"><label class="form-label" for="setMin">Minimum level (' + escapeHtml(d.uom || 'units') + ')</label>' +
          '<input class="form-control" id="setMin" inputmode="decimal" value="' + escapeHtml(s.minLevel || '') + '" placeholder="Blank = none"></div>' +
      '</div>' +
      '<p class="mir-hint">Not kept in store: its future MIRs record the receipt but put nothing into stock (for material that goes straight to use). Receipts already made keep what they are.' +
        (s.updatedBy ? ' Last changed by ' + escapeHtml(s.updatedBy) + '.' : '') + '</p>' +
      '<div class="mir-actions"><button type="button" class="btn btn-navy btn-small" id="setSave">Save settings</button><span class="mir-error-text" id="setErr"></span></div></div>' : '') +
    '<h4 class="mir-subtitle">Lots</h4><div class="table-wrap"><table><thead><tr><th>From</th><th>Received</th><th>Vendor</th>' +
      '<th class="num">Received qty</th><th class="num">Issued</th><th class="num">Returned</th><th class="num">Left</th><th class="num">Rate</th><th class="num">Value</th></tr></thead><tbody>' +
      d.lots.map(l => '<tr' + (Number(l.balance) > 0 ? '' : ' class="lot-used-up"') + '><td class="nowrap"><b>' + escapeHtml(l.doc) + '</b>' +
        (l.billToPlant ? '<div class="mir-muted">PO of ' + escapeHtml(l.billToPlant.name) + '</div>' : '') +
        (!l.stocked ? '<div class="mir-muted">Straight to use</div>' : '') + '</td>' +
        '<td class="nowrap">' + stDate(l.receivedDate) + '</td><td>' + escapeHtml(l.vendor || '-') + '</td>' +
        '<td class="num">' + stQty(l.in) + '</td><td class="num">' + stQty(l.drawn) + '</td><td class="num">' + stQty(l.returned) + '</td>' +
        '<td class="num"><b>' + stQty(l.balance) + '</b></td><td class="num">' + stRate(l.rate) + (l.currency !== 'INR' ? ' ' + escapeHtml(l.currency) : '') + '</td>' +
        '<td class="num">' + stMoney(l.value) + '</td></tr>').join('') +
    '</tbody></table></div>' +
    '<h4 class="mir-subtitle">Ledger</h4><div class="table-wrap"><table><thead><tr><th>Date</th><th>Document</th><th>What</th><th>Detail</th><th class="num">In / out</th><th class="num">Balance</th></tr></thead><tbody>' +
      d.ledger.map(e => '<tr' + (e.counts ? '' : ' class="lot-used-up"') + '><td class="nowrap">' + stDate(e.date) + '</td>' +
        '<td class="nowrap">' + (e.voucherId ? '<button type="button" class="mir-link" data-voucher="' + e.voucherId + '">' + escapeHtml(e.doc) + '</button>' : escapeHtml(e.doc)) + '</td>' +
        '<td>' + escapeHtml(ST_KIND_LABEL[e.kind] || e.kind) + '</td><td>' + escapeHtml(e.detail || '') + (e.note ? '<div class="mir-muted">' + escapeHtml(e.note) + '</div>' : '') + '</td>' +
        '<td class="num">' + (Number(e.qty) > 0 ? '+' : '') + stQty(e.qty) + '</td><td class="num"><b>' + stQty(e.balance) + '</b></td></tr>').join('') +
    '</tbody></table></div></section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  area.querySelectorAll('[data-voucher]').forEach(b => { b.onclick = () => { stShowView('viewRegister'); stLoadVoucher(Number(b.dataset.voucher)); }; });
  const save = document.getElementById('setSave');
  if (save) save.onclick = async () => {
    try {
      await apiStock('/settings', { method: 'POST', body: { plant, materialId, isStocked: document.getElementById('setStocked').checked,
        minLevel: document.getElementById('setMin').value.trim(), minLevelUom: d.uom } });
      stToast('Settings saved for ' + d.material.name + '.');
      stLoadMaterial(materialId, plant, uom);
      stLoadStock();
    } catch (e) { document.getElementById('setErr').textContent = e.message; }
  };
}

// ── The shared form machinery ─────────────────────────────────────────────
// One controller per form: its lines, the latest preview, whether a save was
// tried (fields turn red only after that), and a stale-response counter.
function stSetupForm(formId, kind, hooks) {
  const form = document.getElementById(formId);
  const view = form.closest('[role="tabpanel"]');
  const ctl = { kind, form, lines: [], preview: null, tried: false, seq: 0, hooks, extra: {} };
  if (!stWritable().length) {
    view.querySelector('[data-no-write]').hidden = false;
    form.hidden = true;
    return ctl;
  }
  const plantSel = form.querySelector('[data-field="plant"]');
  plantSel.innerHTML = stWritable().map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
  const date = form.querySelector('[data-field="voucher_date"]');
  date.value = ST_META.today;
  date.max = ST_META.today;
  date.min = stMinDate();
  form.querySelector('[data-date-hint]').textContent = 'Today, or up to ' + ST_META.backdateDays + ' days back.';
  ctl.schedule = stDebounce(() => stRunPreview(ctl), 350);
  form.addEventListener('input', e => { if (!e.target.closest('[data-nopreview]')) ctl.schedule(); });
  plantSel.addEventListener('change', () => { if (hooks.onPlant) hooks.onPlant(); ctl.schedule(); });
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

async function stRunPreview(ctl) {
  const bar = ctl.form.querySelector('[data-savebar]');
  if (!ctl.lines.length) { bar.hidden = true; ctl.preview = null; return; }
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
  ctl.form.querySelector('[data-notices]').innerHTML = (p.notices || []).map(n =>
    '<div class="mir-notice"><span class="mir-notice-icon" aria-hidden="true">!</span><span>' + escapeHtml(n) + '</span></div>').join('');
  const value = p.lines.reduce((s, l) => s + Number(l.value || 0), 0);
  ctl.form.querySelector('[data-totals]').innerHTML = '<div class="mir-total-tile ' + (p.ok ? 'is-ok' : '') + '"><span class="mir-kv-k">Value (before GST)</span>' +
    '<span class="mir-total-val">' + escapeHtml(stMoney(String(Math.abs(value)))) + '</span></div>';
}

const ST_FIELD_LABELS = {
  plant: 'the plant', voucher_date: 'the date', department: 'the department', issued_to: 'who it was issued to',
  lines: 'the lines', return_of: 'the issue', material_id: 'the material', qty: 'the quantity', counted: 'the counted quantity',
  reason: 'the reason', note: 'the note', rate: 'the rate', uom: 'the unit', mode: 'the kind of change', issue_line_id: 'the issue line',
};

function stFieldLabel(ctl, field) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(field || '');
  if (m) {
    const ln = ctl.lines[Number(m[1])];
    return (ST_FIELD_LABELS[m[2]] || m[2]) + ' on line ' + (Number(m[1]) + 1) + (ln && ln.name ? ' (' + ln.name.slice(0, 40) + ')' : '');
  }
  return ST_FIELD_LABELS[field] || field;
}

function stFieldEl(ctl, e) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(e.field || '');
  if (m) return ctl.form.querySelector('[data-line="' + ctl.hooks.lineKey(Number(m[1])) + '"][data-key="' + m[2] + '"]');
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
      stToast('Add at least one line first.');
    }
    return;
  }
  const btn = ctl.form.querySelector('[data-post]');
  btn.disabled = true;
  try {
    const plant = ctl.form.querySelector('[data-field="plant"]').value;
    const v = await apiStock('/vouchers/new', { method: 'POST', body: ctl.hooks.payload() });
    stToast(v.voucherNo + (v.status === 'PENDING' ? ' saved - waiting for an admin to approve it; stock changes once approved.' : ' saved.'));
    await stStockFor(plant, true).catch(() => null);
    ctl.hooks.reset();
    stPaintPending();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    await stRunPreview(ctl);
    stShowErrors(ctl, e.errors && e.errors.length ? e.errors : [{ field: '', message: e.message }]);
  } finally {
    btn.disabled = false;
  }
}

function stResetCommon(ctl) {
  const plant = ctl.form.querySelector('[data-field="plant"]').value;
  ctl.form.reset();
  ctl.form.querySelector('[data-field="plant"]').value = plant;
  ctl.form.querySelector('[data-field="voucher_date"]').value = ST_META.today;
  ctl.lines = []; ctl.preview = null; ctl.tried = false; ctl.extra = {};
  ['[data-todo]', '[data-notices]', '[data-totals]'].forEach(s => { ctl.form.querySelector(s).innerHTML = ''; });
  ctl.form.querySelector('[data-savebar]').hidden = true;
}

// "From MIR HRS/26-27/0003 (12/09/2026): 40 KG at Rs 50" - the lots a line draws.
function stDrawsHtml(draws, uom) {
  if (!draws || !draws.length) return '';
  return '<ul class="st-draws">' + draws.map(d => '<li>' + escapeHtml(d.doc) + ' <span class="mir-muted">(' + stDate(d.receivedDate) + ')</span>: ' +
    stQty(d.qty, uom) + ' at ' + stRate(d.rate) + '</li>').join('') + '</ul>';
}

// A material picker over this plant's stock rows (and, for additions, the
// material master): an input, and the hits under it.
function stPicker(input, hitsBox, source, onPick) {
  const run = stDebounce(async () => {
    const q = input.value.trim().toLowerCase();
    if (q.length < 2) { hitsBox.innerHTML = ''; return; }
    let hits;
    try { hits = await source(q); } catch (e) { hitsBox.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
    if (input.value.trim().toLowerCase() !== q) return;
    hitsBox.innerHTML = hits.length
      ? hits.slice(0, 12).map((h, i) => '<button type="button" class="st-hit" data-hit="' + i + '"' + (h.disabled ? ' disabled' : '') + '>' +
          '<b>' + escapeHtml(h.name) + '</b><span class="mir-muted">' + escapeHtml(h.sub || '') + '</span></button>').join('')
      : '<div class="mir-muted">Nothing matches.</div>';
    hitsBox.querySelectorAll('[data-hit]').forEach(b => { b.onclick = () => { onPick(hits[Number(b.dataset.hit)]); hitsBox.innerHTML = ''; input.value = ''; }; });
  }, 250);
  input.addEventListener('input', run);
}

function stStockHits(plant, q, opts) {
  return stStockFor(plant).then(rows => rows
    .filter(r => r.material.name.toLowerCase().includes(q) && (opts.withZero || Number(r.qty) > 0) && r.isStocked)
    .map(r => ({ materialId: r.material.id, name: r.material.name, uom: r.uom, qty: r.qty,
                 sub: stQty(r.qty, r.uom) + ' in stock' + (r.material.category ? ' - ' + r.material.category : ''),
                 disabled: opts.taken(r.material.id, r.uom) })));
}

// ── Issue ─────────────────────────────────────────────────────────────────
function stIssueForm() {
  let ctl;
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), { lines: ctl.lines.map(l => ({ material_id: l.materialId, uom: l.uom, qty: l.qty })) }),
    lineKey: i => i,
    paint: p => p.lines.forEach(pl => {
      const box = ctl.form.querySelector('[data-figs="' + pl.index + '"]');
      const ln = ctl.lines[pl.index];
      if (box && ln) box.innerHTML = stDrawsHtml(pl.draws, ln.uom) + (pl.value ? '<div class="mir-line-figures">Value <b>' + stMoney(pl.value) + '</b></div>' : '');
    }),
    reset: () => { stResetCommon(ctl); stIssueRender(ctl); },
    onPlant: () => { ctl.lines = []; stIssueRender(ctl); stIssueDepartments(ctl); },
  };
  ctl = stSetupForm('issueForm', 'ISSUE', hooks);
  if (ctl.form.hidden) return ctl;
  stIssueDepartments(ctl);
  const plant = () => ctl.form.querySelector('[data-field="plant"]').value;
  stPicker(document.getElementById('isFind'), document.getElementById('isHits'),
    q => stStockHits(plant(), q, { taken: (m, u) => ctl.lines.some(l => l.materialId === m && l.uom === u) }),
    hit => { ctl.lines.push({ materialId: hit.materialId, name: hit.name, uom: hit.uom, available: hit.qty, qty: '' }); stIssueRender(ctl); ctl.schedule(); });
  return ctl;
}

function stIssueDepartments(ctl) {
  const plant = ctl.form.querySelector('[data-field="plant"]').value;
  document.getElementById('isDeptList').innerHTML = (ST_META.departments[plant] || []).map(d => '<option value="' + escapeHtml(d) + '">').join('');
}

function stIssueRender(ctl) {
  const area = document.getElementById('isLines');
  area.innerHTML = ctl.lines.length ? ctl.lines.map((l, i) =>
    '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(l.name) + '</b>' +
      '<div class="mir-muted">' + stQty(l.available, l.uom) + ' in stock now</div></div></div>' +
      '<button type="button" class="mir-link" data-remove="' + i + '">Remove</button></div>' +
    '<div class="mir-line-grid"><div class="form-group"><label class="form-label" for="isQty' + i + '">Quantity going out (' + escapeHtml(l.uom || 'units') + ') <span class="req-mark">*</span></label>' +
      '<input class="form-control" id="isQty' + i + '" data-line="' + i + '" data-key="qty" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.qty) + '"></div></div>' +
    '<div data-figs="' + i + '"></div></div>').join('')
    : '<div class="mir-empty">No materials yet - find one above.</div>';
  area.querySelectorAll('[data-key="qty"]').forEach(inp => inp.addEventListener('input', () => { ctl.lines[Number(inp.dataset.line)].qty = inp.value; }));
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => { ctl.lines.splice(Number(b.dataset.remove), 1); stIssueRender(ctl); ctl.schedule(); }; });
}

// ── Return ────────────────────────────────────────────────────────────────
function stReturnForm() {
  let ctl;
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), {
      return_of: ctl.issue ? ctl.issue.id : null,
      lines: ctl.lines.filter(l => l.qty.trim() !== '').map(l => ({ issue_line_id: l.lineId, qty: l.qty, reason: l.reason, note: l.note })),
    }),
    // A return's lines on the wire are only the ones with a quantity, so
    // "lines.N" is the Nth of those - map it back to its card.
    lineKey: i => { const filled = ctl.lines.filter(l => l.qty.trim() !== ''); return filled[i] ? filled[i].lineId : ''; },
    paint: p => {
      const filled = ctl.lines.filter(l => l.qty.trim() !== '');
      ctl.lines.forEach(l => { const box = ctl.form.querySelector('[data-figs="' + l.lineId + '"]'); if (box) box.innerHTML = ''; });
      p.lines.forEach(pl => {
        const l = filled[pl.index];
        const box = l && ctl.form.querySelector('[data-figs="' + l.lineId + '"]');
        if (box) box.innerHTML = stDrawsHtml(pl.draws, l.uom).replace('<ul class="st-draws">', '<ul class="st-draws"><li class="mir-muted">Back to:</li>') +
          (pl.value ? '<div class="mir-line-figures">Value <b>' + stMoney(pl.value) + '</b></div>' : '');
      });
    },
    reset: () => { stResetCommon(ctl); ctl.issue = null; document.getElementById('rtIssue').innerHTML = ''; document.getElementById('rtLines').innerHTML = ''; document.getElementById('rtLinesPanel').hidden = true; },
    onPlant: () => { hooks.reset(); },
  };
  ctl = stSetupForm('returnForm', 'RETURN', hooks);
  if (ctl.form.hidden) return ctl;
  ctl.issue = null;
  const find = document.getElementById('rtFind');
  const hits = document.getElementById('rtHits');
  const run = stDebounce(async () => {
    const q = find.value.trim();
    if (q.length < 2) { hits.innerHTML = ''; return; }
    const plant = ctl.form.querySelector('[data-field="plant"]').value;
    let list;
    try { list = (await apiStock('/vouchers?' + new URLSearchParams({ kind: 'ISSUE', status: 'POSTED', plant, q }).toString())).vouchers; }
    catch (e) { hits.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
    if (find.value.trim() !== q) return;
    hits.innerHTML = list.length ? list.slice(0, 12).map((v, i) => '<button type="button" class="st-hit" data-hit="' + i + '"><b>' + escapeHtml(v.voucherNo) + '</b>' +
      '<span class="mir-muted">' + stDate(v.date) + ' - ' + escapeHtml(v.department) + ', ' + escapeHtml(v.issuedTo) + ' - ' + escapeHtml(v.materials) + '</span></button>').join('')
      : '<div class="mir-muted">No posted issue matches at this plant.</div>';
    hits.querySelectorAll('[data-hit]').forEach(b => { b.onclick = () => { hits.innerHTML = ''; find.value = ''; stReturnPick(ctl, list[Number(b.dataset.hit)].id); }; });
  }, 300);
  find.addEventListener('input', run);
  return ctl;
}

async function stReturnPick(ctl, issueId) {
  let issue;
  try { issue = await apiStock('/vouchers/' + issueId); } catch (e) { stToast(e.message); return; }
  ctl.issue = issue;
  ctl.lines = issue.returnable.filter(r => Number(r.stillOut) > 0).map(r => ({
    lineId: r.lineId, name: r.material.name, uom: r.uom, stillOut: r.stillOut, issued: r.issued, qty: '', reason: '', note: '' }));
  document.getElementById('rtIssue').innerHTML = '<div class="mir-po-summary"><span class="mir-po-chip"><span class="mir-kv-k">Issue</span> ' + escapeHtml(issue.voucherNo) + '</span>' +
    '<span class="mir-po-chip"><span class="mir-kv-k">Date</span> ' + stDate(issue.date) + '</span>' +
    '<span class="mir-po-chip"><span class="mir-kv-k">To</span> ' + escapeHtml(issue.department + ', ' + issue.issuedTo) + '</span>' +
    '<button type="button" class="mir-link" id="rtChange">Choose another issue</button></div>';
  document.getElementById('rtChange').onclick = () => ctl.hooks.reset();
  const reasons = stReasons('RETURN');
  const panel = document.getElementById('rtLinesPanel');
  panel.hidden = false;
  document.getElementById('rtLines').innerHTML = ctl.lines.length ? ctl.lines.map((l, i) =>
    '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(l.name) + '</b>' +
      '<div class="mir-muted">Issued ' + stQty(l.issued, l.uom) + ', still out ' + stQty(l.stillOut, l.uom) + '</div></div></div></div>' +
    '<div class="mir-line-grid">' +
      '<div class="form-group"><label class="form-label" for="rtQty' + i + '">Quantity back (' + escapeHtml(l.uom || 'units') + ')</label>' +
        '<input class="form-control" id="rtQty' + i + '" data-line="' + l.lineId + '" data-key="qty" inputmode="decimal" autocomplete="off"></div>' +
      '<div class="form-group"><label class="form-label" for="rtReason' + i + '">Reason</label><select class="form-control" id="rtReason' + i + '" data-line="' + l.lineId + '" data-key="reason">' +
        '<option value="">Choose...</option>' + reasons.map(r => '<option value="' + escapeHtml(r.code) + '">' + escapeHtml(r.label) + '</option>').join('') + '</select></div>' +
      '<div class="form-group"><label class="form-label" for="rtNote' + i + '">Note</label><input class="form-control" id="rtNote' + i + '" data-line="' + l.lineId + '" data-key="note" maxlength="2000"></div>' +
    '</div><div data-figs="' + l.lineId + '"></div></div>').join('')
    : '<div class="mir-empty">Everything on ' + escapeHtml(issue.voucherNo) + ' has already come back.</div>';
  document.getElementById('rtLines').querySelectorAll('[data-key]').forEach(inp => {
    inp.addEventListener('input', () => { const l = ctl.lines.find(x => String(x.lineId) === inp.dataset.line); l[inp.dataset.key] = inp.value; });
  });
  ctl.schedule();
}

// ── Adjust ────────────────────────────────────────────────────────────────
const ST_MODE_LABEL = { count: 'Physical count', add: 'Add stock', remove: 'Write off' };

function stAdjustForm() {
  let ctl;
  let nextKey = 1;
  const hooks = {
    payload: () => Object.assign(stHeader(ctl), { lines: ctl.lines.map(l => ({
      mode: l.mode, material_id: l.materialId, uom: l.uom, qty: l.qty, counted: l.counted, reason: l.reason, note: l.note, rate: l.rate })) }),
    lineKey: i => (ctl.lines[i] ? ctl.lines[i].key : ''),
    paint: p => p.lines.forEach(pl => {
      const l = ctl.lines[pl.index];
      const box = l && ctl.form.querySelector('[data-figs="' + l.key + '"]');
      if (!box) return;
      let html = '';
      if (l.mode === 'count' && pl.book !== null) {
        const diff = Number(pl.counted) - Number(pl.book);
        html += '<div class="mir-line-figures">Books on the day <b>' + stQty(pl.book, l.uom) + '</b> &middot; counted <b>' + stQty(pl.counted, l.uom) + '</b> &middot; ' +
          (diff === 0 ? 'no difference' : (diff > 0 ? 'found <b>' + stQty(String(diff), l.uom) + '</b> more' : '<b>' + stQty(String(-diff), l.uom) + '</b> short')) + '</div>';
      }
      if (pl.direction > 0 && pl.rate) html += '<div class="mir-line-figures">Valued at <b>' + stRate(pl.rate) + '</b> a unit' + (l.rate ? '' : ' (the latest receipt\'s rate)') + '</div>';
      html += stDrawsHtml(pl.draws, l.uom);
      if (pl.value) html += '<div class="mir-line-figures">Value <b>' + stMoney(pl.value) + '</b></div>';
      box.innerHTML = html;
    }),
    reset: () => { stResetCommon(ctl); stAdjustRender(ctl); },
    onPlant: () => { ctl.lines = []; stAdjustRender(ctl); },
  };
  ctl = stSetupForm('adjustForm', 'ADJUST', hooks);
  document.getElementById('adjExplain').innerHTML = ST_META.isAdmin
    ? '<b>Adjustments you save post at once.</b> An editor\'s adjustment waits for an admin; yours is recorded as approved by you.'
    : '<b>An adjustment waits for an admin\'s approval.</b> Nothing changes in stock until an admin approves it in the Register. Use it for an opening balance, a physical count, damage, loss or a sample - never for material that came in with an invoice (that is a MIR) or went to production (that is an issue).';
  if (ctl.form.hidden) return ctl;
  document.getElementById('adPostHint').textContent = ST_META.isAdmin ? '' : 'Saved adjustments wait for an admin to approve them.';
  ctl.form.querySelectorAll('[data-add-mode]').forEach(b => {
    b.onclick = () => {
      ctl.lines.push({ key: 'a' + (nextKey++), mode: b.dataset.addMode, materialId: null, name: '', uom: '', qty: '', counted: '', reason: '', note: '', rate: '' });
      stAdjustRender(ctl);
      const inputs = ctl.form.querySelectorAll('[data-pick]');
      if (inputs.length) inputs[inputs.length - 1].focus();
    };
  });
  stAdjustRender(ctl);
  return ctl;
}

function stAdjustReasonOptions(l) {
  const opts = rs => rs.map(r => '<option value="' + escapeHtml(r.code) + '"' + (l.reason === r.code ? ' selected' : '') + '>' + escapeHtml(r.label) + '</option>').join('');
  if (l.mode === 'add') return opts(stReasons('ADJUST_IN'));
  if (l.mode === 'remove') return opts(stReasons('ADJUST_OUT'));
  return '<optgroup label="Found more than the books">' + opts(stReasons('ADJUST_IN')) + '</optgroup>' +
    '<optgroup label="Found less than the books">' + opts(stReasons('ADJUST_OUT')) + '</optgroup>';
}

function stAdjustRender(ctl) {
  const area = document.getElementById('adLines');
  const plant = ctl.form.querySelector('[data-field="plant"]').value;
  area.innerHTML = ctl.lines.length ? ctl.lines.map((l, i) => {
    const qtyField = l.mode === 'count'
      ? '<div class="form-group"><label class="form-label" for="adCounted' + l.key + '">Counted (' + escapeHtml(l.uom || 'units') + ') <span class="req-mark">*</span></label>' +
        '<input class="form-control" id="adCounted' + l.key + '" data-line="' + l.key + '" data-key="counted" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.counted) + '"></div>'
      : '<div class="form-group"><label class="form-label" for="adQty' + l.key + '">Quantity (' + escapeHtml(l.uom || 'units') + ') <span class="req-mark">*</span></label>' +
        '<input class="form-control" id="adQty' + l.key + '" data-line="' + l.key + '" data-key="qty" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.qty) + '"></div>';
    const unitField = l.mode === 'add' && l.materialId
      ? '<div class="form-group"><label class="form-label" for="adUom' + l.key + '">Unit <span class="req-mark">*</span></label><select class="form-control" id="adUom' + l.key + '" data-line="' + l.key + '" data-key="uom">' +
        '<option value="">Choose...</option>' + ST_META.units.map(u => '<option' + (l.uom === u ? ' selected' : '') + '>' + escapeHtml(u) + '</option>').join('') + '</select>' +
        '<span class="mir-hint">Weight is kept in KG.</span></div>'
      : '';
    const rateField = l.mode === 'add'
      ? '<div class="form-group"><label class="form-label" for="adRate' + l.key + '">Rate per unit (Rs, before GST)</label><input class="form-control" id="adRate' + l.key + '" data-line="' + l.key + '" data-key="rate" inputmode="decimal" autocomplete="off" value="' + escapeHtml(l.rate) + '" placeholder="Blank = latest receipt\'s rate"></div>'
      : '';
    return '<div class="mir-line"><div class="mir-line-head"><div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div><b>' + escapeHtml(ST_MODE_LABEL[l.mode]) + '</b>' +
        (l.materialId ? '<div>' + escapeHtml(l.name) + ' <button type="button" class="mir-link" data-unpick="' + l.key + '">change</button></div>' : '') + '</div></div>' +
        '<button type="button" class="mir-link" data-remove="' + l.key + '">Remove</button></div>' +
      (l.materialId ? '' : '<div class="form-group" data-nopreview><label class="form-label" for="adPick' + l.key + '">Material <span class="req-mark">*</span></label>' +
        '<input class="form-control" type="search" id="adPick' + l.key + '" data-pick="' + l.key + '" data-line="' + l.key + '" data-key="material_id" autocomplete="off" placeholder="' +
        (l.mode === 'add' ? 'Any material, e.g. SBR 1502' : 'Material in stock at this plant') + '"><div class="st-hits" data-hits="' + l.key + '"></div></div>') +
      (l.materialId ? '<div class="mir-line-grid">' + qtyField + unitField + rateField +
        '<div class="form-group"><label class="form-label" for="adReason' + l.key + '">Reason <span class="req-mark">*</span></label><select class="form-control" id="adReason' + l.key + '" data-line="' + l.key + '" data-key="reason"><option value="">Choose...</option>' + stAdjustReasonOptions(l) + '</select></div>' +
        '<div class="form-group"><label class="form-label" for="adNote' + l.key + '">Note</label><input class="form-control" id="adNote' + l.key + '" data-line="' + l.key + '" data-key="note" maxlength="2000" value="' + escapeHtml(l.note) + '"></div>' +
      '</div>' : '') +
      '<div data-figs="' + l.key + '"></div></div>';
  }).join('') : '<div class="mir-empty">No lines yet - pick the kind of change above.</div>';
  area.querySelectorAll('[data-line][data-key]:not([data-pick])').forEach(inp => {
    inp.addEventListener('input', () => { const l = ctl.lines.find(x => x.key === inp.dataset.line); l[inp.dataset.key] = inp.value; });
  });
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => { ctl.lines = ctl.lines.filter(l => l.key !== b.dataset.remove); stAdjustRender(ctl); ctl.schedule(); }; });
  area.querySelectorAll('[data-unpick]').forEach(b => {
    b.onclick = () => { const l = ctl.lines.find(x => x.key === b.dataset.unpick); Object.assign(l, { materialId: null, name: '', uom: '' }); stAdjustRender(ctl); ctl.schedule(); };
  });
  area.querySelectorAll('[data-pick]').forEach(inp => {
    const l = ctl.lines.find(x => x.key === inp.dataset.pick);
    const taken = (m, u) => ctl.lines.some(o => o !== l && o.materialId === m && o.uom === u);
    const source = async q => {
      const stock = await stStockHits(plant, q, { withZero: true, taken });
      if (l.mode !== 'add') return stock;
      // An addition may be of a material this plant holds none of yet (an
      // opening balance): the material master too, minus what stock lists.
      const master = (await apiStock('/material-search?q=' + encodeURIComponent(q))).materials
        .filter(m => !stock.some(s => s.materialId === m.id))
        .map(m => ({ materialId: m.id, name: m.name, uom: m.uom, sub: 'none in stock here yet' + (m.category ? ' - ' + m.category : ''), disabled: taken(m.id, m.uom) }));
      return stock.concat(master);
    };
    stPicker(inp, area.querySelector('[data-hits="' + l.key + '"]'), source, hit => {
      Object.assign(l, { materialId: hit.materialId, name: hit.name, uom: hit.uom || '' });
      stAdjustRender(ctl);
      ctl.schedule();
    });
  });
}

// ── Register ──────────────────────────────────────────────────────────────
function stInitRegister() {
  const readable = ST_META.plants.filter(p => p.canRead);
  document.getElementById('rgPlant').innerHTML = '<option value="">All my plants</option>' +
    readable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
  const reload = stDebounce(stLoadRegister, 300);
  ['rgPlant', 'rgKind', 'rgStatus', 'rgSearch', 'rgFrom', 'rgTo'].forEach(id => document.getElementById(id).addEventListener('input', reload));
  // Pending approvals first for an admin who has some to look at.
  if (ST_META.isAdmin && ST_META.pendingApprovals) document.getElementById('rgStatus').value = 'PENDING';
}

async function stLoadRegister() {
  const area = document.getElementById('registerArea');
  const params = new URLSearchParams();
  [['plant', 'rgPlant'], ['kind', 'rgKind'], ['status', 'rgStatus'], ['q', 'rgSearch'], ['from', 'rgFrom'], ['to', 'rgTo']].forEach(([k, id]) => {
    const v = document.getElementById(id).value.trim();
    if (v) params.set(k, v);
  });
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  try {
    const list = (await apiStock('/vouchers?' + params.toString())).vouchers;
    if (!list.length) { area.innerHTML = '<div class="mir-empty">Nothing matches these filters.</div>'; return; }
    area.innerHTML = '<div class="table-wrap"><table class="mir-click"><thead><tr><th>Number</th><th>Date</th><th>Kind</th><th>Plant</th><th>For / why</th><th>Materials</th><th>Status</th><th>Entered by</th></tr></thead><tbody>' +
      list.map(v => '<tr data-voucher="' + v.id + '" tabindex="0"><td class="nowrap"><b>' + escapeHtml(v.voucherNo) + '</b></td><td class="nowrap">' + stDate(v.date) + '</td>' +
        '<td>' + escapeHtml(v.kindLabel) + '</td><td>' + escapeHtml(v.plant.name) + '</td>' +
        '<td>' + escapeHtml(v.kind === 'ISSUE' ? v.department + ', ' + v.issuedTo : (v.returnOf ? 'From ' + v.returnOf.voucherNo : '')) + '</td>' +
        '<td>' + escapeHtml(v.materials) + '</td><td>' + stPill(v.statusLabel, ST_STATUS_TONE[v.status]) + '</td><td>' + escapeHtml(v.createdBy) + '</td></tr>').join('') +
      '</tbody></table></div>';
    area.querySelectorAll('[data-voucher]').forEach(tr => {
      const open = () => stLoadVoucher(Number(tr.dataset.voucher));
      tr.onclick = open;
      tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
    });
  } catch (e) {
    area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

async function stLoadVoucher(id) {
  const area = document.getElementById('voucherDetail');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let v;
  try { v = await apiStock('/vouchers/' + id); } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const stillOut = v.returnable.some(r => Number(r.stillOut) > 0);
  const canReturn = stillOut && (ST_META.plants.find(p => p.code === v.plant.code) || {}).canWrite;
  const value = v.lines.reduce((s, l) => s + Number(l.value || 0), 0);
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(v.voucherNo) + '</h3>' + stPill(v.statusLabel, ST_STATUS_TONE[v.status]) +
      (canReturn ? '<button type="button" class="btn btn-navy btn-small" id="vReturn">Take material back from this issue</button>' : '') + '</div>' +
    stFacts([['Kind', v.kindLabel], ['Plant', v.plant.name], ['Date', stDate(v.date)], ['Department', v.department], ['Issued to', v.issuedTo],
      ['Production order / batch', v.reference], ['Returns material from', v.returnOf ? v.returnOf.voucherNo : ''],
      ['Value (before GST)', v.status === 'PENDING' ? 'Worked out on approval' : stMoney(String(Math.abs(value)))],
      ['Entered by', v.createdBy + ', ' + new Date(v.createdAt).toLocaleString('en-IN')],
      ['Approval', v.decidedBy ? (v.status === 'REJECTED' ? 'Not approved by ' : 'Approved by ') + v.decidedBy + (v.decisionNote ? ': ' + v.decisionNote : '') : ''],
      ['Cancelled', v.cancelReason ? v.cancelledBy + ': ' + v.cancelReason : ''], ['Remarks', v.remarks]]) +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>Material</th><th class="num">Quantity</th><th>Reason</th><th>From / back to</th><th class="num">Value</th></tr></thead><tbody>' +
      v.lines.map(l => '<tr><td>' + l.lineNo + '</td><td>' + escapeHtml(l.material.name) + '</td>' +
        '<td class="num nowrap">' + (l.direction > 0 ? '+' : '-') + stQty(l.qty, l.uom) +
          (l.counted !== null ? '<div class="mir-muted">Counted ' + stQty(l.counted, l.uom) + ', books ' + stQty(l.book, l.uom) + '</div>' : '') + '</td>' +
        '<td>' + escapeHtml(l.reason || '-') + (l.note ? '<div class="mir-muted">' + escapeHtml(l.note) + '</div>' : '') + '</td>' +
        '<td>' + (l.draws.length ? stDrawsHtml(l.draws, l.uom) : (l.rate ? 'New lot at ' + stRate(l.rate) : '-')) + '</td>' +
        '<td class="num">' + (l.value !== null ? stMoney(l.value) : '-') + '</td></tr>').join('') +
    '</tbody></table></div>' +
    (v.returns.length ? '<h4 class="mir-subtitle">Returns against this issue</h4><p>' + v.returns.map(r => '<button type="button" class="mir-link" data-open="' + r.id + '">' + escapeHtml(r.voucherNo) + '</button> (' + escapeHtml(r.status.toLowerCase()) + ')').join(', ') + '</p>' : '') +
    (v.canApprove ? '<div class="mir-cancel"><input class="form-control" id="vNote" maxlength="500" placeholder="Note (required to turn it down)" aria-label="Approval note">' +
      '<button type="button" class="btn btn-primary" id="vApprove">Approve</button><button type="button" class="btn btn-navy" id="vReject">Turn down</button></div>' : '') +
    (v.canCancel ? '<div class="mir-cancel"><input class="form-control" id="vCancelReason" maxlength="500" placeholder="Why is this being cancelled?" aria-label="Cancel reason">' +
      '<button type="button" class="btn btn-navy" id="vCancel">' + (v.status === 'PENDING' ? 'Withdraw' : 'Cancel') + ' ' + escapeHtml(v.voucherNo) + '</button></div>' : '') +
    '<div class="mir-error-text" id="vErr"></div></section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  area.querySelectorAll('[data-open]').forEach(b => { b.onclick = () => stLoadVoucher(Number(b.dataset.open)); });
  const act = async (path, body, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await apiStock('/vouchers/' + id + path, { method: 'POST', body });
      stLoadVoucher(id);
      stLoadRegister();
      stPaintPending();
      Object.keys(ST_STOCK).forEach(k => delete ST_STOCK[k]);
    } catch (e) { document.getElementById('vErr').textContent = e.message; }
  };
  const approve = document.getElementById('vApprove');
  if (approve) approve.onclick = () => act('/approve', { note: document.getElementById('vNote').value.trim() }, 'Approve ' + v.voucherNo + '? Stock changes at once.');
  const reject = document.getElementById('vReject');
  if (reject) reject.onclick = () => {
    const note = document.getElementById('vNote').value.trim();
    if (!note) { document.getElementById('vErr').textContent = 'Say why it is not approved.'; return; }
    act('/reject', { note });
  };
  const cancel = document.getElementById('vCancel');
  if (cancel) cancel.onclick = () => {
    const reason = document.getElementById('vCancelReason').value.trim();
    if (!reason) { document.getElementById('vErr').textContent = 'Say why it is being cancelled.'; return; }
    act('/cancel', { reason }, (v.status === 'PENDING' ? 'Withdraw ' : 'Cancel ') + v.voucherNo + '? It stops counting at once.');
  };
  const ret = document.getElementById('vReturn');
  if (ret) ret.onclick = () => {
    const ctl = ST_FORMS.RETURN;
    stShowView('viewReturn');
    ctl.form.querySelector('[data-field="plant"]').value = v.plant.code;
    stReturnPick(ctl, v.id);
  };
}
