// search-po.html's page-bootstrap script - extracted from an inline
// <script> so script-src can drop 'unsafe-inline' (see
// config/security_headers.py). Content unchanged from the inline version -
// a straight extraction, not a rewrite.
let POS_BY_PLANT = {}; // lazy-loaded per plant, cached for this page's lifetime
let loadPromise = null;

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'search');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();

  document.getElementById('searchBtn').onclick = runSearch;
  ['searchInput', 'vendorInput', 'materialInput'].forEach(id =>
    document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); })
  );
  document.getElementById('clearFiltersBtn').onclick = () => {
    ['searchInput', 'vendorInput', 'materialInput', 'dateFromInput', 'dateToInput'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('detailArea').innerHTML = '';
    document.getElementById('resultsArea').innerHTML = '';
  };
})();

// All 3 plants' PO data is fetched once, lazily, on the first search -
// not on page load, since a viewer who never searches shouldn't pay for
// 3 fetches they didn't ask for.
function ensureLoaded() {
  if (loadPromise) return loadPromise;
  loadPromise = Promise.all(PLANT_KEYS.map(async key => {
    try {
      const data = await apiForPlant(key, '/purchase-orders');
      POS_BY_PLANT[key] = data.purchaseOrders || [];
    } catch (e) {
      console.error('search-po: failed to load purchase orders for ' + key + ':', e);
      POS_BY_PLANT[key] = null; // failed, not empty - see renderResults()
    }
  }));
  return loadPromise;
}

// Minimum length for a text filter to actually narrow results - a 1-
// character vendor/material filter would match almost everything and
// isn't worth a slow full-item scan across all 3 plants for. Below this,
// a non-empty field is just ignored (not an error) - only the PO Number
// field keeps its own "too short" message, since that's the field every
// user starts with by habit (see runSearch()).
const MIN_FILTER_LEN = 2;

function activeFilters() {
  const trimLower = id => document.getElementById(id).value.trim().toLowerCase();
  return {
    po: trimLower('searchInput'),
    vendor: trimLower('vendorInput'),
    material: trimLower('materialInput'),
    from: document.getElementById('dateFromInput').value || null,
    to: document.getElementById('dateToInput').value || null,
  };
}

function poMatchesFilters(po, f) {
  if (f.po.length >= MIN_FILTER_LEN && !(po.poNumber || '').toLowerCase().includes(f.po)) return false;
  if (f.vendor.length >= MIN_FILTER_LEN && !(po.vendorName || '').toLowerCase().includes(f.vendor)) return false;
  if (f.material.length >= MIN_FILTER_LEN && !(po.items || []).some(it => (it.description || '').toLowerCase().includes(f.material))) return false;
  if (f.from && (!po.createdDate || po.createdDate < f.from)) return false;
  if (f.to && (!po.createdDate || po.createdDate > f.to)) return false;
  return true;
}

async function runSearch() {
  document.getElementById('detailArea').innerHTML = '';
  const resultsEl = document.getElementById('resultsArea');
  const f = activeFilters();

  const hasUsableFilter = f.po.length >= MIN_FILTER_LEN || f.vendor.length >= MIN_FILTER_LEN || f.material.length >= MIN_FILTER_LEN || f.from || f.to;
  if (!hasUsableFilter) {
    const shortField = [
      f.po && 'PO number', f.vendor && 'vendor', f.material && 'material',
    ].filter(Boolean);
    resultsEl.innerHTML = '<div class="search-empty">' +
      (shortField.length
        ? 'Enter at least ' + MIN_FILTER_LEN + ' characters for ' + shortField.join('/') + ', or a date range, to search.'
        : 'Enter a PO number, vendor, or material (at least ' + MIN_FILTER_LEN + ' characters), or a date range, to search.') +
      '</div>';
    return;
  }
  resultsEl.innerHTML = '<div class="loading-overlay"><div class="spinner"></div><span>Searching&hellip;</span></div>';
  await ensureLoaded();
  renderResults(f);
}

function renderResults(f) {
  const failedPlants = PLANT_KEYS.filter(k => POS_BY_PLANT[k] === null);
  const matches = [];
  PLANT_KEYS.forEach(key => {
    (POS_BY_PLANT[key] || []).forEach(po => {
      if (poMatchesFilters(po, f)) matches.push({ po, plantKey: key });
    });
  });

  const resultsEl = document.getElementById('resultsArea');
  const warnHtml = failedPlants.length
    ? '<div class="search-empty" style="color:var(--red);padding:10px 0;">Note: ' + failedPlants.map(k => escapeHtml(PLANTS[k].label)).join(', ') +
      ' failed to load - results from ' + (failedPlants.length > 1 ? 'those plants are' : 'that plant is') + ' missing. Refresh and try again.</div>'
    : '';

  // Human-readable recap of what was actually searched for - useful once
  // there can be up to 4 filters combined at once, not just a single PO
  // number query the way the empty/found messages used to read.
  const criteria = [
    f.po.length >= MIN_FILTER_LEN && 'PO number "' + f.po + '"',
    f.vendor.length >= MIN_FILTER_LEN && 'vendor "' + f.vendor + '"',
    f.material.length >= MIN_FILTER_LEN && 'material "' + f.material + '"',
    (f.from || f.to) && ('created ' + (f.from ? 'from ' + formatDateIN(f.from) : '') + (f.from && f.to ? ' ' : '') + (f.to ? 'to ' + formatDateIN(f.to) : '')),
  ].filter(Boolean).map(escapeHtml).join(', ');

  if (!matches.length) {
    resultsEl.innerHTML = warnHtml + '<div class="search-empty">' + emptyStateHtml('No purchase order matching ' + criteria + ' was found in any plant.') + '</div>';
    return;
  }

  resultsEl.innerHTML = warnHtml +
    '<div class="search-empty" style="padding:0 0 12px;text-align:left;font-size:12.5px;">' + matches.length + ' result' + (matches.length > 1 ? 's' : '') + ' for ' + criteria + '</div>' +
    '<div class="search-results">' + matches.map((m, i) => {
      const po = m.po;
      const matchedCount = (po.items || []).filter(it => it.matched).length;
      const totalCount = (po.items || []).length;
      // When a material filter is active, surface which line item(s) it
      // actually matched - "Vendor A, PO 12345" alone wouldn't otherwise
      // show why this PO showed up for a material search.
      const materialHit = f.material.length >= MIN_FILTER_LEN
        ? (po.items || []).filter(it => (it.description || '').toLowerCase().includes(f.material)).map(it => it.description)
        : [];
      const materialHtml = materialHit.length
        ? '<div class="search-result-meta">Matched material: ' + escapeHtml(materialHit.join(', ')) + '</div>'
        : '';
      return '<div class="search-result-card" data-idx="' + i + '" tabindex="0" role="button" aria-label="View details for PO ' + escapeHtml(po.poNumber) + '">' +
        '<div class="search-result-top">' +
          '<span class="search-result-po">' + escapeHtml(po.poNumber) + '</span>' +
          '<span class="search-result-plant">' + escapeHtml(PLANTS[m.plantKey].label) + '</span>' +
        '</div>' +
        '<div class="search-result-meta">' + escapeHtml(po.vendorName || 'Vendor not recorded') + ' &middot; Created ' + escapeHtml(formatDateIN(po.createdDate)) +
          ' &middot; ' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : 'Value not recorded') +
          ' &middot; ' + matchedCount + ' / ' + totalCount + ' line items matched to MIR</div>' +
        materialHtml +
      '</div>';
    }).join('') + '</div>';

  resultsEl.querySelectorAll('[data-idx]').forEach(el => el.onclick = () => showDetail(matches[Number(el.dataset.idx)]));
}

// A deliberately simpler detail view than the full dashboard modal (see
// this file's header comment) - core fields + line items + remarks,
// no match-confidence/flag-severity styling.
function showDetail(match) {
  const po = match.po;
  const itemsHtml = (po.items || []).length
    ? '<table class="items-table"><thead><tr><th>Description</th><th>Qty</th><th>UOM</th><th>Net Price</th><th>MIR Status</th></tr></thead><tbody>' +
        po.items.map(it => '<tr><td>' + escapeHtml(it.description || '') + '</td><td>' + (it.qty != null ? it.qty : '-') +
          '</td><td>' + escapeHtml(it.uom || '') + '</td><td>' + (it.netPrice != null ? formatInr(it.netPrice) : '-') +
          '</td><td><span class="status-pill ' + (it.matched ? 'matched">Matched' : 'unmatched">Not yet matched') + '</span></td></tr>').join('') +
      '</tbody></table>'
    : '<div style="font-size:12.5px;color:var(--text-muted);">No line items recorded.</div>';
  const remarksHtml = po.remarks
    ? '<div class="detail-block" style="margin-top:16px;"><h4>Remarks</h4><div class="line">' + escapeHtml(po.remarks) + '</div></div>'
    : '';

  document.getElementById('detailArea').innerHTML =
    '<div class="detail-panel">' +
      '<span class="detail-close" id="detailCloseBtn">&times;</span>' +
      '<div class="detail-title">' + escapeHtml(po.poNumber) + '</div>' +
      '<div class="detail-meta">' + escapeHtml(PLANTS[match.plantKey].label) + ' &middot; Domestic Purchases</div>' +
      '<div class="detail-grid">' +
        '<div class="detail-block"><h4>Vendor</h4>' +
          '<div class="line">' + escapeHtml(po.vendorName || '-') + '</div>' +
          '<div class="line">GSTIN: ' + escapeHtml(po.vendorGstin || '-') + '</div>' +
        '</div>' +
        '<div class="detail-block"><h4>Commercials</h4>' +
          '<div class="line">Total incl. tax: ' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : '-') + '</div>' +
          '<div class="line">Payment terms: ' + escapeHtml(po.paymentTerms || '-') + '</div>' +
          '<div class="line">Incoterms: ' + escapeHtml(po.incoterms || '-') + '</div>' +
        '</div>' +
      '</div>' +
      '<h4 style="font-family:var(--font-head);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);margin-bottom:4px;">Items</h4>' +
      itemsHtml +
      remarksHtml +
      '<div style="margin-top:20px;"><a href="/" class="btn btn-navy">Open Full Dashboard &rarr;</a></div>' +
    '</div>';
  document.getElementById('detailCloseBtn').onclick = () => { document.getElementById('detailArea').innerHTML = ''; };
  document.getElementById('detailArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
