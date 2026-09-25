// ── PO list rendering & detail modal (Import Purchases) ─────────────────
// Import Purchases (purchaseType 'import') - same KPI-row / chart-row /
// list / drill-down-modal scaffold as Domestic (renderPoList()/openPoModal()
// above), fed from apps/api/routers/imports_views.py instead of PO<->MIR
// match data. Differs from Domestic in the few places the underlying data
// genuinely differs: shipment stage (Placed/Shipped(BL)/Cleared(BOE)) instead
// of match status, BOE/BL/country-of-origin fields, and inline-edit
// corrections (a real mutating endpoint - see ImportPOCorrection in
// apps/core/models/) since Domestic has no equivalent "fix a bad value in
// place" workflow yet.

const IMPORT_STAGES = ['Placed', 'Shipped (BL)', 'Cleared (BOE)'];
const IMPORT_STAGE_LABELS = { 'Placed': 'Awaiting Bill of Lading', 'Shipped (BL)': 'In Transit', 'Cleared (BOE)': 'Customs Cleared (BOE)' };
const IMPORT_STAGE_PILL_CLASS = { 'Placed': 'status-pending', 'Shipped (BL)': 'status-partial', 'Cleared (BOE)': 'status-received' };

// Same markup/CSS classes as miniStepperHtml()/materialStepperHtml() (3
// dots/2 lines instead of 2 dots/1 line - .mini-stepper's flex layout
// doesn't care how many of each it's given).
function shipmentStepperHtml(po) {
  const idx = IMPORT_STAGES.indexOf(po.shipmentStage);
  const dots = IMPORT_STAGES.map((s, i) =>
    (i > 0 ? '<span class="step-line ' + (i <= idx ? 'done' : '') + '"></span>' : '') +
    '<span class="step-dot ' + (i <= idx ? 'done' : '') + '"></span>'
  ).join('');
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(po.shipmentStage) + '"><div class="mini-stepper">' + dots + '</div>' +
    '<div class="mini-stepper-labels">Placed / Shipped / Cleared</div></div>';
}

// The same four buckets Domestic uses - see flags.js's rowFlagsHtml().
// Categories come from po._categories (importCategoriesFor(), set once per
// render in renderImportPoList()), not the raw F1-F7 codes.
//
// Import is the one view that can report BOTH partial delivery and "on
// order" at once: `partialDelivery` reads each item's MIR receipts
// (import_flags.py's partial_delivery()) while `deliveryDateStatus` is a
// date comparison on the items not yet received, so a PO with one item
// landed and another still due is both. rowFlagsHtml()
// resolves that in favour of partial, the more specific fact.
function importRowFlags(po) {
  return rowFlagsHtml({
    partial: !!po.partialDelivery,
    onOrder: po.deliveryDateStatus === 'On Order',
    categories: po._categories,
  });
}

// Short, stable label per import_flags.py flag code (F1-F7) - that module's
// own `message` is per-item and often includes item-specific text (e.g. a
// literal malformed HSN value), so it can't be used as a Sub Category option
// label the way domestic's fixed DISCREPANCY_LEGEND labels can; this is the
// import-side equivalent, keyed on the stable `code` instead.
const IMPORT_FLAG_LABELS = {
  F1: 'BOE filed, Qty (As Per BOE) missing',
  F2: 'BOE filed, landed-cost fields incomplete',
  F3: 'Inconsistent landed-cost completeness across a BOE',
  F4: 'Laden on Board date without Bill of Lading number',
  F5: 'Malformed HSN code',
  F6: 'BOE filed, Currency (After Taxes) blank',
  F7: 'Delivery date not parseable',
};
// Unified per-PO severity/label list for Import - NOT the material Category/
// Sub Category filters (those read po.materialCategories instead, see
// renderImportPoList()'s own comment on categoryCounts). This one only feeds
// the row-tint flags (_qtyFlag/_rateFlag below) and the Data Quality Flag
// tooltip data; it used to also back "Filter by Category"/"Sub Category"
// before those were repurposed for material data (2026-09-07) - that
// severity/label filtering now lives in "Filter by Flags" instead (see
// criticalCount/flagsOptionsHtml). Critical = the 3 discrepancy KPI cards
// (qty PO-vs-BOE, qty BOE-vs-MIR, rate BOE-vs-MIR); info = the F1-F7 data
// quality flags, deduped by code (a PO can carry the same code on more than
// one line item).
//
// A flag an editor dismissed on the PO's Flags & Corrections tab is left out
// (poFlagDismissed()), so it stops counting on the KPI row and in the row
// flags. An F1-F7 code is left out only once every flag of that code on the
// PO is dismissed - each is dismissed per line item (`code:item_id`).
function importCategoriesFor(po, poQtyDiscMir, poRateDiscMir) {
  const cats = [];
  if (poQtyDiscPo(po)) cats.push({ label: 'Qty Mismatch (PO vs BOE)', severity: 'critical', key: 'qtydisc' });
  if (poQtyDiscMir(po)) cats.push({ label: 'Qty Mismatch in MIR (BOE vs MIR)', severity: 'critical', key: 'qtydiscmir' });
  if (poRateDiscMir(po)) cats.push({ label: 'Rate Mismatch in MIR (BOE vs MIR)', severity: 'critical', key: 'ratedisc' });
  // 2026-09-08 (Data Quality Flags clarity pass, extended to Imports) - same
  // previously-computed-but-discarded match-quality signals Domestic's own
  // computePoFlags() now surfaces (flags.js), read from each item's nested
  // mirMatch object instead of Domestic's flat fields (imports_views.py's
  // _mir_match_dict() shape - see that function's own comment). No `key` set
  // on these (unlike the 3 above) - they only ever appear as `cat:<label>`
  // dropdown options, not a dedicated fixed shortcut.
  const items = po.items || [];
  // A dismissed match is excluded from every match-derived check below
  // (2026-09-10 fix) - same reasoning as flags.js's computePoFlags()'s own
  // comment.
  const live = i => i.mirMatch && !i.mirMatch.dismissedByOverride;
  // Not on an order that is not due yet - see importOrderNotDueYet().
  if (!importOrderNotDueYet(po) && items.some(i => !i.mirMatch)) cats.push({ label: 'PO Not Found in MIR', severity: 'critical' });
  if (items.some(i => live(i) && i.mirMatch.taxTypeMismatch)) cats.push({ label: 'Tax Type Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.netValueMismatched)) cats.push({ label: 'Net Value Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.taxableValueMismatched)) cats.push({ label: 'Taxable Value Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.finalValueMismatched)) cats.push({ label: 'Final Amount Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.uomMismatch)) cats.push({ label: 'UOM Mismatch in MIR', severity: 'info' });
  // A rate gap that is really a different exchange rate (2026-09-25): the
  // matcher cleared rateMismatched and set exchangeRateMismatched instead -
  // see matching_core._exchange_rate_explains(). Info, not critical: the
  // CSV's exchange rate needs correcting, the vendor's price does not.
  if (items.some(i => live(i) && i.mirMatch.exchangeRateMismatched)) cats.push({ label: 'Exchange Rate Differs from MIR', severity: 'info' });
  const liveCats = cats.filter(c => !poFlagDismissed(po, c.label));
  const seenCodes = new Set();
  (po.dataQualityFlags || []).forEach(f => {
    if (seenCodes.has(f.code) || poFlagDismissed(po, f.code + ':' + (f.item_id || ''))) return;
    seenCodes.add(f.code);
    liveCats.push({ label: IMPORT_FLAG_LABELS[f.code] || (f.code + ': ' + f.message), severity: 'info', key: 'flags' });
  });
  return liveCats;
}

// PO-level "Qty Mismatch (PO vs BOE)", unless an editor dismissed it. The
// BOE-vs-MIR and rate checks below it have the same dismissal rule; all
// three feed the KPI cards, the table filter and the row flags.
function poQtyDiscPo(po) {
  return !!po.qtyDiscrepancy && !poFlagDismissed(po, 'Qty Mismatch (PO vs BOE)');
}

// BL Number cell content, shared by the "View all" table and the top5 grid -
// the bare number plus a "Track" link (data-track-bl, wired once after
// render alongside every other [data-...] handler below) that opens the
// shared modal with live shipment status via shared.js's trackBlNumber()
// (see that function's own header comment for why the SAME #modalBackdrop
// every other modal on this page uses is reused here, not a new component).
function blNumberCellHtml(po, emptyText) {
  if (!po.billOfLadingNumber) return emptyText;
  return escapeHtml(po.billOfLadingNumber) +
    ' <span class="row-link fs-11" data-track-bl="' + escapeHtml(po.billOfLadingNumber) + '">Track</span>';
}

function applyImportColFilters(recs) {
  const f = state.importColFilters;
  return recs.filter(po => {
    if (f.poNumber && !(po.poNumber || '').toLowerCase().includes(f.poNumber.toLowerCase())) return false;
    if (f.vendor && !(po.vendorName || '').toLowerCase().includes(f.vendor.toLowerCase())) return false;
    if (f.country && !(po.countryOfOrigin || '').toLowerCase().includes(f.country.toLowerCase())) return false;
    if (f.stage && po.shipmentStage !== f.stage) return false;
    return true;
  });
}

function renderImportPoList(el) {
  const all = currentImportPOs();
  if (!all.length) {
    const msg = isAllPlants()
      ? 'No import purchase orders synced yet for any plant.'
      : 'No import purchase orders synced yet for ' + PLANTS[state.plant].label + ' - run <code>' +
        { hrs: 'sync_hrs_imports_po_csv', achhad: 'sync_achhad_imports_po_csv', vapi: 'sync_vapi_imports_po_csv' }[state.plant] +
        '</code> to load them.';
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml(msg) + '</div>';
    return;
  }

  const inRange = po => {
    if (state.importFrom && (!po.createdDate || po.createdDate < state.importFrom)) return false;
    if (state.importTo && (!po.createdDate || po.createdDate > state.importTo)) return false;
    return true;
  };
  let filtered = all.filter(inRange);

  // Material Inwarded is server-computed (import_flags.material_inwarded()):
  // every line fully received in MIR, the same rule as Domestic's Received.
  const poInwarded = p => !!p.materialInwarded;
  // A PO "has" a MIR mismatch if ANY of its line items does. Excludes a
  // dismissed match (2026-09-10 fix) and a PO-level dismissal of the flag
  // itself (poFlagDismissed()) - same reasoning as flags.js's
  // computePoFlags().
  const poQtyDiscMir = p => !poFlagDismissed(p, 'Qty Mismatch in MIR (BOE vs MIR)') &&
    (p.items || []).some(i => i.mirMatch && !i.mirMatch.dismissedByOverride && i.mirMatch.qtyDiffPct > 0);
  const poRateDiscMir = p => !poFlagDismissed(p, 'Rate Mismatch in MIR (BOE vs MIR)') &&
    (p.items || []).some(i => i.mirMatch && !i.mirMatch.dismissedByOverride && i.mirMatch.rateDiffPct > 0);
  filtered.forEach(po => {
    po._categories = importCategoriesFor(po, poQtyDiscMir, poRateDiscMir);
    // Same _qtyFlag/_rateFlag/_maxDiffPct shape as Domestic's computePoFlags()
    // (see flags.js's rowTintClass(), shared across all three list views) -
    // scoped to the BOE<->MIR diffs (the ones with a real percentage
    // magnitude available client-side; the PO-vs-BOE qty flag has no
    // equivalent % here) so the row tint reflects the same discrepancy the
    // "Qty/Rate Discrepancies (BOE vs MIR)" KPI cards already count.
    po._qtyFlag = poQtyDiscPo(po) || poQtyDiscMir(po);
    po._rateFlag = poRateDiscMir(po);
    const itemDiffs = (po.items || []).flatMap(i => i.mirMatch ? [i.mirMatch.qtyDiffPct, i.mirMatch.rateDiffPct, i.mirMatch.valueDiffPct] : []).filter(v => v != null);
    po._maxDiffPct = itemDiffs.length ? Math.max(...itemDiffs) : 0;
  });

  // Category/Sub Category counts and narrowing - these are now the MATERIAL
  // category/subcategory an import PO's line items belong to (po.materialCategories,
  // backed by MaterialCategoryReference - see apps/api/routers/imports_views.py's
  // _po_dict()), not flag severity/label. Same 2026-09-07 change already made to
  // Domestic's renderPoList() (see its own comment on categoryCounts for the full
  // story) - "Filter by Category"/"Sub Category" never meant material data here
  // either, that was flag severity/label, which is now folded into "Filter by
  // Flags" as a "Critical Issues" shortcut alongside the existing qty/rate
  // mismatch options (the info-severity/per-flag-code granularity is not
  // reproduced there beyond the existing aggregate "Data Quality Flag" option -
  // same disclosed simplification Domestic's own fold-in made).
  const categoryCounts = {};
  filtered.forEach(po => new Set((po.materialCategories || []).map(c => c.category)).forEach(cat => {
    categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
  }));
  const dateRangeCount = filtered.length; // "All Categories (N)" option's count - before category narrowing
  const inSelectedCategory = state.importCategoryFilter
    ? filtered.filter(po => (po.materialCategories || []).some(c => c.category === state.importCategoryFilter))
    : filtered;
  const subCategoryCounts = {};
  inSelectedCategory.forEach(po => new Set(
    (po.materialCategories || [])
      .filter(c => !state.importCategoryFilter || c.category === state.importCategoryFilter)
      .map(c => c.subCategory || 'Uncategorized')
  ).forEach(sub => { subCategoryCounts[sub] = (subCategoryCounts[sub] || 0) + 1; }));
  const subCategoryBaseCount = inSelectedCategory.length;
  if (state.importCategoryFilter) filtered = filtered.filter(po => (po.materialCategories || []).some(c => c.category === state.importCategoryFilter));
  if (state.importSubCategoryFilter) filtered = filtered.filter(po => (po.materialCategories || []).some(c => (c.subCategory || 'Uncategorized') === state.importSubCategoryFilter));
  const total = filtered.length;

  const counts = {
    inwarded: filtered.filter(poInwarded).length,
    partial: filtered.filter(p => p.partialDelivery).length,
    qtyDisc: filtered.filter(poQtyDiscPo).length,
    qtyDiscMir: filtered.filter(poQtyDiscMir).length,
    rateDiscMir: filtered.filter(poRateDiscMir).length,
    overdue: filtered.filter(p => p.deliveryDateStatus === 'Overdue').length,
    onOrder: filtered.filter(p => p.deliveryDateStatus === 'On Order').length,
    unknownDate: filtered.filter(p => p.deliveryDateStatus === 'Unknown').length,
    // Redefined 2026-09-08 (Data Quality Flags clarity pass, same day as
    // Domestic's own renderPoList()) from "has an F1-F7 code" to "any
    // category at all" - qty/rate/PO-not-found/tax-type/net-taxable-final
    // value/UOM mismatches plus the existing F1-F7 codes. See
    // importCategoriesFor()'s own comment for the full category list.
    flags: filtered.filter(po => po._categories.length > 0).length,
    placed: filtered.filter(p => p.shipmentStage === 'Placed').length,
    shipped: filtered.filter(p => p.shipmentStage === 'Shipped (BL)').length,
    cleared: filtered.filter(p => p.shipmentStage === 'Cleared (BOE)').length,
  };
  // "Critical Issues" in "Filter by Flags" below - the combined qty-OR-rate
  // count, filling in for what "Filter by Category" used to mean (severity
  // 'critical') before Category/Sub Category were repurposed for material
  // data - same fold-in as Domestic's own renderPoList().
  const criticalCount = filtered.filter(po => po._qtyFlag || po._rateFlag).length;
  // Per-category breakdown for "Filter by Flags" (added 2026-09-08, same
  // pattern as Domestic's own flagCategoryCounts in po-list.js) - excludes
  // the 3 categories that already have their own dedicated dropdown option
  // (qtydisc/qtydiscmir/ratedisc, identified by importCategoriesFor()'s own
  // `key`) so they don't appear twice.
  const DEDICATED_FLAG_KEYS = ['qtydisc', 'qtydiscmir', 'ratedisc'];
  const flagCategoryCounts = {};
  filtered.forEach(po => po._categories.forEach(c => {
    if (DEDICATED_FLAG_KEYS.includes(c.key)) return;
    flagCategoryCounts[c.label] = (flagCategoryCounts[c.label] || 0) + 1;
  }));

  // Same card shape/order philosophy as Domestic's cardDef (file/comment
  // above renderPoList()'s own cardDef): overall total, then the MIR-backed
  // cards (enabled 2026-09-04 - MIR/Stock are shared Drive files across
  // domestic and import purchases for a plant, see
  // apps/services/matching.py's match_import_po_mir_line_item() docstring;
  // these were "Awaiting MIR" placeholders before that was confirmed), then
  // the discrepancy/timing/flag cards, then the import-specific
  // shipment-stage trio Domestic has no equivalent of.
  const cardDef = [
    { key: 'total', cls: '', label: 'Total Import POs', val: total, tip: 'All import purchase orders in the selected date range.' },
    { key: 'inwarded', cls: 'received', label: 'Material Inwarded', val: counts.inwarded, flag: KPI_FLAG_COLORS.received, tip: 'Every line item on this import PO has fully arrived - matched to a MIR entry, and not short of the quantity cleared. An over-delivered line still counts; a short one shows as Partial Delivered instead. Same rule as Domestic Purchases.' },
    { key: 'partial', cls: 'partial', label: 'Partial Delivered', val: counts.partial, flag: KPI_FLAG_COLORS.partial, tip: 'Something has arrived in MIR against this import PO but the order is not complete - a line item has no MIR entry yet, or one arrived short. A Bill of Entry quantity below the PO quantity is counted under Qty Mismatches (PO vs BOE), not here.' },
    { key: 'qtydisc', cls: 'critical', label: 'Qty Mismatches (PO vs BOE)', val: counts.qtyDisc, flag: KPI_FLAG_COLORS.critical, tip: 'Quantity ordered differs from the quantity cleared on the Bill of Entry.' },
    { key: 'qtydiscmir', cls: 'critical', label: 'Qty Mismatches in MIR (BOE vs MIR)', val: counts.qtyDiscMir, flag: KPI_FLAG_COLORS.critical, tip: 'Quantity mismatch in MIR: Bill of Entry quantity differs from the matched MIR entry\'s quantity.' },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Mismatches in MIR', val: counts.rateDiscMir, flag: KPI_FLAG_COLORS.critical, tip: 'Rate mismatch in MIR: the MIR rate agrees with neither the PO rate converted to INR (before duty) nor, once cleared, the landed rate (landed value per BOE unit, duty and IGST included) against MIR\'s final value per unit. Customs duty alone therefore no longer flags; what remains is usually an exchange-rate difference or a real price difference.' },
    { key: 'overdue', cls: 'overdue', label: 'Overdue', val: counts.overdue, flag: KPI_FLAG_COLORS.critical, tip: 'Delivery date has passed and a line item is still not fully received in MIR. Customs clearance alone does not count as delivered - a cleared shipment with no MIR entry is still overdue.' },
    { key: 'onorder', cls: 'pending', label: 'Pending Deliveries / On Order', val: counts.onOrder, flag: KPI_FLAG_COLORS.pending, tip: 'Not yet due, and not yet fully received in MIR.' },
    { key: 'unknowndate', cls: 'unknown', label: 'Delivery Date Unknown', val: counts.unknownDate, flag: KPI_FLAG_COLORS.unknown, tip: 'A line item not yet fully received in MIR has no delivery date on file, so it can\'t be called overdue or on order.' },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: counts.flags, flag: KPI_FLAG_COLORS.quality, tip: 'Any flagged issue on this import PO - quantity/rate mismatch (PO vs BOE or BOE vs MIR), PO not found in MIR, tax type/net/taxable/final value mismatch, UOM mismatch, or a BOE/customs paperwork flag (F1-F7). Use "Filter by Flags" below to narrow to one specific issue.' },
    { key: 'placed', cls: 'pending', label: 'Awaiting Bill of Lading', val: counts.placed, flag: KPI_FLAG_COLORS.pending, tip: 'PO placed - shipment not yet on a Bill of Lading.' },
    { key: 'shipped', cls: 'partial', label: 'In Transit', val: counts.shipped, flag: KPI_FLAG_COLORS.partial, tip: 'Bill of Lading issued - not yet cleared through customs.' },
    { key: 'cleared', cls: 'received', label: 'Customs Cleared (BOE)', val: counts.cleared, flag: KPI_FLAG_COLORS.received, tip: 'Bill of Entry filed - shipment has cleared customs.' },
  ];
  const kpiHtml = cardDef.map(c => {
    const active = !c.disabled && state.importStatusFilter === c.key;
    return '<div class="kpi-card ' + c.cls + (active ? ' active' : '') + (c.disabled ? ' kpi-disabled' : '') + '" data-kpi="' + (c.disabled ? '' : c.key) + '"' + (c.disabled ? '' : ' tabindex="0" role="button" aria-pressed="' + active + '"') + '>' +
      (c.flag ? flagIconHtml(c.flag) : '') +
      '<div class="val' + (typeof c.val === 'string' ? ' val-text' : '') + '"' + (typeof c.val === 'string' ? '' : ' data-count-target="' + c.val + '" data-count-fmt="int"') + '>' + (typeof c.val === 'string' ? escapeHtml(c.val) : 0) + '</div>' +
      '<div class="label">' + escapeHtml(c.label) + (c.tip ? infoTooltipHtml(c.tip) : '') + '</div></div>';
  }).join('');

  const stageChartData = [
    { key: 'placed', label: IMPORT_STAGE_LABELS['Placed'], val: counts.placed, color: '#d97706' },
    { key: 'shipped', label: IMPORT_STAGE_LABELS['Shipped (BL)'], val: counts.shipped, color: '#2563eb' },
    { key: 'cleared', label: IMPORT_STAGE_LABELS['Cleared (BOE)'], val: counts.cleared, color: '#16a34a' },
  ].filter(s => s.val > 0);

  const monthTotals = {};
  filtered.forEach(po => {
    const m = (po.createdDate || '').slice(0, 7);
    if (!m) return;
    monthTotals[m] = (monthTotals[m] || 0) + (po.totalInclusiveValue || 0);
  });
  const months = Object.keys(monthTotals).sort();


  const categoryOptionsHtml = Object.entries(categoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([cat, n]) => '<option value="' + escapeHtml(cat) + '"' + (state.importCategoryFilter === cat ? ' selected' : '') + '>' + escapeHtml(cat) + ' (' + n + ')</option>')
    .join('');
  const subCategoryOptionsHtml = Object.entries(subCategoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => '<option value="' + escapeHtml(label) + '"' + (state.importSubCategoryFilter === label ? ' selected' : '') + '>' + escapeHtml(label) + ' (' + n + ')</option>')
    .join('');
  const flagCategoryOptionsHtml = Object.keys(flagCategoryCounts).sort().map(function (label) {
    const value = 'cat:' + label;
    const selected = state.importStatusFilter === value ? ' selected' : '';
    return '<option value="' + value + '"' + selected + '>' + label + ' (' + flagCategoryCounts[label] + ')</option>';
  }).join('');
  const flagsOptionsHtml =
    '<option value="qtydisc"' + (state.importStatusFilter === 'qtydisc' ? ' selected' : '') + '>Qty Mismatch - PO vs BOE (' + counts.qtyDisc + ')</option>' +
    '<option value="qtydiscmir"' + (state.importStatusFilter === 'qtydiscmir' ? ' selected' : '') + '>Qty Mismatch in MIR - BOE vs MIR (' + counts.qtyDiscMir + ')</option>' +
    '<option value="ratedisc"' + (state.importStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch in MIR (' + counts.rateDiscMir + ')</option>' +
    '<option value="critical"' + (state.importStatusFilter === 'critical' ? ' selected' : '') + '>Critical Issues (' + criticalCount + ')</option>' +
    '<option value="flags"' + (state.importStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + counts.flags + ')</option>' +
    flagCategoryOptionsHtml;


  // Everything from the list heading down is rendered by
  // importListRegionHtml() from this context, so a header-filter keystroke
  // can rebuild the list alone - see IMPORT_LIST_CTX's own comment.
  IMPORT_LIST_CTX = { el: el, filtered: filtered, poInwarded: poInwarded, poQtyDiscMir: poQtyDiscMir, poRateDiscMir: poRateDiscMir };

  el.innerHTML =
    '<div class="filter-row">' +
      '<div class="filter-group">' +
        '<label>Date filter (Created on)</label>' +
        '<input type="date" id="importFromDate" value="' + (state.importFrom || '') + '">' +
        '<span class="sep-gray">to</span>' +
        '<input type="date" id="importToDate" value="' + (state.importTo || '') + '">' +
      '</div>' +
      '<button class="primary" id="importApplyFilter">Apply</button>' +
      '<button id="importClearFilter">Clear</button>' +
      // RoDTEP is a company-wide resource, not per-plant (see
      // rodtep-panel.js's own header comment) - scoped to Import Purchases
      // only, per the project owner's own instruction, rather than added
      // to Domestic Purchases or as a new top-level nav tab.
      '<button id="importRodtepBtn" class="ml-auto">RoDTEP Ledger</button>' +
      '<button id="importAdvanceLicenseBtn">Advance License</button>' +
    '</div>' +
    '<div class="kpi-grid">' + kpiHtml + '</div>' +
    // Category / Sub Category / Flags filters, same 3-dropdown pattern as
    // Domestic's own renderPoList() and Raw Material Analysis - "global"
    // filters that narrow `filtered` itself, so the KPI counts and both
    // charts below reflect the selection, not just the table.
    (Object.keys(categoryCounts).length ?
      '<div class="filter-row">' +
        '<div class="filter-group">' +
          '<label>Filter by Category</label>' +
          '<select id="importCategoryFilterSelect" class="select-w200">' +
            '<option value="">All Categories (' + dateRangeCount + ')</option>' +
            categoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Sub Category</label>' +
          '<select id="importSubCategoryFilterSelect" class="select-w260">' +
            '<option value="">All Sub Categories (' + subCategoryBaseCount + ')</option>' +
            subCategoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Flags</label>' +
          '<select id="importFlagsFilterSelect" class="select-w240">' +
            '<option value="">All Flags</option>' +
            flagsOptionsHtml +
          '</select>' +
        '</div>' +
        ((state.importCategoryFilter || state.importSubCategoryFilter || ['qtydisc', 'qtydiscmir', 'ratedisc', 'critical', 'flags'].includes(state.importStatusFilter) || (typeof state.importStatusFilter === 'string' && state.importStatusFilter.startsWith('cat:'))) ? '<button id="importClearCategoryFilter">Clear</button>' : '') +
      '</div>' : '') +
    ((stageChartData.length || months.length) ?
      '<div class="chart-row">' +
        '<div class="chart-panel"><h4>Import Value Trend by Month Created</h4>' +
          (months.length ? '<div class="chart-box"><canvas id="importTrendChart"></canvas></div>' : '<div class="no-data-note">No dated POs in range to plot.</div>') +
        '</div>' +
        '<div class="chart-panel"><h4>Shipment Stage Breakdown</h4>' +
          (stageChartData.length ? '<div class="chart-box"><canvas id="importStageChart"></canvas></div>' : '<div class="no-data-note">No POs in range.</div>') +
        '</div>' +
      '</div>' : '') +
    // Its own container so a header-filter keystroke can replace just
    // this (see renderImportListRegion()), leaving the KPI row's count-up
    // and both charts above it untouched.
    '<div id="importListRegion">' + importListRegionHtml() + '</div>';

  wireKpiCountUps();
  wireImportListRegion();

  document.querySelectorAll('[data-kpi]').forEach(c => {
    if (!c.dataset.kpi) return; // disabled (MIR-placeholder) card - no filter to set
    c.onclick = () => {
      const key = c.dataset.kpi;
      state.importStatusFilter = (state.importStatusFilter === key || key === 'total') ? null : key;
      state.importTablePage = 1;
      renderImportPoList(el);
      // Same reasoning as po-list.js/materials.js - the list sits below the
      // KPI row and the chart panel, so a narrowing click is otherwise
      // invisible. 'total' clears rather than narrows, so it never scrolls.
      if (state.importStatusFilter) revealFilteredList('importListRegion');
    };
  });
  document.getElementById('importApplyFilter').onclick = () => {
    state.importFrom = document.getElementById('importFromDate').value || null;
    state.importTo = document.getElementById('importToDate').value || null;
    state.importTablePage = 1;
    renderImportPoList(el);
  };
  document.getElementById('importClearFilter').onclick = () => { state.importFrom = null; state.importTo = null; state.importTablePage = 1; renderImportPoList(el); };
  document.getElementById('importRodtepBtn').onclick = () => openRodtepPanel();
  document.getElementById('importAdvanceLicenseBtn').onclick = () => openAdvanceLicensePanel();
  const importCategorySelect = document.getElementById('importCategoryFilterSelect');
  if (importCategorySelect) importCategorySelect.onchange = () => { state.importCategoryFilter = importCategorySelect.value || null; state.importSubCategoryFilter = null; state.importTablePage = 1; renderImportPoList(el); };
  const importSubCategorySelect = document.getElementById('importSubCategoryFilterSelect');
  if (importSubCategorySelect) importSubCategorySelect.onchange = () => { state.importSubCategoryFilter = importSubCategorySelect.value || null; state.importTablePage = 1; renderImportPoList(el); };
  const importFlagsSelect = document.getElementById('importFlagsFilterSelect');
  if (importFlagsSelect) importFlagsSelect.onchange = () => { state.importStatusFilter = importFlagsSelect.value || null; state.importTablePage = 1; renderImportPoList(el); };
  const importClearCategoryBtn = document.getElementById('importClearCategoryFilter');
  if (importClearCategoryBtn) importClearCategoryBtn.onclick = () => {
    state.importCategoryFilter = null; state.importSubCategoryFilter = null;
    if (['qtydisc', 'qtydiscmir', 'ratedisc', 'critical', 'flags'].includes(state.importStatusFilter) || (typeof state.importStatusFilter === 'string' && state.importStatusFilter.startsWith('cat:'))) state.importStatusFilter = null;
    state.importTablePage = 1; renderImportPoList(el);
  };

  destroyPageCharts();
  if (months.length) {
    try {
      const ctx = document.getElementById('importTrendChart');
      const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, 'rgba(29,138,150,0.9)');
      gradient.addColorStop(1, 'rgba(29,138,150,0.25)');
      pageCharts.push(new Chart(ctx, {
        type: 'bar',
        data: {
          labels: months.map(formatMonthLabel),
          datasets: [{
            data: months.map(m => monthTotals[m]),
            backgroundColor: months.map(m => m === state.importChartMonthFilter ? '#0f1b2d' : gradient),
            hoverBackgroundColor: '#1d8a96',
            borderRadius: 6, borderSkipped: false, maxBarThickness: 46,
          }],
        },
        options: {
          maintainAspectRatio: false,
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const clickedMonth = months[elements[0].index];
            state.importChartMonthFilter = state.importChartMonthFilter === clickedMonth ? null : clickedMonth;
            state.importTablePage = 1;
            renderImportPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8,
              titleFont: { size: 12, weight: '700' }, bodyFont: { size: 12 }, displayColors: false,
              callbacks: { label: c => formatInr(c.parsed.y), afterLabel: () => 'Click to filter the list below' },
            },
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
            y: { grid: { color: '#eef1f5' }, border: { display: false }, ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) } },
          },
        },
      }));
    } catch (e) {
      console.error('Import trend chart failed to render:', e);
      const box = document.getElementById('importTrendChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
  if (stageChartData.length) {
    try {
      const ctx = document.getElementById('importStageChart');
      pageCharts.push(new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: stageChartData.map(s => s.label),
          datasets: [{
            data: stageChartData.map(s => s.val),
            backgroundColor: stageChartData.map(s => s.color),
            borderWidth: 0, spacing: 3, borderRadius: 4, hoverOffset: 8,
            offset: stageChartData.map(s => state.importStatusFilter === s.key ? 14 : 0),
          }],
        },
        options: {
          maintainAspectRatio: false, cutout: '70%',
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const key = stageChartData[elements[0].index].key;
            state.importStatusFilter = state.importStatusFilter === key ? null : key;
            state.importTablePage = 1;
            renderImportPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                boxWidth: 9, boxHeight: 9, padding: 12, font: { size: 10.5 },
                generateLabels: chart => {
                  const vals = chart.data.datasets[0].data;
                  const totalV = vals.reduce((a, b) => a + b, 0);
                  return chart.data.labels.map((label, i) => ({
                    text: label + '  ' + vals[i] + ' (' + (totalV ? Math.round(vals[i] / totalV * 100) : 0) + '%)',
                    fillStyle: chart.data.datasets[0].backgroundColor[i],
                    strokeStyle: chart.data.datasets[0].backgroundColor[i],
                    index: i,
                  }));
                },
              },
            },
            tooltip: {
              backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8,
              callbacks: {
                label: c => {
                  const totalV = c.dataset.data.reduce((a, b) => a + b, 0);
                  return ' ' + c.label + ': ' + c.parsed + (totalV ? ' (' + Math.round(c.parsed / totalV * 100) + '%)' : '');
                },
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
        },
        plugins: [centerImportTextPlugin],
      }));
    } catch (e) {
      console.error('Import stage chart failed to render:', e);
      const box = document.getElementById('importStageChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
}

// ── The list region (heading + header filters + rows + pagination) ──────
// Split out of renderImportPoList() on 2026-09-19, same change and same
// reasoning as Domestic's own PO_LIST_CTX and materials.js's MAT_LIST_CTX
// (see materials.js for the full story): every [data-icf] header filter
// writes into state.importColFilters, which applyImportColFilters() applies
// to `tableRecs` alone - so the KPI row and both charts, built from
// `filtered`, cannot change and were being rebuilt on every keystroke for
// nothing.
let IMPORT_LIST_CTX = null;

function importListRegionHtml() {
  const ctx = IMPORT_LIST_CTX;
  const filtered = ctx.filtered;
  const poInwarded = ctx.poInwarded;
  const poQtyDiscMir = ctx.poQtyDiscMir;
  const poRateDiscMir = ctx.poRateDiscMir;
  const cf = state.importColFilters;
  // Built HERE, not handed over in IMPORT_LIST_CTX: each cell carries its
  // filter's CURRENT value, so a snapshot taken at full-render time would
  // rewrite the box being typed into back to its pre-keystroke value - the
  // list would narrow correctly while the text vanished under the cursor.
  // No Value column filter here - min/max value narrowing was removed
  // (project owner, 2026-09-04), same as Domestic's own table.
  const filterCells = [
    '<input type="text" class="col-filter-input" data-icf="poNumber" placeholder="Search..." value="' + escapeHtml(cf.poNumber) + '">',
    '<input type="text" class="col-filter-input" data-icf="vendor" placeholder="Search..." value="' + escapeHtml(cf.vendor) + '">',
    '<input type="text" class="col-filter-input" data-icf="country" placeholder="Search..." value="' + escapeHtml(cf.country) + '">',
    '',
    '',
    '<select class="col-filter-input" data-icf="stage"><option value="">All</option>' +
      IMPORT_STAGES.map(s => '<option value="' + s + '"' + (cf.stage === s ? ' selected' : '') + '>' + escapeHtml(IMPORT_STAGE_LABELS[s]) + '</option>').join('') +
    '</select>',
    '',
  ];

  let tableRecs = filtered;
  const sf = state.importStatusFilter;
  if (sf === 'inwarded') tableRecs = filtered.filter(poInwarded);
  else if (sf === 'partial') tableRecs = filtered.filter(p => p.partialDelivery);
  else if (sf === 'qtydisc') tableRecs = filtered.filter(poQtyDiscPo);
  else if (sf === 'qtydiscmir') tableRecs = filtered.filter(poQtyDiscMir);
  else if (sf === 'ratedisc') tableRecs = filtered.filter(poRateDiscMir);
  else if (sf === 'critical') tableRecs = filtered.filter(p => p._qtyFlag || p._rateFlag);
  else if (sf === 'overdue') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'Overdue');
  else if (sf === 'onorder') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'On Order');
  else if (sf === 'unknowndate') tableRecs = filtered.filter(p => p.deliveryDateStatus === 'Unknown');
  else if (sf === 'flags') tableRecs = filtered.filter(po => po._categories.length > 0);
  // Per-category filter (added 2026-09-08) - 'cat:<label>' namespacing, same
  // convention/reasoning as Domestic's own po-list.js.
  else if (typeof sf === 'string' && sf.startsWith('cat:')) {
    const wantedLabel = sf.slice(4);
    tableRecs = filtered.filter(po => po._categories.some(c => c.label === wantedLabel));
  }
  else if (sf === 'placed') tableRecs = filtered.filter(p => p.shipmentStage === 'Placed');
  else if (sf === 'shipped') tableRecs = filtered.filter(p => p.shipmentStage === 'Shipped (BL)');
  else if (sf === 'cleared') tableRecs = filtered.filter(p => p.shipmentStage === 'Cleared (BOE)');
  if (state.importChartMonthFilter) tableRecs = tableRecs.filter(po => (po.createdDate || '').slice(0, 7) === state.importChartMonthFilter);
  tableRecs = applyImportColFilters(tableRecs);
  tableRecs = tableRecs.slice().sort((a, b) => (b.createdDate || '').localeCompare(a.createdDate || ''));

  const showingAll = state.importShowAllPOs;
  const totalForList = tableRecs.length;
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(totalForList / PAGE_SIZE));
  const tablePage = Math.min(Math.max(1, state.importTablePage), totalPages);
  const listRecs = showingAll ? tableRecs.slice((tablePage - 1) * PAGE_SIZE, tablePage * PAGE_SIZE) : tableRecs.slice(0, 5);
  // Read back by wireImportListRegion(), which has to clamp the same way
  // rather than re-deriving a second page count that could disagree.
  ctx.totalPages = totalPages;


  const activeFilterCount =
    (state.importChartMonthFilter ? 1 : 0) +
    (state.importStatusFilter ? 1 : 0) +
    (state.importFrom || state.importTo ? 1 : 0) +
    (state.importCategoryFilter ? 1 : 0) + (state.importSubCategoryFilter ? 1 : 0) +
    (cf.poNumber ? 1 : 0) + (cf.vendor ? 1 : 0) + (cf.country ? 1 : 0) + (cf.stage ? 1 : 0);

  return '<div class="list-toggle-row"><div class="section-title m-0">Import Purchase Orders (Latest first)</div>' +
      (listRecs.some(po => po._qtyFlag || po._rateFlag) ? rowTintLegendHtml() : '') +
      '<div class="flex-row-gap10">' +
        (activeFilterCount ? '<span class="clear-list-filters" id="importClearListFilters">' + activeFilterCount + ' filter' + (activeFilterCount > 1 ? 's' : '') + ' active &middot; Clear &times;</span>' : '') +
        (totalForList > 5 ? '<button class="view-all-btn" id="importToggleAllBtn">' + (showingAll ? 'Show top 5' : 'View all') + '</button>' : '') +
      '</div>' +
    '</div>' +
    (() => {
      if (showingAll) {
        const colFilterRow = '<tr class="col-filter-row">' + filterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';
        const pageButtons = totalPages <= 10
          ? Array.from({ length: totalPages }, (_, i) => i + 1)
              .map(p => '<button class="page-btn page-num' + (p === tablePage ? ' active' : '') + '" data-impage="' + p + '">' + p + '</button>')
              .join('')
          : '<span class="page-info">Page ' + tablePage + ' of ' + totalPages + '</span>';
        const paginationHtml = totalPages > 1
          ? '<div class="pagination-row">' +
              '<button id="importPrevPageBtn" class="page-btn"' + (tablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
              pageButtons +
              '<button id="importNextPageBtn" class="page-btn"' + (tablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
              jumpToPageHtml('import', totalPages) +
            '</div>'
          : '';
        return '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Country of Origin</th><th>Value (Incl.)</th><th>BL Number</th><th>Shipment Stage</th><th>Details</th></tr>' + colFilterRow + '</thead>' +
          '<tbody>' + listRecs.map(po => {
            const key = escapeHtml(po.plant + '::' + po.poNumber);
            return '<tr class="' + rowTintClass(po).trim() + '"><td><b>' + escapeHtml(po.poNumber) + '</b></td>' +
              '<td>' + escapeHtml(po.vendorName || '-') + '</td>' +
              '<td>' + escapeHtml(po.countryOfOrigin || '-') + '</td>' +
              '<td>' + (po.totalInclusiveValue != null ? formatInr(po.totalInclusiveValue) : '-') + '</td>' +
              '<td>' + blNumberCellHtml(po, '-') + '</td>' +
              '<td><span class="status-pill ' + IMPORT_STAGE_PILL_CLASS[po.shipmentStage] + '">' + escapeHtml(po.shipmentStage) + '</span>' + importRowFlags(po) + '</td>' +
              '<td><span class="row-link" data-impo="' + key + '">View details</span></td></tr>';
          }).join('') + '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row grid-cols"><div>PO Number</div><div>Vendor</div><div>Country of Origin</div><div>Value (Incl.)</div><div>BL Number</div><div>Shipment Stage</div><div>Details</div></div>' +
        '<div class="list-header-row grid-cols col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="importTop5List">' + listRecs.map(po => {
          const key = escapeHtml(po.plant + '::' + po.poNumber);
          return '<div class="top5-row' + rowTintClass(po) + '">' +
            '<div><span class="po-num">' + escapeHtml(po.poNumber) + '</span></div>' +
            '<div>' + escapeHtml(po.vendorName || 'Not available') + '</div>' +
            '<div>' + escapeHtml(po.countryOfOrigin || 'Not available') + '</div>' +
            '<div>' + (po.totalInclusiveValue != null ? formatInr(po.totalInclusiveValue) : 'Not available') + '</div>' +
            '<div>' + blNumberCellHtml(po, 'Not available') + '</div>' +
            '<div><span class="status-pill ' + IMPORT_STAGE_PILL_CLASS[po.shipmentStage] + '">' + escapeHtml(po.shipmentStage) + '</span>' + importRowFlags(po) + '</div>' +
            '<div><span class="row-link" data-impo="' + key + '">View details</span></div></div>';
        }).join('') + '</div>';
    })();
}

/** Re-renders the list region alone, in place. Falls back to a full
 * renderImportPoList() if the region (or the context it needs) isn't there. */
function renderImportListRegion() {
  const region = document.getElementById('importListRegion');
  if (!region || !IMPORT_LIST_CTX) { renderImportPoList(document.getElementById('content')); return; }
  preserveFocus(region, () => { region.innerHTML = importListRegionHtml(); });
  applyDynamicStyles(region); // the row-tint legend's dots - see shared.js's own comment
  wireImportListRegion();
}

function wireImportListRegion() {
  const region = document.getElementById('importListRegion');
  if (!region) return;
  const el = IMPORT_LIST_CTX ? IMPORT_LIST_CTX.el : region.parentNode;
  const totalPages = IMPORT_LIST_CTX ? IMPORT_LIST_CTX.totalPages : 1;

  const toggleBtn = document.getElementById('importToggleAllBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.importShowAllPOs = !state.importShowAllPOs; state.importTablePage = 1; renderImportListRegion(); };
  region.querySelectorAll('[data-impo]').forEach(el2 => el2.onclick = () => openImportPoModal(el2.dataset.impo));
  region.querySelectorAll('[data-track-bl]').forEach(el2 => el2.onclick = (e) => { e.stopPropagation(); trackBlNumber(el2.dataset.trackBl); });

  const prevPageBtn = document.getElementById('importPrevPageBtn');
  if (prevPageBtn) prevPageBtn.onclick = () => { state.importTablePage = Math.max(1, state.importTablePage - 1); renderImportListRegion(); };
  const nextPageBtn = document.getElementById('importNextPageBtn');
  if (nextPageBtn) nextPageBtn.onclick = () => { state.importTablePage = state.importTablePage + 1; renderImportListRegion(); };
  region.querySelectorAll('[data-impage]').forEach(btn => btn.onclick = () => { state.importTablePage = Number(btn.dataset.impage); renderImportListRegion(); });
  wireJumpToPage('import', totalPages, (n) => { state.importTablePage = n; renderImportListRegion(); });

  // Clears the "global" date/category/status filters too, so this one has
  // to go through the full render.
  const clearListFiltersBtn = document.getElementById('importClearListFilters');
  if (clearListFiltersBtn) clearListFiltersBtn.onclick = () => {
    state.importStatusFilter = null; state.importChartMonthFilter = null; state.importFrom = null; state.importTo = null;
    state.importCategoryFilter = null; state.importSubCategoryFilter = null; state.importTablePage = 1;
    state.importColFilters = { poNumber: '', vendor: '', country: '', stage: '' };
    renderImportPoList(el);
  };

  // Every one of these is a table-only filter (see applyImportColFilters()),
  // so they all re-render this region only. The text boxes are debounced
  // (shared.js's debounceRender()); the Stage <select> fires on 'change',
  // already one committed event.
  region.querySelectorAll('[data-icf]').forEach(inp => {
    const key = inp.dataset.icf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'number') ? 'change' : 'input';
    const rerender = eventName === 'change' ? () => renderImportListRegion() : debounceRender(() => renderImportListRegion());
    inp.addEventListener(eventName, () => {
      state.importColFilters[key] = inp.value;
      state.importTablePage = 1;
      rerender();
    });
  });
}



// compositeKey is "<plant>::<poNumber>" (po.plant is already the imports
// API's plant key - see currentImportPOs()'s comment). Unlike openPoModal()
// this needs a real fetch: the list payload already carries everything the
// KPIs/charts/table need, but not the Overview tab's vendor address/GSTIN/
// email/code/billing/ship-to/currency/remarks (detail-only fields, see
// imports_views.py's _po_dict()) - fetched once per PO and cached.
async function openImportPoModal(compositeKey) {
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const poNumber = compositeKey.slice(sep + 2);

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
  body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="load-banner"><div class="spinner"></div><div>Loading&hellip;</div></div>';

  let po;
  try {
    po = await ensureImportPoDetailLoaded(plantKey, poNumber);
  } catch (e) {
    console.error('Failed to load import PO detail:', e);
    if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
    body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="noaccess">Couldn\'t load this purchase order. Please close and try again.</div>';
    return;
  }
  // See po-modal.js's openPoModal() for why this loads all 3 plants (the
  // "View full material analysis" link's "All plants total" figure) and is
  // best-effort (a failure here just means no material-analysis links on
  // this open, not a broken modal).
  try {
    await ensureMaterialsLoaded(PLANT_KEYS);
  } catch (e) {
    console.error('openImportPoModal: ensureMaterialsLoaded failed:', e);
  }
  if (myModalRequestId !== modalRequestId) return; // a newer modal open superseded this one
  renderImportPoModalBody(plantKey, poNumber, po);
}

async function ensureImportPoDetailLoaded(plantKey, poNumber, force) {
  const cacheKey = plantKey + '|' + poNumber;
  if (IMPORT_PO_DETAIL_CACHE[cacheKey] && !force) return IMPORT_PO_DETAIL_CACHE[cacheKey];
  const data = await apiImports('/purchase-orders/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(poNumber));
  IMPORT_PO_DETAIL_CACHE[cacheKey] = data;
  return data;
}

let importModalTab = 'overview';

// editableLine/plainLine/wireEditIcons/savePoField are shared with
// Domestic's openPoModal() - see frontend/js/shared.js. itemId is omitted
// for a PO-level field, present for a line-item field (matches
// correct_field's own PO-level-vs-item-level branch on both the Domestic
// and Import backends).
function renderImportPoModalBody(plantKey, poNumber, po) {
  importModalTab = importModalTab || 'overview';
  const body = document.getElementById('modalBody');
  const apiBase = '/api/imports';
  const edit = (label, value, field, itemId, fieldType, options) => editableLine(plantKey, label, value, field, itemId, fieldType, options);

  const currencyOptions = distinctFieldValues(IMPORT_PO_CACHE || [], p => p.currency);
  const taxTypeOptions = Array.from(new Set(
    (IMPORT_PO_CACHE || []).flatMap(p => (p.items || []).map(i => i.taxType)).filter(Boolean)
  )).sort();

  // Same trimmed set as the Domestic modal (2026-09-24) - see po-modal.js's
  // own comment on why vendor address/GSTIN/email/code left this tab and
  // why "Packing and Incoterms" carries Incoterms and Payment Terms.
  const overviewHtml =
    '<div class="po-overview"><div class="field-grid">' +
      '<div class="field-block"><h4>Purchase Order</h4>' +
        plainLine('PO No', po.poNumber) +
        edit('Created on', po.createdDate, 'po_created_date', null, 'date') +
        plainLine('Plant', po.plantLabel + ' (Imports)') +
      '</div>' +
      '<div class="field-block value-card"><h4>Value</h4>' +
        edit('Total Value (in ' + (po.currency || 'PO currency') + ')', po.totalValue, 'total_value', null, 'number') +
        edit('Currency (as per PO)', po.currency, 'currency', null, 'select', currencyOptions) +
      '</div>' +
      '<div class="field-block"><h4>Vendor</h4>' +
        edit('Vendor Name', po.vendorName, 'vendor_name') +
      '</div>' +
      '<div class="field-block"><h4>Packing and Incoterms</h4>' +
        edit('Incoterms', po.incoterms, 'incoterms') +
        edit('Payment Terms', po.paymentTerms, 'payment_terms') +
      '</div>' +
      '<div class="field-block"><h4>Billing Address</h4>' +
        edit('Billing Address', po.billingAddress, 'billing_address') +
      '</div>' +
      '<div class="field-block"><h4>Shipping Address</h4>' +
        edit('Ship To', po.shipTo, 'ship_to') +
      '</div>' +
    '</div>' +
    '<div class="field-block full-width mt-14"><h4>Remarks</h4>' + edit('Remarks', po.remarks, 'remarks') + '</div></div>';

  // One reconciliation card per line (po-reconcile.js). An import line is
  // compared as the matcher compares it: BOE quantity, INR rate and landed
  // INR value against MIR, with the PO quantity and the currency conversion
  // shown alongside. The PO-vs-BOE variance the old table carried is a
  // separate, customs-side check and stays on the Flags tab.
  const reconLines = (po.items || []).map((it, i) => importReconLine(it, i + 1, po, plantKey));
  const itemsTabHtml = reconItemsHtml(reconLines, plantKey, 'INR') +
    // Rendered once and moved under whichever line's "change" was
    // clicked - shared with the Domestic modal (po-modal.js).
    mirPickerHtml();

  const shipmentTabHtml = '<div class="mb-block-16">' + shipmentStepperHtml(po) + '</div>' +
    (po.items || []).map(it =>
      '<div class="field-grid">' +
        '<div class="field-block"><h4>Shipment and Customs' + (it.itemId ? ' - Item ' + escapeHtml(it.itemId) : '') + '</h4>' +
          edit('Bill of Lading No.', it.billOfLadingNumber, 'bill_of_lading_number', it.itemId) +
          (it.billOfLadingNumber ? '<div class="line"><span class="row-link" data-track-bl="' + escapeHtml(it.billOfLadingNumber) + '">Track this shipment</span></div>' : '') +
          edit('Laden on Board date', it.ladenOnBoardDate, 'laden_on_board_date', it.itemId, 'date') +
          edit('BOE (Bill of Entry) No.', it.boeNumber, 'boe_number', it.itemId) +
          edit('Exchange Rate', it.exchangeRate, 'exchange_rate', it.itemId, 'number') +
          edit('Tax Type', it.taxType, 'tax_type', it.itemId, 'select', taxTypeOptions) +
          plainLine('Total Inclusive Value', it.totalInclusiveValue != null ? formatInr(it.totalInclusiveValue) : null) +
        '</div>' +
        '<div class="field-block"><h4>Export Incentive / License Scheme' + (it.itemId ? ' - Item ' + escapeHtml(it.itemId) : '') + '</h4>' +
          edit('License Type', it.licenseType, 'license_type', it.itemId) +
          edit('License / Scrip Number(s)', it.licenseNumber, 'license_number', it.itemId) +
        '</div>' +
      '</div>'
    ).join('');

  // Critical (Qty/Rate discrepancy) flags first, then the F1-F7 data
  // quality flags - previously this tab only ever showed the latter (see
  // importCriticalFlagsFor()'s own comment for why that was an
  // inconsistency with Domestic's equivalent tab, which always showed both).
  const criticalCats = importCriticalFlagsFor(po);
  const FLAG_DESCRIPTIONS_NOTE = 'Each flag below is a real data-quality check against this PO\'s own synced fields (apps/services/import_flags.py) - not a placeholder.';
  const flagsTabHtml =
    criticalCats.map(c => poFlagHtml(c, po, plantKey, true)).join('') +
    ((po.dataQualityFlags || []).length
      ? '<div class="no-data-note note-block">' + FLAG_DESCRIPTIONS_NOTE + '</div>' +
        po.dataQualityFlags.map(f => importFlagHtml(f, po, plantKey)).join('')
      : (criticalCats.length ? '' : '<div class="empty-note">No data quality flags on this PO.</div>'));
  const correctionsHtml = (po.corrections || []).length
    ? '<div class="section-title mt-18">Correction History</div>' +
      po.corrections.map(c =>
        '<div class="field-block mb-8">' +
          '<div class="fs-12-5"><b>' + escapeHtml(c.fieldName) + (c.itemId ? ' (item ' + escapeHtml(c.itemId) + ')' : '') + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') +
          // Unlike Domestic, an Import line item DOES have a real, stable
          // item_id (ImportPOCorrection keys on it), so an item-level
          // correction can be reverted here too - see shared.js's
          // wireRevertLinks() and po-modal.js's narrower version.
          (canEditField(plantKey)
            ? '<span class="revert-link" data-field="' + escapeHtml(c.fieldName) + '"' +
              ' data-label="' + escapeHtml(c.fieldName) + '"' +
              (c.itemId ? ' data-item="' + escapeHtml(c.itemId) + '"' : '') +
              ' data-old-value="' + escapeHtml(c.oldValue || '') + '">revert</span>'
            : '') +
          '</div>' +
          (c.reason ? '<div class="mt-4 fs-12 italic text-slate">"' + escapeHtml(c.reason) + '"</div>' : '') +
          '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? c.correctedAt.slice(0, 10) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const correctionBoxHtml = overrideBoxHtml('Click the ✎ beside any field to correct it.');

  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(po.poNumber) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(po.vendorName || 'Unknown vendor') + ' &middot; ' + escapeHtml(po.plantLabel) + ' (Imports) &middot; Country of Origin: ' + escapeHtml(po.countryOfOrigin || 'Not available') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="modal-tabs" id="importPoModalTabs" role="tablist">' +
      '<div class="modal-tab' + (importModalTab === 'overview' ? ' active' : '') + '" data-itab="overview" tabindex="0" role="tab" aria-selected="' + (importModalTab === 'overview') + '">Overview</div>' +
      '<div class="modal-tab' + (importModalTab === 'items' ? ' active' : '') + '" data-itab="items" tabindex="0" role="tab" aria-selected="' + (importModalTab === 'items') + '">Items &amp; MIR</div>' +
      '<div class="modal-tab' + (importModalTab === 'shipment' ? ' active' : '') + '" data-itab="shipment" tabindex="0" role="tab" aria-selected="' + (importModalTab === 'shipment') + '">Shipment &amp; License</div>' +
      '<div class="modal-tab' + (importModalTab === 'flags' ? ' active' : '') + '" data-itab="flags" tabindex="0" role="tab" aria-selected="' + (importModalTab === 'flags') + '">Flags &amp; Corrections</div>' +
    '</div>' +
    '<div class="modal-tab-panel" id="importPoModalOverview"' + (importModalTab !== 'overview' ? ' hidden' : '') + '>' + overviewHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalItems"' + (importModalTab !== 'items' ? ' hidden' : '') + '>' + itemsTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalShipment"' + (importModalTab !== 'shipment' ? ' hidden' : '') + '>' + shipmentTabHtml + '</div>' +
    '<div class="modal-tab-panel" id="importPoModalFlags"' + (importModalTab !== 'flags' ? ' hidden' : '') + '>' + flagsTabHtml + correctionsHtml + correctionBoxHtml + '</div>';

  body.querySelectorAll('[data-itab]').forEach(tab => tab.onclick = () => {
    importModalTab = tab.dataset.itab;
    body.querySelectorAll('[data-itab]').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    ['overview', 'items', 'shipment', 'flags'].forEach(k => {
      document.getElementById('importPoModal' + k.charAt(0).toUpperCase() + k.slice(1)).hidden = k !== importModalTab;
    });
  });

  const fieldsUrl = apiBase + '/purchase-orders/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(poNumber) + '/fields';
  const switchToFlagsTab = () => { const t = body.querySelector('[data-itab="flags"]'); if (t) t.click(); };
  wireEditIcons(body, fieldsUrl, switchToFlagsTab, () => onImportFieldSaved(plantKey, poNumber));
  wireRevertLinks(body, fieldsUrl, () => onImportFieldSaved(plantKey, poNumber));
  // A manual MIR pin re-runs the whole plant's matching (domestic AND import
  // line items compete for the same MIR table), so the full invalidate+reload
  // is the right refresh here, not a modal-only one.
  const mirBase = '/purchase-orders/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(poNumber);
  wireMirPicker(body, {
    // itemRef lets the server mark this line's Bill of Entry siblings, which
    // share a BOE-booked receipt rather than lose it (sharesReceipt).
    candidates: (q, itemRef) => apiImports(mirBase + '/mir-candidates?itemRef=' + encodeURIComponent(itemRef || '') +
      (q ? '&q=' + encodeURIComponent(q) : '')),
    save: (payload) => apiImports(mirBase + '/mir-match',
      { method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  }, poNumber, () => onImportFieldSaved(plantKey, poNumber));
  wireDismissLinks(body, plantKey, () => onImportFieldSaved(plantKey, poNumber));
  applyDynamicStyles(body); // the reconciliation cards' progress bars
  body.querySelectorAll('[data-track-bl]').forEach(el2 => el2.onclick = () => trackBlNumber(el2.dataset.trackBl));
  body.querySelectorAll('[data-material-link]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.materialLink));
}

// A corrected field can move a PO in/out of a KPI bucket (e.g. fixing a bad
// Tax Type can clear an F2 flag) - invalidate both caches so the list,
// KPIs, charts and this modal all reflect the new value rather than a stale
// one until the next unrelated refresh.
async function onImportFieldSaved(plantKey, poNumber) {
  IMPORT_PO_CACHE = null;
  await ensureImportPOsLoaded();
  const fresh = await ensureImportPoDetailLoaded(plantKey, poNumber, true);
  renderImportPoModalBody(plantKey, poNumber, fresh);
  const content = document.getElementById('content');
  if (content && state.purchaseType === 'import') renderImportPoList(content);
}

