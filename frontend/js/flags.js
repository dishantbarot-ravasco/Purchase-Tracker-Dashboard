/**
 * frontend/js/flags.js - PO status, match-confidence badges, and Data
 * Quality Flag rendering shared across po-list.js, po-modal.js,
 * import-po.js, and material-modal.js. Split out of main.js - see that
 * file's own header comment for the module map.
 */

// FLAG_PCT mirrors apps/services/matching*.py's FLAG_DIFF_PCT - kept in
// sync manually since the frontend only receives the already-computed diff
// percentages, not the threshold itself. Zero tolerance (2026-09-04,
// project owner, superseding the earlier "5%, deliberately not the
// artifact's 10%" decision) - even a 1kg-out-of-1000kg (0.1%) qty diff, or
// the equivalent for rate/value, now counts as a discrepancy. Since every
// comparison below uses strict `>`, an exact match (diff === 0) still never
// flags - only a real, nonzero difference does.
const FLAG_PCT = 0;

// Colored flag icon for the KPI row - a small monochrome SVG (Material
// Design's "flag" glyph) recolored via `fill` so every KPI card carries a
// color-coded symbol instead of plain text. Per the project owner's
// 2026-09-04 request every status card gets one (not just the discrepancy
// cards), each matching that status's own established color elsewhere in
// the app (status pills, .kpi-card.<status> border colors) - and per the
// 2026-09-04 follow-up, Overdue moved out of the amber "delivery-timing"
// group into the same red as the qty/rate discrepancy cards, since an
// overdue PO is treated as urgent/critical, not just a scheduling note.
const KPI_FLAG_COLORS = {
  critical: '#dc2626', // qty/rate discrepancies + Overdue
  received: '#16a34a',
  partial: '#2563eb',
  pending: '#d97706',
  unknown: '#64748b',
  quality: '#7c3aed', // Data Quality Flags
};
// `cls` defaults to 'kpi-flag-icon' (absolutely positioned in a KPI card's
// top-right corner). Row-level flags (see rowFlags() in renderPoList) pass
// 'row-flag-icon' instead - a plain inline icon, not absolutely positioned,
// since it sits inline next to a status pill rather than alone in a card.
function flagIconHtml(hexColor, cls) {
  return '<svg class="' + (cls || 'kpi-flag-icon') + '" width="15" height="15" viewBox="0 0 24 24" fill="' + hexColor + '" aria-hidden="true">' +
    '<path d="M14.4 6L14 4H5v17h2v-7h5.6l.4 2h7V6h-4.6z"/></svg>';
}

// ── Row flags: four buckets, one icon each ──────────────────────────────
// Project owner, 2026-09-22: "keeping 1-4 flags aside of status seem too
// much - I was thinking of keeping only red for any mismatches, blue for
// partial delivery, yellow for on order and purple for all data quality
// issues."
//
// What it replaces: each list view rendered ONE ICON PER CRITICAL CATEGORY.
// All five critical categories are `#dc2626` in CATEGORY_COLORS, so a PO
// with a qty AND a rate mismatch showed two identical red flags, and one
// that was also over-delivered showed three - repetition that carried no
// information the tooltip did not already hold. Info-severity categories,
// meanwhile, had no row icon at all (2026-09-10), so a paperwork problem was
// invisible until you opened the PO.
//
// The four buckets are deliberately the colours this app ALREADY uses for
// these meanings, not new ones: KPI_FLAG_COLORS.partial/pending/critical/
// quality are the same values the KPI cards and the status pills use
// (.status-partial is blue, .status-pending amber, .status-overdue red).
//
// NOTHING IS LOST BY COLLAPSING - the specific category names move into the
// tooltip, which is where a reader looked for them anyway. Keep it that way
// if you add a bucket: an icon that cannot say WHICH flag it stands for is
// strictly worse than the list it replaced.
//
// Overdue is deliberately NOT a bucket. It is an overlay, not a delivery
// state (see computeStatus()), and the status pill beside these icons
// already turns red for it - a fifth flag would say it twice.
const ROW_FLAG_BUCKETS = {
  partial: { color: KPI_FLAG_COLORS.partial, label: 'Partial delivery' },
  onorder: { color: KPI_FLAG_COLORS.pending, label: 'On order' },
  mismatch: { color: KPI_FLAG_COLORS.critical, label: 'Mismatch' },
  quality: { color: KPI_FLAG_COLORS.quality, label: 'Data quality' },
};

function _rowFlagHtml(bucket, detail) {
  const tip = detail && detail.length ? bucket.label + ': ' + detail.join('; ') : bucket.label;
  // data-tooltip + CSS (.row-flag-wrap::after, see style.css) instead of a
  // native title attribute - title tooltips have a ~1s hover delay and are
  // easy to dismiss with the slightest mouse movement, which read as
  // "hovering isn't working" (project owner, 2026-09-04). The CSS tooltip
  // shows immediately and reliably instead.
  return ' <span class="row-flag-wrap" data-tooltip="' + escapeHtml(tip) + '">' +
    flagIconHtml(bucket.color, 'row-flag-icon') + '</span>';
}

/**
 * The flags shown beside a row's status pill, in every list view.
 * @param {object} opts
 *   partial    - something arrived but the order is not complete
 *   onOrder    - nothing has arrived yet and it is not yet overdue
 *   categories - this row's flag categories ({label, severity}), the same
 *                shape computePoFlags()/importCategoriesFor()/
 *                computeMaterialPoLinkage() already produce
 *
 * Order matches the four buckets as listed above: delivery state, then
 * mismatch, then data quality. `partial` wins over `onOrder` when a caller
 * can report both (Import can - see importRowFlags()) because it is the more
 * specific fact: part of it HAS arrived.
 */
function rowFlagsHtml(opts) {
  opts = opts || {};
  const cats = opts.categories || [];
  // "PO Not Found in MIR" is NOT a mismatch on an order that is not due yet -
  // it is the same fact the yellow flag already states, and it fires on
  // EVERY such order by construction (computePoFlags() raises it whenever no
  // line item has matched; CLAUDE.md's Data Quality Flags section notes the
  // same density effect for Import and Materials). Left in the red bucket it
  // put a red flag on almost every on-order row, which is how a colour stops
  // meaning anything - measured while building this: a perfectly ordinary
  // not-yet-due PO carries exactly this one critical category and nothing
  // else.
  //
  // Suppressed ONLY while on order. An OVERDUE row gets no delivery flag at
  // all (the pill is already red), so there the same category is real news -
  // it should have arrived and there is no receipt - and it still counts.
  const suppressed = opts.onOrder && !opts.partial ? 'PO Not Found in MIR' : null;
  const critical = cats
    .filter(c => c.severity === 'critical' && c.label !== suppressed)
    .map(c => c.label);
  const info = cats.filter(c => c.severity !== 'critical').map(c => c.label);
  let out = '';
  if (opts.partial) out += _rowFlagHtml(ROW_FLAG_BUCKETS.partial);
  else if (opts.onOrder) out += _rowFlagHtml(ROW_FLAG_BUCKETS.onorder);
  if (critical.length) out += _rowFlagHtml(ROW_FLAG_BUCKETS.mismatch, critical);
  if (info.length) out += _rowFlagHtml(ROW_FLAG_BUCKETS.quality, info);
  return out;
}

/** The colour key shown above the Data Quality legend - the row flags are
 * their own four-colour vocabulary now, distinct from the legend's
 * per-category dots below it (which stay one hue per category, so a reader
 * can still tell two categories apart there). */
function rowFlagKeyHtml() {
  const item = (bucket, meaning) =>
    '<span class="flag-key-item">' + flagIconHtml(bucket.color, 'row-flag-icon') +
    '<b>' + escapeHtml(bucket.label) + '</b> ' + escapeHtml(meaning) + '</span>';
  return '<div class="flag-key">' +
    item(ROW_FLAG_BUCKETS.mismatch, 'qty, rate or a receipt that could not be found') +
    item(ROW_FLAG_BUCKETS.partial, 'some of the order has arrived') +
    item(ROW_FLAG_BUCKETS.onorder, 'nothing has arrived yet') +
    item(ROW_FLAG_BUCKETS.quality, 'paperwork and data problems') +
  '</div>';
}

// The PO modals' per-line MIR badge (matchStatusHtml / importMatchStatusHtml,
// with its qty/rate/value "Δ%" badges) was replaced on 2026-09-24 by the
// reconciliation cards in po-reconcile.js, which show the real Ordered /
// Received / Difference figures and every matched MIR receipt instead. The
// confidence badge and dismiss link moved there (reconControlsHtml()).

// MIR<->Stock match badges, for a Stock lot's
// `mirStockMatches` (apps/api/routers/*_views.py's _lot_dict()) - a lot can
// carry more than one MIR<->Stock pairing, so this renders one badge per
// flagged match rather than a single blended one.
function mirStockMatchHtml(lot, plantKey) {
  const matches = lot.mirStockMatches || [];
  const flagged = matches.filter(m => m.isFlagged);
  // A unit clash (MIR in MT, stock in KG, and no conversion between their
  // families) makes the matcher SKIP the qty/rate comparison and store
  // is_flagged=False - see match_mir_entry_stock()'s uom_mismatch branch in
  // matching_core.py. Until 2026-09-23 that fell through to the green
  // "matched" badge below, telling the reader a pair had reconciled when its
  // figures were never compared at all. It gets its own amber badge now.
  // CLAUDE.md listed this as a known gap ("uom_mismatch on MIR<->Stock
  // matches has no frontend badge yet").
  const uomBadge = matches.some(m => m.uomMismatch) ? uomMismatchBadgeHtml(lot) : '';
  const compared = matches.filter(m => !m.uomMismatch);
  if (!flagged.length) {
    if (!matches.length) return '-';
    return (compared.length ? '<span class="conf-badge conf-high">matched</span>' : '') + uomBadge;
  }
  return flagged.map(m => {
    const dismissedCls = m.dismissedByOverride ? ' dismissed' : '';
    const parts = [];
    const titleParts = [];
    if (m.rateDiffPct != null) {
      parts.push('rate Δ' + m.rateDiffPct.toFixed(1) + '%');
      titleParts.push('Rate mismatch in RM: rate recorded in this material\'s Raw Material stock differs from the matched MIR entry\'s rate by ' + m.rateDiffPct.toFixed(2) + '% - verify manually');
    }
    if (m.qtyDiffPct != null) {
      parts.push('qty Δ' + m.qtyDiffPct.toFixed(1) + '%');
      titleParts.push('Quantity mismatch in RM: qty recorded in this material\'s Raw Material stock differs from the matched MIR entry\'s qty by ' + m.qtyDiffPct.toFixed(2) + '% - verify manually');
    }
    let badge = '<span class="flag-badge' + dismissedCls + '" title="' + escapeHtml(titleParts.join(' | ') || 'Flagged in RM: this material\'s Raw Material stock does not line up with its matched MIR entry - verify manually') + '">' + escapeHtml(parts.join(', ') || 'flagged') + '</span>';
    if (m.dismissedByOverride) {
      badge += ' <span class="dismissed-tag">dismissed</span>';
      if (canEditField(plantKey)) badge += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="mir-stock" data-plant="' + plantKey + '" data-dismiss="false">reinstate</span>';
    } else if (canEditField(plantKey)) {
      badge += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="mir-stock" data-plant="' + plantKey + '" data-dismiss="true">dismiss</span>';
    }
    return badge;
  }).join(' ') + uomBadge;
}

// The amber "units differ" badge for a MIR<->Stock pairing whose qty/rate
// could not be compared - see mirStockMatchHtml()'s comment. Deliberately
// NOT a flag (no dismiss link, not red): nothing is known to be wrong, the
// check simply could not run. Only the stock lot's own unit is on the
// payload, so the tooltip names that one.
function uomMismatchBadgeHtml(lot) {
  const unit = lot.uom ? ' (' + lot.uom + ')' : '';
  const tip = 'Units differ: the matched MIR entry records this material in a different unit from this stock lot' + unit +
    ', and the two cannot be converted, so quantity and rate were not compared. Check the unit in both files.';
  return ' <span class="conf-badge conf-medium" title="' + escapeHtml(tip) + '">units differ</span>';
}

// Wires every .dismiss-link under `container` (rendered by po-reconcile.js's reconControlsHtml()
// on each PO line card, or the equivalent MIR<->Stock badge in the materials modal, or
// poFlagHtml()'s PO-level Quantity/Rate-Value/Data-Quality flags below) to
// PATCH the right dismiss endpoint and then re-run `onDone` to refresh
// whatever view is showing the flag. A plain confirm()/prompt() for the
// optional reason - this app has no reusable modal-with-textarea component,
// and a full one felt like overkill for a single optional text field.
// data-match-type 'po-flag'/'import-po-flag' route to dismissPoFlag()
// (apps/services/flag_dismiss.py, keyed by an opaque flagKey string) instead
// of dismissMatch() (which needs a real match row's numeric id) - see
// poFlagHtml()'s own comment for why these two flag families needed a
// separate backend table in the first place.
function wireDismissLinks(container, plantKey, onDone) {
  container.querySelectorAll('.dismiss-link').forEach(el => {
    el.onclick = async () => {
      const matchType = el.dataset.matchType;
      // data-plant overrides the passed-in plantKey for a cross-plant table
      // (the material modal's Stock by Plant rows, each possibly a
      // different plant) - the PO modal's single-plant table never sets it,
      // so it falls back to the plantKey argument there.
      const rowPlantKey = el.dataset.plant || plantKey;
      const dismissing = el.dataset.dismiss === 'true';
      let reason = '';
      if (dismissing) {
        reason = window.prompt('Optional note for dismissing this flag (why is it fine to ignore?):', '') || '';
        if (reason === null) return;
      } else if (!window.confirm('Reinstate this flag? It will show as flagged again.')) {
        return;
      }
      const original = el.textContent;
      el.textContent = '…';
      try {
        if (matchType === 'po-flag' || matchType === 'import-po-flag') {
          await dismissPoFlag(rowPlantKey, el.dataset.poNumber, el.dataset.flagKey, dismissing, reason, matchType === 'import-po-flag');
        } else {
          await dismissMatch(rowPlantKey, matchType, el.dataset.matchId, dismissing, reason);
        }
        if (onDone) await onDone();
      } catch (e) {
        alert('Could not update this flag: ' + e.message);
        el.textContent = original;
      }
    };
  });
}

// ── Data Quality Flags: categorized from each PO's own `remarks` text ──
// Ported from the original "Purchase Tracker" Claude Artifact prototype's
// CATEGORY_RULES/categorizeFlag() (confirmed by reading its real source via
// the Artifact tool - see CLAUDE.md's "Artifact-parity decisions"), run
// against the same `remarks` field this app already parses verbatim from
// the source CSV (apps/services/parsers/po_csv.py) - no new data needed.
const FLAG_CATEGORY_RULES = [
  { label: 'Misfiled: wrong plant or company', test: t => /misfiled/i.test(t) },
  { label: 'Duplicate file or PO', test: t => /duplicate/i.test(t) },
  { label: 'Revision or superseded PO conflict', test: t => /(changed purchase order|rev\s?0?1|revision|superced|supersede)/i.test(t) },
  { label: 'Tax calculation or labeling mismatch', test: t => /(ugst|igst|cgst|sgst|tax rate|tax mislabel|tax section|inconsistent tax)/i.test(t) },
  { label: 'Header or template data error', test: t => /(header|letterhead|template)/i.test(t) },
  { label: 'Vendor GSTIN anomaly', test: t => /gstin/i.test(t) },
  { label: 'Missing or blank field', test: t => /(blank|not captured|no tax breakdown|no incoterms|missing)/i.test(t) },
  { label: 'Import PO filed in Domestic folder', test: t => /(import po|is an import|belongs in.*import)/i.test(t) },
  { label: 'Delivery date anomaly', test: t => /(delivery date|equals created date|predates|passed)/i.test(t) },
  { label: 'Vendor code scheme inconsistency', test: t => /vendor code/i.test(t) },
  { label: 'Non raw material or different category', test: t => /(not raw material|different category|capital equipment|tooling)/i.test(t) },
];
// Renders one PO-level flag (a Quantity/Rate-Value Discrepancy critical
// flag, or the single Data Quality Flag category derived from `remarks`)
// with a dismiss/reinstate control, reading the current dismissed state
// from `po.flagDismissals` (apps/api/routers/hrs_views.py's/
// achhad_views.py's/vapi_views.py's own _flag_dismissal_dict()). These
// flags have no underlying match row to carry a dismissed_by_override
// column the way PO<->MIR/MIR<->Stock flags do (see matchStatusHtml()) -
// they're computed client-side from po.remarks/diff percentages, not
// stored - so FlagDismissal (apps/services/flag_dismiss.py) is a separate
// generic table keyed by the flag's own label, and wireDismissLinks()
// above routes data-match-type="po-flag" here via dismissPoFlag() instead
// of dismissMatch().
// `isImport` (added 2026-09-05, alongside importCriticalFlagsFor() below)
// routes the dismiss link through 'import-po-flag' instead of 'po-flag' -
// same distinction importFlagHtml()'s own dismiss link already makes for
// F1-F7, needed here too now that Import's own Qty/Rate discrepancy
// critical flags (previously only shown as KPI cards, never listed
// alongside the F1-F7 flags in this tab) reuse this same renderer.
function poFlagHtml(c, po, plantKey, isImport) {
  const fd = (po.flagDismissals || []).find(f => f.flagKey === c.label);
  const dismissed = !!(fd && fd.dismissed);
  const dismissTag = dismissed
    ? ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (fd.dismissedBy ? ' by ' + fd.dismissedBy : '') + (fd.dismissedReason ? ': ' + fd.dismissedReason : '')) + '">dismissed</span>'
    : '';
  const link = canEditField(plantKey)
    ? ' <span class="dismiss-link" data-match-type="' + (isImport ? 'import-po-flag' : 'po-flag') + '" data-plant="' + plantKey + '" data-po-number="' + escapeHtml(po.poNumber) + '" data-flag-key="' + escapeHtml(c.label) + '" data-dismiss="' + (dismissed ? 'false' : 'true') + '">' + (dismissed ? 'reinstate' : 'dismiss') + '</span>'
    : '';
  return '<div class="field-block mb-8' + (dismissed ? ' dimmed' : '') + '">' +
    flagIconHtml(categoryColor(c.label), 'row-flag-icon') +
    ' <span class="lg-label ' + c.severity + '">' + (c.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span> ' +
    '<b' + (dismissed ? ' class="strike"' : '') + '>' + escapeHtml(c.label) + '</b>' + dismissTag + link +
  '</div>';
}

// Import's Qty/Rate discrepancy critical flags - previously only surfaced as
// KPI cards/row icons (renderImportPoList()'s poQtyDiscMir()/poRateDiscMir())
// and never listed in a PO's own Flags & Corrections tab alongside its
// F1-F7 data quality flags, which the project owner flagged as an
// inconsistency (2026-09-05: "flags and correction shouldn't only be
// happening for data quality flags but for all the flags"). Computed here
// per-PO (not the list-level closures those functions are, which need the
// whole filtered array in scope) so a single PO detail modal can build its
// own category list independent of which list view opened it.
function importCriticalFlagsFor(po) {
  const cats = [];
  const items = po.items || [];
  // A dismissed match (i.mirMatch.dismissedByOverride) is excluded from
  // every match-derived check below - 2026-09-10 fix, same reasoning as
  // computePoFlags()'s own comment (dashboard/email must agree on the same
  // underlying dismissed-match handling).
  const live = i => i.mirMatch && !i.mirMatch.dismissedByOverride;
  if (po.qtyDiscrepancy) cats.push({ label: 'Qty Mismatch (PO vs BOE)', severity: 'critical' });
  if (items.some(i => live(i) && i.mirMatch.qtyDiffPct > 0)) cats.push({ label: 'Qty Mismatch in MIR (BOE vs MIR)', severity: 'critical' });
  if (items.some(i => live(i) && i.mirMatch.rateDiffPct > 0)) cats.push({ label: 'Rate Mismatch in MIR (BOE vs MIR)', severity: 'critical' });
  // 2026-09-08 (extended to Imports, same day as Domestic's own version) -
  // see computePoFlags()'s own comment for what these 6 fields are and why
  // they were previously discarded. `!i.mirMatch` (no mir_match payload at
  // all - imports_views.py's _mir_match_dict() returns null below
  // MATCH_THRESHOLD) is the import equivalent of Domestic's `!it.matched`.
  if (items.some(i => !i.mirMatch)) cats.push({ label: 'PO Not Found in MIR', severity: 'critical' });
  // See computePoFlags()'s own comment on this same flag.
  if (items.some(i => live(i) && i.mirMatch.vendorMatched === false)) cats.push({ label: 'Vendor Name Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.taxTypeMismatch)) cats.push({ label: 'Tax Type Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.netValueMismatched)) cats.push({ label: 'Net Value Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.taxableValueMismatched)) cats.push({ label: 'Taxable Value Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.finalValueMismatched)) cats.push({ label: 'Final Amount Mismatch in MIR', severity: 'info' });
  if (items.some(i => live(i) && i.mirMatch.uomMismatch)) cats.push({ label: 'UOM Mismatch in MIR', severity: 'info' });
  return cats;
}

// Import-PO equivalent of poFlagHtml() above, for one entry of an Import
// PO's `dataQualityFlags` (apps/services/import_flags.py's per-item flags -
// `code`/`item_id`/`message`/`fields`, not Domestic's per-category label).
// flagKey is built the same way the backend's dismiss_flag view docstring
// says the frontend should: `code + ':' + (item_id || '')` - stable per
// flag on this PO since import_flags.py always emits the same code for the
// same underlying check on the same item.
function importFlagHtml(f, po, plantKey) {
  const flagKey = f.code + ':' + (f.item_id || '');
  const fd = (po.flagDismissals || []).find(row => row.flagKey === flagKey);
  const dismissed = !!(fd && fd.dismissed);
  const dismissTag = dismissed
    ? ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (fd.dismissedBy ? ' by ' + fd.dismissedBy : '') + (fd.dismissedReason ? ': ' + fd.dismissedReason : '')) + '">dismissed</span>'
    : '';
  const link = canEditField(plantKey)
    ? ' <span class="dismiss-link" data-match-type="import-po-flag" data-plant="' + plantKey + '" data-po-number="' + escapeHtml(po.poNumber) + '" data-flag-key="' + escapeHtml(flagKey) + '" data-dismiss="' + (dismissed ? 'false' : 'true') + '">' + (dismissed ? 'reinstate' : 'dismiss') + '</span>'
    : '';
  return '<div class="field-block mb-10' + (dismissed ? ' dimmed' : '') + '">' +
    '<span class="status-pill status-overdue">' + escapeHtml(f.code) + '</span>' + dismissTag + link +
    '<div class="mt-8 fs-13' + (dismissed ? ' strike' : '') + '">' + escapeHtml(f.message) + '</div>' +
    '<div class="mt-6 fs-11 text-slate-soft">Fields: ' + escapeHtml((f.fields || []).join(', ')) + '</div>' +
    (f.item_id ? '<div class="mt-4 fs-11 text-slate-soft">Item: ' + escapeHtml(f.item_id) + '</div>' : '') +
  '</div>';
}

// Material modal's Flags & Corrections tab equivalent of poFlagHtml()/
// importFlagHtml() above, for one flagged entry of a sibling lot's
// mirStockMatches (see material-modal.js's openMaterialModal() -
// `m._plantKey`/`m._plantLabel` are stamped on there since a material rolls
// up lots across all 3 plants, unlike a single-plant PO). Same field-block/
// dismiss-link markup/wiring as the other two - wireDismissLinks() already
// runs against the whole modal body, so a dismiss-link rendered here is
// picked up with no extra wiring.
function materialFlagHtml(m) {
  const dismissed = !!m.dismissedByOverride;
  const parts = [];
  if (m.qtyDiffPct != null) parts.push('qty Δ' + m.qtyDiffPct.toFixed(1) + '%');
  if (m.rateDiffPct != null) parts.push('rate Δ' + m.rateDiffPct.toFixed(1) + '%');
  const dismissTag = dismissed ? ' <span class="dismissed-tag">dismissed</span>' : '';
  const link = canEditField(m._plantKey)
    ? ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="mir-stock" data-plant="' + m._plantKey + '" data-dismiss="' + (dismissed ? 'false' : 'true') + '">' + (dismissed ? 'reinstate' : 'dismiss') + '</span>'
    : '';
  return '<div class="field-block mb-8' + (dismissed ? ' dimmed' : '') + '">' +
    flagIconHtml(KPI_FLAG_COLORS.critical, 'row-flag-icon') +
    ' <span class="lg-label critical">CRITICAL</span> ' +
    '<b' + (dismissed ? ' class="strike"' : '') + '>' + escapeHtml(m._plantLabel) + ' &middot; MIR&harr;Stock Mismatch</b>: ' +
    escapeHtml(parts.join(', ') || 'flagged') + dismissTag + link +
  '</div>';
}

// Material modal's INFO note for a sibling lot whose MIR<->Stock pairing hit
// a unit clash - the modal-tab counterpart of uomMismatchBadgeHtml(). Same
// field-block shape as materialFlagHtml() above but INFO, not CRITICAL, and
// no dismiss link: nothing is known to be wrong, the comparison could not run.
function materialUomNoteHtml(lot) {
  const unit = lot.uom ? ' (stock unit: ' + lot.uom + ')' : '';
  return '<div class="field-block mb-8">' +
    flagIconHtml(KPI_FLAG_COLORS.quality, 'row-flag-icon') +
    ' <span class="lg-label info">INFO</span> ' +
    '<b>' + escapeHtml(lot._plantLabel || '') + ' &middot; MIR&harr;Stock units differ</b>' + escapeHtml(unit) +
    '<div class="mt-4 fs-11-5 text-slate-soft">The matched MIR entry uses a unit that cannot be converted to the stock lot\'s, ' +
    'so quantity and rate were not compared. Check the unit in both files.</div>' +
  '</div>';
}

// Match Accuracy Programme fix 3.G: one apps/services/arithmetic_checks.py
// mismatch (a real typo in the source spreadsheet itself - qty x rate vs
// net_value, tax arithmetic, or a broken stock formula - not a matching
// artifact). Rendered in the same Flags & Corrections tab as
// FlagDismissal-backed flags, no dismiss link - this is a computed fact
// about the source data, not a judgment call a reviewer overrides.
const DATA_QUALITY_CHECK_LABELS = {
  po_qty_rate_value: 'Qty × Rate does not match Net Value',
  mir_tax_arithmetic: 'Taxable + GST + TCS − Discount does not match Final Value',
  stock_balance: 'Opening + Received − Issued does not match Current Stock',
};
function dataQualityFlagHtml(f) {
  const label = DATA_QUALITY_CHECK_LABELS[f.checkName] || f.checkName;
  const plantPrefix = f._plantLabel ? escapeHtml(f._plantLabel) + ' &middot; ' : '';
  return '<div class="field-block mb-8">' +
    flagIconHtml(KPI_FLAG_COLORS.critical, 'row-flag-icon') +
    ' <span class="lg-label critical">DATA QUALITY</span> ' +
    '<b>' + plantPrefix + escapeHtml(label) + '</b>: expected ' + formatInr(f.expected) + ', sheet says ' + formatInr(f.actual) +
  '</div>';
}

/** Matches `text` (a PO's own `remarks` field) against FLAG_CATEGORY_RULES
 * in order, returning the first rule's label, or a generic fallback
 * category when nothing matches. Every result is severity 'info' - the 2
 * 'critical' categories (Quantity/Rate-Value Discrepancy) are computed
 * separately from diff percentages, not from this regex pass. */
function categorizeFlag(text) {
  for (const rule of FLAG_CATEGORY_RULES) if (rule.test(text)) return { label: rule.label, severity: 'info' };
  return { label: 'Other data quality issue', severity: 'info' };
}

// Full glossary for the collapsible legend panel - one source of truth
// alongside FLAG_CATEGORY_RULES above. Zero tolerance (2026-09-04, see
// FLAG_PCT's own comment) - the two critical categories' wording says so
// explicitly rather than interpolating FLAG_PCT ("by more than 0 percent"
// is technically correct but reads oddly; "any difference" is clearer).
const DISCREPANCY_LEGEND = [
  { label: 'Over-Delivered in MIR', severity: 'critical', meaning: 'More arrived than the PO ordered, summed across every delivery against this line.', detail: 'Zero tolerance - any difference at all flags. Can be a genuine over-shipment, a receipt booked against the wrong order, or an order topped up without the PO being revised.' },
  { label: 'Short-Delivered in MIR', severity: 'critical', meaning: 'Less arrived than the PO ordered.', detail: 'Zero tolerance - any difference at all flags. On an open order this is just a part-delivery and expected; on a closed one it is a short shipment. The Progress column shows how much has arrived.' },
  { label: 'Quantity Mismatch in MIR', severity: 'critical', meaning: 'Received quantity does not exactly match the ordered quantity.', detail: 'Zero tolerance - 999kg against a 1000kg order still flags. Can be a short shipment, an over shipment, or a receipt logged against the wrong PO.' },
  { label: 'Rate Mismatch in MIR', severity: 'critical', meaning: 'Received rate does not exactly match the PO rate.', detail: 'Zero tolerance - any difference at all flags. Can be a price change not reflected on the PO, or a billing error. Value is deliberately not compared: it is qty x rate, so a quantity mismatch alone would double-count as a second, unrelated-looking problem.' },
  { label: 'PO Not Found in MIR', severity: 'critical', meaning: 'No MIR entry could be matched to this line item at all.', detail: 'No exact PO-number match, and nothing scored high enough on the weighted match. The goods may not have arrived yet, or the receipt was logged in a way the matcher could not link back.' },
  { label: 'Vendor Name Mismatch in MIR', severity: 'info', meaning: 'Matched on PO number and material, but the party name disagrees with the PO vendor.', detail: 'The match itself is sound - the order number and the amounts agree - so this is a name to fix at source: usually a typo, a placeholder left in the Party Name column, or one supplier written two ways across the two files.' },
  { label: 'Tax Type Mismatch in MIR', severity: 'info', meaning: 'The tax structure used (e.g. IGST vs CGST+SGST) is not consistent between the PO and the matched MIR entry.' },
  { label: 'Net Value Mismatch in MIR', severity: 'info', meaning: 'The pre-tax net value on the matched MIR entry differs from the PO’s net value by more than a small rounding allowance.' },
  { label: 'Taxable Value Mismatch in MIR', severity: 'info', meaning: 'The taxable value on the matched MIR entry differs from the PO’s taxable value by more than a small rounding allowance.' },
  { label: 'Final Amount Mismatch in MIR', severity: 'info', meaning: 'The final (post-tax) amount on the matched MIR entry differs from the PO’s final amount by more than a small rounding allowance.' },
  { label: 'UOM Mismatch in MIR', severity: 'info', meaning: 'The matched MIR entry records quantity in a different unit family than the PO (for example mass vs count) - the two are not directly comparable without conversion.' },
  { label: 'Misfiled: wrong plant or company', severity: 'info', meaning: 'The PO document was found filed under the wrong plant or company folder in Drive.' },
  { label: 'Duplicate file or PO', severity: 'info', meaning: 'The same PO appears to have been saved or extracted more than once.' },
  { label: 'Revision or superseded PO conflict', severity: 'info', meaning: 'A later revision of the PO exists, or PO numbering suggests it replaced an earlier one, and both versions are present.' },
  { label: 'Tax calculation or labeling mismatch', severity: 'info', meaning: 'The tax type, rate, or amount on the PO looks inconsistent, for example IGST used where CGST plus SGST was expected, or the numbers do not add up.' },
  { label: 'Header or template data error', severity: 'info', meaning: 'The PO letterhead or template fields, such as company name or address, look wrong or inconsistent with the vendor or plant.' },
  { label: 'Vendor GSTIN anomaly', severity: 'info', meaning: 'The vendor GSTIN on the PO looks malformed or does not match known records.' },
  { label: 'Missing or blank field', severity: 'info', meaning: 'A field expected on the PO, such as incoterms or a tax breakdown, is blank or was not captured during extraction.' },
  { label: 'Import PO filed in Domestic folder', severity: 'info', meaning: 'A PO that is actually an international or import order was found in the Domestic purchases folder.' },
  { label: 'Delivery date anomaly', severity: 'info', meaning: 'The delivery date is the same as the created date, which usually means immediate delivery and is unusual, or otherwise looks off.' },
  { label: 'Vendor code scheme inconsistency', severity: 'info', meaning: 'The vendor code format does not match the scheme used elsewhere.' },
  { label: 'Non raw material or different category', severity: 'info', meaning: 'The PO is for something that is not raw material, such as capital equipment or tooling, and may not belong in this tracker.' },
  { label: 'Other data quality issue', severity: 'info', meaning: 'A note recorded in the PO’s Remarks field during extraction that did not match any of the specific patterns above.' },
];
// Per-category flag color - "the flag color should match the error it
// represents" (project owner, 2026-09-04): previously every info category
// shared one flat purple, so "Duplicate file or PO" and "Tax calculation
// mismatch" looked identical at a glance and only the hover tooltip told
// them apart. Grouped into a handful of thematically distinct colors rather
// than either one flat color (too little signal) or 14 unique hues (too
// noisy to read) - money/qty problems (red), tax/compliance (amber),
// filing/process issues - wrong plant, duplicate, superseded, wrong
// domestic/import folder (indigo), data-completeness gaps - template,
// missing fields, vendor code scheme, wrong category (slate), delivery
// timing (blue), and an "other" catch-all (purple). Keys must match
// DISCREPANCY_LEGEND's/FLAG_CATEGORY_RULES' `label` strings exactly - if
// you add a new category there, add its color here too, or it silently
// falls back to DEFAULT_CATEGORY_COLOR.
const CATEGORY_COLORS = {
  'Quantity Mismatch in MIR': '#dc2626',
  'Over-Delivered in MIR': '#dc2626',
  'Short-Delivered in MIR': '#dc2626',
  'Rate Mismatch in MIR': '#dc2626',
  'PO Not Found in MIR': '#dc2626',
  'Tax Type Mismatch in MIR': '#d97706',
  'Net Value Mismatch in MIR': '#d97706',
  'Taxable Value Mismatch in MIR': '#d97706',
  'Final Amount Mismatch in MIR': '#d97706',
  'UOM Mismatch in MIR': '#64748b',
  'Tax calculation or labeling mismatch': '#d97706',
  'Vendor GSTIN anomaly': '#d97706',
  'Misfiled: wrong plant or company': '#6366f1',
  'Duplicate file or PO': '#6366f1',
  'Revision or superseded PO conflict': '#6366f1',
  'Import PO filed in Domestic folder': '#6366f1',
  'Header or template data error': '#64748b',
  'Missing or blank field': '#64748b',
  'Vendor code scheme inconsistency': '#64748b',
  // Slate, with the other data-completeness gaps: a name typed wrong in
  // one of the two files, not a money or compliance problem.
  'Vendor Name Mismatch in MIR': '#64748b',
  'Non raw material or different category': '#64748b',
  'Delivery date anomaly': '#2563eb',
};
const DEFAULT_CATEGORY_COLOR = '#7c3aed'; // 'Other data quality issue' + any unmapped label
function categoryColor(label) { return CATEGORY_COLORS[label] || DEFAULT_CATEGORY_COLOR; }

function renderLegendHtml() {
  // categoryColor() returns an arbitrary per-category hex, not a fixed
  // small enum a CSS class could cover - kept as a data-attribute here and
  // applied via JS after render (see wireLegendDotColors(), called once the
  // caller inserts this HTML) rather than a literal style="..." attribute,
  // since that's blocked once style-src drops 'unsafe-inline' (setting the
  // property via JS afterwards is not - see brand.css's "Utility classes"
  // comment for why).
  // `meaning` is the one-sentence definition and always shows; `detail`
  // carries the causes and caveats on a second, muted line (readability
  // pass, 2026-09-21 - six entries used to run 60+ words as one paragraph
  // with the definition buried mid-sentence). An entry short enough to say
  // everything in one sentence simply has no `detail`.
  const rows = DISCREPANCY_LEGEND.map(d => '<li class="legend-item"><span class="lg-dot" data-dot-color="' + categoryColor(d.label) + '"></span><span class="lg-label ' + d.severity + '">' + (d.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span><b>' + escapeHtml(d.label) + '</b>: ' + escapeHtml(d.meaning) + (d.detail ? '<div class="legend-detail">' + escapeHtml(d.detail) + '</div>' : '') + '</li>').join('');
  const criticalCount = DISCREPANCY_LEGEND.filter(d => d.severity === 'critical').length;
  const infoCount = DISCREPANCY_LEGEND.length - criticalCount;
  return '<div class="legend-box" id="legendBox"' + (state.legendOpen ? '' : ' hidden') + '>' +
    // The four row-flag colours first: that is the vocabulary a reader
    // actually meets in the list, and the per-category glossary below is
    // what they consult once one of them prompts a question.
    '<h4>The flags beside a status</h4>' +
    rowFlagKeyHtml() +
    '<h4 class="mt-14">What each flag means (' + DISCREPANCY_LEGEND.length + ' categories total: ' + criticalCount + ' critical, ' + infoCount + ' informational)</h4>' +
    '<div class="legend-sub">Critical flags are computed directly from PO versus actual goods receipt data and need action first. Informational flags come from data quality notes recorded when each PO was extracted, and are process or paperwork issues rather than money or quantity problems.</div>' +
    '<ul class="legend-list">' + rows + '</ul>' +
  '</div>';
}

// Mirrors the artifact's rec._deliveryDate: the earliest delivery date among
// this PO's still-unmatched line items (what a viewer actually needs to
// know - "when is the outstanding part due"), falling back to the earliest
// delivery date overall only once every line item has already matched.
function computePoDeliveryDate(po) {
  const items = po.items || [];
  const unmatchedDates = items.filter(it => !it.matched).map(it => it.deliveryDate).filter(Boolean).sort();
  if (unmatchedDates.length) return unmatchedDates[0];
  const allDates = items.map(it => it.deliveryDate).filter(Boolean).sort();
  return allDates.length ? allDates[0] : null;
}

// Computed once per PO alongside computeStatus() - drives the Quantity/
// Rate-Value Discrepancy KPI cards, the per-row flag badges, and the
// legend panel above. Uses this app's own matching-engine threshold
// (FLAG_PCT = apps/services/matching*.py's FLAG_DIFF_PCT) - zero tolerance
// as of 2026-09-04, see FLAG_PCT's own comment for why.
function computePoFlags(po) {
  const items = po.items || [];
  // Every check below that reads a match-row field (qty/rate/tax/value/uom)
  // excludes an item whose match was dismissed by an editor
  // (it.dismissedByOverride) - 2026-09-10 fix, project owner report: the
  // Plant Data Correction email already excludes a dismissed match
  // (plant_mismatch_report.py's own dismissed_by_override=False filter),
  // and a dismissed match's own badge already renders struck-through/muted
  // (matchStatusHtml() below) to say "reviewed, not a real problem" - but
  // these KPI/category counts kept counting it anyway, so the dashboard and
  // the email could disagree on the same underlying data for no reason
  // other than this inconsistency. `!it.matched` (no match row exists at
  // all) is unaffected - there's nothing to dismiss when there's no match.
  po._qtyFlag = items.some(it => it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT && !it.dismissedByOverride);
  // Rate mismatch only - NOT value. Value = qty x rate, so a qty mismatch
  // alone already drags value along with it; counting that as a second,
  // independent "rate/value" problem double-counted the same underlying
  // partial-delivery event under two different KPI cards (confirmed
  // 2026-09-08: of 119 POs this used to flag, 114 were already flagged by
  // qty alone - only 5 had a genuine standalone rate issue). Project owner
  // decision, 2026-09-08: drop value from this determination entirely.
  po._rateFlag = items.some(it => it.rateDiffPct != null && it.rateDiffPct > FLAG_PCT && !it.dismissedByOverride);
  // Largest single diff percentage across every line item (qty/rate/value
  // alike) - drives rowTintClass()'s severity-scaled row background in the
  // "View all" table/top-5 preview, so a reviewer's eye is pulled toward the
  // worst offenders instead of every flagged row reading identically. Only
  // meaningful when _qtyFlag/_rateFlag is actually true - a PO with no flag
  // may still have a small nonzero diff sitting under FLAG_PCT's zero-
  // tolerance threshold that rounds to 0.00% and shouldn't drive any tint.
  const allDiffs = items.filter(it => !it.dismissedByOverride).flatMap(it => [it.qtyDiffPct, it.rateDiffPct, it.valueDiffPct]).filter(v => v != null);
  po._maxDiffPct = allDiffs.length ? Math.max(...allDiffs) : 0;
  // Over vs under delivery (2026-09-18, project owner). The qty flag splits
  // by DIRECTION, not by size: tolerance stays zero, so every difference
  // still flags - but "took more than we ordered" and "hasn't all arrived
  // yet" are different problems with different owners, and until now both
  // read as one `Quantity Mismatch in MIR`. A PO can legitimately show both
  // at once when it has several line items, so these are two independent
  // checks rather than an if/else.
  //
  // `=== true` / `=== false` deliberately, never truthiness: qtyOverDelivered
  // is null when no quantity comparison was possible at all (UOM mismatch, or
  // a missing qty), and a null must not be counted as "under".
  const qtyOverItems = items.filter(it => it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT
    && !it.dismissedByOverride && it.qtyOverDelivered === true);
  const qtyUnderItems = items.filter(it => it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT
    && !it.dismissedByOverride && it.qtyOverDelivered === false);
  // Hung on the PO alongside _qtyFlag/_rateFlag so the KPI row and the
  // "Filter by Flags" dropdown can count the two directions separately
  // without re-deriving them per render - see po-list.js's cardDef.
  po._qtyOverFlag = qtyOverItems.length > 0;
  po._qtyUnderFlag = qtyUnderItems.length > 0;
  const cats = new Map();
  if (qtyOverItems.length) cats.set('Over-Delivered in MIR', { label: 'Over-Delivered in MIR', severity: 'critical' });
  if (qtyUnderItems.length) cats.set('Short-Delivered in MIR', { label: 'Short-Delivered in MIR', severity: 'critical' });
  // Retained for a row whose direction could not be determined at all, so a
  // real qty mismatch can never vanish from the flag list just because the
  // units did not convert.
  if (po._qtyFlag && !qtyOverItems.length && !qtyUnderItems.length) cats.set('Quantity Mismatch in MIR', { label: 'Quantity Mismatch in MIR', severity: 'critical' });
  if (po._rateFlag) cats.set('Rate Mismatch in MIR', { label: 'Rate Mismatch in MIR', severity: 'critical' });
  // 2026-09-08: previously-computed-but-discarded match-quality signals
  // (matching_core.py's _diffs_and_flag() always computed these, but only
  // qty/rate ever surfaced anywhere - see CLAUDE.md's "Identification/
  // Financial-Check redesign") are now their own Data Quality Flag
  // categories instead of being invisible. "PO Not Found in MIR" reads
  // straight off `matched` (no MIR entry crossed MATCH_THRESHOLD for this
  // line item at all) rather than any match-row field, since there's no
  // match row to read from in that case.
  if (items.some(it => !it.matched)) cats.set('PO Not Found in MIR', { label: 'PO Not Found in MIR', severity: 'critical' });
  // Identification 2-of-3 (2026-09-18, Achhad and HRS - see matching_core.py's
  // _MatchConfig.identification_two_of_three). `vendorMatched` is true
  // everywhere the vendor gate is still mandatory, so this flag never fires on
  // Vapi; on Achhad, false means the match was identified by its PO number and
  // material while the party name disagreed. On HRS it should not fire either
  // on today's file - no HRS MIR row that names an order we hold disagrees on
  // vendor (matching.py's comment has the count) - so the first one that does
  // appear is genuinely new, not a backlog. That is always a
  // real name error at source - a typo, a placeholder left in the column, or
  // one supplier written two ways - and the whole reason the rule surfaces it
  // instead of quietly accepting the match. Read with `=== false` rather than
  // `!it.vendorMatched` so a line item with no match row at all (vendorMatched
  // undefined) is not counted as a vendor mismatch; those are already reported
  // as 'PO Not Found in MIR'.
  if (items.some(it => it.vendorMatched === false && !it.dismissedByOverride)) cats.set('Vendor Name Mismatch in MIR', { label: 'Vendor Name Mismatch in MIR', severity: 'info' });
  if (items.some(it => it.taxTypeMismatch && !it.dismissedByOverride)) cats.set('Tax Type Mismatch in MIR', { label: 'Tax Type Mismatch in MIR', severity: 'info' });
  if (items.some(it => it.netValueMismatched && !it.dismissedByOverride)) cats.set('Net Value Mismatch in MIR', { label: 'Net Value Mismatch in MIR', severity: 'info' });
  if (items.some(it => it.taxableValueMismatched && !it.dismissedByOverride)) cats.set('Taxable Value Mismatch in MIR', { label: 'Taxable Value Mismatch in MIR', severity: 'info' });
  if (items.some(it => it.finalValueMismatched && !it.dismissedByOverride)) cats.set('Final Amount Mismatch in MIR', { label: 'Final Amount Mismatch in MIR', severity: 'info' });
  if (items.some(it => it.uomMismatch && !it.dismissedByOverride)) cats.set('UOM Mismatch in MIR', { label: 'UOM Mismatch in MIR', severity: 'info' });
  if (po.remarks) { const c = categorizeFlag(po.remarks); cats.set(c.label, c); }
  po._categories = Array.from(cats.values());
  po._hasInfoFlag = po._categories.some(c => c.severity === 'info');
}

// Severity-scaled row background for the "View all" table/top-5 preview
// (see .row-tint-mild/-moderate/-severe in style.css) - a quantity/rate/
// value discrepancy already gets a flag badge in that row, but every
// flagged row read identically regardless of whether the diff was 0.5% or
// 80%. Bucketed rather than a continuous gradient (simpler to reason about
// and to eyeball consistently row-to-row) - thresholds are a judgment call,
// not measured against labeled data, same as FLAG_PCT itself (see its own
// comment) and easy to retune in one place if they prove wrong in practice.
// Works for any object computePoFlags() stamped with _qtyFlag/_rateFlag/
// _maxDiffPct (Domestic POs), or Materials' own computeMaterialPoLinkage()
// entries, which use the same fields without the underscore prefix
// (qtyFlag/rateFlag/maxDiffPct) - checks both naming conventions rather
// than making every caller normalize its own object shape first.
// Compact always-visible legend for rowTintClass()'s row shading - the
// shading has no other on-screen explanation otherwise (a flag badge is
// visible per-row already, but nothing said why one flagged row looked
// pinker than another), which read as an unexplained/broken row rather than
// a deliberate severity cue when the project owner first saw it. Placed
// next to each list's own toggle-all/pagination row, not inside the (PO-
// remarks-specific) DISCREPANCY_LEGEND panel, since this applies to every
// list (PO/Materials/Import) uniformly.
function rowTintLegendHtml() {
  return '<span class="row-tint-legend">Row shading = mismatch size:' +
    '<span class="row-tint-swatch row-tint-mild"></span>Mild' +
    '<span class="row-tint-swatch row-tint-moderate"></span>Moderate' +
    '<span class="row-tint-swatch row-tint-severe"></span>Severe</span>';
}

function rowTintClass(rec) {
  const qtyFlag = rec._qtyFlag != null ? rec._qtyFlag : rec.qtyFlag;
  const rateFlag = rec._rateFlag != null ? rec._rateFlag : rec.rateFlag;
  const maxDiffPct = rec._maxDiffPct != null ? rec._maxDiffPct : (rec.maxDiffPct || 0);
  if (!qtyFlag && !rateFlag) return '';
  if (maxDiffPct >= 20) return ' row-tint-severe';
  if (maxDiffPct >= 5) return ' row-tint-moderate';
  return ' row-tint-mild';
}

// Same 5-status model as the source artifact (received / partial / pending
// / overdue / unknown), driven by the real PO<->MIR match, not a
// placeholder. A PO is only "received"/"partial" once matching has
// actually linked a line item to a live MIR row.
// Display label only - the internal status key stays 'pending' everywhere
// (state.statusFilter, computeStatus()'s return value, the 'pending' CSS
// class on .status-pill/.kpi-card, etc.) so nothing else needs to change to
// pick this up; STATUS_LABELS.pending is the single place the visible text
// lives. Renamed 'Pending' -> 'On Order' (2026-09-04, project owner).
const STATUS_LABELS = { received: 'Received', partial: 'Partial Delivered', pending: 'On Order', overdue: 'Overdue', unknown: 'Delivery Date Unknown' };
// Did anything at all arrive against this line item? A match dismissed by a
// reviewer (dismissedByOverride) means "this pairing is wrong", so it is not
// an arrival - computePoFlags() has always excluded dismissed matches from
// its counts, and before 2026-09-18 the status did not, so dismissing a bad
// match cleared a PO's flags while leaving it counted as Material Inwarded.
function lineItemArrived(it) {
  return !!it.matched && !it.dismissedByOverride;
}

// Is this line item's order actually FULFILLED - not merely matched?
//
// This distinction is what the status buckets got wrong until 2026-09-18.
// Matching is existence-based: it answers "which MIR row belongs to this
// line", not "did all of it turn up". Measured on Achhad's 134 domestic POs,
// 23 of the 120 counted as Material Inwarded were short-delivered, including
// PO 1100000790 (100 of 500 KG of Titanium Dioxide - 80% short) and
// 1100000834 (coal, 49% short). Both read as fully received.
//
// Short-delivered is therefore NOT received. Over-delivered still is: the
// material did arrive, and taking too much is a different problem, already
// flagged separately as Over-Delivered in MIR.
//
// A null qtyOverDelivered means the direction could not be determined (a UOM
// mismatch, a missing quantity, or a match row written before migration
// 0050). That is treated as received rather than short - this function must
// not invent a shortfall it cannot actually measure, since the cost of a
// false "incomplete" is a PO chased that was already fine.
function lineItemFullyReceived(it) {
  if (!lineItemArrived(it)) return false;
  if (it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT && it.qtyOverDelivered === false) return false;
  return true;
}

// Sets po._overdue as a side effect, deliberately - see the OVERDUE note
// below. Every caller assigns `po._status = computeStatus(po)`, so doing it
// here is what keeps the overlay in step across all four call sites
// (po-list.js, materials.js, material-modal.js) without each having to
// remember a second call.
function computeStatus(po) {
  const items = po.items || [];
  const dates = items.map(it => it.deliveryDate).filter(Boolean).sort();
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const dd = dates.length ? new Date(dates[0] + 'T00:00:00') : null;
  const pastDue = !!dd && !isNaN(dd.getTime()) && dd < today;

  let status;
  if (!items.length) {
    status = 'pending';
  } else if (items.every(lineItemFullyReceived)) {
    status = 'received';
  } else if (items.some(lineItemArrived)) {
    // Something turned up, but the order is not complete - either a line is
    // still unmatched, or one arrived short. Before 2026-09-18 this second
    // case was impossible to reach on a single-line-item PO (102 of Achhad's
    // 134), because matchedCount could only be 0 or 1 - so "Partial
    // Delivered" measured "multi-item PO with some items unmatched", never
    // partial delivery.
    status = 'partial';
  } else if (!dates.length) {
    status = 'unknown';
  } else {
    status = pastDue ? 'overdue' : 'pending';
  }

  // OVERDUE IS AN OVERLAY, NOT A BUCKET (2026-09-18, project owner).
  // It used to be reachable only when NOTHING had matched, because the
  // received/partial branches returned before the date was ever consulted -
  // so one delivery landing made a PO permanently un-overdue however much
  // was still outstanding. On Achhad that hid 26 of 34 genuinely late
  // orders; the card read 8.
  //
  // Being past due is orthogonal to how much has arrived, so it is now its
  // own boolean and a PO can be both Partial and Overdue. The consequence,
  // accepted deliberately: the status KPI cards no longer sum to Total PO's
  // Created. See po-list.js's cardDef.
  po._overdue = pastDue && status !== 'received';
  // No delivery date on file AT ALL - an overlay, for the same reason
  // _overdue is one (2026-09-18). As a status bucket this was only ever
  // reachable when nothing had arrived either, so a PO with no delivery date
  // whose material HAS turned up was silently counted as Received and the
  // card read zero. Measured on Achhad: 10 of 134 POs carry no delivery date
  // and the card showed 0. The card's own tooltip already described it this
  // way - "no delivery date on file" - so the count now matches what it says.
  //
  // This is a data-completeness signal, not a delivery state: a missing date
  // is worth chasing whether or not the goods arrived, because without it
  // nothing can ever be called overdue or on order.
  po._noDeliveryDate = !dates.length;
  return status;
}

// 3-step stepper (Ordered / Material Inwarded / Received in Inventory) -
// the 3rd step reads each matched line item's `stockMatched` (see
// hrs_views.py/achhad_views.py/vapi_views.py's _line_item_dict, added
// 2026-09-04) - true only when the MIR entry a line item matched to *also*
// has a real MIR<->Stock match, i.e. the material is currently sitting in
// the Stock file, not just MIR-received. This is a coarse, honest signal
// (a real PO->MIR->Stock chain per line item, not "does this material show
// up anywhere in stock" the way the old artifact prototype approximated it
// - see CLAUDE.md's roadmap note this replaces) - expect it to rarely show
// "done" for HRS specifically, since HRS's Stock sheet's own `received`
// column already reads 0 for nearly every real lot (documented elsewhere in
// CLAUDE.md), which limits how often a fresh MirStockMatch forms at all.
function miniStepperHtml(po) {
  const items = po.items || [];
  const anyMatched = items.some(it => it.matched);
  const anyStocked = items.some(it => it.stockMatched);
  const title = 'Ordered' +
    (anyMatched ? ' → Material Inwarded' : ' (awaiting MIR match)') +
    (anyStocked ? ' → Received in Inventory' : (anyMatched ? ' (awaiting Stock match)' : ''));
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(title) + '">' +
    '<div class="mini-stepper">' +
      '<span class="step-dot done"></span><span class="step-line ' + (anyMatched ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (anyMatched ? 'done' : '') + '"></span><span class="step-line ' + (anyStocked ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (anyStocked ? 'done' : '') + '"></span>' +
    '</div>' +
    '<div class="mini-stepper-labels">Ordered / Inwarded / Stocked</div>' +
  '</div>';
}

// Materials' own 2-step progress indicator - same markup/CSS classes as
// miniStepperHtml() above, but the two steps are "MIR" (has this material
// been received against at least one linked PO line item - i.e. matched to
// a real MIR entry, same `matched` flag PO rows use) and "Stocked" (is it
// actually sitting in current warehouse stock right now, qty > 0) - per the
// project owner's 2026-09-04 request, deliberately not PO's "Ordered"
// framing (a material has no universal "always true" first state the way a
// PO's own existence means "Ordered" always happened).
function materialStepperHtml(m) {
  // `m.mirMatched` is a real signal from the backend (HRSMirStockMatch/etc,
  // the actual computed Stock<->MIR match - see apps/api/routers/*_views.py's
  // _lot_dict()), not inferred from whether this app's own fuzzy PO<->
  // material text matching happened to also find a linked, MIR-matched PO
  // line item. That fuzzy-link-based proxy is what this used to read here -
  // wrong by construction, since a material can be truly MIR-received with
  // zero POs ever fuzzy-linked to it (or the fuzzy match simply missing),
  // so it could show "Stocked" done with "MIR" not done, which the project
  // owner correctly flagged as logically backwards (2026-09-04): stock can
  // only ever have arrived via an MIR receipt in the first place.
  const mirMatched = !!m.mirMatched;
  const stocked = (m.qty || 0) > 0;
  const title = (mirMatched ? 'MIR matched' : 'Awaiting MIR match') + ' → ' + (stocked ? 'Stocked' : 'Not currently in stock');
  return '<div class="mini-stepper-wrap" title="' + escapeHtml(title) + '">' +
    '<div class="mini-stepper">' +
      '<span class="step-dot ' + (mirMatched ? 'done' : '') + '"></span><span class="step-line ' + (mirMatched ? 'done' : '') + '"></span>' +
      '<span class="step-dot ' + (stocked ? 'done' : '') + '"></span>' +
    '</div>' +
    '<div class="mini-stepper-labels">MIR / Stocked</div>' +
  '</div>';
}

// Per-column header filters on the "View all" PO table - see the header
// filter row rendered inside renderPoList() and state.colFilters' own
// comment. Table-only (like chartMonthFilter/statusFilter above it in the
// filter chain), so these never change the KPI counts or chart data, only
// which rows the list shows. Status itself isn't here - the header's
// Status <select> reads/writes state.statusFilter directly (see
// statusChartData's `key` comment) so there's only ever one source of
// truth for "which status is selected", not two that could disagree.
function applyColFilters(recs) {
  const f = state.colFilters;
  return recs.filter(po => {
    if (f.poNumber && !(po.poNumber || '').toLowerCase().includes(f.poNumber.toLowerCase())) return false;
    if (f.vendor && !(po.vendorName || '').toLowerCase().includes(f.vendor.toLowerCase())) return false;
    if (f.deliveryFrom && (!po._deliveryDate || po._deliveryDate < f.deliveryFrom)) return false;
    if (f.deliveryTo && (!po._deliveryDate || po._deliveryDate > f.deliveryTo)) return false;
    if (f.progress === 'inwarded' && !(po.items || []).some(it => it.matched)) return false;
    if (f.progress === 'not' && (po.items || []).some(it => it.matched)) return false;
    return true;
  });
}

// Refocuses (and restores cursor position on) whatever header filter input
// had focus before an innerHTML re-render blew it away - a text filter
// rebuilds its own subtree on every keystroke, which would otherwise kick
// focus out after the first character typed. Wrap any state-mutating handler
// that triggers a re-render while a filter input might be focused:
// preserveFocus(el, () => { ...; renderPoList(el); }).
//
// All THREE filter attributes, not just data-cf: data-cf (Domestic PO),
// data-icf (Import PO), data-mcf (Materials) - the same set
// applyAccessibleNames() reads in shared.js. This used to look at data-cf
// alone, so Materials' and Import Purchases' own header filters were never
// re-focused at all: the input was rebuilt, nothing restored focus, and the
// next character went nowhere - reported (2026-09-19) as the Raw Materials
// search "reloading" on every keystroke. Domestic PO was the only one that
// ever worked, which is why the bug survived in the two files that copied
// this call without owning the helper.
const FILTER_ATTRS = ['data-cf', 'data-icf', 'data-mcf'];
function preserveFocus(container, renderFn) {
  const active = document.activeElement;
  const attr = active && active.getAttribute ? FILTER_ATTRS.find(a => active.hasAttribute(a)) : null;
  const key = attr ? active.getAttribute(attr) : null;
  const selStart = key && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  const selEnd = key && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;
  renderFn();
  if (!key) return;
  const restored = container.querySelector('[' + attr + '="' + key + '"]');
  if (!restored) return;
  restored.focus();
  if (selStart != null && restored.setSelectionRange) {
    try { restored.setSelectionRange(selStart, selEnd); } catch (e) { /* not a text-selectable input (date/number/select) */ }
  }
}

