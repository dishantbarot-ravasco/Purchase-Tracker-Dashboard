// mir.html's page script - see mir.html's header comment. Self-contained
// like review-page.js: its own fetch wrapper, no dependency on main.js.
//
// The page never prices a line or decides a mismatch itself. Every input
// change sends the whole form to /api/mir/preview (debounced) and paints
// what comes back - the figures, the differences that need a reason, the
// field errors, the notices. Saving sends the same body to
// /api/mir/entries/new, which re-runs the same check server-side inside the
// transaction. Inputs are never re-rendered while the clerk types, so focus
// and cursor survive a preview; only the computed cells and the difference
// cards are repainted.

let META = null;
// The debounced preview, built once in initNewMir().
let schedulePreview = () => {};
const S = {
  lines: [],          // [{line: <po line payload>, po: <po summary>, v: {input values}}]
  vendor: null,       // the MIR's vendor: from the POs, or chosen for a PO with none
  vendorFromPo: false,
  taxTouched: false,
  header: {},         // reason/note values for tax type and invoice total
  preview: null,
  previewSeq: 0,
  triedToPost: false,
};

// Each difference the server can report, what reason list answers it, and
// the payload keys its reason and note travel under.
const DIFF = {
  QTY_SHORT: { reasonKind: 'QTY_SHORT', key: 'qty', title: 'Quantity short' },
  QTY_OVER: { reasonKind: 'QTY_OVER', key: 'qty', title: 'Quantity over' },
  QTY_REJECTED: { reasonKind: 'REJECTION', key: 'reject', title: 'Quantity rejected' },
  RATE_HIGH: { reasonKind: 'RATE', key: 'rate', title: 'Rate above the PO' },
  RATE_LOW: { reasonKind: 'RATE', key: 'rate', title: 'Rate below the PO' },
  GST_RATE: { reasonKind: 'GST_RATE', key: 'gst', title: 'GST rate differs from the PO' },
  INVOICE_TOTAL: { reasonKind: 'INVOICE_TOTAL', key: 'invoice_total', title: 'Invoice total does not add up' },
  TAX_TYPE: { reasonKind: 'TAX_TYPE', key: 'tax_type', title: 'Tax type differs' },
};

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'mir');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
  initViewTabs();
  try {
    META = await apiMir('/meta');
  } catch (e) {
    document.getElementById('main-content').prepend(bannerEl(e.message));
    return;
  }
  initNewMir();
  initRegister();
  initMismatches();
  refreshMismatchCount();
})();

/** /api/mir/... fetch wrapper - same shape as review-page.js's apiReview(). */
async function apiMir(path, opts) {
  const o = Object.assign({ credentials: 'same-origin' }, opts || {});
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
  }
  const res = await authFetch('/api/mir' + path, o);
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
// Exact figures, never the dashboard's rounded KPI shorthand (formatInr()):
// this is a record being entered, and every paisa is compared. The currency
// is the PO's own (PurchaseOrder.currency), never assumed.
function money(s, currency) {
  if (s === null || s === undefined || s === '') return '-';
  const n = Number(s);
  try {
    return n.toLocaleString('en-IN', { style: 'currency', currency: currency || 'INR', minimumFractionDigits: 2, maximumFractionDigits: 4 });
  } catch (e) {
    return (currency || '') + ' ' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }
}
function qty(s) {
  if (s === null || s === undefined || s === '') return '-';
  return Number(s).toLocaleString('en-IN', { maximumFractionDigits: 3 });
}
// "115.0000" -> "115", "12.5000" -> "12.5": the PO rate as a person would type it.
function trimZeros(s) { return s === null || s === undefined ? '' : String(s).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, ''); }
function dateIN(iso) { return iso ? formatDateIN(iso) : '-'; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function bannerEl(text) { const d = document.createElement('div'); d.className = 'mir-banner mir-banner-warn'; d.textContent = text; return d; }
function reasonsOf(kind) { return META.reasons.filter(r => r.kind === kind); }
function reasonByCode(code) { return META.reasons.find(r => r.code === code); }
function plantName(code) { const p = META.plants.find(x => x.code === code); return p ? p.name : code; }
function req() { return ' <span class="req-mark" aria-hidden="true">*</span>'; }
// The MIR's currency: its lines' PO currency (one vendor, one invoice).
function mirCurrency() { return S.lines.length ? (S.lines[0].line.currency || 'INR') : 'INR'; }
function statusPill(text, tone) { return '<span class="status-pill mir-pill-' + tone + '">' + escapeHtml(text) + '</span>'; }
const MIR_STATUS_TONE = { POSTED: 'ok', CANCELLED: 'bad' };
const MISMATCH_STATUS_TONE = { OPEN: 'warn', RESOLVED: 'ok', VOID: 'muted' };

// ── View tabs ─────────────────────────────────────────────────────────────
function initViewTabs() {
  const tabs = [['tabNew', 'viewNew'], ['tabRegister', 'viewRegister'], ['tabMismatches', 'viewMismatches']];
  tabs.forEach(([tabId, viewId]) => {
    document.getElementById(tabId).onclick = () => {
      tabs.forEach(([t, v]) => {
        const on = t === tabId;
        document.getElementById(t).classList.toggle('active', on);
        document.getElementById(t).setAttribute('aria-selected', String(on));
        document.getElementById(v).hidden = !on;
      });
      if (viewId === 'viewRegister') loadRegister();
      if (viewId === 'viewMismatches') loadMismatches();
    };
  });
}

// ── New MIR ───────────────────────────────────────────────────────────────
function initNewMir() {
  const receivable = META.plants.filter(p => p.canReceive);
  if (!receivable.length) {
    document.getElementById('noReceive').hidden = false;
    document.getElementById('mirForm').hidden = true;
    return;
  }
  const plantSel = document.getElementById('plantSel');
  plantSel.innerHTML = receivable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
  const mirDate = document.getElementById('mirDate');
  mirDate.value = META.today;
  mirDate.max = META.today;
  document.getElementById('invoiceDate').max = META.today;
  document.getElementById('taxType').innerHTML = META.taxTypes.map(t => '<option value="' + t.code + '">' + escapeHtml(t.label) + '</option>').join('');

  const search = debounce(runSearch, 300);
  document.getElementById('poSearch').addEventListener('input', search);
  schedulePreview = debounce(runPreview, 350);
  const schedule = schedulePreview;
  ['plantSel', 'mirDate', 'invoiceNo', 'invoiceDate', 'invoiceTotal', 'tcsAmount', 'sapGrnNo'].forEach(id =>
    document.getElementById(id).addEventListener('input', schedule));
  document.getElementById('taxType').addEventListener('change', () => { S.taxTouched = true; schedule(); });
  document.getElementById('mirForm').addEventListener('submit', e => { e.preventDefault(); postMir(); });
  document.getElementById('newAnotherBtn').onclick = resetForm;
  document.querySelectorAll('[data-goto]').forEach(b => {
    b.onclick = () => {
      const target = document.getElementById(b.dataset.goto);
      if (target && !target.hidden) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
  });
  updateProgress(null);
}

/** The four-step strip above the form: done, current, or still to come.
    Worked out from the latest preview, so it never disagrees with Save. */
function updateProgress(p) {
  const errs = p ? p.errors : [];
  const has = prefix => errs.some(e => (e.field || '').startsWith(prefix));
  const receipt = !has('plant') && !has('mir_date') && !!document.getElementById('mirDate').value;
  const po = S.lines.length > 0;
  const invoice = po && !!p && !['invoice_no', 'invoice_date', 'invoice_total', 'tcs_amount', 'tax_type', 'vendor_id']
    .some(f => errs.some(e => e.field === f || e.field === f + '_reason' || e.field === f + '_note'));
  const lines = po && !!p && !has('lines');
  const done = { 1: receipt, 2: po, 3: invoice, 4: lines };
  let currentSet = false;
  document.querySelectorAll('#mirProgress li').forEach(li => {
    const n = Number(li.dataset.step);
    li.classList.toggle('is-done', !!done[n]);
    const current = !done[n] && !currentSet;
    if (current) currentSet = true;
    li.classList.toggle('is-current', current);
  });
}

async function runSearch() {
  const q = document.getElementById('poSearch').value.trim();
  const box = document.getElementById('poResults');
  if (q.length < 2) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="mir-muted">Searching...</div>';
  try {
    const data = await apiMir('/open-pos?q=' + encodeURIComponent(q));
    if (document.getElementById('poSearch').value.trim() !== q) return;
    if (!data.purchaseOrders.length) {
      box.innerHTML = '<div class="mir-empty">No open PO number contains "' + escapeHtml(q) + '". If the PO exists, it may be missing from the master PO sheet, or every line may already be received - ask purchase.</div>';
      return;
    }
    box.innerHTML = data.purchaseOrders.map(poResultHtml).join('');
    box.querySelectorAll('[data-open-po]').forEach(b => { b.onclick = () => openPo(Number(b.dataset.openPo), b.closest('.mir-po')); });
  } catch (e) {
    box.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

function vendorClash(po) {
  return S.lines.length && S.vendor && po.vendor && po.vendor.id !== S.vendor.id;
}

function poResultHtml(po) {
  const clash = vendorClash(po);
  return '<div class="mir-po' + (clash ? ' is-disabled' : '') + '">' +
    '<div class="mir-po-head">' +
      '<div class="mir-po-id"><b>PO ' + escapeHtml(po.poNumber) + '</b><span class="mir-plant-tag">' + escapeHtml(po.plant.name) + '</span></div>' +
      '<div class="mir-po-vendor">' + escapeHtml(po.vendor ? po.vendor.name : 'No vendor on the PO') + '</div>' +
      '<div class="mir-muted">Dated ' + dateIN(po.poDate) + ' &middot; ' + po.openLines + ' of ' + po.totalLines + ' lines open</div>' +
      (clash ? '<div class="mir-muted">Another vendor - one MIR is one invoice</div>'
        : '<button type="button" class="btn btn-navy btn-small" data-open-po="' + po.id + '">Show lines</button>') +
    '</div><div class="mir-po-lines"></div></div>';
}

function poHeaderHtml(po) {
  const item = (k, v) => v ? '<div class="mir-kv-item"><span class="mir-kv-k">' + escapeHtml(k) + '</span><span class="mir-kv-v">' + escapeHtml(v) + '</span></div>' : '';
  const tax = META.taxTypes.find(t => t.code === po.taxType);
  const chip = (k, v) => v ? '<span class="mir-po-chip"><span class="mir-kv-k">' + escapeHtml(k) + '</span> ' + escapeHtml(v) + '</span>' : '';
  const more = item('Payment terms', po.paymentTerms) + item('Incoterms', po.incoterms) +
    item('Value (excl. GST)', po.totalValue ? money(po.totalValue, po.currency) : '') +
    item('Bill to', po.billingAddress) + item('Ship to', po.shipTo) + item('Vendor address', po.vendorAddress) +
    item('PO remarks', po.remarks);
  return '<div class="mir-po-summary">' +
      chip('PO date', dateIN(po.poDate)) + chip('Tax', tax ? tax.label : po.taxTypeRaw) +
      chip('GST', po.gstRate ? Number(po.gstRate) + '%' : '') + chip('Currency', po.currency) +
      chip('Value incl. GST', po.totalInclusiveValue ? money(po.totalInclusiveValue, po.currency) : '') +
    '</div>' +
    (more ? '<details class="mir-po-more"><summary>More PO details (terms, addresses, remarks)</summary><div class="mir-po-facts">' + more + '</div></details>' : '');
}

async function openPo(poId, el) {
  const target = el.querySelector('.mir-po-lines');
  target.innerHTML = '<div class="mir-muted">Loading lines...</div>';
  try {
    const po = await apiMir('/purchase-orders/' + poId);
    const picked = new Set(S.lines.map(l => l.line.id));
    target.innerHTML = poHeaderHtml(po) +
      '<div class="table-wrap mir-table-wrap"><table><thead><tr><th></th><th>#</th><th>Material</th><th>HSN</th><th>Unit</th><th class="num">Ordered</th>' +
      '<th class="num">Received</th><th class="num">Open</th><th class="num">PO rate</th><th>Delivery</th></tr></thead><tbody>' +
      po.lines.map(l => {
        const late = l.deliveryDate && l.deliveryDate < META.today && l.receivable;
        return '<tr class="' + (l.receivable ? '' : 'is-disabled') + '">' +
          '<td>' + (l.receivable ? '<input type="checkbox" data-pick="' + l.id + '"' + (picked.has(l.id) ? ' checked disabled' : '') + ' aria-label="Pick line ' + l.lineNo + '">' : '') + '</td>' +
          '<td>' + l.lineNo + '</td>' +
          '<td>' + escapeHtml(l.description) + (l.itemCode ? ' <span class="mir-muted">' + escapeHtml(l.itemCode) + '</span>' : '') +
            (l.receivable ? '' : '<div class="mir-muted">' + escapeHtml(l.blockedReason) + '</div>') + '</td>' +
          '<td>' + escapeHtml(l.hsn || '-') + '</td>' +
          '<td>' + escapeHtml(l.uom || '-') + (l.uomKnown ? '' : ' <span class="mir-flag" title="Unit not recognised on the PO sheet">?</span>') + '</td>' +
          '<td class="num">' + qty(l.qtyOrdered) + '</td><td class="num">' + qty(l.accepted) + '</td>' +
          '<td class="num"><b>' + qty(l.openQty) + '</b></td><td class="num">' + money(l.rate, l.currency) + '</td>' +
          '<td>' + dateIN(l.deliveryDate) + (late ? ' ' + statusPill('late', 'bad') : '') + '</td></tr>';
      }).join('') + '</tbody></table></div>' +
      '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" data-add>Add ticked lines to this MIR</button></div>';
    target.querySelector('[data-add]').onclick = () => {
      const ids = [...target.querySelectorAll('[data-pick]:checked:not(:disabled)')].map(c => Number(c.dataset.pick));
      addLines(po, po.lines.filter(l => ids.includes(l.id)));
      openPo(poId, el);
    };
  } catch (e) {
    target.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

function addLines(po, lines) {
  if (!lines.length) return;
  if (!S.lines.length) {
    S.vendor = po.vendor;
    S.vendorFromPo = !!po.vendor;
  } else if (!S.vendor && po.vendor) {
    S.vendor = po.vendor;
    S.vendorFromPo = true;
  }
  lines.forEach(line => {
    if (S.lines.some(x => x.line.id === line.id)) return;
    S.lines.push({ line, po, v: {
      qty_received: '', qty_rejected: '', rate: trimZeros(line.rate), discount: '',
      gst_rate: line.poGstRate ? trimZeros(line.poGstRate) : '', dept_use: '',
      material_category: line.suggestedCategory || '', material_subcategory: line.suggestedSubcategory || '',
      qty_reason: '', qty_note: '', rate_reason: '', rate_note: '', reject_reason: '', reject_note: '', gst_reason: '', gst_note: '',
    } });
  });
  showEntrySections(true);
  document.querySelectorAll('[data-currency]').forEach(el => { el.textContent = mirCurrency(); });
  renderVendor();
  renderLines();
  schedulePreview();
  updateProgress(S.preview);
  const first = document.querySelector('[data-idx="0"][data-key="qty_received"]');
  document.getElementById('invoiceCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (first && !first.value) setTimeout(() => document.getElementById('invoiceNo').focus({ preventScroll: true }), 400);
}

function renderVendor() {
  const box = document.getElementById('vendorBox');
  if (S.vendor) {
    box.innerHTML = '<span class="mir-kv-k">Vendor</span> <b>' + escapeHtml(S.vendor.name) + '</b>' +
      (S.vendor.gstin ? ' <span class="mir-muted">GSTIN ' + escapeHtml(S.vendor.gstin) + '</span>' : ' <span class="mir-flag">no GSTIN on file</span>') +
      (S.vendorFromPo ? '' : ' <button type="button" class="mir-link" id="changeVendor">change</button>');
    const change = document.getElementById('changeVendor');
    if (change) change.onclick = () => { S.vendor = null; renderVendor(); schedulePreview(); };
    return;
  }
  box.innerHTML = '<div class="form-group"><label class="form-label" for="vendorSearch">Vendor on the invoice (the PO names none)' + req() + '</label>' +
    '<input class="form-control" id="vendorSearch" data-field="vendor_id" placeholder="Vendor name or GSTIN" autocomplete="off"></div><div id="vendorHits" class="mir-vendor-hits"></div>';
  document.getElementById('vendorSearch').addEventListener('input', debounce(async () => {
    const q = document.getElementById('vendorSearch').value.trim();
    const hits = document.getElementById('vendorHits');
    if (q.length < 2) { hits.innerHTML = ''; return; }
    const data = await apiMir('/vendors?q=' + encodeURIComponent(q));
    hits.innerHTML = data.vendors.map(v => '<button type="button" class="mir-hit" data-vendor="' + v.id + '">' + escapeHtml(v.name) +
      (v.gstin ? ' <span class="mir-muted">' + escapeHtml(v.gstin) + '</span>' : '') + '</button>').join('') ||
      '<div class="mir-muted">No vendor matches.</div>';
    hits.querySelectorAll('[data-vendor]').forEach(b => { b.onclick = () => {
      S.vendor = data.vendors.find(v => v.id === Number(b.dataset.vendor));
      S.vendorFromPo = false;
      renderVendor();
      schedulePreview();
    }; });
  }, 300));
}

function categoryControl(i, ln) {
  const cats = META.categories || [];
  if (!cats.length) {
    return '<input class="form-control" data-idx="' + i + '" data-key="material_category" maxlength="200" value="' + escapeHtml(ln.v.material_category) + '">';
  }
  return '<select class="form-control" data-idx="' + i + '" data-key="material_category"><option value="">Choose</option>' +
    cats.map(c => '<option value="' + escapeHtml(c.name) + '"' + (c.name === ln.v.material_category ? ' selected' : '') + '>' + escapeHtml(c.name) + '</option>').join('') +
    '</select>';
}

function subcategoryOptions(ln) {
  const cat = (META.categories || []).find(c => c.name === ln.v.material_category);
  const subs = cat ? cat.subcategories : [];
  if (ln.v.material_subcategory && !subs.includes(ln.v.material_subcategory)) ln.v.material_subcategory = '';
  return '<option value="">' + (subs.length ? 'Choose (optional)' : (cat ? 'None listed' : 'Pick a category first')) + '</option>' +
    subs.map(s => '<option value="' + escapeHtml(s) + '"' + (s === ln.v.material_subcategory ? ' selected' : '') + '>' + escapeHtml(s) + '</option>').join('');
}

function subcategoryControl(i, ln) {
  if (!(META.categories || []).length) {
    return '<input class="form-control" data-idx="' + i + '" data-key="material_subcategory" maxlength="200" value="' + escapeHtml(ln.v.material_subcategory) + '">';
  }
  return '<select class="form-control" data-idx="' + i + '" data-key="material_subcategory">' + subcategoryOptions(ln) + '</select>';
}

function renderLines() {
  const area = document.getElementById('linesArea');
  const slabs = META.gstSlabs;
  area.innerHTML = S.lines.map((ln, i) => {
    const l = ln.line;
    const unit = l.uom || 'unit';
    const cur = l.currency || 'INR';
    const input = (key, attrs) => '<input class="form-control" data-idx="' + i + '" data-key="' + key + '" value="' + escapeHtml(ln.v[key]) + '" ' + (attrs || '') + '>';
    const gstSelect = '<select class="form-control" data-idx="' + i + '" data-key="gst_rate"><option value="">Choose</option>' +
      slabs.map(s => '<option value="' + s + '"' + (ln.v.gst_rate !== '' && String(Number(ln.v.gst_rate)) === String(Number(s)) ? ' selected' : '') + '>' + s + '%</option>').join('') + '</select>';
    return '<div class="mir-line" data-line="' + i + '">' +
      '<div class="mir-line-head">' +
        '<div class="mir-line-title"><span class="mir-line-no">' + (i + 1) + '</span><div>' +
          '<div><b>' + escapeHtml(l.description) + '</b>' + (l.itemCode ? ' <span class="mir-muted">' + escapeHtml(l.itemCode) + '</span>' : '') + '</div>' +
          '<div class="mir-muted">PO ' + escapeHtml(l.poNumber) + ' line ' + l.lineNo + ' &middot; ' + escapeHtml(plantName(l.plant)) + (l.hsn ? ' &middot; HSN ' + escapeHtml(l.hsn) : '') + '</div>' +
        '</div></div>' +
        '<button type="button" class="mir-link" data-remove="' + i + '">Remove</button>' +
      '</div>' +
      '<div class="mir-line-facts">' +
        fact('Ordered', qty(l.qtyOrdered) + ' ' + unit) + fact('Received so far', qty(l.accepted) + ' ' + unit) +
        fact('Still open', '<b>' + qty(l.openQty) + ' ' + escapeHtml(unit) + '</b>', true) + fact('PO rate', money(l.rate, cur) + ' / ' + unit) +
        (l.poGstRate ? fact('PO GST', Number(l.poGstRate) + '%') : '') + (l.deliveryDate ? fact('Due', dateIN(l.deliveryDate)) : '') +
      '</div>' +
      '<div class="mir-line-group"><div class="mir-line-group-title">1. What came in</div><div class="mir-line-grid">' +
        field('Qty received (' + escapeHtml(unit) + ')' + req(), input('qty_received', 'inputmode="decimal" autocomplete="off" placeholder="As weighed / counted"') +
          '<button type="button" class="mir-fill" data-fill="' + i + '">Full open qty: ' + escapeHtml(qty(l.openQty)) + ' ' + escapeHtml(unit) + '</button>') +
        field('Qty rejected (' + escapeHtml(unit) + ')', input('qty_rejected', 'inputmode="decimal" placeholder="0" autocomplete="off"') +
          '<span class="mir-hint">Leave 0 if nothing was rejected.</span>') +
      '</div></div>' +
      '<div class="mir-line-group"><div class="mir-line-group-title">2. What the invoice charges</div><div class="mir-line-grid">' +
        field('Rate (' + escapeHtml(cur) + ' / ' + escapeHtml(unit) + ')' + req(), input('rate', 'inputmode="decimal" autocomplete="off"') +
          '<span class="mir-hint">Filled from the PO: ' + escapeHtml(money(l.rate, cur)) + '. Change it if the invoice differs.</span>') +
        field('Discount (' + escapeHtml(cur) + ')', input('discount', 'inputmode="decimal" placeholder="0" autocomplete="off"') +
          '<span class="mir-hint">Amount off this line, if any.</span>') +
        field('GST %' + req(), gstSelect + (l.poGstRate ? '<span class="mir-hint">The PO implies ' + Number(l.poGstRate) + '%.</span>' : '')) +
      '</div></div>' +
      '<div class="mir-line-group"><div class="mir-line-group-title">3. Classification</div><div class="mir-line-grid">' +
        field('Material category' + req(), categoryControl(i, ln) +
          (l.suggestedCategory ? '<span class="mir-hint">Suggested from the material list.</span>' : '')) +
        field('Sub-category', subcategoryControl(i, ln)) +
        field('Department use', '<input class="form-control" data-idx="' + i + '" data-key="dept_use" maxlength="60" value="' + escapeHtml(ln.v.dept_use) + '" placeholder="e.g. Mixing, Calendering">') +
      '</div></div>' +
      '<div class="mir-line-figures" id="fig-' + i + '"></div>' +
      '<div class="mir-diffs" id="reasons-' + i + '"></div>' +
    '</div>';
  }).join('');
  area.querySelectorAll('[data-key]').forEach(el => {
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
      const ln = S.lines[Number(el.dataset.idx)];
      ln.v[el.dataset.key] = el.value;
      if (el.dataset.key === 'material_category') {
        const sub = area.querySelector('select[data-idx="' + el.dataset.idx + '"][data-key="material_subcategory"]');
        if (sub) sub.innerHTML = subcategoryOptions(ln);
      }
      schedulePreview();
    });
  });
  area.querySelectorAll('[data-fill]').forEach(b => { b.onclick = () => {
    const i = Number(b.dataset.fill);
    const ln = S.lines[i];
    ln.v.qty_received = trimZeros(ln.line.openQty);
    const input = area.querySelector('[data-idx="' + i + '"][data-key="qty_received"]');
    input.value = ln.v.qty_received;
    input.focus();
    schedulePreview();
  }; });
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => {
    S.lines.splice(Number(b.dataset.remove), 1);
    if (!S.lines.length) { S.vendor = null; S.vendorFromPo = false; showEntrySections(false); }
    renderVendor();
    renderLines();
    schedulePreview();
    updateProgress(S.preview);
  }; });
}

/** Steps 3 and 4 and the save bar appear once a PO line is picked. */
function showEntrySections(on) {
  ['invoiceCard', 'linesCard', 'saveBar'].forEach(id => { document.getElementById(id).hidden = !on; });
}

function fact(label, valueHtml, strong) {
  return '<div class="mir-fact' + (strong ? ' is-strong' : '') + '"><span class="mir-kv-k">' + escapeHtml(label) + '</span><span>' +
    (valueHtml.indexOf('<') === -1 ? escapeHtml(valueHtml) : valueHtml) + '</span></div>';
}

function field(labelHtml, control) {
  return '<label class="form-group"><span class="form-label">' + labelHtml + '</span>' + control + '</label>';
}

function payload() {
  const val = id => document.getElementById(id).value;
  const body = {
    plant: val('plantSel'), mir_date: val('mirDate'), invoice_no: val('invoiceNo'), invoice_date: val('invoiceDate'),
    invoice_total: val('invoiceTotal'), tcs_amount: val('tcsAmount'), sap_grn_number: val('sapGrnNo'),
    challan_no: val('challanNo'), lr_no: val('lrNo'), vehicle_no: val('vehicleNo'), eway_bill_no: val('ewayBillNo'),
    gate_entry_no: val('gateEntryNo'), weighbridge_slip_no: val('weighbridgeSlipNo'), remarks: val('remarks'),
    tax_type_reason: S.header.tax_type_reason || '', tax_type_note: S.header.tax_type_note || '',
    invoice_total_reason: S.header.invoice_total_reason || '', invoice_total_note: S.header.invoice_total_note || '',
    lines: S.lines.map(ln => Object.assign({ po_line_id: ln.line.id }, ln.v)),
  };
  if (S.taxTouched) body.tax_type = val('taxType');
  if (S.vendor && !S.vendorFromPo) body.vendor_id = S.vendor.id;
  return body;
}

async function runPreview() {
  if (!S.lines.length) return;
  const seq = ++S.previewSeq;
  let result;
  try {
    result = await apiMir('/preview', { method: 'POST', body: payload() });
  } catch (e) {
    if (seq === S.previewSeq) showErrors([{ field: '', message: e.message }]);
    return;
  }
  if (seq !== S.previewSeq) return;  // a newer keystroke's preview is on its way
  S.preview = result;
  paintPreview(result);
}

function paintPreview(p) {
  const cur = mirCurrency();
  // Figures per line.
  S.lines.forEach((ln, i) => {
    const fig = p.lines.find(x => x.index === i);
    const box = document.getElementById('fig-' + i);
    if (!box) return;
    box.innerHTML = fig && fig.total !== null
      ? '<span>Value <b>' + money(fig.gross, cur) + '</b></span><span>Taxable <b>' + money(fig.taxable, cur) + '</b></span>' +
        (Number(fig.igst) ? '<span>IGST ' + money(fig.igst, cur) + '</span>' : '<span>CGST ' + money(fig.cgst, cur) + '</span><span>' + (p.taxType === 'CGST_UGST' ? 'UGST ' : 'SGST ') + money(fig.sgst, cur) + '</span>') +
        '<span class="mir-line-total">Line total <b>' + money(fig.total, cur) + '</b></span>'
      : '<span class="mir-muted">Enter the quantity, rate and GST % to see the line\'s figures.</span>';
  });
  // Difference cards: one per difference the server found on each line.
  S.lines.forEach((ln, i) => {
    const box = document.getElementById('reasons-' + i);
    if (!box) return;
    const mms = p.mismatches.filter(m => m.line === i);
    const sig = mms.map(m => m.kind + m.expected + m.actual + (ln.v[DIFF[m.kind].key + '_reason'] || '')).join('|');
    if (box.dataset.sig === sig) return;  // unchanged - keep the clerk's selection and focus
    box.dataset.sig = sig;
    box.innerHTML = mms.map(m => lineDiffHtml(m, i, ln)).join('');
    wireLineDiff(box, i);
  });
  // Tax type: default to what the states imply until the clerk changes it.
  const taxSel = document.getElementById('taxType');
  if (!S.taxTouched && p.taxTypeExpected) taxSel.value = p.taxTypeExpected;
  const expected = META.taxTypes.find(t => t.code === p.taxTypeExpected);
  document.getElementById('taxTypeHint').textContent = expected ? 'Expected from the vendor\'s and plant\'s states: ' + expected.label : 'Vendor state unknown - choose the tax type on the invoice.';
  headerDiff('TAX_TYPE', 'taxReasonRow', p);
  headerDiff('INVOICE_TOTAL', 'totalReasonRow', p);
  // The same invoice on earlier MIRs: allowed, but said.
  document.getElementById('noticeBox').innerHTML = (p.notices || []).map(n =>
    '<div class="mir-notice"><span class="mir-notice-icon" aria-hidden="true">i</span><span>' + escapeHtml(n) + '</span></div>').join('');
  // Totals.
  const diff = p.computedTotal !== null && p.invoiceTotal !== null ? Number(p.invoiceTotal) - Number(p.computedTotal) : null;
  const off = diff !== null && Math.abs(diff) > Number(META.invoiceRoundingTolerance);
  document.getElementById('totalsBox').innerHTML =
    tile('Computed total', money(p.computedTotal, cur), '') +
    tile('Invoice total', money(p.invoiceTotal, cur), '') +
    (diff !== null ? tile('Difference', money(diff.toFixed(2), cur), off ? 'is-bad' : 'is-ok') : '');
  showErrors(p.errors);
  updateProgress(p);
  const open = p.mismatches.length;
  document.getElementById('postHint').textContent = p.ok
    ? (open ? open + ' difference' + (open === 1 ? '' : 's') + ' will be saved with the reasons chosen, for the purchase team to follow up.' : 'Everything agrees with the PO.')
    : '';
}

function tile(label, value, tone) {
  return '<div class="mir-total-tile ' + tone + '"><span class="mir-kv-k">' + escapeHtml(label) + '</span><span class="mir-total-val">' + escapeHtml(value) + '</span></div>';
}

/** One line of plain English per difference: what the numbers are. */
function diffText(m, ln) {
  const unit = ln ? ' ' + (ln.line.uom || '') : '';
  const cur = ln ? ln.line.currency : mirCurrency();
  const pct = m.differencePct !== null && m.differencePct !== undefined ? ' (' + (Number(m.differencePct) > 0 ? '+' : '') + Number(m.differencePct) + '%)' : '';
  switch (m.kind) {
    case 'QTY_SHORT': return 'Accepted ' + qty(m.actual) + unit + ' against ' + qty(m.expected) + unit + ' still open on the PO: short by ' + qty(Number(m.expected) - Number(m.actual)) + unit + pct + '.';
    case 'QTY_OVER': return 'Accepted ' + qty(m.actual) + unit + ' against ' + qty(m.expected) + unit + ' still open on the PO: over by ' + qty(Number(m.actual) - Number(m.expected)) + unit + pct + '.';
    case 'QTY_REJECTED': return qty(m.actual) + unit + ' rejected' + (m.differencePct ? ' (' + Number(m.differencePct) + '% of the quantity received)' : '') + '. Only the accepted quantity counts against the PO.';
    case 'RATE_HIGH': return 'Invoice rate ' + money(m.actual, cur) + ' is above the PO rate ' + money(m.expected, cur) + pct + '.';
    case 'RATE_LOW': return 'Invoice rate ' + money(m.actual, cur) + ' is below the PO rate ' + money(m.expected, cur) + pct + '.';
    case 'GST_RATE': return 'GST ' + Number(m.actual) + '% on the invoice; the PO\'s totals imply ' + Number(m.expected) + '%.';
    case 'INVOICE_TOTAL': return 'The invoice says ' + money(m.actual, cur) + ' but the lines add up to ' + money(m.expected, cur) + pct + '.';
    case 'TAX_TYPE': return 'The invoice charges ' + m.actualText + '; the vendor\'s and plant\'s states imply ' + m.expectedText + '.';
    default: return m.kind;
  }
}

function reasonSelectHtml(kind, selected, attrs) {
  return '<select class="form-control" ' + attrs + '><option value="">Choose a reason</option>' +
    reasonsOf(kind).map(r => '<option value="' + r.code + '"' + (r.code === selected ? ' selected' : '') + '>' + escapeHtml(r.label) + '</option>').join('') + '</select>';
}

/** A difference card: what differs, whether a reason is chosen, the reason
    and its note. Amber until a reason is picked, green once it is. */
function diffCardHtml(m, ln, reasonCode, note, reasonAttrs, noteAttrs) {
  const d = DIFF[m.kind];
  const reason = reasonByCode(reasonCode);
  const done = !!(reason && reason.kind === d.reasonKind && (!reason.noteRequired || (note || '').trim()));
  return '<div class="mir-diff ' + (done ? 'is-done' : 'is-todo') + '">' +
    '<div class="mir-diff-head">' +
      '<span class="mir-diff-icon" aria-hidden="true">' + (done ? '&#10003;' : '!') + '</span>' +
      '<span class="mir-diff-title">' + escapeHtml(d.title) + '</span>' +
      '<span class="mir-diff-state">' + (done ? 'Reason recorded' : 'Needs a reason') + '</span>' +
    '</div>' +
    '<div class="mir-diff-text">' + escapeHtml(diffText(m, ln)) + '</div>' +
    '<div class="mir-diff-inputs">' +
      '<label class="form-group"><span class="form-label">Reason' + req() + '</span>' +
        reasonSelectHtml(d.reasonKind, reason && reason.kind === d.reasonKind ? reason.code : '', reasonAttrs + ' aria-label="Reason: ' + escapeHtml(d.title) + '"') + '</label>' +
      '<label class="form-group"><span class="form-label">Note' + (reason && reason.noteRequired ? req() : ' (optional)') + '</span>' +
        '<input class="form-control" ' + noteAttrs + ' maxlength="2000" value="' + escapeHtml(note || '') + '" placeholder="' +
        (reason && reason.noteRequired ? 'This reason needs a short explanation' : 'Anything the purchase team should know') + '"></label>' +
    '</div>' +
    (reason && reason.closesLine ? '<div class="mir-diff-foot">This closes the PO line: no balance will be expected from the vendor.</div>' : '') +
  '</div>';
}

function lineDiffHtml(m, i, ln) {
  const key = DIFF[m.kind].key;
  return diffCardHtml(m, ln, ln.v[key + '_reason'], ln.v[key + '_note'],
    'data-reason-idx="' + i + '" data-reason-key="' + key + '_reason"', 'data-reason-idx="' + i + '" data-reason-key="' + key + '_note"');
}

function wireLineDiff(box, i) {
  box.querySelectorAll('[data-reason-key]').forEach(el => {
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
      S.lines[i].v[el.dataset.reasonKey] = el.value;
      if (el.tagName === 'SELECT') box.dataset.sig = '';  // repaint: state chip, note hint, closes-line notice
      schedulePreview();
    });
  });
}

function headerDiff(kind, rowId, p) {
  const row = document.getElementById(rowId);
  const prefix = DIFF[kind].key;
  const m = p.mismatches.find(x => x.kind === kind);
  if (!m) { row.hidden = true; row.innerHTML = ''; row.dataset.sig = ''; S.header[prefix + '_reason'] = ''; return; }
  const sig = m.kind + m.expected + m.actual + (m.actualText || '') + (S.header[prefix + '_reason'] || '');
  row.hidden = false;
  if (row.dataset.sig === sig) return;
  row.dataset.sig = sig;
  row.innerHTML = diffCardHtml(m, null, S.header[prefix + '_reason'], S.header[prefix + '_note'],
    'data-h="' + prefix + '_reason"', 'data-h="' + prefix + '_note"');
  row.querySelectorAll('[data-h]').forEach(el => el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
    S.header[el.dataset.h] = el.value;
    if (el.tagName === 'SELECT') row.dataset.sig = '';
    schedulePreview();
  }));
}

const FIELD_LABELS = {
  plant: 'the receiving plant', mir_date: 'the MIR date', invoice_no: 'the invoice number', invoice_date: 'the invoice date',
  invoice_total: 'the invoice grand total', tcs_amount: 'TCS', tax_type: 'the tax type', vendor_id: 'the vendor', lines: 'Lines',
  sap_grn_number: 'the SAP GRN number',
  tax_type_reason: 'the tax type difference', tax_type_note: 'the note on the tax type difference',
  invoice_total_reason: 'the invoice total difference', invoice_total_note: 'the note on the invoice total difference',
  qty_received: 'the qty received', qty_rejected: 'the qty rejected', rate: 'the invoice rate', discount: 'the discount',
  gst_rate: 'the GST %', po_line_id: 'PO line', material_category: 'the material category', material_subcategory: 'the sub-category',
  qty_reason: 'the quantity difference', qty_note: 'the note on the quantity difference',
  rate_reason: 'the rate difference', rate_note: 'the note on the rate difference',
  reject_reason: 'the rejected quantity', reject_note: 'the note on the rejected quantity',
  gst_reason: 'the GST difference', gst_note: 'the note on the GST difference',
};

function fieldLabel(field) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(field);
  if (m) {
    const ln = S.lines[Number(m[1])];
    const what = FIELD_LABELS[m[2]] || m[2];
    return what + ' on line ' + (Number(m[1]) + 1) + (ln ? ' (' + ln.line.description.slice(0, 40) + ')' : '');
  }
  return (FIELD_LABELS[field] || field).toLowerCase();
}

function showErrors(errors) {
  document.querySelectorAll('.is-invalid').forEach(el => el.classList.remove('is-invalid'));
  // "Still to do" is always shown, as a plain checklist, so a first-time
  // user can see why Save will not go through yet. Fields turn red only
  // after the first Save attempt - before that an empty field is not a
  // mistake, just not done yet.
  const box = document.getElementById('todoBox');
  box.innerHTML = errors.length
    ? '<div class="mir-todo-head' + (S.triedToPost ? ' is-bad' : '') + '">Still to do before saving (' + errors.length + ')</div><ul>' +
      errors.map((e, n) => '<li><button type="button" class="mir-todo-item" data-todo="' + n + '">' + escapeHtml(todoText(e)) + '</button></li>').join('') + '</ul>'
    : '<div class="mir-todo-head is-ok">Everything needed is filled in. Check the figures and save.</div>';
  box.querySelectorAll('[data-todo]').forEach(b => { b.onclick = () => focusField(errors[Number(b.dataset.todo)]); });
  document.getElementById('formErrors').innerHTML = '';
  if (S.triedToPost) errors.forEach(e => { const el = fieldEl(e); if (el) el.classList.add('is-invalid'); });
}

function todoText(e) {
  const label = fieldLabel(e.field || '');
  if (e.message === 'Required.') return 'Enter ' + label;
  if (e.message === 'Choose a reason.') return 'Pick a reason for ' + label;
  if (e.message === 'This reason needs a note.') return 'Write ' + label + ' (the reason chosen needs one)';
  return (e.field ? label + ': ' : '') + e.message;
}

function fieldEl(e) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(e.field || '');
  return m
    ? document.querySelector('[data-idx="' + m[1] + '"][data-key="' + m[2] + '"], [data-reason-idx="' + m[1] + '"][data-reason-key="' + m[2] + '"]')
    : document.querySelector('[data-field="' + e.field + '"], [data-h="' + e.field + '"]');
}

function focusField(e) {
  const el = fieldEl(e);
  if (!el) return;
  const details = el.closest('details');
  if (details) details.open = true;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => el.focus({ preventScroll: true }), 300);
}

async function postMir() {
  S.triedToPost = true;
  const btn = document.getElementById('postBtn');
  if (!S.preview || !S.preview.ok) {
    // Not ready: say what is missing and go to the first thing, rather than
    // a greyed-out button that gives no reason.
    if (S.preview) {
      showErrors(S.preview.errors);
      if (S.preview.errors.length) focusField(S.preview.errors[0]);
    }
    return;
  }
  btn.disabled = true;
  try {
    const mir = await apiMir('/entries/new', { method: 'POST', body: payload() });
    const n = mir.mismatches.length;
    document.getElementById('postedNo').textContent = mir.mirNo;
    document.getElementById('postedDetail').textContent = mir.vendor.name + ', invoice ' + mir.invoiceNo + ', ' + money(mir.computedTotal, mirCurrency()) +
      (n ? ' - ' + n + ' difference' + (n === 1 ? '' : 's') + ' sent to Open mismatches.' : '.');
    document.getElementById('postedBanner').hidden = false;
    document.getElementById('mirForm').hidden = true;
    document.getElementById('mirProgress').hidden = true;
    refreshMismatchCount();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    btn.disabled = false;
    await runPreview();
    showErrors(e.errors && e.errors.length ? e.errors : [{ field: '', message: e.message }]);
  }
}

function resetForm() {
  S.lines = []; S.vendor = null; S.vendorFromPo = false; S.taxTouched = false; S.header = {}; S.preview = null; S.triedToPost = false;
  document.getElementById('mirForm').reset();
  document.getElementById('mirDate').value = META.today;
  ['poResults', 'linesArea', 'totalsBox', 'formErrors', 'vendorBox', 'noticeBox', 'todoBox'].forEach(id => { document.getElementById(id).innerHTML = ''; });
  ['taxReasonRow', 'totalReasonRow'].forEach(id => { const r = document.getElementById(id); r.hidden = true; r.innerHTML = ''; r.dataset.sig = ''; });
  showEntrySections(false);
  document.getElementById('postedBanner').hidden = true;
  document.getElementById('mirForm').hidden = false;
  document.getElementById('mirProgress').hidden = false;
  document.getElementById('postBtn').disabled = false;
  updateProgress(null);
  document.getElementById('poSearch').focus();
}

// ── Register ──────────────────────────────────────────────────────────────
function plantOptions() {
  return '<option value="">All my plants</option>' +
    META.plants.filter(p => p.canRead).map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
}

function initRegister() {
  document.getElementById('regPlant').innerHTML = plantOptions();
  const reload = debounce(loadRegister, 300);
  ['regPlant', 'regStatus', 'regSearch', 'regFrom', 'regTo'].forEach(id => document.getElementById(id).addEventListener('input', reload));
}

async function loadRegister() {
  const area = document.getElementById('registerArea');
  const params = new URLSearchParams();
  [['plant', 'regPlant'], ['status', 'regStatus'], ['q', 'regSearch'], ['from', 'regFrom'], ['to', 'regTo']].forEach(([k, id]) => {
    const v = document.getElementById(id).value.trim();
    if (v) params.set(k, v);
  });
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  try {
    const data = await apiMir('/entries?' + params.toString());
    if (!data.entries.length) { area.innerHTML = '<div class="mir-empty">No MIRs match these filters.</div>'; return; }
    area.innerHTML = '<div class="table-wrap"><table class="mir-click"><thead><tr><th>MIR no.</th><th>MIR date</th><th>Plant</th><th>Vendor</th><th>Invoice</th>' +
      '<th class="num">Total</th><th>Status</th><th>Entered by</th></tr></thead><tbody>' +
      data.entries.map(e => '<tr data-mir="' + e.id + '" tabindex="0"><td class="nowrap"><b>' + escapeHtml(e.mirNo) + '</b></td><td class="nowrap">' + dateIN(e.mirDate) + '</td>' +
        '<td>' + escapeHtml(e.plant.name) + '</td><td>' + escapeHtml(e.vendor.name) + '</td>' +
        '<td>' + escapeHtml(e.invoiceNo) + '<div class="mir-muted">' + dateIN(e.invoiceDate) + '</div></td>' +
        '<td class="num">' + money(e.computedTotal) + '</td><td>' + statusPill(e.status === 'POSTED' ? 'Posted' : 'Cancelled', MIR_STATUS_TONE[e.status]) + '</td>' +
        '<td>' + escapeHtml(e.createdBy) + '</td></tr>').join('') + '</tbody></table></div>';
    area.querySelectorAll('[data-mir]').forEach(tr => {
      const open = () => loadDetail(Number(tr.dataset.mir));
      tr.onclick = open;
      tr.onkeydown = ev => { if (ev.key === 'Enter') open(); };
    });
  } catch (e) {
    area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

async function loadDetail(id) {
  const area = document.getElementById('detailArea');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let m;
  try { m = await apiMir('/entries/' + id); } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const canCancel = m.status === 'POSTED' && (META.plants.find(p => p.code === m.plant.code) || {}).canReceive;
  const tax = META.taxTypes.find(t => t.code === m.taxType);
  const cur = m.lines.length ? m.lines[0].currency : 'INR';
  const item = (k, v) => v ? '<div class="mir-kv-item"><span class="mir-kv-k">' + escapeHtml(k) + '</span><span class="mir-kv-v">' + escapeHtml(v) + '</span></div>' : '';
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(m.mirNo) + '</h3>' +
      statusPill(m.status === 'POSTED' ? 'Posted' : 'Cancelled', MIR_STATUS_TONE[m.status]) + '</div>' +
    '<div class="mir-po-facts">' +
      item('Plant', m.plant.name) + item('MIR date', dateIN(m.mirDate)) + item('Vendor', m.vendor.name + (m.vendor.gstin ? ' (' + m.vendor.gstin + ')' : '')) +
      item('Invoice', m.invoiceNo + ', ' + dateIN(m.invoiceDate)) + item('Invoice total', money(m.invoiceTotal, cur)) + item('Computed total', money(m.computedTotal, cur)) +
      item('Tax type', tax ? tax.label : m.taxType) + item('TCS', Number(m.tcsAmount) ? money(m.tcsAmount, cur) : '') +
      item('SAP GRN number', m.sapGrnNumber) + item('Vehicle', m.vehicleNo) + item('Challan', m.challanNo) + item('LR', m.lrNo) +
      item('E-way bill', m.ewayBillNo) + item('Gate entry', m.gateEntryNo) + item('Weighbridge slip', m.weighbridgeSlipNo) +
      item('Remarks', m.remarks) + item('Entered by', m.createdBy + ', ' + new Date(m.createdAt).toLocaleString('en-IN')) +
      (m.cancelReason ? item('Cancelled', m.cancelledBy + ': ' + m.cancelReason) : '') +
    '</div>' +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>PO / line</th><th>Material</th><th>Category</th><th class="num">Received</th><th class="num">Rejected</th>' +
      '<th class="num">Rate</th><th class="num">PO rate</th><th class="num">GST %</th><th class="num">Taxable</th><th class="num">Total</th></tr></thead><tbody>' +
      m.lines.map(l => '<tr><td>' + l.lineNo + '</td><td>' + escapeHtml(l.poNumber) + ' #' + l.poLineNo + '<div class="mir-muted">' + escapeHtml(plantName(l.poPlant)) + '</div></td>' +
        '<td>' + escapeHtml(l.description) + (l.deptUse ? '<div class="mir-muted">For ' + escapeHtml(l.deptUse) + '</div>' : '') + '</td>' +
        '<td>' + escapeHtml(l.materialCategory || '-') + (l.materialSubcategory ? '<div class="mir-muted">' + escapeHtml(l.materialSubcategory) + '</div>' : '') + '</td>' +
        '<td class="num">' + qty(l.qtyReceived) + ' ' + escapeHtml(l.uom) + '</td><td class="num">' + (Number(l.qtyRejected) ? qty(l.qtyRejected) + ' ' + escapeHtml(l.uom) : '-') + '</td>' +
        '<td class="num">' + money(l.rate, l.currency) + '</td><td class="num">' + money(l.poRate, l.currency) + '</td><td class="num">' + Number(l.gstRate) + '%</td>' +
        '<td class="num">' + money(l.taxable, l.currency) + '</td><td class="num">' + money(l.lineTotal, l.currency) + '</td></tr>').join('') +
    '</tbody></table></div>' +
    (m.mismatches.length ? '<h4 class="mir-subtitle">Differences recorded</h4>' + mismatchListHtml(m.mismatches.map(x => Object.assign({ currency: cur }, x)), false) : '') +
    (canCancel ? '<div class="mir-cancel"><input class="form-control" id="cancelReason" maxlength="500" placeholder="Why is this MIR being cancelled?" aria-label="Cancel reason">' +
      '<button type="button" class="btn btn-navy" id="cancelBtn">Cancel this MIR</button></div><div class="mir-error-text" id="cancelErr"></div>' : '') +
  '</section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const btn = document.getElementById('cancelBtn');
  if (btn) btn.onclick = async () => {
    const reason = document.getElementById('cancelReason').value.trim();
    if (!reason) { document.getElementById('cancelErr').textContent = 'Say why the MIR is cancelled.'; return; }
    if (!window.confirm('Cancel ' + m.mirNo + '? Its quantities stop counting against the PO at once.')) return;
    try {
      await apiMir('/entries/' + id + '/cancel', { method: 'POST', body: { reason } });
      loadDetail(id);
      loadRegister();
      refreshMismatchCount();
    } catch (e) { document.getElementById('cancelErr').textContent = e.message; }
  };
}

// ── Mismatches ────────────────────────────────────────────────────────────
function initMismatches() {
  document.getElementById('mmPlant').innerHTML = plantOptions();
  ['mmStatus', 'mmPlant'].forEach(id => document.getElementById(id).addEventListener('input', loadMismatches));
}

async function refreshMismatchCount() {
  const badge = document.getElementById('mismatchCount');
  try {
    const data = await apiMir('/mismatches?status=OPEN');
    const n = data.mismatches.length;
    badge.textContent = n >= 500 ? '500+' : String(n);
    badge.hidden = n === 0;
  } catch (e) { badge.hidden = true; }
}

/** Differences as cards: the MIR and PO they belong to, what differed, the
    reason given at entry, and - while open - the resolve box. */
function mismatchListHtml(list, withMir) {
  return '<div class="mir-mm-list">' + list.map(x => {
    const cur = x.currency || 'INR';
    const fig = v => v === null || v === undefined ? '-' : ((x.kind.startsWith('RATE') || x.kind === 'INVOICE_TOTAL') ? money(v, cur) : (x.kind === 'GST_RATE' ? Number(v) + '%' : qty(v)));
    const figures = x.kind === 'TAX_TYPE' ? '' : '<span>Expected <b>' + fig(x.expected) + '</b></span><span>Actual <b>' + fig(x.actual) + '</b></span>' +
      (x.differencePct ? '<span>' + (Number(x.differencePct) > 0 ? '+' : '') + Number(x.differencePct) + '%</span>' : '');
    return '<div class="mir-mm is-' + x.status.toLowerCase() + '">' +
      '<div class="mir-mm-head">' +
        '<span class="mir-mm-kind">' + escapeHtml(x.kindLabel) + (x.lineNo ? ' <span class="mir-muted">line ' + x.lineNo + '</span>' : ' <span class="mir-muted">whole invoice</span>') + '</span>' +
        statusPill(x.status === 'VOID' ? 'Void' : x.status.charAt(0) + x.status.slice(1).toLowerCase(), MISMATCH_STATUS_TONE[x.status]) +
      '</div>' +
      (withMir ? '<div class="mir-mm-where"><b>' + escapeHtml(x.mirNo) + '</b> of ' + dateIN(x.mirDate) + ' &middot; ' + escapeHtml(x.plant.name) +
        ' &middot; ' + escapeHtml(x.vendor.name) + ', invoice ' + escapeHtml(x.invoiceNo) +
        (x.poNumber ? '<div class="mir-muted">PO ' + escapeHtml(x.poNumber) + (x.description ? ' - ' + escapeHtml(x.description) : '') + '</div>' : '') + '</div>' : '') +
      (figures ? '<div class="mir-mm-figs">' + figures + '</div>' : '') +
      '<div class="mir-mm-reason"><span class="mir-kv-k">Reason given</span> ' + escapeHtml(x.reasonLabel) + (x.note ? ' <span class="mir-muted">- ' + escapeHtml(x.note) + '</span>' : '') + '</div>' +
      (x.resolutionNote ? '<div class="mir-mm-reason"><span class="mir-kv-k">Resolved</span> ' + escapeHtml(x.resolvedBy + ': ' + x.resolutionNote) + '</div>' : '') +
      (withMir && x.status === 'OPEN' && canResolve(x) ? '<div class="mir-resolve"><input class="form-control" data-resolve-note="' + x.id + '" placeholder="What was done? e.g. debit note 123 raised, balance received on MIR ..." aria-label="Resolution note">' +
        '<button type="button" class="btn btn-navy btn-small" data-resolve="' + x.id + '">Mark resolved</button></div>' : '') +
    '</div>';
  }).join('') + '</div>';
}

function canResolve(x) {
  return (META.plants.find(p => p.code === x.plant.code) || {}).canReceive;
}

async function loadMismatches() {
  const area = document.getElementById('mismatchArea');
  const params = new URLSearchParams({ status: document.getElementById('mmStatus').value });
  const plant = document.getElementById('mmPlant').value;
  if (plant) params.set('plant', plant);
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  try {
    const data = await apiMir('/mismatches?' + params.toString());
    if (!data.mismatches.length) { area.innerHTML = '<div class="mir-empty">Nothing here.</div>'; return; }
    area.innerHTML = mismatchListHtml(data.mismatches, true);
    area.querySelectorAll('[data-resolve]').forEach(b => { b.onclick = async () => {
      const input = area.querySelector('[data-resolve-note="' + b.dataset.resolve + '"]');
      const note = input.value.trim();
      if (!note) { input.classList.add('is-invalid'); input.focus(); return; }
      try {
        await apiMir('/mismatches/' + b.dataset.resolve + '/resolve', { method: 'POST', body: { note } });
        loadMismatches();
        refreshMismatchCount();
      } catch (e) { window.alert(e.message); }
    }; });
  } catch (e) {
    area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}
