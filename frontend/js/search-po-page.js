// search-po.html's page-bootstrap script - extracted from an inline
// <script> so script-src can drop 'unsafe-inline' (see
// config/security_headers.py). Content unchanged from the inline version -
// a straight extraction, not a rewrite.
let POS_BY_PLANT = {}; // lazy-loaded per plant, cached for this page's lifetime
let loadPromise = null;
let dataReady = false; // true once loadPromise has actually resolved - see runSearch()'s spinner check

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'search');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();

  // Kicked off here rather than waiting for the first search (added
  // alongside live-as-you-type search below, 2026-09-10, project owner:
  // "make searching a bit more user friendly") - by the time the user has
  // typed enough characters to search, the data is very likely already
  // cached, so the debounced live search below rarely has to show its own
  // loading spinner. Fire-and-forget (not awaited) - a viewer who never
  // searches at all still only pays for this once, same as before, just
  // started a little earlier instead of waiting for their first keystroke.
  ensureLoaded();

  document.getElementById('searchBtn').onclick = () => runSearch();
  ['searchInput', 'vendorInput', 'materialInput', 'dateFromInput', 'dateToInput'].forEach(id => {
    const el = document.getElementById(id);
    // Live search (2026-09-10): every keystroke/date pick re-filters
    // automatically, debounced so a fast typist doesn't re-run the filter
    // on every single character - no more "type, then remember to click
    // Search" step for the common case. Enter/the Search button still
    // trigger an immediate, non-debounced search for anyone who prefers
    // the old explicit-submit feel (e.g. muscle memory, or wanting to
    // finish a whole query before searching).
    el.addEventListener('input', () => runSearch({ debounce: true }));
    el.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  });
  document.getElementById('clearFiltersBtn').onclick = () => {
    clearTimeout(searchDebounceTimer);
    ['searchInput', 'vendorInput', 'materialInput', 'dateFromInput', 'dateToInput'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('detailArea').innerHTML = '';
    document.getElementById('resultsArea').innerHTML = '';
  };
})();

// All 3 plants' PO data is fetched once (see the prefetch-on-load comment
// above) and cached for this page's lifetime - ensureLoaded() is still
// safe to call from runSearch() too, since a second call while the first
// is still in flight (or already resolved) just returns the same promise.
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
  })).then(() => { dataReady = true; });
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

// Debounce delay for live-as-you-type search - long enough that a fast
// typist doesn't re-filter on every keystroke, short enough that the result
// still feels immediate once they pause. 250ms is the same rough figure
// most search-as-you-type UIs settle on.
const SEARCH_DEBOUNCE_MS = 250;
let searchDebounceTimer = null;
// Stale-response guard, same pattern/reasoning as charts.js's own
// modalRequestId (see that file's comment) - without it, a fast typist
// whose earlier keystroke's ensureLoaded() await is still in flight when a
// later keystroke fires its own runSearch() could have the earlier, now-
// stale call's renderResults() overwrite the newer one's results. In
// practice ensureLoaded() usually resolves instantly after the first real
// load (see the page-load prefetch above), so this mostly guards the rare
// case where the very first search races the initial fetch.
let searchRequestId = 0;

/** opts.debounce: true schedules a delayed, cancelable re-search (every
 * keystroke/date-field change) instead of running immediately - used by the
 * live-search wiring above. Any call with opts.debounce falsy (the Search
 * button, Enter key) runs right away and cancels a pending debounced one,
 * so pressing Enter mid-type doesn't leave a stale debounced search to fire
 * a moment later on top of it. */
async function runSearch(opts) {
  opts = opts || {};
  clearTimeout(searchDebounceTimer);
  if (opts.debounce) {
    searchDebounceTimer = setTimeout(() => runSearch(), SEARCH_DEBOUNCE_MS);
    return;
  }

  const myRequestId = ++searchRequestId;
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
  // Only show the spinner before data is cached at all - once
  // ensureLoaded() has resolved once (the common case now that it's kicked
  // off on page load, see the bootstrap IIFE above), every later keystroke's
  // re-filter is instant and a spinner flash would just be visual noise on
  // top of an otherwise-live search.
  if (!dataReady) {
    resultsEl.innerHTML = '<div class="loading-overlay"><div class="spinner"></div><span>Searching&hellip;</span></div>';
  }
  await ensureLoaded();
  if (myRequestId !== searchRequestId) return; // a newer search superseded this one
  renderResults(f);
}

// Wraps the first case-insensitive occurrence of `query` in `text` with a
// <mark> tag, so a result card visually shows WHY it matched instead of
// making the user re-scan the whole card for their own search term (2026-
// 09-10, project owner: "make searching a bit more user friendly"). Every
// piece of `text` is still run through escapeHtml() - only the tag itself
// is real markup, never anything derived from `text`/`query` directly, so
// this can't reopen the XSS-safety this app's innerHTML sites are otherwise
// careful about (see CLAUDE.md's "Security hardening pass").
function highlightMatch(text, query) {
  const raw = text || '';
  if (!query) return escapeHtml(raw);
  const idx = raw.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escapeHtml(raw);
  return escapeHtml(raw.slice(0, idx)) +
    '<mark class="search-hit">' + escapeHtml(raw.slice(idx, idx + query.length)) + '</mark>' +
    escapeHtml(raw.slice(idx + query.length));
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
    ? '<div class="search-empty text-red pad-y10">Note: ' + failedPlants.map(k => escapeHtml(PLANTS[k].label)).join(', ') +
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
    '<div class="search-empty pad-b12 text-left fs-12-5">' + matches.length + ' result' + (matches.length > 1 ? 's' : '') + ' for ' + criteria + '</div>' +
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
        ? '<div class="search-result-meta">Matched material: ' + materialHit.map(d => highlightMatch(d, f.material)).join(', ') + '</div>'
        : '';
      const poNumberHtml = f.po.length >= MIN_FILTER_LEN ? highlightMatch(po.poNumber, f.po) : escapeHtml(po.poNumber);
      const vendorHtml = f.vendor.length >= MIN_FILTER_LEN
        ? highlightMatch(po.vendorName || 'Vendor not recorded', f.vendor)
        : escapeHtml(po.vendorName || 'Vendor not recorded');
      return '<div class="search-result-card" data-idx="' + i + '" tabindex="0" role="button" aria-label="View details for PO ' + escapeHtml(po.poNumber) + '">' +
        '<div class="search-result-top">' +
          '<span class="search-result-po">' + poNumberHtml + '</span>' +
          '<span class="search-result-plant">' + escapeHtml(PLANTS[m.plantKey].label) + '</span>' +
        '</div>' +
        '<div class="search-result-meta">' + vendorHtml + ' &middot; Created ' + escapeHtml(formatDateIN(po.createdDate)) +
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
    : '<div class="fs-12-5 text-muted">No line items recorded.</div>';
  const remarksHtml = po.remarks
    ? '<div class="detail-block mt-16"><h4>Remarks</h4><div class="line">' + escapeHtml(po.remarks) + '</div></div>'
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
      '<h4 class="detail-h4-label">Items</h4>' +
      itemsHtml +
      remarksHtml +
      '<div class="mt-20"><a href="/" class="btn btn-navy">Open Full Dashboard &rarr;</a></div>' +
    '</div>';
  document.getElementById('detailCloseBtn').onclick = () => { document.getElementById('detailArea').innerHTML = ''; };
  document.getElementById('detailArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
