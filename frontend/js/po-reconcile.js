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
// quantity and value go short/over, rate goes lower/higher. `tolerated`
// marks an over-receipt the matcher accepted inside the weighbridge
// allowance (flags.js's BULK_QTY_TOLERANCE_PCT): the real amount still
// shows, in the matched colour.
function reconDiffHtml(ordered, received, kind, fmt, eps, tolerated) {
  if (ordered == null || received == null) return '<td class="recon-diff">-</td>';
  const diff = received - ordered;
  if (Math.abs(diff) < eps) return '<td class="recon-diff diff-ok"><span class="recon-diff-val">matches</span></td>';
  const pct = ordered ? Math.abs(diff) / Math.abs(ordered) * 100 : null;
  const under = diff < 0;
  if (tolerated && !under) {
    return '<td class="recon-diff diff-ok"><span class="recon-diff-val">+' + fmt(diff) + '</span>' +
      '<span class="recon-diff-note">' + (pct != null ? pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '% ' : '') + 'over, within tolerance</span></td>';
  }
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
  // Madura fabric: a roll count that differs decides, whatever the weight.
  if (line.rolls && line.rolls.received != null && line.rolls.received !== line.rolls.ordered) {
    const rp = line.rolls.received / line.rolls.ordered * 100;
    const of = line.rolls.received + ' of ' + rollsText(line.rolls.ordered);
    return line.rolls.received < line.rolls.ordered
      ? { cls: 'recon-part', text: 'Partly received · ' + of, pct: rp }
      : { cls: 'recon-over', text: 'Over-received · ' + of, pct: rp };
  }
  if (Math.abs(r.qty - line.ordered.qty) < RECON_QTY_EPS) return { cls: 'recon-full', text: 'Fully received', pct: 100 };
  // Over, but inside the weighbridge allowance: matched, and says by how much.
  if (line.qtyWithinTolerance && pct > 100) return { cls: 'recon-full', text: 'Qty matched · +' + (pct - 100).toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '% within tolerance', pct };
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
  // a reader reconciling by hand can go straight to it. Every column but the
  // converted qty sorts (PO_RECEIPTS_SORT, one sort for all of a modal's
  // receipt tables, its bar above the cards).
  const num = ' class="num"';
  const headers = [
    { label: 'MIR No.', key: 'mirNo' },
    { label: 'Sheet row', key: 'sheetRow', attrs: num + ' title="Row number in the MIR Excel sheet"' },
    { label: 'Date', key: 'date' },
    { label: 'Invoice', key: 'invoice' },
    { label: 'Qty', key: 'qty', attrs: num },
  ].concat(unitsDiffer ? [{ label: 'Qty (' + (line.uom || 'PO unit') + ')', attrs: num }] : [])
    .concat([{ label: 'Rate', key: 'rate', attrs: num }, { label: 'Value', key: 'value', attrs: num }]);
  const rowHtml = (m, idx) => '<tr data-sort-row="' + idx + '"><td class="mono fw-700">' + escapeHtml(m.mirNo || '-') + '</td>' +
    '<td class="num mono">' + (m.sheetRow != null ? escapeHtml(String(m.sheetRow)) : '-') + '</td>' +
    '<td>' + escapeHtml(formatDateIN(m.mirDate)) + '</td>' +
    '<td>' + escapeHtml(m.invoiceNo || '-') + '</td>' +
    '<td class="num">' + reconQty(m.qty) + ' <span class="recon-unit">' + escapeHtml(m.uom || '') + '</span></td>' +
    (unitsDiffer ? '<td class="num">' + reconQty(m.qtyInPoUnit) + '</td>' : '') +
    '<td class="num">' + reconMoney(m.rate) + '</td>' +
    '<td class="num">' + reconMoney(m.value) + '</td></tr>';
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
    sortedTableHtml(PO_MODAL_SORT_TABLES, 'receipts-' + line.index, PO_RECEIPTS_SORT, mirs, rowHtml, headers,
      { wrapClass: 'recon-receipts-scroll', tableClass: 'recon-mini', tfoot: foot }) +
  '</details>';
}

// ── Sorting the Item & Stock tab (2026-09-28) ──
// Project owner: "add the same sorting to the PO modal tables too". The line
// cards sort through list-sort.js (PO_LINES_SORT: line number, material,
// category, sub category, delivery, status, received %, ordered qty and
// value, with Material / Category / Sub Category pickable), and every MIR
// receipts table under them through one shared PO_RECEIPTS_SORT. Both
// Domestic and Import PO modals render through reconItemsHtml(), so both get
// it. The cards and rows carry the "change MIR", dismiss and edit controls,
// so a sort MOVES them (resortReconLines() / list-sort.js's
// resortSortedTables()) rather than re-rendering. PO_MODAL_SORT_TABLES is
// the receipts tables' registry and PO_RECON_LINES the cards', both reset by
// every reconItemsHtml().
const PO_MODAL_SORT_TABLES = {};
let PO_RECON_LINES = [];

// Most urgent first: nothing received, partly received, units differ,
// over-received, fully received.
const RECON_STATUS_RANK = { 'recon-none': 0, 'recon-part': 1, 'recon-units': 2, 'recon-over': 3, 'recon-full': 4 };

const PO_LINES_SORT_COLUMNS = [
  { key: 'line', label: 'Line number', kind: 'num', dir: 'asc' },
  { key: 'material', label: 'Material', kind: 'text', dir: 'asc', pick: true, parent: 'subCategory' },
  { key: 'category', label: 'Category', kind: 'text', dir: 'asc', pick: true },
  { key: 'subCategory', label: 'Sub Category', kind: 'text', dir: 'asc', pick: true, parent: 'category' },
  { key: 'delivery', label: 'Delivery Date', kind: 'date', dir: 'asc' },
  { key: 'status', label: 'Receipt status', kind: 'rank', dir: 'asc' },
  { key: 'receivedPct', label: 'Received %', kind: 'num', dir: 'asc' },
  { key: 'orderedQty', label: 'Ordered qty', kind: 'num', dir: 'desc' },
  { key: 'orderedValue', label: 'Ordered value', kind: 'num', dir: 'desc' },
];

// The first is the default: line order, as the PO lists them.
const PO_LINES_BUILTIN_SORTS = [
  { id: 'builtin:line', name: 'Line order (default)', levels: [{ key: 'line', dir: 'asc' }] },
  { id: 'builtin:status', name: 'Least received first', levels: [{ key: 'status', dir: 'asc' }, { key: 'receivedPct', dir: 'asc' }] },
  { id: 'builtin:value', name: 'Highest ordered value first', levels: [{ key: 'orderedValue', dir: 'desc' }] },
  { id: 'builtin:delivery', name: 'Delivery Date (soonest first)', levels: [{ key: 'delivery', dir: 'asc' }] },
  { id: 'builtin:catSub', name: 'Category, then Sub Category, then Material', levels: [{ key: 'category', dir: 'asc' }, { key: 'subCategory', dir: 'asc' }, { key: 'material', dir: 'asc' }] },
  { id: 'builtin:material', name: 'Material (A to Z)', levels: [{ key: 'material', dir: 'asc' }] },
];

function reconLineSortValue(key, l) {
  switch (key) {
    case 'line': return l.index;
    case 'material': return l.description || null;
    case 'category': return l.category && l.category !== 'Uncategorized' ? l.category : null;
    case 'subCategory': return l.subCategory && l.subCategory !== 'Uncategorized' ? l.subCategory : null;
    case 'delivery': return l.deliveryDate || null;
    case 'status': return RECON_STATUS_RANK[reconStatus(l).cls];
    case 'receivedPct': { const st = reconStatus(l); return st.pct != null ? st.pct : (l.matched ? null : 0); }
    case 'orderedQty': return l.ordered.qty != null ? l.ordered.qty : null;
    case 'orderedValue': return l.ordered.value != null ? l.ordered.value : null;
    default: return null;
  }
}

const PO_LINES_SORT = createListSort({
  view: 'po_lines',
  idPrefix: 'poLines',
  barLabel: 'Sort lines by',
  columns: PO_LINES_SORT_COLUMNS,
  builtins: PO_LINES_BUILTIN_SORTS,
  rowValue: reconLineSortValue,
  tieBreak: (a, b) => a.index - b.index,
  rerender: () => resortReconLines(document.getElementById('modalBody')),
});

const PO_RECEIPTS_SORT_COLUMNS = [
  { key: 'date', label: 'Date', kind: 'date', dir: 'asc' },
  { key: 'mirNo', label: 'MIR No.', kind: 'text', dir: 'asc' },
  { key: 'sheetRow', label: 'Sheet row', kind: 'num', dir: 'asc' },
  { key: 'invoice', label: 'Invoice', kind: 'text', dir: 'asc' },
  { key: 'qty', label: 'Qty', kind: 'num', dir: 'desc' },
  { key: 'rate', label: 'Rate', kind: 'num', dir: 'asc' },
  { key: 'value', label: 'Value', kind: 'num', dir: 'desc' },
];

// The first is the default: the order the backend lists them in
// (_domestic_base._counted_mirs(): oldest first, then MIR number).
const PO_RECEIPTS_BUILTIN_SORTS = [
  { id: 'builtin:date', name: 'Oldest receipt first (default)', levels: [{ key: 'date', dir: 'asc' }, { key: 'mirNo', dir: 'asc' }] },
  { id: 'builtin:newest', name: 'Newest receipt first', levels: [{ key: 'date', dir: 'desc' }, { key: 'mirNo', dir: 'desc' }] },
  { id: 'builtin:sheet', name: 'Sheet order', levels: [{ key: 'sheetRow', dir: 'asc' }] },
  { id: 'builtin:qty', name: 'Largest quantity first', levels: [{ key: 'qty', dir: 'desc' }] },
  { id: 'builtin:rate', name: 'Highest rate first', levels: [{ key: 'rate', dir: 'desc' }] },
];

// `m` is one of a line's matchedMirs. Quantity compares in the PO line's
// unit where the backend converted it, so receipts in KG and MT line up.
function reconReceiptSortValue(key, m) {
  switch (key) {
    case 'date': return m.mirDate || null;
    case 'mirNo': return m.mirNo || null;
    case 'sheetRow': return m.sheetRow != null ? m.sheetRow : null;
    case 'invoice': return m.invoiceNo || null;
    case 'qty': return m.qtyInPoUnit != null ? m.qtyInPoUnit : (m.qty != null ? m.qty : null);
    case 'rate': return m.rate != null ? m.rate : null;
    case 'value': return m.value != null ? m.value : null;
    default: return null;
  }
}

const PO_RECEIPTS_SORT = createListSort({
  view: 'po_receipts',
  idPrefix: 'poReceipts',
  barLabel: 'Sort MIR receipts by',
  columns: PO_RECEIPTS_SORT_COLUMNS,
  builtins: PO_RECEIPTS_BUILTIN_SORTS,
  rowValue: reconReceiptSortValue,
  tieBreak: (a, b) => String(a.mirNo || '').localeCompare(String(b.mirNo || ''), 'en', { numeric: true }),
  rerender: () => resortSortedTables(PO_MODAL_SORT_TABLES, PO_RECEIPTS_SORT, document.getElementById('modalBody')),
});

// The line cards' rerender: move each card into the new order, hide what
// "Show only these" drops, redraw the lines bar, re-wire its controls.
function resortReconLines(root) {
  if (!root) return;
  const list = root.querySelector('.recon-list');
  if (!list) return;
  const byIndex = new Map();
  list.querySelectorAll(':scope > [data-sort-row]').forEach(card => byIndex.set(Number(card.dataset.sortRow), card));
  const sorted = PO_LINES_SORT.sortRows(PO_RECON_LINES);
  const kept = new Set(sorted);
  sorted.concat(PO_RECON_LINES.filter(l => !kept.has(l))).forEach(l => {
    const card = byIndex.get(l.index);
    if (!card) return;
    card.hidden = !kept.has(l);
    list.appendChild(card);
  });
  root.querySelectorAll('[data-sort-bar="' + PO_LINES_SORT.prefix + '"]').forEach(bar => { bar.innerHTML = PO_LINES_SORT.barHtml(); });
  PO_LINES_SORT.wire(root);
}

// Called by both PO modals after their body lands: binds the two sorters'
// controls (each binds only its own - list-sort.js's data-sorter).
function wireReconSorting(body) {
  PO_LINES_SORT.wire(body);
  PO_RECEIPTS_SORT.wire(body);
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
      (r ? reconDiffHtml(o.qty, rQty, 'qty', v => reconQty(v) + ' ' + (line.uom || ''), RECON_QTY_EPS, line.qtyWithinTolerance) : '<td class="recon-diff diff-short"><span class="recon-diff-val">' + reconQty(o.qty) + ' ' + escapeHtml(line.uom || '') + '</span><span class="recon-diff-note">pending</span></td>') + '</tr>' +
    '<tr><th scope="row">Rate' + (line.rateNote ? ' <span class="recon-muted">' + escapeHtml(line.rateNote) + '</span>' : '') + '</th><td class="num">' + reconMoney(o.rate) + '</td>' +
      '<td class="num">' + (r ? reconMoney(r.rate) : '-') + '</td>' +
      (r ? reconDiffHtml(o.rate, r.rate, 'rate', v => reconMoney(v), RECON_RATE_EPS) : '<td class="recon-diff">-</td>') + '</tr>' +
    (line.rolls ?
      '<tr><th scope="row">Rolls</th><td class="num">' + line.rolls.ordered + '</td>' +
        '<td class="num">' + (r ? (line.rolls.received != null ? line.rolls.received : '<span class="recon-muted">not stated in MIR</span>') : '<span class="recon-muted">0</span>') + '</td>' +
        (r && line.rolls.received != null ? reconDiffHtml(line.rolls.ordered, line.rolls.received, 'qty', v => rollsText(v), 0.5) : '<td class="recon-diff">-</td>') + '</tr>'
      : '') +
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
      (r ? reconDiffHtml(o.value, r.value, 'value', v => reconMoney(v), RECON_VALUE_EPS, line.valueWithinTolerance) : '<td class="recon-diff">-</td>') + '</tr>';
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
  // Received in full OR more: the same rule the list's "Material Inwarded"
  // card applies. Counting exact quantities only read "0 of 3" on POs that
  // card called received, because their lines were over-received; the over
  // ones are named instead of silently dropped (2026-09-25).
  const full = lines.filter(l => reconStatus(l).cls === 'recon-full').length;
  const over = lines.filter(l => reconStatus(l).cls === 'recon-over').length;
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
    tile('Lines fully received', (full + over) + ' <span class="recon-muted">of ' + total + '</span>',
      over ? over + ' of them over-received' : '') +
    tile('MIR receipts', String(receipts)) +
    tile('Received value' + (currencyLabel ? ' (' + escapeHtml(currencyLabel) + ')' : ''), reconMoney(receivedValue, 0),
      'of ' + reconMoney(orderedValue, 0) + ' ordered · ' + pct.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '% of the order fulfilled' +
      '<div class="recon-bar recon-bar-sm"><div class="recon-bar-fill" data-width-pct="' + Math.min(100, pct).toFixed(1) + '"></div></div>') +
  '</div>';
}

// The Item & Stock tab: summary, the two sort bars, then the line cards in
// PO_LINES_SORT's order (each tagged data-sort-row with its line number; a
// card "Show only these" drops is rendered hidden). The cards are built
// before the bars, since sorting records the rows the bars' value pickers
// offer. The caller wires the sorting with wireReconSorting(body).
function reconItemsHtml(lines, plantKey, currencyLabel) {
  Object.keys(PO_MODAL_SORT_TABLES).forEach(k => { delete PO_MODAL_SORT_TABLES[k]; });
  PO_RECON_LINES = lines;
  if (!lines.length) return '<div class="fs-12-5 text-slate-soft">No line items recorded.</div>';
  const sorted = PO_LINES_SORT.sortRows(lines);
  const kept = new Set(sorted);
  const card = (l, hidden) => reconLineHtml(l, plantKey).replace(/^<article/, '<article data-sort-row="' + l.index + '"' + (hidden ? ' hidden' : ''));
  const cards = sorted.map(l => card(l, false)).join('') + lines.filter(l => !kept.has(l)).map(l => card(l, true)).join('');
  const anyReceipts = lines.some(l => (l.mirs || []).length);
  return reconSummaryHtml(lines, currencyLabel) +
    '<div class="recon-sort-bars">' + sortedTableBarHtml(PO_LINES_SORT) + (anyReceipts ? sortedTableBarHtml(PO_RECEIPTS_SORT) : '') + '</div>' +
    '<div class="recon-list">' + cards + '</div>';
}

// Whether the backend flagged anything on a match - the same four conditions
// matchStatusHtml() used to decide whether a dismiss link belongs there.
// `m` carries qtyDiffPct / qtyWithinTolerance / rateDiffPct / uomMismatch.
function reconAnyFlag(m, isFlagged) {
  const rate = m.rateDiffPct != null && m.rateDiffPct > FLAG_PCT;
  return isQtyMismatch(m) || rate || !!m.uomMismatch || !!isFlagged;
}

// The weight-tolerance and roll-count fields both adapters hand the
// renderer. `rolls` only for a line whose PO states a count (Madura fabric).
function reconToleranceFields(m) {
  const qtyWithinTolerance = !!m.qtyWithinTolerance;
  return {
    qtyWithinTolerance, valueWithinTolerance: qtyWithinTolerance && !m.netValueMismatched,
    rolls: m.rollsOrdered != null ? { ordered: m.rollsOrdered, received: m.rollsReceived } : null,
  };
}

// A line counting only part of one MIR receipt. BOE settlement splits a
// receipt across its Bill of Entry's lines (tier 'boe_number'); a "Keep
// both" manual match splits one across whichever lines hold it. Both by
// ordered quantity.
// A pooled line (`poolRefs`, matching_core._pool_duplicate_lines()) is one
// of the order's identical lines - same material, rate and unit - which
// share every receipt booked against them: by ordered quantity once the
// order has arrived in full, filling the lines in order while it is partly
// delivered.
function receiptShareNote(share, tier, poolRefs) {
  if (share == null) return '';
  const pct = (share * 100).toLocaleString('en-IN', { maximumFractionDigits: 1 }) + '%';
  if (poolRefs) {
    return 'Lines ' + poolRefs + ' of this order are the same material at the same rate, so their MIR receipts are ' +
      'counted together and shared between them - by ordered quantity once the order has fully arrived, filling ' +
      'the lines in order while it is partly delivered. This line counts ' + pct + ' of those receipts.';
  }
  return tier === 'boe_number'
    ? 'One MIR receipt covers several lines of this Bill of Entry; this line counts ' + pct +
      ' of it (its share of the BOE quantity), and the rate is checked at the blended rate of the BOE.'
    : 'This MIR receipt is shared with another line (a "Keep both" manual match); this line counts ' + pct +
      ' of it, its share of the ordered quantity.';
}

function domesticReconLine(it, index, po, plantKey) {
  return {
    index, description: it.description, uom: it.uom,
    // For sorting the cards (PO_LINES_SORT) - not shown as figures.
    deliveryDate: it.deliveryDate || null, category: it.category || '', subCategory: it.subCategory || '',
    chips: [{ label: 'Delivery', value: it.deliveryDate ? formatDateIN(it.deliveryDate) : '' }],
    ordered: { qty: it.qty, rate: it.netPrice, value: it.netValue != null ? it.netValue : (it.qty != null && it.netPrice != null ? it.qty * it.netPrice : null) },
    notes: [qtyToleranceNote(it), receiptShareNote(it.receiptShare, it.matchTier, it.poolLineRefs)].filter(Boolean),
    ...reconToleranceFields(it),
    received: it.received, mirs: it.matchedMirs, matched: !!it.matched,
    tier: it.matchTier, score: it.matchScore, pinned: !!it.manuallyPinned, itemRef: it.itemRef,
    currentMirNo: it.matchedMirNo, uomMismatch: !!it.uomMismatch,
    flagged: reconAnyFlag(it, it.matchFlagged),
    dismissed: !!it.dismissedByOverride, dismissedBy: it.dismissedBy, dismissedReason: it.dismissedReason,
    matchId: it.matchId, matchType: 'po-mir', dismissPlantAttr: '',
    footerHtml: materialAnalysisLinkHtml(it.description, po.vendorName, plantKey) || '<span class="text-slate-soft">Not tracked in RM Stock</span>',
  };
}

function importReconLine(it, index, po, plantKey) {
  const m = it.mirMatch || {};
  const currency = po.currency || 'PO currency';
  const noFx = it.netPrice != null && it.exchangeRate == null;
  const fxNote = it.netPrice != null && it.exchangeRate != null
    ? currency + ' ' + Number(it.netPrice).toLocaleString('en-IN', { maximumFractionDigits: 4 }) + ' × ' + it.exchangeRate
    : '';
  return {
    index, description: it.description, uom: it.uom,
    // For sorting the cards (PO_LINES_SORT) - not shown as figures.
    deliveryDate: it.deliveryDate || null, category: it.category || '', subCategory: it.subCategory || '',
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
    // No exchange rate on file: the line's price is in the PO currency and
    // there is no honest INR figure to set against MIR's, so the rate and
    // value rows show nothing to compare. They used to show the bare USD
    // price labelled "(in INR)", reading about 9,500% "higher" (1000001508,
    // 1000001318 - 2026-09-25).
    ordered: {
      qty: it.qtyAsPerBoe,
      rate: noFx ? null : (m.orderedRateInr != null ? m.orderedRateInr : null),
      // What cleared (net price x BOE qty), the matcher's own value basis
      // (matching_core._import_rate_value_inr()).
      value: noFx ? null : (it.netPrice != null && it.qtyAsPerBoe != null ? it.netPrice * it.qtyAsPerBoe * it.exchangeRate
        : (m.orderedValueInr != null ? m.orderedValueInr : null)),
    },
    rateNote: noFx ? '(no exchange rate on file)' : (fxNote ? '(' + fxNote + ', before duty)' : '(in INR)'),
    // Cleared lines only: the landed rate (duty + IGST in) against MIR's
    // final rate - the second basis the matcher's rate check accepts
    // (matching_core._landed_rate_diff()), shown so a line whose pre-duty
    // rate reads "8.25% higher" visibly agrees once duty is counted.
    // Not for a shared receipt: that is compared at the BOE's blended landed
    // rate, which this line's own figure is not - the note below says so.
    landed: m.landedRateInr != null && m.receiptShare == null ? { ordered: m.landedRateInr, received: m.receivedFinalRate } : null,
    ...reconToleranceFields(m),
    notes: [
      qtyToleranceNote(m),
      noFx ? 'This line has no exchange rate in the Imports CSV, so its ' + (po.currency || 'PO currency') + ' price cannot be compared with MIR in INR. Fill in the exchange rate to compare it.' : '',
      receiptShareNote(m.receiptShare, m.tier, m.poolLineRefs),
      m.exchangeRateMismatched && m.mirExchangeRate != null
        ? 'Exchange rate differs: MIR works at ' + m.mirExchangeRate.toFixed(2) + ', the Imports CSV records ' +
          it.exchangeRate + '. The price agrees at the rate MIR uses - correct the exchange rate on the CSV.'
        : '',
    ].filter(Boolean),
    valueNote: noFx ? '(no exchange rate on file)' : '(value of what cleared, INR, before duty)',
    received: m.received, mirs: m.matchedMirs, matched: !!it.mirMatch,
    tier: m.tier, score: m.matchScore, pinned: !!it.manuallyPinned, itemRef: it.itemRef,
    currentMirNo: (m.matchedMirs && m.matchedMirs[0] && m.matchedMirs[0].mirNo) || '',
    uomMismatch: !!m.uomMismatch,
    flagged: reconAnyFlag(m, m.isFlagged),
    dismissed: !!m.dismissedByOverride, dismissedBy: m.dismissedBy, dismissedReason: m.dismissedReason,
    matchId: m.matchId, matchType: 'import-po-mir', dismissPlantAttr: ' data-plant="' + escapeHtml(plantKey) + '"',
    footerHtml: materialAnalysisLinkHtml(it.description, po.vendorName, plantKey) || '<span class="text-slate-soft">Not tracked in RM Stock</span>',
  };
}
