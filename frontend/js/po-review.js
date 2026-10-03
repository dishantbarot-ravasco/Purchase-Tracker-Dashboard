// po-files.html's "PO readings" panel (owner, 2026-10-03): an uploaded PO
// copy is read by the extraction model on the background worker
// (apps/services/po_extraction.py) into a draft; a purchase manager checks
// every field here, corrects what is wrong, and approves it into the
// purchase orders MIR entry receives against - or rejects it with a reason.
// The page never decides anything itself: what blocks approval (`problems`),
// what does not add up (`checks`) and how the draft differs from the PO
// sheet (`sheet`) all come from the server. Every top-level name is pr...
// (one global scope with auth.js, shared.js, doc-files.js, po-files-page.js).

let PR_ROWS = [];
let PR_POLL = null;
let PR_OPEN = null; // the reading shown in #reviewArea

const PR_STATUS_TONE = { QUEUED: 'muted', RUNNING: 'muted', READY: 'warn', FAILED: 'bad', APPROVED: 'ok', REJECTED: 'muted' };

function prCanReview() {
  return !!(PO_FILES_META && PO_FILES_META.can && PO_FILES_META.can.poUpload);
}

async function prLoad() {
  if (!prCanReview()) return;
  const panel = document.getElementById('readingsPanel');
  panel.hidden = false;
  let data;
  try {
    data = await poFilesApi('/api/po-extractions?' + new URLSearchParams(document.getElementById('listPlant').value ? { plant: document.getElementById('listPlant').value } : {}).toString());
  } catch (e) {
    document.getElementById('readingsArea').textContent = e.message;
    return;
  }
  PR_ROWS = data.extractions;
  document.getElementById('readingsOff').hidden = data.configured;
  prRender();
  // Keep watching while the worker still has some to read.
  clearTimeout(PR_POLL);
  if (PR_ROWS.some(r => r.status === 'QUEUED' || r.status === 'RUNNING')) PR_POLL = setTimeout(prLoad, 5000);
}

function prPill(row) {
  return '<span class="status-pill mir-pill-' + (PR_STATUS_TONE[row.status] || 'muted') + '">' + escapeHtml(row.statusLabel) + '</span>';
}

function prRender() {
  const area = document.getElementById('readingsArea');
  const wanted = document.getElementById('readingsShow').value;
  const rows = PR_ROWS.filter(r => !wanted || (wanted === 'OPEN' ? ['QUEUED', 'RUNNING', 'READY', 'FAILED'].includes(r.status) : r.status === wanted));
  if (!rows.length) {
    area.innerHTML = '<div class="mir-muted">' + (PR_ROWS.length ? 'No readings in this view.' : 'No PO copies have been read yet. Upload a PO copy and it is read here.') + '</div>';
    return;
  }
  area.innerHTML = '<div class="table-wrap"><table><thead><tr><th>PO number</th><th>Plant</th><th>File</th><th>Status</th><th>Read</th><th>Decided</th><th></th></tr></thead><tbody>' +
    rows.map(r => '<tr><td><b>' + escapeHtml(r.poNumber) + '</b></td><td>' + escapeHtml(poFilesPlantName(r.plant)) + '</td>' +
      '<td>' + escapeHtml(r.fileName) + ' <span class="mir-muted">rev ' + r.revision + '</span></td>' +
      '<td>' + prPill(r) + (r.error ? '<div class="mir-error-text">' + escapeHtml(r.error) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(r.requestedBy) + '<div class="mir-muted">' + escapeHtml(new Date(r.createdAt).toLocaleString('en-IN')) + '</div></td>' +
      '<td>' + (r.reviewedBy ? escapeHtml(r.reviewedBy) + (r.reviewNote ? '<div class="mir-muted">' + escapeHtml(r.reviewNote) + '</div>' : '') : '') + '</td>' +
      '<td>' + (r.status === 'READY' ? '<button type="button" class="btn btn-navy btn-small" data-review="' + r.id + '">Review</button>'
        : r.status === 'APPROVED' || r.status === 'REJECTED' ? '<button type="button" class="mir-link" data-review="' + r.id + '">View</button>' : '') + '</td></tr>').join('') +
    '</tbody></table></div>';
  area.querySelectorAll('[data-review]').forEach(b => { b.onclick = () => prOpen(Number(b.dataset.review)); });
}

/** Ask for a PO copy to be read (again) - po-files-page.js's list calls it. */
async function prRead(documentId, btn) {
  btn.disabled = true;
  try {
    await poFilesApi('/api/documents/' + documentId + '/extract', { method: 'POST', body: {} });
    prLoad();
    document.getElementById('readingsPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    btn.disabled = false;
    alert(e.message);
  }
}

async function prOpen(id) {
  const area = document.getElementById('reviewArea');
  area.innerHTML = '<div class="mir-muted">Loading...</div>';
  let d;
  try { d = await poFilesApi('/api/po-extractions/' + id); } catch (e) { area.textContent = e.message; return; }
  PR_OPEN = d;
  prPaint();
  area.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function prInput(attrs, value, label) {
  return '<input class="form-control" ' + attrs + ' value="' + escapeHtml(value || '') + '" aria-label="' + escapeHtml(label) + '">';
}

function prPaint() {
  const d = PR_OPEN;
  const area = document.getElementById('reviewArea');
  const editable = d.status === 'READY';
  const draft = d.draft || {};
  const ro = editable ? '' : ' readonly';
  const required = f => !['remarks', 'tax_amount', 'item_code'].includes(f);
  const lineReq = f => ['description', 'hsn', 'qty', 'uom', 'rate', 'net_value', 'delivery_date'].includes(f);
  const problemFields = new Set((d.problems || []).filter(p => !p.line).map(p => p.field));
  const wide = f => ['vendor_address', 'billing_address', 'shipping_address', 'remarks'].includes(f);
  const header = d.headerFields.map(f => '<div class="form-group' + (wide(f) ? ' pr-wide' : '') + (problemFields.has(f) ? ' pr-bad' : '') + '">' +
    '<label class="form-label" for="pr-' + f + '">' + escapeHtml(d.labels[f]) + (required(f) ? ' <span class="req-mark">*</span>' : '') + '</label>' +
    prInput('id="pr-' + f + '" data-hf="' + f + '"' + ro, draft[f], d.labels[f]) + '</div>').join('');
  const lines = draft.lines || [];
  const lineRows = lines.map((ln, i) => '<tr>' + '<td class="num">' + (i + 1) + '</td>' +
    d.lineFields.map(f => '<td class="pr-cell-' + f + '">' + prInput('data-li="' + i + '" data-lf="' + f + '"' + ro, ln[f], 'Line ' + (i + 1) + ' ' + d.labels[f]) + '</td>').join('') +
    (editable ? '<td><button type="button" class="mir-link" data-drop-line="' + i + '">Remove</button></td>' : '') + '</tr>').join('');
  const list = (items, cls, title) => items.length ? '<div class="' + cls + '" role="note"><b>' + escapeHtml(title) + '</b><ul>' +
    items.map(p => '<li>' + escapeHtml(p.message) + '</li>').join('') + '</ul></div>' : '';
  const sheet = d.sheet;
  const sheetHtml = !sheet ? '<p class="mir-muted">The PO sheet does not have this PO number at this plant - approving adds it.</p>'
    : sheet.source === 'app' ? '<p class="mir-muted">This PO is already in the app (approved earlier) - approving replaces it with this reading.</p>'
    : '<h4 class="mir-subtitle">PO sheet vs this file</h4>' + (sheet.differences.length
      ? '<div class="table-wrap"><table><thead><tr><th>What</th><th>PO sheet</th><th>This file</th></tr></thead><tbody>' +
        sheet.differences.map(x => '<tr><td>' + escapeHtml(x.field) + '</td><td>' + escapeHtml(x.sheet || '-') + '</td><td><b>' + escapeHtml(x.pdf || '-') + '</b></td></tr>').join('') + '</tbody></table></div>' +
        '<p class="mir-hint">Approving makes this file the PO\'s record: the PO sheet stops updating it.</p>'
      : '<p class="mir-muted">The PO sheet agrees with this file. Approving makes this file the PO\'s record.</p>');
  area.innerHTML = '<section class="mir-panel">' +
    '<div class="mir-detail-head"><h3 class="mir-panel-title">PO ' + escapeHtml(d.poNumber) + ' <span class="mir-muted">' + escapeHtml(poFilesPlantName(d.plant)) + '</span></h3>' + prPill(d) +
      '<button type="button" class="mir-link" data-doc-open="' + d.documentId + '">Open the file</button></div>' +
    (draft.notes ? '<div class="mir-banner mir-banner-warn">Reader\'s note: ' + escapeHtml(draft.notes) + '</div>' : '') +
    (editable ? list(d.problems || [], 'pr-problems', 'Fix before approving:') : '') +
    list(d.checks || [], 'mir-po-checks', 'Check with the PO:') +
    '<h4 class="mir-subtitle">Order</h4><div class="mir-grid pr-grid">' + header + '</div>' +
    '<h4 class="mir-subtitle">Lines</h4><div class="table-wrap pr-lines"><table><thead><tr><th>#</th>' +
      d.lineFields.map(f => '<th>' + escapeHtml(d.labels[f]) + (lineReq(f) ? ' <span class="req-mark">*</span>' : '') + '</th>').join('') + (editable ? '<th></th>' : '') +
    '</tr></thead><tbody>' + lineRows + '</tbody></table></div>' +
    (editable ? '<button type="button" class="mir-link" id="prAddLine">+ Add a line</button>' : '') +
    sheetHtml +
    (editable ? '<div class="mir-actions pr-actions">' +
      '<button type="button" class="btn btn-primary" id="prApprove">Approve into purchase orders</button>' +
      '<button type="button" class="btn btn-navy btn-small" id="prSave">Save corrections</button>' +
      '<input class="form-control pr-reject-note" id="prRejectNote" maxlength="500" placeholder="Why reject? e.g. wrong file" aria-label="Why reject this reading">' +
      '<button type="button" class="btn btn-small" id="prReject">Reject</button>' +
      '<span class="mir-error-text" id="prErr" role="alert"></span></div>' : '') +
    '</section>';
  docFileBindOpen(area);
  if (!editable) return;
  area.querySelectorAll('[data-drop-line]').forEach(b => { b.onclick = () => { PR_OPEN.draft = prCollect(); PR_OPEN.draft.lines.splice(Number(b.dataset.dropLine), 1); prPaint(); }; });
  document.getElementById('prAddLine').onclick = () => {
    PR_OPEN.draft = prCollect();
    PR_OPEN.draft.lines.push(Object.fromEntries(d.lineFields.map(f => [f, ''])));
    prPaint();
  };
  document.getElementById('prSave').onclick = () => prSend('draft', { draft: prCollect() }, 'Corrections saved.');
  document.getElementById('prApprove').onclick = () => prSend('approve', { draft: prCollect() }, null);
  document.getElementById('prReject').onclick = () => {
    const note = document.getElementById('prRejectNote').value.trim();
    if (!note) { document.getElementById('prErr').textContent = 'Say why it is rejected.'; return; }
    prSend('reject', { note }, null);
  };
}

/** The draft as the form now stands. */
function prCollect() {
  const area = document.getElementById('reviewArea');
  const draft = Object.assign({}, PR_OPEN.draft || {});
  area.querySelectorAll('[data-hf]').forEach(i => { draft[i.dataset.hf] = i.value; });
  const lines = [];
  area.querySelectorAll('[data-lf]').forEach(i => {
    const n = Number(i.dataset.li);
    lines[n] = lines[n] || {};
    lines[n][i.dataset.lf] = i.value;
  });
  draft.lines = lines.filter(Boolean);
  return draft;
}

// Approve saves the corrections first, so the server checks exactly what is
// on screen; any problem it finds is painted in place and nothing is written.
async function prSend(action, body, okText) {
  const err = document.getElementById('prErr');
  err.textContent = '';
  document.querySelectorAll('.pr-actions button').forEach(b => { b.disabled = true; });
  try {
    if (action === 'draft' || action === 'approve') {
      PR_OPEN = await poFilesApi('/api/po-extractions/' + PR_OPEN.id + '/draft', { method: 'POST', body: { draft: body.draft } });
      if (action === 'draft' || PR_OPEN.problems.length) {
        prPaint();
        document.getElementById('prErr').textContent = action === 'draft' ? okText : 'Fix what is listed above, then approve.';
        return;
      }
    }
    await poFilesApi('/api/po-extractions/' + PR_OPEN.id + '/' + action, { method: 'POST', body: action === 'reject' ? body : {} });
    await prOpen(PR_OPEN.id);
    prLoad();
    poFilesLoad();
  } catch (e) {
    document.querySelectorAll('.pr-actions button').forEach(b => { b.disabled = false; });
    const e2 = document.getElementById('prErr');
    if (e2) e2.textContent = e.message;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const show = document.getElementById('readingsShow');
  if (show) show.addEventListener('change', prRender);
});
