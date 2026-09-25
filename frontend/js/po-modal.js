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
  try {
    await ensureMaterialsLoaded(PLANT_KEYS);
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
  const flagsTabHtml = catsHtml + correctionsHtml +
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
  // A manual MIR pin re-runs the whole plant's matching, so other POs'
  // badges can move too - the same full invalidate+reload a field
  // correction already does, not a modal-only refresh.
  wireMirPicker(body, {
    candidates: (q) => apiForPlant(plantKey,
      '/purchase-orders/' + encodeURIComponent(poNumber) + '/mir-candidates' +
      (q ? '?q=' + encodeURIComponent(q) : '')),
    save: (payload) => apiForPlant(plantKey,
      '/purchase-orders/' + encodeURIComponent(poNumber) + '/mir-match',
      { method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  }, poNumber, () => onDomesticFieldSaved(plantKey, poNumber));
  wireDismissLinks(body, plantKey, () => onDomesticFieldSaved(plantKey, poNumber));
  applyDynamicStyles(body); // the reconciliation cards' progress bars
  body.querySelectorAll('[data-material-link]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.materialLink));
}

// ── Change MIR match ────────────────────────────────────────────────────
// "It would be great if we can edit the MIR number too, and if that was
// assigned to some other PO then a pop up would appear telling that matching
// with this would break so and so" (project owner, 2026-09-21).
//
// A picker, not a free-text box. Typing a MIR number blind is how you pin a
// line to a document that does not exist, or to the wrong one with the same
// digits - the list shows date, party, material and qty/rate so the reader
// can confirm they have the right receipt before committing, and it is the
// only place the collision warning can come from (only the backend knows
// which other line items hold that document's rows).
//
// It names a MIR NUMBER, not a row - see ManualMirMatch's docstring for why.
// The panel moves to sit under the items table, same "come to the thing you
// clicked" behaviour as the correction box (shared.js's
// _moveOverrideBoxTo()).
//
// Shared by the Domestic PO modal (this file) and the Import PO modal
// (import-po.js), which reach genuinely different endpoints: Domestic's is
// per-plant-prefixed, Import's carries the plant as a path segment under
// one cross-plant router. The caller therefore hands wireMirPicker() an
// `api` object with `candidates(q)` and `save(body)`, rather than this
// file branching on which modal it is running in.

let MIR_PICKER = null;  // { api, poNumber, itemRef, description, onDone, ... }

function mirPickerHtml() {
  return '<div class="mir-picker" id="mirPicker" hidden>' +
    '<div class="mir-picker-head">' +
      '<h4>Change MIR match</h4>' +
      '<span class="mir-picker-close" id="mirPickerClose" role="button" tabindex="0" title="Close">&times;</span>' +
    '</div>' +
    '<div class="mir-picker-for" id="mirPickerFor"></div>' +
    '<div class="row">' +
      '<input type="search" id="mirPickerSearch" placeholder="Search MIR number, party or material">' +
    '</div>' +
    '<div class="mir-picker-list" id="mirPickerList"></div>' +
    '<div class="row mt-8">' +
      '<button type="button" id="mirPickerNone" class="mir-picker-secondary">No MIR - not received</button>' +
      '<button type="button" id="mirPickerAuto" class="mir-picker-secondary">Back to automatic</button>' +
    '</div>' +
    '<div class="mir-choice" id="mirPickerChoice" role="group" aria-label="This MIR is already matched" hidden></div>' +
    '<div id="mirPickerStatus"></div>' +
    '<div class="ov-disclaimer">A manual match survives every re-match and re-sync until someone changes it back. It does not alter the MIR or the PO - only which receipt this line is reconciled against.</div>' +
  '</div>';
}

/** Who else currently holds rows of this MIR document, as a sentence. Empty
 * when nothing does. The line item being edited is excluded - re-pinning a
 * line to the document it already holds is not a collision. */
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

function renderMirCandidates(candidates) {
  const list = document.getElementById('mirPickerList');
  if (!list) return;
  if (!candidates.length) {
    list.innerHTML = '<div class="empty-note-sm">No MIR entries matched that search.</div>';
    return;
  }
  list.innerHTML = candidates.map(c => {
    const claim = mirClaimSummary(c, MIR_PICKER.poNumber, MIR_PICKER.itemRef);
    return '<div class="mir-cand" data-mir-no="' + escapeHtml(c.mirNo) + '" role="button" tabindex="0">' +
      '<div class="mir-cand-head"><b>' + escapeHtml(c.mirNo) + '</b>' +
        (c.mirDate ? ' <span class="mir-cand-date">' + escapeHtml(formatDateIN(c.mirDate)) + '</span>' : '') +
        (c.rowCount > 1 ? ' <span class="mir-cand-rows">' + c.rowCount + ' lines</span>' : '') +
        // Where the document sits in the MIR Excel sheet (2026-09-24).
        ((c.sheetRows || []).length ? ' <span class="mir-cand-date">sheet row' + (c.sheetRows.length > 1 ? 's ' : ' ') + escapeHtml(c.sheetRows.join(', ')) + '</span>' : '') +
      '</div>' +
      '<div class="mir-cand-sub">' + escapeHtml(c.party || 'Unknown party') + ' &middot; ' + escapeHtml(c.material || '') + '</div>' +
      '<div class="mir-cand-sub">' +
        (c.qty != null ? escapeHtml(String(c.qty)) + ' ' + escapeHtml(c.uom || '') : 'qty n/a') +
        (c.rate != null ? ' @ ' + formatInr(c.rate) : '') +
        (c.invoiceNo ? ' &middot; inv ' + escapeHtml(c.invoiceNo) : '') +
      '</div>' +
      (claim ? '<div class="mir-cand-claim">Currently matched to ' + escapeHtml(claim) + '</div>' : '') +
    '</div>';
  }).join('');
  list.querySelectorAll('.mir-cand').forEach(el => {
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

/** The popup the project owner asked for, with the three answers they
 * asked for (2026-09-25): keep both, move it here, or cancel. Names exactly
 * what each costs - "Move" says the other line is re-matched automatically
 * and may end up unmatched, because the matcher really does get another go
 * at it; "Keep both" says the receipt is then split by ordered quantity,
 * which is what matching_core's _split_shared_rows() does. Built from DOM
 * nodes, not markup: the claim text carries descriptions from the sheet. */
function chooseMirCandidate(mirNo, candidates) {
  const candidate = candidates.find(c => c.mirNo === mirNo);
  if (!candidate) return;
  const claim = mirClaimSummary(candidate, MIR_PICKER.poNumber, MIR_PICKER.itemRef);
  if (!claim) { applyMirMatch({ mirNo: mirNo }); return; }
  const box = document.getElementById('mirPickerChoice');
  if (!box) return;
  box.textContent = '';
  const text = document.createElement('p');
  text.textContent = mirLabel(mirNo) + ' is already matched to ' + claim + '.';
  box.appendChild(text);
  const options = [
    { label: 'Keep both', cls: 'mir-choice-primary', hint: 'Both lines use this receipt. Where both count just this one receipt in the same unit, each counts its share by ordered quantity; otherwise each is compared against the whole receipt.', run: () => applyMirMatch({ mirNo: mirNo, share: true }) },
    { label: 'Move it here', cls: '', hint: 'Only this line uses it. The other line is re-matched automatically and may end up with no MIR.', run: () => applyMirMatch({ mirNo: mirNo }) },
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

async function applyMirMatch(opts) {
  const status = document.getElementById('mirPickerStatus');
  const p = MIR_PICKER;
  if (!p) return;
  status.className = '';
  status.textContent = 'Saving…';
  const body = {
    itemRef: p.itemRef,
    mirNo: opts.mirNo || '',
    clear: !!opts.clear,
    share: !!opts.share,
    reason: opts.reason || '',
  };
  try {
    const res = await p.api.save(body);
    // The pin is saved but could not be applied: every row of that MIR
    // document is already held by a newer pin (run_full_match()'s
    // manual_pins_unfilled). The line is left unmatched, never auto-matched
    // behind the reader's back, so they must be told rather than shown
    // "Matched".
    const unfilled = body.mirNo && ((res && res.unfilledPins) || []).some(u =>
      String(u.poNumber) === String(p.poNumber) && String(u.itemRef) === String(p.itemRef) && u.mirNo === body.mirNo);
    if (unfilled) {
      status.className = 'override-status err';
      status.textContent = 'Saved, but ' + mirLabel(body.mirNo) + ' is already fully taken by another pinned line, so this line is now unmatched. Pick a different MIR or free that one first.';
    } else {
      status.className = 'override-status ok';
      status.textContent = opts.clear ? 'Back to automatic matching.'
        : (body.mirNo ? 'Matched to ' + mirLabel(body.mirNo) + (body.share ? ', shared with the line that already had it.' : '.') : 'Marked as not received.');
    }
    announce(status.textContent);
    // A pin re-runs the whole plant's matching, so other rows can move too -
    // the caller reloads the list, not just this modal.
    if (p.onDone) await p.onDone();
    // onDone() re-renders the modal, taking the status line with it - so an
    // unapplied pin is also said in a dialog that survives the reload.
    if (unfilled) window.alert(status.textContent);
  } catch (e) {
    status.className = 'override-status err';
    status.textContent = 'Could not save: ' + e.message;
  }
}

function closeMirPicker() {
  const panel = document.getElementById('mirPicker');
  if (panel) panel.hidden = true;
  document.querySelectorAll('.mir-editing').forEach(el => el.classList.remove('mir-editing'));
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

/** Opens the picker for one line item, anchored under its own card (or,
 * for a caller still rendering a table, under that table). */
function openMirPicker(ctx) {
  const panel = document.getElementById('mirPicker');
  if (!panel) return;
  MIR_PICKER = ctx;
  panel.hidden = false;
  const anchor = ctx.rowEl && (ctx.rowEl.closest('.recon-line') || ctx.rowEl.closest('.table-wrap') || ctx.rowEl.closest('table'));
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor.nextSibling);
  document.querySelectorAll('.mir-editing').forEach(el => el.classList.remove('mir-editing'));
  if (ctx.rowEl) ctx.rowEl.classList.add('mir-editing');
  document.getElementById('mirPickerFor').textContent =
    'Line ' + (Number(ctx.itemRef) + 1) + ': ' + (ctx.description || 'no description') +
    ' - currently ' + (ctx.currentMir ? mirLabel(ctx.currentMir) : 'not matched') +
    (ctx.manuallyPinned ? ' (set by hand)' : '');
  document.getElementById('mirPickerStatus').textContent = '';
  document.getElementById('mirPickerStatus').className = '';
  const choice = document.getElementById('mirPickerChoice');
  if (choice) { choice.hidden = true; choice.textContent = ''; }
  const search = document.getElementById('mirPickerSearch');
  search.value = '';
  loadMirCandidates('');
  search.focus();
  if (panel.scrollIntoView) panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/** Wires the picker's own controls plus every "change" link under
 * `container`. Called once per modal render, same as wireEditIcons().
 * `api` is { candidates(q) -> {candidates:[...]}, save(body) } - see this
 * section's header comment for why the endpoint is injected. */
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
    noneBtn.onclick = () => {
      if (!window.confirm('Record that this line has NO matching MIR entry?\n\nIt will stay unmatched until someone changes it back, and will not be re-matched automatically.')) return;
      applyMirMatch({ mirNo: '' });
    };
  }
  const autoBtn = container.querySelector('#mirPickerAuto');
  if (autoBtn) {
    autoBtn.onclick = () => {
      if (!window.confirm('Remove the manual match and let the matcher decide again?')) return;
      applyMirMatch({ clear: true });
    };
  }
  container.querySelectorAll('.mir-change-link').forEach(el => {
    const open = () => openMirPicker({
      api: api,
      poNumber: poNumber,
      itemRef: el.dataset.itemRef,
      description: el.dataset.description,
      currentMir: el.dataset.currentMir || '',
      manuallyPinned: el.dataset.pinned === '1',
      rowEl: el.closest('.recon-line') || el.closest('tr'),
      onDone: onDone,
    });
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  });
}
