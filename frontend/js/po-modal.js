// ── PO detail modal (Domestic) ──────────────────────────────────────────
// compositeKey is "<plantKey>::<poNumber>" (see plantKeyFor()) - always
// looked up in that specific plant's own cache, never the merged "All
// Plants" array, since PO numbers aren't guaranteed unique across plants.
// Two tabs, per the project owner's 2026-09-04 request (reference: the
// original "Purchase Tracker" artifact's PO detail screenshot): "Overview"
// (PO/vendor/commercial fields, card-grid layout matching that reference)
// and "Item & Stock" (the line-item table, already carrying each item's MIR
// match status - the closest this app's real data gets to the reference's
// separate "stock" concept, since there's no per-line PO<->Stock chain yet,
// see CLAUDE.md's known gaps). Deliberately doesn't invent fields the
// reference image shows but this app's data model doesn't have (ship-to/
// billing address, vendor email/contact/code, HSN, tax breakdown) - showing
// a fake "Not available" for a field that was never even attempted to be
// captured would misrepresent what this app actually extracts, vs. a field
// that was captured but happens to be blank on this PO. Only real fields
// from _po_dict() (apps/api/routers/*_views.py) are shown.
let poModalTab = 'overview';

// Re-fetches plantKey's PO list after a successful inline-edit save (the
// list endpoint is Domestic's only PO read endpoint - see hrs_views.py's
// module docstring - so a full-list invalidate+reload is how a correction's
// downstream effects (recomputed PO<->MIR match, KPI counts, flag badges)
// reach both the modal and the list behind it), then re-renders both.
async function onDomesticFieldSaved(plantKey, poNumber) {
  PURCHASE_ORDERS_BY_PLANT[plantKey] = null;
  await ensurePOsLoaded([plantKey]);
  await openPoModal(plantKey + '::' + poNumber);
  const content = document.getElementById('content');
  if (content) renderPoList(content);
}

async function openPoModal(compositeKey) {
  // Stale-response guard - same fix/reasoning as openImportPoModal()'s and
  // openMaterialModal()'s own modalRequestId checks (charts.js): without
  // this, clicking one PO row then a different one before the first row's
  // own await below resolves could let the first (now-stale) response land
  // after the second and silently overwrite the modal with the wrong PO's
  // data. This function was missed when that fix was applied to the other
  // two modal-openers on 2026-09-04 - found during a later full-codebase
  // audit and fixed here to match.
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const poNumber = compositeKey.slice(sep + 2);
  const po = (PURCHASE_ORDERS_BY_PLANT[plantKey] || []).find(p => p.poNumber === poNumber);
  if (!po) return;
  computePoFlags(po);
  // This modal has no charts of its own, but clear any left over from a
  // previous material modal open just in case one was left dangling -
  // cheap defensive cleanup, see modalCharts' own comment.
  destroyModalCharts();
  const apiBase = PLANTS[plantKey].apiPrefix;
  const edit = (label, value, field, itemId, fieldType, options) => editableLine(plantKey, label, value, field, itemId, fieldType, options);

  // Loaded across all 3 plants (not just this PO's own) so the "View full
  // material analysis" link below can show an "All plants total" figure,
  // same cross-plant framing Raw Material Analysis itself uses - cached
  // per plant (shared.js's findMaterialLotsFor()), so this is a no-op
  // re-fetch once any PO/material modal has opened once this session.
  // Best-effort: a failure here just means no material-analysis links on
  // this open, not a broken modal.
  // Plus the saved sort presets of the Item & Stock tab's line cards and
  // MIR receipts (po-reconcile.js) - those loaders never throw.
  try {
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS), PO_LINES_SORT.ensurePresetsLoaded(), PO_RECEIPTS_SORT.ensurePresetsLoaded()]);
  } catch (e) {
    console.error('openPoModal: ensureMaterialsLoaded failed:', e);
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one

  // One reconciliation card per line (po-reconcile.js): Ordered / Received /
  // Difference in real figures, every matched MIR receipt listed. The MIR
  // "change" link and the dismiss link live on the card, and still carry the
  // same data-* attributes wireMirPicker()/wireDismissLinks() read. Domestic
  // line items have no stable item_id, so per-field item edits stay unwired -
  // see DomesticPOCorrection; the MIR pin is addressed by `itemRef` instead.
  const reconLines = (po.items || []).map((it, i) => domesticReconLine(it, i + 1, po, plantKey));

  const currencyOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.currency);
  const incotermsOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.incoterms);
  const taxTypeOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.taxType);

  // Trimmed to what the project owner asked for (2026-09-24): PO details,
  // vendor name, billing and shipping address, packing and incoterms, value,
  // remarks. Vendor address/GSTIN/email/code are still synced and still
  // correctable - they simply are not what a reader opens this for. The
  // master CSV has no packing column, so that card carries Incoterms and
  // Payment Terms, the two delivery terms the PO does record.
  const overviewHtml =
    '<div class="po-overview"><div class="field-grid">' +
      '<div class="field-block"><h4>Purchase Order</h4>' +
        '<div class="line">PO No: ' + escapeHtml(po.poNumber) + '</div>' +
        edit('Created on', po.createdDate, 'po_created_date', null, 'date') +
        '<div class="line">Plant: ' + escapeHtml(PLANTS[plantKey].label) + '</div>' +
      '</div>' +
      '<div class="field-block value-card"><h4>Value</h4>' +
        edit('Total Value', po.totalValue, 'total_value', null, 'number') +
        edit('Total Inclusive Value', po.totalInclTax, 'total_inclusive_value', null, 'number') +
        edit('Tax Type', po.taxType, 'tax_type', null, 'select', taxTypeOptions) +
        edit('Currency', po.currency, 'currency', null, 'select', currencyOptions) +
      '</div>' +
      '<div class="field-block"><h4>Vendor</h4>' +
        edit('Vendor Name', po.vendorName, 'vendor_name') +
      '</div>' +
      '<div class="field-block"><h4>Packing and Incoterms</h4>' +
        edit('Incoterms', po.incoterms, 'incoterms', null, 'select', incotermsOptions) +
        edit('Payment Terms', po.paymentTerms, 'payment_terms') +
      '</div>' +
      '<div class="field-block"><h4>Billing Address</h4>' +
        edit('Billing Address', po.billingAddress, 'billing_address') +
      '</div>' +
      '<div class="field-block"><h4>Shipping Address</h4>' +
        edit('Ship To', po.shipTo, 'ship_to') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Remarks</h4>' +
      edit('Remarks', po.remarks, 'remarks') + '</div></div>';

  const itemStockHtml =
    '<div class="mb-block-16">' + miniStepperHtml(po) + '</div>' +
    reconItemsHtml(reconLines, plantKey, '') +
    // Rendered once per modal and moved under whichever line's "change" was
    // clicked - same behaviour as the correction box (shared.js).
    mirPickerHtml();

  // Match Accuracy Programme fix 3.G: arithmetic-mismatch flags
  // (dataQualityFlagHtml, flags.js) alongside the existing remarks-derived
  // categories (poFlagHtml) - a real source-sheet typo, not a matching
  // artifact, so it's shown even when there are no categories. Lists
  // _allCategories, not _categories: a dismissed flag must stay listed here
  // (struck through, with "reinstate") even though it no longer counts.
  const arithmeticFlagsHtml = (po.dataQualityFlags || []).map(dataQualityFlagHtml).join('');
  const allCats = po._allCategories || po._categories || [];
  const catsHtml = allCats.length || arithmeticFlagsHtml
    ? allCats.map(c => poFlagHtml(c, po, plantKey)).join('') + arithmeticFlagsHtml
    : '<div class="empty-note-sm">No data quality flags on this PO.</div>';
  // "revert" puts the old value back (shared.js's wireRevertLinks()). Shown
  // only to someone who could have made the correction in the first place,
  // and only for a PO-level field: an item-level correction is keyed on an
  // item_id domestic line items do not reliably carry (see this file's own
  // itemsHtml comment), so reverting one has nothing dependable to address.
  const canRevert = canEditField(plantKey);
  const correctionsHtml = (po.corrections || []).length
    ? '<div class="section-title mt-18">Correction History</div>' +
      po.corrections.map(c =>
        '<div class="field-block mb-8">' +
          '<div class="fs-12-5"><b>' + escapeHtml(c.fieldName) + (c.itemId ? ' (item ' + escapeHtml(c.itemId) + ')' : '') + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') +
          (canRevert && !c.itemId
            ? '<span class="revert-link" data-field="' + escapeHtml(c.fieldName) + '"' +
              ' data-label="' + escapeHtml(c.fieldName) + '"' +
              ' data-old-value="' + escapeHtml(c.oldValue || '') + '">revert</span>'
            : '') +
          '</div>' +
          (c.reason ? '<div class="mt-4 fs-12 italic text-slate">"' + escapeHtml(c.reason) + '"</div>' : '') +
          '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? localDateOf(c.correctedAt) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const flagsTabHtml = catsHtml + correctionsHtml + manualChangesSectionHtml() +
    overrideBoxHtml('Click the ✎ beside any field to correct it.');

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
  poModalTab = poModalTab || 'overview';
  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(po.poNumber) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(po.vendorName || 'Unknown vendor') + ' &middot; ' + escapeHtml(PLANTS[plantKey].label) + ' &middot; ' + (po.createdDate ? escapeHtml(formatDateIN(po.createdDate)) : 'no date') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="modal-tabs" id="poModalTabs" role="tablist">' +
      '<div class="modal-tab' + (poModalTab === 'overview' ? ' active' : '') + '" data-tab="overview" tabindex="0" role="tab" aria-selected="' + (poModalTab === 'overview') + '">Overview</div>' +
      '<div class="modal-tab' + (poModalTab === 'itemstock' ? ' active' : '') + '" data-tab="itemstock" tabindex="0" role="tab" aria-selected="' + (poModalTab === 'itemstock') + '">Item &amp; Stock</div>' +
      '<div class="modal-tab' + (poModalTab === 'flags' ? ' active' : '') + '" data-tab="flags" tabindex="0" role="tab" aria-selected="' + (poModalTab === 'flags') + '">Flags &amp; Corrections</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="poModalOverview"' + (poModalTab !== 'overview' ? ' hidden' : '') + '>' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="poModalItemStock"' + (poModalTab !== 'itemstock' ? ' hidden' : '') + '>' + itemStockHtml + '</div>' +
    '<div class="modal-tab-panel" id="poModalFlags"' + (poModalTab !== 'flags' ? ' hidden' : '') + '>' + flagsTabHtml + '</div>';

  body.querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    poModalTab = tab.dataset.tab;
    body.querySelectorAll('[data-tab]').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    document.getElementById('poModalOverview').hidden = poModalTab !== 'overview';
    document.getElementById('poModalItemStock').hidden = poModalTab !== 'itemstock';
    document.getElementById('poModalFlags').hidden = poModalTab !== 'flags';
  });

  const fieldsUrl = apiBase + '/purchase-orders/' + encodeURIComponent(poNumber) + '/fields';
  const switchToFlagsTab = () => { const t = body.querySelector('[data-tab="flags"]'); if (t) t.click(); };
  wireEditIcons(body, fieldsUrl, switchToFlagsTab, (fieldName, itemId) => onDomesticFieldSaved(plantKey, poNumber));
  wireRevertLinks(body, fieldsUrl, () => onDomesticFieldSaved(plantKey, poNumber));
  // A manual MIR change re-runs the whole plant's matching, so other POs'
  // badges can move too - the same full invalidate+reload a field
  // correction already does, not a modal-only refresh.
  const poPath = '/purchase-orders/' + encodeURIComponent(poNumber);
  const jsonBody = (method, payload) => ({ method: method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  const mirApi = {
    plantKey: plantKey,
    candidates: (q, itemRef) => apiForPlant(plantKey, poPath + '/mir-candidates?itemRef=' + encodeURIComponent(itemRef || '') +
      (q ? '&q=' + encodeURIComponent(q) : '')),
    save: (payload) => apiForPlant(plantKey, poPath + '/mir-match', jsonBody('PATCH', payload)),
    preview: (payload) => apiForPlant(plantKey, poPath + '/mir-match/preview', jsonBody('POST', payload)),
    previewStatus: (id) => apiForPlant(plantKey, '/mir-match-previews/' + encodeURIComponent(id)),
    changes: () => apiForPlant(plantKey, poPath + '/manual-changes'),
  };
  const onMirChanged = () => onDomesticFieldSaved(plantKey, poNumber);
  wireMirPicker(body, mirApi, poNumber, onMirChanged);
  loadManualChanges(body, mirApi, plantKey, onMirChanged, myModalRequestId);
  wireDismissLinks(body, plantKey, () => onDomesticFieldSaved(plantKey, poNumber));
  applyDynamicStyles(body); // the reconciliation cards' progress bars
  body.querySelectorAll('[data-material-link]').forEach(el2 => el2.onclick = () => openMaterialLink(el2.dataset.materialLink));
  wireReconSorting(body); // Item & Stock's line and receipt sorting (po-reconcile.js)
}

// ── Edit a line's MIR receipts ──────────────────────────────────────────
// "It would be great if we can edit the MIR number too, and if that was
// assigned to some other PO then a pop up would appear telling that matching
// with this would break so and so" (project owner, 2026-09-21).
//
// Rebuilt 2026-09-29 after HRS 3000001167: the only control used to be
// "change MIR", which REPLACED a line's receipts with one document, so adding
// the missing 59/09 to the 7MPA line took away the two receipts it already
// counted. The panel now works one receipt at a time:
//   - every receipt the line counts is listed with a Remove button, and with
//     who put it there when a person did (receipt_notes);
//   - "Add a receipt" searches MIR, grouped most-likely first, and says for
//     each receipt why the matcher is not already counting it here;
//   - every change is previewed first - the matcher runs on the background
//     worker without saving (apps/services/receipt_preview.py) and the panel
//     shows each line whose receipts or received quantity would move, before
//     the reader saves it;
//   - "No MIR - not received" and "Back to automatic" stay.
//
// A picker, not a free-text box: typing a MIR number blind is how you name a
// document that does not exist. It names a MIR NUMBER, not a row - see
// ManualMirMatch's docstring. The panel moves to sit under the line it edits.
//
// Shared by the Domestic PO modal (this file) and the Import PO modal
// (import-po.js), which reach different endpoints; the caller hands
// wireMirPicker() an `api` object - candidates(q, itemRef), save(body),
// preview(body), previewStatus(id), changes() - rather than this file
// branching on which modal it is running in.

let MIR_PICKER = null;  // { api, poNumber, itemRef, description, mirs, changes, onDone, ... }

function mirPickerHtml() {
  return '<div class="mir-picker" id="mirPicker" hidden>' +
    '<div class="mir-picker-head">' +
      '<h4>Edit MIR receipts</h4>' +
      '<span class="mir-picker-close" id="mirPickerClose" role="button" tabindex="0" title="Close">&times;</span>' +
    '</div>' +
    '<div class="mir-picker-for" id="mirPickerFor"></div>' +
    '<div class="mir-picker-sec">' +
      '<div class="mir-picker-sec-title">Counted on this line</div>' +
      '<div id="mirPickerCurrent"></div>' +
    '</div>' +
    '<div class="mir-preview" id="mirPickerPreview" hidden></div>' +
    '<div class="mir-choice" id="mirPickerChoice" role="group" aria-label="This MIR is already matched" hidden></div>' +
    '<div class="mir-picker-sec">' +
      '<div class="mir-picker-sec-title">Add a receipt</div>' +
      '<div class="row">' +
        '<input type="search" id="mirPickerSearch" placeholder="Search MIR number, party or material" aria-label="Search MIR receipts">' +
      '</div>' +
      '<div class="mir-picker-list" id="mirPickerList"></div>' +
    '</div>' +
    '<div class="row mt-8">' +
      '<button type="button" id="mirPickerNone" class="mir-picker-secondary">No MIR - not received</button>' +
      '<button type="button" id="mirPickerAuto" class="mir-picker-secondary">Back to automatic</button>' +
    '</div>' +
    '<div id="mirPickerStatus" role="status"></div>' +
    '<div class="ov-disclaimer">A manual change survives every re-match and re-sync until someone undoes it. It does not alter the MIR or the PO - only which receipts this line is reconciled against. The lasting fix for a receipt the matcher cannot place is usually its PO number in the MIR sheet.</div>' +
  '</div>';
}

/** Who else currently holds rows of this MIR document, as a sentence. Empty
 * when nothing does. The line item being edited is excluded - adding a
 * document the line already holds is not a collision. */
function mirClaimSummary(candidate, selfPoNumber, selfItemRef) {
  // sharesReceipt: a line of this order on the same Bill of Entry, which
  // keeps its share of a BOE-booked receipt whatever is chosen here
  // (imports_views.mir_candidates()), so it is not a collision.
  const others = (candidate.claimedBy || []).filter(
    c => !(c.poNumber === selfPoNumber && c.itemRef === selfItemRef) && !c.sharesReceipt);
  if (!others.length) return '';
  return others.map(c => 'PO ' + c.poNumber + ', line ' + (Number(c.itemRef) + 1) +
    ' (' + (c.description || 'no description') + ')' + (c.manuallyPinned ? ' - itself a manual match' : '')).join('; ');
}

// The picker's groups, most likely first (manual_receipts.GROUP_ORDER).
const MIR_CANDIDATE_GROUPS = {
  cites: 'Receipts citing this PO',
  vendorMaterial: 'Same vendor and material',
  vendor: 'Same vendor',
  other: 'Other receipts',
};

function renderMirCandidates(candidates) {
  const list = document.getElementById('mirPickerList');
  if (!list) return;
  if (!candidates.length) {
    list.innerHTML = '<div class="empty-note-sm">No MIR entries matched that search.</div>';
    return;
  }
  let lastGroup = null;
  list.innerHTML = candidates.map(c => {
    const claim = mirClaimSummary(c, MIR_PICKER.poNumber, MIR_PICKER.itemRef);
    const heading = c.group && c.group !== lastGroup
      ? '<div class="mir-cand-group">' + escapeHtml(MIR_CANDIDATE_GROUPS[c.group] || 'Other receipts') + '</div>' : '';
    lastGroup = c.group || lastGroup;
    const here = !!c.onThisLine;
    return heading + '<div class="mir-cand' + (here ? ' mir-cand-here' : '') + '" data-mir-no="' + escapeHtml(c.mirNo) + '"' +
        (here ? ' aria-disabled="true"' : ' role="button" tabindex="0"') + '>' +
      '<div class="mir-cand-head"><b>' + escapeHtml(c.mirNo) + '</b>' +
        (c.mirDate ? ' <span class="mir-cand-date">' + escapeHtml(formatDateIN(c.mirDate)) + '</span>' : '') +
        (c.rowCount > 1 ? ' <span class="mir-cand-rows">' + c.rowCount + ' lines</span>' : '') +
        // Where the document sits in the MIR Excel sheet (2026-09-24).
        ((c.sheetRows || []).length ? ' <span class="mir-cand-date">sheet row' + (c.sheetRows.length > 1 ? 's ' : ' ') + escapeHtml(c.sheetRows.join(', ')) + '</span>' : '') +
        (here ? ' <span class="mir-cand-rows">counted on this line</span>' : '') +
      '</div>' +
      '<div class="mir-cand-sub">' + escapeHtml(c.party || 'Unknown party') + ' &middot; ' + escapeHtml(c.material || '') + '</div>' +
      '<div class="mir-cand-sub">' +
        (c.qty != null ? escapeHtml(String(c.qty)) + ' ' + escapeHtml(c.uom || '') : 'qty n/a') +
        (c.rate != null ? ' @ ' + formatInr(c.rate) : '') +
        (c.invoiceNo ? ' &middot; inv ' + escapeHtml(c.invoiceNo) : '') +
        (c.poNumberRaw ? ' &middot; PO column ' + escapeHtml(String(c.poNumberRaw).replace(/\.0+$/, '')) : '') +
      '</div>' +
      ((c.why || []).length ? '<ul class="mir-cand-why">' + c.why.map(w => '<li>' + escapeHtml(w) + '</li>').join('') + '</ul>' : '') +
      (claim ? '<div class="mir-cand-claim">Currently matched to ' + escapeHtml(claim) + '</div>' : '') +
    '</div>';
  }).join('');
  list.querySelectorAll('.mir-cand:not(.mir-cand-here)').forEach(el => {
    const choose = () => chooseMirCandidate(el.dataset.mirNo, candidates);
    el.onclick = choose;
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); } };
  });
}

/** A MIR number as a reader says it: Vapi's already read "MIR43/05", so
 * prefixing "MIR " there printed "MIR MIR43/05". */
function mirLabel(mirNo) {
  return /^mir/i.test(mirNo || '') ? mirNo : 'MIR ' + mirNo;
}

/** What the line counts now, each receipt with its Remove button, and the
 * receipts kept off it by hand with an Undo. */
function renderMirPickerCurrent() {
  const box = document.getElementById('mirPickerCurrent');
  const p = MIR_PICKER;
  if (!box || !p) return;
  const mirs = p.mirs || [];
  const removed = (p.changes || []).filter(c => c.action === 'removed');
  let html = mirs.length
    ? mirs.map(m => {
      const note = manualNoteText(m.manualNote, p.poNumber);
      return '<div class="mir-cur-row">' +
        '<div><b class="mono">' + escapeHtml(m.mirNo || '-') + '</b>' +
          (m.mirDate ? ' <span class="mir-cand-date">' + escapeHtml(formatDateIN(m.mirDate)) + '</span>' : '') +
          ' <span class="text-slate-soft">&middot; ' + reconQty(m.qty) + ' ' + escapeHtml(m.uom || '') + '</span>' +
          (note ? '<div class="mir-note">' + escapeHtml(note) + '</div>' : '') +
        '</div>' +
        '<button type="button" class="mir-picker-secondary" data-remove-mir="' + escapeHtml(m.mirNo || '') + '">Remove</button>' +
      '</div>';
    }).join('')
    : '<div class="empty-note-sm">No receipt is counted on this line.</div>';
  if (removed.length) {
    html += '<div class="mir-picker-sub">Kept off this line by hand</div>' + removed.map(c =>
      '<div class="mir-cur-row mir-cur-removed">' +
        '<div><b class="mono">' + escapeHtml(c.mirNo) + '</b>' +
          '<div class="mir-note">' + escapeHtml(changeByText('Removed', c)) + '</div></div>' +
        '<button type="button" class="mir-picker-secondary" data-undo-edit="' + escapeHtml(String(c.id)) + '" data-mir-no="' + escapeHtml(c.mirNo) + '">Undo</button>' +
      '</div>').join('');
  }
  box.innerHTML = html;
  box.querySelectorAll('[data-remove-mir]').forEach(btn => btn.onclick = () =>
    proposeMirChange({ action: 'remove', mirNo: btn.dataset.removeMir },
      'Remove ' + mirLabel(btn.dataset.removeMir) + ' from this line. The matcher then gives it to whichever line it fits next, or to none.'));
  box.querySelectorAll('[data-undo-edit]').forEach(btn => btn.onclick = () =>
    proposeMirChange({ action: 'undo', undoType: 'edit', undoId: btn.dataset.undoEdit },
      'Undo the removal of ' + mirLabel(btn.dataset.mirNo) + ': the matcher may count it on this line again.'));
}

/** "Removed by Dishant Barot on 29 Sep 2026" for one manual change. */
function changeByText(verb, c) {
  const who = c.byName || c.by || 'someone';
  return verb + ' by ' + who + (c.at ? ' on ' + formatDateIN(localDateOf(c.at)) : '') + (c.reason ? ' - "' + c.reason + '"' : '');
}

/** The popup the project owner asked for, with the three answers they
 * asked for (2026-09-25): keep both, move it here, or cancel. "Move" says the
 * other line is re-matched automatically and may end up without it; "Keep
 * both" says the receipt is then split by ordered quantity where it can be,
 * which is what matching_core's _split_shared_rows() does. Built from DOM
 * nodes, not markup: the claim text carries descriptions from the sheet. */
function chooseMirCandidate(mirNo, candidates) {
  const candidate = candidates.find(c => c.mirNo === mirNo);
  if (!candidate || candidate.onThisLine) return;
  const label = 'Add ' + mirLabel(mirNo) + ' to this line, on top of the receipts it already counts.';
  const claim = mirClaimSummary(candidate, MIR_PICKER.poNumber, MIR_PICKER.itemRef);
  if (!claim) { proposeMirChange({ action: 'add', mirNo: mirNo }, label); return; }
  const box = document.getElementById('mirPickerChoice');
  if (!box) return;
  box.textContent = '';
  const text = document.createElement('p');
  text.textContent = mirLabel(mirNo) + ' is already matched to ' + claim + '.';
  box.appendChild(text);
  const options = [
    { label: 'Keep both', cls: 'mir-choice-primary', hint: 'Both lines use this receipt. Where both count just this one receipt in the same unit, each counts its share by ordered quantity; otherwise each is compared against the whole receipt.', run: () => proposeMirChange({ action: 'add', mirNo: mirNo, share: true }, label + ' The other line keeps it too.') },
    { label: 'Move it here', cls: '', hint: 'Only this line uses it. The other line is re-matched automatically and may end up with no MIR.', run: () => proposeMirChange({ action: 'add', mirNo: mirNo }, label + ' The other line loses it.') },
    { label: 'Cancel', cls: '', hint: '', run: () => {} },
  ];
  const row = document.createElement('div');
  row.className = 'mir-choice-buttons';
  options.forEach(o => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = ('mir-picker-secondary ' + o.cls).trim();
    btn.textContent = o.label;
    btn.onclick = () => { box.hidden = true; box.textContent = ''; o.run(); };
    row.appendChild(btn);
  });
  box.appendChild(row);
  const hints = document.createElement('ul');
  options.filter(o => o.hint).forEach(o => {
    const li = document.createElement('li');
    const b = document.createElement('b');
    b.textContent = o.label + ': ';
    li.appendChild(b);
    li.appendChild(document.createTextNode(o.hint));
    hints.appendChild(li);
  });
  box.appendChild(hints);
  box.hidden = false;
  row.firstChild.focus();
}

// ── Preview before saving ──
// Polls receipt_preview's status the way waitForRematch() polls a re-match.
// Rejects when the worker is not picking the run up or it takes too long -
// the reader can still save without the preview.
const MIR_PREVIEW_POLL_MS = 1500;
const MIR_PREVIEW_WAIT_MS = 90000;
let mirPreviewRequestId = 0;

async function runMirPreview(api, body) {
  const started = await api.preview(body);
  let st = started;
  const deadline = Date.now() + MIR_PREVIEW_WAIT_MS;
  while (st && (st.state === 'queued' || st.state === 'running')) {
    if (st.stalled) throw new Error('the background worker has not started it');
    if (Date.now() > deadline) throw new Error('it is taking longer than usual');
    await new Promise(resolve => setTimeout(resolve, MIR_PREVIEW_POLL_MS));
    st = await api.previewStatus(started.previewId);
  }
  if (!st || st.state === 'failed') throw new Error((st && st.error) || 'the preview failed');
  return st;
}

function previewQtyText(side, uom) {
  if (!side || side.received == null) return '<span class="recon-muted">nothing</span>';
  const pct = side.diffPct;
  const tail = pct == null ? '' : (Math.abs(pct) < 0.005 ? ' (matches)'
    : ' (' + Math.abs(pct).toLocaleString('en-IN', { maximumFractionDigits: 2 }) + '% ' + (pct < 0 ? 'short' : 'over') + ')');
  return escapeHtml(reconQty(side.received) + ' ' + (uom || '') + tail);
}

function previewReceiptsHtml(before, after) {
  const was = new Set(before.receipts || []);
  const now = new Set(after.receipts || []);
  const all = Array.from(new Set([].concat(before.receipts || [], after.receipts || [])))
    .sort((a, b) => a.localeCompare(b, 'en', { numeric: true }));
  if (!all.length) return '<span class="recon-muted">none</span>';
  return all.map(no => {
    if (!was.has(no)) return '<span class="mir-prev-add">+' + escapeHtml(no) + '</span>';
    if (!now.has(no)) return '<span class="mir-prev-drop">&minus;' + escapeHtml(no) + '</span>';
    return '<span class="mir-prev-same">' + escapeHtml(no) + '</span>';
  }).join(' ');
}

/** The preview's table: every line whose receipts or received quantity
 * would move, the edited line first and always. */
function mirPreviewHtml(result) {
  const lines = (result.lines || []).filter(l => l.changed || l.editedLine)
    .sort((a, b) => (b.editedLine - a.editedLine) || (b.samePo - a.samePo));
  const anyChange = lines.some(l => l.changed);
  const rows = lines.map(l => '<tr' + (l.editedLine ? ' class="mir-prev-edited"' : '') + '>' +
    '<td>' + escapeHtml((l.samePo ? 'Line ' + l.line : (l.poKind === 'import' ? 'Import ' : '') + 'PO ' + l.poNumber + ', line ' + l.line)) +
      '<div class="text-slate-soft fs-11">' + escapeHtml(l.description || '') + '</div></td>' +
    '<td>' + previewReceiptsHtml(l.before, l.after) + '</td>' +
    '<td class="num">' + previewQtyText(l.before, l.uom) + '</td>' +
    '<td class="num">' + previewQtyText(l.after, l.uom) + '</td></tr>').join('');
  return (result.unfilled
    ? '<div class="override-status err">That receipt is already fully taken by a line someone set by hand, so this line would not get it. Free it there first.</div>' : '') +
    (anyChange ? '' : '<div class="empty-note-sm">This does not change what any line counts.</div>') +
    (rows ? '<div class="recon-receipts-scroll"><table class="recon-mini mir-prev-table"><thead><tr><th>Line</th><th>Receipts</th>' +
      '<th class="num">Received now</th><th class="num">After this change</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '');
}

/** Shows what `change` would do and lets the reader save it or not. The
 * Save button works while the preview is still running, and when it could
 * not be worked out: the preview informs the decision, it is not a gate. */
async function proposeMirChange(change, label) {
  const p = MIR_PICKER;
  const box = document.getElementById('mirPickerPreview');
  if (!p || !box) return;
  const choice = document.getElementById('mirPickerChoice');
  if (choice) { choice.hidden = true; choice.textContent = ''; }
  const myId = ++mirPreviewRequestId;
  box.innerHTML =
    '<div class="mir-preview-title"></div>' +
    '<div class="mir-preview-body"><div class="empty-note-sm">Working out what this changes - the matcher is running without saving…</div></div>' +
    '<div class="row mt-8"><input type="text" class="mir-preview-reason" maxlength="300" placeholder="Why (optional) - shown with the change" aria-label="Reason for this change"></div>' +
    '<div class="mir-choice-buttons">' +
      '<button type="button" class="mir-picker-secondary mir-choice-primary" data-preview-save>Save this change</button>' +
      '<button type="button" class="mir-picker-secondary" data-preview-cancel>Cancel</button>' +
    '</div>';
  box.querySelector('.mir-preview-title').textContent = label;
  box.hidden = false;
  if (box.scrollIntoView) box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const reasonEl = box.querySelector('.mir-preview-reason');
  box.querySelector('[data-preview-save]').onclick = () => {
    ++mirPreviewRequestId;
    box.hidden = true;
    applyMirMatch(Object.assign({}, change, { reason: reasonEl.value.trim() }));
  };
  box.querySelector('[data-preview-cancel]').onclick = () => { ++mirPreviewRequestId; box.hidden = true; };
  let html;
  try {
    const result = await runMirPreview(p.api, mirChangeBody(p, change));
    html = mirPreviewHtml(result);
  } catch (e) {
    html = '<div class="override-status err">Could not work out the effect (' + escapeHtml(e.message) + '). You can still save the change.</div>';
  }
  if (myId !== mirPreviewRequestId || MIR_PICKER !== p) return;
  const bodyEl = box.querySelector('.mir-preview-body');
  if (bodyEl) bodyEl.innerHTML = html;
}

function mirChangeBody(p, change) {
  return {
    itemRef: p.itemRef,
    action: change.action,
    mirNo: change.mirNo || '',
    share: !!change.share,
    reason: change.reason || '',
    undoType: change.undoType || '',
    undoId: change.undoId || '',
  };
}

const MIR_CHANGE_DONE_TEXT = {
  add: (b) => 'Added ' + mirLabel(b.mirNo) + ' to this line' + (b.share ? ', shared with the line that already had it.' : '.'),
  remove: (b) => 'Removed ' + mirLabel(b.mirNo) + ' from this line.',
  notReceived: () => 'Marked as not received.',
  auto: () => 'Back to automatic matching.',
  undo: () => 'Change undone.',
};

async function applyMirMatch(opts) {
  const status = document.getElementById('mirPickerStatus');
  const p = MIR_PICKER;
  if (!p || !status) return;
  status.className = '';
  status.textContent = 'Saving…';
  const body = mirChangeBody(p, opts);
  try {
    const res = await saveMirChange(p.api, body, (text) => { if (MIR_PICKER === p) status.textContent = text; });
    if (res.stalled) {
      status.className = 'override-status err';
      status.textContent = rematchStalledText();
      return;
    }
    if (res.slow) {
      status.className = 'override-status ok';
      status.textContent = 'Saved. The re-match is taking longer than usual; the page will update itself when it finishes.';
      return;
    }
    // A pin or an added receipt is saved but could not be applied: every row
    // of that document is already held by a newer manual choice
    // (run_full_match()'s manual_pins_unfilled / manual_edits_unfilled). The
    // line is not given it behind the reader's back, so they must be told.
    const unfilled = body.mirNo && (res.unfilledPins || []).concat(res.unfilledEdits || []).some(u =>
      String(u.poNumber) === String(p.poNumber) && String(u.itemRef) === String(p.itemRef) && u.mirNo === body.mirNo);
    if (unfilled) {
      status.className = 'override-status err';
      status.textContent = 'Saved, but ' + mirLabel(body.mirNo) + ' is already fully taken by another line set by hand, so this line does not get it. Pick a different MIR or free that one first.';
    } else {
      status.className = 'override-status ok';
      status.textContent = (MIR_CHANGE_DONE_TEXT[body.action] || (() => 'Saved.'))(body);
    }
    announce(status.textContent);
    // A change re-runs the whole plant's matching, so other rows can move
    // too - the caller reloads the list, not just this modal.
    if (p.onDone) await p.onDone();
    // onDone() re-renders the modal, taking the status line with it - so an
    // unapplied change is also said in a dialog that survives the reload.
    if (unfilled) window.alert(status.textContent);
  } catch (e) {
    status.className = 'override-status err';
    status.textContent = 'Could not save: ' + e.message;
  }
}

/** Saves one change and follows its background re-match. Resolves with the
 * save response merged with the finished run's unfilled lists, or with
 * {stalled} / {slow} when the run did not finish. `progress` gets the
 * interim status text. Shared by the panel and the Flags tab's Undo. */
async function saveMirChange(api, body, progress) {
  let res = await api.save(body);
  // The re-match runs on the background worker (apps/services/rematch.py),
  // so the change is saved now and the result comes a little later.
  if (rematchPending(res && res.rematch)) {
    if (progress) progress('Saved. Re-matching in the background - this line updates in a moment…');
    const done = await waitForRematch(api.plantKey);
    if (done && done.stalled) return Object.assign({}, res, { stalled: true });
    if (!done) return Object.assign({}, res, { slow: true });
    res = Object.assign({}, res, { unfilledPins: done.unfilledPins || [], unfilledEdits: done.unfilledEdits || [] });
  }
  return res;
}

function closeMirPicker() {
  const panel = document.getElementById('mirPicker');
  if (panel) panel.hidden = true;
  document.querySelectorAll('.mir-editing').forEach(el => el.classList.remove('mir-editing'));
  ++mirPreviewRequestId;
  MIR_PICKER = null;
}

// Stale-response guard for the picker's search, same pattern as
// modalRequestId: a slower earlier search, or the previous line's list after
// a quick switch, must not overwrite the newer one.
let mirCandidatesRequestId = 0;

async function loadMirCandidates(q) {
  const list = document.getElementById('mirPickerList');
  if (!list || !MIR_PICKER) return;
  const myRequestId = ++mirCandidatesRequestId;
  const picker = MIR_PICKER;
  list.innerHTML = '<div class="empty-note-sm">Loading&hellip;</div>';
  try {
    const data = await picker.api.candidates(q, picker.itemRef);
    if (myRequestId !== mirCandidatesRequestId || MIR_PICKER !== picker) return;
    renderMirCandidates(data.candidates || []);
  } catch (e) {
    if (myRequestId !== mirCandidatesRequestId) return;
    list.innerHTML = '<div class="empty-note-sm">Could not load MIR entries: ' + escapeHtml(e.message) + '</div>';
  }
}

/** Opens the panel for one line item, anchored under its own card (or, for
 * a caller still rendering a table, under that table). */
function openMirPicker(ctx) {
  const panel = document.getElementById('mirPicker');
  if (!panel) return;
  MIR_PICKER = ctx;
  ++mirPreviewRequestId;
  panel.hidden = false;
  const anchor = ctx.rowEl && (ctx.rowEl.closest('.recon-line') || ctx.rowEl.closest('.table-wrap') || ctx.rowEl.closest('table'));
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor.nextSibling);
  document.querySelectorAll('.mir-editing').forEach(el => el.classList.remove('mir-editing'));
  if (ctx.rowEl) ctx.rowEl.classList.add('mir-editing');
  document.getElementById('mirPickerFor').textContent =
    'Line ' + (Number(ctx.itemRef) + 1) + ': ' + (ctx.description || 'no description') +
    (ctx.manuallyPinned ? ' - its receipt was set by hand' : '');
  document.getElementById('mirPickerStatus').textContent = '';
  document.getElementById('mirPickerStatus').className = '';
  ['mirPickerChoice', 'mirPickerPreview'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.hidden = true; el.textContent = ''; }
  });
  renderMirPickerCurrent();
  // The line's own manual changes, for "Kept off this line by hand".
  if (ctx.api.changes) {
    ctx.api.changes().then(data => {
      if (MIR_PICKER !== ctx) return;
      const own = ((data && data.lines) || []).find(l => String(l.itemRef) === String(ctx.itemRef));
      ctx.changes = own ? own.changes : [];
      renderMirPickerCurrent();
    }).catch(() => {});
  }
  const search = document.getElementById('mirPickerSearch');
  search.value = '';
  loadMirCandidates('');
  search.focus();
  if (panel.scrollIntoView) panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/** Wires the panel's own controls plus every "edit receipts" link under
 * `container`. Called once per modal render, same as wireEditIcons().
 * `api` is described in this section's header comment. */
function wireMirPicker(container, api, poNumber, onDone) {
  MIR_PICKER = null;
  const search = container.querySelector('#mirPickerSearch');
  if (search) {
    // debounceRender() is a FACTORY - it returns the debounced function, so
    // it is called once here rather than per keystroke (calling it inside
    // the handler would build a fresh timer each time and debounce nothing).
    // 250ms, matching search-po's own box: this one awaits a fetch too.
    search.oninput = debounceRender(() => loadMirCandidates(search.value.trim()), 250);
  }
  const closeBtn = container.querySelector('#mirPickerClose');
  if (closeBtn) {
    closeBtn.onclick = closeMirPicker;
    closeBtn.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); closeMirPicker(); } };
  }
  const noneBtn = container.querySelector('#mirPickerNone');
  if (noneBtn) {
    noneBtn.onclick = () => proposeMirChange({ action: 'notReceived' },
      'Record that this line has NO matching MIR entry. It stays unmatched, and is not re-matched automatically, until someone undoes it.');
  }
  const autoBtn = container.querySelector('#mirPickerAuto');
  if (autoBtn) {
    autoBtn.onclick = () => proposeMirChange({ action: 'auto' },
      'Back to automatic: remove every manual change on this line and let the matcher decide again.');
  }
  container.querySelectorAll('.mir-change-link').forEach(el => {
    const open = () => {
      const line = (PO_RECON_LINES || []).find(l => String(l.itemRef) === String(el.dataset.itemRef));
      openMirPicker({
        api: api,
        poNumber: poNumber,
        itemRef: el.dataset.itemRef,
        description: el.dataset.description,
        manuallyPinned: el.dataset.pinned === '1',
        mirs: line && line.matched ? (line.mirs || []) : [],
        changes: [],
        rowEl: el.closest('.recon-line') || el.closest('tr'),
        onDone: onDone,
      });
    };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  });
}

// ── Manual MIR changes, on the Flags & Corrections tab ──
// Every manual receipt decision on this order's lines, who made it and when,
// with an Undo each (project owner, 2026-09-29: a pin was invisible except
// for a "manual" tag, and the tab's "revert" links - which undo field
// corrections only - read as if they would undo it). Plus decisions on OTHER
// orders' lines that took a receipt citing this order. Loaded after the
// modal renders, into manualChangesSectionHtml()'s placeholder.

function manualChangesSectionHtml() {
  return '<div id="manualChangesBox"><div class="section-title mt-18">Manual MIR changes</div>' +
    '<div class="empty-note-sm">Loading&hellip;</div></div>';
}

const MANUAL_CHANGE_VERB = {
  pinned: (c) => 'Set ' + mirLabel(c.mirNo) + ' as this line\'s receipt' + (c.shared ? ' (kept on the other line too)' : ''),
  notReceived: () => 'Marked as not received',
  added: (c) => 'Added ' + mirLabel(c.mirNo) + (c.shared ? ' (kept on the other line too)' : ''),
  removed: (c) => 'Removed ' + mirLabel(c.mirNo),
};

async function loadManualChanges(body, api, plantKey, onDone, requestId) {
  const box = body.querySelector('#manualChangesBox');
  if (!box || !api.changes) return;
  let data;
  try {
    data = await api.changes();
  } catch (e) {
    if (requestId !== modalRequestId) return;
    box.innerHTML = '<div class="section-title mt-18">Manual MIR changes</div><div class="empty-note-sm">Could not load them: ' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (requestId !== modalRequestId || !box.isConnected) return;
  const canEdit = canEditField(plantKey);
  const rows = [];
  (data.lines || []).forEach(l => (l.changes || []).forEach(c => rows.push({ l, c })));
  const own = rows.map(({ l, c }) =>
    '<div class="field-block mb-8 manual-change">' +
      '<div class="fs-12-5"><b>Line ' + escapeHtml(String(l.line || '?')) + '</b> ' +
        '<span class="text-slate-soft">' + escapeHtml(l.description || (l.missingLine ? 'line no longer on this PO' : '')) + '</span>: ' +
        escapeHtml((MANUAL_CHANGE_VERB[c.action] || (() => c.action))(c)) +
        (c.stale ? ' <span class="pinned-tag" title="The PO\'s lines changed after this was set, so the matcher is ignoring it.">not applied - line changed</span>' : '') +
        (canEdit ? '<span class="revert-link" data-undo-type="' + escapeHtml(c.type) + '" data-undo-id="' + escapeHtml(String(c.id)) + '" data-item-ref="' + escapeHtml(l.itemRef) + '">undo</span>' : '') +
      '</div>' +
      (c.reason ? '<div class="mt-4 fs-12 italic text-slate">"' + escapeHtml(c.reason) + '"</div>' : '') +
      '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.byName || c.by || 'unknown') + ' &middot; ' + escapeHtml(formatDateIN(c.at ? localDateOf(c.at) : null)) + '</div>' +
    '</div>').join('');
  const elsewhere = (data.elsewhere || []).map(c =>
    '<div class="field-block mb-8 manual-change">' +
      '<div class="fs-12-5">' + escapeHtml(mirLabel(c.mirNo)) + ' cites this PO but was ' +
        escapeHtml(c.action === 'added' ? 'added' : 'set by hand') + ' onto ' + escapeHtml((c.poKind === 'import' ? 'Import ' : '') + 'PO ' + c.poNumber + ', line ' + (c.line || '?')) +
        (c.description ? ' <span class="text-slate-soft">(' + escapeHtml(c.description) + ')</span>' : '') + '</div>' +
      '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.byName || c.by || 'unknown') + ' &middot; ' + escapeHtml(formatDateIN(c.at ? localDateOf(c.at) : null)) + '</div>' +
    '</div>').join('');
  box.innerHTML = '<div class="section-title mt-18">Manual MIR changes</div>' +
    (own || '<div class="empty-note-sm">No MIR receipt on this PO has been changed by hand.</div>') +
    (elsewhere ? '<div class="section-title mt-18">This PO\'s receipts placed on other orders by hand</div>' + elsewhere : '') +
    '<div id="manualChangesStatus" role="status"></div>';
  box.querySelectorAll('[data-undo-id]').forEach(link => link.onclick = async () => {
    if (!window.confirm('Undo this manual change? The matcher then decides that receipt again, which can move it to another line.')) return;
    const statusEl = box.querySelector('#manualChangesStatus');
    statusEl.className = '';
    statusEl.textContent = 'Undoing…';
    try {
      const res = await saveMirChange(api, {
        itemRef: link.dataset.itemRef, action: 'undo', undoType: link.dataset.undoType, undoId: link.dataset.undoId,
      }, (text) => { if (statusEl.isConnected) statusEl.textContent = text; });
      if (res.stalled) { window.alert(rematchStalledText()); }
      if (onDone) await onDone();
    } catch (e) {
      if (statusEl.isConnected) {
        statusEl.className = 'override-status err';
        statusEl.textContent = 'Could not undo: ' + e.message;
      }
    }
  });
}
