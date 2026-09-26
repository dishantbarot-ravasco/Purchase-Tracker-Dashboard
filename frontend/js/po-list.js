// ── PO list rendering (Domestic Purchases) ──────────────────────────────
/** Renders the whole Purchase Orders view for the current plant/filter
 * selection: KPI row, chart row, and either the top-5 preview or the full
 * "View all" table, wiring every click/filter handler to re-render itself.
 * Delegates to renderImportPoList() when purchaseType is 'import' (see that
 * function's own header comment for why it's separate, not a branch here). */
// The Value column: incl. tax, else the PO's value before tax - the same
// figure the month chart plots - marked as such, rather than "-".
function poValueCellHtml(po) {
  if (po.totalInclTax != null) return formatInr(po.totalInclTax);
  if (po.totalValue != null) return formatInr(po.totalValue) + ' <span class="text-slate-soft fs-11" title="No tax-inclusive total on file">excl. tax</span>';
  return '-';
}

function poHasCriticalCategory(po) {
  return (po._categories || []).some(c => c.severity === 'critical');
}

function renderPoList(el) {
  // Import Purchases render through the exact same KPI-row -> chart-row ->
  // list -> drill-down-modal scaffold as Domestic (this function), just
  // fed from apps/api/routers/imports_views.py's combined cross-plant data
  // instead of currentPOs() - see renderImportPoList() below. Kept as a
  // separate function rather than branching this one line-by-line: the two
  // datasets share the same *shape* of UI but genuinely different fields
  // (shipment stage/BOE/BL/country-of-origin vs PO<->MIR match status), and
  // interleaving both field sets into one function would be harder to
  // follow than two functions that are each internally consistent.
  if (state.purchaseType === 'import') {
    renderImportPoList(el);
    return;
  }

  const all = currentPOs();
  all.forEach(po => {
    po._status = computeStatus(po);
    po._deliveryDate = computePoDeliveryDate(po);
    computePoFlags(po);
  });

  if (!all.length) {
    const msg = isAllPlants()
      ? 'No purchase orders synced yet for any plant.'
      : 'No purchase orders synced yet - run <code>' + PLANTS[state.plant].syncCmdPoCsv + '</code> to load them.';
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml(msg) + '</div>';
    return;
  }

  const inRange = po => {
    if (state.from && (!po.createdDate || po.createdDate < state.from)) return false;
    if (state.to && (!po.createdDate || po.createdDate > state.to)) return false;
    return true;
  };
  let filtered = all.filter(inRange);

  // Category/Sub Category counts computed BEFORE state.categoryFilter/
  // subCategoryFilter narrow `filtered` further, so the dropdowns keep
  // showing every option's count while one is selected - same cascading
  // pattern as Raw Material Analysis's own Category/Sub Category pair.
  // Category/Sub Category here are the MATERIAL category/subcategory a PO's
  // own line items belong to (apps/api/routers/_domestic_base.py's
  // materialCategories field, backed by MaterialCategoryReference - same
  // canonical lookup the Raw Material Analysis page uses) - a PO can touch
  // more than one category, so it's counted once per DISTINCT category it
  // touches (a Set per PO), not once per line item. This is a 2026-09-08
  // change (project owner): "Category"/"Sub Category" on this page used to
  // mean Data Quality Flag severity/label instead, which was never material
  // data at all - that filtering moved into "Filter by Flags" below
  // (Critical Issues/Informational Issues), not lost, just relabeled to
  // where it actually belongs. All three dropdowns are still "global"
  // filters like from/to above: they narrow `filtered` itself, so the KPI
  // counts and both charts below reflect the selection, not just the table.
  const categoryCounts = {};
  filtered.forEach(po => new Set((po.materialCategories || []).map(c => c.category)).forEach(cat => {
    categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
  }));
  const dateRangeCount = filtered.length; // "All Categories (N)" option's count - before category narrowing
  const inSelectedCategory = state.categoryFilter
    ? filtered.filter(po => (po.materialCategories || []).some(c => c.category === state.categoryFilter))
    : filtered;
  const subCategoryCounts = {};
  inSelectedCategory.forEach(po => new Set(
    (po.materialCategories || [])
      .filter(c => !state.categoryFilter || c.category === state.categoryFilter)
      .map(c => c.subCategory || 'Uncategorized')
  ).forEach(sub => { subCategoryCounts[sub] = (subCategoryCounts[sub] || 0) + 1; }));
  const subCategoryBaseCount = inSelectedCategory.length; // "All Sub Categories (N)" option's count
  if (state.categoryFilter) filtered = filtered.filter(po => (po.materialCategories || []).some(c => c.category === state.categoryFilter));
  if (state.subCategoryFilter) // The sub-category must sit under the chosen category on the SAME entry,
  // not merely somewhere on the PO.
  filtered = filtered.filter(po => (po.materialCategories || []).some(c => (c.subCategory || 'Uncategorized') === state.subCategoryFilter && (!state.categoryFilter || c.category === state.categoryFilter)));

  const counts = { received: 0, partial: 0, pending: 0, overdue: 0, unknown: 0 };
  filtered.forEach(po => counts[po._status]++);
  // Overdue is an OVERLAY over the status buckets, not one of them
  // (2026-09-18) - a PO past its delivery date with anything still
  // outstanding counts, whether or not part of it has already arrived. See
  // computeStatus()'s own note for the 8-vs-34 undercount this fixes. The
  // consequence is that the status cards no longer sum to Total PO's
  // Created, which is deliberate: an overdue PO is also counted under
  // Partial or On Order.
  const overdueCount = filtered.filter(po => po._overdue).length;
  // Also an overlay - see computeStatus()'s note on _noDeliveryDate.
  const noDateCount = filtered.filter(po => po._noDeliveryDate).length;
  const total = filtered.length;
  const qtyDiscCount = filtered.filter(po => po._qtyFlag).length;
  // Over vs under delivery (2026-09-18, project owner). Tolerance is still
  // zero - this splits the same flagged set by DIRECTION, so the two add up
  // to qtyDiscCount except where a PO has both (several line items, one over
  // and one short), which is why they are counted independently rather than
  // as a partition. See flags.js's computePoFlags().
  const qtyOverCount = filtered.filter(po => po._qtyOverFlag).length;
  const qtyUnderCount = filtered.filter(po => po._qtyUnderFlag).length;
  // The combined Quantity Mismatches card is KEPT alongside the two
  // directional ones, and is not redundant with them. qtyOverDelivered is
  // NULL on any match row written before migration 0050 - i.e. every row on
  // a plant whose matching has not been re-run since - and NULL is counted
  // as neither direction (correctly: it means "not recorded", same as "could
  // not be determined"). Dropping the combined card would therefore make a
  // plant's entire quantity-mismatch count vanish from the KPI row until
  // someone happened to re-run matching for it, which is exactly the
  // regression this comment exists to prevent a future edit from
  // reintroducing. The three also genuinely disagree in normal operation: a
  // PO with several line items can be over on one and short on another, so
  // over + short can EXCEED the combined count as well as fall short of it.
  const rateDiscCount = filtered.filter(po => po._rateFlag).length;
  // "Data Quality Flags" used to mean "informational severity only"
  // (po._hasInfoFlag) - narrower than the name promised, and confusingly
  // disjoint from Quantity/Rate Mismatches (a PO could show 0 Data Quality
  // Flags while still having a completely invisible-to-this-KPI PO-Not-
  // Found/tax-type/taxable-value/final-amount problem). Redefined
  // 2026-09-08 (project owner: "add all the errors... under one KPI") to
  // mean ANY flag at all - every category in po._categories, critical and
  // info alike (Quantity/Rate/PO-Not-Found/Tax Type/Net-Taxable-Final Value/
  // UOM mismatches, plus the existing remarks-based info categories). See
  // flags.js's computePoFlags() for the full category list this now covers,
  // and flagCategoryCounts below for the per-category breakdown the
  // "Filter by Flags" dropdown now exposes instead of one opaque bucket.
  const flagsCount = filtered.filter(po => po._categories.length > 0).length;
  // "Critical Issues" in "Filter by Flags" below - the combined qty-OR-rate
  // count, filling in for what "Filter by Category" used to mean before
  // Category/Sub Category were repurposed for material data (2026-09-08) -
  // see that comment above for the full story.
  // Any critical category, the same set the red row flag and the legend
  // call critical - "PO Not Found in MIR" included. Counting qty/rate only
  // left 14 / 8 / 17 red-flagged POs (HRS/Achhad/Vapi) this option could not
  // find (2026-09-25).
  const criticalCount = filtered.filter(poHasCriticalCategory).length;
  // Per-category breakdown across every PO in the current date range/plant
  // selection - same aggregation pattern as categoryCounts/subCategoryCounts
  // above, just keyed on flag category label instead of material category.
  // Lets "Filter by Flags" list every actual issue type present (with its
  // own count) instead of the old fixed 4-option list, so a reviewer can
  // jump straight to e.g. "Taxable Value Mismatch in MIR (3)" instead of
  // opening every flagged PO to find out which ones have that problem.
  const flagCategoryCounts = {};
  filtered.forEach(po => po._categories.forEach(c => {
    flagCategoryCounts[c.label] = (flagCategoryCounts[c.label] || 0) + 1;
  }));

  // Order requested by the project owner (2026-09-04): Total -> Material
  // Inwarded -> Partial Delivered -> the two "critical" (money/quantity)
  // discrepancy cards -> Overdue -> Pending -> Date Unknown -> Data Quality
  // Flags last. Follow-up request (same day): every status card now carries
  // a colored flag icon, not just the discrepancy/attention ones - each
  // matching that status's own established color (status pills,
  // .kpi-card.<status> border colors) - and Overdue uses the same red as
  // the qty/rate discrepancy cards (moved out of the amber group it used to
  // share with Pending/Date Unknown), since an overdue PO is treated as
  // urgent, not merely a scheduling note. Total is the only card with no
  // flag icon - it's a plain aggregate, not a status.
  const cardDef = [
    { key: 'total', cls: '', label: "Total PO's Created", val: total, tip: 'All purchase orders in the selected date range and plant(s), narrowed by the Category and Sub Category filters when either is set.' },
    { key: 'received', cls: 'received', label: 'Material Inwarded', val: counts.received, flag: KPI_FLAG_COLORS.received, tip: 'Every line item on this PO has fully arrived - matched to a MIR entry, and not short of the ordered quantity. An over-delivered line still counts as received (the material did arrive); a short-delivered one does not, and shows as Partial Delivered instead.' },
    { key: 'partial', cls: 'partial', label: STATUS_LABELS.partial, val: counts.partial, flag: KPI_FLAG_COLORS.partial, tip: 'Something has arrived against this PO but the order is not complete - either a line item has no MIR entry yet, or one arrived short of the ordered quantity.' },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Mismatches', val: qtyDiscCount, flag: KPI_FLAG_COLORS.critical, tip: 'Quantity mismatch in MIR: quantity on the PO differs from its matched MIR entry - zero tolerance, any nonzero difference flags. The two cards beside this one split the same set by direction; they can add up to less than this total, which means some of these rows have no recorded direction yet (their last matching run predates the over/under split - re-run matching for this plant).' },
    { key: 'qtyover', cls: 'critical', label: 'Over-Delivered', val: qtyOverCount, flag: KPI_FLAG_COLORS.critical, tip: 'More was received than the PO ordered, summed across every delivery against each line - zero tolerance, any nonzero difference flags. A subset of Quantity Mismatches.' },
    { key: 'qtyunder', cls: 'critical', label: 'Short-Delivered', val: qtyUnderCount, flag: KPI_FLAG_COLORS.critical, tip: 'Less was received than the PO ordered - zero tolerance. On an order still open this is a part-delivery; on a closed one it is a short shipment. Check the Progress column. A subset of Quantity Mismatches.' },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Mismatches', val: rateDiscCount, flag: KPI_FLAG_COLORS.critical, tip: 'Rate mismatch in MIR: rate differs between the PO and its matched MIR entry - zero tolerance. Value is not compared here - see the Data Quality legend for why.' },
    { key: 'overdue', cls: 'overdue', label: 'Overdue', val: overdueCount, flag: KPI_FLAG_COLORS.critical, tip: 'Delivery date has passed and the order is still not fully received - including POs that are partly delivered. This overlaps the other status cards rather than excluding them, so the cards here add up to more than Total PO\u2019s Created.' },
    { key: 'pending', cls: 'pending', label: STATUS_LABELS.pending, val: counts.pending, flag: KPI_FLAG_COLORS.pending, tip: 'Nothing has arrived against this PO yet, and its delivery date has not passed.' },
    { key: 'unknown', cls: 'unknown', label: STATUS_LABELS.unknown, val: noDateCount, flag: KPI_FLAG_COLORS.unknown, tip: 'No delivery date on file for any line item, so this PO can never be called overdue or on order. Counted whether or not the material has arrived - a missing date is worth chasing either way. Overlaps the other cards rather than excluding them.' },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: flagsCount, flag: KPI_FLAG_COLORS.quality, tip: 'Any flagged issue on this PO - quantity/rate mismatch, PO not found in MIR, vendor name, net value, taxable value, final amount or tax type mismatch, UOM mismatch, or a paperwork note from remarks. Use "Filter by Flags" below to narrow to one specific issue.' },
  ];
  const kpiHtml = cardDef.map(c => '<div class="kpi-card ' + c.cls + ' ' + (state.statusFilter === c.key ? 'active' : '') + '" data-kpi="' + c.key + '" tabindex="0" role="button" aria-pressed="' + (state.statusFilter === c.key) + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="val" data-count-target="' + c.val + '" data-count-fmt="int">0</div><div class="label">' + escapeHtml(c.label) + (c.tip ? infoTooltipHtml(c.tip) : '') + '</div></div>').join('');

  // `key` drives both the doughnut's onClick (maps a clicked slice back to
  // the same statusFilter value a KPI-card click would set) and the header
  // Status <select> (see the "View all" table below) - one status value,
  // three ways to set it, never out of sync with each other.
  //
  // Two rings, each a partition of `filtered` (see charts.js's
  // renderTwoRingDoughnut()). Overdue and Date Unknown are overlays across
  // received/partial, so one ring could only show their "nothing received"
  // part - on production the Date Unknown card read 142 while its slice read
  // 7. Split by question instead, every status card has a slice with its own
  // number and its own key: inner = what has arrived, outer = delivery date.
  // The outer ring's groups are disjoint by construction (overdue needs a
  // passed date, Date Unknown has none, On Order is dated and not due); a PO
  // with no line items is both On Order and undated, and is shown undated.
  const nothingReceived = filtered.filter(po => po._status !== 'received' && po._status !== 'partial').length;
  const pendingDated = filtered.filter(po => po._status === 'pending' && !po._noDeliveryDate).length;
  const statusRings = {
    inner: [
      { key: 'received', label: STATUS_LABELS.received, val: counts.received, color: '#16a34a' },
      { key: 'partial', label: STATUS_LABELS.partial, val: counts.partial, color: '#2563eb' },
      { key: 'status:nothing', label: 'Nothing received yet', val: nothingReceived, color: '#f59e0b' },
    ],
    outer: [
      { key: 'overdue', label: STATUS_LABELS.overdue, val: overdueCount, color: '#dc2626' },
      { key: 'pending', label: STATUS_LABELS.pending, val: pendingDated, color: '#d97706' },
      { key: 'unknown', label: STATUS_LABELS.unknown, val: noDateCount, color: '#64748b' },
      { key: null, label: 'Not overdue, some or all received', val: total - overdueCount - pendingDated - noDateCount, color: '#cbd5e1' },
    ],
  };
  const statusChartData = total ? [total] : [];

  // Order value per created month, stacked by the doughnut's inner ring
  // (received / partial / nothing received), so the bars answer the same
  // question as the Material Inwarded and Partial cards in rupees; the
  // tooltip adds how much of the month is overdue. Pre-tax
  // totalValue only: mixing in the tax-inclusive total where present added
  // GST-inclusive and GST-exclusive figures into one bar, and every value
  // comparison in this app is pre-tax.
  const monthStatus = {};
  // POs the chart cannot place are counted and said under it, never dropped
  // silently (HRS's legacy 1074-1082 carry no value at all).
  let unplotted = 0;
  filtered.forEach(po => {
    const m = (po.createdDate || '').slice(0, 7);
    const v = po.totalValue;
    if (!m || v == null) { unplotted++; return; }
    const row = monthStatus[m] || (monthStatus[m] = {});
    const k = (po._status === 'received' || po._status === 'partial') ? po._status : 'nothing';
    row[k] = (row[k] || 0) + v;
    if (po._overdue) row.overdueValue = (row.overdueValue || 0) + v;
  });
  const months = fillMonthRange(Object.keys(monthStatus));
  const trendSeries = [
    { key: 'received', label: STATUS_LABELS.received, color: '#16a34a' },
    { key: 'partial', label: STATUS_LABELS.partial, color: '#2563eb' },
    { key: 'nothing', label: 'Nothing received yet', color: '#f59e0b' },
  ].map(sr => Object.assign(sr, { total: months.reduce((a, m) => a + ((monthStatus[m] || {})[sr.key] || 0), 0) }))
    .filter(sr => sr.total > 0);
  const trendTotal = trendSeries.reduce((a, sr) => a + sr.total, 0);

  // Category/Sub Category/Flags dropdowns, rendered just above the chart row
  // (see el.innerHTML below) - same 3-dropdown pattern as Raw Material
  // Analysis's own Category/Sub Category/Flags bar, and now genuinely the
  // same MEANING too (2026-09-08): Category/Sub Category are the material
  // category/subcategory a PO's line items belong to (see categoryCounts's
  // own comment above) - flag severity/label filtering (what these two used
  // to mean) moved into "Filter by Flags" as "Critical Issues"/"Data
  // Quality Flag" alongside the existing qty/rate mismatch shortcuts. Flags
  // is a coarse KPI-style shortcut sharing state.statusFilter with the KPI
  // cards above (see statusRings' comment - one source of truth,
  // never disagreeing). Only options actually present in the current date
  // range are listed, so a dropdown never shows an option with nothing
  // behind it.
  const categoryOptionsHtml = Object.entries(categoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([cat, n]) => '<option value="' + escapeHtml(cat) + '"' + (state.categoryFilter === cat ? ' selected' : '') + '>' + escapeHtml(categoryLabel(cat)) + ' (' + n + ')</option>')
    .join('');
  const subCategoryOptionsHtml = Object.entries(subCategoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([sub, n]) => '<option value="' + escapeHtml(sub) + '"' + (state.subCategoryFilter === sub ? ' selected' : '') + '>' + escapeHtml(categoryLabel(sub)) + ' (' + n + ')</option>')
    .join('');
  // "Data Quality Flag" already meant "informational severity" (po._hasInfoFlag,
  // see computePoFlags()) before this page had any material-category
  // concept - kept under that same name/value ('flags') rather than adding
  // a second, differently-labeled option for the identical underlying set.
  // "Critical Issues" is new here: the qty-OR-rate combined count that
  // "Filter by Category" used to expose as one of its two severity buckets.
  const flagCategoryOptionsHtml = Object.keys(flagCategoryCounts).sort().map(function (label) {
    const value = 'cat:' + label;
    const selected = state.statusFilter === value ? ' selected' : '';
    return '<option value="' + value + '"' + selected + '>' + label + ' (' + flagCategoryCounts[label] + ')</option>';
  }).join('');
  const flagsOptionsHtml =
    '<option value="qtydisc"' + (state.statusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Mismatch (' + qtyDiscCount + ')</option>' +
    '<option value="qtyover"' + (state.statusFilter === 'qtyover' ? ' selected' : '') + '>&nbsp;&nbsp;Over-Delivered (' + qtyOverCount + ')</option>' +
    '<option value="qtyunder"' + (state.statusFilter === 'qtyunder' ? ' selected' : '') + '>&nbsp;&nbsp;Short-Delivered (' + qtyUnderCount + ')</option>' +
    '<option value="ratedisc"' + (state.statusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch (' + rateDiscCount + ')</option>' +
    '<option value="critical"' + (state.statusFilter === 'critical' ? ' selected' : '') + '>Critical Issues (' + criticalCount + ')</option>' +
    '<option value="flags"' + (state.statusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + flagsCount + ')</option>' +
    flagCategoryOptionsHtml;
  // Everything from the list heading down is rendered by poListRegionHtml()
  // from this context, so a PO Number/Vendor keystroke can rebuild the list
  // alone instead of this whole view - see PO_LIST_CTX's own comment.
  PO_LIST_CTX = { el: el, filtered: filtered };

  el.innerHTML =
    '<div class="filter-row">' +
      '<div class="filter-group">' +
        '<label>Date filter (Created on)</label>' +
        '<input type="date" id="fromDate" value="' + (state.from || '') + '">' +
        '<span class="sep-gray">to</span>' +
        '<input type="date" id="toDate" value="' + (state.to || '') + '">' +
      '</div>' +
      '<button class="primary" id="applyFilter">Apply</button>' +
      '<button id="clearFilter">Clear</button>' +
    '</div>' +
    '<div class="kpi-grid">' + kpiHtml + '</div>' +
    '<span class="legend-toggle" id="legendToggle">What do these flags mean?</span>' +
    renderLegendHtml() +
    // Category / Sub Category / Flags filters, placed right before the
    // charts per the project owner's 2026-09-04 request (extended
    // 2026-09-04 to the full 3-dropdown row, matching Raw Material
    // Analysis's own Category/Sub Category/Flags bar) - "global" filters
    // like the date-range bar above (narrow `filtered`, so they drive the
    // KPI counts and both charts below too, not just the table - see
    // state.categoryFilter's own comment). Only rendered when at least one
    // category is present in the current date range (an empty dropdown row
    // would be dead weight).
    (Object.keys(categoryCounts).length ?
      '<div class="filter-row">' +
        '<div class="filter-group">' +
          '<label>Filter by Category</label>' +
          '<select id="categoryFilterSelect" class="select-w200">' +
            '<option value="">All Categories (' + dateRangeCount + ')</option>' +
            categoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Sub Category</label>' +
          '<select id="subCategoryFilterSelect" class="select-w240">' +
            '<option value="">All Sub Categories (' + subCategoryBaseCount + ')</option>' +
            subCategoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Flags</label>' +
          '<select id="flagsFilterSelect" class="select-w220">' +
            '<option value="">All Flags</option>' +
            flagsOptionsHtml +
          '</select>' +
        '</div>' +
        ((state.categoryFilter || state.subCategoryFilter || ['qtydisc', 'qtyover', 'qtyunder', 'ratedisc', 'critical', 'flags'].includes(state.statusFilter) || (typeof state.statusFilter === 'string' && state.statusFilter.startsWith('cat:'))) ? '<button id="clearCategoryFilter">Clear</button>' : '') +
      '</div>' : '') +
    ((statusChartData.length || months.length) ?
      '<div class="chart-row">' +
        '<div class="chart-panel" id="poTrendPanel">' +
          chartHeadHtml('Order Value by Month', 'Pre-tax value of the POs created each month, split by how much has arrived. Click a month to list its POs.',
            'Total in view', months.length ? formatInr(trendTotal) : null) +
          (months.length ? '<div class="chart-box"><canvas id="poTrendChart"></canvas></div>' +
            chartLegendHtml([{ items: trendSeries.map(sr => ({ key: null, label: sr.label, color: sr.color, valText: formatInr(sr.total) })) }])
            : '<div class="no-data-note">No dated POs in range to plot.</div>') +
          '<div class="chart-foot">' +
            (state.chartMonthFilter ? '<span class="chart-filter-chip">List shows ' + escapeHtml(formatMonthLabel(state.chartMonthFilter)) + ' only <button type="button" id="poMonthClear" aria-label="Show all months">&times;</button></span>' : '') +
            (unplotted ? '<span class="no-data-note">' + unplotted + ' PO' + (unplotted === 1 ? '' : 's') + ' not shown: no created date or no value on file.</span>' : '') +
          '</div>' +
        '</div>' +
        '<div class="chart-panel" id="poStatusPanel">' +
          chartHeadHtml('Status Breakdown', 'Each slice matches its card above. Click a slice or a label to filter the list.') +
          (statusChartData.length ? '<div class="doughnut-layout"><div class="chart-box chart-box-doughnut"><canvas id="poStatusChart"></canvas></div>' + twoRingLegendHtml(statusRings, state.statusFilter) + '</div>'
            : '<div class="no-data-note">No POs in range.</div>') +
        '</div>' +
      '</div>' : '') +
    // Its own container so a PO Number/Vendor keystroke can replace just
    // this (see renderPoListRegion()), leaving the KPI row's count-up and
    // both charts above it untouched.
    '<div id="poListRegion">' + poListRegionHtml() + '</div>';

  applyDynamicStyles(el); // legend dots (categoryColor()) - see shared.js's own comment
  wireKpiCountUps();
  wirePoListRegion();

  document.querySelectorAll('[data-kpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.kpi;
    state.statusFilter = (state.statusFilter === key || key === 'total') ? null : key;
    state.tablePage = 1;
    renderPoList(el);
    // See materials.js's own copy of this - the PO list sits below two chart
    // panels, so a KPI click that narrows it is otherwise invisible from the
    // top of the page. 'total' clears rather than narrows, so it never scrolls.
    if (state.statusFilter) revealFilteredList('poListRegion');
  });
  document.getElementById('legendToggle').onclick = () => { state.legendOpen = !state.legendOpen; renderPoList(el); };
  document.getElementById('applyFilter').onclick = () => {
    state.from = document.getElementById('fromDate').value || null;
    state.to = document.getElementById('toDate').value || null;
    state.tablePage = 1;
    renderPoList(el);
  };
  document.getElementById('clearFilter').onclick = () => { state.from = null; state.to = null; state.tablePage = 1; renderPoList(el); };
  const categorySelect = document.getElementById('categoryFilterSelect');
  if (categorySelect) categorySelect.onchange = () => { state.categoryFilter = categorySelect.value || null; state.subCategoryFilter = null; state.tablePage = 1; renderPoList(el); };
  const subCategorySelect = document.getElementById('subCategoryFilterSelect');
  if (subCategorySelect) subCategorySelect.onchange = () => { state.subCategoryFilter = subCategorySelect.value || null; state.tablePage = 1; renderPoList(el); };
  const flagsSelect = document.getElementById('flagsFilterSelect');
  if (flagsSelect) flagsSelect.onchange = () => { state.statusFilter = flagsSelect.value || null; state.tablePage = 1; renderPoList(el); };
  const clearCategoryBtn = document.getElementById('clearCategoryFilter');
  if (clearCategoryBtn) clearCategoryBtn.onclick = () => {
    state.categoryFilter = null; state.subCategoryFilter = null;
    if (['qtydisc', 'qtyover', 'qtyunder', 'ratedisc', 'critical', 'flags'].includes(state.statusFilter) || (typeof state.statusFilter === 'string' && state.statusFilter.startsWith('cat:'))) state.statusFilter = null;
    state.tablePage = 1; renderPoList(el);
  };

  const pickStatus = key => {
    state.statusFilter = state.statusFilter === key ? null : key;
    state.tablePage = 1;
    renderPoList(el);
  };
  wireChartLegend(document.getElementById('poStatusPanel'), pickStatus);
  applyDynamicStyles(document.getElementById('poTrendPanel') || el);
  const monthClear = document.getElementById('poMonthClear');
  if (monthClear) monthClear.onclick = () => { state.chartMonthFilter = null; state.tablePage = 1; renderPoList(el); };

  destroyPageCharts();
  // Charts are a secondary view on top of the KPIs/table already rendered
  // above - if Chart.js failed to load (e.g. its CDN script is unreachable
  // or CSP-blocked) or the draw call otherwise throws, that must not take
  // down the whole dashboard the way an uncaught error here would (it
  // would propagate to loadDashboard()'s catch and replace the entire,
  // already-correct page with the generic "couldn't load" message - a real
  // failure mode hit in this app on 2026-09-03, see security_headers.py).
  // Each chart fails on its own, visibly, with the rest of the page intact.
  if (months.length) {
    try {
      const ctx = document.getElementById('poTrendChart');
      // Clicking a bar sets state.chartMonthFilter to that month - see the
      // "table-only filter" comment on state.chartMonthFilter. Clicking the
      // already-selected bar again clears it (same toggle pattern as a KPI
      // card). The selected bar is drawn solid navy so it's visually clear
      // which month the list below is currently narrowed to - other bars
      // keep their status colour.
      pageCharts.push(new Chart(ctx, {
        type: 'bar',
        data: {
          labels: months.map(shortMonthLabel),
          datasets: trendSeries.map((sr, i) => ({
            label: sr.label,
            data: months.map(m => (monthStatus[m] || {})[sr.key] || 0),
            backgroundColor: months.map(m => (state.chartMonthFilter && m !== state.chartMonthFilter) ? sr.color + '40' : sr.color),
            borderRadius: i === trendSeries.length - 1 ? { topLeft: 4, topRight: 4 } : 0,
            borderSkipped: false,
            maxBarThickness: 42,
            categoryPercentage: 0.7,
          })),
        },
        options: {
          maintainAspectRatio: false,
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const clickedMonth = months[elements[0].index];
            state.chartMonthFilter = state.chartMonthFilter === clickedMonth ? null : clickedMonth;
            state.tablePage = 1;
            renderPoList(el);
          },
          interaction: { mode: 'index', intersect: false },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            tooltip: {
              filter: c => c.parsed.y > 0,
              callbacks: {
                title: items => (items.length ? formatMonthLabel(months[items[0].dataIndex]) : ''),
                label: c => c.dataset.label + ': ' + formatInr(c.parsed.y),
                footer: items => {
                  const od = items.length ? (monthStatus[months[items[0].dataIndex]] || {}).overdueValue : 0;
                  return 'Total: ' + formatInr(items.reduce((a, c) => a + c.parsed.y, 0))
                    + (od ? '\nOf which overdue: ' + formatInr(od) : '') + '\nClick to filter the list below';
                },
              },
            },
          },
          scales: {
            x: { stacked: true, grid: { display: false }, border: { color: CHART_GRID }, ticks: { maxRotation: 0, autoSkipPadding: 8 } },
            y: {
              stacked: true,
              grid: { color: CHART_GRID },
              border: { display: false },
              ticks: { maxTicksLimit: 6, callback: v => formatInr(v) },
            },
          },
        },
      }));
    } catch (e) {
      console.error('PO trend chart failed to render:', e);
      const box = document.getElementById('poTrendChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
  if (statusChartData.length) {
    try {
      // A slice click sets the same state.statusFilter a KPI card click
      // does, with the same toggle-off-on-repeat, so the card lights up too.
      pageCharts.push(renderTwoRingDoughnut(document.getElementById('poStatusChart'), {
        outer: statusRings.outer,
        inner: statusRings.inner,
        selectedKey: state.statusFilter,
        centerPlugin: centerTextPlugin,
        onPick: pickStatus,
      }));
    } catch (e) {
      console.error('PO status chart failed to render:', e);
      const box = document.getElementById('poStatusChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
}

// ── The list region (heading + header filters + rows + pagination) ───────
// Split out of renderPoList() on 2026-09-19, same change and same reasoning
// as materials.js's own MAT_LIST_CTX (see that comment for the full
// story): the PO Number/Vendor header searches are table-only filters -
// they narrow `tableRecs`, never `filtered` - so rebuilding the KPI row
// (restarting 10 count-up animations from 0) and destroying/recreating both
// Chart.js canvases on every keystroke was work that could not change
// anything on screen except the list.
//
// PO_LIST_CTX holds what the last full render computed and the text search
// cannot affect: the date/category-narrowed `filtered` array, the header
// filter cells, and the container to render back into.
let PO_LIST_CTX = null;

function poListRegionHtml() {
  const ctx = PO_LIST_CTX;
  const filtered = ctx.filtered;
  let tableRecs = filtered;
  if (state.statusFilter === 'overdue') tableRecs = filtered.filter(po => po._overdue);
  else if (state.statusFilter === 'unknown') tableRecs = filtered.filter(po => po._noDeliveryDate);
  else if (state.statusFilter === 'qtydisc') tableRecs = filtered.filter(po => po._qtyFlag);
  else if (state.statusFilter === 'qtyover') tableRecs = filtered.filter(po => po._qtyOverFlag);
  else if (state.statusFilter === 'qtyunder') tableRecs = filtered.filter(po => po._qtyUnderFlag);
  else if (state.statusFilter === 'ratedisc') tableRecs = filtered.filter(po => po._rateFlag);
  else if (state.statusFilter === 'critical') tableRecs = filtered.filter(poHasCriticalCategory);
  else if (state.statusFilter === 'flags') tableRecs = filtered.filter(po => po._categories.length > 0);
  // "Filter by Flags" per-category options (added 2026-09-08) are namespaced
  // 'cat:<label>' rather than the bare label, so they can never collide with
  // a real STATUS_LABELS key (received/partial/pending/overdue/unknown) in
  // the catch-all branch below.
  else if (typeof state.statusFilter === 'string' && state.statusFilter.startsWith('cat:')) {
    const wantedLabel = state.statusFilter.slice(4);
    tableRecs = filtered.filter(po => po._categories.some(c => c.label === wantedLabel));
  }
  // The doughnut's "Nothing received yet" slice - see statusRings.
  else if (state.statusFilter === 'status:nothing') tableRecs = filtered.filter(po => po._status !== 'received' && po._status !== 'partial');
  // A raw po._status value, e.g. from an old link or the header select.
  else if (typeof state.statusFilter === 'string' && state.statusFilter.startsWith('status:')) {
    const wantedStatus = state.statusFilter.slice(7);
    tableRecs = filtered.filter(po => po._status === wantedStatus);
  }
  else if (state.statusFilter && state.statusFilter !== 'total') tableRecs = filtered.filter(po => po._status === state.statusFilter);
  // Table-only filters (never touch the KPI counts/charts above, which stay
  // scoped to `filtered` = the from/to date range + category filter only) -
  // clicking a bar in the trend chart narrows the list to that month; the
  // "View all" table's header filters (poNumber/vendor/delivery/value/
  // progress) narrow further. See applyColFilters() and the chart onClick
  // handlers below.
  if (state.chartMonthFilter) tableRecs = tableRecs.filter(po => (po.createdDate || '').slice(0, 7) === state.chartMonthFilter);
  tableRecs = applyColFilters(tableRecs);
  tableRecs = tableRecs.slice().sort((a, b) => (b.createdDate || '').localeCompare(a.createdDate || ''));

  const showingAll = state.showAllPOs;
  const totalForList = tableRecs.length;
  // "View all" is paginated 10/page instead of dumping every matching row
  // at once (project owner, 2026-09-04) - the compact top-5 view is
  // unaffected, it's always just the first 5 of tableRecs, a preview, not
  // a paginated browse. state.tablePage is clamped here (not just where
  // it's set) so a filter change that shrinks the result set below the
  // previously-viewed page can never stick on a blank page.
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(totalForList / PAGE_SIZE));
  const tablePage = Math.min(Math.max(1, state.tablePage), totalPages);
  const listRecs = showingAll ? tableRecs.slice((tablePage - 1) * PAGE_SIZE, tablePage * PAGE_SIZE) : tableRecs.slice(0, 5);
  // Read back by wirePoListRegion(), which has to clamp the same way rather
  // than re-deriving a second page count that could disagree.
  ctx.totalPages = totalPages;

  // Per-column header filter content - "as per their data": text (contains)
  // for PO Number/Vendor, date-range for Created On (bound directly to the
  // same state.from/state.to the top filter-row uses, not a duplicate
  // field) and Delivery Date, and a <select> each for Status (bound to
  // state.statusFilter - see statusRings' comment) and Progress.
  // No Value column filter here - min/max value narrowing was removed
  // (project owner, 2026-09-04) to keep this header row to "as per their
  // data" text/date/select controls only. Details has no data of its own
  // (just a link), so its cell is empty. One shared array of inner-cell HTML
  // so both the "View all" table's <thead> AND the compact top-5 grid's
  // header row render the identical controls - filtering works from either
  // view, not just the expanded table.
  //
  // Built HERE, not handed over in PO_LIST_CTX: each cell carries its
  // filter's CURRENT value, so a snapshot taken at full-render time would
  // rewrite the PO Number box back to what it held before the keystroke that
  // triggered this render - the list would narrow correctly while the box
  // the reader is typing into went blank under the cursor. (Found exactly
  // that way while verifying this split.)
  // Plus the doughnut's "Nothing received yet" slice (see statusRings), so
  // the select still shows what is selected after a slice click.
  const statusOptionsHtml = Object.keys(STATUS_LABELS).concat(['status:nothing']).map(k =>
    '<option value="' + k + '"' + (state.statusFilter === k ? ' selected' : '') + '>' +
      escapeHtml(k === 'status:nothing' ? 'Nothing received yet' : STATUS_LABELS[k]) + '</option>'
  ).join('');
  const filterCells = [
    '<input type="text" class="col-filter-input" data-cf="poNumber" placeholder="Search..." value="' + escapeHtml(state.colFilters.poNumber) + '">',
    '<input type="text" class="col-filter-input" data-cf="vendor" placeholder="Search..." value="' + escapeHtml(state.colFilters.vendor) + '">',
    '<div class="col-filter-range"><input type="date" data-cf="createdFrom" value="' + (state.from || '') + '"><input type="date" data-cf="createdTo" value="' + (state.to || '') + '"></div>',
    '<div class="col-filter-range"><input type="date" data-cf="deliveryFrom" value="' + (state.colFilters.deliveryFrom || '') + '"><input type="date" data-cf="deliveryTo" value="' + (state.colFilters.deliveryTo || '') + '"></div>',
    '', // Value (incl. tax) - no header filter (min/max removed); keeps this array 1:1 with the 8 table columns
    '<select class="col-filter-input" data-cf="status"><option value="">All</option>' + statusOptionsHtml + '</select>',
    '<select class="col-filter-input" data-cf="progress"><option value="">All</option><option value="inwarded"' + (state.colFilters.progress === 'inwarded' ? ' selected' : '') + '>Inwarded</option><option value="not"' + (state.colFilters.progress === 'not' ? ' selected' : '') + '>Not Inwarded</option></select>',
    '',
  ];

  // Drives the "N filter(s) active - Clear" chip next to the list heading -
  // every table-only filter that could be narrowing the list below what the
  // KPI cards/charts show (which stay scoped to `filtered`, the date-range
  // bar only). The date range counts once here even though it's exposed in
  // two places (the bar above and the "Created On" header filter below -
  // same state.from/state.to, not two separate fields).
  const cf = state.colFilters;
  const activeFilterCount =
    (state.chartMonthFilter ? 1 : 0) +
    (state.statusFilter && state.statusFilter !== 'total' ? 1 : 0) +
    (state.from || state.to ? 1 : 0) +
    (state.categoryFilter ? 1 : 0) +
    (state.subCategoryFilter ? 1 : 0) +
    (cf.poNumber ? 1 : 0) +
    (cf.vendor ? 1 : 0) +
    (cf.deliveryFrom || cf.deliveryTo ? 1 : 0) +
    (cf.progress ? 1 : 0);

  return '<div class="list-toggle-row"><div class="section-title m-0">Purchase Orders (Latest first)</div>' +
      (listRecs.some(po => po._qtyFlag || po._rateFlag) ? rowTintLegendHtml() : '') +
      '<div class="flex-row-gap10">' +
        (activeFilterCount ? '<span class="clear-list-filters" id="clearListFilters">' + activeFilterCount + ' filter' + (activeFilterCount > 1 ? 's' : '') + ' active &middot; Clear &times;</span>' : '') +
        (totalForList > 5 ? '<button class="view-all-btn" id="toggleAllBtn">' + (showingAll ? 'Show top 5' : 'View all') + '</button>' : '') +
      '</div>' +
    '</div>' +
    (() => {
      // Four buckets, one icon each - see flags.js's rowFlagsHtml() for what
      // they are and what this replaced (one identical red icon per critical
      // category). 'pending' is the status computeStatus() gives an order
      // with a delivery date still ahead of it; 'overdue' deliberately gets
      // no delivery-state flag, since the pill beside it is already red.
      const rowFlags = po => rowFlagsHtml({
        partial: po._status === 'partial',
        onOrder: po._status === 'pending',
        categories: po._categories,
      });
      if (showingAll) {
        const colFilterRow = '<tr class="col-filter-row">' + filterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';
        // 10 rows/page instead of dumping the whole filtered result set at
        // once (project owner, 2026-09-04) - direct page-number buttons up
        // to 10 pages (comfortably covers real data sizes today); beyond
        // that, falls back to a plain "Page X of Y" indicator rather than
        // rendering 11+ buttons in a row.
        const pageButtons = totalPages <= 10
          ? Array.from({ length: totalPages }, (_, i) => i + 1)
              .map(p => '<button class="page-btn page-num' + (p === tablePage ? ' active' : '') + '" data-page="' + p + '">' + p + '</button>')
              .join('')
          : '<span class="page-info">Page ' + tablePage + ' of ' + totalPages + '</span>';
        const paginationHtml = totalPages > 1
          ? '<div class="pagination-row">' +
              '<button id="prevPageBtn" class="page-btn"' + (tablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
              pageButtons +
              '<button id="nextPageBtn" class="page-btn"' + (tablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
              jumpToPageHtml('po', totalPages) +
            '</div>'
          : '';
        return '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Created On</th><th>Delivery Date</th><th>Value (incl. tax)</th><th>Status</th><th>Progress</th><th>Details</th></tr>' + colFilterRow + '</thead>' +
          '<tbody>' + listRecs.map(po => {
            const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
            return '<tr class="' + rowTintClass(po).trim() + '"><td><b>' + escapeHtml(po.poNumber) + '</b></td>' +
              '<td>' + escapeHtml(po.vendorName || '-') + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po.createdDate)) + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po._deliveryDate)) + '</td>' +
              '<td>' + poValueCellHtml(po) + '</td>' +
              '<td><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</td>' +
              '<td>' + miniStepperHtml(po) + '</td>' +
              '<td><span class="row-link" data-po="' + key + '">View details</span></td></tr>';
          }).join('') + '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row grid-cols"><div>PO Number</div><div>Vendor</div><div>Created On</div><div>Delivery Date</div><div>Value (incl. tax)</div><div>Status</div><div>Progress</div><div>Details</div></div>' +
        '<div class="list-header-row grid-cols col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="top5List">' + listRecs.map(po => {
          const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
          return '<div class="top5-row' + rowTintClass(po) + '">' +
            '<div><span class="po-num">' + escapeHtml(po.poNumber) + '</span></div>' +
            '<div>' + escapeHtml(po.vendorName || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po.createdDate) || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po._deliveryDate) || 'Not available') + '</div>' +
            '<div>' + poValueCellHtml(po) + '</div>' +
            '<div><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</div>' +
            '<div>' + miniStepperHtml(po) + '</div>' +
            '<div><span class="row-link" data-po="' + key + '">View details</span></div></div>';
        }).join('') + '</div>';
    })() +
    importCrossHitsHtml();
}

// ── Import orders found by a Domestic search ──────────────────────────────
// Project owner, 2026-09-25: searching the Domestic list by PO number or
// vendor for an order that is in fact an import should still find it, and
// clicking it should go to Import Purchases. The Domestic rows are left
// exactly as they are; the import hits are listed under them, by the same
// two header filters (contains, case-insensitive - applyColFilters()'s
// rule), for the plants currently selected. The import cache is fetched on
// the first such search and the region re-renders once it lands.
let IMPORT_CROSS_LOAD = null;

function importCrossHits() {
  const po = (state.colFilters.poNumber || '').toLowerCase();
  const vendor = (state.colFilters.vendor || '').toLowerCase();
  if (!po && !vendor) return [];
  if (!IMPORT_PO_CACHE) {
    if (!IMPORT_CROSS_LOAD) {
      IMPORT_CROSS_LOAD = ensureImportPOsLoaded()
        .then(() => { if (state.view === 'po' && state.purchaseType === 'domestic') renderPoListRegion(); })
        // No hint is the whole cost of a failure here; the Import tab
        // reports its own load errors when visited.
        .catch(e => console.error('Import cross-search: failed to load import purchase orders:', e))
        .finally(() => { IMPORT_CROSS_LOAD = null; });
    }
    return [];
  }
  const keys = selectedPlantKeys();
  return IMPORT_PO_CACHE.filter(p =>
    keys.indexOf(p.plant) !== -1 &&
    (!po || (p.poNumber || '').toLowerCase().includes(po)) &&
    (!vendor || (p.vendorName || '').toLowerCase().includes(vendor)));
}

function importCrossHitsHtml() {
  const hits = importCrossHits();
  if (!hits.length) return '';
  const shown = hits.slice(0, 10);
  return '<div class="cross-kind-hits">' +
    '<div class="cross-kind-title">Also in Import Purchases (' + hits.length + ')</div>' +
    shown.map(p =>
      '<div class="cross-kind-row">' +
        '<span class="row-link" tabindex="0" role="button" data-import-po="' + escapeHtml(p.plant + '::' + p.poNumber) + '">' + escapeHtml(p.poNumber) + '</span>' +
        '<span>' + escapeHtml(p.vendorName || '-') + '</span>' +
        '<span class="text-muted">' + escapeHtml(PLANTS[p.plant] ? PLANTS[p.plant].label : p.plant) + ' &middot; ' + escapeHtml(formatDateIN(p.createdDate) || '-') + '</span>' +
      '</div>'
    ).join('') +
    (hits.length > shown.length ? '<div class="text-muted fs-12-5">and ' + (hits.length - shown.length) + ' more - open Import Purchases to see them all.</div>' : '') +
  '</div>';
}

/** Moves to Import Purchases filtered to that order, and opens it. */
async function openImportPoFromDomestic(compositeKey) {
  const poNumber = compositeKey.slice(compositeKey.indexOf('::') + 2);
  if (await switchPurchaseType('import', { importPoNumber: poNumber })) await openImportPoModal(compositeKey);
}

/** Re-renders the list region alone, in place. Falls back to a full
 * renderPoList() if the region (or the context it needs) isn't there - e.g.
 * called before the first full render, or after something else replaced
 * #viewContent. */
function renderPoListRegion() {
  const region = document.getElementById('poListRegion');
  if (!region || !PO_LIST_CTX) { renderPoList(document.getElementById('content')); return; }
  preserveFocus(region, () => { region.innerHTML = poListRegionHtml(); });
  applyDynamicStyles(region); // the row-tint legend's dots - see shared.js's own comment
  wirePoListRegion();
}

function wirePoListRegion() {
  const region = document.getElementById('poListRegion');
  if (!region) return;
  const el = PO_LIST_CTX ? PO_LIST_CTX.el : region.parentNode;
  const totalPages = PO_LIST_CTX ? PO_LIST_CTX.totalPages : 1;

  const toggleBtn = document.getElementById('toggleAllBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllPOs = !state.showAllPOs; state.tablePage = 1; renderPoListRegion(); };
  region.querySelectorAll('[data-po]').forEach(el2 => el2.onclick = () => openPoModal(el2.dataset.po));
  region.querySelectorAll('[data-import-po]').forEach(el2 => el2.onclick = () => openImportPoFromDomestic(el2.dataset.importPo));

  const prevPageBtn = document.getElementById('prevPageBtn');
  if (prevPageBtn) prevPageBtn.onclick = () => { state.tablePage = Math.max(1, state.tablePage - 1); renderPoListRegion(); };
  const nextPageBtn = document.getElementById('nextPageBtn');
  if (nextPageBtn) nextPageBtn.onclick = () => { state.tablePage = state.tablePage + 1; renderPoListRegion(); }; // clamped to totalPages on next render
  region.querySelectorAll('.page-num').forEach(btn => btn.onclick = () => { state.tablePage = Number(btn.dataset.page); renderPoListRegion(); });
  wireJumpToPage('po', totalPages, (n) => { state.tablePage = n; renderPoListRegion(); });

  // Clears the "global" date/category/status filters too, so this one has
  // to go through the full render.
  const clearListFiltersBtn = document.getElementById('clearListFilters');
  if (clearListFiltersBtn) clearListFiltersBtn.onclick = () => {
    state.statusFilter = null; state.chartMonthFilter = null; state.from = null; state.to = null;
    state.categoryFilter = null; state.subCategoryFilter = null; state.tablePage = 1;
    state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, progress: '' };
    renderPoList(el);
  };

  // Header filter row (only present when showingAll - see the colFilterRow
  // markup above).
  //
  // PO Number / Vendor / Delivery Date / Progress are table-only filters
  // (see applyColFilters()), so they re-render THIS REGION ONLY - the KPI
  // row and both charts above are built from `filtered`, which they cannot
  // narrow. Created On and Status do write into the global fields those are
  // built from, so they take the full render. The text boxes are debounced
  // (shared.js's debounceRender()) so a fast typist gets one pass per pause
  // rather than one per character; date/select controls fire on 'change',
  // which is already one committed event.
  region.querySelectorAll('[data-cf]').forEach(inp => {
    const key = inp.dataset.cf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'date' || inp.type === 'number') ? 'change' : 'input';
    const isGlobal = key === 'createdFrom' || key === 'createdTo' || key === 'status';
    const rerender = isGlobal ? () => renderPoList(el) : debounceRender(() => renderPoListRegion());
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'createdFrom') state.from = raw || null;
      else if (key === 'createdTo') state.to = raw || null;
      else if (key === 'status') state.statusFilter = raw || null;
      else if (key === 'deliveryFrom' || key === 'deliveryTo') state.colFilters[key] = raw || null;
      else state.colFilters[key] = raw;
      state.tablePage = 1;
      rerender();
    });
  });
}
