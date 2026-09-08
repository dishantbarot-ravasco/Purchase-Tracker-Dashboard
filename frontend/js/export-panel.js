// ── Data Export panel ────────────────────────────────────────────────────
// Added 2026-09-08 (project owner request, alongside "Refresh Data") - lets
// an Editor/Admin download the full daily RM stock snapshot history as a
// CSV, optionally narrowed to a date range. Deliberately scoped to ONLY the
// RM snapshot data (apps/api/routers/_domestic_base.py's
// make_export_stock_snapshots()) - the project owner explicitly asked for
// this dataset specifically ("raw material stock only the date wise daily
// data as there isn't any way to store that in excel yet") since the source
// Stock xlsx files only ever hold today's position; the daily-snapshot table
// this reads from is the ONLY place that day-by-day history exists at all.
// Not Purchase Orders/Import Purchases - those are already fully present in
// their own source CSVs, re-exporting them here would just be a copy of
// data the project owner already has.
//
// Reuses the shared #modalBackdrop/#modalBody the PO/Material modals already
// use (same close-button/backdrop-click wiring, no new CSS needed) rather
// than a bespoke panel - see charts.js's closeModal()/delegated close-btn
// listener.
//
// Download mechanism: a plain same-origin GET via window.open(), not a
// fetch()+blob dance - this app's auth is httpOnly-cookie-based for browser
// clients (see CLAUDE.md's "Auth & security architecture"), so the cookie
// rides along on a normal navigation with no extra JS needed; the backend's
// Content-Disposition: attachment header does the rest. Opened in a new tab
// (not the current one) so a same-tab navigation failure (e.g. a stale
// session) can't blow away the whole SPA - it would just show a JSON error
// in that new tab instead, which the user can close.

function exportPlantRowHtml(key) {
  const allowed = canEditField(key);
  const label = PLANTS[key].label;
  if (!allowed) {
    return '<div class="field-block mb-8"><b>' + escapeHtml(label) + '</b>' +
      '<span class="mt-4 fs-11-5 text-slate-soft"> - you don\'t have export access for this plant.</span></div>';
  }
  return '<div class="field-block mb-8 flex-row-gap10">' +
    '<b>' + escapeHtml(label) + '</b>' +
    '<button type="button" class="row-link" data-export-plant="' + key + '">Download CSV</button>' +
    '</div>';
}

function openExportPanel() {
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };

  body.innerHTML =
    '<div class="modal-head"><div><h2>Export Data</h2>' +
    '<div class="modal-meta">Raw Material daily stock snapshot history (CSV)</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width"><h4>Date Range (optional)</h4>' +
      '<div class="filter-group">' +
        '<label>From</label><input type="date" id="exportFromDate">' +
        '<span class="sep-gray">to</span>' +
        '<input type="date" id="exportToDate">' +
      '</div>' +
      '<div class="mt-8 fs-12 text-slate-soft">Leave both blank to download everything captured so far - snapshot capture began 2026-09-02 (HRS) / 2026-09-03 (RTP-Achhad, RTP-Vapi), see the sync-status badges for the latest date.</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Plant</h4>' +
      PLANT_KEYS.map(exportPlantRowHtml).join('') +
    '</div>';

  body.querySelectorAll('[data-export-plant]').forEach(btn => btn.onclick = () => {
    const key = btn.dataset.exportPlant;
    const from = document.getElementById('exportFromDate').value;
    const to = document.getElementById('exportToDate').value;
    const params = [];
    if (from) params.push('from=' + encodeURIComponent(from));
    if (to) params.push('to=' + encodeURIComponent(to));
    const qs = params.length ? '?' + params.join('&') : '';
    window.open(PLANTS[key].apiPrefix + '/stock-snapshots/export' + qs, '_blank');
  });
}
