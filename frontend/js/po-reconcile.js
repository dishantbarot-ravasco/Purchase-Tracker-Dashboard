// ── PO line reconciliation cards (Domestic + Import PO modals) ───────────
// Project owner, 2026-09-24: "along with MIR match show all the MIR's row
// matched and instead of delta in red or green show the real values too for
// immediate reconciliation."
//
// The Item tab used to be one wide table whose MIR column carried "qty Δ67.8%"
// style badges - a percentage with nothing to check it against. Each line is
// now a card that puts the PO's own figures beside what MIR actually
// received, in the PO line's own unit, with the difference as a real number
// and every matched MIR receipt listed underneath.
//
// Two adapters (domesticReconLine() / importReconLine()) turn each router's
// line shape into one neutral object, and ONE renderer draws both - the same
// "one implementation, two callers" shape as wireMirPicker()'s `api` object.
//
// The received side is computed by the backend (matching_core's
// received_against_line(), served as `received` + `matchedMirs`), never here:
// it is the matcher's own unit conversion, so a PO in MT and receipts in KG
// cannot be summed one way in the browser and another in the matcher.
//
// Loaded after flags.js (canEditField, FLAG_PCT) and before po-modal.js /
// import-po.js, which call it.

// Below these the difference is rounding, not a discrepancy. Value uses the
// matcher's own Rs 1 epsilon (matching_core.VALUE_FLAG_EPSILON) so a card never
// shows a difference the flags beside it do not.
const RECON_QTY_EPS = 0.0005;
const RECON_RATE_EPS = 0.005;
const RECON_VALUE_EPS = 1;
// Relative, as a fraction: matching_core._LANDED_RATE_ROUNDING_PCT (0.01%).
const RECON_LANDED_RATE_REL_EPS = 0.0001;

function reconQty(v) {
  return v == null ? '-' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 });
}
// Exact rupees and paise, deliberately NOT formatInr(): that rounds to whole
// rupees and abbreviates at a lakh, which rounds away the exact comparison a
// reconciliation exists for (same reasoning as review.html's cards).
function reconMoney(v, decimals) {
  if (v == null) return '-';
  const d = decimals == null ? 2 : decimals;
  return '₹' + Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
}

// A difference cell: the signed real amount, its percentage of the ordered
// figure, and a class saying which way it went. `kind` picks the words -
// quantity and value go short/over, rate goes lower/higher.
function reconDiffHtml(ordered, received, kind, fmt, eps) {
  if (ordered == null || received == null) return '<td class="recon-diff">-</td>';
  const diff = received - ordered;
  if (Math.abs(diff) < eps) return '<td class="recon-diff diff-ok"><span class="recon-diff-val">matches</span></td>';
  const pct = ordered ? Math.abs(diff) / Math.abs(ordered) * 100 : null;
  const under = diff < 0;
  const word = kind === 'rate' ? (under ? 'lower' : 'higher') : (under ? 'short' : 'over');
  const cls = kind === 'rate' ? 'diff-rate' : (under ? 'diff-short' : 'diff-over');
  return '<td class="recon-diff ' + cls + '"><span class="recon-diff-val">' + (under ? '−' : '+') + fmt(Math.abs(diff)) + '</span>' +
    '<span class="recon-diff-note">' + (pct != null ? pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '% ' : '') + word + '</span></td>';
}

// Where the line stands, in words - the first thing a reader wants.
function reconStatus(line) {
  if (!line.matched) return { cls: 'recon-none', text: 'Not received yet' };
  const r = line.received;
  if (!r || !r.comparable || r.qty == null || !line.ordered.qty) return { cls: 'recon-units', text: 'Received - units differ' };
  const pct = r.qty / line.ordered.qty * 100;
  if (Math.abs(r.qty - line.ordered.qty) < RECON_QTY_EPS) return { cls: 'recon-full', text: 'Fully received', pct: 100 };
  if (pct < 100) return { cls: 'recon-part', text: 'Partly received · ' + pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '%', pct };
  return { cls: 'recon-over', text: 'Over-received · ' + pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '%', pct };
}

// Confidence, manual-pin tag, "change" link and dismiss/reinstate - the
// controls matchStatusHtml() used to carry, minus the Δ% badges the table
// below now replaces with real numbers. Dismiss still appears only when the
// backend flagged something, exactly as before.
function reconControlsHtml(line, plantKey) {
  let out = '';
  if (line.matched) {
    const exactTier = line.tier === 'po_number' || line.tier === 'boe_number';
    const conf = exactTier ? 'high' : (line.score != null && line.score >= 0.75 ? 'medium' : 'low');
    const confTitle = { high: line.tier === 'boe_number' ? 'High confidence: MIR cites the Bill of Entry number of this shipment' : 'High confidence: exact PO number match', medium: 'Medium confidence: weighted score ≥ 0.75', low: 'Low confidence: weighted score below 0.75 - verify manually' }[conf] +
      (line.score != null ? ' (score ' + line.score.toFixed(2) + ')' : '');
    out += '<span class="conf-badge conf-' + conf + '" title="' + escapeHtml(confTitle) + '">' + conf + '</span>';
  }
  if (line.pinned) out += ' <span class="pinned-tag" title="This MIR match was set by hand and is not re-decided by the matcher.">manual</span>';
  if (line.flagged) {
    if (line.dismissed) {
      out += ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (line.dismissedBy ? ' by ' + line.dismissedBy : '') + (line.dismissedReason ? ': ' + line.dismissedReason : '')) + '">flags dismissed</span>';
      if (canEditField(plantKey)) out += ' <span class="dismiss-link" data-match-id="' + line.matchId + '" data-match-type="' + line.matchType + '"' + line.dismissPlantAttr + ' data-dismiss="false">reinstate</span>';
    } else if (canEditField(plantKey)) {
      out += ' <span class="dismiss-link" data-match-id="' + line.matchId + '" data-match-type="' + line.matchType + '"' + line.dismissPlantAttr + ' data-dismiss="true">dismiss flags</span>';
    }
  }
  if (canEditField(plantKey) && line.itemRef !== undefined) {
    out += ' <span class="mir-change-link" role="button" tabindex="0"' +
      ' data-item-ref="' + escapeHtml(String(line.itemRef)) + '"' +
      ' data-description="' + escapeHtml(line.description || '') + '"' +
      ' data-current-mir="' + escapeHtml(line.currentMirNo || '') + '"' +
      ' data-pinned="' + (line.pinned ? '1' : '0') + '">change MIR</span>';
  }
  return out;
}

function reconReceiptsHtml(line) {
  const mirs = line.mirs || [];
  if (!mirs.length) return '';
  const r = line.received || {};
  // A receipt booked in a different unit shows its own qty AND its qty in the
  // PO's unit, so the total row is visibly the sum of the second column.
  const unitsDiffer = mirs.some(m => (m.uom || '').trim().toLowerCase() !== (line.uom || '').trim().toLowerCase() && m.qtyInPoUnit != null && m.qtyInPoUnit !== m.qty);
  // "Sheet row" is the receipt's row in the MIR Excel file (2026-09-24), so
  // a reader reconciling by hand can go straight to it.
  const head = '<tr><th>MIR No.</th><th class="num" title="Row number in the MIR Excel sheet">Sheet row</th><th>Date</th><th>Invoice</th><th class="num">Qty</th>' +
    (unitsDiffer ? '<th class="num">Qty (' + escapeHtml(line.uom || 'PO unit') + ')</th>' : '') +
    '<th class="num">Rate</th><th class="num">Value</th></tr>';
  const rows = mirs.map(m => '<tr><td class="mono fw-700">' + escapeHtml(m.mirNo || '-') + '</td>' +
    '<td class="num mono">' + (m.sheetRow != null ? escapeHtml(String(m.sheetRow)) : '-') + '</td>' +
    '<td>' + escapeHtml(formatDateIN(m.mirDate)) + '</td>' +
    '<td>' + escapeHtml(m.invoiceNo || '-') + '</td>' +
    '<td class="num">' + reconQty(m.qty) + ' <span class="recon-unit">' + escapeHtml(m.uom || '') + '</span></td>' +
    (unitsDiffer ? '<td class="num">' + reconQty(m.qtyInPoUnit) + '</td>' : '') +
    '<td class="num">' + reconMoney(m.rate) + '</td>' +
    '<td class="num">' + reconMoney(m.value) + '</td></tr>').join('');
  const foot = mirs.length > 1
    ? '<tfoot><tr><td colspan="4">Total of ' + mirs.length + ' receipts</td>' +
      '<td class="num">' + (unitsDiffer ? '' : reconQty(r.qty) + ' <span class="recon-unit">' + escapeHtml(line.uom || '') + '</span>') + '</td>' +
      (unitsDiffer ? '<td class="num">' + reconQty(r.qty) + '</td>' : '') +
      '<td class="num">' + reconMoney(r.rate) + '</td><td class="num">' + reconMoney(r.value) + '</td></tr></tfoot>'
    : '';
  // Open by default up to 6 receipts - the answer to "which MIRs?" should
  // not need a click - and folded beyond that so a long blanket order does
  // not bury the next line.
  return '<details class="recon-receipts"' + (mirs.length <= 6 ? ' open' : '') + '>' +
    '<summary>' + mirs.length + ' MIR receipt' + (mirs.length === 1 ? '' : 's') + ' matched</summary>' +
    '<div class="recon-receipts-scroll"><table class="recon-mini">' + '<thead>' + head + '</thead><tbody>' + rows + '</tbody>' + foot + '</table></div>' +
  '</details>';
}

function reconLineHtml(line, plantKey) {
  const st = reconStatus(line);
  const o = line.ordered;
  const r = line.received && line.matched ? line.received : null;
  const qtyUnit = ' <span class="recon-unit">' + escapeHtml(line.uom || '') + '</span>';
  const rQty = r && r.comparable ? r.qty : null;
  const rows =
    '<tr><th scope="row">Quantity</th><td class="num">' + reconQty(o.qty) + qtyUnit + '</td>' +
      '<td class="num">' + (r ? (rQty != null ? reconQty(rQty) + qtyUnit : '<span class="recon-muted">see receipts</span>') : '<span class="recon-muted">0</span>') + '</td>' +
      (r ? reconDiffHtml(o.qty, rQty, 'qty', v => reconQty(v) + ' ' + (line.uom || ''), RECON_QTY_EPS) : '<td class="recon-diff diff-short"><span class="recon-diff-val">' + reconQty(o.qty) + ' ' + escapeHtml(line.uom || '') + '</span><span class="recon-diff-note">pending</span></td>') + '</tr>' +
    '<tr><th scope="row">Rate' + (line.rateNote ? ' <span class="recon-muted">' + escapeHtml(line.rateNote) + '</span>' : '') + '</th><td class="num">' + reconMoney(o.rate) + '</td>' +
      '<td class="num">' + (r ? reconMoney(r.rate) : '-') + '</td>' +
      (r ? reconDiffHtml(o.rate, r.rate, 'rate', v => reconMoney(v), RECON_RATE_EPS) : '<td class="recon-diff">-</td>') + '</tr>' +
    (line.landed ?
      '<tr><th scope="row">Landed rate <span class="recon-muted">(duty + IGST, per ' + escapeHtml(line.uom || 'unit') + ')</span></th><td class="num">' + reconMoney(line.landed.ordered) + '</td>' +
        '<td class="num">' + (r ? reconMoney(line.landed.received) : '-') + '</td>' +
        // Same 0.01% rounding allowance the matcher uses for this basis
        // (matching_core._LANDED_RATE_ROUNDING_PCT), so the card never shows a
        // difference the rate flag ignored.
        (r ? reconDiffHtml(line.landed.ordered, line.landed.received, 'rate', v => reconMoney(v), Math.abs(line.landed.ordered) * RECON_LANDED_RATE_REL_EPS) : '<td class="recon-diff">-</td>') + '</tr>'
      : '') +
    '<tr><th scope="row">Value' + (line.valueNote ? ' <span class="recon-muted">' + escapeHtml(line.valueNote) + '</span>' : '') + '</th><td class="num">' + reconMoney(o.value) + '</td>' +
      '<td class="num">' + (r ? reconMoney(r.value) : '-') + '</td>' +
      (r ? reconDiffHtml(o.value, r.value, 'value', v => reconMoney(v), RECON_VALUE_EPS) : '<td class="recon-diff">-</td>') + '</tr>';
  const chips = (line.chips || []).filter(c => c.value).map(c =>
    '<span class="recon-chip"><span class="recon-chip-k">' + escapeHtml(c.label) + '</span> ' + escapeHtml(c.value) + '</span>').join('');
  const barPct = st.pct != null ? Math.min(100, st.pct) : (line.matched ? 100 : 0);
  return '<article class="recon-line ' + st.cls + '">' +
    '<header class="recon-head">' +
      '<div class="recon-title"><span class="recon-idx">' + line.index + '</span>' +
        '<div><div class="recon-desc">' + escapeHtml(line.description || 'No description') + '</div>' +
        (chips ? '<div class="recon-chips">' + chips + '</div>' : '') + '</div></div>' +
      '<div class="recon-status"><span class="recon-pill">' + escapeHtml(st.text) + '</span>' + reconControlsHtml(line, plantKey) + '</div>' +
    '</header>' +
    '<div class="recon-bar" role="img" aria-label="' + escapeHtml(st.text) + '"><div class="recon-bar-fill" data-width-pct="' + barPct.toFixed(1) + '"></div></div>' +
    // Its own horizontal scroll, so a narrow screen scrolls this table
    // rather than pushing the whole modal sideways.
    '<div class="recon-table-scroll"><table class="recon-table"><thead><tr><th scope="col"></th><th scope="col" class="num">Ordered (PO)</th><th scope="col" class="num">Received (MIR)</th><th scope="col" class="num">Difference</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
    (line.uomMismatch ? '<div class="recon-note">Units cannot be converted between the PO and MIR - compare the receipts below by hand.</div>' : '') +
    (line.notes || []).map(n => '<div class="recon-note">' + escapeHtml(n) + '</div>').join('') +
    reconReceiptsHtml(line) +
    (line.footerHtml ? '<footer class="recon-foot">' + line.footerHtml + '</footer>' : '') +
  '</article>';
}

// PO-level summary above the cards: how much of the order has landed.
function reconSummaryHtml(lines, currencyLabel) {
  const total = lines.length;
  const full = lines.filter(l => reconStatus(l).cls === 'recon-full').length;
  const receipts = lines.reduce((n, l) => n + (l.matched ? (l.mirs || []).length : 0), 0);
  const orderedValue = lines.reduce((s, l) => s + (l.ordered.value || 0), 0);
  const receivedOf = l => (l.matched && l.received && l.received.value != null ? l.received.value : 0);
  const receivedValue = lines.reduce((s, l) => s + receivedOf(l), 0);
  // Progress counts each line up to its own ordered value: one line
  // over-delivered three times over must not make an order whose other lines
  // received nothing read "100% received" (seen on Vapi 1000001517). The
  // real received total is still the headline figure.
  const fulfilled = lines.reduce((s, l) => s + Math.min(receivedOf(l), l.ordered.value || 0), 0);
  const pct = orderedValue ? fulfilled / orderedValue * 100 : 0;
  const tile = (k, v, sub) => '<div class="recon-tile"><div class="recon-tile-k">' + k + '</div><div class="recon-tile-v">' + v + '</div>' + (sub ? '<div class="recon-tile-sub">' + sub + '</div>' : '') + '</div>';
  return '<div class="recon-summary">' +
    tile('Lines fully received', full + ' <span class="recon-muted">of ' + total + '</span>') +
    tile('MIR receipts', String(receipts)) +
    tile('Received value' + (currencyLabel ? ' (' + escapeHtml(currencyLabel) + ')' : ''), reconMoney(receivedValue, 0),
      'of ' + reconMoney(orderedValue, 0) + ' ordered · ' + pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '% of the order fulfilled' +
      '<div class="recon-bar recon-bar-sm"><div class="recon-bar-fill" data-width-pct="' + Math.min(100, pct).toFixed(1) + '"></div></div>') +
  '</div>';
}

function reconItemsHtml(lines, plantKey, currencyLabel) {
  if (!lines.length) return '<div class="fs-12-5 text-slate-soft">No line items recorded.</div>';
  return reconSummaryHtml(lines, currencyLabel) + '<div class="recon-list">' + lines.map(l => reconLineHtml(l, plantKey)).join('') + '</div>';
}

// Whether the backend flagged anything on a match - the same four conditions
// matchStatusHtml() used to decide whether a dismiss link belongs there.
function reconAnyFlag(qtyDiffPct, rateDiffPct, uomMismatch, isFlagged) {
  const qty = qtyDiffPct != null && qtyDiffPct > FLAG_PCT;
  const rate = rateDiffPct != null && rateDiffPct > FLAG_PCT;
  return qty || rate || !!uomMismatch || !!isFlagged;
}

function domesticReconLine(it, index, po, plantKey) {
  return {
    index, description: it.description, uom: it.uom,
    chips: [{ label: 'Delivery', value: it.deliveryDate ? formatDateIN(it.deliveryDate) : '' }],
    ordered: { qty: it.qty, rate: it.netPrice, value: it.netValue != null ? it.netValue : (it.qty != null && it.netPrice != null ? it.qty * it.netPrice : null) },
    received: it.received, mirs: it.matchedMirs, matched: !!it.matched,
    tier: it.matchTier, score: it.matchScore, pinned: !!it.manuallyPinned, itemRef: it.itemRef,
    currentMirNo: it.matchedMirNo, uomMismatch: !!it.uomMismatch,
    flagged: reconAnyFlag(it.qtyDiffPct, it.rateDiffPct, it.uomMismatch, it.matchFlagged),
    dismissed: !!it.dismissedByOverride, dismissedBy: it.dismissedBy, dismissedReason: it.dismissedReason,
    matchId: it.matchId, matchType: 'po-mir', dismissPlantAttr: '',
    footerHtml: materialAnalysisLinkHtml(it.description, po.vendorName, plantKey) || '<span class="text-slate-soft">Not tracked in RM Stock</span>',
  };
}

function importReconLine(it, index, po, plantKey) {
  const m = it.mirMatch || {};
  const currency = po.currency || 'PO currency';
  const fxNote = it.netPrice != null && it.exchangeRate != null
    ? currency + ' ' + Number(it.netPrice).toLocaleString('en-IN', { maximumFractionDigits: 4 }) + ' × ' + it.exchangeRate
    : '';
  return {
    index, description: it.description, uom: it.uom,
    chips: [
      { label: 'Item', value: it.itemId || '' },
      { label: 'HSN', value: it.hsn || '' },
      { label: 'PO qty', value: it.qtyAsPerPo != null ? reconQty(it.qtyAsPerPo) + ' ' + (it.uom || '') : '' },
      // The customs-side check (PO against Bill of Entry), separate from MIR.
      { label: 'PO vs BOE', value: it.qtyDiscrepancy && it.qtyDiscrepancyPct != null
        ? (it.qtyDiscrepancyPct >= 0 ? '+' : '') + it.qtyDiscrepancyPct.toFixed(1) + '%' : '' },
      { label: 'BOE', value: it.boeNumber || '' },
    ],
    // The BOE quantity and the INR figures are what the matcher compares
    // against MIR (see matching_core's _import_matchable()), so the card
    // compares the same things - the PO quantity sits in a chip above.
    // Value is the PO's NET value in INR, not the landed total the matcher's
    // own value score uses: MIR's value is pre-tax, so landed-with-duty
    // against it read "15% short" on a line whose qty matched and whose rate
    // was 8% HIGHER (Vapi 1000001569). Net x exchange moves with qty x rate,
    // which is what a reconciliation needs.
    ordered: {
      qty: it.qtyAsPerBoe,
      rate: m.orderedRateInr != null ? m.orderedRateInr : null,
      value: it.netValue != null && it.exchangeRate != null ? it.netValue * it.exchangeRate : (m.orderedValueInr != null ? m.orderedValueInr : null),
    },
    rateNote: fxNote ? '(' + fxNote + ', before duty)' : '(in INR)',
    // Cleared lines only: the landed rate (duty + IGST in) against MIR's
    // final rate - the second basis the matcher's rate check accepts
    // (matching_core._landed_rate_diff()), shown so a line whose pre-duty
    // rate reads "8.25% higher" visibly agrees once duty is counted.
    // Not for a shared receipt: that is compared at the BOE's blended landed
    // rate, which this line's own figure is not - the note below says so.
    landed: m.landedRateInr != null && m.receiptShare == null ? { ordered: m.landedRateInr, received: m.receivedFinalRate } : null,
    notes: [
      m.receiptShare != null
        ? 'One MIR receipt covers several lines of this Bill of Entry; this line counts ' +
          (m.receiptShare * 100).toLocaleString('en-IN', { maximumFractionDigits: 1 }) +
          '% of it (its share of the BOE quantity), and the rate is checked at the blended rate of the BOE.'
        : '',
      m.exchangeRateMismatched && m.mirExchangeRate != null
        ? 'Exchange rate differs: MIR works at ' + m.mirExchangeRate.toFixed(2) + ', the Imports CSV records ' +
          it.exchangeRate + '. The price agrees at the rate MIR uses - correct the exchange rate on the CSV.'
        : '',
    ].filter(Boolean),
    valueNote: it.netValue != null && it.exchangeRate != null ? '(PO net value in INR)' : '(landed, INR)',
    received: m.received, mirs: m.matchedMirs, matched: !!it.mirMatch,
    tier: m.tier, score: m.matchScore, pinned: !!it.manuallyPinned, itemRef: it.itemRef,
    currentMirNo: (m.matchedMirs && m.matchedMirs[0] && m.matchedMirs[0].mirNo) || '',
    uomMismatch: !!m.uomMismatch,
    flagged: reconAnyFlag(m.qtyDiffPct, m.rateDiffPct, m.uomMismatch, m.isFlagged),
    dismissed: !!m.dismissedByOverride, dismissedBy: m.dismissedBy, dismissedReason: m.dismissedReason,
    matchId: m.matchId, matchType: 'import-po-mir', dismissPlantAttr: ' data-plant="' + escapeHtml(plantKey) + '"',
    footerHtml: materialAnalysisLinkHtml(it.description, po.vendorName, plantKey) || '<span class="text-slate-soft">Not tracked in RM Stock</span>',
  };
}
