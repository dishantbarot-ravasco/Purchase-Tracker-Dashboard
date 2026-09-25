// ── Material modal ───────────────────────────────────────────────────────
// compositeKey is "<plantKey>::<lotId>" (see plantKeyFor()) - resolves the
// anchor lot against that specific plant (Stock-lot ids are per-plant
// autoincrement PKs, NOT unique across plants), then rolls up to every
// plant regardless of which plant tab was open when this was clicked - see
// the file header/CLAUDE.md for why this modal alone, unlike the rest of
// this view, always aggregates across all three plants.
//
// "order::<normalized description>" opens a material that is on an open order
// but has no Stock lot anywhere in the current view (materials.js's
// orderOnlyMaterials()). It has no lot to anchor on, so the anchor is built
// from the open lines themselves - see resolveOrderOnlyAnchor() - and the two
// things that need a real lot (the Category pencil, the stock-trend fetch)
// are skipped. Everything else reads the same data either way.
function resolveOrderOnlyAnchor(normKey) {
  let anchor = null;
  const vendorSeen = new Set();
  PLANT_KEYS.forEach(key => materialOrders(key).forEach(po => (po.items || []).forEach(item => {
    if (normalizeMaterial(item.description) !== normKey || !isOpenPoLine(po, item)) return;
    if (!anchor) anchor = { description: item.description, materialCode: '', category: item.category || '', subCategory: item.subCategory || '', vendors: [], orderOnly: true, _plantKey: key };
    const nv = normalizeVendor(po.vendorName);
    if (nv && !vendorSeen.has(nv)) { vendorSeen.add(nv); anchor.vendors.push(po.vendorName); }
  })));
  return anchor;
}

async function openMaterialModal(compositeKey) {
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const isOrderOnly = compositeKey.slice(0, sep) === 'order';
  let plantKey = compositeKey.slice(0, sep);
  const lotId = isOrderOnly ? null : Number(compositeKey.slice(sep + 2));
  let anchor = null;
  if (isOrderOnly) {
    // Orders for every plant, not just the open tab's - the lookup below
    // spans all three, same as the rest of this modal.
    try { await Promise.all([ensurePOsLoaded(PLANT_KEYS), ensureImportPOsLoaded()]); } catch (e) { console.error('openMaterialModal order lookup failed:', e); return; }
    if (myModalRequestId !== modalRequestId) return;
    anchor = resolveOrderOnlyAnchor(compositeKey.slice(sep + 2));
    if (anchor) plantKey = anchor._plantKey;
  } else {
    anchor = (MATERIALS_BY_PLANT[plantKey] || []).find(m => m.lotId === lotId);
  }
  if (!anchor) return;

  // Reopening on a different lot without closing first (e.g. clicking
  // another drill-down chart bar) would otherwise leak the previous open's
  // Chart.js instances - clear them before this open creates its own.
  destroyModalCharts();

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
  body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="load-banner"><div class="spinner"></div><div>Loading material analysis&hellip;</div></div>';

  try {
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS), ensurePOsLoaded(PLANT_KEYS), ensureImportPOsLoaded()]);
  } catch (e) {
    console.error('openMaterialModal cross-plant load failed:', e);
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="noaccess">Couldn\'t load the full material analysis right now. Please refresh, or contact IT if this keeps happening.</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one

  // Sibling lots: the same material (exact normalized-description match,
  // deliberately stricter than the PO linkage below - this asserts "the
  // same material", not just "related") across all 3 plants.
  const normAnchor = normalizeMaterial(anchor.description);
  const siblingLots = [];
  PLANT_KEYS.forEach(key => {
    (MATERIALS_BY_PLANT[key] || []).forEach(lot => {
      if (normalizeMaterial(lot.description) === normAnchor) siblingLots.push(Object.assign({}, lot, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });

  const linked = linkedPoItemsForMaterial(anchor, PLANT_KEYS);
  linked.forEach(l => {
    l.po._status = l.po._status || computeStatus(l.po);
    l.po._deliveryDate = l.po._deliveryDate || computePoDeliveryDate(l.po);
    if (!l.po._categories) computePoFlags(l.po);
  });
  const openLinked = linked.filter(l => isOpenPoLine(l.po, l.item))
    .sort((a, b) => (a.po._deliveryDate || '').localeCompare(b.po._deliveryDate || ''));

  const vendorSeen = new Set();
  const vendors = [];
  siblingLots.forEach(lot => { if (lot.vendor) { const n = normalizeVendor(lot.vendor); if (n && !vendorSeen.has(n)) { vendorSeen.add(n); vendors.push(lot.vendor); } } });
  linked.forEach(l => { if (l.po.vendorName) { const n = normalizeVendor(l.po.vendorName); if (n && !vendorSeen.has(n)) { vendorSeen.add(n); vendors.push(l.po.vendorName); } } });

  const knownDescs = new Set([normAnchor, ...siblingLots.map(l => normalizeMaterial(l.description))]);
  const aliasSeen = new Set();
  const aliases = [];
  linked.forEach(l => {
    const n = normalizeMaterial(l.item.description);
    if (n && !knownDescs.has(n) && !aliasSeen.has(n)) { aliasSeen.add(n); aliases.push(l.item.description); }
  });

  const category = siblingLots.map(l => l.category).find(c => c) || '';
  const subCategory = siblingLots.map(l => l.subCategory).find(c => c) || '';
  // Distinct plants, not lots: SBR 1502 has five lots at one plant and read
  // "rolled up across 5 plant locations".
  const plantCount = new Set(siblingLots.map(l => l._plantKey)).size;
  // Stock per unit, never one sum across units: Rubber Process Oil 710 is
  // KG at HRS and LTR at Vapi, and one bare total read 29,385.
  const qtyByUom = new Map();
  siblingLots.forEach(l => { const u = (l.uom || '').trim().toUpperCase() || 'unit not recorded'; qtyByUom.set(u, (qtyByUom.get(u) || 0) + (l.qty || 0)); });
  const qtyAllPlantsText = Array.from(qtyByUom.entries()).map(([u, q]) => q.toLocaleString('en-IN', { maximumFractionDigits: 3 }) + ' ' + u).join(' + ') || '0';
  const valueAllPlants = siblingLots.reduce((s, l) => s + (l.value || 0), 0);
  // What is still to come, as the list's Value in Transit counts it - a
  // part-delivered line's received part is not "not yet delivered".
  const openValue = openLinked.reduce((s, l) => s + openValueOfLine(l.item), 0);

  const atAGlanceHtml =
    '<div class="line">In stock, all plants: ' + escapeHtml(qtyAllPlantsText) + '</div>' +
    '<div class="line">Total inventory value: ' + formatInr(valueAllPlants) + '</div>' +
    '<div class="line">Ordered, not yet delivered: ' + formatInr(openValue) + ' across ' + openLinked.length + ' open PO(s)</div>';

  // Category/Sub Category are editable here against the anchor lot
  // specifically (anchor.category/anchor.subCategory - this row's own
  // value, NOT the `category`/`subCategory` rolled-up "first found across
  // every sibling" variables used elsewhere on this page, which can differ
  // from the anchor's own value and would silently edit the wrong lot's
  // field otherwise). Sub Category has no real column on Achhad's Stock
  // sheet at all (see RTPAchhadStockLot's docstring - achhad_views.py's
  // _lot_dict always returns "" for it, not a real value), so it stays
  // plain there rather than offering a pencil that would 400.
  const overviewHtml =
    '<div class="field-grid">' +
      '<div class="field-block"><h4>Classification</h4>' +
        // No lot to correct on an order-only material - its category comes
        // from the category reference, and there is no row to PATCH.
        (isOrderOnly
          ? plainLine('Category', anchor.category) + plainLine('Sub Category', anchor.subCategory)
          : editableLine(plantKey, 'Category', anchor.category, 'category', lotId, 'text') +
        (plantKey === 'achhad'
          ? plainLine('Sub Category', anchor.subCategory)
          : editableLine(plantKey, 'Sub Category', anchor.subCategory, 'sub_category', lotId, 'text'))) +
        '<div class="line">Also known as: ' + (aliases.length ? escapeHtml(aliases.slice(0, 5).join('; ')) + (aliases.length > 5 ? ' (+' + (aliases.length - 5) + ' more)' : '') : 'Not available') + '</div>' +
      '</div>' +
      '<div class="field-block"><h4>Vendors (from POs and Stock Supplier History)</h4>' +
        (vendors.length ? vendors.map(v => '<span class="vendor-pill">' + escapeHtml(v) + '</span>').join('') : '<div class="line">Not available</div>') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>At a Glance</h4>' + atAGlanceHtml + '</div>';

  // Category/Rate are also editable per-row here, one row per sibling lot
  // across all 3 plants (unlike the Overview tab's Category/Sub Category
  // pencils above, which only ever target the modal's own anchor lot).
  // editableCell()'s plantKey argument is this row's own l._plantKey, not
  // the modal's anchor plant, since siblingLots spans all 3 plants - each
  // row gates/targets its own plant's lot independently. A material with
  // stock at more than one plant can therefore have a different Category
  // per plant's lot until someone corrects them to match - same "each row
  // is its own real row, corrections don't auto-propagate" situation the
  // artifact's own multi-field correction UX never had to deal with either.
  //
  // ONE ROW PER LOT, and a lot is one purchase (2026-09-24). HRS's and Vapi's
  // Stock sheets add a row every time a material arrives from a vendor at a
  // new price, so SBR 1502 at HRS is five rows: four GPC International lots
  // and one Expol, three of them used up. With no Vendor or Received column
  // those read as the same plant listed five times (project owner: "why are
  // there so many instances of HRS Silvassa"). The columns say what each row
  // is; the order puts what is on hand first and the newest lot at the top of
  // each plant; a used-up lot (qty 0, still listed in the sheet) is faded
  // rather than hidden, because its rate and MIR match are still real history.
  // The sheet's own location tag rides under the plant, since both sheets
  // file lots for more than one site (HRS's imported SBR sits under RTP-1).
  const plantOrder = key => PLANT_KEYS.indexOf(key);
  siblingLots.sort((a, b) =>
    plantOrder(a._plantKey) - plantOrder(b._plantKey)
    || ((b.qty || 0) > 0) - ((a.qty || 0) > 0)
    || (b.receivedDate || '').localeCompare(a.receivedDate || ''));
  const stockTableHtml = siblingLots.length
    ? '<div class="table-wrap"><table><thead><tr><th>Plant</th><th>Vendor</th><th>Received</th><th>Category</th><th>Qty</th><th>Rate</th><th>Value</th><th>MIR↔Stock Match</th></tr></thead><tbody>' +
        siblingLots.map(l => {
          const usedUp = !((l.qty || 0) > 0);
          return '<tr' + (usedUp ? ' class="lot-used-up" title="Used up - still listed in the Stock sheet"' : '') + '>' +
            '<td>' + escapeHtml(l._plantLabel) +
              (l.locationTag ? '<div class="fs-11 text-slate-soft nowrap" title="Where the Stock sheet files this lot">Location: ' + escapeHtml(l.locationTag) + '</div>' : '') + '</td>' +
            '<td>' + escapeHtml(l.vendor || '-') + '</td>' +
            '<td class="nowrap">' + escapeHtml(l.receivedDate ? formatDateIN(l.receivedDate) : '-') + '</td>' +
            '<td>' + editableCell(l._plantKey, 'Category (' + l._plantLabel + ')', l.category, 'category', l.lotId, 'text') + '</td>' +
            '<td>' + (l.qty != null ? l.qty : '-') + (usedUp ? '<div class="fs-11 text-slate-soft">used up</div>' : '') + '</td>' +
            '<td>' + editableCell(l._plantKey, 'Rate (' + l._plantLabel + ')', l.rate, materialRateFieldName(l._plantKey), l.lotId, 'number') + '</td>' +
            '<td>' + (l.value != null ? formatInr(l.value) : '-') + '</td>' +
            '<td>' + mirStockMatchHtml(l, l._plantKey) + '</td></tr>';
        }).join('') +
        '<tr class="fw-700"><td>Total</td><td></td><td></td><td></td><td>' + escapeHtml(qtyAllPlantsText) + '</td><td>-</td><td>' + formatInr(valueAllPlants) + '</td><td></td></tr>' +
      '</tbody></table></div>'
    : '<div class="no-data-note">No stock found for this material at any plant.</div>';

  // "Flags & Corrections" tab (2026-09-05, project owner: the Domestic/
  // Import PO detail modals both have this as a real, separate tab - Raw
  // Material Analysis was missing the equivalent). An earlier pass here
  // folded these into the bottom of "Stock by Plant" instead, reasoning
  // that the original artifact prototype never gave its own material modal
  // a separate tab at all (confirmed by reading its real
  // openMaterialModalByKey() source) - the project owner overrode that
  // directly, asking for a genuine separate tab matching the PO modals'
  // own structure. Built with this app's own per-flag `.field-block` +
  // dismiss-link styling (poFlagHtml()/materialFlagHtml() in flags.js),
  // not the artifact's plain read-only bullet list - real dismiss/reason
  // controls matter more here than matching a static aesthetic, and it
  // stays visually consistent with the PO modals' own Flags & Corrections
  // tabs already in this app.
  const flaggedMatches = [];
  siblingLots.forEach(l => (l.mirStockMatches || []).forEach(m => {
    if (m.isFlagged) flaggedMatches.push(Object.assign({}, m, { _plantKey: l._plantKey, _plantLabel: l._plantLabel }));
  }));

  // PO<->MIR Quantity/Rate discrepancy flags for this material's own linked
  // PO line items (2026-09-05, project owner: "flags and correction
  // shouldn't only be happening for data quality flags but for all the
  // flags" - this box used to show MIR<->Stock mismatches only, missing the
  // Quantity/Rate Discrepancy flags that already drive this same view's own
  // "Quantity Discrepancies"/"Rate Discrepancies" KPI cards). Reuses
  // computeMaterialPoLinkage()'s own per-item-scoped logic (materials.js) -
  // checked against `l.item`'s own diff%, not a PO's blanket _qtyFlag/
  // _rateFlag, so a discrepancy on a different line item in a multi-item PO
  // is never misattributed to this material. Not individually dismissable
  // here - the underlying flag is a real per-PO Quantity/Rate Discrepancy
  // flag with its own dismiss control already on that PO's own
  // Flags & Corrections tab (poFlagHtml, po-modal.js); this box just needs
  // to stop hiding that it exists.
  // label -> { severity, poSet: Set of "PO (plant)" strings }. Extended
  // 2026-09-08 (Data Quality Flags clarity pass, same day as Domestic's/
  // Import's own extension) beyond the original qty/rate-only pair - see
  // computeMaterialPoLinkage()'s own comment (materials.js) for the full
  // list and why each is scoped to `l.item`, not the parent PO.
  const materialCritPos = new Map();
  const addMaterialFlag = (label, severity, l) => {
    if (!materialCritPos.has(label)) materialCritPos.set(label, { severity, poSet: new Set() });
    materialCritPos.get(label).poSet.add(l.po.poNumber + ' (' + l.plantLabel + (l.po.isImport ? ', import' : '') + ')');
  };
  // Same rules as the list's computeMaterialPoLinkage() (materials.js): a
  // dismissed match raises nothing, and "PO Not Found" is not raised on an
  // order that is simply not due yet. The modal used to apply neither, so it
  // showed as CRITICAL what the row beside it deliberately did not.
  linked.forEach(l => {
    const dismissed = l.item.dismissedByOverride;
    if (!dismissed && l.item.qtyDiffPct != null && l.item.qtyDiffPct > FLAG_PCT) addMaterialFlag('Quantity Mismatch in MIR', 'critical', l);
    // Rate only, not value - see flags.js's computePoFlags() for why.
    if (!dismissed && l.item.rateDiffPct != null && l.item.rateDiffPct > FLAG_PCT) addMaterialFlag('Rate Mismatch in MIR', 'critical', l);
    if (!l.item.matched && l.po._status !== 'pending') addMaterialFlag('PO Not Found in MIR', 'critical', l);
    if (!dismissed && l.item.taxTypeMismatch) addMaterialFlag('Tax Type Mismatch in MIR', 'info', l);
    if (!dismissed && l.item.netValueMismatched) addMaterialFlag('Net Value Mismatch in MIR', 'info', l);
    if (!dismissed && l.item.taxableValueMismatched) addMaterialFlag('Taxable Value Mismatch in MIR', 'info', l);
    if (!dismissed && l.item.finalValueMismatched) addMaterialFlag('Final Amount Mismatch in MIR', 'info', l);
    if (!dismissed && l.item.uomMismatch) addMaterialFlag('UOM Mismatch in MIR', 'info', l);
  });
  const materialCritFlagsHtml = Array.from(materialCritPos.entries()).map(([label, entry]) =>
    '<div class="field-block mb-8">' +
      flagIconHtml(categoryColor(label), 'row-flag-icon') +
      ' <span class="lg-label ' + entry.severity + '">' + (entry.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span> <b>' + escapeHtml(label) + '</b>' +
      '<div class="mt-4 fs-11-5 text-slate-soft">Affects: ' + escapeHtml(Array.from(entry.poSet).join(', ')) + ' - open that PO\'s own detail view to dismiss.</div>' +
    '</div>'
  ).join('');
  // Match Accuracy Programme fix 3.G: each sibling lot's own stock-balance
  // arithmetic flag, if any - a real source-sheet formula problem, not a
  // matching artifact, so it's shown alongside the MIR<->Stock/discrepancy
  // flags above rather than only being visible in Django Admin.
  const dataQualityFlagsHtml = [];
  siblingLots.forEach(l => (l.dataQualityFlags || []).forEach(f =>
    dataQualityFlagsHtml.push(dataQualityFlagHtml(Object.assign({}, f, { _plantLabel: l._plantLabel })))
  ));
  // One INFO note per sibling lot whose MIR<->Stock pairing could not compare
  // qty/rate because the units clash - see flags.js's mirStockMatchHtml().
  // Collected separately from flaggedMatches because these are not flags
  // (is_flagged is False and there is nothing to dismiss); without this the
  // tab said "No ... match flags" about a pairing that was never checked.
  const uomNotesHtml = siblingLots
    .filter(l => (l.mirStockMatches || []).some(m => m.uomMismatch))
    .map(materialUomNoteHtml);
  const totalFlagCount = flaggedMatches.length + materialCritPos.size + dataQualityFlagsHtml.length + uomNotesHtml.length;

  const allMaterialCorrections = [];
  // _plantKey/_lotId ride along so a Correction History row can build its
  // own revert target - this modal's rows span all three plants' separate
  // lot tables and PATCH endpoints, so a bare field name is not enough.
  siblingLots.forEach(l => (l.corrections || []).forEach(c => allMaterialCorrections.push(
    Object.assign({}, c, { _plantLabel: l._plantLabel, _plantKey: l._plantKey, _lotId: l.lotId }))));
  allMaterialCorrections.sort((a, b) => (b.correctedAt || '').localeCompare(a.correctedAt || ''));

  const materialFlagsListHtml = totalFlagCount
    ? materialCritFlagsHtml + flaggedMatches.map(materialFlagHtml).join('') + uomNotesHtml.join('') + dataQualityFlagsHtml.join('')
    : '<div class="empty-note-sm">No discrepancy or MIR&harr;Stock match flags for this material.</div>';
  const materialCorrectionsHtml = allMaterialCorrections.length
    ? '<div class="section-title mt-18">Correction History</div>' +
      allMaterialCorrections.map(c =>
        '<div class="field-block mb-8">' +
          '<div class="fs-12-5"><b>' + escapeHtml(c._plantLabel) + ' &middot; ' + escapeHtml(c.fieldName) + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') +
          // Gated on THIS row's own plant, not the modal's anchor plant -
          // a plant-scoped editor may be able to correct one sibling lot
          // and not another (same reasoning as editableCell()'s own
          // per-row plantKey argument).
          (canEditField(c._plantKey)
            ? '<span class="revert-link" data-field="' + escapeHtml(c.fieldName) + '"' +
              ' data-label="' + escapeHtml(c._plantLabel + ' ' + c.fieldName) + '"' +
              ' data-plant="' + escapeHtml(c._plantKey) + '"' +
              ' data-item="' + escapeHtml(String(c._lotId)) + '"' +
              ' data-old-value="' + escapeHtml(c.oldValue || '') + '">revert</span>'
            : '') +
          '</div>' +
          (c.reason ? '<div class="mt-4 fs-12 italic text-slate">"' + escapeHtml(c.reason) + '"</div>' : '') +
          '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? localDateOf(c.correctedAt) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const flagsTabHtml = materialFlagsListHtml + materialCorrectionsHtml +
    overrideBoxHtml('Click the ✎ beside Category, Sub Category or Rate to correct it.');
  const stockByPlantHtml = stockTableHtml;

  const purchaseActivityHtml =
    '<div class="field-block"><h4>Quantity</h4>' + atAGlanceHtml + '</div>' +
    '<div class="section-title">Open Purchase Orders</div>' +
    (openLinked.length
      // Import orders are listed here too since 2026-09-24 (materials.js's
      // materialOrders()), tagged so they are never mistaken for domestic
      // ones, with the INR value the In Transit figure counts them at.
      ? '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Plant</th><th>Qty to come</th><th>Value to come (INR)</th><th>Status</th></tr></thead><tbody>' +
          // The PO number opens that order's own detail modal (Domestic or
          // Import), wired below by [data-open-po].
          openLinked.map(l => '<tr><td><span class="row-link" tabindex="0" role="button"' +
            ' data-open-po="' + escapeHtml(l.plantKey + '::' + l.po.poNumber) + '"' +
            ' data-open-po-kind="' + (l.po.isImport ? 'import' : 'domestic') + '"' +
            ' title="Open this purchase order">' + escapeHtml(l.po.poNumber) + '</span>' +
            (l.po.isImport ? ' <span class="badge-import" title="' + escapeHtml(importPriceTitle(l.item)) + '">Import</span>' : '') +
            '</td><td>' + escapeHtml(l.po.vendorName || '-') + '</td><td>' + escapeHtml(l.plantLabel) +
            '</td><td>' + (openQtyOfLine(l.item) != null ? openQtyOfLine(l.item) : '-') + ' ' + escapeHtml(l.item.uom || '') +
            (openQtyOfLine(l.item) !== l.item.qty && l.item.qty != null ? ' <span class="text-slate-soft">of ' + l.item.qty + '</span>' : '') +
            '</td><td>' + (l.item.netPrice != null && l.item.qty != null ? formatInr(openValueOfLine(l.item)) : '-') +
            '</td><td><span class="status-pill status-' + l.po._status + '">' + escapeHtml(STATUS_LABELS[l.po._status]) + '</span></td></tr>').join('') +
        '</tbody></table></div>'
      : '<div class="no-data-note">No open purchase orders currently linked to this material by automated matching.</div>');

  const priceTrendHtml =
    '<div class="chart-box" id="matPriceTrendBox"><div class="no-data-note">Loading&hellip;</div></div>' +
    // Readability pass (2026-09-21): this used to be a permanent 38-word
    // paragraph under the chart. It answers a question a reader only asks
    // once ("what is the 21/50/100-day line?"), so it is a tooltip on a
    // short label now rather than standing text competing with the chart.
    '<div class="no-data-note mt-8" title="' +
      escapeHtml('A trailing calendar-day average of actual PO prices, looking 21/50/100 days back from each PO date. POs happen irregularly rather than daily, so averaging over calendar days smooths the line without inventing prices for days that had no PO.') +
      '">Moving averages: 21 / 50 / 100 calendar days &#9432;</div>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(anchor.description || anchor.materialCode) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(category || 'Uncategorized') + ' &middot; ' + siblingLots.length + ' stock lot' + (siblingLots.length === 1 ? '' : 's') + ' across ' + plantCount + ' plant' + (plantCount === 1 ? '' : 's') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    matchingDisclaimerHtml(
      'Purchase Activity, Price Trend, Vendors and "Also known as" are matched automatically.',
      '<p>These four tie this material to PO line items by description, and by vendor where it is known. That is not guaranteed-correct identity resolution - a similarly worded material can be pulled in, and a differently worded one missed.</p>' +
      '<p>Verify manually before treating anything here as ground truth.</p>'
    ) +
    '<div class="modal-tabs" id="matModalTabs" role="tablist">' +
      '<div class="modal-tab active" data-tab="overview" tabindex="0" role="tab" aria-selected="true">Overview</div>' +
      '<div class="modal-tab" data-tab="stockplant" tabindex="0" role="tab" aria-selected="false">Stock by Plant</div>' +
      '<div class="modal-tab" data-tab="poactivity" tabindex="0" role="tab" aria-selected="false">Purchase Activity' + (openLinked.length ? ' (' + openLinked.length + ' open)' : '') + '</div>' +
      '<div class="modal-tab" data-tab="pricetrend" tabindex="0" role="tab" aria-selected="false">Price Trend</div>' +
      '<div class="modal-tab" data-tab="flags" tabindex="0" role="tab" aria-selected="false">Flags &amp; Corrections' + (totalFlagCount ? ' (' + totalFlagCount + ')' : '') + '</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="matModalOverview">' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalStockPlant" hidden>' + stockByPlantHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalPoActivity" hidden>' + purchaseActivityHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalPriceTrend" hidden>' + priceTrendHtml + '</div>' +
    '<div class="modal-tab-panel" id="matModalFlags" hidden>' + flagsTabHtml + '</div>';

  // wireDismissLinks reads each link's own data-plant (set per-row above,
  // since siblingLots spans multiple plants) - the plantKey argument here
  // is only a fallback, never actually used by this table's own links.
  wireDismissLinks(body, plantKey, async () => {
    PLANT_KEYS.forEach(key => { MATERIALS_BY_PLANT[key] = null; });
    await openMaterialModal(compositeKey);
  });

  // Each row's .editable-line carries its own data-plant (see the
  // stockByPlantHtml row template above) - wireEditIcons' fieldsUrl
  // function reads that back per line instead of one fixed URL, since this
  // table's rows can target 3 different plants' own PATCH endpoints.
  // switchToTab jumps to the Flags & Corrections tab, same as the Domestic/
  // Import PO modals - that's where this modal's own "Submit a Correction"
  // panel lives now (see flagsTabHtml above).
  const switchToFlagsTab = () => { const t = body.querySelector('[data-tab="flags"]'); if (t) t.click(); };
  const onMaterialSaved = async () => {
    PLANT_KEYS.forEach(key => { MATERIALS_BY_PLANT[key] = null; });
    await openMaterialModal(compositeKey);
    renderMaterialsView();
  };
  const materialUrlFor = (el) => materialFieldsUrl(el.dataset.plant, el.dataset.item);
  wireEditIcons(body, materialUrlFor, switchToFlagsTab, onMaterialSaved);
  wireRevertLinks(body, materialUrlFor, onMaterialSaved);

  // Purchase Activity's PO numbers. Opening the PO replaces this modal; the
  // PO modal's own "View full material analysis" link leads back. Enter and
  // Space activate it too, since it is a span with role="button".
  body.querySelectorAll('[data-open-po]').forEach(el2 => {
    const open = () => (el2.dataset.openPoKind === 'import' ? openImportPoModal : openPoModal)(el2.dataset.openPo);
    el2.onclick = open;
    el2.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  });

  const panelIds = { overview: 'matModalOverview', stockplant: 'matModalStockPlant', poactivity: 'matModalPoActivity', pricetrend: 'matModalPriceTrend', flags: 'matModalFlags' };
  body.querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    body.querySelectorAll('[data-tab]').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    Object.entries(panelIds).forEach(([key, id]) => { document.getElementById(id).hidden = tab.dataset.tab !== key; });
  });

  // Price Trend: net price of every linked PO line item (all statuses, not
  // just open ones) plotted over the PO's own createdDate, with 21/50/100-
  // day trailing moving averages (see trailingPriceAvg()). Some real line
  // items carry a net value/qty but no explicit unit netPrice - derive one
  // from netValue/qty rather than dropping the point outright, so a
  // material isn't misreported as having "no PO price history" just
  // because that one field was left blank upstream.
  const points = linked
    .map(l => {
      const price = l.item.netPrice != null ? l.item.netPrice
        : (l.item.netValue != null && l.item.qty) ? l.item.netValue / l.item.qty
        : null;
      return { date: l.po.createdDate, price, poNumber: l.po.poNumber };
    })
    .filter(p => p.price != null && p.date)
    .sort((a, b) => a.date.localeCompare(b.date));
  const trendBox = document.getElementById('matPriceTrendBox');
  if (!points.length) {
    trendBox.innerHTML = '<div class="no-data-note">No purchase order price history found for this material by automated matching.</div>';
  } else {
    trendBox.innerHTML = '<canvas id="matPriceTrendChart"></canvas>';
    try {
      // Moving averages need >=2 points to mean anything - with just one PO,
      // still plot that single price (a real, visible dot) rather than
      // showing nothing; only add the trend-line datasets once there's
      // enough history to actually average.
      const datasets = [
        { label: 'Net price per PO', data: points.map(p => p.price), borderColor: '#2563eb', backgroundColor: '#2563eb', tension: 0.15, pointRadius: 4, showLine: points.length > 1 },
      ];
      if (points.length >= 2) {
        datasets.push(
          { label: '21-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 21)), borderColor: '#16a34a', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
          { label: '50-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 50)), borderColor: '#d97706', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
          { label: '100-day moving avg', data: points.map((p, i) => trailingPriceAvg(points, i, 100)), borderColor: '#7c3aed', borderDash: [5, 3], pointRadius: 0, tension: 0.15 },
        );
      }
      // A single point has no natural y-range for Chart.js to pad around,
      // so give it one explicitly (+/-10%) rather than risk a collapsed axis.
      const singlePointPadding = points.length === 1 ? { suggestedMin: points[0].price * 0.9, suggestedMax: points[0].price * 1.1 } : {};
      modalCharts.push(new Chart(document.getElementById('matPriceTrendChart'), {
        type: 'line',
        data: {
          labels: points.map(p => formatDateIN(p.date)),
          datasets,
        },
        options: {
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 9, boxHeight: 9, font: { size: 10.5 } } },
            tooltip: { backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8, callbacks: { label: c => c.dataset.label + ': ' + formatInr(c.parsed.y) } } },
          scales: { y: { ticks: { callback: v => formatInr(v) }, ...singlePointPadding } },
        },
      }));
    } catch (e) {
      console.error('Price trend chart failed to render:', e);
      trendBox.innerHTML = '<div class="no-data-note">Chart unavailable right now.</div>';
    }
  }

  // Anchor lot's own qty-over-time stock trend (per-lot, not cross-plant -
  // the stock-trend endpoint only knows about one specific lot id), appended
  // under the Stock by Plant table. An order-only material has no lot.
  if (isOrderOnly) return;
  try {
    const data = await apiForPlant(plantKey, '/materials/' + encodeURIComponent(lotId) + '/stock-trend');
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    const stockPlantPanel = document.getElementById('matModalStockPlant');
    if (!stockPlantPanel) return; // modal closed/replaced while this was in flight
    const trendSection = document.createElement('div');
    if (!data.snapshots.length) {
      trendSection.innerHTML = '<div class="section-title">Stock Trend (' + escapeHtml(PLANTS[plantKey].label) + ')</div><div class="no-data-note">Not enough daily snapshots yet to plot a trend.</div>';
    } else {
      trendSection.innerHTML = '<div class="section-title">Stock Trend (' + escapeHtml(PLANTS[plantKey].label) + ')</div><div class="chart-box"><canvas id="matTrendChart"></canvas></div>';
    }
    stockPlantPanel.appendChild(trendSection);
    if (data.snapshots.length) {
      modalCharts.push(new Chart(document.getElementById('matTrendChart'), {
        type: 'line',
        data: { labels: data.snapshots.map(s => formatDateIN(s.date)), datasets: [{ label: 'Qty', data: data.snapshots.map(s => s.qty), borderColor: '#2563eb', tension: 0.2 }] },
        options: { plugins: { legend: { display: false } } },
      }));
    }
  } catch (e) {
    console.error('openMaterialModal stock-trend fetch failed:', e);
    const stockPlantPanel = document.getElementById('matModalStockPlant');
    if (stockPlantPanel) stockPlantPanel.insertAdjacentHTML('beforeend', '<div class="no-data-note">Couldn\'t load the stock trend right now.</div>');
  }
}
