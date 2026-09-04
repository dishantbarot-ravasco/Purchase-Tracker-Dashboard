// ── PO list rendering (Domestic Purchases) ──────────────────────────────
/** Renders the whole Purchase Orders view for the current plant/filter
 * selection: KPI row, chart row, and either the top-5 preview or the full
 * "View all" table, wiring every click/filter handler to re-render itself.
 * Delegates to renderImportPoList() when purchaseType is 'import' (see that
 * function's own header comment for why it's separate, not a branch here). */
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
    el.innerHTML = '<div class="empty-state">' + msg + '</div>';
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
  // Category here is flag *severity* (critical/info - a PO's closest
  // equivalent of a material's Category field, see state.categoryFilter's
  // own comment); Sub Category cascades to a specific DISCREPANCY_LEGEND
  // label within the selected severity (or across all severities when none
  // is selected). All are "global" filters like from/to above: they narrow
  // `filtered` itself, so the KPI counts and both charts below reflect the
  // selection, not just the table.
  const categoryCounts = {};
  filtered.forEach(po => (po._categories || []).forEach(c => { categoryCounts[c.severity] = (categoryCounts[c.severity] || 0) + 1; }));
  const dateRangeCount = filtered.length; // "All Categories (N)" option's count - before category narrowing
  const inSelectedSeverity = state.categoryFilter
    ? filtered.filter(po => (po._categories || []).some(c => c.severity === state.categoryFilter))
    : filtered;
  const subCategoryCounts = {};
  inSelectedSeverity.forEach(po => (po._categories || []).forEach(c => {
    if (state.categoryFilter && c.severity !== state.categoryFilter) return;
    subCategoryCounts[c.label] = (subCategoryCounts[c.label] || 0) + 1;
  }));
  const subCategoryBaseCount = inSelectedSeverity.length; // "All Sub Categories (N)" option's count
  if (state.categoryFilter) filtered = filtered.filter(po => (po._categories || []).some(c => c.severity === state.categoryFilter));
  if (state.subCategoryFilter) filtered = filtered.filter(po => (po._categories || []).some(c => c.label === state.subCategoryFilter));

  const counts = { received: 0, partial: 0, pending: 0, overdue: 0, unknown: 0 };
  filtered.forEach(po => counts[po._status]++);
  const total = filtered.length;
  const qtyDiscCount = filtered.filter(po => po._qtyFlag).length;
  const rateDiscCount = filtered.filter(po => po._rateFlag).length;
  const flagsCount = filtered.filter(po => po._hasInfoFlag).length;

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
    { key: 'total', cls: '', label: "Total PO's Created", val: total },
    { key: 'received', cls: 'received', label: 'Material Inwarded', val: counts.received, flag: KPI_FLAG_COLORS.received },
    { key: 'partial', cls: 'partial', label: STATUS_LABELS.partial, val: counts.partial, flag: KPI_FLAG_COLORS.partial },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Discrepancies', val: qtyDiscCount, flag: KPI_FLAG_COLORS.critical },
    { key: 'ratedisc', cls: 'critical', label: 'Rate / Value Discrepancies', val: rateDiscCount, flag: KPI_FLAG_COLORS.critical },
    { key: 'overdue', cls: 'overdue', label: 'Overdue', val: counts.overdue, flag: KPI_FLAG_COLORS.critical },
    { key: 'pending', cls: 'pending', label: STATUS_LABELS.pending, val: counts.pending, flag: KPI_FLAG_COLORS.pending },
    { key: 'unknown', cls: 'unknown', label: STATUS_LABELS.unknown, val: counts.unknown, flag: KPI_FLAG_COLORS.unknown },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', val: flagsCount, flag: KPI_FLAG_COLORS.quality },
  ];
  const kpiHtml = cardDef.map(c => '<div class="kpi-card ' + c.cls + ' ' + (state.statusFilter === c.key ? 'active' : '') + '" data-kpi="' + c.key + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="val">' + c.val + '</div><div class="label">' + escapeHtml(c.label) + '</div></div>').join('');

  let tableRecs = filtered;
  if (state.statusFilter === 'qtydisc') tableRecs = filtered.filter(po => po._qtyFlag);
  else if (state.statusFilter === 'ratedisc') tableRecs = filtered.filter(po => po._rateFlag);
  else if (state.statusFilter === 'flags') tableRecs = filtered.filter(po => po._hasInfoFlag);
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

  // `key` drives both the doughnut's onClick (maps a clicked slice back to
  // the same statusFilter value a KPI-card click would set) and the header
  // Status <select> (see the "View all" table below) - one status value,
  // three ways to set it, never out of sync with each other.
  const statusChartData = [
    { key: 'received', label: STATUS_LABELS.received, val: counts.received, color: '#16a34a' },
    { key: 'partial', label: STATUS_LABELS.partial, val: counts.partial, color: '#2563eb' },
    { key: 'pending', label: STATUS_LABELS.pending, val: counts.pending, color: '#d97706' },
    { key: 'overdue', label: STATUS_LABELS.overdue, val: counts.overdue, color: '#dc2626' },
    { key: 'unknown', label: STATUS_LABELS.unknown, val: counts.unknown, color: '#64748b' },
  ].filter(s => s.val > 0);

  const monthTotals = {};
  filtered.forEach(po => {
    const m = (po.createdDate || '').slice(0, 7);
    const v = po.totalInclTax != null ? po.totalInclTax : po.totalValue;
    if (!m || v == null) return;
    monthTotals[m] = (monthTotals[m] || 0) + v;
  });
  const months = Object.keys(monthTotals).sort();

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
  const statusOptionsHtml = Object.keys(STATUS_LABELS).map(k =>
    '<option value="' + k + '"' + (state.statusFilter === k ? ' selected' : '') + '>' + escapeHtml(STATUS_LABELS[k]) + '</option>'
  ).join('');
  // Category/Sub Category/Flags dropdowns, rendered just above the chart row
  // (see el.innerHTML below) - same 3-dropdown pattern as Raw Material
  // Analysis's own Category/Sub Category/Flags bar. Category = severity
  // ('Critical Issues'/'Informational Issues'); Sub Category cascades to the
  // specific DISCREPANCY_LEGEND label within the selected severity; Flags is
  // a coarse KPI-style shortcut sharing state.statusFilter with the KPI
  // cards above (see statusChartData's `key` comment - one source of truth,
  // never disagreeing), same qtydisc/ratedisc/flags options the KPI row
  // already computes. Only options actually present in the current date
  // range are listed, so a dropdown never shows an option with nothing
  // behind it.
  const SEVERITY_LABELS = { critical: 'Critical Issues', info: 'Informational Issues' };
  const categoryOptionsHtml = Object.entries(categoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([sev, n]) => '<option value="' + sev + '"' + (state.categoryFilter === sev ? ' selected' : '') + '>' + escapeHtml(SEVERITY_LABELS[sev] || sev) + ' (' + n + ')</option>')
    .join('');
  const subCategoryOptionsHtml = Object.entries(subCategoryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => '<option value="' + escapeHtml(label) + '"' + (state.subCategoryFilter === label ? ' selected' : '') + '>' + escapeHtml(label) + ' (' + n + ')</option>')
    .join('');
  const flagsOptionsHtml =
    '<option value="qtydisc"' + (state.statusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Discrepancy (' + qtyDiscCount + ')</option>' +
    '<option value="ratedisc"' + (state.statusFilter === 'ratedisc' ? ' selected' : '') + '>Rate / Value Discrepancy (' + rateDiscCount + ')</option>' +
    '<option value="flags"' + (state.statusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + flagsCount + ')</option>';
  // Per-column header filter content - "as per their data": text (contains)
  // for PO Number/Vendor, date-range for Created On (bound directly to the
  // same state.from/state.to the top filter-row uses, not a duplicate
  // field) and Delivery Date, and a <select> each for Status (bound to
  // state.statusFilter - see statusChartData's `key` comment) and Progress.
  // No Value column filter here - min/max value narrowing was removed
  // (project owner, 2026-09-04) to keep this header row to "as per their
  // data" text/date/select controls only. Details has no data of its own
  // (just a link), so its cell is empty. One shared array of inner-cell HTML
  // so both the "View all" table's <thead> AND the compact top-5 grid's
  // header row render the identical controls - filtering works from either
  // view, not just the expanded table.
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

  el.innerHTML =
    '<div class="filter-row">' +
      '<div class="filter-group">' +
        '<label>Date filter (Created on)</label>' +
        '<input type="date" id="fromDate" value="' + (state.from || '') + '">' +
        '<span style="color:#9ca3af;font-size:12px;">to</span>' +
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
          '<select id="categoryFilterSelect" style="min-width:200px;">' +
            '<option value="">All Categories (' + dateRangeCount + ')</option>' +
            categoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Sub Category</label>' +
          '<select id="subCategoryFilterSelect" style="min-width:240px;">' +
            '<option value="">All Sub Categories (' + subCategoryBaseCount + ')</option>' +
            subCategoryOptionsHtml +
          '</select>' +
        '</div>' +
        '<div class="filter-group">' +
          '<label>Filter by Flags</label>' +
          '<select id="flagsFilterSelect" style="min-width:220px;">' +
            '<option value="">All Flags</option>' +
            flagsOptionsHtml +
          '</select>' +
        '</div>' +
        ((state.categoryFilter || state.subCategoryFilter || ['qtydisc', 'ratedisc', 'flags'].includes(state.statusFilter)) ? '<button id="clearCategoryFilter">Clear</button>' : '') +
      '</div>' : '') +
    ((statusChartData.length || months.length) ?
      '<div class="chart-row">' +
        '<div class="chart-panel"><h4>PO Value Trend by Month Created</h4>' +
          (months.length ? '<div class="chart-box"><canvas id="poTrendChart"></canvas></div>' : '<div class="no-data-note">No dated POs in range to plot.</div>') +
        '</div>' +
        '<div class="chart-panel"><h4>Status Breakdown</h4>' +
          (statusChartData.length ? '<div class="chart-box"><canvas id="poStatusChart"></canvas></div>' : '<div class="no-data-note">No POs in range.</div>') +
        '</div>' +
      '</div>' : '') +
    '<div class="list-toggle-row"><div class="section-title" style="margin:0;">Purchase Orders (Latest first)</div>' +
      '<div style="display:flex;align-items:center;gap:10px;">' +
        (activeFilterCount ? '<span class="clear-list-filters" id="clearListFilters">' + activeFilterCount + ' filter' + (activeFilterCount > 1 ? 's' : '') + ' active &middot; Clear &times;</span>' : '') +
        (totalForList > 5 ? '<button class="view-all-btn" id="toggleAllBtn">' + (showingAll ? 'Show top 5' : 'View all') + '</button>' : '') +
      '</div>' +
    '</div>' +
    (() => {
      // One colored flag icon per category on this PO (not a "2 critical /
      // 1 flag" text count) - see categoryColor()/CATEGORY_COLORS for the
      // color-per-category scheme and DISCREPANCY_LEGEND for what each one
      // means.
      const rowFlags = po => {
        const cats = po._categories || [];
        return cats.map(c =>
          // data-tooltip + CSS (.row-flag-wrap::after, see style.css) instead
          // of a native title attribute - title tooltips have a ~1s hover
          // delay and are easy to dismiss with the slightest mouse movement,
          // which read as "hovering isn't working" per the project owner's
          // 2026-09-04 report. The CSS tooltip shows immediately and
          // reliably instead.
          ' <span class="row-flag-wrap" data-tooltip="' + escapeHtml(c.label) + '">' + flagIconHtml(categoryColor(c.label), 'row-flag-icon') + '</span>'
        ).join('');
      };
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
            '</div>'
          : '';
        return '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Vendor</th><th>Created On</th><th>Delivery Date</th><th>Value (incl. tax)</th><th>Status</th><th>Progress</th><th>Details</th></tr>' + colFilterRow + '</thead>' +
          '<tbody>' + listRecs.map(po => {
            const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
            return '<tr><td><b>' + escapeHtml(po.poNumber) + '</b></td>' +
              '<td>' + escapeHtml(po.vendorName || '-') + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po.createdDate)) + '</td>' +
              '<td>' + escapeHtml(formatDateIN(po._deliveryDate)) + '</td>' +
              '<td>' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : '-') + '</td>' +
              '<td><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</td>' +
              '<td>' + miniStepperHtml(po) + '</td>' +
              '<td><span class="row-link" data-po="' + key + '">View details</span></td></tr>';
          }).join('') + '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row grid-cols"><div>PO Number</div><div>Vendor</div><div>Created On</div><div>Delivery Date</div><div>Value (incl. tax)</div><div>Status</div><div>Progress</div><div>Details</div></div>' +
        '<div class="list-header-row grid-cols col-filter-row-grid">' + filterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="top5List">' + listRecs.map(po => {
          const key = escapeHtml(plantKeyFor(po) + '::' + po.poNumber);
          return '<div class="top5-row">' +
            '<div><span class="po-num">' + escapeHtml(po.poNumber) + '</span></div>' +
            '<div>' + escapeHtml(po.vendorName || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po.createdDate) || 'Not available') + '</div>' +
            '<div>' + escapeHtml(formatDateIN(po._deliveryDate) || 'Not available') + '</div>' +
            '<div>' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : '-') + '</div>' +
            '<div><span class="status-pill status-' + po._status + '">' + escapeHtml(STATUS_LABELS[po._status]) + '</span>' + rowFlags(po) + '</div>' +
            '<div>' + miniStepperHtml(po) + '</div>' +
            '<div><span class="row-link" data-po="' + key + '">View details</span></div></div>';
        }).join('') + '</div>';
    })();

  document.querySelectorAll('[data-kpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.kpi;
    state.statusFilter = (state.statusFilter === key || key === 'total') ? null : key;
    state.tablePage = 1;
    renderPoList(el);
  });
  document.getElementById('legendToggle').onclick = () => { state.legendOpen = !state.legendOpen; renderPoList(el); };
  document.getElementById('applyFilter').onclick = () => {
    state.from = document.getElementById('fromDate').value || null;
    state.to = document.getElementById('toDate').value || null;
    state.tablePage = 1;
    renderPoList(el);
  };
  document.getElementById('clearFilter').onclick = () => { state.from = null; state.to = null; state.tablePage = 1; renderPoList(el); };
  const toggleBtn = document.getElementById('toggleAllBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllPOs = !state.showAllPOs; state.tablePage = 1; renderPoList(el); };
  document.querySelectorAll('[data-po]').forEach(el2 => el2.onclick = () => openPoModal(el2.dataset.po));

  const prevPageBtn = document.getElementById('prevPageBtn');
  if (prevPageBtn) prevPageBtn.onclick = () => { state.tablePage = Math.max(1, state.tablePage - 1); renderPoList(el); };
  const nextPageBtn = document.getElementById('nextPageBtn');
  if (nextPageBtn) nextPageBtn.onclick = () => { state.tablePage = state.tablePage + 1; renderPoList(el); }; // clamped to totalPages on next render
  document.querySelectorAll('.page-num').forEach(btn => btn.onclick = () => { state.tablePage = Number(btn.dataset.page); renderPoList(el); });

  const categorySelect = document.getElementById('categoryFilterSelect');
  if (categorySelect) categorySelect.onchange = () => { state.categoryFilter = categorySelect.value || null; state.subCategoryFilter = null; state.tablePage = 1; renderPoList(el); };
  const subCategorySelect = document.getElementById('subCategoryFilterSelect');
  if (subCategorySelect) subCategorySelect.onchange = () => { state.subCategoryFilter = subCategorySelect.value || null; state.tablePage = 1; renderPoList(el); };
  const flagsSelect = document.getElementById('flagsFilterSelect');
  if (flagsSelect) flagsSelect.onchange = () => { state.statusFilter = flagsSelect.value || null; state.tablePage = 1; renderPoList(el); };
  const clearCategoryBtn = document.getElementById('clearCategoryFilter');
  if (clearCategoryBtn) clearCategoryBtn.onclick = () => {
    state.categoryFilter = null; state.subCategoryFilter = null;
    if (['qtydisc', 'ratedisc', 'flags'].includes(state.statusFilter)) state.statusFilter = null;
    state.tablePage = 1; renderPoList(el);
  };

  const clearListFiltersBtn = document.getElementById('clearListFilters');
  if (clearListFiltersBtn) clearListFiltersBtn.onclick = () => {
    state.statusFilter = null; state.chartMonthFilter = null; state.from = null; state.to = null;
    state.categoryFilter = null; state.subCategoryFilter = null; state.tablePage = 1;
    state.colFilters = { poNumber: '', vendor: '', deliveryFrom: null, deliveryTo: null, progress: '' };
    renderPoList(el);
  };
  // Header filter row (only present when showingAll - see the colFilterRow
  // markup above). Text inputs re-render on every keystroke ('input') for a
  // live-filter feel; date/number/select commit on 'change' instead, since
  // re-rendering mid-typing a number or mid-picking a date is jarring and
  // unnecessary. preserveFocus() re-focuses the same input (and restores
  // its cursor position) after the innerHTML rebuild a text-input keystroke
  // triggers - without it, typing a second character would be impossible.
  document.querySelectorAll('[data-cf]').forEach(inp => {
    const key = inp.dataset.cf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'date' || inp.type === 'number') ? 'change' : 'input';
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'createdFrom') state.from = raw || null;
      else if (key === 'createdTo') state.to = raw || null;
      else if (key === 'status') state.statusFilter = raw || null;
      else if (key === 'deliveryFrom' || key === 'deliveryTo') state.colFilters[key] = raw || null;
      else state.colFilters[key] = raw;
      state.tablePage = 1;
      preserveFocus(el, () => renderPoList(el));
    });
  });

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
      const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, 'rgba(37,99,235,0.9)');
      gradient.addColorStop(1, 'rgba(37,99,235,0.25)');
      // Clicking a bar sets state.chartMonthFilter to that month - see the
      // "table-only filter" comment on state.chartMonthFilter. Clicking the
      // already-selected bar again clears it (same toggle pattern as a KPI
      // card). The selected bar is drawn solid navy so it's visually clear
      // which month the list below is currently narrowed to - other bars
      // keep the gradient.
      pageCharts.push(new Chart(ctx, {
        type: 'bar',
        data: {
          labels: months.map(formatMonthLabel),
          datasets: [{
            data: months.map(m => monthTotals[m]),
            backgroundColor: months.map(m => m === state.chartMonthFilter ? '#0f1b2d' : gradient),
            hoverBackgroundColor: '#2563eb',
            borderRadius: 6,
            borderSkipped: false,
            maxBarThickness: 46,
          }],
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
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#0f1b2d',
              padding: 10,
              cornerRadius: 8,
              titleFont: { size: 12, weight: '700' },
              bodyFont: { size: 12 },
              displayColors: false,
              callbacks: {
                label: c => formatInr(c.parsed.y),
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
            y: {
              grid: { color: '#eef1f5' },
              border: { display: false },
              ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) },
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
      const ctx = document.getElementById('poStatusChart');
      // Clicking a slice sets statusFilter to that status - the exact same
      // state field (and toggle-off-on-repeat behavior) a KPI card click
      // already uses, so the corresponding KPI card lights up (.active
      // outline) as the same signal a click on that card would give. The
      // selected slice also pops out persistently (not just on hover) via a
      // per-index `offset`, so the selection is visible even after the
      // mouse moves away.
      pageCharts.push(new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: statusChartData.map(s => s.label),
          datasets: [{
            data: statusChartData.map(s => s.val),
            backgroundColor: statusChartData.map(s => s.color),
            borderWidth: 0,
            spacing: 3,
            borderRadius: 4,
            hoverOffset: 8,
            offset: statusChartData.map(s => state.statusFilter === s.key ? 14 : 0),
          }],
        },
        options: {
          maintainAspectRatio: false,
          cutout: '70%',
          onClick: (evt, elements) => {
            if (!elements.length) return;
            const key = statusChartData[elements[0].index].key;
            state.statusFilter = state.statusFilter === key ? null : key;
            state.tablePage = 1;
            renderPoList(el);
          },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                boxWidth: 9,
                boxHeight: 9,
                padding: 12,
                font: { size: 10.5 },
                generateLabels: chart => {
                  const vals = chart.data.datasets[0].data;
                  const total = vals.reduce((a, b) => a + b, 0);
                  return chart.data.labels.map((label, i) => ({
                    text: label + '  ' + vals[i] + ' (' + (total ? Math.round(vals[i] / total * 100) : 0) + '%)',
                    fillStyle: chart.data.datasets[0].backgroundColor[i],
                    strokeStyle: chart.data.datasets[0].backgroundColor[i],
                    index: i,
                  }));
                },
              },
            },
            tooltip: {
              backgroundColor: '#0f1b2d',
              padding: 10,
              cornerRadius: 8,
              callbacks: {
                label: c => {
                  const total = c.dataset.data.reduce((a, b) => a + b, 0);
                  return ' ' + c.label + ': ' + c.parsed + (total ? ' (' + Math.round(c.parsed / total * 100) + '%)' : '');
                },
                afterLabel: () => 'Click to filter the list below',
              },
            },
          },
        },
        plugins: [centerTextPlugin],
      }));
    } catch (e) {
      console.error('PO status chart failed to render:', e);
      const box = document.getElementById('poStatusChart');
      if (box && box.parentNode) box.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the dashboard is unaffected.</div>';
    }
  }
}

