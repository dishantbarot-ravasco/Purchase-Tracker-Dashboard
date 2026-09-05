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
  document.getElementById('searchInput').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
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

async function runSearch() {
  const query = document.getElementById('searchInput').value.trim();
  document.getElementById('detailArea').innerHTML = '';
  const resultsEl = document.getElementById('resultsArea');
  if (query.length < 2) {
    resultsEl.innerHTML = '<div class="search-empty">Enter at least 2 characters of a PO number to search.</div>';
    return;
  }
  resultsEl.innerHTML = '<div class="loading-overlay"><div class="spinner"></div><span>Searching&hellip;</span></div>';
  await ensureLoaded();
  renderResults(query);
}

function renderResults(query) {
  const q = query.toLowerCase();
  const failedPlants = PLANT_KEYS.filter(k => POS_BY_PLANT[k] === null);
  const matches = [];
  PLANT_KEYS.forEach(key => {
    (POS_BY_PLANT[key] || []).forEach(po => {
      if (po.poNumber && po.poNumber.toLowerCase().includes(q)) matches.push({ po, plantKey: key });
    });
  });

  const resultsEl = document.getElementById('resultsArea');
  const warnHtml = failedPlants.length
    ? '<div class="search-empty" style="color:var(--red);padding:10px 0;">Note: ' + failedPlants.map(k => escapeHtml(PLANTS[k].label)).join(', ') +
      ' failed to load - results from ' + (failedPlants.length > 1 ? 'those plants are' : 'that plant is') + ' missing. Refresh and try again.</div>'
    : '';

  if (!matches.length) {
    resultsEl.innerHTML = warnHtml + '<div class="search-empty">' + emptyStateHtml('No purchase order matching "' + escapeHtml(query) + '" was found in any plant.') + '</div>';
    return;
  }

  resultsEl.innerHTML = warnHtml +
    '<div class="search-results">' + matches.map((m, i) => {
      const po = m.po;
      const matchedCount = (po.items || []).filter(it => it.matched).length;
      const totalCount = (po.items || []).length;
      return '<div class="search-result-card" data-idx="' + i + '" tabindex="0" role="button" aria-label="View details for PO ' + escapeHtml(po.poNumber) + '">' +
        '<div class="search-result-top">' +
          '<span class="search-result-po">' + escapeHtml(po.poNumber) + '</span>' +
          '<span class="search-result-plant">' + escapeHtml(PLANTS[m.plantKey].label) + '</span>' +
        '</div>' +
        '<div class="search-result-meta">' + escapeHtml(po.vendorName || 'Vendor not recorded') + ' &middot; Created ' + escapeHtml(formatDateIN(po.createdDate)) +
          ' &middot; ' + (po.totalInclTax != null ? formatInr(po.totalInclTax) : 'Value not recorded') +
          ' &middot; ' + matchedCount + ' / ' + totalCount + ' line items matched to MIR</div>' +
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
