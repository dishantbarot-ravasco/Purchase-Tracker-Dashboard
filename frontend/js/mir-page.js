// mir.html's page script - see mir.html's header comment. Self-contained:
// its own fetch wrapper, no dependency on main.js.
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
  INVOICE_BEFORE_PO: { reasonKind: 'INVOICE_DATE', key: 'invoice_date', title: 'Invoice dated before the PO' },
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
  offerDraft();
})();

/** /api/mir/... fetch wrapper: authFetch(), JSON in and out, an error carries the server's message. */
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

// Each list's newest request: debouncing spaces the requests out but they can
// still overlap, and a slower earlier response must not paint over a newer one.
// mirLoadTicket(name) returns a check that is true only while no later load of
// that list has started.
const MIR_LOAD_SEQ = {};
function mirLoadTicket(name) {
  const n = (MIR_LOAD_SEQ[name] = (MIR_LOAD_SEQ[name] || 0) + 1);
  return () => MIR_LOAD_SEQ[name] === n;
}
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
  // Every change, including the transport fields no preview needs.
  document.getElementById('mirForm').addEventListener('input', () => scheduleDraft());
  document.getElementById('mirForm').addEventListener('change', () => scheduleDraft());
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

/** Label/value facts as a table: short facts two to a row, a long one
    (an address, remarks) across the full width. Empty values are left out. */
function factsTableHtml(rows) {
  const filled = rows.filter(r => r[1]);
  if (!filled.length) return '';
  const cell = (k, v) => '<th scope="row">' + escapeHtml(k) + '</th><td>' + escapeHtml(v) + '</td>';
  let html = '', pending = null;
  filled.forEach(([k, v, long]) => {
    if (long) {
      if (pending) { html += '<tr>' + cell(pending[0], pending[1]) + '<td colspan="2"></td></tr>'; pending = null; }
      html += '<tr>' + '<th scope="row">' + escapeHtml(k) + '</th><td colspan="3">' + escapeHtml(v) + '</td></tr>';
    } else if (pending) {
      html += '<tr>' + cell(pending[0], pending[1]) + cell(k, v) + '</tr>'; pending = null;
    } else {
      pending = [k, v];
    }
  });
  if (pending) html += '<tr>' + cell(pending[0], pending[1]) + '<td colspan="2"></td></tr>';
  return '<table class="mir-facts-table"><tbody>' + html + '</tbody></table>';
}

function poHeaderHtml(po) {
  const tax = META.taxTypes.find(t => t.code === po.taxType);
  const chip = (k, v) => v ? '<span class="mir-po-chip"><span class="mir-kv-k">' + escapeHtml(k) + '</span> ' + escapeHtml(v) + '</span>' : '';
  const more = factsTableHtml([
    ['Payment terms', po.paymentTerms], ['Incoterms', po.incoterms],
    ['Value (excl. GST)', po.totalValue ? money(po.totalValue, po.currency) : ''],
    ['Value (incl. GST)', po.totalInclusiveValue ? money(po.totalInclusiveValue, po.currency) : ''],
    ['Bill to', po.billingAddress, true], ['Ship to', po.shipTo, true], ['Vendor address', po.vendorAddress, true],
    ['PO remarks', po.remarks, true],
  ]);
  return '<div class="mir-po-summary">' +
      chip('PO date', dateIN(po.poDate)) + chip('Tax', tax ? tax.label : po.taxTypeRaw) +
      chip('GST', po.gstRate ? Number(po.gstRate) + '%' : '') + chip('Currency', po.currency) +
      chip('Value incl. GST', po.totalInclusiveValue ? money(po.totalInclusiveValue, po.currency) : '') +
    '</div>' +
    (more ? '<details class="mir-po-more"><summary>More PO details (terms, addresses, remarks)</summary>' + more + '</details>' : '');
}

/** The PO's uploaded copy (po-files.html), so the store can check the
    delivery against the order as printed. Only the current revision is
    offered; older ones stay on the PO Files page. */
function poFilesHtml(po) {
  const current = (po.poFiles || []).find(f => f.status === 'CURRENT');
  if (!current) return '<p class="mir-muted doc-file-none">No PO copy uploaded yet.</p>';
  return '<div class="doc-file-po"><span class="mir-kv-k">PO copy</span> ' + docFileLineHtml(current) + '</div>';
}

async function openPo(poId, el) {
  const target = el.querySelector('.mir-po-lines');
  target.innerHTML = '<div class="mir-muted">Loading lines...</div>';
  try {
    const po = await apiMir('/purchase-orders/' + poId);
    const picked = new Set(S.lines.map(l => l.line.id));
    target.innerHTML = poHeaderHtml(po) + poFilesHtml(po) +
      '<div class="table-wrap mir-table-wrap"><table><thead><tr><th></th><th>#</th><th>Material</th><th>HSN</th><th>Unit</th><th class="num">Ordered</th>' +
      '<th class="num">Received</th><th class="num">Open</th><th class="num">PO rate</th><th>Delivery</th>' + (po.canManage ? '<th>Purchase manager</th>' : '') + '</tr></thead><tbody>' +
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
          '<td>' + dateIN(l.deliveryDate) + (late ? ' ' + statusPill('late', 'bad') : '') + '</td>' +
          (po.canManage ? '<td class="nowrap">' + lineActionsHtml(l) + '</td>' : '') + '</tr>' +
          (po.canManage ? '<tr class="mir-action-row" data-action-row="' + l.id + '" hidden><td colspan="11"></td></tr>' : '');
      }).join('') + '</tbody></table></div>' +
      '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" data-add>Add ticked lines to this MIR</button></div>';
    docFileBindOpen(target);
    target.querySelector('[data-add]').onclick = () => {
      const ids = [...target.querySelectorAll('[data-pick]:checked:not(:disabled)')].map(c => Number(c.dataset.pick));
      addLines(po, po.lines.filter(l => ids.includes(l.id)));
      openPo(poId, el);
    };
    target.querySelectorAll('[data-line-action]').forEach(b => { b.onclick = () => {
      const line = po.lines.find(x => x.id === Number(b.dataset.line));
      openLineAction(b.dataset.lineAction, line, target, () => openPo(poId, el));
    }; });
  } catch (e) {
    target.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

/** What a purchase manager can do to one PO line: confirm a line the CSV
    changed after receipts, reopen a short-closed one, or short-close an
    open one when no balance is coming. The server re-checks each. */
function lineActionsHtml(l) {
  const btn = (action, label) => '<button type="button" class="mir-link" data-line-action="' + action + '" data-line="' + l.id + '">' + label + '</button>';
  if (l.needsReview) return btn('review', 'Review change');
  if (l.closed) return btn('reopen', 'Reopen');
  if (l.status !== 'received' && l.receivable) return btn('close', 'Short-close');
  return '';
}

function openLineAction(action, line, container, done) {
  const row = container.querySelector('[data-action-row="' + line.id + '"]');
  row.hidden = false;
  const cell = row.firstElementChild;
  const closeReasons = META.reasons.filter(r => r.kind === 'QTY_SHORT' && r.closesLine);
  const intro = {
    review: 'The PO sheet changed this line after receipts were posted against it' + (line.reviewNote ? ': ' + line.reviewNote : '.') +
      ' Check the MIRs on it still belong to this line, then confirm. Receipts are blocked until then.',
    reopen: 'Short-closed' + (line.closedReason ? ' (' + line.closedReason + ')' : '') + (line.closeNote ? ': ' + line.closeNote : '') +
      '. Reopen it if the balance (or a replacement for rejected material) is coming after all.',
    close: 'Close this line if the balance of ' + qty(line.openQty) + ' ' + (line.uom || '') + ' is not coming. It stops taking receipts; it can be reopened.',
  }[action];
  cell.innerHTML = '<div class="mir-edit">' +
    '<div class="mir-line-group-title">' + escapeHtml({ review: 'Review a changed line', reopen: 'Reopen a closed line', close: 'Short-close a line' }[action]) +
      ' - line ' + line.lineNo + ', ' + escapeHtml(line.description) + '</div>' +
    '<p class="mir-help">' + escapeHtml(intro) + '</p>' +
    '<div class="mir-grid">' +
      (action === 'close' ? field('Reason' + req(), '<select class="form-control" data-act="reason"><option value="">Choose a reason</option>' +
        closeReasons.map(r => '<option value="' + r.code + '">' + escapeHtml(r.label) + '</option>').join('') + '</select>') : '') +
      (action !== 'reopen' ? field(action === 'review' ? 'What was checked' + req() : 'Note', '<input class="form-control" data-act="note" maxlength="2000">') : '') +
    '</div>' +
    '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" data-act-go>' +
      escapeHtml({ review: 'Confirm the line', reopen: 'Reopen the line', close: 'Short-close the line' }[action]) + '</button>' +
      '<button type="button" class="mir-link" data-act-cancel>Cancel</button><span class="mir-error-text" data-act-err></span></div></div>';
  cell.querySelector('[data-act-cancel]').onclick = () => { row.hidden = true; };
  cell.querySelector('[data-act-go]').onclick = async () => {
    const val = k => { const el = cell.querySelector('[data-act="' + k + '"]'); return el ? el.value : ''; };
    try {
      await apiMir('/po-lines/' + line.id + '/' + action, { method: 'POST', body: { reason: val('reason'), note: val('note') } });
      mirToast({ review: 'Line confirmed - it can take receipts again.', reopen: 'Line reopened.', close: 'Line short-closed.' }[action]);
      done();
    } catch (e) { cell.querySelector('[data-act-err]').textContent = e.message; }
  };
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
      material_category: line.materialCategory || '', material_subcategory: line.materialSubcategory || '',
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
        (l.materialCategory
          ? field('Material category', '<div class="mir-readonly">' + escapeHtml(l.materialCategory) + '</div>' +
              '<span class="mir-hint">From the material master - the same on every receipt of this material. ' +
              (l.materialId ? '<button type="button" class="mir-link" data-fix-category="' + i + '">Wrong? Correct it</button>' : '') + '</span>' +
              '<div data-fix-box="' + i + '"></div>') +
            field('Sub-category', '<div class="mir-readonly">' + escapeHtml(l.materialSubcategory || '-') + '</div>')
          : field('Material category' + req(), categoryControl(i, ln) +
              '<span class="mir-hint">First receipt of this material: the category you pick is saved on the material for every future receipt.</span>') +
            field('Sub-category', subcategoryControl(i, ln))) +
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
  area.querySelectorAll('[data-fix-category]').forEach(b => { b.onclick = () => openCategoryFix(Number(b.dataset.fixCategory)); });
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

/** Correct a filed material's category from the MIR form: the material
    master changes for every receipt of it, with a reason, logged. */
function openCategoryFix(i) {
  const ln = S.lines[i];
  const box = document.querySelector('[data-fix-box="' + i + '"]');
  const draft = { v: { material_category: ln.line.materialCategory, material_subcategory: ln.line.materialSubcategory } };
  box.innerHTML = '<div class="mir-edit">' +
    '<p class="mir-help">This changes the category of <b>' + escapeHtml(ln.line.description) + '</b> everywhere, for every receipt. ' +
      'If the plant manager\'s reference list has it wrong, fix the list too - it wins the next time it is loaded.</p>' +
    field('Category' + req(), categoryControl('fix' + i, draft)) + field('Sub-category', subcategoryControl('fix' + i, draft)) +
    field('Why?' + req(), '<input class="form-control" data-fix-reason maxlength="500" placeholder="e.g. This is a filler, not carbon black">') +
    '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" data-fix-go>Save the category</button>' +
      '<button type="button" class="mir-link" data-fix-cancel>Cancel</button><span class="mir-error-text" data-fix-err></span></div></div>';
  const cat = box.querySelector('[data-key="material_category"]');
  const sub = box.querySelector('[data-key="material_subcategory"]');
  cat.addEventListener('change', () => { draft.v.material_category = cat.value; if (sub.tagName === 'SELECT') sub.innerHTML = subcategoryOptions(draft); });
  sub.addEventListener('change', () => { draft.v.material_subcategory = sub.value; });
  box.querySelector('[data-fix-cancel]').onclick = () => { box.innerHTML = ''; };
  box.querySelector('[data-fix-go]').onclick = async () => {
    try {
      const m = await apiMir('/materials/' + ln.line.materialId + '/category', { method: 'POST', body: {
        category: cat.value, subcategory: sub.value, reason: box.querySelector('[data-fix-reason]').value } });
      S.lines.forEach(x => { if (x.line.materialId === m.id) { x.line.materialCategory = m.category; x.line.materialSubcategory = m.subcategory; } });
      mirToast('Category saved on the material master.');
      renderLines();
      schedulePreview();
    } catch (e) { box.querySelector('[data-fix-err]').textContent = e.message; }
  };
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
    invoice_date_reason: S.header.invoice_date_reason || '', invoice_date_note: S.header.invoice_date_note || '',
    lines: S.lines.map(ln => Object.assign({ po_line_id: ln.line.id }, ln.v)),
  };
  if (S.taxTouched) body.tax_type = val('taxType');
  if (S.vendor && !S.vendorFromPo) body.vendor_id = S.vendor.id;
  return body;
}

async function runPreview() {
  scheduleDraft();
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
  headerDiff('INVOICE_BEFORE_PO', 'invoiceDateReasonRow', p);
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
    case 'INVOICE_BEFORE_PO': return 'The invoice is dated ' + m.actualText + ', ' + Number(m.actual) + ' day' + (Number(m.actual) === 1 ? '' : 's') +
      ' before the PO date ' + m.expectedText + '. Check the invoice date; if it is right, say why the vendor billed before the PO.';
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
  invoice_date_reason: 'the invoice dated before the PO', invoice_date_note: 'the note on the invoice date',
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
  const invoiceFile = document.getElementById('invoiceFile').files[0];
  const fileProblem = invoiceFile ? docFileProblem(invoiceFile) : '';
  if (fileProblem) {
    showErrors([{ field: '', message: 'Invoice copy: ' + fileProblem }]);
    document.getElementById('invoiceFile').focus();
    return;
  }
  btn.disabled = true;
  try {
    const mir = await apiMir('/entries/new', { method: 'POST', body: payload() });
    const n = mir.mismatches.length;
    // The invoice copy goes up once the MIR exists. The MIR is saved either
    // way; a failed upload says so and can be retried from the register.
    let fileNote = '';
    if (invoiceFile) {
      try {
        const fd = new FormData();
        fd.append('file', invoiceFile);
        await docFileUpload('/api/mir/entries/' + mir.id + '/invoice', fd);
        fileNote = ' Invoice copy attached.';
      } catch (e) {
        fileNote = ' The invoice copy was NOT attached (' + e.message + ') - attach it from the MIR register.';
      }
    }
    // Straight back to an empty form for the next delivery; the saved MIR
    // is confirmed in a toast that fades, and stays findable in the register.
    mirToast(mir.mirNo + ' saved - ' + mir.vendor.name + ', invoice ' + mir.invoiceNo + ', ' + money(mir.computedTotal, mirCurrency()) +
      (n ? '. ' + n + ' difference' + (n === 1 ? '' : 's') + ' sent to Open mismatches.' : '.') + fileNote);
    clearDraft();
    resetForm();
    refreshMismatchCount();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    btn.disabled = false;
    await runPreview();
    showErrors(e.errors && e.errors.length ? e.errors : [{ field: '', message: e.message }]);
  }
}

// A draft of the form in this browser only (localStorage), per user, so a
// sign-in that expires mid-entry - apiMir() then sends the page to the
// login screen - or a closed tab does not lose what was typed. Saved on
// every change, removed when the MIR is saved or the form is cleared, and
// offered back (never restored silently) when the page opens. Restoring
// re-reads each PO from the server, so a line closed or received meanwhile
// is dropped rather than resurrected.
const DRAFT_FIELDS = ['plantSel', 'mirDate', 'invoiceNo', 'invoiceDate', 'invoiceTotal', 'tcsAmount', 'taxType', 'sapGrnNo',
  'challanNo', 'lrNo', 'vehicleNo', 'ewayBillNo', 'gateEntryNo', 'weighbridgeSlipNo', 'remarks'];
const DRAFT_MAX_AGE_MS = 3 * 24 * 3600 * 1000;
// CURRENT_USER is auth.js's own (set by requireAuth()); this page must not declare it again.
function draftKey() { return 'mirDraft:v1:' + ((CURRENT_USER && CURRENT_USER.email) || ''); }

function saveDraft() {
  try {
    if (!S.lines.length) { localStorage.removeItem(draftKey()); return; }
    const fields = {};
    DRAFT_FIELDS.forEach(id => { fields[id] = document.getElementById(id).value; });
    localStorage.setItem(draftKey(), JSON.stringify({
      savedAt: Date.now(), fields, taxTouched: S.taxTouched, header: S.header,
      vendor: S.vendor, vendorFromPo: S.vendorFromPo,
      lines: S.lines.map(ln => ({ poId: ln.po.id, lineId: ln.line.id, poNumber: ln.line.poNumber, v: ln.v })),
    }));
  } catch (e) { /* storage full or blocked: the form still works, just without a draft */ }
}
const scheduleDraft = debounce(saveDraft, 500);

function clearDraft() {
  try { localStorage.removeItem(draftKey()); } catch (e) { /* nothing to clear */ }
}

function readDraft() {
  try {
    const d = JSON.parse(localStorage.getItem(draftKey()) || 'null');
    if (!d || !Array.isArray(d.lines) || !d.lines.length) return null;
    if (Date.now() - d.savedAt > DRAFT_MAX_AGE_MS) { clearDraft(); return null; }
    return d;
  } catch (e) { return null; }
}

function offerDraft() {
  const d = readDraft();
  const box = document.getElementById('draftBanner');
  if (!d || !box || document.getElementById('mirForm').hidden) return;
  const pos = [...new Set(d.lines.map(l => l.poNumber))].join(', ');
  box.innerHTML = '<div>You have an unsaved MIR from ' + escapeHtml(new Date(d.savedAt).toLocaleString('en-IN')) + ' - ' +
    d.lines.length + ' line' + (d.lines.length === 1 ? '' : 's') + ' of PO ' + escapeHtml(pos) +
    (d.fields.invoiceNo ? ', invoice ' + escapeHtml(d.fields.invoiceNo) : '') + '.</div>' +
    '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" id="draftRestore">Continue it</button>' +
    '<button type="button" class="mir-link" id="draftDiscard">Discard</button></div>';
  box.hidden = false;
  document.getElementById('draftDiscard').onclick = () => { clearDraft(); box.hidden = true; };
  document.getElementById('draftRestore').onclick = async () => {
    box.innerHTML = '<div class="mir-muted">Restoring...</div>';
    try {
      const dropped = await restoreDraft(d);
      box.hidden = true;
      mirToast('Your unsaved MIR is back' + (dropped ? '; ' + dropped + ' line' + (dropped === 1 ? ' is' : 's are') + ' no longer receivable and were left out.' : '.'));
    } catch (e) {
      box.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
    }
  };
}

async function restoreDraft(d) {
  resetForm(true);
  DRAFT_FIELDS.forEach(id => { if (d.fields[id] !== undefined) document.getElementById(id).value = d.fields[id]; });
  S.taxTouched = !!d.taxTouched;
  S.header = d.header || {};
  const pos = {};
  for (const poId of [...new Set(d.lines.map(l => l.poId))]) pos[poId] = await apiMir('/purchase-orders/' + poId);
  let dropped = 0;
  d.lines.forEach(saved => {
    const po = pos[saved.poId];
    const line = po && po.lines.find(l => l.id === saved.lineId);
    if (!line || !line.receivable) { dropped += 1; return; }
    S.lines.push({ line, po, v: saved.v });
  });
  if (S.lines.length) {
    S.vendor = d.vendor || S.lines[0].po.vendor;
    S.vendorFromPo = !!d.vendorFromPo;
    showEntrySections(true);
    document.querySelectorAll('[data-currency]').forEach(el => { el.textContent = mirCurrency(); });
    renderVendor();
    renderLines();
    schedulePreview();
  }
  saveDraft();
  return dropped;
}

function mirToast(text) {
  let stack = document.getElementById('mirToasts');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'mirToasts';
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

function resetForm(keepDraft) {
  S.lines = []; S.vendor = null; S.vendorFromPo = false; S.taxTouched = false; S.header = {}; S.preview = null; S.triedToPost = false;
  // The receiving plant is kept: a store entering several MIRs is at one plant.
  const plant = document.getElementById('plantSel').value;
  document.getElementById('mirForm').reset();
  document.getElementById('plantSel').value = plant;
  document.getElementById('mirDate').value = META.today;
  ['poResults', 'linesArea', 'totalsBox', 'formErrors', 'vendorBox', 'noticeBox', 'todoBox'].forEach(id => { document.getElementById(id).innerHTML = ''; });
  ['taxReasonRow', 'totalReasonRow', 'invoiceDateReasonRow'].forEach(id => { const r = document.getElementById(id); r.hidden = true; r.innerHTML = ''; r.dataset.sig = ''; });
  showEntrySections(false);
  document.getElementById('mirForm').hidden = false;
  document.getElementById('postBtn').disabled = false;
  updateProgress(null);
  if (!keepDraft) clearDraft();
  document.getElementById('poSearch').focus();
}

// ── Register ──────────────────────────────────────────────────────────────
function plantOptions() {
  return '<option value="">All plants</option>' +
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
  const current = mirLoadTicket('register');
  try {
    const data = await apiMir('/entries?' + params.toString());
    if (!current()) return;
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
    if (current()) area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}

async function loadDetail(id) {
  const area = document.getElementById('detailArea');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let m;
  try { m = await apiMir('/entries/' + id); } catch (e) { area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>'; return; }
  const mayWrite = m.status === 'POSTED' && (META.plants.find(p => p.code === m.plant.code) || {}).canReceive;
  const tax = META.taxTypes.find(t => t.code === m.taxType);
  const cur = m.lines.length ? m.lines[0].currency : 'INR';
  const facts = factsTableHtml([
    ['Plant', m.plant.name], ['MIR date', dateIN(m.mirDate)],
    ['Vendor', m.vendor.name + (m.vendor.gstin ? ' (' + m.vendor.gstin + ')' : '')], ['Invoice', m.invoiceNo + ', ' + dateIN(m.invoiceDate)],
    ['Invoice total', money(m.invoiceTotal, cur)], ['Computed total', money(m.computedTotal, cur)],
    ['Tax type', tax ? tax.label : m.taxType], ['TCS', Number(m.tcsAmount) ? money(m.tcsAmount, cur) : ''],
    ['SAP GRN number', m.sapGrnNumber], ['Vehicle', m.vehicleNo], ['Challan', m.challanNo], ['LR', m.lrNo],
    ['E-way bill', m.ewayBillNo], ['Gate entry', m.gateEntryNo], ['Weighbridge slip', m.weighbridgeSlipNo],
    ['Entered by', m.createdBy + ', ' + new Date(m.createdAt).toLocaleString('en-IN')],
    ['Remarks', m.remarks, true], ['Cancelled', m.cancelReason ? m.cancelledBy + ': ' + m.cancelReason : '', true],
  ]);
  const canReject = mayWrite && m.canReject;
  area.innerHTML = '<section class="mir-panel mir-detail">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">' + escapeHtml(m.mirNo) + '</h3>' +
      statusPill(m.status === 'POSTED' ? 'Posted' : 'Cancelled', MIR_STATUS_TONE[m.status]) +
      (mayWrite && (m.canEdit || m.canEditGrn) ? '<button type="button" class="btn btn-navy btn-small" id="editBtn">' + (m.canEdit ? 'Edit details' : 'Add SAP GRN number') + '</button>' : '') +
    '</div>' +
    (mayWrite ? '<p class="mir-help">' + (m.canEdit
      ? 'Paperwork (invoice number and date, SAP GRN, transport, remarks, department) can be corrected until ' + dateIN(m.editUntil) + '. '
      : 'The edit window closed on ' + dateIN(m.editUntil) + '; the SAP GRN number can still be added. ') +
      (m.canReject ? 'A rejection found later can be recorded until ' + dateIN(m.rejectUntil) + '. ' : '') +
      'Quantities, rates, GST and totals are never edited: if one is wrong, cancel this MIR and enter it again.</p>' : '') +
    facts +
    invoiceFilesHtml(m, mayWrite) +
    '<div id="editArea"></div>' +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>PO / line</th><th>Material</th><th>Category</th><th class="num">Received</th><th class="num">Rejected</th>' +
      '<th class="num">Rate</th><th class="num">PO rate</th><th class="num">GST %</th><th class="num">Taxable</th><th class="num">Total</th><th class="num" title="What this line put into the store, and how much of it is still there (RM Store)">In store</th>' + (canReject ? '<th></th>' : '') + '</tr></thead><tbody>' +
      m.lines.map(l => '<tr><td>' + l.lineNo + '</td><td>' + escapeHtml(l.poNumber) + ' #' + l.poLineNo + '<div class="mir-muted">' + escapeHtml(plantName(l.poPlant)) + '</div></td>' +
        '<td>' + escapeHtml(l.description) + (l.deptUse ? '<div class="mir-muted">For ' + escapeHtml(l.deptUse) + '</div>' : '') + (l.remarks ? '<div class="mir-muted">' + escapeHtml(l.remarks) + '</div>' : '') + '</td>' +
        '<td>' + escapeHtml(l.materialCategory || '-') + (l.materialSubcategory ? '<div class="mir-muted">' + escapeHtml(l.materialSubcategory) + '</div>' : '') + '</td>' +
        '<td class="num">' + qty(l.qtyReceived) + ' ' + escapeHtml(l.uom) + '</td><td class="num">' + (Number(l.qtyRejected) ? qty(l.qtyRejected) + ' ' + escapeHtml(l.uom) : '-') + '</td>' +
        '<td class="num">' + money(l.rate, l.currency) + '</td><td class="num">' + money(l.poRate, l.currency) + '</td><td class="num">' + Number(l.gstRate) + '%</td>' +
        '<td class="num">' + money(l.taxable, l.currency) + '</td><td class="num">' + money(l.lineTotal, l.currency) + '</td>' +
        '<td class="num">' + mirStockCellHtml(l.stock) + '</td>' +
        (canReject ? '<td><button type="button" class="mir-link" data-reject="' + l.lineNo + '">Record rejection</button></td>' : '') + '</tr>' +
        (canReject ? '<tr class="mir-reject-row" data-reject-row="' + l.lineNo + '" hidden><td colspan="13"></td></tr>' : '')).join('') +
    '</tbody></table></div>' +
    (m.mismatches.length ? '<h4 class="mir-subtitle">Differences recorded</h4>' + mismatchListHtml(m.mismatches.map(x => Object.assign({ currency: cur }, x)), false) : '') +
    (m.history.length ? '<h4 class="mir-subtitle">Change history</h4><div class="table-wrap"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>From</th><th>To</th><th>Reason</th></tr></thead><tbody>' +
      m.history.map(h => '<tr><td class="nowrap">' + escapeHtml(new Date(h.at).toLocaleString('en-IN')) + '</td><td>' + escapeHtml(h.by) + '</td>' +
        '<td>' + escapeHtml((HISTORY_LABELS[h.field] || h.field) + (h.lineNo ? ', line ' + h.lineNo : '')) + '</td>' +
        '<td>' + escapeHtml(h.oldValue || '-') + '</td><td>' + escapeHtml(h.newValue || '-') + '</td><td>' + escapeHtml(h.reason) + '</td></tr>').join('') +
      '</tbody></table></div>' : '') +
    (mayWrite ? '<div class="mir-cancel"><input class="form-control" id="cancelReason" maxlength="500" placeholder="Why is this MIR being cancelled?" aria-label="Cancel reason">' +
      '<button type="button" class="btn btn-navy" id="cancelBtn">Cancel this MIR</button></div><div class="mir-error-text" id="cancelErr"></div>' : '') +
  '</section>';
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
  bindInvoiceFiles(area, id);
  const editBtn = document.getElementById('editBtn');
  if (editBtn) editBtn.onclick = () => openEdit(m);
  area.querySelectorAll('[data-reject]').forEach(b => { b.onclick = () => openReject(m, Number(b.dataset.reject)); });
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

/** The MIR's invoice copies, newest revision first, and - for a posted MIR
    the caller may write - a picker to attach one or replace the current one
    (the older copy is kept as a superseded revision). */
function invoiceFilesHtml(m, mayWrite) {
  const files = m.invoiceFiles || [];
  return '<h4 class="mir-subtitle">Invoice copy</h4>' +
    (files.length ? files.map(docFileLineHtml).join('') : '<p class="mir-muted doc-file-none">No invoice copy attached.</p>') +
    (mayWrite ? '<div class="doc-file-upload"><label class="form-label" for="invoiceFileLater">' + (files.length ? 'Replace with a newer copy' : 'Attach the invoice') + '</label>' +
      '<input class="form-control" type="file" id="invoiceFileLater" accept="' + DOC_FILE_ACCEPT + '">' +
      '<button type="button" class="btn btn-navy btn-small" id="invoiceUploadBtn">Upload</button>' +
      '<span class="mir-error-text" id="invoiceUploadErr" role="alert"></span></div>' : '');
}

function bindInvoiceFiles(area, mirId) {
  docFileBindOpen(area);
  const btn = document.getElementById('invoiceUploadBtn');
  if (!btn) return;
  btn.onclick = async () => {
    const err = document.getElementById('invoiceUploadErr');
    const file = document.getElementById('invoiceFileLater').files[0];
    const problem = docFileProblem(file);
    if (problem) { err.textContent = problem; return; }
    btn.disabled = true;
    err.textContent = '';
    try {
      const fd = new FormData();
      fd.append('file', file);
      await docFileUpload('/api/mir/entries/' + mirId + '/invoice', fd);
      loadDetail(mirId);
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
    }
  };
}

/** A MIR line's stock (stock_service.mir_line_stock()): what it put into
    the store, in the stock unit (weight in KG), and how much is left. */
function mirStockCellHtml(st) {
  if (!st) return '-';
  if (!st.stocked) return '<span class="mir-muted">Straight to use</span>';
  return qty(st.balance) + ' ' + escapeHtml(st.uom) + '<div class="mir-muted">of ' + qty(st.in) + ' received</div>';
}

const HISTORY_LABELS = {
  invoice_no: 'Invoice number', invoice_date: 'Invoice date', sap_grn_number: 'SAP GRN number', challan_no: 'Challan no.',
  lr_no: 'LR no.', vehicle_no: 'Vehicle no.', eway_bill_no: 'E-way bill no.', gate_entry_no: 'Gate entry no.',
  weighbridge_slip_no: 'Weighbridge slip no.', remarks: 'Remarks', dept_use: 'Department use', qty_rejected: 'Qty rejected',
};

/** The limited edit form: only what edit_mir() accepts, everything else is
    shown read-only in the table above. After the edit window only the SAP
    GRN number is open. */
function openEdit(m) {
  const area = document.getElementById('editArea');
  const full = m.canEdit;
  const input = (key, value, attrs) => '<input class="form-control" data-edit="' + key + '" value="' + escapeHtml(value || '') + '" ' + (attrs || '') + '>';
  const header = [
    ['sap_grn_number', 'SAP GRN number', m.sapGrnNumber, 'maxlength="50"', true],
    ['invoice_no', 'Invoice number', m.invoiceNo, 'maxlength="60"'], ['invoice_date', 'Invoice date', m.invoiceDate, 'type="date" max="' + m.mirDate + '"'],
    ['vehicle_no', 'Vehicle no.', m.vehicleNo, 'maxlength="30"'], ['challan_no', 'Challan no.', m.challanNo, 'maxlength="60"'],
    ['lr_no', 'LR no.', m.lrNo, 'maxlength="60"'], ['eway_bill_no', 'E-way bill no.', m.ewayBillNo, 'maxlength="30"'],
    ['gate_entry_no', 'Gate entry no.', m.gateEntryNo, 'maxlength="40"'], ['weighbridge_slip_no', 'Weighbridge slip no.', m.weighbridgeSlipNo, 'maxlength="40"'],
  ].filter(f => full || f[4]);
  area.innerHTML = '<div class="mir-edit">' +
    '<div class="mir-line-group-title">' + (full ? 'Edit details' : 'Add the SAP GRN number') + '</div>' +
    '<div class="mir-grid">' + header.map(f => field(escapeHtml(f[1]), input(f[0], f[2], f[3]))).join('') + '</div>' +
    (full ? field('Remarks', '<textarea class="form-control" data-edit="remarks" rows="2" maxlength="2000">' + escapeHtml(m.remarks || '') + '</textarea>') +
      '<div class="mir-grid">' + m.lines.map(l => field('Line ' + l.lineNo + ' - department use', '<input class="form-control" data-edit-line="' + l.lineNo + '" data-edit-key="dept_use" maxlength="60" value="' + escapeHtml(l.deptUse || '') + '">')).join('') + '</div>' : '') +
    field('Why is this being changed?' + req(), '<input class="form-control" id="editReason" maxlength="500" placeholder="e.g. Invoice number typed wrongly">') +
    '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" id="saveEdit">Save changes</button>' +
      '<button type="button" class="mir-link" id="closeEdit">Close</button><span class="mir-error-text" id="editErr"></span></div>' +
  '</div>';
  document.getElementById('closeEdit').onclick = () => { area.innerHTML = ''; };
  document.getElementById('saveEdit').onclick = async () => {
    const header = {}, lines = {};
    area.querySelectorAll('[data-edit]').forEach(el => {
      const was = el.dataset.edit === 'remarks' ? (m.remarks || '') : String((m[{ sap_grn_number: 'sapGrnNumber', invoice_no: 'invoiceNo', invoice_date: 'invoiceDate',
        vehicle_no: 'vehicleNo', challan_no: 'challanNo', lr_no: 'lrNo', eway_bill_no: 'ewayBillNo', gate_entry_no: 'gateEntryNo',
        weighbridge_slip_no: 'weighbridgeSlipNo' }[el.dataset.edit]]) || '');
      if (el.value !== was) header[el.dataset.edit] = el.value;
    });
    area.querySelectorAll('[data-edit-line]').forEach(el => {
      const l = m.lines.find(x => String(x.lineNo) === el.dataset.editLine);
      if (el.value !== (l.deptUse || '')) (lines[el.dataset.editLine] = lines[el.dataset.editLine] || {})[el.dataset.editKey] = el.value;
    });
    try {
      await apiMir('/entries/' + m.id + '/edit', { method: 'POST', body: { header, lines, reason: document.getElementById('editReason').value } });
      loadDetail(m.id);
      loadRegister();
    } catch (e) { document.getElementById('editErr').textContent = e.message; }
  };
}

/** A rejection found after posting (the QC report): the new TOTAL rejected
    on the line, a rejection reason and a note. */
function openReject(m, lineNo) {
  const l = m.lines.find(x => x.lineNo === lineNo);
  const row = document.querySelector('[data-reject-row="' + lineNo + '"]');
  row.hidden = false;
  row.firstElementChild.innerHTML = '<div class="mir-edit">' +
    '<div class="mir-line-group-title">Record a rejection on line ' + lineNo + ' - ' + escapeHtml(l.description) + '</div>' +
    '<p class="mir-help">Received ' + escapeHtml(qty(l.qtyReceived) + ' ' + l.uom) + ', rejected so far ' + escapeHtml(qty(l.qtyRejected) + ' ' + l.uom) +
      '. Enter the new total rejected; the accepted quantity and the PO balance update at once, and the rejection goes to Open mismatches for the debit note or replacement.</p>' +
    '<div class="mir-grid">' +
      field('Total rejected (' + escapeHtml(l.uom) + ')' + req(), '<input class="form-control" id="rejQty" inputmode="decimal" autocomplete="off">') +
      field('Reason' + req(), reasonSelectHtml('REJECTION', '', 'id="rejReason"')) +
      field('Note', '<input class="form-control" id="rejNote" maxlength="2000" placeholder="e.g. QC report no.">') +
    '</div>' +
    '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" id="saveRej">Record rejection</button>' +
      '<button type="button" class="mir-link" id="closeRej">Close</button><span class="mir-error-text" id="rejErr"></span></div></div>';
  document.getElementById('closeRej').onclick = () => { row.hidden = true; };
  document.getElementById('saveRej').onclick = async () => {
    try {
      await apiMir('/entries/' + m.id + '/lines/' + lineNo + '/reject', { method: 'POST', body: {
        qtyRejected: document.getElementById('rejQty').value, reason: document.getElementById('rejReason').value, note: document.getElementById('rejNote').value } });
      loadDetail(m.id);
      refreshMismatchCount();
    } catch (e) { document.getElementById('rejErr').textContent = e.message; }
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
    const figures = x.kind === 'TAX_TYPE' ? '' : x.kind === 'INVOICE_BEFORE_PO' ? '<span>Invoice <b>' + Number(x.actual) + ' days</b> before the PO date</span>' : '<span>Expected <b>' + fig(x.expected) + '</b></span><span>Actual <b>' + fig(x.actual) + '</b></span>' +
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
  const current = mirLoadTicket('mismatches');
  try {
    const data = await apiMir('/mismatches?' + params.toString());
    if (!current()) return;
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
    if (current()) area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}
