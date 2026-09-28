// mir.html's page script - see mir.html's header comment. Self-contained
// like review-page.js: its own fetch wrapper, no dependency on main.js.
//
// The page never prices a line or decides a mismatch itself. Every input
// change sends the whole form to /api/mir/preview (debounced) and paints
// what comes back - the figures, the mismatches that need a reason, the
// field errors. Saving sends the same body to /api/mir/entries/new, which
// re-runs the same check server-side inside the transaction. Inputs are
// never re-rendered while the clerk types, so focus and cursor survive a
// preview; only the computed cells and the reason rows are repainted.

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
const LINE_KEYS = ['qty_received', 'qty_rejected', 'rate', 'discount', 'other_charges', 'gst_rate', 'rolls', 'batch_no', 'dept_use',
  'qty_reason', 'qty_note', 'rate_reason', 'rate_note'];

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
    document.getElementById('main-content').prepend(noticeEl(e.message));
    return;
  }
  initNewMir();
  initRegister();
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
// this is a record being entered, and every paisa is compared.
function money(s) {
  if (s === null || s === undefined || s === '') return '-';
  return 'Rs ' + Number(s).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function qty(s) {
  if (s === null || s === undefined || s === '') return '-';
  return Number(s).toLocaleString('en-IN', { maximumFractionDigits: 3 });
}
// "115.0000" -> "115", "12.5000" -> "12.5": the PO rate as a person would type it.
function trimZeros(s) { return s === null || s === undefined ? '' : String(s).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, ''); }
function dateIN(iso) { return iso ? formatDateIN(iso) : '-'; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function noticeEl(text) { const d = document.createElement('div'); d.className = 'mir-notice'; d.textContent = text; return d; }
function reasonsOf(kind) { return META.reasons.filter(r => r.kind === kind); }
function reasonByCode(code) { return META.reasons.find(r => r.code === code); }
function plantName(code) { const p = META.plants.find(x => x.code === code); return p ? p.name : code; }
const MISMATCH_KIND = { QTY_SHORT: 'QTY_SHORT', QTY_OVER: 'QTY_OVER', RATE_HIGH: 'RATE', RATE_LOW: 'RATE', INVOICE_TOTAL: 'INVOICE_TOTAL', TAX_TYPE: 'TAX_TYPE' };

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
  ['plantSel', 'mirDate', 'invoiceNo', 'invoiceDate', 'invoiceTotal', 'tcsAmount'].forEach(id =>
    document.getElementById(id).addEventListener('input', schedule));
  document.getElementById('taxType').addEventListener('change', () => { S.taxTouched = true; schedule(); });
  document.getElementById('mirForm').addEventListener('submit', e => { e.preventDefault(); postMir(); });
  document.getElementById('newAnotherBtn').onclick = resetForm;
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
      box.innerHTML = '<div class="mir-muted">No open PO matches "' + escapeHtml(q) + '". If the PO exists, it may be missing from the master PO sheet - ask purchase to add it.</div>';
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
      '<span class="mir-badge">' + escapeHtml(po.plant.name) + '</span>' +
      '<b>' + escapeHtml(po.poNumber) + '</b>' +
      '<span>' + escapeHtml(po.vendor ? po.vendor.name : 'No vendor on the PO') + '</span>' +
      '<span class="mir-muted">' + dateIN(po.poDate) + ' &middot; ' + po.openLines + ' of ' + po.totalLines + ' lines open</span>' +
      (clash ? '<span class="mir-muted">Another vendor - one MIR is one invoice</span>'
        : '<button type="button" class="btn btn-navy btn-small" data-open-po="' + po.id + '">Show lines</button>') +
    '</div><div class="mir-po-lines"></div></div>';
}

async function openPo(poId, el) {
  const target = el.querySelector('.mir-po-lines');
  target.innerHTML = '<div class="mir-muted">Loading lines...</div>';
  try {
    const po = await apiMir('/purchase-orders/' + poId);
    const picked = new Set(S.lines.map(l => l.line.id));
    target.innerHTML = '<table class="mir-table"><thead><tr><th></th><th>#</th><th>Material</th><th>Unit</th><th class="num">Ordered</th>' +
      '<th class="num">Received</th><th class="num">Open</th><th class="num">PO rate</th><th>Delivery</th></tr></thead><tbody>' +
      po.lines.map(l => {
        const late = l.deliveryDate && l.deliveryDate < META.today && l.receivable;
        return '<tr class="' + (l.receivable ? '' : 'is-disabled') + '">' +
          '<td>' + (l.receivable ? '<input type="checkbox" data-pick="' + l.id + '"' + (picked.has(l.id) ? ' checked disabled' : '') + ' aria-label="Pick line ' + l.lineNo + '">' : '') + '</td>' +
          '<td>' + l.lineNo + '</td>' +
          '<td>' + escapeHtml(l.description) + (l.itemCode ? ' <span class="mir-muted">' + escapeHtml(l.itemCode) + '</span>' : '') +
            (l.receivable ? '' : '<div class="mir-muted">' + escapeHtml(l.blockedReason) + '</div>') + '</td>' +
          '<td>' + escapeHtml(l.uom) + (l.uomKnown ? '' : ' <span class="mir-flag" title="Unit not recognised on the PO sheet">?</span>') + '</td>' +
          '<td class="num">' + qty(l.qtyOrdered) + '</td><td class="num">' + qty(l.accepted) + '</td>' +
          '<td class="num"><b>' + qty(l.openQty) + '</b></td><td class="num">' + money(l.rate) + '</td>' +
          '<td>' + dateIN(l.deliveryDate) + (late ? ' <span class="mir-flag">late</span>' : '') + '</td></tr>';
      }).join('') + '</tbody></table>' +
      '<div class="mir-actions"><button type="button" class="btn btn-primary btn-small" data-add>Add selected lines</button></div>';
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
    S.lines.push({ line, po, v: { qty_received: '', qty_rejected: '', rate: trimZeros(line.rate), discount: '', other_charges: '',
      gst_rate: po.gstRate || '', rolls: '', batch_no: '', dept_use: '', qty_reason: '', qty_note: '', rate_reason: '', rate_note: '' } });
  });
  document.getElementById('invoiceCard').hidden = false;
  document.getElementById('linesCard').hidden = false;
  renderVendor();
  renderLines();
  schedulePreview();
}

function renderVendor() {
  const box = document.getElementById('vendorBox');
  if (S.vendor) {
    box.innerHTML = '<span class="form-label">Vendor</span> <b>' + escapeHtml(S.vendor.name) + '</b>' +
      (S.vendor.gstin ? ' <span class="mir-muted">GSTIN ' + escapeHtml(S.vendor.gstin) + '</span>' : ' <span class="mir-flag">no GSTIN on file</span>') +
      (S.vendorFromPo ? '' : ' <button type="button" class="mir-link" id="changeVendor">change</button>');
    const change = document.getElementById('changeVendor');
    if (change) change.onclick = () => { S.vendor = null; renderVendor(); schedulePreview(); };
    return;
  }
  box.innerHTML = '<div class="form-group"><label class="form-label" for="vendorSearch">Vendor on the invoice (the PO names none)</label>' +
    '<input class="form-control" id="vendorSearch" placeholder="Vendor name or GSTIN" autocomplete="off"></div><div id="vendorHits" class="mir-vendor-hits"></div>';
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

function renderLines() {
  const area = document.getElementById('linesArea');
  const slabs = META.gstSlabs;
  area.innerHTML = S.lines.map((ln, i) => {
    const l = ln.line;
    const input = (key, attrs) => '<input class="form-control num" data-idx="' + i + '" data-key="' + key + '" value="' + escapeHtml(ln.v[key]) + '" ' + (attrs || '') + '>';
    return '<div class="mir-line" data-line="' + i + '">' +
      '<div class="mir-line-head">' +
        '<div><span class="mir-badge">' + escapeHtml(plantName(l.plant)) + '</span> <b>PO ' + escapeHtml(l.poNumber) + '</b> line ' + l.lineNo +
          ' - ' + escapeHtml(l.description) + '</div>' +
        '<div class="mir-muted">Ordered ' + qty(l.qtyOrdered) + ' ' + escapeHtml(l.uom) + ' &middot; received so far ' + qty(l.accepted) +
          ' &middot; <b>open ' + qty(l.openQty) + '</b> &middot; PO rate ' + money(l.rate) + '</div>' +
        '<button type="button" class="mir-link" data-remove="' + i + '">Remove</button>' +
      '</div>' +
      '<div class="mir-line-grid">' +
        field('Qty received (' + escapeHtml(l.uom) + ')', input('qty_received', 'inputmode="decimal"')) +
        field('Qty rejected', input('qty_rejected', 'inputmode="decimal" placeholder="0"')) +
        field('Rate on invoice (Rs)', input('rate', 'inputmode="decimal"')) +
        field('Discount (Rs)', input('discount', 'inputmode="decimal" placeholder="0"')) +
        field('Freight / packing (Rs)', input('other_charges', 'inputmode="decimal" placeholder="0"')) +
        field('GST %', '<select class="form-control" data-idx="' + i + '" data-key="gst_rate"><option value="">choose</option>' +
          slabs.map(s => '<option value="' + s + '"' + (String(Number(ln.v.gst_rate)) === String(Number(s)) && ln.v.gst_rate !== '' ? ' selected' : '') + '>' + s + '%</option>').join('') + '</select>') +
        field('Rolls', input('rolls', 'inputmode="numeric" placeholder="-"')) +
        field('Batch / lot no.', '<input class="form-control" data-idx="' + i + '" data-key="batch_no" maxlength="60" value="' + escapeHtml(ln.v.batch_no) + '">') +
        field('Department use', '<input class="form-control" data-idx="' + i + '" data-key="dept_use" maxlength="60" value="' + escapeHtml(ln.v.dept_use) + '">') +
      '</div>' +
      '<div class="mir-line-figures" id="fig-' + i + '"></div>' +
      '<div class="mir-reasons" id="reasons-' + i + '"></div>' +
    '</div>';
  }).join('');
  area.querySelectorAll('[data-key]').forEach(el => {
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
      S.lines[Number(el.dataset.idx)].v[el.dataset.key] = el.value;
      schedulePreview();
    });
  });
  area.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => {
    S.lines.splice(Number(b.dataset.remove), 1);
    if (!S.lines.length) { S.vendor = null; S.vendorFromPo = false; document.getElementById('invoiceCard').hidden = true; document.getElementById('linesCard').hidden = true; }
    renderVendor();
    renderLines();
    schedulePreview();
  }; });
}

function field(label, control) {
  return '<label class="form-group"><span class="form-label">' + label + '</span>' + control + '</label>';
}

function payload() {
  const val = id => document.getElementById(id).value;
  const body = {
    plant: val('plantSel'), mir_date: val('mirDate'), invoice_no: val('invoiceNo'), invoice_date: val('invoiceDate'),
    invoice_total: val('invoiceTotal'), tcs_amount: val('tcsAmount'),
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
  // Figures per line.
  S.lines.forEach((ln, i) => {
    const fig = p.lines.find(x => x.index === i);
    const box = document.getElementById('fig-' + i);
    if (!box) return;
    box.innerHTML = fig && fig.total !== null
      ? '<span>Taxable <b>' + money(fig.taxable) + '</b></span>' +
        (Number(fig.igst) ? '<span>IGST ' + money(fig.igst) + '</span>' : '<span>CGST ' + money(fig.cgst) + '</span><span>' + (p.taxType === 'CGST_UGST' ? 'UGST ' : 'SGST ') + money(fig.sgst) + '</span>') +
        '<span>Line total <b>' + money(fig.total) + '</b></span>'
      : '';
  });
  // Reason rows: one per mismatch the server found on each line.
  S.lines.forEach((ln, i) => {
    const box = document.getElementById('reasons-' + i);
    if (!box) return;
    const mms = p.mismatches.filter(m => m.line === i);
    const sig = mms.map(m => m.kind + m.expected + m.actual).join('|');
    if (box.dataset.sig === sig) return;  // unchanged - keep the clerk's selection and focus
    box.dataset.sig = sig;
    box.innerHTML = mms.map(m => reasonRowHtml(m, i, ln)).join('');
    wireReasonRow(box, i);
  });
  // Tax type: default to what the states imply until the clerk changes it.
  const taxSel = document.getElementById('taxType');
  if (!S.taxTouched && p.taxTypeExpected) taxSel.value = p.taxTypeExpected;
  const expected = META.taxTypes.find(t => t.code === p.taxTypeExpected);
  document.getElementById('taxTypeHint').textContent = expected ? 'Expected from the vendor\'s and plant\'s states: ' + expected.label : 'Vendor state unknown - choose the tax type on the invoice.';
  headerReason('TAX_TYPE', 'taxReasonRow', 'tax_type', p);
  headerReason('INVOICE_TOTAL', 'totalReasonRow', 'invoice_total', p);
  // Totals.
  const diff = p.computedTotal !== null && p.invoiceTotal !== null ? Number(p.invoiceTotal) - Number(p.computedTotal) : null;
  document.getElementById('totalsBox').innerHTML =
    '<span>Computed total <b>' + money(p.computedTotal) + '</b></span>' +
    '<span>Invoice total <b>' + money(p.invoiceTotal) + '</b></span>' +
    (diff !== null ? '<span class="' + (Math.abs(diff) > Number(META.invoiceRoundingTolerance) ? 'mir-bad' : 'mir-good') + '">Difference ' + money(diff.toFixed(2)) + '</span>' : '');
  showErrors(p.errors);
  document.getElementById('postBtn').disabled = !p.ok;
  document.getElementById('postHint').textContent = p.ok
    ? (p.mismatches.length ? p.mismatches.length + ' difference(s) will be recorded for review.' : 'Everything agrees with the PO.')
    : '';
}

function mismatchText(m, ln) {
  const unit = ln ? ' ' + ln.line.uom : '';
  const pct = m.differencePct !== null ? ' (' + (Number(m.differencePct) > 0 ? '+' : '') + Number(m.differencePct) + '%)' : '';
  switch (m.kind) {
    case 'QTY_SHORT': return 'Accepted ' + qty(m.actual) + unit + ' against ' + qty(m.expected) + unit + ' open - short by ' + qty(Number(m.expected) - Number(m.actual)) + unit + pct + '.';
    case 'QTY_OVER': return 'Accepted ' + qty(m.actual) + unit + ' against ' + qty(m.expected) + unit + ' open - over by ' + qty(Number(m.actual) - Number(m.expected)) + unit + pct + '.';
    case 'RATE_HIGH': return 'Invoice rate ' + money(m.actual) + ' is above the PO rate ' + money(m.expected) + pct + '.';
    case 'RATE_LOW': return 'Invoice rate ' + money(m.actual) + ' is below the PO rate ' + money(m.expected) + pct + '.';
    case 'INVOICE_TOTAL': return 'Invoice total ' + money(m.actual) + ' differs from the computed ' + money(m.expected) + pct + '.';
    case 'TAX_TYPE': return 'Tax type ' + m.actualText + ' differs from the expected ' + m.expectedText + '.';
    default: return m.kind;
  }
}

function reasonSelectHtml(kind, selected, attrs) {
  return '<select class="form-control" ' + attrs + '><option value="">Choose a reason</option>' +
    reasonsOf(kind).map(r => '<option value="' + r.code + '"' + (r.code === selected ? ' selected' : '') + '>' + escapeHtml(r.label) + '</option>').join('') + '</select>';
}

function reasonRowHtml(m, i, ln) {
  const which = m.kind.startsWith('QTY') ? 'qty' : 'rate';
  const kind = MISMATCH_KIND[m.kind];
  const reason = reasonByCode(ln.v[which + '_reason']);
  const code = reason && reason.kind === kind ? reason.code : '';
  if (!code) ln.v[which + '_reason'] = '';
  return '<div class="mir-reason ' + (m.kind === 'QTY_SHORT' || m.kind === 'RATE_LOW' ? 'is-short' : 'is-over') + '">' +
    '<div class="mir-reason-text">' + escapeHtml(mismatchText(m, ln)) + '</div>' +
    reasonSelectHtml(kind, code, 'data-reason-idx="' + i + '" data-reason-key="' + which + '_reason" aria-label="Reason"') +
    '<input class="form-control" data-reason-idx="' + i + '" data-reason-key="' + which + '_note" maxlength="2000" placeholder="Note' +
      (reason && reason.noteRequired ? ' (required)' : ' (optional)') + '" value="' + escapeHtml(ln.v[which + '_note']) + '" aria-label="Note">' +
    (reason && reason.closesLine ? '<div class="mir-muted">This closes the PO line: no balance will be expected.</div>' : '') +
  '</div>';
}

function wireReasonRow(box, i) {
  box.querySelectorAll('[data-reason-key]').forEach(el => {
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
      S.lines[i].v[el.dataset.reasonKey] = el.value;
      if (el.tagName === 'SELECT') { box.dataset.sig = ''; }  // repaint for the note hint / closes-line notice
      schedulePreview();
    });
  });
}

function headerReason(kind, rowId, prefix, p) {
  const row = document.getElementById(rowId);
  const m = p.mismatches.find(x => x.kind === kind);
  if (!m) { row.hidden = true; row.innerHTML = ''; row.dataset.sig = ''; S.header[prefix + '_reason'] = ''; return; }
  const sig = m.kind + m.expected + m.actual + (m.actualText || '');
  row.hidden = false;
  if (row.dataset.sig === sig) return;
  row.dataset.sig = sig;
  const reason = reasonByCode(S.header[prefix + '_reason']);
  row.innerHTML = '<div class="mir-reason is-over"><div class="mir-reason-text">' + escapeHtml(mismatchText(m)) + '</div>' +
    reasonSelectHtml(kind, reason ? reason.code : '', 'data-h="' + prefix + '_reason" aria-label="Reason"') +
    '<input class="form-control" data-h="' + prefix + '_note" maxlength="2000" placeholder="Note' + (reason && reason.noteRequired ? ' (required)' : ' (optional)') +
      '" value="' + escapeHtml(S.header[prefix + '_note'] || '') + '" aria-label="Note"></div>';
  row.querySelectorAll('[data-h]').forEach(el => el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
    S.header[el.dataset.h] = el.value;
    if (el.tagName === 'SELECT') row.dataset.sig = '';
    schedulePreview();
  }));
}

const FIELD_LABELS = {
  plant: 'Receiving plant', mir_date: 'MIR date', invoice_no: 'Invoice number', invoice_date: 'Invoice date',
  invoice_total: 'Invoice total', tcs_amount: 'TCS', tax_type: 'Tax type', vendor_id: 'Vendor', lines: 'Lines',
  tax_type_reason: 'Tax type reason', tax_type_note: 'Tax type note', invoice_total_reason: 'Invoice total reason',
  invoice_total_note: 'Invoice total note',
  qty_received: 'qty received', qty_rejected: 'qty rejected', rate: 'rate', discount: 'discount', other_charges: 'freight/packing',
  gst_rate: 'GST %', rolls: 'rolls', po_line_id: 'PO line', qty_reason: 'quantity reason', qty_note: 'quantity note',
  rate_reason: 'rate reason', rate_note: 'rate note',
};

function fieldLabel(field) {
  const m = /^lines\.(\d+)\.(\w+)$/.exec(field);
  if (m) {
    const ln = S.lines[Number(m[1])];
    return 'Line ' + (Number(m[1]) + 1) + (ln ? ' (PO ' + ln.line.poNumber + ' #' + ln.line.lineNo + ')' : '') + ', ' + (FIELD_LABELS[m[2]] || m[2]);
  }
  return FIELD_LABELS[field] || field;
}

function showErrors(errors) {
  document.querySelectorAll('.is-invalid').forEach(el => el.classList.remove('is-invalid'));
  // Before the first Save, "Required." on an untouched field is noise, not news.
  const shown = S.triedToPost ? errors : errors.filter(e => e.message !== 'Required.' && e.message !== 'Choose a reason.');
  document.getElementById('formErrors').innerHTML = shown.map(e =>
    '<li>' + (e.field ? '<b>' + escapeHtml(fieldLabel(e.field)) + ':</b> ' : '') + escapeHtml(e.message) + '</li>').join('');
  errors.forEach(e => {
    const m = /^lines\.(\d+)\.(\w+)$/.exec(e.field || '');
    const el = m
      ? document.querySelector('[data-idx="' + m[1] + '"][data-key="' + m[2] + '"], [data-reason-idx="' + m[1] + '"][data-reason-key="' + m[2] + '"]')
      : document.querySelector('[data-field="' + e.field + '"], [data-h="' + e.field + '"]');
    if (el && (S.triedToPost || (e.message !== 'Required.' && e.message !== 'Choose a reason.'))) el.classList.add('is-invalid');
  });
}

async function postMir() {
  S.triedToPost = true;
  const btn = document.getElementById('postBtn');
  btn.disabled = true;
  try {
    const mir = await apiMir('/entries/new', { method: 'POST', body: payload() });
    document.getElementById('postedNo').textContent = mir.mirNo;
    document.getElementById('postedDetail').textContent = mir.vendor.name + ', invoice ' + mir.invoiceNo + ', ' + money(mir.computedTotal) +
      (mir.mismatches.length ? ' - ' + mir.mismatches.length + ' difference(s) recorded for review.' : '.');
    document.getElementById('postedBanner').hidden = false;
    document.getElementById('mirForm').hidden = true;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    showErrors(e.errors && e.errors.length ? e.errors : [{ field: '', message: e.message }]);
    btn.disabled = false;
    await runPreview();
  }
}

function resetForm() {
  S.lines = []; S.vendor = null; S.vendorFromPo = false; S.taxTouched = false; S.header = {}; S.preview = null; S.triedToPost = false;
  document.getElementById('mirForm').reset();
  document.getElementById('mirDate').value = META.today;
  ['poResults', 'linesArea', 'totalsBox', 'formErrors', 'vendorBox'].forEach(id => { document.getElementById(id).innerHTML = ''; });
  ['taxReasonRow', 'totalReasonRow'].forEach(id => { const r = document.getElementById(id); r.hidden = true; r.innerHTML = ''; r.dataset.sig = ''; });
  document.getElementById('invoiceCard').hidden = true;
  document.getElementById('linesCard').hidden = true;
  document.getElementById('postedBanner').hidden = true;
  document.getElementById('mirForm').hidden = false;
  document.getElementById('postBtn').disabled = true;
  document.getElementById('poSearch').focus();
}

// ── Register ──────────────────────────────────────────────────────────────
function initRegister() {
  const readable = META.plants.filter(p => p.canRead);
  document.getElementById('regPlant').innerHTML = '<option value="">All my plants</option>' +
    readable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
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
    if (!data.entries.length) { area.innerHTML = '<div class="mir-muted">No MIRs match.</div>'; return; }
    area.innerHTML = '<table class="mir-table mir-table-click"><thead><tr><th>MIR</th><th>Date</th><th>Plant</th><th>Vendor</th><th>Invoice</th>' +
      '<th class="num">Total</th><th>Status</th><th>Entered by</th></tr></thead><tbody>' +
      data.entries.map(e => '<tr data-mir="' + e.id + '" tabindex="0"><td><b>' + escapeHtml(e.mirNo) + '</b></td><td>' + dateIN(e.mirDate) + '</td>' +
        '<td>' + escapeHtml(e.plant.name) + '</td><td>' + escapeHtml(e.vendor.name) + '</td><td>' + escapeHtml(e.invoiceNo) + '</td>' +
        '<td class="num">' + money(e.computedTotal) + '</td><td><span class="mir-status mir-status-' + e.status.toLowerCase() + '">' + escapeHtml(e.status) + '</span></td>' +
        '<td>' + escapeHtml(e.createdBy) + '</td></tr>').join('') + '</tbody></table>';
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
  area.innerHTML = '<section class="mir-card mir-detail">' +
    '<h2 class="mir-card-title">' + escapeHtml(m.mirNo) + ' <span class="mir-status mir-status-' + m.status.toLowerCase() + '">' + escapeHtml(m.status) + '</span></h2>' +
    '<div class="mir-kv">' +
      kv('Plant', m.plant.name) + kv('MIR date', dateIN(m.mirDate)) + kv('Vendor', m.vendor.name + (m.vendor.gstin ? ' (' + m.vendor.gstin + ')' : '')) +
      kv('Invoice', m.invoiceNo + ', ' + dateIN(m.invoiceDate)) + kv('Invoice total', money(m.invoiceTotal)) + kv('Computed total', money(m.computedTotal)) +
      kv('Tax type', tax ? tax.label : m.taxType) + kv('TCS', money(m.tcsAmount)) + kv('Entered by', m.createdBy + ', ' + new Date(m.createdAt).toLocaleString('en-IN')) +
      (m.vehicleNo ? kv('Vehicle', m.vehicleNo) : '') + (m.challanNo ? kv('Challan', m.challanNo) : '') + (m.gateEntryNo ? kv('Gate entry', m.gateEntryNo) : '') +
      (m.cancelReason ? kv('Cancelled', m.cancelledBy + ': ' + m.cancelReason) : '') +
    '</div>' +
    '<table class="mir-table"><thead><tr><th>#</th><th>PO / line</th><th>Material</th><th class="num">Received</th><th class="num">Rejected</th>' +
      '<th class="num">Rate</th><th class="num">PO rate</th><th class="num">GST %</th><th class="num">Taxable</th><th class="num">Total</th></tr></thead><tbody>' +
      m.lines.map(l => '<tr><td>' + l.lineNo + '</td><td>' + escapeHtml(l.poNumber) + ' #' + l.poLineNo + ' <span class="mir-muted">' + escapeHtml(plantName(l.poPlant)) + '</span></td>' +
        '<td>' + escapeHtml(l.description) + '</td><td class="num">' + qty(l.qtyReceived) + ' ' + escapeHtml(l.uom) + '</td><td class="num">' + qty(l.qtyRejected) + '</td>' +
        '<td class="num">' + money(l.rate) + '</td><td class="num">' + money(l.poRate) + '</td><td class="num">' + Number(l.gstRate) + '</td>' +
        '<td class="num">' + money(l.taxable) + '</td><td class="num">' + money(l.lineTotal) + '</td></tr>').join('') +
    '</tbody></table>' +
    (m.mismatches.length ? '<h3 class="mir-subtitle">Differences recorded</h3>' + mismatchTableHtml(m.mismatches, false) : '') +
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
    } catch (e) { document.getElementById('cancelErr').textContent = e.message; }
  };
}

function kv(k, v) { return '<div><span class="form-label">' + escapeHtml(k) + '</span><div>' + escapeHtml(v) + '</div></div>'; }

function mismatchTableHtml(list, withMir) {
  return '<table class="mir-table"><thead><tr>' + (withMir ? '<th>MIR</th><th>Plant</th><th>Vendor</th><th>PO</th>' : '') +
    '<th>Line</th><th>Difference</th><th class="num">Expected</th><th class="num">Actual</th><th>Reason</th><th>Status</th>' + (withMir ? '<th></th>' : '') + '</tr></thead><tbody>' +
    list.map(x => '<tr>' +
      (withMir ? '<td><b>' + escapeHtml(x.mirNo) + '</b><div class="mir-muted">' + dateIN(x.mirDate) + '</div></td><td>' + escapeHtml(x.plant.name) + '</td>' +
        '<td>' + escapeHtml(x.vendor.name) + '<div class="mir-muted">Inv ' + escapeHtml(x.invoiceNo) + '</div></td><td>' + escapeHtml(x.poNumber || '-') +
        (x.description ? '<div class="mir-muted">' + escapeHtml(x.description) + '</div>' : '') + '</td>' : '') +
      '<td>' + (x.lineNo || 'invoice') + '</td><td>' + escapeHtml(x.kindLabel) + (x.differencePct ? ' <span class="mir-muted">' + (Number(x.differencePct) > 0 ? '+' : '') + Number(x.differencePct) + '%</span>' : '') + '</td>' +
      '<td class="num">' + figure(x, x.expected) + '</td><td class="num">' + figure(x, x.actual) + '</td>' +
      '<td>' + escapeHtml(x.reasonLabel) + (x.note ? '<div class="mir-muted">' + escapeHtml(x.note) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(x.status) + (x.resolutionNote ? '<div class="mir-muted">' + escapeHtml(x.resolvedBy + ': ' + x.resolutionNote) + '</div>' : '') + '</td>' +
      (withMir ? '<td>' + (x.status === 'OPEN' && canResolve(x) ? '<div class="mir-resolve"><input class="form-control" data-resolve-note="' + x.id + '" placeholder="How was it resolved?" aria-label="Resolution note">' +
        '<button type="button" class="btn btn-navy btn-small" data-resolve="' + x.id + '">Resolve</button></div>' : '') + '</td>' : '') +
    '</tr>').join('') + '</tbody></table>';
}

// A mismatch's expected/actual in its own terms: rupees for a rate or an
// invoice total, a quantity otherwise, a dash for the tax type (no figure).
function figure(x, v) {
  if (v === null || v === undefined) return '-';
  return (x.kind.startsWith('RATE') || x.kind === 'INVOICE_TOTAL') ? money(v) : qty(v);
}

function canResolve(x) {
  return (META.plants.find(p => p.code === x.plant.code) || {}).canReceive;
}

async function loadMismatches() {
  const area = document.getElementById('mismatchArea');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  try {
    const data = await apiMir('/mismatches?status=OPEN');
    if (!data.mismatches.length) { area.innerHTML = '<div class="mir-muted">No open mismatches.</div>'; return; }
    area.innerHTML = mismatchTableHtml(data.mismatches, true);
    area.querySelectorAll('[data-resolve]').forEach(b => { b.onclick = async () => {
      const note = area.querySelector('[data-resolve-note="' + b.dataset.resolve + '"]').value.trim();
      if (!note) { window.alert('Say how it was resolved.'); return; }
      try {
        await apiMir('/mismatches/' + b.dataset.resolve + '/resolve', { method: 'POST', body: { note } });
        loadMismatches();
      } catch (e) { window.alert(e.message); }
    }; });
  } catch (e) {
    area.innerHTML = '<div class="mir-error-text">' + escapeHtml(e.message) + '</div>';
  }
}
