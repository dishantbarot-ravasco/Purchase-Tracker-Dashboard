// po-files.html's page script - see po-files.html's header comment. Uses
// /api/mir/meta for the plant list (canReceive is the same rule as
// uploading: Editor or Admin at that plant) and doc-files.js for uploading
// and opening files.

let PO_FILES_META = null;
let poFilesRows = [];

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'pofiles');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
  try {
    PO_FILES_META = await poFilesApi('/api/mir/meta');
  } catch (e) {
    poFilesListMessage(e.message);
    return;
  }
  const readable = PO_FILES_META.plants.filter(p => p.canRead);
  const uploadable = PO_FILES_META.plants.filter(p => p.canReceive);
  document.getElementById('listPlant').innerHTML = '<option value="">All plants</option>' +
    readable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
  if (uploadable.length) {
    document.getElementById('upPlant').innerHTML = (uploadable.length > 1 ? '<option value="">Pick a plant</option>' : '') +
      uploadable.map(p => '<option value="' + escapeHtml(p.code) + '">' + escapeHtml(p.name) + '</option>').join('');
    document.getElementById('uploadPanel').hidden = false;
    document.getElementById('uploadForm').addEventListener('submit', ev => { ev.preventDefault(); poFilesUpload(); });
  }
  let t;
  const reload = () => { clearTimeout(t); t = setTimeout(poFilesLoad, 250); };
  document.getElementById('listPlant').addEventListener('change', reload);
  document.getElementById('listSearch').addEventListener('input', reload);
  document.getElementById('listStatus').addEventListener('change', poFilesRender);
  poFilesLoad();
})();

async function poFilesApi(url, opts) {
  const o = Object.assign({ credentials: 'same-origin' }, opts || {});
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
  }
  const res = await authFetch(url, o);
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  let data = {};
  try { data = await res.json(); } catch (e) { throw new Error('The server sent an unexpected response. Please try again.'); }
  if (!res.ok) throw new Error(data.error || data.detail || 'Something went wrong. Please try again.');
  return data;
}

function poFilesListMessage(text) {
  const area = document.getElementById('listArea');
  area.innerHTML = '';
  const d = document.createElement('div');
  d.className = 'mir-muted';
  d.textContent = text;
  area.appendChild(d);
}

async function poFilesUpload() {
  const err = document.getElementById('upErr');
  const ok = document.getElementById('upOk');
  const plant = document.getElementById('upPlant').value;
  const po = document.getElementById('upPo').value.trim();
  const file = document.getElementById('upFile').files[0];
  err.textContent = ''; ok.textContent = '';
  if (!plant) { err.textContent = 'Pick the plant.'; return; }
  if (!po) { err.textContent = 'Enter the PO number.'; return; }
  const problem = docFileProblem(file);
  if (problem) { err.textContent = problem; return; }
  const btn = document.getElementById('upBtn');
  btn.disabled = true;
  try {
    const fd = new FormData();
    fd.append('plant', plant);
    fd.append('poNumber', po);
    fd.append('note', document.getElementById('upNote').value.trim());
    fd.append('file', file);
    const doc = await docFileUpload('/api/documents/po/upload', fd);
    ok.textContent = 'PO ' + doc.poNumber + ' uploaded as revision ' + doc.revision + '.';
    document.getElementById('upPo').value = '';
    document.getElementById('upNote').value = '';
    document.getElementById('upFile').value = '';
    poFilesLoad();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function poFilesLoad() {
  const params = new URLSearchParams();
  const plant = document.getElementById('listPlant').value;
  const q = document.getElementById('listSearch').value.trim();
  if (plant) params.set('plant', plant);
  if (q) params.set('q', q);
  try {
    const data = await poFilesApi('/api/documents/po?' + params.toString());
    poFilesRows = data.documents;
    document.getElementById('storageBanner').hidden = data.storageReady;
    poFilesRender();
  } catch (e) {
    poFilesListMessage(e.message);
  }
}

function poFilesCanWrite(plantCode) {
  const p = PO_FILES_META.plants.find(x => x.code === plantCode);
  return !!(p && p.canReceive);
}

function poFilesRender() {
  const wanted = document.getElementById('listStatus').value;
  const rows = poFilesRows.filter(d => !wanted || d.status === wanted);
  const area = document.getElementById('listArea');
  if (!rows.length) { poFilesListMessage(poFilesRows.length ? 'No files match this view.' : 'No PO files uploaded yet.'); return; }
  area.innerHTML = '<div class="table-wrap"><table><thead><tr><th>PO number</th><th>Plant</th><th class="num">Rev</th><th>Status</th>' +
    '<th>File</th><th>Uploaded</th><th>In the app</th><th></th></tr></thead><tbody>' +
    rows.map(d => '<tr><td><b>' + escapeHtml(d.poNumber) + '</b>' + (d.note ? '<div class="mir-muted">' + escapeHtml(d.note) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(poFilesPlantName(d.plant)) + '</td><td class="num">' + d.revision + '</td>' +
      '<td>' + docFileStatusPill(d.status) + (d.withdrawReason ? '<div class="mir-muted">' + escapeHtml(d.withdrawReason) + '</div>' : '') + '</td>' +
      '<td><button type="button" class="mir-link" data-doc-open="' + d.id + '">Open</button><div class="mir-muted">' +
        escapeHtml(d.fileName) + ', ' + docFileSize(d.sizeBytes) + '</div></td>' +
      '<td>' + escapeHtml(d.uploadedBy) + '<div class="mir-muted">' + escapeHtml(new Date(d.uploadedAt).toLocaleString('en-IN')) + '</div></td>' +
      '<td>' + (d.poInSystem ? 'Yes' : '<span class="mir-muted" title="No PO with this number at this plant yet">Not yet</span>') + '</td>' +
      '<td>' + (d.status !== 'WITHDRAWN' && poFilesCanWrite(d.plant) ? '<button type="button" class="mir-link" data-withdraw="' + d.id + '">Withdraw</button>' : '') + '</td></tr>' +
      '<tr class="mir-action-row" data-withdraw-row="' + d.id + '" hidden><td colspan="8"></td></tr>').join('') +
    '</tbody></table></div>';
  docFileBindOpen(area);
  area.querySelectorAll('[data-withdraw]').forEach(b => { b.onclick = () => poFilesOpenWithdraw(Number(b.dataset.withdraw)); });
}

function poFilesPlantName(code) {
  const p = PO_FILES_META.plants.find(x => x.code === code);
  return p ? p.name : code;
}

function poFilesOpenWithdraw(id) {
  const row = document.querySelector('[data-withdraw-row="' + id + '"]');
  row.hidden = !row.hidden;
  if (row.hidden) return;
  const cell = row.querySelector('td');
  cell.innerHTML = '<div class="doc-file-upload"><label class="form-label" for="wdReason' + id + '">Why is this file withdrawn?</label>' +
    '<input class="form-control" id="wdReason' + id + '" maxlength="500" placeholder="e.g. PO cancelled, uploaded under the wrong number">' +
    '<button type="button" class="btn btn-navy btn-small" data-confirm>Withdraw</button>' +
    '<span class="mir-error-text" role="alert"></span></div>';
  const input = cell.querySelector('input');
  input.focus();
  cell.querySelector('[data-confirm]').onclick = async () => {
    const reason = input.value.trim();
    const err = cell.querySelector('.mir-error-text');
    if (!reason) { err.textContent = 'Say why.'; return; }
    try {
      await poFilesApi('/api/documents/' + id + '/withdraw', { method: 'POST', body: { reason } });
      poFilesLoad();
    } catch (e) { err.textContent = e.message; }
  };
}
