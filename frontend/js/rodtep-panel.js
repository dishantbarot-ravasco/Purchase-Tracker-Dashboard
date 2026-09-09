// ── RoDTEP Ledger panel (added 2026-09-09) ──────────────────────────────────
// Scoped to Import Purchases ONLY, per the project owner's own instruction -
// no Domestic Purchases changes, no new top-level nav tab. RoDTEP scrips are
// a company-wide resource (see apps/core/models.py's RodtepScrollEntry/
// SyncRun.Plant.COMPANY), not tied to any one plant, so this lives as a
// button in Import Purchases' own toolbar (import-po.js) rather than under
// any plant's tab hierarchy.
//
// Reuses the shared #modalBackdrop/#modalBody the PO/Material/Export
// modals already use (same pattern export-panel.js already established -
// see that file's own header comment) rather than a bespoke panel.
//
// GET /api/imports/rodtep is IsAuthenticated-any-role (read); POST
// /api/imports/rodtep/usage is IsEditor-gated server-side - the "Log Usage"
// button is hidden client-side for a viewer as defense-in-depth only, same
// convention canEditField()'s own docstring already describes ("saves a
// wasted round trip ... never the only enforcement").

function formatInrOrDash(value) {
  return (value === null || value === undefined) ? '-' : formatInr(value);
}

async function openRodtepPanel() {
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };

  body.innerHTML = '<div class="modal-head"><div><h2>RoDTEP Scrip Ledger</h2>' +
    '<div class="modal-meta">Loading...</div></div><span class="close-btn">&times;</span></div>';

  let data;
  try {
    data = await apiImports('/rodtep');
  } catch (e) {
    body.innerHTML = '<div class="modal-head"><div><h2>RoDTEP Scrip Ledger</h2></div><span class="close-btn">&times;</span></div>' +
      '<div class="field-block full-width">' + escapeHtml(e.message || 'Failed to load RoDTEP data.') + '</div>';
    return;
  }
  renderRodtepLedgerBody(body, data);
}

function renderRodtepLedgerBody(body, data) {
  const canEdit = CURRENT_USER && (CURRENT_USER.role === 'admin' || CURRENT_USER.role === 'editor');
  const scripts = data.scripts || [];
  const lastSync = data.lastSync;
  const syncNote = lastSync
    ? 'Last synced: ' + formatDateIN(lastSync.finishedAt) + ' (' + lastSync.status + ')'
    : 'Never synced yet.';

  const rowsHtml = scripts.length ? scripts.map(s =>
    '<tr class="row-link" data-script="' + escapeHtml(s.scriptNo) + '">' +
      '<td>' + escapeHtml(s.scriptNo) + '</td>' +
      '<td>' + (s.scriptDate ? formatDateIN(s.scriptDate) : '-') + '</td>' +
      '<td>' + escapeHtml(s.location || '-') + '</td>' +
      '<td>' + s.entryCount + '</td>' +
      '<td>' + formatInrOrDash(s.totalSanctioned) + '</td>' +
      '<td>' + formatInrOrDash(s.totalUsed) + '</td>' +
      '<td><b>' + formatInrOrDash(s.balance) + '</b></td>' +
    '</tr>'
  ).join('') : '<tr><td colspan="7" class="empty-state">No RoDTEP data synced yet.</td></tr>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>RoDTEP Scrip Ledger</h2>' +
    '<div class="modal-meta">' + escapeHtml(syncNote) + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width flex-row-gap10 mb-8">' +
      (canEdit ? '<button class="primary" id="rodtepSyncBtn">Sync Now</button>' : '') +
      (canEdit ? '<button id="rodtepLogUsageBtn">Log Usage</button>' : '') +
    '</div>' +
    '<div class="field-block full-width">' +
      '<table class="items-table"><thead><tr>' +
        '<th>Script No</th><th>Script Date</th><th>Location</th><th>SB Rows</th>' +
        '<th>Total Sanctioned</th><th>Total Used</th><th>Balance</th>' +
      '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' +
    '</div>';

  body.querySelectorAll('tr[data-script]').forEach(tr => tr.onclick = () => openRodtepScriptDetail(tr.dataset.script));

  const syncBtn = document.getElementById('rodtepSyncBtn');
  if (syncBtn) syncBtn.onclick = async () => {
    syncBtn.disabled = true;
    syncBtn.textContent = 'Syncing...';
    try {
      await apiImports('/rodtep/sync-trigger', { method: 'POST', credentials: 'same-origin' });
      const fresh = await apiImports('/rodtep');
      renderRodtepLedgerBody(body, fresh);
    } catch (e) {
      // 409 means the daily scheduled sync (or another admin) is already
      // running one right now (see sync_trigger.py's shared RoDTEP lock) -
      // not a real failure, so don't alarm the user with "Sync failed".
      alert(e.status === 409 ? 'A RoDTEP sync is already running - try again in a moment.' : 'Sync failed: ' + (e.message || 'unknown error'));
      syncBtn.disabled = false;
      syncBtn.textContent = 'Sync Now';
    }
  };

  const logBtn = document.getElementById('rodtepLogUsageBtn');
  if (logBtn) logBtn.onclick = () => openRodtepUsageForm(body, data, null);
}

async function openRodtepScriptDetail(scriptNo) {
  const body = document.getElementById('modalBody');
  body.innerHTML = '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2>' +
    '<div class="modal-meta">Loading...</div></div><span class="close-btn">&times;</span></div>';

  let detail;
  try {
    detail = await apiImports('/rodtep/' + encodeURIComponent(scriptNo));
  } catch (e) {
    body.innerHTML = '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2></div><span class="close-btn">&times;</span></div>' +
      '<div class="field-block full-width">' + escapeHtml(e.message || 'Failed to load.') + '</div>';
    return;
  }

  const canEdit = CURRENT_USER && (CURRENT_USER.role === 'admin' || CURRENT_USER.role === 'editor');
  const entriesHtml = detail.entries.length ? detail.entries.map(e =>
    '<tr><td>' + escapeHtml(e.sbNumber) + '</td><td>' + (e.sbDate ? formatDateIN(e.sbDate) : '-') + '</td>' +
    '<td>' + escapeHtml(e.scrollNumber || '-') + '</td><td>' + formatInrOrDash(e.sanctionedAmount) + '</td></tr>'
  ).join('') : '<tr><td colspan="4" class="empty-state">No Shipping Bill rows synced for this script yet.</td></tr>';

  const usagesHtml = detail.usages.length ? detail.usages.map(u =>
    '<tr><td>' + formatInrOrDash(u.usedAmount) + '</td>' +
    '<td>' + escapeHtml(u.boeNumber || '-') + (u.boeNumber ? (u.boeVerified ? ' <span class="badge-verified">verified</span>' : ' <span class="badge-warn">not found</span>') : '') + '</td>' +
    '<td>' + escapeHtml(u.importPoNumber || '-') + '</td>' +
    '<td>' + (u.usedDate ? formatDateIN(u.usedDate) : '-') + '</td>' +
    '<td>' + escapeHtml(u.notes || '-') + '</td></tr>'
  ).join('') : '<tr><td colspan="5" class="empty-state">No usage logged against this script yet.</td></tr>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2>' +
    '<div class="modal-meta">RoDTEP scrip detail</div></div><span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width"><h4>Shipping Bill Credits</h4>' +
      '<table class="items-table"><thead><tr><th>SB Number</th><th>SB Date</th><th>Scroll Number</th><th>Sanctioned</th></tr></thead>' +
      '<tbody>' + entriesHtml + '</tbody></table>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Usage Against Imports' +
      (canEdit ? ' <button id="rodtepLogUsageBtn2" class="row-link">+ Log Usage</button>' : '') + '</h4>' +
      '<table class="items-table"><thead><tr><th>Amount Used</th><th>BOE Number</th><th>Import PO</th><th>Date</th><th>Notes</th></tr></thead>' +
      '<tbody>' + usagesHtml + '</tbody></table>' +
    '</div>';

  const logBtn2 = document.getElementById('rodtepLogUsageBtn2');
  if (logBtn2) logBtn2.onclick = () => openRodtepUsageForm(body, null, scriptNo);
}

function openRodtepUsageForm(body, ledgerData, presetScriptNo) {
  body.innerHTML =
    '<div class="modal-head"><div><h2>Log RoDTEP Usage</h2>' +
    '<div class="modal-meta">Record credit used against a specific import</div></div><span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width">' +
      '<label>Script Number</label><input type="text" id="rodtepUsageScript" value="' + escapeHtml(presetScriptNo || '') + '">' +
      '<label class="mt-8">Amount Used (INR)</label><input type="number" step="0.01" id="rodtepUsageAmount">' +
      '<label class="mt-8">BOE Number</label><input type="text" id="rodtepUsageBoe" placeholder="Verified against Import Purchases if it matches a real BOE">' +
      '<label class="mt-8">Import PO Number (optional)</label><input type="text" id="rodtepUsagePo">' +
      '<label class="mt-8">Date Used</label><input type="date" id="rodtepUsageDate">' +
      '<label class="mt-8">Notes (optional)</label><textarea id="rodtepUsageNotes" rows="2"></textarea>' +
      '<div class="mt-14"><button class="primary" id="rodtepUsageSave">Save</button> <button id="rodtepUsageCancel">Cancel</button></div>' +
    '</div>';

  document.getElementById('rodtepUsageCancel').onclick = () => {
    if (presetScriptNo) openRodtepScriptDetail(presetScriptNo);
    else openRodtepPanel();
  };

  document.getElementById('rodtepUsageSave').onclick = async () => {
    const scriptNo = document.getElementById('rodtepUsageScript').value.trim();
    const amount = document.getElementById('rodtepUsageAmount').value;
    if (!scriptNo || !amount) { alert('Script Number and Amount Used are required.'); return; }
    const payload = {
      scriptNo: scriptNo,
      usedAmount: amount,
      boeNumber: document.getElementById('rodtepUsageBoe').value.trim(),
      importPoNumber: document.getElementById('rodtepUsagePo').value.trim(),
      usedDate: document.getElementById('rodtepUsageDate').value || null,
      notes: document.getElementById('rodtepUsageNotes').value.trim(),
    };
    try {
      await apiImports('/rodtep/usage', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      openRodtepScriptDetail(scriptNo);
    } catch (e) {
      alert('Could not save: ' + (e.message || 'unknown error'));
    }
  };
}
