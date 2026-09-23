// ── RoDTEP Ledger panel (added 2026-09-09) ──────────────────────────────────
// Scoped to Import Purchases ONLY, per the project owner's own instruction -
// no Domestic Purchases changes, no new top-level nav tab. RoDTEP scrips are
// a company-wide resource (see apps/core/models/ledgers.py's RodtepScrollEntry/
// SyncRun.Plant.COMPANY), not tied to any one plant, so this lives as a
// button in Import Purchases' own toolbar (import-po.js) rather than under
// any plant's tab hierarchy.
//
// Reuses the shared #modalBackdrop/#modalBody the PO/Material/Export
// modals already use (same pattern export-panel.js already established -
// see that file's own header comment) rather than a bespoke panel, and
// existing CSS only: .items-table, .field-block, .modal-tabs/.modal-tab,
// .badge-*, .empty-state, .no-data-note.
//
// ── Read-only since 2026-09-22 ─────────────────────────────────────────────
// Both the "Sync Now" and the "Log Usage" buttons are gone (project owner:
// "remove log usage and sync now from both the license tabs and make them
// sync simultaneously like we have for csv's and refresh data").
//
//   Sync Now  was redundant. The dashboard's own "Refresh Data" button has
//             triggered both company-wide ledger syncs since 2026-09-10 and
//             waits on their in-progress flags before finishing, so this
//             panel's copy was a second, differently-labelled way to do the
//             same thing - and the only one a reader could mistake for the
//             ONLY way.
//   Log Usage recorded, by hand, which import a scrip's credit was spent
//             against. That link turns out to be in the imports master CSV
//             already, under its `License Type`/`License Number` columns -
//             an exact join, 100% of cited scrips resolving to a real
//             ledger row. See apps/services/license_links.py's header for
//             the measurement. The form had never been used once (0 rows).
//
// What the CSV does NOT carry is the AMOUNT of credit debited, so this
// panel deliberately does not show a remaining balance. The Total Used /
// Balance columns appear only when the legacy hand-entered RodtepUsage
// table actually holds rows (`summary.hasLoggedUsage`) - with it empty they
// rendered Balance == Sanctioned on every row, which reads as "none of this
// scrip has been spent" while the CSV says several imports were cleared
// under it. Saying nothing is the honest answer; saying "full balance" is
// not.

// Three helpers below - licenseGapsHtml(), licenseImportsTableHtml() and
// formatQtyOrDash() - are shared with advance-license-panel.js, which loads
// after this file (see index.html's ordering comment). Same cross-panel
// sharing-by-load-order precedent po-modal.js's mirPickerHtml() already
// set for import-po.js; the two licence panels render these two blocks
// identically and the wording is the instruction, so a second copy would
// only give them a way to drift apart.

function formatInrOrDash(value) {
  return (value === null || value === undefined) ? '-' : formatInr(value);
}

function formatQtyOrDash(value) {
  return (value === null || value === undefined) ? '-' : Number(value).toLocaleString('en-IN');
}

async function openRodtepPanel() {
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  // Dialog semantics + focus trap + Escape-to-close (shared.js).
  // Safe to call before this modal's content is assigned: openModalA11y()
  // watches the panel and applies the heading label + initial focus as soon
  // as content lands. (An earlier version of this comment claimed the call
  // had to come after the content - it does not, and in this file it does
  // not; that mismatch is what the late-content handling now covers.)
  openModalA11y(backdrop);
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

function rodtepSyncNote(lastSync) {
  if (!lastSync) return 'Never synced yet.';
  const failedTitle = lastSync.status !== 'success' && lastSync.errorDetail
    ? ' title="' + escapeHtml(lastSync.errorDetail) + '"' : '';
  return '<span' + failedTitle + '>Last synced: ' + formatDateIN(lastSync.finishedAt) +
    ' (' + escapeHtml(lastSync.status) + ')</span>';
}

// The headline numbers, rendered into .modal-meta rather than as KPI cards.
// .kpi-card is a filter button everywhere else in this app (see CLAUDE.md's
// "KPI cards are filter buttons - all of them, or none of them"), and
// nothing in this panel filters, so borrowing that component would promise
// a click that does nothing.
function rodtepSummaryLine(summary) {
  if (!summary) return '';
  const parts = [
    summary.scripCount + ' scrip' + (summary.scripCount === 1 ? '' : 's'),
    formatInr(summary.totalSanctioned) + ' sanctioned',
    summary.importLines + ' import line' + (summary.importLines === 1 ? '' : 's') + ' cleared under them',
  ];
  if (summary.hasLoggedUsage) parts.push(formatInr(summary.totalLoggedUsed) + ' logged as used');
  return parts.join(' &middot; ');
}

function renderRodtepLedgerBody(body, data) {
  const summary = data.summary || {};
  const scripts = data.scripts || [];
  const unknown = data.unknownScrips || [];
  const unclassified = data.unclassifiedCitations || [];
  const showUsage = !!summary.hasLoggedUsage;

  const rowsHtml = scripts.length ? scripts.map(s => {
    const imports = s.imports || {};
    return '<tr class="row-link" data-script="' + escapeHtml(s.scriptNo) + '">' +
      '<td>' + escapeHtml(s.scriptNo) + '</td>' +
      '<td>' + (s.scriptDate ? formatDateIN(s.scriptDate) : '-') + '</td>' +
      '<td>' + escapeHtml(s.location || '-') + '</td>' +
      '<td>' + s.entryCount + '</td>' +
      '<td>' + formatInrOrDash(s.totalSanctioned) + '</td>' +
      // The whole point of the CSV join: whether this scrip has actually
      // been put to work, and where. A scrip sitting at zero is idle credit.
      '<td>' + (imports.lineCount
        ? imports.lineCount + ' line' + (imports.lineCount === 1 ? '' : 's') +
          ' <span class="no-data-note">(' + escapeHtml((imports.plants || []).join(', ')) + ')</span>'
        : '<span class="badge-warn" title="No import line in any plant\'s master CSV names this scrip.">not used yet</span>') +
      '</td>' +
      '<td>' + formatInrOrDash(imports.landedValue) + '</td>' +
      (showUsage ? '<td>' + formatInrOrDash(s.totalUsed) + '</td><td><b>' + formatInrOrDash(s.balance) + '</b></td>' : '') +
    '</tr>';
  }).join('') : '<tr><td colspan="' + (showUsage ? 9 : 7) + '" class="empty-state">No RoDTEP data synced yet.</td></tr>';

  const scripsTabHtml =
    '<div class="field-block full-width">' +
      '<table class="items-table"><thead><tr>' +
        '<th>Script No</th><th>Script Date</th><th>Location</th><th>SB Rows</th>' +
        '<th>Total Sanctioned</th><th>Used Against Imports</th><th>Landed Value Cleared</th>' +
        (showUsage ? '<th>Total Used</th><th>Balance</th>' : '') +
      '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' +
      '<div class="no-data-note mt-8">Credit is earned from exports (each scrip\'s Shipping Bills) and spent ' +
        'against imports. Click a scrip for both sides. No synced source records how much credit a given ' +
        'import debited, so this panel does not show a remaining balance.</div>' +
    '</div>';

  const gapCount = unknown.length + unclassified.length;
  body.innerHTML =
    '<div class="modal-head"><div><h2>RoDTEP Scrip Ledger</h2>' +
    '<div class="modal-meta">' + rodtepSyncNote(data.lastSync) +
      (summary.scripCount ? ' &middot; ' + rodtepSummaryLine(summary) : '') +
    '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="modal-tabs" id="rodtepTabs" role="tablist">' +
      '<div class="modal-tab active" data-rodtep-tab="scrips" tabindex="0" role="tab" aria-selected="true">Scrips (' + scripts.length + ')</div>' +
      '<div class="modal-tab" data-rodtep-tab="gaps" tabindex="0" role="tab" aria-selected="false">Needs attention' + (gapCount ? ' (' + gapCount + ')' : '') + '</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="rodtepScripsPanel">' + scripsTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="rodtepGapsPanel" hidden>' + licenseGapsHtml(unknown, unclassified, 'scrip') + '</div>';

  body.querySelectorAll('tr[data-script]').forEach(tr => tr.onclick = () => openRodtepScriptDetail(tr.dataset.script));
  wireRodtepTabs(body);
}

function wireRodtepTabs(body) {
  const panels = { scrips: 'rodtepScripsPanel', gaps: 'rodtepGapsPanel' };
  body.querySelectorAll('[data-rodtep-tab]').forEach(tab => {
    const pick = () => {
      body.querySelectorAll('[data-rodtep-tab]').forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      Object.entries(panels).forEach(([key, id]) => {
        const el = document.getElementById(id);
        if (el) el.hidden = tab.dataset.rodtepTab !== key;
      });
    };
    tab.onclick = pick;
    tab.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
  });
}

// Shared by both licence panels - the two "somebody has to fix this
// upstream" lists are the same shape and the same instruction on both
// schemes, so they are rendered once here rather than twice per panel.
// `noun` is 'scrip' or 'licence' purely for the wording.
function licenseGapsHtml(unknown, unclassified, noun) {
  const unknownHtml = unknown.length ? unknown.map(u =>
    '<tr>' +
      '<td>' + escapeHtml(u.licenseNumber) + '</td>' +
      '<td>' + escapeHtml((u.licenseNumbersRaw || []).join(', ')) + '</td>' +
      '<td>' + u.lineCount + '</td>' +
      '<td>' + escapeHtml((u.plants || []).join(', ') || '-') + '</td>' +
      '<td>' + escapeHtml((u.boeNumbers || []).join(', ') || '-') + '</td>' +
      '<td>' + formatInrOrDash(u.landedValue) + '</td>' +
    '</tr>'
  ).join('') : '<tr><td colspan="6" class="empty-state">Every ' + noun + ' the imports data names is one we hold - good.</td></tr>';

  const unclassifiedHtml = unclassified.length ? unclassified.map(c =>
    '<tr>' +
      '<td>' + escapeHtml(c.plant) + '</td>' +
      '<td>' + escapeHtml(c.poNumber) + (c.itemId ? ' / ' + escapeHtml(c.itemId) : '') + '</td>' +
      '<td>' + escapeHtml(c.description || '-') + '</td>' +
      '<td>' + escapeHtml(c.boeNumber || '-') + '</td>' +
      '<td>' + escapeHtml(c.licenseNumberRaw || '-') + '</td>' +
      '<td>' + (c.licenseTypeRaw ? escapeHtml(c.licenseTypeRaw) : '<span class="badge-warn">blank</span>') + '</td>' +
    '</tr>'
  ).join('') : '<tr><td colspan="6" class="empty-state">Every import line naming a licence says which scheme it is under - good.</td></tr>';

  return '<div class="field-block full-width">' +
      '<h4>Named by an import, not in this ledger</h4>' +
      '<div class="no-data-note mb-8">An import cites this number and we hold no ' + noun + ' file for it. ' +
        'Either the file has not reached Drive yet, or the number in the master CSV is wrong - both are fixed upstream, not here.</div>' +
      '<table class="items-table"><thead><tr>' +
        '<th>Number</th><th>As written in the CSV</th><th>Import Lines</th><th>Plants</th><th>BOEs</th><th>Landed Value</th>' +
      '</tr></thead><tbody>' + unknownHtml + '</tbody></table>' +
    '</div>' +
    '<div class="field-block full-width mt-14">' +
      '<h4>Names a licence but not which scheme</h4>' +
      '<div class="no-data-note mb-8">These lines fill in License Number and leave License Type blank (or write something ' +
        'neither RODTEP nor ADVANCE), so neither ledger can claim them. Filling in that one column is the whole fix.</div>' +
      '<table class="items-table"><thead><tr>' +
        '<th>Plant</th><th>PO / Item</th><th>Material</th><th>BOE</th><th>License Number</th><th>License Type</th>' +
      '</tr></thead><tbody>' + unclassifiedHtml + '</tbody></table>' +
    '</div>';
}

// Rendered by both panels: the import lines a licence/scrip was actually
// used on, straight from the master CSV.
function licenseImportsTableHtml(citations, totals) {
  if (!citations.length) {
    return '<div class="no-data-note">No import line in any plant\'s master CSV names this one. Either it has not been ' +
      'used yet, or the License Number column was left blank on the imports it was used for.</div>';
  }
  const rowsHtml = citations.map(c =>
    '<tr>' +
      '<td>' + escapeHtml(c.plant) + '</td>' +
      '<td>' + escapeHtml(c.poNumber) + (c.itemId ? ' / ' + escapeHtml(c.itemId) : '') + '</td>' +
      '<td>' + escapeHtml(c.description || '-') + '</td>' +
      '<td>' + escapeHtml(c.boeNumber || '-') + '</td>' +
      '<td>' + formatQtyOrDash(c.qty) + ' ' + escapeHtml(c.uom || '') + '</td>' +
      '<td>' + formatInrOrDash(c.landedValue) +
        // Without this a reader sums the column and believes the total was
        // all drawn against this one licence. Nothing in any source says
        // how a shared line splits, so the panel says it is shared rather
        // than inventing a split.
        ((c.sharedWith || []).length
          ? ' <span class="badge-warn" title="This line also names ' + escapeHtml(c.sharedWith.join(', ')) +
            '. The value shown is the whole line, not this one\'s share.">shared</span>'
          : '') +
      '</td>' +
    '</tr>'
  ).join('');
  const t = totals || {};
  return '<table class="items-table"><thead><tr>' +
      '<th>Plant</th><th>PO / Item</th><th>Material</th><th>BOE</th><th>Qty (BOE)</th><th>Landed Value</th>' +
    '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' +
    '<div class="no-data-note mt-8">' + (t.lineCount || 0) + ' line(s) across ' + (t.poCount || 0) + ' purchase order(s)' +
      (t.sharedLines ? ' &middot; ' + t.sharedLines + ' also name another licence, so the landed values are not additive' : '') +
    '</div>';
}

async function openRodtepScriptDetail(scriptNo) {
  // Stale-response guard - same fix/reasoning as openImportPoModal()'s/
  // openMaterialModal()'s modalRequestId checks (charts.js): clicking one
  // script row then a different one before the first row's own await below
  // resolves could otherwise let the first (now-stale) response land after
  // the second and silently overwrite the modal with the wrong script's
  // data. Found missing here during a full-codebase audit and fixed to
  // match the existing pattern.
  const myModalRequestId = ++modalRequestId;
  const body = document.getElementById('modalBody');
  body.innerHTML = '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2>' +
    '<div class="modal-meta">Loading...</div></div><span class="close-btn">&times;</span></div>';

  let detail;
  try {
    detail = await apiImports('/rodtep/' + encodeURIComponent(scriptNo));
  } catch (e) {
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    body.innerHTML = '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2></div><span class="close-btn">&times;</span></div>' +
      '<div class="field-block full-width">' + escapeHtml(e.message || 'Failed to load.') + '</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one

  const entriesHtml = detail.entries.length ? detail.entries.map(e =>
    '<tr><td>' + escapeHtml(e.sbNumber) + '</td><td>' + (e.sbDate ? formatDateIN(e.sbDate) : '-') + '</td>' +
    '<td>' + escapeHtml(e.scrollNumber || '-') + '</td><td>' + formatInrOrDash(e.sanctionedAmount) + '</td></tr>'
  ).join('') : '<tr><td colspan="4" class="empty-state">No Shipping Bill rows synced for this script yet.</td></tr>';

  // Legacy, and shown only when it holds something. The form that wrote
  // these rows is gone (see this file's header); the rows themselves are a
  // record of a real human decision and are not hidden.
  const usagesHtml = detail.usages.length
    ? '<div class="field-block full-width mt-14"><h4>Usage logged by hand (legacy)</h4>' +
        '<table class="items-table"><thead><tr><th>Amount Used</th><th>BOE Number</th><th>Import PO</th><th>Date</th><th>Notes</th></tr></thead>' +
        '<tbody>' + detail.usages.map(u =>
          '<tr><td>' + formatInrOrDash(u.usedAmount) + '</td>' +
          '<td>' + escapeHtml(u.boeNumber || '-') + (u.boeNumber ? (u.boeVerified ? ' <span class="badge-verified">verified</span>' : ' <span class="badge-warn">not found</span>') : '') + '</td>' +
          '<td>' + escapeHtml(u.importPoNumber || '-') + '</td>' +
          '<td>' + (u.usedDate ? formatDateIN(u.usedDate) : '-') + '</td>' +
          '<td>' + escapeHtml(u.notes || '-') + '</td></tr>'
        ).join('') + '</tbody></table>' +
      '</div>'
    : '';

  const totals = detail.importTotals || {};
  body.innerHTML =
    '<div class="modal-head"><div><h2>Script ' + escapeHtml(scriptNo) + '</h2>' +
    '<div class="modal-meta">RoDTEP scrip detail &middot; ' + detail.entries.length + ' Shipping Bill(s) earned it, ' +
      (totals.lineCount || 0) + ' import line(s) used it' +
    '</div></div><span class="close-btn">&times;</span></div>' +
    '<div class="field-block full-width"><h4>Shipping Bill Credits (earned from exports)</h4>' +
      '<table class="items-table"><thead><tr><th>SB Number</th><th>SB Date</th><th>Scroll Number</th><th>Sanctioned</th></tr></thead>' +
      '<tbody>' + entriesHtml + '</tbody></table>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Imports Cleared Under This Scrip</h4>' +
      '<div class="no-data-note mb-8">From each plant\'s Imports Purchase Data master CSV (License Type = RODTEP). ' +
        'The CSV records which scrip was applied, never how much credit it debited.</div>' +
      licenseImportsTableHtml(detail.imports || [], totals) +
    '</div>' +
    usagesHtml +
    '<div class="field-block full-width mt-14"><span class="row-link" id="rodtepBackLink">&larr; Back to all scrips</span></div>';

  const back = document.getElementById('rodtepBackLink');
  if (back) back.onclick = () => openRodtepPanel();
}
