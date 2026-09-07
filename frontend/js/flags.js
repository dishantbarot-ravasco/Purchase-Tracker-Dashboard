/**
 * frontend/js/flags.js — PO status, match-confidence badges, and Data
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

// Separate qty/rate/value badges (previously one blended "review" flag) so a
// normal partial delivery (qty differs, rate/value line up) doesn't read the
// same as a genuine price/value discrepancy. Value is suppressed when qty is
// already flagged, since value = qty x rate - a value gap fully explained by
// a partial-delivery qty gap isn't a separate problem worth a second badge.
// Matches are algorithmic (PO-number exact match or a weighted score, see
// CLAUDE.md) - the confidence badge and these diff badges are there so a
// human still manually verifies anything that isn't a plain PO-number match,
// not as a replacement for that review.
function matchStatusHtml(it, plantKey) {
  if (!it.matched) return '-';
  let out = '✓' + (it.matchedMirNo ? ' (' + escapeHtml(it.matchedMirNo) + ')' : '');

  const conf = it.matchTier === 'po_number' ? 'high' : (it.matchScore != null && it.matchScore >= 0.75 ? 'medium' : 'low');
  const confTitle = { high: 'High confidence: exact PO number match', medium: 'Medium confidence: weighted score ≥ 0.75', low: 'Low confidence: weighted score below 0.75 - verify manually' }[conf]
    + (it.matchScore != null ? ' (score ' + it.matchScore.toFixed(2) + ')' : '');
  out += ' <span class="conf-badge conf-' + conf + '" title="' + escapeHtml(confTitle) + '">' + conf + '</span>';

  const qtyFlag = it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT;
  const rateFlag = it.rateDiffPct != null && it.rateDiffPct > FLAG_PCT;
  const uomFlag = !!it.uomMismatch;
  // Match Accuracy Programme fix 3.F (2026-09-05): value gets a small
  // absolute-currency epsilon on the backend now (matching_core.py's
  // VALUE_FLAG_EPSILON), not zero tolerance like qty/rate - re-deriving a
  // value flag here from `valueDiffPct > FLAG_PCT` would ignore that
  // epsilon and show a badge the backend no longer considers a real
  // discrepancy. `it.matchFlagged` (match.is_flagged) is the backend's own
  // decision; once qty/rate/uom are accounted for, any remaining
  // matchFlagged must be the value epsilon firing.
  const valueFlag = !!it.matchFlagged && !qtyFlag && !rateFlag && !uomFlag;
  const anyFlag = qtyFlag || rateFlag || valueFlag || uomFlag;
  // Dismissed (apps/services/match_dismiss.py) keeps the badges visible but
  // muted, rather than hiding them - a reviewer who dismissed a flag should
  // still be able to see what was dismissed and why, not lose the record.
  const dismissedCls = it.dismissedByOverride ? ' dismissed' : '';
  // Severity band (fix 3.F) as a CSS modifier class - see style.css's
  // .flag-badge.sev-material/.sev-minor/.sev-rounding - so a reviewer's eye
  // is pulled toward material discrepancies first instead of hunting for
  // them among rounding noise, without hiding or discarding anything.
  const severityCls = it.severity ? ' sev-' + it.severity : '';
  // Badge text uses 1 decimal place, not toFixed(0) - with zero tolerance
  // (see FLAG_PCT's own comment) a genuinely flagged 0.1% diff would
  // otherwise round to "Δ0%", which reads as "no difference" and
  // contradicts the badge existing at all.
  if (qtyFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Quantity mismatch in MIR: qty received differs from PO qty by ' + it.qtyDiffPct.toFixed(2) + '% - likely a partial/over delivery, verify manually">qty Δ' + it.qtyDiffPct.toFixed(1) + '%</span>';
  if (rateFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Rate mismatch in MIR: rate differs from PO rate by ' + it.rateDiffPct.toFixed(2) + '% - verify manually">rate Δ' + it.rateDiffPct.toFixed(1) + '%</span>';
  if (uomFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="UOM mismatch in MIR: quantity is recorded in a different unit family on each side (e.g. mass vs count) - not directly comparable, verify manually">uom mismatch</span>';
  if (valueFlag && it.valueDiffPct != null) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Value mismatch in MIR: value differs from PO value by ' + it.valueDiffPct.toFixed(2) + '%, beyond the rounding epsilon and not explained by qty - verify manually">value Δ' + it.valueDiffPct.toFixed(1) + '%</span>';

  if (anyFlag && it.dismissedByOverride) {
    out += ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (it.dismissedBy ? ' by ' + it.dismissedBy : '') + (it.dismissedReason ? ': ' + it.dismissedReason : '')) + '">dismissed</span>';
    if (canEditField(plantKey)) {
      out += ' <span class="dismiss-link" data-match-id="' + it.matchId + '" data-match-type="po-mir" data-dismiss="false">reinstate</span>';
    }
  } else if (anyFlag && canEditField(plantKey)) {
    out += ' <span class="dismiss-link" data-match-id="' + it.matchId + '" data-match-type="po-mir" data-dismiss="true">dismiss</span>';
  }
  return out;
}

// Import-PO equivalent of matchStatusHtml() above, for an import line
// item's nested `mirMatch` object (apps/api/routers/imports_views.py's
// _mir_match_dict()) instead of the flat matched/matchTier/... fields
// domestic line items carry - same badge shapes/colors, just reading from
// a nested object since imports_views.py is one cross-plant router rather
// than three per-plant ones (see that module's docstring) and couldn't
// reuse the exact flat shape without colliding with its own PO-level
// fields (dataQualityFlags, etc.) that already use similar names.
function importMatchStatusHtml(it, plantKey) {
  const m = it.mirMatch;
  if (!m) return '-';
  let out = '✓';
  const conf = m.tier === 'po_number' ? 'high' : (m.matchScore != null && m.matchScore >= 0.75 ? 'medium' : 'low');
  const confTitle = { high: 'High confidence: exact PO number match', medium: 'Medium confidence: weighted score ≥ 0.75', low: 'Low confidence: weighted score below 0.75 - verify manually' }[conf]
    + (m.matchScore != null ? ' (score ' + m.matchScore.toFixed(2) + ')' : '');
  out += ' <span class="conf-badge conf-' + conf + '" title="' + escapeHtml(confTitle) + '">' + conf + '</span>';

  const qtyFlag = m.qtyDiffPct != null && m.qtyDiffPct > FLAG_PCT;
  const rateFlag = m.rateDiffPct != null && m.rateDiffPct > FLAG_PCT;
  const uomFlag = !!m.uomMismatch;
  // See matchStatusHtml()'s own comment (fix 3.F) - value flags per the
  // backend's epsilon-aware isFlagged, not a client-side re-derivation.
  const valueFlag = !!m.isFlagged && !qtyFlag && !rateFlag && !uomFlag;
  const anyFlag = qtyFlag || rateFlag || valueFlag || uomFlag;
  const dismissedCls = m.dismissedByOverride ? ' dismissed' : '';
  const severityCls = m.severity ? ' sev-' + m.severity : '';
  if (qtyFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Quantity mismatch in MIR: qty (as per BOE) differs from MIR qty by ' + m.qtyDiffPct.toFixed(2) + '% - verify manually">qty Δ' + m.qtyDiffPct.toFixed(1) + '%</span>';
  if (rateFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Rate mismatch in MIR: rate (converted to INR) differs from MIR rate by ' + m.rateDiffPct.toFixed(2) + '% - verify manually">rate Δ' + m.rateDiffPct.toFixed(1) + '%</span>';
  if (uomFlag) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="UOM mismatch in MIR: quantity is recorded in a different unit family on each side - not directly comparable, verify manually">uom mismatch</span>';
  if (valueFlag && m.valueDiffPct != null) out += ' <span class="flag-badge' + dismissedCls + severityCls + '" title="Value mismatch in MIR: value differs from MIR value by ' + m.valueDiffPct.toFixed(2) + '%, beyond the rounding epsilon and not explained by qty - verify manually">value Δ' + m.valueDiffPct.toFixed(1) + '%</span>';

  if (anyFlag && m.dismissedByOverride) {
    out += ' <span class="dismissed-tag" title="' + escapeHtml('Dismissed' + (m.dismissedReason ? ': ' + m.dismissedReason : '')) + '">dismissed</span>';
    if (canEditField(plantKey)) out += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="import-po-mir" data-plant="' + plantKey + '" data-dismiss="false">reinstate</span>';
  } else if (anyFlag && canEditField(plantKey)) {
    out += ' <span class="dismiss-link" data-match-id="' + m.matchId + '" data-match-type="import-po-mir" data-plant="' + plantKey + '" data-dismiss="true">dismiss</span>';
  }
  if (m.stockMatched) out += ' <span class="conf-badge conf-high" title="The MIR entry this item matched to also has a Stock match">stocked</span>';
  return out;
}

// MIR<->Stock equivalent of matchStatusHtml() above, for a Stock lot's
// `mirStockMatches` (apps/api/routers/*_views.py's _lot_dict()) - a lot can
// carry more than one MIR<->Stock pairing, so this renders one badge per
// flagged match rather than a single blended one.
function mirStockMatchHtml(lot, plantKey) {
  const matches = lot.mirStockMatches || [];
  const flagged = matches.filter(m => m.isFlagged);
  if (!flagged.length) return matches.length ? '<span class="conf-badge conf-high">matched</span>' : '-';
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
  }).join(' ');
}

// Wires every .dismiss-link under `container` (rendered by matchStatusHtml()
// above, or the equivalent MIR<->Stock badge in the materials modal, or
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
  return '<div class="field-block" style="margin-bottom:8px;' + (dismissed ? 'opacity:.6;' : '') + '">' +
    flagIconHtml(categoryColor(c.label), 'row-flag-icon') +
    ' <span class="lg-label ' + c.severity + '">' + (c.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span> ' +
    '<b' + (dismissed ? ' style="text-decoration:line-through;"' : '') + '>' + escapeHtml(c.label) + '</b>' + dismissTag + link +
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
  if (po.qtyDiscrepancy) cats.push({ label: 'Qty Mismatch (PO vs BOE)', severity: 'critical' });
  if ((po.items || []).some(i => i.mirMatch && i.mirMatch.qtyDiffPct > 0)) cats.push({ label: 'Qty Mismatch in MIR (BOE vs MIR)', severity: 'critical' });
  if ((po.items || []).some(i => i.mirMatch && i.mirMatch.rateDiffPct > 0)) cats.push({ label: 'Rate Mismatch in MIR (BOE vs MIR)', severity: 'critical' });
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
  return '<div class="field-block" style="margin-bottom:10px;' + (dismissed ? 'opacity:.6;' : '') + '">' +
    '<span class="status-pill status-overdue">' + escapeHtml(f.code) + '</span>' + dismissTag + link +
    '<div style="margin-top:8px;font-size:13px;' + (dismissed ? 'text-decoration:line-through;' : '') + '">' + escapeHtml(f.message) + '</div>' +
    '<div style="margin-top:6px;font-size:11px;color:var(--slate-soft);">Fields: ' + escapeHtml((f.fields || []).join(', ')) + '</div>' +
    (f.item_id ? '<div style="margin-top:4px;font-size:11px;color:var(--slate-soft);">Item: ' + escapeHtml(f.item_id) + '</div>' : '') +
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
  return '<div class="field-block" style="margin-bottom:8px;' + (dismissed ? 'opacity:.6;' : '') + '">' +
    flagIconHtml(KPI_FLAG_COLORS.critical, 'row-flag-icon') +
    ' <span class="lg-label critical">CRITICAL</span> ' +
    '<b' + (dismissed ? ' style="text-decoration:line-through;"' : '') + '>' + escapeHtml(m._plantLabel) + ' &middot; MIR&harr;Stock Mismatch</b>: ' +
    escapeHtml(parts.join(', ') || 'flagged') + dismissTag + link +
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
  return '<div class="field-block" style="margin-bottom:8px;">' +
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
  { label: 'Quantity Mismatch in MIR', severity: 'critical', meaning: 'A line item’s received quantity (from the matched MIR entry) does not exactly match the PO’s ordered quantity - any difference at all counts, there is no tolerance (e.g. 999kg received against a 1000kg order still flags). Can mean a short shipment, an over shipment, or a receipt logged against the wrong PO.' },
  { label: 'Rate / Value Mismatch in MIR', severity: 'critical', meaning: 'A line item’s received rate or value (from the matched MIR entry) does not exactly match the PO’s rate or value - any difference at all counts, there is no tolerance. Can mean a price change was not reflected on the PO, a tax calculation difference, or a billing error.' },
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
  'Rate / Value Mismatch in MIR': '#dc2626',
  'Tax calculation or labeling mismatch': '#d97706',
  'Vendor GSTIN anomaly': '#d97706',
  'Misfiled: wrong plant or company': '#6366f1',
  'Duplicate file or PO': '#6366f1',
  'Revision or superseded PO conflict': '#6366f1',
  'Import PO filed in Domestic folder': '#6366f1',
  'Header or template data error': '#64748b',
  'Missing or blank field': '#64748b',
  'Vendor code scheme inconsistency': '#64748b',
  'Non raw material or different category': '#64748b',
  'Delivery date anomaly': '#2563eb',
};
const DEFAULT_CATEGORY_COLOR = '#7c3aed'; // 'Other data quality issue' + any unmapped label
function categoryColor(label) { return CATEGORY_COLORS[label] || DEFAULT_CATEGORY_COLOR; }

function renderLegendHtml() {
  const rows = DISCREPANCY_LEGEND.map(d => '<li class="legend-item"><span class="lg-dot" style="background:' + categoryColor(d.label) + '"></span><span class="lg-label ' + d.severity + '">' + (d.severity === 'critical' ? 'CRITICAL' : 'INFO') + '</span><b>' + escapeHtml(d.label) + '</b>: ' + escapeHtml(d.meaning) + '</li>').join('');
  const infoCount = DISCREPANCY_LEGEND.length - 2;
  return '<div class="legend-box" id="legendBox"' + (state.legendOpen ? '' : ' hidden') + '>' +
    '<h4>What each flag means (' + DISCREPANCY_LEGEND.length + ' categories total: 2 critical, ' + infoCount + ' informational)</h4>' +
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
  po._qtyFlag = items.some(it => it.qtyDiffPct != null && it.qtyDiffPct > FLAG_PCT);
  po._rateFlag = items.some(it => (it.rateDiffPct != null && it.rateDiffPct > FLAG_PCT) || (it.valueDiffPct != null && it.valueDiffPct > FLAG_PCT));
  // Largest single diff percentage across every line item (qty/rate/value
  // alike) - drives rowTintClass()'s severity-scaled row background in the
  // "View all" table/top-5 preview, so a reviewer's eye is pulled toward the
  // worst offenders instead of every flagged row reading identically. Only
  // meaningful when _qtyFlag/_rateFlag is actually true - a PO with no flag
  // may still have a small nonzero diff sitting under FLAG_PCT's zero-
  // tolerance threshold that rounds to 0.00% and shouldn't drive any tint.
  const allDiffs = items.flatMap(it => [it.qtyDiffPct, it.rateDiffPct, it.valueDiffPct]).filter(v => v != null);
  po._maxDiffPct = allDiffs.length ? Math.max(...allDiffs) : 0;
  const cats = new Map();
  if (po._qtyFlag) cats.set('Quantity Mismatch in MIR', { label: 'Quantity Mismatch in MIR', severity: 'critical' });
  if (po._rateFlag) cats.set('Rate / Value Mismatch in MIR', { label: 'Rate / Value Mismatch in MIR', severity: 'critical' });
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
function computeStatus(po) {
  const items = po.items || [];
  if (!items.length) return 'pending';
  const matchedCount = items.filter(it => it.matched).length;
  if (matchedCount === items.length) return 'received';
  if (matchedCount > 0) return 'partial';
  const dates = items.map(it => it.deliveryDate).filter(Boolean).sort();
  if (!dates.length) return 'unknown';
  const dd = new Date(dates[0] + 'T00:00:00');
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return (!isNaN(dd.getTime()) && dd < today) ? 'overdue' : 'pending';
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

// Refocuses (and restores cursor position on) whatever [data-cf] header
// filter input had focus before a full innerHTML re-render blew it away -
// renderPoList() rebuilds the whole subtree on every keystroke of a text
// filter, which would otherwise kick focus out after the first character
// typed. Wrap any state-mutating handler that triggers a re-render while a
// filter input might be focused: preserveFocus(el, () => { ...; renderPoList(el); }).
function preserveFocus(container, renderFn) {
  const active = document.activeElement;
  const cf = active && active.dataset ? active.dataset.cf : null;
  const selStart = cf && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  const selEnd = cf && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;
  renderFn();
  if (!cf) return;
  const restored = container.querySelector('[data-cf="' + cf + '"]');
  if (!restored) return;
  restored.focus();
  if (selStart != null && restored.setSelectionRange) {
    try { restored.setSelectionRange(selStart, selEnd); } catch (e) { /* not a text-selectable input (date/number/select) */ }
  }
}

