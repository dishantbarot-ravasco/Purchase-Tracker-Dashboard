// ── Material modal ───────────────────────────────────────────────────────
// compositeKey is "<plantKey>::<lotId>" (see plantKeyFor()) - resolves the
// anchor lot against that specific plant (Stock-lot ids are per-plant
// autoincrement PKs, NOT unique across plants), then rolls up to every
// plant regardless of which plant tab was open when this was clicked - see
// the file header/CLAUDE.md for why this modal alone, unlike the rest of
// this view, always aggregates across all three plants.
async function openMaterialModal(compositeKey) {
  const myModalRequestId = ++modalRequestId;
  const sep = compositeKey.indexOf('::');
  const plantKey = compositeKey.slice(0, sep);
  const lotId = Number(compositeKey.slice(sep + 2));
  const anchor = (MATERIALS_BY_PLANT[plantKey] || []).find(m => m.lotId === lotId);
  if (!anchor) return;

  // Reopening on a different lot without closing first (e.g. clicking
  // another drill-down chart bar) would otherwise leak the previous open's
  // Chart.js instances - clear them before this open creates its own.
  destroyModalCharts();

  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };
  body.innerHTML = '<div class="modal-head"><div></div><span class="close-btn">&times;</span></div><div class="load-banner"><div class="spinner"></div><div>Loading material analysis&hellip;</div></div>';

  try {
    await Promise.all([ensureMaterialsLoaded(PLANT_KEYS), ensurePOsLoaded(PLANT_KEYS)]);
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
  const openLinked = linked.filter(l => l.po._status !== 'received')
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
  const qtyAllPlants = siblingLots.reduce((s, l) => s + (l.qty || 0), 0);
  const valueAllPlants = siblingLots.reduce((s, l) => s + (l.value || 0), 0);
  const openValue = openLinked.reduce((s, l) => s + (l.item.netPrice != null && l.item.qty != null ? l.item.netPrice * l.item.qty : 0), 0);

  const atAGlanceHtml =
    '<div class="line">In stock, all plants: ' + qtyAllPlants + '</div>' +
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
        editableLine(plantKey, 'Category', anchor.category, 'category', lotId, 'text') +
        (plantKey === 'achhad'
          ? plainLine('Sub Category', anchor.subCategory)
          : editableLine(plantKey, 'Sub Category', anchor.subCategory, 'sub_category', lotId, 'text')) +
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
  const stockTableHtml = siblingLots.length
    ? '<div class="table-wrap"><table><thead><tr><th>Plant</th><th>Category</th><th>Qty</th><th>Rate</th><th>Value</th><th>MIR↔Stock Match</th></tr></thead><tbody>' +
        siblingLots.map(l => '<tr><td>' + escapeHtml(l._plantLabel) + '</td>' +
          '<td>' + editableCell(l._plantKey, 'Category (' + l._plantLabel + ')', l.category, 'category', l.lotId, 'text') + '</td>' +
          '<td>' + (l.qty != null ? l.qty : '-') + '</td>' +
          '<td>' + editableCell(l._plantKey, 'Rate (' + l._plantLabel + ')', l.rate, materialRateFieldName(l._plantKey), l.lotId, 'number') + '</td>' +
          '<td>' + (l.value != null ? formatInr(l.value) : '-') + '</td>' +
          '<td>' + mirStockMatchHtml(l, l._plantKey) + '</td></tr>').join('') +
        '<tr class="fw-700"><td>Total</td><td></td><td>' + qtyAllPlants + '</td><td>-</td><td>' + formatInr(valueAllPlants) + '</td><td></td></tr>' +
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
  const materialCritPos = new Map(); // label -> Set of "PO (plant)" strings
  linked.forEach(l => {
    if (l.item.qtyDiffPct != null && l.item.qtyDiffPct > FLAG_PCT) {
      if (!materialCritPos.has('Quantity Mismatch in MIR')) materialCritPos.set('Quantity Mismatch in MIR', new Set());
      materialCritPos.get('Quantity Mismatch in MIR').add(l.po.poNumber + ' (' + l.plantLabel + ')');
    }
    // Rate only, not value - see flags.js's computePoFlags() for why.
    if (l.item.rateDiffPct != null && l.item.rateDiffPct > FLAG_PCT) {
      if (!materialCritPos.has('Rate Mismatch in MIR')) materialCritPos.set('Rate Mismatch in MIR', new Set());
      materialCritPos.get('Rate Mismatch in MIR').add(l.po.poNumber + ' (' + l.plantLabel + ')');
    }
  });
  const materialCritFlagsHtml = Array.from(materialCritPos.entries()).map(([label, poSet]) =>
    '<div class="field-block mb-8">' +
      flagIconHtml(categoryColor(label), 'row-flag-icon') +
      ' <span class="lg-label critical">CRITICAL</span> <b>' + escapeHtml(label) + '</b>' +
      '<div class="mt-4 fs-11-5 text-slate-soft">Affects: ' + escapeHtml(Array.from(poSet).join(', ')) + ' - open that PO\'s own detail view to dismiss.</div>' +
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
  const totalFlagCount = flaggedMatches.length + materialCritPos.size + dataQualityFlagsHtml.length;

  const allMaterialCorrections = [];
  siblingLots.forEach(l => (l.corrections || []).forEach(c => allMaterialCorrections.push(Object.assign({}, c, { _plantLabel: l._plantLabel }))));
  allMaterialCorrections.sort((a, b) => (b.correctedAt || '').localeCompare(a.correctedAt || ''));

  const materialFlagsListHtml = totalFlagCount
    ? materialCritFlagsHtml + flaggedMatches.map(materialFlagHtml).join('') + dataQualityFlagsHtml.join('')
    : '<div class="empty-note-sm">No discrepancy or MIR&harr;Stock match flags for this material.</div>';
  const materialCorrectionsHtml = allMaterialCorrections.length
    ? '<div class="section-title mt-18">Correction History</div>' +
      allMaterialCorrections.map(c =>
        '<div class="field-block mb-8">' +
          '<div class="fs-12-5"><b>' + escapeHtml(c._plantLabel) + ' &middot; ' + escapeHtml(c.fieldName) + ':</b> ' +
          escapeHtml(c.oldValue || 'blank') + ' &rarr; ' + escapeHtml(c.newValue || 'blank') + '</div>' +
          (c.reason ? '<div class="mt-4 fs-12 italic text-slate">"' + escapeHtml(c.reason) + '"</div>' : '') +
          '<div class="mt-4 fs-11 text-slate-soft">' + escapeHtml(c.correctedBy || 'unknown') +
          ' &middot; ' + escapeHtml(formatDateIN(c.correctedAt ? c.correctedAt.slice(0, 10) : null)) + '</div>' +
        '</div>'
      ).join('')
    : '';
  const flagsTabHtml = materialFlagsListHtml + materialCorrectionsHtml +
    overrideBoxHtml('Click the ✎ icon next to Category/Sub Category (Overview tab) or Category/Rate (Stock by Plant tab) to correct it - no need to know column names.');
  const stockByPlantHtml = stockTableHtml;

  const purchaseActivityHtml =
    '<div class="field-block"><h4>Quantity</h4>' + atAGlanceHtml + '</div>' +
    '<div class="section-title">Open Purchase Orders</div>' +
    (openLinked.length
      ? '<div class="table-wrap"><table><thead><tr><th>PO Number</th><th>Plant</th><th>Qty</th><th>Status</th></tr></thead><tbody>' +
          openLinked.map(l => '<tr><td><b>' + escapeHtml(l.po.poNumber) + '</b></td><td>' + escapeHtml(l.plantLabel) +
            '</td><td>' + (l.item.qty != null ? l.item.qty : '-') + ' ' + escapeHtml(l.item.uom || '') +
            '</td><td><span class="status-pill status-' + l.po._status + '">' + escapeHtml(STATUS_LABELS[l.po._status]) + '</span></td></tr>').join('') +
        '</tbody></table></div>'
      : '<div class="no-data-note">No open purchase orders currently linked to this material by automated matching.</div>');

  const priceTrendHtml =
    '<div class="chart-box" id="matPriceTrendBox"><div class="no-data-note">Loading&hellip;</div></div>' +
    '<div class="no-data-note mt-8">Moving averages are a trailing calendar-day average of actual PO prices (21/50/100 days back from each PO date) - since POs happen irregularly rather than daily, this smooths the line without inventing prices on days with no PO.</div>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>' + escapeHtml(anchor.description || anchor.materialCode) + '</h2>' +
    '<div class="modal-meta">' + escapeHtml(category || 'Uncategorized') + ' &middot; rolled up across ' + siblingLots.length + ' plant location' + (siblingLots.length === 1 ? '' : 's') + '</div></div>' +
    '<span class="close-btn">&times;</span></div>' +
    '<div class="validation-note"><svg class="validation-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 21h20L12 3Z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r=".6" fill="currentColor" stroke="none"/></svg> <div>Purchase Activity, Price Trend, Vendors, and "Also known as" are computed by automatically matching this material to purchase order line items by description (and vendor, where known) - not guaranteed-correct identity resolution. <strong>Verify manually before treating a match as ground truth.</strong></div></div>' +
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
  wireEditIcons(body, (lineEl) => materialFieldsUrl(lineEl.dataset.plant, lineEl.dataset.item), switchToFlagsTab, async () => {
    PLANT_KEYS.forEach(key => { MATERIALS_BY_PLANT[key] = null; });
    await openMaterialModal(compositeKey);
    renderMaterialsView();
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
  // under the Stock by Plant table.
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
