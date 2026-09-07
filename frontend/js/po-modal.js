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

  const itemsHtml = (po.items || []).length
    ? '<table class="items-table"><thead><tr><th>Description</th><th>Qty</th><th>UOM</th><th>Net Price</th><th>Delivery Date</th><th>MIR Matched</th><th>Material Analysis</th></tr></thead><tbody>' +
        po.items.map(it => {
          // Domestic line items have no stable item_id in a meaningful
          // number of real rows (see DomesticPOCorrection's docstring) -
          // itemId isn't part of _line_item_dict() at all today, so item-
          // level edits aren't wired up yet; only PO-level fields are
          // editable for now (Overview tab below).
          return '<tr><td>' + escapeHtml(it.description || '') + '</td><td>' + (it.qty != null ? it.qty : '-') +
          '</td><td>' + escapeHtml(it.uom || '') + '</td><td>' + (it.netPrice != null ? formatInr(it.netPrice) : '-') +
          '</td><td>' + escapeHtml(formatDateIN(it.deliveryDate)) + '</td><td>' +
          matchStatusHtml(it, plantKey) +
          '</td><td>' + (materialAnalysisLinkHtml(it.description, po.vendorName, plantKey) || '<span style="color:var(--slate-soft);">Not tracked in Stock</span>') +
          '</td></tr>';
        }).join('') + '</tbody></table>'
    : '<div style="font-size:12.5px;color:var(--slate-soft);">No line items recorded.</div>';

  const currencyOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.currency);
  const incotermsOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.incoterms);
  const taxTypeOptions = distinctFieldValues(PURCHASE_ORDERS_BY_PLANT[plantKey] || [], p => p.taxType);

  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Purchase Order</h4>' +
        '<div class="line">PO No: ' + escapeHtml(po.poNumber) + '</div>' +
        edit('Created on', po.createdDate, 'po_created_date', null, 'date') +
        '<div class="line">Plant: ' + escapeHtml(PLANTS[plantKey].label) + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>Vendor</h4>' +
        edit('Vendor Name', po.vendorName, 'vendor_name') +
        edit('Vendor Address', po.vendorAddress, 'vendor_address') +
        edit('Vendor GSTIN', po.vendorGstin, 'vendor_gstin') +
        edit('Vendor Email', po.vendorEmail, 'vendor_email') +
        edit('Vendor Code', po.vendorCode, 'vendor_code') +
      '</div>' +
      '<div class="field-block"><h4>Billing and Ship To</h4>' +
        edit('Billing Address', po.billingAddress, 'billing_address') +
        edit('Ship To', po.shipTo, 'ship_to') +
      '</div>' +
      '<div class="field-block"><h4>Packing and Incoterms</h4>' +
        edit('Payment Terms', po.paymentTerms, 'payment_terms') +
        edit('Incoterms', po.incoterms, 'incoterms', null, 'select', incotermsOptions) +
        edit('Currency', po.currency, 'currency', null, 'select', currencyOptions) +
      '</div>' +
      '<div class="field-block"><h4>Value</h4>' +
        edit('Total Value', po.totalValue, 'total_value', null, 'number') +
        edit('Tax Type', po.taxType, 'tax_type', null, 'select', taxTypeOptions) +
        edit('Total Inclusive Value', po.totalInclTax, 'total_inclusive_value', null, 'number') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width" style="margin-top:14px;"><h4>Remarks</h4>' +
      edit('Remarks', po.remarks, 'remarks') + '</div>';

  const itemStockHtml =
    '<div style="margin:0 0 16px;">' + miniStepperHtml(po) + '</div>' +
    '<div class="section-title" style="margin-top:0;">Material / Product Details</div>' + itemsHtml;

  // Match Accuracy Programme fix 3.G: arithmetic-mismatch flags
  // (dataQualityFlagHtml, flags.js) alongside the existing remarks-derived
  // categories (poFlagHtml) - a real source-sheet typo, not a matching
  // artifact, so it's shown even when po._categories is empty.
  const arithmeticFlagsHtml = (po.dataQualityFlags || []).map(dataQualityFlagHtml).join('');
  const catsHtml = (po._categories || []).length || arithmeticFlagsHtml
    ? po._categories.map(c => poFlagHtml(c, po, plantKey)).join('') + arithmeticFlagsHtml
    : '<div style="text-align:center;color:var(--slate-soft);padding:14px;">No data quality flags on this PO.</div>';
  const correctionsHtml = (po.corrections || []).length
    ? '<div class="section-title" style="margin-top:18px;">Correction History</div>' +
      po.corrections.map(c =>
        '<div class="field-block" style="margin-bottom:8px;">' +
          '<div style="font-size:12.5px;"><b>' + escapeHtml(c.fieldName) + (c.itemId ? ' (item ' + escapeHtml(c.itemId) + ')' : '') + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') + '</div>' +
          (c.reason ? '<div style="margin-top:4px;font-size:12px;font-style:italic;color:var(--slate);">"' + escapeHtml(c.reason) + '"</div>' : '') +
          '<div style="margin-top:4px;font-size:11px;color:var(--slate-soft);">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? c.correctedAt.slice(0, 10) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const flagsTabHtml = catsHtml + correctionsHtml +
    overrideBoxHtml('Click the ✎ icon next to any field in the Overview or Item & Stock tab to correct it - no need to know column names.');

  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
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
  wireDismissLinks(body, plantKey, () => onDomesticFieldSaved(plantKey, poNumber));
  body.querySelectorAll('[data-material-link]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.materialLink));
}

