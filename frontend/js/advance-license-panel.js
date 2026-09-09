// ── Advance License panel (added 2026-09-09) ────────────────────────────────
// Scoped to Import Purchases ONLY, per the project owner's own instruction -
// no Domestic Purchases changes, no new top-level nav tab. Same reasoning
// and same shared-modal pattern as rodtep-panel.js (Advance Licenses are a
// company-wide resource too - see apps/core/models.py's AdvanceLicense).
//
// Unlike RoDTEP, there is no manual "Log Usage" form here - the source
// workbook the project owner maintains by hand already carries the BOE/
// import-PO/qty/value usage columns per material, synced as-is. This panel
// is read-only, showing exactly the fields the project owner asked to see:
// License Number, Export Product Description, CIF Value Authorized, FOB
// Export Target, Export Validity, and the license's input Material
// Descriptions.

function formatInrOrDashAL(value) {
  return (value === null || value === undefined) ? '-' : formatInr(value);
}

async function openAdvanceLicensePanel() {
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };

  body.innerHTML = '<div class="modal-head"><div><h2>Advance License Ledger</h2>' +
    '<div class="modal-meta">Loading...</div></div><span class="close-btn">&times;</span></div>';

  let data;
  try {
    data = await apiImports('/advance-license');
  } catch (e) {
    body.innerHTML = '<div class="modal-head"><div><h2>Advance License Ledger</h2></div><span class="close-btn">&times;</span></div>' +
      '<div class="field-block full-width">' + escapeHtml(e.message || 'Failed to load Advance License data.') + '</div>';
    return;
  }
  renderAdvanceLicenseLedgerBody(body, data);
}

function renderAdvanceLicenseLedgerBody(body, data) {
  const canSync = CURRENT_USER && CURRENT_USER.role === 'admin';
  const licenses = data.licenses || [];
  const lastSync = data.lastSync;
  const failedTitle = lastSync && lastSync.status !== 'success' && lastSync.errorDetail
    ? ' title="' + escapeHtml(lastSync.errorDetail) + '"' : '';
  const syncNote = lastSync
    ? '<span' + failedTitle + '>Last synced: ' + formatDateIN(lastSync.finishedAt) + ' (' + escapeHtml(lastSync.status) + ')</span>'
    : 'Never synced yet.';

  const today = new Date().toISOString().slice(0, 10);

  const rowsHtml = licenses.length ? licenses.map(lic => {
    const materialNames = (lic.materials || [])
      .map(m => m.materialDescription)
      .filter((v, i, arr) => v && arr.indexOf(v) === i);
    const expiringSoon = lic.exportValidityDate && lic.exportValidityDate < today;
    return (
      '<tr>' +
        '<td>' + escapeHtml(lic.licenseNumber) + '</td>' +
        '<td>' + escapeHtml(lic.exportProductDescription || '-') + '</td>' +
        '<td>' + formatInrOrDashAL(lic.cifValueAuthorized) + '</td>' +
        '<td>' + formatInrOrDashAL(lic.fobValueExportTarget) + '</td>' +
        '<td' + (expiringSoon ? ' class="badge-warn"' : '') + '>' +
          (lic.exportValidityDate ? formatDateIN(lic.exportValidityDate) : '-') +
        '</td>' +
        '<td>' + escapeHtml(materialNames.join(', ') || '-') + '</td>' +
      '</tr>'
    );
  }).join('') : '<tr><td colspan="6" class="empty-state">No Advance License data synced yet.</td></tr>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>Advance License Ledger</h2>' +
    '<div class="modal-meta">' + syncNote + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width flex-row-gap10 mb-8">' +
      (canSync ? '<button class="primary" id="advanceLicenseSyncBtn">Sync Now</button>' : '') +
    '</div>' +
    '<div class="field-block full-width">' +
      '<table class="items-table"><thead><tr>' +
        '<th>License Number</th><th>Export Product Description</th>' +
        '<th>CIF Value Authorized</th><th>FOB Export Target</th>' +
        '<th>Export Validity</th><th>Material Description(s)</th>' +
      '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' +
    '</div>';

  const syncBtn = document.getElementById('advanceLicenseSyncBtn');
  if (syncBtn) syncBtn.onclick = async () => {
    syncBtn.disabled = true;
    syncBtn.textContent = 'Syncing...';
    try {
      await apiImports('/advance-license/sync-trigger', { method: 'POST', credentials: 'same-origin' });
      const fresh = await apiImports('/advance-license');
      renderAdvanceLicenseLedgerBody(body, fresh);
    } catch (e) {
      // 409 means the daily scheduled sync (or another admin) is already
      // running one right now - not a real failure, same convention as
      // rodtep-panel.js's own sync button.
      alert(e.status === 409 ? 'An Advance License sync is already running - try again in a moment.' : 'Sync failed: ' + (e.message || 'unknown error'));
      syncBtn.disabled = false;
      syncBtn.textContent = 'Sync Now';
    }
  };
}
