// search-po.html's page-bootstrap script - extracted from an inline
// <script> so script-src can drop 'unsafe-inline' (see
// config/security_headers.py). Content unchanged from the inline version -
// a straight extraction, not a rewrite.
let POS_BY_PLANT = {}; // lazy-loaded per plant, cached for this page's lifetime
// Import orders come from one combined cross-plant endpoint, each row tagged
// with its own `plant`. null = the fetch failed (warned about, never read as
// "no import orders").
let IMPORT_POS = [];
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
  const importLoad = (async () => {
    try {
      const data = await apiImports('/purchase-orders');
      IMPORT_POS = data.purchaseOrders || [];
    } catch (e) {
      console.error('search-po: failed to load import purchase orders:', e);
      IMPORT_POS = null; // failed, not empty - see renderResults()
    }
  })();
  loadPromise = Promise.all(PLANT_KEYS.map(async key => {
    try {
      const data = await apiForPlant(key, '/purchase-orders');
      POS_BY_PLANT[key] = data.purchaseOrders || [];
    } catch (e) {
      console.error('search-po: failed to load purchase orders for ' + key + ':', e);
      POS_BY_PLANT[key] = null; // failed, not empty - see renderResults()
    }
  }).concat([importLoad])).then(() => { dataReady = true; });
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
      if (poMatchesFilters(po, f)) matches.push({ po, plantKey: key, kind: 'domestic' });
    });
  });
  // Import orders carry the fields poMatchesFilters() reads (poNumber,
  // vendorName, items[].description, createdDate), so one filter serves
  // both. A plant key the frontend does not know is skipped rather than
  // rendered with an undefined label.
  (IMPORT_POS || []).forEach(po => {
    if (PLANTS[po.plant] && poMatchesFilters(po, f)) matches.push({ po, plantKey: po.plant, kind: 'import' });
  });

  const resultsEl = document.getElementById('resultsArea');
  const failedSources = failedPlants.map(k => PLANTS[k].label).concat(IMPORT_POS === null ? ['Import purchase orders'] : []);
  const warnHtml = failedSources.length
    ? '<div class="search-empty text-red pad-y10">Note: ' + failedSources.map(escapeHtml).join(', ') +
      ' failed to load, so ' + (failedSources.length > 1 ? 'their' : 'its') + ' results are missing. Refresh and try again.</div>'
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
      const matchedCount = (po.items || []).filter(it => itemIsMatched(it, m.kind)).length;
      const value = poValue(po, m.kind);
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
          '<span class="search-result-badges">' +
            (m.kind === 'import' ? '<span class="search-result-plant search-result-import">Import</span>' : '') +
            '<span class="search-result-plant">' + escapeHtml(PLANTS[m.plantKey].label) + '</span>' +
          '</span>' +
        '</div>' +
        '<div class="search-result-meta">' + vendorHtml + ' &middot; Created ' + escapeHtml(formatDateIN(po.createdDate)) +
          ' &middot; ' + (value != null ? formatInr(value) : 'Value not recorded') +
          ' &middot; ' + matchedCount + ' / ' + totalCount + ' line items matched to MIR</div>' +
        materialHtml +
      '</div>';
    }).join('') + '</div>';

  resultsEl.querySelectorAll('[data-idx]').forEach(el => el.onclick = () => showDetail(matches[Number(el.dataset.idx)]));
}

// Deep links into the dashboard (2026-09-19, project owner: "instead of
// open entire dashboard can't we take user to the info about that PO, or
// raw material"). "/" alone landed the reader on All Plants with no filters
// and made them find the record they had just searched for all over again;
// these open that exact PO's / material's own detail modal on arrival. See
// main.js's readDeepLinkParams() for the receiving end.
//
// encodeURIComponent on every value: a real PO number here can contain a
// space, a slash and brackets (e.g. '3000001104 (Changed Purchase Order)',
// 'HRS/HO/26-27/003'), and a material description is free text from a
// spreadsheet.
//
// `kind=import` opens Import Purchases instead: one PO number can exist as
// both a Domestic and an Import order, so the number alone cannot say which
// one was clicked.
function dashboardPoHref(plantKey, poNumber, kind) {
  return '/?plant=' + encodeURIComponent(plantKey) + '&po=' + encodeURIComponent(poNumber) +
    (kind === 'import' ? '&kind=import' : '');
}

// A Domestic line carries a flat `matched`; an Import line carries its MIR
// match as an object (imports_views.py's _mir_match_dict()), null when none.
function itemIsMatched(it, kind) {
  return kind === 'import' ? !!it.mirMatch : !!it.matched;
}

// Domestic sends the PO total incl. tax; Import sends its lines' summed
// total inclusive value, already in INR.
function poValue(po, kind) {
  return kind === 'import' ? po.totalInclusiveValue : po.totalInclTax;
}
function dashboardMaterialHref(description) {
  // No plant: Raw Material Analysis rolls a material up across all three
  // plants anyway (see main.js's own file header), so pinning the link to
  // the PO's plant would only narrow what the reader sees.
  return '/?plant=all&material=' + encodeURIComponent(description);
}

// A deliberately simpler detail view than the full dashboard modal (see
// this file's header comment) - core fields + line items + remarks,
// no match-confidence/flag-severity styling.
function showDetail(match) {
  const po = match.po;
  const isImport = match.kind === 'import';
  // An import line's quantity is the PO's own (qtyAsPerPo), and its price is
  // in the PO currency, so it is shown as a bare number, never as rupees.
  const qtyOf = it => isImport ? it.qtyAsPerPo : it.qty;
  const priceOf = it => it.netPrice == null ? '-' : (isImport ? escapeHtml(String(it.netPrice)) : formatInr(it.netPrice));
  const itemsHtml = (po.items || []).length
    ? '<table class="items-table"><thead><tr><th>Description</th><th>Qty</th><th>UOM</th><th>Net Price' + (isImport ? ' (PO currency)' : '') + '</th><th>MIR Status</th><th>Material</th></tr></thead><tbody>' +
        po.items.map(it => '<tr><td>' + escapeHtml(it.description || '') + '</td><td>' + (qtyOf(it) != null ? qtyOf(it) : '-') +
          '</td><td>' + escapeHtml(it.uom || '') + '</td><td>' + priceOf(it) +
          '</td><td><span class="status-pill ' + (itemIsMatched(it, match.kind) ? 'matched">Matched' : 'unmatched">Not yet matched') + '</span></td>' +
          // Per line item, not once for the PO: a PO can carry several
          // different materials, and "stock and consumption for THIS
          // material" is the question a reader has while looking at that row.
          '<td>' + (it.description
            ? '<a class="detail-link" href="' + escapeHtml(dashboardMaterialHref(it.description)) + '">Stock &amp; usage &rarr;</a>'
            : '-') + '</td></tr>').join('') +
      '</tbody></table>'
    : '<div class="fs-12-5 text-muted">No line items recorded.</div>';
  const remarksHtml = po.remarks
    ? '<div class="detail-block mt-16"><h4>Remarks</h4><div class="line">' + escapeHtml(po.remarks) + '</div></div>'
    : '';
  const value = poValue(po, match.kind);
  // The import list payload carries no vendor GSTIN (a foreign supplier
  // rarely has one); its shipment fields take that line instead.
  const vendorExtraHtml = isImport
    ? '<div class="line">Country of origin: ' + escapeHtml(po.countryOfOrigin || '-') + '</div>' +
      '<div class="line">Bill of Lading: ' + escapeHtml(po.billOfLadingNumber || '-') + '</div>'
    : '<div class="line">GSTIN: ' + escapeHtml(po.vendorGstin || '-') + '</div>';

  document.getElementById('detailArea').innerHTML =
    '<div class="detail-panel">' +
      '<span class="detail-close" id="detailCloseBtn">&times;</span>' +
      '<div class="detail-title">' + escapeHtml(po.poNumber) + '</div>' +
      '<div class="detail-meta">' + escapeHtml(PLANTS[match.plantKey].label) + ' &middot; ' + (isImport ? 'Import Purchases' : 'Domestic Purchases') + '</div>' +
      '<div class="detail-grid">' +
        '<div class="detail-block"><h4>Vendor</h4>' +
          '<div class="line">' + escapeHtml(po.vendorName || '-') + '</div>' +
          vendorExtraHtml +
        '</div>' +
        '<div class="detail-block"><h4>Commercials</h4>' +
          '<div class="line">' + (isImport ? 'Total inclusive value: ' : 'Total incl. tax: ') + (value != null ? formatInr(value) : '-') + '</div>' +
          '<div class="line">Payment terms: ' + escapeHtml(po.paymentTerms || '-') + '</div>' +
          '<div class="line">Incoterms: ' + escapeHtml(po.incoterms || '-') + '</div>' +
        '</div>' +
      '</div>' +
      '<h4 class="detail-h4-label">Items</h4>' +
      itemsHtml +
      remarksHtml +
      '<div class="mt-20"><a href="' + escapeHtml(dashboardPoHref(match.plantKey, po.poNumber, match.kind)) + '" class="btn btn-navy">Open this PO in the dashboard &rarr;</a></div>' +
    '</div>';
  document.getElementById('detailCloseBtn').onclick = () => { document.getElementById('detailArea').innerHTML = ''; };
  document.getElementById('detailArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
