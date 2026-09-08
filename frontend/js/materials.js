// ── Raw Material Analysis (list rendering) ──────────────────────────────
// One row per Stock lot (see file header). No purchase-type split here (see
// the module docstring at the top of this file for why) - only the
// plant/All-Plants axis applies.
function currentMaterials() {
  const keys = selectedPlantKeys();
  if (keys.length === 1) return MATERIALS_BY_PLANT[keys[0]] || [];
  const merged = [];
  keys.forEach(key => {
    (MATERIALS_BY_PLANT[key] || []).forEach(m => {
      merged.push(Object.assign({}, m, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });
  return merged;
}

async function ensureMaterialsLoaded(plantKeys) {
  await Promise.all(plantKeys.map(async key => {
    if (!MATERIALS_BY_PLANT[key]) {
      const data = await apiForPlant(key, '/materials');
      MATERIALS_BY_PLANT[key] = data.materials;
    }
  }));
}

async function loadAndRenderMaterials() {
  const el = document.getElementById('viewContent');
  el.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading materials&hellip;</div></div>';
  try {
    // Also loads Purchase Orders for the same plant(s) - the KPI row and
    // drill-down chart below need PO line items to compute the "in transit"/
    // discrepancy/data-quality numbers (see materialLinksToItem()), not just
    // Stock data. Cheap: ensurePOsLoaded/ensureMaterialsLoaded both cache per
    // plant, so this is a no-op re-fetch if the Purchase Orders tab was
    // already visited for the same plant(s).
    await Promise.all([ensureMaterialsLoaded(selectedPlantKeys()), ensurePOsLoaded(selectedPlantKeys())]);
    el.innerHTML = '<div id="materialsContent"></div>';
    renderMaterialsView();
  } catch (e) {
    console.error('loadAndRenderMaterials failed:', e);
    el.innerHTML = '<div class="noaccess">Couldn\'t load material data right now. Please refresh, or contact IT if this keeps happening.</div>';
  }
}

// ── Material <-> PO line item linkage (best-effort, not ground truth) ──────
// There's no persisted DB join between a Stock lot and a PO line item (the
// only real chain is Stock<->MIR<->PO via *MirStockMatch/*POMirMatch, which
// by construction only ever covers already-delivered/matched goods - never
// an open, undelivered PO). So "which POs/vendors belong to this material"
// is computed the same best-effort way this app's own backend already
// computes PO<->MIR matches (apps/services/matching.py): normalized
// description equality (always linked), or token-overlap above
// MATERIAL_LINK_THRESHOLD gated on vendor when the material has one (Achhad
// Stock rows have no vendor column at all - description-only there, same
// precedent as matching_achhad.py's weaker material-only gate). NOT
// guaranteed-correct identity resolution - same "verify manually" caveat
// this app already gives PO<->MIR matches (see matchStatusHtml()).
const MATERIAL_LINK_THRESHOLD = 0.3;
function materialLinksToItem(material, po, item) {
  const normMat = normalizeMaterial(material.description);
  const normItem = normalizeMaterial(item.description);
  if (!normMat || !normItem) return false;
  if (normMat === normItem) return true;
  const a = new Set(tokenizeMaterial(material.description));
  const b = new Set(tokenizeMaterial(item.description));
  if (!a.size || !b.size) return false;
  let overlapCount = 0;
  a.forEach(t => { if (b.has(t)) overlapCount++; });
  const union = new Set([...a, ...b]).size;
  if (overlapCount / union < MATERIAL_LINK_THRESHOLD) return false;
  // `material` is either a single Stock lot (real `.vendor` string) or an
  // aggregateMaterialsByName() group (real `.vendors` array, one per
  // distinct contributing lot's vendor) - gate on whichever is present, any
  // one of a group's vendors clearing the gate is enough. Neither present
  // (Achhad has no vendor column at all) - description-only, same weaker-
  // gate precedent as matching_achhad.py.
  const vendorCandidates = (material.vendors && material.vendors.length) ? material.vendors : (material.vendor ? [material.vendor] : []);
  if (vendorCandidates.length) {
    const poVendor = normalizeVendor(po.vendorName);
    return vendorCandidates.some(v => vendorContains(normalizeVendor(v), poVendor));
  }
  return true;
}

// Days-left confidence bands, weakest to strongest (see
// apps/services/stock_consumption.py's own _BANDS) - used both to pick a
// group's overall confidence (the weakest contributing lot's band, never
// the best or a mean - see aggregateMaterialsByName() below) and to color
// the confidence dot (daysLeftCellHtml()).
const CONF_RANK = { none: 0, low: 1, medium: 2, high: 3 };
const CONF_DOT_CLASS = { high: 'conf-dot-high', medium: 'conf-dot-medium', low: 'conf-dot-low', none: 'conf-dot-low' };

// One row per unique material NAME within the current plant scope (sums
// qty/value across every vendor lot contributing to it, and across all 3
// plants when "All Plants" is selected) - per the project owner's
// 2026-09-04 request, matching CLAUDE.md's long-planned "Materials
// consolidated cross-plant view" roadmap item. Scoped to selectedPlantKeys()
// like the rest of this view (not always cross-plant) - a single plant tab
// sums only that plant's own vendor lots, "All Plants" sums across all
// three. Never silently loses the underlying per-vendor/per-plant detail
// CLAUDE.md warned about losing (`lots` keeps every contributing row,
// `vendors` a deduped list) - the row-click modal and materialLinksToItem()
// above both use these, not just the summed totals.
function aggregateMaterialsByName(lots) {
  const groups = new Map();
  lots.forEach(m => {
    const key = normalizeMaterial(m.description);
    if (!key) return;
    if (!groups.has(key)) {
      groups.set(key, { description: m.description, materialCode: m.materialCode, category: '', subCategory: '', qty: 0, value: 0, rates: new Set(), vendorSeen: new Set(), vendors: [], lots: [] });
    }
    const g = groups.get(key);
    if (!g.category && m.category) g.category = m.category;
    if (!g.subCategory && m.subCategory) g.subCategory = m.subCategory;
    g.qty += (m.qty || 0);
    g.value += (m.value || 0);
    if (m.rate != null) g.rates.add(m.rate);
    if (m.vendor) {
      const nv = normalizeVendor(m.vendor);
      if (nv && !g.vendorSeen.has(nv)) { g.vendorSeen.add(nv); g.vendors.push(m.vendor); }
    }
    g.lots.push(m);
  });
  // Rate is only shown when every contributing lot agrees on it - once
  // summed across vendors/plants a single "rate" is otherwise misleading
  // (which lot's rate would it even be?), same reasoning as the reference
  // design's own "Not available" cells for multi-lot materials.
  return Array.from(groups.values()).map(g => {
    // Days-left is not summable (averaging/adding days across lots is
    // simply wrong) - sum the consumption *rates* instead, then divide the
    // already-summed quantity. Group confidence is the weakest contributing
    // lot's band: one thin lot makes the whole group's rate thin, and
    // taking the best or the mean would overstate it. A lot with no
    // consumption block yet (brand-new, or too little history) counts as
    // 'none' for this purpose, same as the backend's own confidence value.
    let avgDaily = 0;
    let weakest = null;
    g.lots.forEach(l => {
      const c = l.consumption;
      const conf = (c && c.confidence) || 'none';
      if (c && c.avgDaily) avgDaily += c.avgDaily;
      if (!weakest || CONF_RANK[conf] < CONF_RANK[weakest.confidence]) {
        weakest = { confidence: conf, historyDays: c ? c.historyDays : 0, intervalsUsed: c ? c.intervalsUsed : 0 };
      }
    });
    return {
      description: g.description, materialCode: g.materialCode,
      category: g.category, subCategory: g.subCategory,
      qty: g.qty, value: g.value,
      rate: g.rates.size === 1 ? Array.from(g.rates)[0] : null,
      // True if ANY contributing lot has a real Stock<->MIR match (see the
      // backend's `mirMatched` field, apps/api/routers/*_views.py) - not a
      // frontend-computed proxy.
      mirMatched: g.lots.some(l => l.mirMatched),
      vendors: g.vendors, lots: g.lots, anchorLot: g.lots[0],
      consumption: {
        avgDaily: avgDaily > 0 ? avgDaily : null,
        daysLeft: avgDaily > 0 ? g.qty / avgDaily : null,
        confidence: weakest ? weakest.confidence : 'none',
        historyDays: weakest ? weakest.historyDays : 0,
        intervalsUsed: weakest ? weakest.intervalsUsed : 0,
      },
    };
  });
}

// Below-15-days-of-cover, or already at/below Achhad's msl reorder point
// (daysToMsl === 0 - see _domestic_base.py's _lot_dict()) on any
// contributing lot - wired into the Status filter (matStatusFilter
// 'lowstock') alongside the existing qtydisc/ratedisc/flags states.
function isMaterialLowStock(m) {
  if (m.consumption && m.consumption.daysLeft != null && m.consumption.daysLeft < 15) return true;
  return (m.lots || []).some(l => l.daysToMsl === 0);
}

// Days Left cell: the number plus a confidence dot (green/amber/grey),
// tooltipped with the history it's based on - the visible half of the
// decision to show a days-left figure even on thin history (see
// apps/services/stock_consumption.py's module docstring). Band 'none'
// renders an em dash instead of a number - a two-day estimate must never
// look identical to a month-long one.
function daysLeftCellHtml(m) {
  const c = m.consumption;
  const confidence = (c && c.confidence) || 'none';
  const dotClass = CONF_DOT_CLASS[confidence] || CONF_DOT_CLASS.none;
  if (confidence === 'none' || !c) {
    return '<span class="days-left-cell"><span class="days-left-value">&mdash;</span>' +
      '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="Not enough snapshot history yet" tabindex="0"></span></span>';
  }
  const valueText = c.daysLeft != null ? Math.round(c.daysLeft).toLocaleString('en-IN') + ' d' : 'No movement';
  const tip = 'Based on ' + c.historyDays + ' day' + (c.historyDays === 1 ? '' : 's') + ' of history (' + c.intervalsUsed + ' interval' + (c.intervalsUsed === 1 ? '' : 's') + ')';
  return '<span class="days-left-cell"><span class="days-left-value">' + escapeHtml(valueText) + '</span>' +
    '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="' + escapeHtml(tip) + '" tabindex="0"></span></span>';
}

// All (po, item) pairs across the given plant keys' cached PO data that link
// to `material` - each pair tagged with its plant key/label for display.
// Callers must ensure PURCHASE_ORDERS_BY_PLANT is already populated for
// every key in plantKeys (ensurePOsLoaded()) before calling this.
function linkedPoItemsForMaterial(material, plantKeys) {
  const out = [];
  plantKeys.forEach(key => {
    (PURCHASE_ORDERS_BY_PLANT[key] || []).forEach(po => {
      (po.items || []).forEach(item => {
        if (materialLinksToItem(material, po, item)) {
          out.push({ po, item, plantKey: key, plantLabel: PLANTS[key].label });
        }
      });
    });
  });
  return out;
}

// Every material with >=1 linked open (non-received) PO line item, for the
// KPI row below - computed once per render and reused across cards 3/4/5/6/7
// rather than re-scanning PURCHASE_ORDERS_BY_PLANT per card.
function computeMaterialPoLinkage(materials, plantKeys) {
  return materials.map(m => {
    const links = linkedPoItemsForMaterial(m, plantKeys);
    links.forEach(l => { l.po._status = l.po._status || computeStatus(l.po); if (!l.po._categories) computePoFlags(l.po); });
    const openLinks = links.filter(l => l.po._status !== 'received');
    // Same categories a PO row's own rowFlags() shows (see renderPoList()),
    // but scoped to just this material's own linked line item(s) - qty/rate
    // discrepancy is checked against `l.item`'s own diff%, not the parent
    // PO's blanket _qtyFlag/_rateFlag, so a flag on a different line item in
    // a multi-item PO never gets misattributed to this material. Remarks-
    // based info categories stay PO-level (remarks are a whole-PO field, no
    // finer-grained source exists) - same as computePoFlags() itself.
    const catMap = new Map();
    links.forEach(l => {
      if (l.item.qtyDiffPct != null && l.item.qtyDiffPct > FLAG_PCT) catMap.set('Quantity Mismatch in MIR', { label: 'Quantity Mismatch in MIR', severity: 'critical' });
      if ((l.item.rateDiffPct != null && l.item.rateDiffPct > FLAG_PCT) || (l.item.valueDiffPct != null && l.item.valueDiffPct > FLAG_PCT)) catMap.set('Rate / Value Mismatch in MIR', { label: 'Rate / Value Mismatch in MIR', severity: 'critical' });
      if (l.po.remarks) { const c = categorizeFlag(l.po.remarks); catMap.set(c.label, c); }
    });
    const categories = Array.from(catMap.values());
    // Same reasoning as computePoFlags()'s own _maxDiffPct (see flags.js) -
    // drives rowTintClass()'s severity-scaled row background.
    const allDiffs = links.flatMap(l => [l.item.qtyDiffPct, l.item.rateDiffPct, l.item.valueDiffPct]).filter(v => v != null);
    return {
      material: m,
      links,
      openLinks,
      categories,
      qtyFlag: categories.some(c => c.label === 'Quantity Mismatch in MIR'),
      rateFlag: categories.some(c => c.label === 'Rate / Value Mismatch in MIR'),
      hasInfoFlag: categories.some(c => c.severity === 'info'),
      maxDiffPct: allDiffs.length ? Math.max(...allDiffs) : 0,
    };
  });
}

// Materials' own status pill, same visual language as PO's Received/
// Partial Delivered/Pending/Overdue/Delivery Date Unknown pills (reuses the
// identical .status-pill.status-<key> CSS classes - no new styling needed).
// Not a delivery status like a PO has - a "business state" for the material
// itself, derived from its linked POs' own statuses + current stock:
//   overdue   - at least one linked, still-open PO is itself overdue
//   partial   - some qty already in stock AND a linked PO is still open
//               (mid-replenishment - part on hand, more incoming)
//   onorder   - a linked PO is open, nothing in stock yet
//   received  - every linked PO has been fully received, nothing open
//   instock   - no linked PO history at all, just sitting in stock (or,
//               rarer, no stock and no PO history either)
const MAT_STATUS_LABELS = { received: 'Received', partial: 'Partial', onorder: 'On Order', overdue: 'Overdue', instock: 'In Stock' };
const MAT_STATUS_PILL_CLASS = { received: 'status-received', partial: 'status-partial', onorder: 'status-pending', overdue: 'status-overdue', instock: 'status-unknown' };
function computeMaterialStatus(m, entry) {
  const links = entry ? entry.links : [];
  const openLinks = entry ? entry.openLinks : [];
  const stocked = (m.qty || 0) > 0;
  if (openLinks.some(l => l.po._status === 'overdue')) return 'overdue';
  if (openLinks.length && stocked) return 'partial';
  if (openLinks.length) return 'onorder';
  // "Received" once there's real evidence of an MIR receipt (m.mirMatched -
  // the actual Stock<->MIR match, see materialStepperHtml()'s comment) or,
  // failing that, a fuzzy-matched PO that's already fully received - never
  // inferred from stock qty alone, since qty>0 with neither signal just
  // means the material is sitting in stock with no traceable receipt found.
  if (m.mirMatched || links.length) return 'received';
  return 'instock';
}

// Table-only header filters (Material text search + Progress) - never touch
// the KPI row/chart, only which rows the table shows. Same role/shape as
// PO's applyColFilters()/state.colFilters. Category/Sub Category filter in
// the header row too (see matFilterCells below) but write into
// state.matCategoryFilter/matSubCategoryFilter directly, not here - they're
// "global" filters that narrow `filtered` before this function ever runs
// (KPI row + chart + table all reflect them), same as PO's Status. Status
// isn't filtered here either - it's applied separately in
// renderMaterialsView() itself since it needs the `linkage` lookup, not
// just a plain field on `m`. No more Stock/Inventory Value/Latest Rate
// range filters (project owner, 2026-09-04, removed to make room for
// Category/Sub Category/Progress).
function applyMatColFilters(recs) {
  const f = state.matColFilters;
  return recs.filter(m => {
    if (f.material && !(m.description || '').toLowerCase().includes(f.material.toLowerCase())) return false;
    if (f.progress === 'mirmatched' && !m.mirMatched) return false;
    if (f.progress === 'notmirmatched' && m.mirMatched) return false;
    return true;
  });
}

function renderMaterialsView() {
  const el = document.getElementById('materialsContent');
  destroyPageCharts();
  // One row per unique material NAME within the current plant scope, not
  // per Stock lot (see aggregateMaterialsByName()'s own header comment for
  // why/scope) - "All Plants" sums across all 3 plants, a single plant tab
  // sums only that plant's own vendor lots.
  const allLots = currentMaterials();
  const all = aggregateMaterialsByName(allLots);

  const catCounts = {};
  all.forEach(m => { catCounts[m.category || 'Uncategorized'] = (catCounts[m.category || 'Uncategorized'] || 0) + 1; });
  const catOptions = Object.entries(catCounts).sort((a, b) => b[1] - a[1]);

  // Sub-category options are scoped to the currently selected category (if
  // any) - a cascading dropdown, same UX convention as most category/
  // sub-category filter pairs, so it never offers a sub-category that can't
  // possibly match anything under the chosen category.
  const inSelectedCategory = state.matCategoryFilter ? all.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter) : all;
  const subCatCounts = {};
  inSelectedCategory.forEach(m => { subCatCounts[m.subCategory || 'Uncategorized'] = (subCatCounts[m.subCategory || 'Uncategorized'] || 0) + 1; });
  const subCatOptions = Object.entries(subCatCounts).sort((a, b) => b[1] - a[1]);

  // "Global" filters - narrow `filtered` itself, so the KPI row and
  // drill-down chart reflect only the selected category/sub-category, not
  // just the table (see state.matCategoryFilter's own comment).
  let filtered = all;
  if (state.matCategoryFilter) filtered = filtered.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter);
  if (state.matSubCategoryFilter) filtered = filtered.filter(m => (m.subCategory || 'Uncategorized') === state.matSubCategoryFilter);

  if (!all.length) {
    const msg = isAllPlants()
      ? 'No Raw Material Stock synced yet for any plant.'
      : 'No RM Stock synced yet - run <code>' + PLANTS[state.plant].syncCmdStock + '</code> to load it.';
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml(msg) + '</div>';
    return;
  }

  const plantKeys = selectedPlantKeys();
  const linkage = computeMaterialPoLinkage(filtered, plantKeys);
  // O(1) lookup from a (category/sub-category-filtered) material back to
  // its linkage entry (flags/categories/open-PO links) while rendering the
  // table below - built from the exact same `filtered` array `linkage` was
  // computed from, so every material in `filtered` has an entry.
  const linkageByKey = new Map(linkage.map(l => [normalizeMaterial(l.material.description), l]));

  const totalMaterials = filtered.length;
  const totalValue = filtered.reduce((s, m) => s + (m.value || 0), 0);
  const inTransitValue = linkage.reduce((s, l) => s + l.openLinks.reduce((s2, x) => s2 + (x.item.netPrice != null && x.item.qty != null ? x.item.netPrice * x.item.qty : 0), 0), 0);
  const qtyOrderedOpen = linkage.reduce((s, l) => s + l.openLinks.reduce((s2, x) => s2 + (x.item.qty || 0), 0), 0);
  const qtyDiscMats = linkage.filter(l => l.qtyFlag);
  const rateDiscMats = linkage.filter(l => l.rateFlag);
  const flaggedMats = linkage.filter(l => l.hasInfoFlag);
  const lowStockMats = filtered.filter(isMaterialLowStock);

  // Same visual language as Purchase Orders' KPI row (renderPoList()) -
  // colored left border + flag icon for discrepancy/quality cards, plain
  // counts for the rest. Order: plain counts -> in-transit pair -> the two
  // "critical" (money/quantity) discrepancy cards -> Data Quality Flags last.
  // `fmt` picks the count-up animation's display formatter (wired below,
  // near the [data-matkpi] click handlers) - 'inr' for the two currency
  // cards, 'locale' for the plain comma-grouped quantity, 'int' for the rest.
  const cardDef = [
    { key: 'total', cls: '', label: 'Materials Tracked', raw: totalMaterials, fmt: 'int', tip: 'Distinct materials with current stock, summed across every vendor lot.' },
    { key: 'value', cls: '', label: 'Total Inventory Value (Warehouse)', raw: totalValue, fmt: 'inr', tip: 'Current stock quantity x rate, summed across every lot in the selected plant(s).' },
    { key: 'transit', cls: 'partial', label: 'Inventory Value in Transit (Open POs)', raw: inTransitValue, fmt: 'inr', tip: 'Value of ordered-but-not-yet-received line items linked to this material.' },
    { key: 'qtyordered', cls: 'partial', label: 'Quantity Ordered (Open POs)', raw: qtyOrderedOpen, fmt: 'locale', tip: 'Total quantity still open on purchase orders linked to this material.' },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Mismatches', raw: qtyDiscMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical, tip: 'Quantity mismatch in MIR: a linked PO line item\'s quantity differs from its matched MIR entry.' },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Mismatches', raw: rateDiscMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical, tip: 'Rate mismatch in MIR: a linked PO line item\'s rate differs from its matched MIR entry.' },
    { key: 'lowstock', cls: 'critical', label: 'Low Stock (Reorder Soon)', raw: lowStockMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical, tip: 'Under 15 days of cover at the current consumption rate, or already at/below Achhad\'s minimum stock level.' },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', raw: flaggedMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.quality, tip: 'Paperwork/process notes on a linked PO\'s remarks - not a money or quantity problem.' },
  ];
  const kpiHtml = cardDef.map(c => '<div class="kpi-card ' + c.cls + ' ' + (state.matStatusFilter === c.key ? 'active' : '') + '" data-matkpi="' + c.key + '" tabindex="0" role="button" aria-pressed="' + (state.matStatusFilter === c.key) + '">' +
    (c.flag ? flagIconHtml(c.flag) : '') +
    '<div class="val" data-count-target="' + c.raw + '" data-count-fmt="' + c.fmt + '">0</div><div class="label">' + escapeHtml(c.label) + (c.tip ? infoTooltipHtml(c.tip) : '') + '</div></div>').join('');

  // matStatusFilter is table-only-in-effect here (like PO's statusFilter on
  // its own table) even though it's also a KPI-card click target - narrows
  // `tableRecs`, not `filtered`, so the KPI counts above always show the
  // full category/sub-category-filtered picture regardless of which status
  // chip is selected, exactly like PO's Quantity/Rate Discrepancy cards.
  let tableRecs = filtered;
  if (state.matStatusFilter === 'qtydisc') tableRecs = qtyDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'ratedisc') tableRecs = rateDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'lowstock') tableRecs = lowStockMats;
  else if (state.matStatusFilter === 'flags') tableRecs = flaggedMats.map(l => l.material);
  tableRecs = applyMatColFilters(tableRecs);
  // "Materials by Stock Quantity" - sorted by stock qty descending, not
  // value (per the reference design).
  const sorted = tableRecs.slice().sort((a, b) => (b.qty || 0) - (a.qty || 0));

  const showingAll = state.showAllMaterials;
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const matTablePage = Math.min(Math.max(1, state.matTablePage), totalPages);
  const listRecs = showingAll ? sorted.slice((matTablePage - 1) * PAGE_SIZE, matTablePage * PAGE_SIZE) : sorted.slice(0, 5);

  const stockAllPlantsSuffix = isAllPlants() ? ' (All Plants)' : '';

  // Colored flag-icon cluster per material row, identical pattern to PO's
  // own rowFlags() in renderPoList() (categoryColor()/CATEGORY_COLORS +
  // .row-flag-wrap's CSS hover tooltip) - built from computeMaterialPoLinkage()'s
  // `categories` list. Rendered next to the status pill below (see
  // computeMaterialStatus()), same "pill + flags" combo as PO's own Status
  // column.
  const rowFlags = entry => {
    const cats = entry ? entry.categories : [];
    if (!cats.length) return '';
    return cats.map(c =>
      '<span class="row-flag-wrap" data-tooltip="' + escapeHtml(c.label) + '">' + flagIconHtml(categoryColor(c.label), 'row-flag-icon') + '</span>'
    ).join('');
  };

  // Header filter row inside the table, same pattern as PO's filterCells -
  // text search for Material, a <select> each for Category, Sub Category,
  // Status, and Progress (project owner, 2026-09-04: added Category/Sub
  // Category/Progress here, removed the old Stock/Inventory Value/Latest
  // Rate min/max ranges). One entry per <th> in the table header (Material,
  // Category, Sub Category, Stock, Inventory Value, Latest Rate, Status,
  // Progress, Details) - Stock/Inventory Value/Latest Rate/Details have no
  // header-row control of their own, so they still need an empty
  // placeholder entry, or every later cell silently shifts one column left
  // under the wrong header.
  // Category/Sub Category header-row selects reuse catOptions/subCatOptions
  // (already built above for the "Filter by" bar) and write into
  // state.matCategoryFilter/matSubCategoryFilter directly - same field, two
  // controls, same single-source-of-truth reasoning as Status. No more
  // Stock/Inventory Value/Latest Rate range filters (project owner,
  // 2026-09-04, removed to make room for these plus Progress).
  const matCatColOptionsHtml = catOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('');
  const matSubCatColOptionsHtml = subCatOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matSubCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('');
  const matFilterCells = [
    '<input type="text" class="col-filter-input" data-mcf="material" placeholder="Search..." value="' + escapeHtml(state.matColFilters.material) + '">',
    '<select class="col-filter-input" data-mcf="category"><option value="">All</option>' + matCatColOptionsHtml + '</select>',
    '<select class="col-filter-input" data-mcf="subCategory"><option value="">All</option>' + matSubCatColOptionsHtml + '</select>',
    '',
    '',
    '',
    '', // Days Left - no header-row control of its own, same reasoning as Stock/Inventory Value/Latest Rate above.
    '<select class="col-filter-input" data-mcf="status"><option value="">All</option>' +
      '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Mismatch</option>' +
      '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch</option>' +
      '<option value="lowstock"' + (state.matStatusFilter === 'lowstock' ? ' selected' : '') + '>Low Stock</option>' +
      '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag</option>' +
    '</select>',
    '<select class="col-filter-input" data-mcf="progress"><option value="">All</option>' +
      '<option value="mirmatched"' + (state.matColFilters.progress === 'mirmatched' ? ' selected' : '') + '>MIR Matched</option>' +
      '<option value="notmirmatched"' + (state.matColFilters.progress === 'notmirmatched' ? ' selected' : '') + '>Not MIR Matched</option>' +
    '</select>',
    '',
  ];
  const colFilterRow = '<tr class="col-filter-row">' + matFilterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';

  const pageButtons = totalPages <= 10
    ? Array.from({ length: totalPages }, (_, i) => i + 1)
        .map(p => '<button class="page-btn page-num' + (p === matTablePage ? ' active' : '') + '" data-matpage="' + p + '">' + p + '</button>')
        .join('')
    : '<span class="page-info">Page ' + matTablePage + ' of ' + totalPages + '</span>';
  const paginationHtml = showingAll && totalPages > 1
    ? '<div class="pagination-row">' +
        '<button id="matPrevPageBtn" class="page-btn"' + (matTablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
        pageButtons +
        '<button id="matNextPageBtn" class="page-btn"' + (matTablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
        jumpToPageHtml('mat', totalPages) +
      '</div>'
    : '';

  el.innerHTML =
    '<div class="section-title">Raw Material and Inventory Analysis: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">One row per unique material' + (isAllPlants() ? ', summed across every vendor lot and all 3 plants' : ', summed across every vendor lot at this plant') + '. Click a row for its full cross-plant analysis.</div>' +
    '<div class="validation-note"><svg class="validation-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 21h20L12 3Z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r=".6" fill="currentColor" stroke="none"/></svg> <div>"Inventory Value in Transit", "Quantity Ordered", and the mismatch/flag columns below are computed by automatically matching each material to purchase order line items by description (and vendor, where known) - the same best-effort approach this app already uses for PO&harr;MIR matching. <strong>Not guaranteed-correct identity resolution - verify manually before relying on it.</strong> "Days Left" is likewise an estimate, derived from recent stock-snapshot history, not a figure reported by the sheet - the confidence dot next to it shows how much history it is based on.</div></div>' +
    '<div class="kpi-grid">' + kpiHtml + '</div>' +
    // "Filter by Category" / "Filter by Sub Category" / "Filter by Flags"
    // bar - moved above the chart (project owner, 2026-09-04) so the chart
    // itself already reflects the selection. All three are "global"
    // filters, same as PO's own category filter bar: Category/Sub Category
    // narrow `filtered` (KPI row + chart + table all reflect them); Flags
    // is just this same bar's control over matStatusFilter (also settable
    // from a KPI-card click or the table's own Status header select - one
    // shared field, three ways to set it, same pattern as PO's Status).
    '<div class="filter-row">' +
      '<div class="filter-group">' +
        '<label>Filter by Category</label>' +
        '<select id="matCatSelect" class="select-w220">' +
          '<option value="">All categories (' + all.length + ')</option>' +
          catOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('') +
        '</select>' +
      '</div>' +
      '<div class="filter-group">' +
        '<label>Filter by Sub Category</label>' +
        '<select id="matSubCatSelect" class="select-w220">' +
          '<option value="">All sub-categories (' + inSelectedCategory.length + ')</option>' +
          subCatOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matSubCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(c) + ' (' + n + ')</option>').join('') +
        '</select>' +
      '</div>' +
      '<div class="filter-group">' +
        '<label>Filter by Flags</label>' +
        '<select id="matFlagsSelect" class="select-w200">' +
          '<option value="">All flags</option>' +
          '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Mismatch (' + qtyDiscMats.length + ')</option>' +
          '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch (' + rateDiscMats.length + ')</option>' +
          '<option value="lowstock"' + (state.matStatusFilter === 'lowstock' ? ' selected' : '') + '>Low Stock (' + lowStockMats.length + ')</option>' +
          '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + flaggedMats.length + ')</option>' +
        '</select>' +
      '</div>' +
      ((state.matCategoryFilter || state.matSubCategoryFilter || state.matStatusFilter) ? '<button id="matClearCategoryFilter">Clear</button>' : '') +
    '</div>' +
    renderMaterialsChart(filtered) +
    '<div class="list-toggle-row"><div class="section-title m-0">Materials by Stock Quantity - showing ' + listRecs.length + ' of ' + sorted.length + '</div>' +
      (listRecs.some(m => { const e = linkageByKey.get(normalizeMaterial(m.description)); return e && (e.qtyFlag || e.rateFlag); }) ? rowTintLegendHtml() : '') +
      (sorted.length > 5 ? '<button class="view-all-btn" id="toggleMatBtn">' + (showingAll ? 'Show top 5' : 'View all ' + sorted.length + ' materials') + '</button>' : '') +
    '</div>' +
    (() => {
      // Compact top-5 preview renders as CSS-grid card rows, same
      // .list-header-row/.top5-list/.top5-row structure as Purchase
      // Orders'/Import Purchases' own top-5 preview (project owner,
      // 2026-09-04: Materials should match PO/Import's card structure, not
      // the other way around - see renderPoList()'s equivalent branch for
      // the fuller reasoning). "View all" still renders as a plain <table>
      // for all three views.
      if (showingAll) {
        return '<div class="table-wrap"><table><thead><tr><th>Material</th><th>Category</th><th>Sub Category</th><th>Stock' + stockAllPlantsSuffix + '</th><th>Inventory Value' + stockAllPlantsSuffix + '</th><th>Latest Rate</th><th>Days Left</th><th>Status</th><th>Progress</th><th>Details</th></tr>' +
          colFilterRow +
        '</thead><tbody>' +
        listRecs.map(m => {
          const anchor = m.anchorLot;
          const key = escapeHtml(plantKeyFor(anchor) + '::' + anchor.lotId);
          const entry = linkageByKey.get(normalizeMaterial(m.description));
          return '<tr class="' + (entry ? rowTintClass(entry).trim() : '') + '"><td><span class="row-link" data-lot="' + key + '">' + escapeHtml(m.description || m.materialCode) + '</span></td>' +
          '<td>' + escapeHtml(m.category || '-') + '</td>' +
          '<td>' + escapeHtml(m.subCategory || '-') + '</td>' +
          '<td>' + (m.qty ? m.qty.toLocaleString('en-IN') : '0') + '</td>' +
          '<td>' + formatInr(m.value || 0) + '</td>' +
          '<td>' + (m.rate != null ? formatInr(m.rate) : 'Not available') + '</td>' +
          '<td>' + daysLeftCellHtml(m) + '</td>' +
          (() => { const st = computeMaterialStatus(m, entry); return '<td><span class="status-pill ' + MAT_STATUS_PILL_CLASS[st] + '">' + escapeHtml(MAT_STATUS_LABELS[st]) + '</span>' + rowFlags(entry) + '</td>'; })() +
          '<td>' + materialStepperHtml(m) + '</td>' +
          '<td><span class="row-link" data-lot="' + key + '">View analysis</span></td></tr>';
        }).join('') +
        '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row grid-cols"><div>Material</div><div>Category</div><div>Sub Category</div><div>Stock' + stockAllPlantsSuffix + '</div><div>Inventory Value' + stockAllPlantsSuffix + '</div><div>Latest Rate</div><div>Days Left</div><div>Status</div><div>Progress</div><div>Details</div></div>' +
        '<div class="list-header-row grid-cols col-filter-row-grid">' + matFilterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="matTop5List">' + listRecs.map(m => {
          const anchor = m.anchorLot;
          const key = escapeHtml(plantKeyFor(anchor) + '::' + anchor.lotId);
          const entry = linkageByKey.get(normalizeMaterial(m.description));
          const st = computeMaterialStatus(m, entry);
          return '<div class="top5-row' + (entry ? rowTintClass(entry) : '') + '">' +
            '<div><span class="row-link" data-lot="' + key + '">' + escapeHtml(m.description || m.materialCode) + '</span></div>' +
            '<div>' + escapeHtml(m.category || 'Not available') + '</div>' +
            '<div>' + escapeHtml(m.subCategory || 'Not available') + '</div>' +
            '<div>' + (m.qty ? m.qty.toLocaleString('en-IN') : '0') + '</div>' +
            '<div>' + formatInr(m.value || 0) + '</div>' +
            '<div>' + (m.rate != null ? formatInr(m.rate) : 'Not available') + '</div>' +
            '<div>' + daysLeftCellHtml(m) + '</div>' +
            '<div><span class="status-pill ' + MAT_STATUS_PILL_CLASS[st] + '">' + escapeHtml(MAT_STATUS_LABELS[st]) + '</span>' + rowFlags(entry) + '</div>' +
            '<div>' + materialStepperHtml(m) + '</div>' +
            '<div><span class="row-link" data-lot="' + key + '">View analysis</span></div></div>';
        }).join('') + '</div>';
    })();

  applyDynamicStyles(el); // chart-box height, legend dots - see shared.js's own comment; must run before wireMaterialsChart() reads the container's height
  wireKpiCountUps();

  document.querySelectorAll('[data-matkpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.matkpi;
    state.matStatusFilter = (state.matStatusFilter === key || key === 'total' || key === 'value' || key === 'transit' || key === 'qtyordered') ? null : key;
    state.matTablePage = 1;
    renderMaterialsView();
  });
  const matCatSelect = document.getElementById('matCatSelect');
  if (matCatSelect) matCatSelect.onchange = () => { state.matCategoryFilter = matCatSelect.value || null; state.matSubCategoryFilter = null; state.matTablePage = 1; renderMaterialsView(); };
  const matSubCatSelect = document.getElementById('matSubCatSelect');
  if (matSubCatSelect) matSubCatSelect.onchange = () => { state.matSubCategoryFilter = matSubCatSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matFlagsSelect = document.getElementById('matFlagsSelect');
  if (matFlagsSelect) matFlagsSelect.onchange = () => { state.matStatusFilter = matFlagsSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matClearCategoryBtn = document.getElementById('matClearCategoryFilter');
  if (matClearCategoryBtn) matClearCategoryBtn.onclick = () => { state.matCategoryFilter = null; state.matSubCategoryFilter = null; state.matStatusFilter = null; state.matTablePage = 1; renderMaterialsView(); };
  const toggleBtn = document.getElementById('toggleMatBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllMaterials = !state.showAllMaterials; state.matTablePage = 1; renderMaterialsView(); };
  document.querySelectorAll('[data-lot]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.lot));

  const matPrevPageBtn = document.getElementById('matPrevPageBtn');
  if (matPrevPageBtn) matPrevPageBtn.onclick = () => { state.matTablePage = Math.max(1, state.matTablePage - 1); renderMaterialsView(); };
  const matNextPageBtn = document.getElementById('matNextPageBtn');
  if (matNextPageBtn) matNextPageBtn.onclick = () => { state.matTablePage = state.matTablePage + 1; renderMaterialsView(); };
  document.querySelectorAll('[data-matpage]').forEach(btn => btn.onclick = () => { state.matTablePage = Number(btn.dataset.matpage); renderMaterialsView(); });
  wireJumpToPage('mat', totalPages, (n) => { state.matTablePage = n; renderMaterialsView(); });

  // Header filter row (only present when showingAll). Text input re-renders
  // live on every keystroke via preserveFocus() (same as PO's own [data-cf]
  // handling - see that function's comment); number/select inputs commit on
  // 'change' instead, since re-rendering mid-typing a number is jarring.
  document.querySelectorAll('[data-mcf]').forEach(inp => {
    const key = inp.dataset.mcf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'number') ? 'change' : 'input';
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'status') { state.matStatusFilter = raw || null; }
      else if (key === 'material') { state.matColFilters.material = raw; }
      else if (key === 'category') { state.matCategoryFilter = raw || null; state.matSubCategoryFilter = null; }
      else if (key === 'subCategory') { state.matSubCategoryFilter = raw || null; }
      else { state.matColFilters[key] = raw; }
      state.matTablePage = 1;
      preserveFocus(el, () => renderMaterialsView());
    });
  });

  wireMaterialsChart(filtered);
}

// ── Drill-down horizontal bar chart: Inventory Value by Category ->
// Subcategory -> Material. Bars/breadcrumb markup built here; the actual
// Chart.js instance + click handling is wired in wireMaterialsChart() right
// after this HTML lands in the DOM (mirrors renderPoList()'s own
// render-HTML-then-wire-charts split).
const MAT_CHART_TOP_N = 12;
function materialsChartLevelData(materials) {
  if (state.matChartLevel === 'category') {
    const totals = {};
    materials.forEach(m => { const k = m.category || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  const inCategory = materials.filter(m => (m.category || 'Uncategorized') === state.matChartCategory);
  if (state.matChartLevel === 'subcategory') {
    const totals = {};
    inCategory.forEach(m => { const k = m.subCategory || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  // 'material' level
  const inSub = inCategory.filter(m => (m.subCategory || 'Uncategorized') === state.matChartSubcategory);
  return inSub.map(m => ({ label: m.description || m.materialCode, value: m.value || 0, material: m }));
}
function renderMaterialsChart(materials) {
  let bars = materialsChartLevelData(materials).sort((a, b) => (b.value || 0) - (a.value || 0));
  if (bars.length > MAT_CHART_TOP_N) {
    const kept = bars.slice(0, MAT_CHART_TOP_N);
    const rest = bars.slice(MAT_CHART_TOP_N);
    const otherTotal = rest.reduce((s, b) => s + (b.value || 0), 0);
    kept.push({ label: 'Other (' + rest.length + ' more)', value: otherTotal, isOther: true });
    bars = kept;
  }
  const crumbs = [{ label: 'All Categories', level: 'category' }];
  if (state.matChartCategory) crumbs.push({ label: state.matChartCategory, level: 'subcategory' });
  if (state.matChartSubcategory) crumbs.push({ label: state.matChartSubcategory, level: 'material' });
  const crumbHtml = crumbs.map((c, i) => {
    const isLast = i === crumbs.length - 1;
    return '<span class="crumb ' + (isLast ? 'active' : '') + '"' + (isLast ? '' : ' data-crumb-level="' + c.level + '"') + '>' + escapeHtml(c.label) + '</span>';
  }).join('<span class="crumb-sep">&rsaquo;</span>');

  if (!bars.length) {
    return '<div class="chart-panel mb-20"><h4>Inventory Value by Category</h4>' +
      '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
      '<div class="no-data-note">No materials in this ' + (state.matChartLevel === 'category' ? 'view' : state.matChartLevel) + ' to chart.</div></div>';
  }
  // Capped range (was Math.max(180, bars.length * 34), uncapped - at the
  // 13-bar max (MAT_CHART_TOP_N + one "Other" bucket), that rendered a
  // ~440px-tall panel, dwarfing the fixed 250px .chart-box every other
  // page's charts use and making Raw Material Analysis feel visually
  // inconsistent next to Purchase Orders/Import Purchases side by side.
  // Chart.js's own maxBarThickness (below) still caps how THICK a bar can
  // get when there's room to spare; nothing here stops it from shrinking
  // bars thinner than that to fit when there are many categories - a
  // legible tradeoff for a panel that stays roughly the same size as every
  // other chart on this dashboard, not a case-by-case judgment call.
  const chartHeight = Math.min(300, Math.max(220, bars.length * 22));
  return '<div class="chart-panel mb-20"><h4>Inventory Value by Category' + (state.matChartLevel !== 'category' ? ' &rsaquo; Subcategory' : '') + (state.matChartLevel === 'material' ? ' &rsaquo; Material' : '') + '</h4>' +
    '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
    '<div class="chart-box" data-height-px="' + chartHeight + '"><canvas id="matDrillChart"></canvas></div>' +
    (state.matChartLevel !== 'material' ? '<div class="no-data-note mt-8">Click a bar to drill down.</div>' : '') +
  '</div>';
}
function wireMaterialsChart(materials) {
  document.querySelectorAll('[data-crumb-level]').forEach(c => c.onclick = () => {
    const level = c.dataset.crumbLevel;
    state.matChartLevel = level;
    if (level === 'category') { state.matChartCategory = null; state.matChartSubcategory = null; }
    if (level === 'subcategory') state.matChartSubcategory = null;
    renderMaterialsView();
  });
  const canvas = document.getElementById('matDrillChart');
  if (!canvas) return;
  let bars = materialsChartLevelData(materials).sort((a, b) => (b.value || 0) - (a.value || 0));
  if (bars.length > MAT_CHART_TOP_N) {
    const kept = bars.slice(0, MAT_CHART_TOP_N);
    const rest = bars.slice(MAT_CHART_TOP_N);
    kept.push({ label: 'Other (' + rest.length + ' more)', value: rest.reduce((s, b) => s + (b.value || 0), 0), isOther: true });
    bars = kept;
  }
  try {
    const chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bars.map(b => b.label),
        datasets: [{
          data: bars.map(b => b.value),
          backgroundColor: bars.map(b => b.isOther ? '#cbd5e1' : '#2563eb'),
          borderRadius: 5,
          borderSkipped: false,
          maxBarThickness: 26,
        }],
      },
      options: {
        indexAxis: 'y',
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8, displayColors: false, callbacks: { label: c => formatInr(c.parsed.x) } },
        },
        scales: {
          x: { grid: { color: '#eef1f5' }, border: { display: false }, ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) } },
          y: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
        },
        onClick: (evt, elements) => {
          if (!elements.length) return;
          const bar = bars[elements[0].index];
          if (bar.isOther) return;
          if (state.matChartLevel === 'category') { state.matChartCategory = bar.label; state.matChartLevel = 'subcategory'; renderMaterialsView(); }
          else if (state.matChartLevel === 'subcategory') { state.matChartSubcategory = bar.label; state.matChartLevel = 'material'; renderMaterialsView(); }
          // `bar.material` is an aggregateMaterialsByName() group now (no
          // top-level lotId of its own) - open on its anchor lot, same as
          // the table's own row click (openMaterialModal re-derives the
          // full cross-plant picture from the description regardless of
          // which specific contributing lot it's handed).
          else if (bar.material) { const anchor = bar.material.anchorLot; openMaterialModal(plantKeyFor(anchor) + '::' + anchor.lotId); }
        },
      },
    });
    pageCharts.push(chart);
  } catch (e) {
    console.error('Materials drill-down chart failed to render:', e);
    if (canvas.parentNode) canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
  }
}

