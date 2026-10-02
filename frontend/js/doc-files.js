// Uploaded PO and invoice files (2026-09-30) - shared by po-files.html and
// mir.html. The files live in Cloudflare R2; /api/documents/<id>/open
// redirects to a five-minute link, so nothing here
// ever holds a permanent file URL. Every name is prefixed `docFile` - all
// page scripts share one global scope.

const DOC_FILE_ACCEPT = 'application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png';
const DOC_FILE_MAX_MB = 20;
const DOC_FILE_STATUS = {
  CURRENT: { label: 'Current', tone: 'ok' },
  SUPERSEDED: { label: 'Older revision', tone: 'muted' },
  WITHDRAWN: { label: 'Withdrawn', tone: 'bad' },
};

/** POST a FormData upload; returns the parsed JSON or throws with the
    server's message. No Content-Type header: the browser sets the
    multipart boundary itself. */
async function docFileUpload(url, formData) {
  const res = await authFetch(url, { method: 'POST', credentials: 'same-origin', body: formData });
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
  if (!res.ok) throw new Error(data.error || data.detail || 'The upload failed. Please try again.');
  return data;
}

/** Checks the browser can make before sending: size and type. Returns an
    error message, or '' when the file may go. The server checks again, by
    the file's content. `allowExcel` admits an .xlsx workbook (the license
    kinds on po-files.html). */
function docFileProblem(file, allowExcel) {
  if (!file) return 'Choose a file.';
  if (file.size === 0) return 'The file is empty.';
  if (file.size > DOC_FILE_MAX_MB * 1024 * 1024) return 'The file is larger than ' + DOC_FILE_MAX_MB + ' MB.';
  if (allowExcel) {
    if (/\.(xls|xlsm|csv)$/i.test(file.name)) return 'Save the sheet as .xlsx (Google Sheets: File, Download, .xlsx) and upload that.';
    if (!/\.(pdf|jpe?g|png|xlsx)$/i.test(file.name)) return 'Upload a PDF, JPEG, PNG or Excel (.xlsx) file.';
    return '';
  }
  if (!/\.(pdf|jpe?g|png)$/i.test(file.name)) return 'Upload a PDF, JPEG or PNG file.';
  return '';
}

/** Open a stored file in a new tab. The server redirects to a short-lived
    storage link (document_views.open_document()); opening the app's own URL
    directly keeps the pop-up blocker out of it. */
function docFileOpen(id) {
  window.open('/api/documents/' + encodeURIComponent(id) + '/open', '_blank', 'noopener');
}

function docFileSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  return Math.max(1, Math.round(bytes / 1024)) + ' KB';
}

function docFileStatusPill(status) {
  const s = DOC_FILE_STATUS[status] || { label: status, tone: 'muted' };
  return '<span class="status-pill mir-pill-' + s.tone + '">' + escapeHtml(s.label) + '</span>';
}

/** One file as a line: Open link, revision, status, name, who and when. */
function docFileLineHtml(f) {
  return '<div class="doc-file-line">' +
    '<button type="button" class="mir-link" data-doc-open="' + f.id + '">Open</button> ' +
    '<span>Rev ' + f.revision + '</span> ' + docFileStatusPill(f.status) + ' ' +
    '<span class="mir-muted">' + escapeHtml(f.fileName) + ' (' + docFileSize(f.sizeBytes) + '), ' +
      escapeHtml(f.uploadedBy) + ', ' + escapeHtml(new Date(f.uploadedAt).toLocaleString('en-IN')) + '</span>' +
    (f.withdrawReason ? '<div class="mir-muted">Withdrawn: ' + escapeHtml(f.withdrawReason) + '</div>' : '') +
  '</div>';
}

/** Wire every [data-doc-open] button inside `root`. */
function docFileBindOpen(root) {
  root.querySelectorAll('[data-doc-open]').forEach(b => { b.onclick = () => docFileOpen(b.dataset.docOpen); });
}
