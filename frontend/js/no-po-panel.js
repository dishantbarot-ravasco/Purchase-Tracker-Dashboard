// ── "No purchase order behind it" panel (2026-09-21) ────────────────────────
// The drill-down behind the "N purchased without a PO" badge main.js renders
// beside the sync badges. Project owner: "we have orders without a PO in MIR,
// which might be true or waiting for a PO to be matched with them - can we
// show info about them?"
//
// That question is the whole design. A receipt with no order behind it is
// three different situations wearing one number, and each needs a different
// person to do something different:
//
//   No PO number in MIR        bought (or booked) without an order. Nothing
//                              is pending - purchasing's to close.
//   Names a PO we don't hold   our PO master hasn't got that order yet. The
//                              fix is upstream, in the master CSV.
//   PO on file, not yet        we have the order and the matcher hasn't
//   matched                    linked this receipt to it. Ours to fix.
//
// So the panel is three tabs over one fetch, not one flat list - a flat list
// would put the three back in a pile and lose the only thing that makes them
// actionable. The server decides the bucket (services/mir_without_po.py,
// using the MATCHER's own PO-number logic, not a string compare); this file
// never re-derives it, so the panel and the badge can't disagree.
//
// Reuses the shared #modalBackdrop/#modalBody every other panel uses (same
// pattern as export-panel.js / rodtep-panel.js - see those files' headers),
// and existing CSS only: .items-table, .field-block, .badge, .empty-state.
//
// Read-only. Nothing here writes: the fixes all live outside this app (fill
// in MIR's PO column, add the order upstream, correct a description through
// "Edit Everywhere"), which is exactly why the CSV download matters - it is
// how the list leaves the screen and reaches the person who acts on it.

// Filled by openNoPoPanel(); read by the tab/search handlers so a tab switch
// or a keystroke re-renders from what is already loaded instead of refetching.
let NO_PO_CTX = { plantKey: null, rows: [], summary: null, bucket: 'no_po', query: '' };

// Built ONCE, at load, not per render. debounceRender() is a factory - it
// returns a debounced function holding its own timer - so calling it inside
// the input handler would hand every keystroke a brand-new timer and debounce
// nothing at all. One module-level instance also means a pending tick from
// the previous render can't fire against an input element that render has
// already replaced.
const rerenderNoPoPanelDebounced = debounceRender(() => {
  const box = document.getElementById('noPoSearch');
  if (!box) return;
  NO_PO_CTX.query = box.value;
  renderNoPoPanel();
  // Re-focus the rebuilt input and restore the caret to the end. The whole
  // panel body is replaced on every render, so without this the caret is
  // gone after the first character and the next keystroke goes nowhere -
  // exactly the defect preserveFocus() exists for in the three list views.
  const again = document.getElementById('noPoSearch');
  if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
});

const NO_PO_BUCKET_HELP = {
  no_po: 'MIR names no purchase order at all. Nothing is pending on these - either the material was genuinely bought without raising a PO, or the PO column was left blank. Inter-plant transfers are already excluded.',
  po_unknown: "MIR names a purchase order we have never received. The receipt is fine; our PO master is missing that order, so the fix is upstream in the file the master CSV comes from.",
  po_known_unmatched: 'We hold the order MIR names, and the matcher still has not linked this receipt to any of its line items - usually a quantity, description or vendor-spelling drift.',
};

/** Opens the panel for one plant, on `bucket` (defaults to No PO number). */
async function openNoPoPanel(plantKey, bucket) {
  // The modal stale-response guard every opener that awaits a fetch needs
  // (CLAUDE.md): a slower earlier open must not overwrite a newer modal.
  const myModalRequestId = ++modalRequestId;
  const backdrop = document.getElementById('modalBackdrop');
  const body = document.getElementById('modalBody');
  backdrop.classList.add('open');
  // Dialog semantics + focus trap + Escape-to-close (shared.js). Safe to
  // call before the content lands - openModalA11y() watches the panel and
  // applies the heading label and initial focus as soon as it does.
  openModalA11y(backdrop);
  backdrop.onclick = (e) => { if (e.target === backdrop) closeModal(); };

  const title = 'Receipts with no purchase order behind them';
  body.innerHTML = '<div class="modal-head"><div><h2>' + title + '</h2>'
    + '<div class="modal-meta">Loading&hellip;</div></div><span class="close-btn">&times;</span></div>';

  let data;
  try {
    data = await apiForPlant(plantKey, '/mir-without-po');
  } catch (e) {
    if (myModalRequestId !== modalRequestId) return;
    body.innerHTML = '<div class="modal-head"><div><h2>' + title + '</h2></div><span class="close-btn">&times;</span></div>'
      + '<div class="field-block full-width">' + escapeHtml(e.message || 'Could not load this list right now.') + '</div>';
    return;
  }
  if (myModalRequestId !== modalRequestId) return;

  NO_PO_CTX = {
    plantKey: plantKey,
    rows: data.rows || [],
    summary: data.summary || { buckets: {} },
    // An explicitly requested bucket wins; otherwise open on the first one
    // that actually has rows, so the panel never opens on an empty tab
    // while another holds three hundred.
    bucket: bucket || firstNonEmptyNoPoBucket(data.summary),
    query: '',
  };
  renderNoPoPanel();
}

function firstNonEmptyNoPoBucket(summary) {
  const buckets = (summary && summary.buckets) || {};
  const order = ['no_po', 'po_unknown', 'po_known_unmatched'];
  return order.find(k => buckets[k] && buckets[k].rowCount > 0) || 'no_po';
}

function renderNoPoPanel() {
  const body = document.getElementById('modalBody');
  const ctx = NO_PO_CTX;
  const buckets = ctx.summary.buckets || {};
  const plantLabel = (PLANTS[ctx.plantKey] || {}).label || ctx.plantKey;

  // .sub-tab, not .view-tab - these are a third-level choice inside a panel,
  // and the three tab components are deliberately distinct in this app (see
  // CLAUDE.md's "Dashboard structure"). Don't swap one for another to get a
  // size you prefer.
  const tabsHtml = ['no_po', 'po_unknown', 'po_known_unmatched'].map(key => {
    const b = buckets[key] || { label: key, rowCount: 0 };
    return '<div class="sub-tab ' + (ctx.bucket === key ? 'active' : '') + '" data-nopo-bucket="' + key + '"'
      + ' tabindex="0" role="tab" aria-selected="' + (ctx.bucket === key) + '">'
      + escapeHtml(b.label) + ' (' + b.rowCount + ')</div>';
  }).join('');

  const active = buckets[ctx.bucket] || { rowCount: 0, value: 0 };
  const q = ctx.query.trim().toLowerCase();
  const rows = ctx.rows.filter(r => r.bucket === ctx.bucket).filter(r => !q
    || (r.vendor || '').toLowerCase().includes(q)
    || (r.description || '').toLowerCase().includes(q)
    || (r.poNumberRaw || '').toLowerCase().includes(q)
    || (r.mirNo || '').toLowerCase().includes(q));

  const rowsHtml = rows.length ? rows.map(r =>
    '<tr>'
      + '<td>' + escapeHtml(r.mirNo || '-') + '</td>'
      + '<td>' + formatDateIN(r.mirDate) + '</td>'
      + '<td>' + escapeHtml(r.vendor || '-')
        // Only meaningful in the No-PO bucket, and it is the one distinction
        // worth making there: a vendor already known to be bought without an
        // order is a logged process gap, one that is not is a surprise
        // nobody has looked at. Same split purchasesWithoutPo already draws.
        + (ctx.bucket === 'no_po' && !r.registeredNoPoVendor
            ? ' <span class="badge-warn" title="This supplier is not on the known no-PO list, so nobody has looked at this one yet.">not on the no-PO list</span>'
            : '')
      + '</td>'
      + '<td>' + escapeHtml(r.description || '-') + '</td>'
      + '<td>' + (r.qty == null ? '-' : Number(r.qty).toLocaleString('en-IN')) + ' ' + escapeHtml(r.uom || '') + '</td>'
      + '<td>' + formatInr(r.value == null ? null : Number(r.value)) + '</td>'
      + '<td>' + (r.poNumberRaw ? escapeHtml(r.poNumberRaw) : '<span class="modal-meta">none</span>') + '</td>'
      + '<td>' + escapeHtml(r.invoiceNo || '-') + '</td>'
      + '<td>' + (r.matched
          ? '<span class="badge-verified" title="Reconciled against a purchase order on material description, even though MIR names no PO number.">matched anyway</span>'
          : '') + '</td>'
    + '</tr>'
  ).join('') : '<tr><td colspan="9" class="empty-state">'
      + (ctx.query ? 'Nothing here matches &ldquo;' + escapeHtml(ctx.query) + '&rdquo;.' : 'Nothing in this bucket - good.')
      + '</td></tr>';

  body.innerHTML =
    '<div class="modal-head"><div><h2>Receipts with no purchase order behind them</h2>'
      + '<div class="modal-meta">' + escapeHtml(plantLabel) + ' &middot; ' + ctx.summary.total + ' in total, '
      + ctx.summary.openTotal + ' still open'
      // Stated rather than silently excluded, the same rule the whole no-PO
      // area works under: a reader who subtracts these numbers from the MIR
      // total should be able to see where the rest went.
      + ' &middot; ' + ctx.summary.internalTransfer + ' inter-plant transfers excluded (not purchases)'
      + '</div></div><span class="close-btn">&times;</span></div>'
    + '<div class="sub-tabs" role="tablist">' + tabsHtml + '</div>'
    + '<div class="field-block full-width">'
      + '<div class="validation-note">' + escapeHtml(NO_PO_BUCKET_HELP[ctx.bucket] || '') + '</div>'
      + '<div class="flex-row-gap10 mb-8">'
        + '<input type="search" id="noPoSearch" placeholder="Filter by vendor, material, PO or MIR number"'
        + ' aria-label="Filter this list" value="' + escapeHtml(ctx.query) + '">'
        + '<button id="noPoExportBtn">Download this list (CSV)</button>'
        // The value of the rows SHOWN, beside their count - the bucket's
        // total next to a filtered count read as the filtered rows' value.
        + '<span class="modal-meta">' + rows.length + ' shown &middot; '
          + formatInr(rows.reduce((sum, r) => sum + (r.value == null ? 0 : Number(r.value)), 0))
          + (ctx.query ? ' (of ' + formatInr(active.value) + ' in this tab)' : '') + '</span>'
      + '</div>'
      + '<table class="items-table"><thead><tr>'
        + '<th>MIR No</th><th>MIR Date</th><th>Vendor</th><th>Material</th><th>Qty</th>'
        + '<th>Value</th><th>PO in MIR</th><th>Invoice</th>'
        + '<th><span class="sr-only">Reconciliation note</span></th>'
      + '</tr></thead><tbody>' + rowsHtml + '</tbody></table>'
    + '</div>';

  body.querySelectorAll('[data-nopo-bucket]').forEach(t => {
    const pick = () => {
      if (t.dataset.nopoBucket === NO_PO_CTX.bucket) return;
      NO_PO_CTX.bucket = t.dataset.nopoBucket;
      // The filter text is deliberately NOT carried across tabs - the three
      // buckets hold different vendors, and a query left over from one
      // silently showing "nothing here" in the next reads as an empty bucket.
      NO_PO_CTX.query = '';
      renderNoPoPanel();
    };
    t.onclick = pick;
    t.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
  });

  const search = document.getElementById('noPoSearch');
  // Debounced through the same shared helper the three list views use, for
  // the same reason (see CLAUDE.md's header-filter section): a keystroke must
  // not rebuild the table synchronously per character.
  if (search) search.oninput = rerenderNoPoPanelDebounced;

  const exportBtn = document.getElementById('noPoExportBtn');
  if (exportBtn) exportBtn.onclick = () => {
    // Plain window.open(), not fetch+blob: auth here is an httpOnly cookie,
    // so it rides along on an ordinary same-origin navigation and
    // Content-Disposition does the rest. Same reasoning as export-panel.js.
    window.open(PLANTS[NO_PO_CTX.plantKey].apiPrefix
      + '/mir-without-po?download=csv&bucket=' + encodeURIComponent(NO_PO_CTX.bucket), '_blank');
  };
}
