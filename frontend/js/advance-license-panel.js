// ── Advance License panel (added 2026-09-09) ────────────────────────────────
// Scoped to Import Purchases ONLY, per the project owner's own instruction -
// no Domestic Purchases changes, no new top-level nav tab. Same reasoning
// and same shared-modal pattern as rodtep-panel.js (Advance Licenses are a
// company-wide resource too - see apps/core/models/ledgers.py's AdvanceLicense),
// and the same existing-CSS-only rule.
//
// There has never been a manual "Log Usage" form here: the source workbook
// the project owner maintains by hand already carries the BOE/import-PO/
// qty/value usage columns per material, synced as-is. "Sync Now" is gone as
// of 2026-09-22, for the same reason it went from the RoDTEP panel - see
// that file's header. This panel is read-only.
//
// ── What it shows, and which source each number comes from ─────────────────
// A licence is worth watching for three separate reasons, and until
// 2026-09-22 this panel showed a flat table that answered none of them:
//
//   How much is left    CIF authorised vs value imported. From the WORKBOOK's
//                       own usage columns - the only source carrying a value
//                       drawn against a licence.
//   How long is left    days to the export-obligation deadline and to the
//                       import validity end. Both from the workbook.
//   Where it was used   the import lines the master CSV says were cleared
//                       under it, via apps/services/license_links.py.
//
// The two sources are shown side by side rather than reconciled into one
// figure, and `boeCrossCheck` is the reason that is worth doing: a BOE in
// the workbook with no import line naming the licence, or an import line
// naming the licence for a BOE the workbook has no usage row for, is a real
// bookkeeping gap in whichever side is missing it. Nothing else in this app
// could see it.
//
// formatQtyOrDash(), licenseGapsHtml() and licenseImportsTableHtml() come
// from rodtep-panel.js, which loads first - see that file's own note.

// Which licence's detail view is open, or null for the list. Read by the
// back link and by the tab wiring so a re-render returns to the same place.
let AL_CTX = { data: null, licenseNumber: null };

async function openAdvanceLicensePanel() {
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
  AL_CTX = { data: data, licenseNumber: null };
  renderAdvanceLicenseLedgerBody(body, data);
}

function alSyncNote(lastSync) {
  if (!lastSync) return 'Never synced yet.';
  const failedTitle = lastSync.status !== 'success' && lastSync.errorDetail
    ? ' title="' + escapeHtml(lastSync.errorDetail) + '"' : '';
  return '<span' + failedTitle + '>Last synced: ' + formatDateIN(lastSync.finishedAt) +
    ' (' + escapeHtml(lastSync.status) + ')</span>';
}

// Utilisation as a percentage, or a dash. Null (rather than 0) is what the
// API sends when nothing was authorised - see advance_license_ledger()'s own
// comment: a percentage of zero is a question about the source row, not
// "0% used", so it must not render as 0%.
function formatPctOrDash(value) {
  if (value === null || value === undefined) return '-';
  const pct = Number(value);
  // A real but tiny draw must not round to "0.0%", which reads as "nothing
  // has been drawn against this licence" - the same wrong answer the null
  // case above exists to avoid, arrived at from the other direction.
  if (pct > 0 && pct < 0.05) return '&lt;0.1%';
  return pct.toFixed(1) + '%';
}

// How much export-obligation time is left, with the urgency stated in the
// badge rather than left for the reader to work out from a date. Same amber/
// red convention the rest of the dashboard uses for overdue work.
function validityCellHtml(dateStr, daysLeft, expired, expiringSoon) {
  if (!dateStr) return '-';
  const shown = formatDateIN(dateStr);
  if (expired) {
    return shown + ' <span class="badge failed" title="The export obligation period has ended.">expired ' +
      Math.abs(daysLeft) + 'd ago</span>';
  }
  if (expiringSoon) {
    return shown + ' <span class="badge stale" title="Export obligation deadline is close.">' + daysLeft + 'd left</span>';
  }
  return shown + ' <span class="no-data-note">(' + daysLeft + 'd)</span>';
}

function alSummaryLine(summary) {
  if (!summary || !summary.licenseCount) return '';
  const parts = [
    summary.licenseCount + ' licence' + (summary.licenseCount === 1 ? '' : 's'),
    formatInr(summary.cifAuthorized) + ' CIF authorised',
    formatPctOrDash(summary.cifUtilisedPct) + ' used',
  ];
  if (summary.exportExpired) parts.push(summary.exportExpired + ' expired');
  if (summary.exportExpiringSoon) {
    parts.push(summary.exportExpiringSoon + ' due within ' + summary.expirySoonDays + ' days');
  }
  return parts.join(' &middot; ');
}

function renderAdvanceLicenseLedgerBody(body, data) {
  const summary = data.summary || {};
  const licenses = data.licenses || [];
  const unknown = data.unknownLicenses || [];
  const unclassified = data.unclassifiedCitations || [];

  const rowsHtml = licenses.length ? licenses.map(lic => {
    const usage = lic.usage || {};
    const validity = lic.validity || {};
    const imports = lic.imports || {};
    const gaps = (lic.boeCrossCheck || {});
    const gapCount = (gaps.workbookOnly || []).length + (gaps.csvOnly || []).length;
    return (
      '<tr class="row-link" data-license="' + escapeHtml(lic.licenseNumber) + '">' +
        '<td>' + escapeHtml(lic.licenseNumber) + '</td>' +
        '<td>' + escapeHtml(lic.exportProductDescription || '-') + '</td>' +
        '<td>' + formatInrOrDash(lic.cifValueAuthorized) + '</td>' +
        '<td>' + formatInrOrDash(usage.valueImported) + '</td>' +
        '<td>' + formatPctOrDash(usage.cifUtilisedPct) + '</td>' +
        '<td>' + formatInrOrDash(usage.cifRemaining) + '</td>' +
        '<td>' + validityCellHtml(lic.exportValidityDate, validity.exportDaysLeft,
          validity.exportExpired, validity.exportExpiringSoon) + '</td>' +
        '<td>' + (imports.lineCount
          ? imports.lineCount + ' line' + (imports.lineCount === 1 ? '' : 's')
          : '<span class="badge-warn" title="No import line in any plant\'s master CSV names this licence.">none</span>') +
        '</td>' +
        '<td>' + (gapCount
          ? '<span class="badge-warn" title="The workbook and the imports CSV disagree about ' + gapCount +
            ' Bill(s) of Entry. Open this licence to see which.">' + gapCount + ' gap' + (gapCount === 1 ? '' : 's') + '</span>'
          : '<span class="badge-verified">agree</span>') +
        '</td>' +
      '</tr>'
    );
  }).join('') : '<tr><td colspan="9" class="empty-state">No Advance License data synced yet.</td></tr>';

  const listTabHtml =
    '<div class="field-block full-width">' +
      '<table class="items-table"><thead><tr>' +
        '<th>License Number</th><th>Export Product</th>' +
        '<th>CIF Authorised</th><th>Value Imported</th><th>Used</th><th>CIF Remaining</th>' +
        '<th>Export Validity</th><th>Imports Citing It</th><th>BOE Check</th>' +
      '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' +
      '<div class="no-data-note mt-8">CIF and validity come from the Advance License workbook; "Imports Citing It" ' +
        'and the BOE check come from each plant\'s Imports Purchase Data master CSV. Click a licence for both sides, ' +
        'plus its input materials.</div>' +
    '</div>';

  const gapCount = unknown.length + unclassified.length;
  body.innerHTML =
    '<div class="modal-head"><div><h2>Advance License Ledger</h2>' +
    '<div class="modal-meta">' + alSyncNote(data.lastSync) +
      (summary.licenseCount ? ' &middot; ' + alSummaryLine(summary) : '') +
    '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="modal-tabs" id="alTabs" role="tablist">' +
      '<div class="modal-tab active" data-al-tab="licenses" tabindex="0" role="tab" aria-selected="true">Licences (' + licenses.length + ')</div>' +
      '<div class="modal-tab" data-al-tab="gaps" tabindex="0" role="tab" aria-selected="false">Needs attention' + (gapCount ? ' (' + gapCount + ')' : '') + '</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="alLicensesPanel">' + listTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="alGapsPanel" hidden>' + licenseGapsHtml(unknown, unclassified, 'licence') + '</div>';

  body.querySelectorAll('tr[data-license]').forEach(tr => tr.onclick = () => openAdvanceLicenseDetail(tr.dataset.license));
  wireAdvanceLicenseTabs(body);
}

function wireAdvanceLicenseTabs(body) {
  const panels = { licenses: 'alLicensesPanel', gaps: 'alGapsPanel' };
  body.querySelectorAll('[data-al-tab]').forEach(tab => {
    const pick = () => {
      body.querySelectorAll('[data-al-tab]').forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      Object.entries(panels).forEach(([key, id]) => {
        const el = document.getElementById(id);
        if (el) el.hidden = tab.dataset.alTab !== key;
      });
    };
    tab.onclick = pick;
    tab.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
  });
}

// No fetch here, so no modalRequestId guard is needed (that guard exists for
// openers that await - see charts.js): the ledger endpoint already returns
// every licence with its materials and citations, and licence counts are in
// the tens, so a per-licence endpoint would be a round trip for data already
// in hand.
function openAdvanceLicenseDetail(licenseNumber) {
  const body = document.getElementById('modalBody');
  const lic = (AL_CTX.data && (AL_CTX.data.licenses || []).find(l => l.licenseNumber === licenseNumber)) || null;
  if (!lic) { openAdvanceLicensePanel(); return; }
  AL_CTX.licenseNumber = licenseNumber;

  const usage = lic.usage || {};
  const validity = lic.validity || {};
  const gaps = lic.boeCrossCheck || {};

  // Authorised vs imported per input material, with what is left. The
  // workbook repeats a material once per import drawn against it, so the
  // server rolls those up before they get here (see _material_rollup) -
  // don't re-derive that in the browser.
  const materials = lic.materialRollup || [];
  const materialsHtml = materials.length ? materials.map(m =>
    '<tr>' +
      '<td>' + escapeHtml(m.materialDescription || '-') + '</td>' +
      '<td>' + escapeHtml(m.itchsCode || '-') + '</td>' +
      '<td>' + formatQtyOrDash(m.qtyAuthorized) + '</td>' +
      '<td>' + formatQtyOrDash(m.qtyImported) + '</td>' +
      '<td>' + formatQtyOrDash(m.qtyRemaining) + '</td>' +
      '<td>' + formatInrOrDash(m.cifValueAuthorized) + '</td>' +
      '<td>' + formatInrOrDash(m.valueImported) + '</td>' +
      '<td>' + (m.dutySavedPct === null || m.dutySavedPct === undefined ? '-' : Number(m.dutySavedPct) + '%') + '</td>' +
      '<td>' + m.usageRows + '</td>' +
    '</tr>'
  ).join('') : '<tr><td colspan="9" class="empty-state">No input materials in the workbook for this licence.</td></tr>';

  const boeGapHtml =
    ((gaps.workbookOnly || []).length || (gaps.csvOnly || []).length)
      ? '<div class="field-block full-width mt-14"><h4>The two sources disagree</h4>' +
          ((gaps.workbookOnly || []).length
            ? '<div class="line">In the workbook, not named by any import line: <b>' +
              escapeHtml(gaps.workbookOnly.join(', ')) + '</b>' +
              '<div class="no-data-note">Those imports\' License Number column is probably blank in the master CSV.</div></div>'
            : '') +
          ((gaps.csvOnly || []).length
            ? '<div class="line mt-8">Named by an import, with no usage row in the workbook: <b>' +
              escapeHtml(gaps.csvOnly.join(', ')) + '</b>' +
              '<div class="no-data-note">The workbook has not been updated for those clearances, so the CIF ' +
              'utilisation above understates what has been drawn.</div></div>'
            : '') +
        '</div>'
      : '';

  body.innerHTML =
    '<div class="modal-head"><div><h2>License ' + escapeHtml(lic.licenseNumber) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(lic.exportProductDescription || 'Advance authorisation') +
      (lic.status ? ' &middot; ' + escapeHtml(lic.status) : '') +
    '</div></div><span class="close-btn">&times;</span></div>' +
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Authorisation</h4>' +
        '<div class="line">IEC: ' + escapeHtml(lic.iec || '-') + '</div>' +
        '<div class="line">Issued: ' + (lic.issueDate ? formatDateIN(lic.issueDate) : '-') + '</div>' +
        '<div class="line">CIF value authorised: <b>' + formatInrOrDash(lic.cifValueAuthorized) + '</b></div>' +
        '<div class="line">FOB export target: ' + formatInrOrDash(lic.fobValueExportTarget) + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>Drawn so far (workbook)</h4>' +
        '<div class="line">Value imported: <b>' + formatInrOrDash(usage.valueImported) + '</b></div>' +
        '<div class="line">CIF remaining: ' + formatInrOrDash(usage.cifRemaining) + '</div>' +
        '<div class="line">Utilised: ' + formatPctOrDash(usage.cifUtilisedPct) + '</div>' +
        '<div class="line">' + usage.usageRows + ' usage row(s) across ' + usage.materialCount + ' material(s)</div>' +
      '</div>' +
      '<div class="field-block"><h4>Time left</h4>' +
        '<div class="line">Export obligation ends: ' +
          validityCellHtml(lic.exportValidityDate, validity.exportDaysLeft,
            validity.exportExpired, validity.exportExpiringSoon) + '</div>' +
        '<div class="line">Import validity ends: ' +
          validityCellHtml(lic.importValidityDate, validity.importDaysLeft, validity.importExpired, false) + '</div>' +
        '<div class="no-data-note">Days are counted in plant time (Asia/Kolkata), not the server\'s.</div>' +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Input Materials - authorised vs imported</h4>' +
      '<table class="items-table"><thead><tr>' +
        '<th>Material</th><th>ITC(HS)</th><th>Qty Authorised</th><th>Qty Imported</th><th>Qty Remaining</th>' +
        '<th>CIF Authorised</th><th>Value Imported</th><th>Duty Saved</th><th>Usage Rows</th>' +
      '</tr></thead><tbody>' + materialsHtml + '</tbody></table>' +
      '<div class="no-data-note mt-8">Authorised figures are taken once per material, never summed across its ' +
        'usage rows - the workbook repeats them on every row of the same material.</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Imports Cleared Under This Licence</h4>' +
      '<div class="no-data-note mb-8">From each plant\'s Imports Purchase Data master CSV ' +
        '(License Type = ADVANCE), independent of the workbook\'s own usage columns above.</div>' +
      licenseImportsTableHtml(lic.importCitations || [], lic.imports || {}) +
    '</div>' +
    boeGapHtml +
    '<div class="field-block full-width mt-14"><span class="row-link" id="alBackLink">&larr; Back to all licences</span></div>';

  const back = document.getElementById('alBackLink');
  if (back) back.onclick = () => {
    AL_CTX.licenseNumber = null;
    renderAdvanceLicenseLedgerBody(document.getElementById('modalBody'), AL_CTX.data);
  };
}
