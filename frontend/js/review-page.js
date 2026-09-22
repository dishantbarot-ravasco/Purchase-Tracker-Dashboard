// review.html's page-bootstrap script - see review.html's own header
// comment for why this is deliberately self-contained (its own fetch
// wrapper, no dependency on js/main.js's per-plant caches/modal).

// CURRENT_BATCH holds the batch _BATCH_SIZE (review_views.py) matches
// currently on screen, one .review-card per entry - reviewing one no longer
// immediately fetches a replacement (project owner, 2026-09-07: "add 5
// instead of 1" for throughput); the next batch is fetched when the reviewer
// asks for it, once every card in the current one has a verdict (see
// renderBatchFooter()).
let CURRENT_BATCH = [];
let REVIEWED_COUNT = 0;
// Progress against the programme's own ~200-match sample target, from the
// server (review_views.py's _progress()) - an open-ended "12 reviewed this
// session" counter gives a reviewer no reason to do the next five.
let PROGRESS = null;
// The card keyboard verdicts apply to: the first one in the batch without a
// verdict. Mouse users never see it do anything except carry a highlight.
let ACTIVE_IDX = 0;

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'review');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
  initViewTabs();
  initKeyboard();
  await loadNext();
})();

/** Cross-plant fetch wrapper for /api/review/... - same 401-redirect and
 * JSON-parse-guard shape as shared.js's apiForPlant()/main.js's apiImports(),
 * but review has no plant prefix (it spans all 3 plants from one screen). */
async function apiReview(path, opts) {
  const res = await fetch('/api/review' + path, opts || {});
  if (res.status === 401) { window.location.href = '/login.html'; throw new Error('Not authenticated'); }
  let data;
  try {
    data = await res.json();
  } catch (e) {
    const err = new Error('The server sent an unexpected response. Please try again, or contact IT if this keeps happening.');
    err.status = res.status;
    throw err;
  }
  if (!res.ok) {
    const err = new Error(data.error || data.detail || 'Something went wrong. Please try again.');
    err.status = res.status;
    throw err;
  }
  return data;
}

// ── Formatting ────────────────────────────────────────────────────────────

// DELIBERATELY NOT shared.js's formatInr() on this page. That helper is the
// dashboard's KPI shorthand - it rounds to whole rupees and abbreviates at
// a lakh ("₹1.20 L"), which is right for a headline number and wrong here:
// a reviewer deciding whether ₹12.4567 and ₹12.46 are the same rate saw
// "₹12" on both sides, i.e. the exact comparison the verdict depends on was
// rounded away before it reached them. Full precision, en-IN grouping.
function formatMoneyExact(n) {
  if (n == null || isNaN(n)) return '-';
  const sign = n < 0 ? '-' : '';
  return sign + '₹' + Math.abs(Number(n)).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function formatNumber(n) {
  if (n == null || isNaN(n)) return '-';
  return Number(n).toLocaleString('en-IN', { maximumFractionDigits: 3 });
}

/** Precision/recall/F1 come back as 0-1 floats or null. Null is "not
 * computable from this sample" (nothing judged either way yet), which is a
 * different statement from 0.000 and must not render as one. */
function formatScore(value) {
  return value == null ? '&ndash;' : (value * 100).toFixed(1) + '%';
}

// ── Review queue ──────────────────────────────────────────────────────────

function refValue(side, label) {
  const hit = (side.refs || []).find(r => r.label === label);
  return hit ? hit.value : null;
}

/** The identifying fields of one row - PO number, MIR number, dates, UOM,
 * item code. Rendered above the compared figures and visually quieter: they
 * are what lets a reviewer find the row in the source sheet and tell two
 * similar deliveries apart, not what they are being asked to compare.
 * `highlight` carries the labels to call out in red (currently just UOM,
 * when the two sides disagree - a qty comparison across different units is
 * meaningless and is the single easiest wrong "Correct" to click). */
function refsHtml(refs, highlight) {
  if (!refs || !refs.length) return '';
  const rows = refs.map(r => {
    let text;
    if (r.value == null || r.value === '') text = '-';
    else if (r.kind === 'date') text = formatDateIN(r.value);
    else if (r.kind === 'num') text = formatNumber(r.value);
    else text = String(r.value);
    const cls = 'review-ref' + (highlight.indexOf(r.label) >= 0 ? ' clash' : '');
    return '<div class="' + cls + '"><span class="label">' + escapeHtml(r.label) + '</span>' +
      '<span class="value">' + escapeHtml(text) + '</span></div>';
  });
  return '<div class="review-refs">' + rows.join('') + '</div>';
}

function fieldRow(label, value) {
  return '<div class="review-field"><span class="label">' + escapeHtml(label) + '</span><span class="value">' + escapeHtml(value == null ? '-' : String(value)) + '</span></div>';
}

function sideHtml(side, highlight) {
  return '<div class="review-side"><h4>' + escapeHtml(side.title) + '</h4>' +
    refsHtml(side.refs, highlight || []) +
    fieldRow('Description', side.description) +
    fieldRow('Qty', side.qty != null ? formatNumber(side.qty) : null) +
    fieldRow('Rate', side.rate != null ? formatMoneyExact(side.rate) : null) +
    fieldRow('Value', side.value != null ? formatMoneyExact(side.value) : null) +
    fieldRow('Vendor', side.vendor) +
    '</div>';
}

/** The matcher's own identification booleans, in words - see review_views.py's
 * _signal(). A reviewer who can see that a pair was built on "vendor agrees +
 * material agrees, PO number never cited" knows where to look; one shown only
 * two descriptions is being asked to re-run the algorithm by eye. */
function signalsHtml(signals) {
  if (!signals || !signals.length) return '';
  const pills = signals.map(s => {
    const icon = s.state === 'yes' ? '&#10003;' : (s.state === 'warn' ? '&#9888;' : '&#10007;');
    return '<span class="signal ' + escapeHtml(s.state) + '" title="' + escapeHtml(s.hint || '') + '">' +
      icon + ' ' + escapeHtml(s.label) + '</span>';
  });
  return '<div class="review-signals"><span class="signals-label">Why these were paired</span>' + pills.join('') + '</div>';
}

function diffPill(label, value) {
  if (value == null) return '';
  const flagged = value > 0;
  return '<span class="diff-pill' + (flagged ? ' flagged' : '') + '">' + escapeHtml(label) + ' &Delta;' + value.toFixed(2) + '%</span>';
}

function cardHtml(data, idx) {
  const metaBits = [
    '<span><b>Plant:</b> ' + escapeHtml(data.plantLabel) + '</span>',
    '<span><b>Type:</b> ' + escapeHtml(data.matchTypeLabel) + '</span>',
  ];
  if (data.tier) metaBits.push('<span><b>Tier:</b> ' + escapeHtml(data.tier) + '</span>');
  if (data.matchScore != null) metaBits.push('<span><b>Score:</b> ' + data.matchScore.toFixed(4) + '</span>');

  // A UOM disagreement between the two sides is worth calling out on both:
  // 500 KG against 500 MTR reads as perfect agreement until you notice the
  // unit. Compared here rather than server-side because it is a presentation
  // cue over two fields already on the card, not a new matching judgement -
  // MIR<->Stock's real uom_mismatch flag still arrives as its own signal.
  const leftUom = refValue(data.left, 'UOM');
  const rightUom = refValue(data.right, 'UOM');
  const uomClash = leftUom && rightUom &&
    String(leftUom).trim().toUpperCase() !== String(rightUom).trim().toUpperCase();
  const highlight = uomClash ? ['UOM'] : [];

  return '<div class="review-card" id="reviewCard' + idx + '" data-idx="' + idx + '">' +
      '<div class="review-meta">' + metaBits.join('') + '</div>' +
      '<div class="review-sides">' + sideHtml(data.left, highlight) + sideHtml(data.right, highlight) + '</div>' +
      signalsHtml(data.signals) +
      '<div class="review-diffs">' +
        diffPill('qty', data.qtyDiffPct) + diffPill('rate', data.rateDiffPct) + diffPill('value', data.valueDiffPct) +
      '</div>' +
      '<div class="review-actions" id="reviewActions' + idx + '">' +
        '<button type="button" class="review-btn correct" data-verdict="correct"><span class="key">1</span> &#10003; Correct</button>' +
        '<button type="button" class="review-btn incorrect" data-verdict="incorrect"><span class="key">2</span> &#10007; Incorrect</button>' +
        '<button type="button" class="review-btn unsure" data-verdict="unsure"><span class="key">3</span> ? Unsure</button>' +
      '</div>' +
      '<div class="review-verdict" id="reviewVerdict' + idx + '" hidden>' +
        '<span class="verdict-text"></span>' +
        '<button type="button" class="review-undo" data-idx="' + idx + '">Undo</button>' +
      '</div>' +
      '<textarea class="review-note" id="reviewNote' + idx + '" placeholder="Optional note..." aria-label="Optional note about this match"></textarea>' +
    '</div>';
}

/** Marks the first unreviewed card as the keyboard target. Purely a cue -
 * every card keeps its own three buttons, so nothing here is reachable by
 * keyboard only. */
function setActiveCard(idx, scroll) {
  ACTIVE_IDX = idx;
  document.querySelectorAll('.review-card').forEach(card => {
    card.classList.toggle('active', Number(card.dataset.idx) === idx);
  });
  if (scroll) {
    const card = document.getElementById('reviewCard' + idx);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

/** The next card to judge. Skips one whose POST is still in flight as well
 * as one already recorded: a reviewer holding "1" down sends the second
 * keypress long before the first round trip returns, and without the
 * in-flight check every one of them would land on the same card - five
 * verdicts on one match and four matches skipped. Found by driving the real
 * page from the keyboard, not by reading it. */
function firstUnreviewedIdx() {
  for (let i = 0; i < CURRENT_BATCH.length; i++) {
    if (!CURRENT_BATCH[i]._verdict && !CURRENT_BATCH[i]._pending) return i;
  }
  return -1;
}

function updateProgress() {
  const remaining = CURRENT_BATCH.filter(m => !m._verdict).length;
  document.getElementById('reviewProgress').textContent =
    REVIEWED_COUNT + ' reviewed this session' + (remaining ? ' · ' + remaining + ' left in this batch' : '');
  renderGoal();
}

/** The sample-size bar. Width is set from JS rather than a style="" attribute
 * because config/security_headers.py drops 'unsafe-inline' from style-src -
 * same reason shared.js's applyDynamicStyles() exists. */
function renderGoal() {
  const wrap = document.getElementById('reviewGoal');
  if (!PROGRESS) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const pct = PROGRESS.target ? Math.min(100, (PROGRESS.reviewed / PROGRESS.target) * 100) : 0;
  document.getElementById('reviewGoalFill').style.width = pct.toFixed(1) + '%';
  const left = Math.max(0, PROGRESS.target - PROGRESS.reviewed);
  document.getElementById('reviewGoalText').innerHTML =
    '<b>' + PROGRESS.reviewed + '</b> of ' + PROGRESS.target + ' matches reviewed' +
    (left ? ' · ' + left + ' to go' : ' · target reached');
}

function renderBatchFooter() {
  const area = document.getElementById('reviewArea');
  let footer = document.getElementById('reviewBatchFooter');
  const done = CURRENT_BATCH.length > 0 && CURRENT_BATCH.every(m => m._verdict);
  if (!done) {
    if (footer) footer.remove();
    return;
  }
  if (footer) return;
  footer = document.createElement('div');
  footer.id = 'reviewBatchFooter';
  footer.className = 'review-batch-footer';
  // The batch used to auto-advance the instant the fifth verdict landed,
  // which made Undo unreachable for that fifth card - the one most likely to
  // be a misclick, since it is the one that makes the screen jump. An
  // explicit button costs one keypress per five reviews and buys a pause
  // point where a correction is still possible.
  footer.innerHTML = '<button type="button" class="review-next-btn" id="reviewNextBtn">Next 5 matches <span class="key">N</span></button>';
  area.appendChild(footer);
  footer.querySelector('#reviewNextBtn').onclick = () => loadNext();
}

async function loadNext() {
  const area = document.getElementById('reviewArea');
  const progress = document.getElementById('reviewProgress');
  progress.textContent = 'Loading next batch...';
  try {
    const data = await apiReview('/next');
    if (data.progress) PROGRESS = data.progress;
    if (data.done) {
      CURRENT_BATCH = [];
      progress.textContent = REVIEWED_COUNT + ' reviewed this session.';
      renderGoal();
      area.innerHTML = '<div class="review-done">Every current match has been reviewed. Nice work - the <b>Accuracy</b> tab above has what the sample says.</div>';
      return;
    }
    CURRENT_BATCH = data.matches;
    updateProgress();
    area.innerHTML = CURRENT_BATCH.map((m, i) => cardHtml(m, i)).join('');
    area.querySelectorAll('.review-card').forEach(card => {
      const idx = Number(card.dataset.idx);
      card.querySelectorAll('.review-btn').forEach(btn => {
        btn.onclick = () => submitVerdict(idx, btn.dataset.verdict);
      });
      card.querySelector('.review-undo').onclick = () => undoVerdict(idx);
    });
    setActiveCard(0, false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    area.innerHTML = '<div class="review-error">Could not load the next batch: ' + escapeHtml(e.message) + '</div>';
    progress.textContent = '';
  }
}

const VERDICT_LABELS = { correct: 'Correct', incorrect: 'Incorrect', unsure: 'Unsure' };

async function submitVerdict(idx, verdict) {
  const match = CURRENT_BATCH[idx];
  // _pending is set SYNCHRONOUSLY, before the first await - see
  // firstUnreviewedIdx() for the double-submit this closes.
  if (!match || match._verdict || match._pending) return;
  match._pending = true;
  const noteEl = document.getElementById('reviewNote' + idx);
  const note = noteEl ? noteEl.value : '';
  const actionsEl = document.getElementById('reviewActions' + idx);
  const buttons = actionsEl.querySelectorAll('.review-btn');
  buttons.forEach(b => b.disabled = true);
  // Move the keyboard target on immediately rather than after the round
  // trip, so a fast reviewer's next keypress already belongs to the next
  // card instead of waiting on the network.
  // -1 clears the highlight outright once the batch is finished - leaving it
  // on the last card rings a card that is already judged.
  const next = firstUnreviewedIdx();
  setActiveCard(next, next >= 0);
  try {
    const saved = await apiReview('', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plant: match.plant,
        matchType: match.matchType,
        matchId: match.matchId,
        verdict,
        note,
      }),
    });
    match._verdict = verdict;
    match._pending = false;
    match._reviewId = saved.id;
    if (saved.progress) PROGRESS = saved.progress;
    REVIEWED_COUNT += 1;
    showToast('Recorded: ' + VERDICT_LABELS[verdict] + ' (U to undo)');
    // Leave the reviewed card visible (buttons stay disabled, its verdict
    // shown) rather than removing it - a reviewer working through 5 at once
    // benefits from seeing what they've already done in this batch, same
    // reasoning a form doesn't erase a field the instant you fill it in.
    if (noteEl) noteEl.disabled = true;
    const card = document.getElementById('reviewCard' + idx);
    if (card) card.classList.add('reviewed-' + verdict);
    const verdictEl = document.getElementById('reviewVerdict' + idx);
    verdictEl.hidden = false;
    verdictEl.querySelector('.verdict-text').textContent = 'Recorded: ' + VERDICT_LABELS[verdict];
    updateProgress();
    renderBatchFooter();
  } catch (e) {
    match._pending = false;
    buttons.forEach(b => b.disabled = false);
    setActiveCard(idx, false);
    showToast('Could not save: ' + e.message);
  }
}

/** Undo one verdict - see review_views.py's undo_review() for why this
 * deletes the row rather than recording a superseding one. */
async function undoVerdict(idx) {
  const match = CURRENT_BATCH[idx];
  if (!match || !match._verdict || !match._reviewId) return;
  try {
    const data = await apiReview('/' + match._reviewId, { method: 'DELETE' });
    if (data.progress) PROGRESS = data.progress;
    const card = document.getElementById('reviewCard' + idx);
    card.classList.remove('reviewed-' + match._verdict);
    match._verdict = null;
    match._pending = false;
    match._reviewId = null;
    REVIEWED_COUNT = Math.max(0, REVIEWED_COUNT - 1);
    document.getElementById('reviewVerdict' + idx).hidden = true;
    document.querySelectorAll('#reviewActions' + idx + ' .review-btn').forEach(b => b.disabled = false);
    const noteEl = document.getElementById('reviewNote' + idx);
    if (noteEl) noteEl.disabled = false;
    updateProgress();
    renderBatchFooter();
    setActiveCard(idx, true);
    showToast('Undone');
  } catch (e) {
    showToast('Could not undo: ' + e.message);
  }
}

/** Most recently recorded verdict still on screen - what U undoes. */
function lastReviewedIdx() {
  for (let i = CURRENT_BATCH.length - 1; i >= 0; i--) {
    if (CURRENT_BATCH[i]._verdict) return i;
  }
  return -1;
}

// ── Keyboard ──────────────────────────────────────────────────────────────

/** 1/2/3 verdicts, U undo, N next batch. The whole point of this screen is
 * ~200 judgements in as few interactions as possible; a mouse round trip to
 * one of three buttons, five times a batch, is the bulk of the work.
 * Deliberately plain keys with no modifier (this page has no other
 * single-key binding) and deliberately inert while the note textarea has
 * focus - typing "1 pallet short" in a note must not record a verdict. */
function initKeyboard() {
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input' || tag === 'select' || e.target.isContentEditable) return;
    if (document.getElementById('viewQueue').hidden) return;

    const key = e.key.toLowerCase();
    if (key === '1' || key === '2' || key === '3') {
      const verdict = { '1': 'correct', '2': 'incorrect', '3': 'unsure' }[key];
      const idx = firstUnreviewedIdx();
      if (idx >= 0) { e.preventDefault(); submitVerdict(idx, verdict); }
      return;
    }
    if (key === 'u') {
      const idx = lastReviewedIdx();
      if (idx >= 0) { e.preventDefault(); undoVerdict(idx); }
      return;
    }
    if (key === 'n') {
      const btn = document.getElementById('reviewNextBtn');
      if (btn) { e.preventDefault(); btn.click(); }
    }
  });
}

// ── View switching ────────────────────────────────────────────────────────

function initViewTabs() {
  const tabQueue = document.getElementById('tabQueue');
  const tabStats = document.getElementById('tabStats');
  tabQueue.onclick = () => showView('queue');
  tabStats.onclick = () => showView('stats');
}

function showView(name) {
  const isStats = name === 'stats';
  document.getElementById('viewQueue').hidden = isStats;
  document.getElementById('viewStats').hidden = !isStats;
  document.getElementById('tabQueue').classList.toggle('active', !isStats);
  document.getElementById('tabStats').classList.toggle('active', isStats);
  document.getElementById('tabQueue').setAttribute('aria-selected', String(!isStats));
  document.getElementById('tabStats').setAttribute('aria-selected', String(isStats));
  // Always refetched on open rather than cached: the numbers change every
  // time anyone anywhere records a verdict, and a stale precision figure is
  // worse than a half-second wait.
  if (isStats) loadStats();
}

// ── Accuracy panel ────────────────────────────────────────────────────────

async function loadStats() {
  const area = document.getElementById('statsArea');
  area.innerHTML = '<div class="review-done">Loading accuracy...</div>';
  try {
    const report = await apiReview('/stats');
    renderStats(report);
  } catch (e) {
    area.innerHTML = '<div class="review-error">Could not load accuracy: ' + escapeHtml(e.message) + '</div>';
  }
}

function kpiHtml(label, value, hint) {
  return '<div class="stat-kpi"><span class="kpi-label">' + escapeHtml(label) + '</span>' +
    '<span class="kpi-value">' + value + '</span>' +
    (hint ? '<span class="kpi-hint">' + escapeHtml(hint) + '</span>' : '') + '</div>';
}

/** One scored group per row. An n=0 row is rendered as "not sampled" rather
 * than 0% - never reviewed and measured-as-wrong are opposite statements,
 * and this table is read by people deciding where to look next. */
function statRowsHtml(rows, minSample) {
  return rows.map(row => {
    const unsampled = row.n === 0;
    const cls = unsampled ? ' class="unsampled"' : (row.smallSample ? ' class="small-sample"' : '');
    const scores = unsampled
      ? '<td colspan="3" class="not-sampled">not sampled yet</td>'
      : '<td>' + formatScore(row.precision) + '</td><td>' + formatScore(row.recall) + '</td><td>' + formatScore(row.f1) + '</td>';
    return '<tr' + cls + '><th scope="row">' + escapeHtml(row.label) +
      (!unsampled && row.smallSample ? ' <span class="small-flag" title="Fewer than ' + minSample + ' reviews - not enough sample to trust yet">small sample</span>' : '') +
      '</th>' +
      '<td>' + row.n + '</td>' +
      '<td class="c-correct">' + row.correct + '</td>' +
      '<td class="c-incorrect">' + row.incorrect + '</td>' +
      '<td class="c-unsure">' + row.unsure + '</td>' +
      scores + '</tr>';
  }).join('');
}

function statTableHtml(title, subtitle, rows, minSample) {
  return '<div class="stat-block"><h3>' + escapeHtml(title) + '</h3>' +
    (subtitle ? '<p class="stat-sub">' + escapeHtml(subtitle) + '</p>' : '') +
    '<div class="stat-table-wrap"><table class="stat-table">' +
    '<thead><tr><th scope="col">Group</th><th scope="col">n</th><th scope="col">&#10003;</th>' +
    '<th scope="col">&#10007;</th><th scope="col">?</th>' +
    '<th scope="col">Precision</th><th scope="col">Recall</th><th scope="col">F1</th></tr></thead>' +
    '<tbody>' + statRowsHtml(rows, minSample) + '</tbody></table></div></div>';
}

function renderStats(report) {
  const area = document.getElementById('statsArea');
  if (!report.reviewsRecorded) {
    area.innerHTML = '<div class="review-done">No matches have been reviewed yet, so there is nothing to measure. ' +
      'Start in the <b>Review queue</b> tab - the figures here need about ' + report.target +
      ' judgements spread across the plants before they mean much.</div>';
    return;
  }

  const o = report.overall;
  const pctOfTarget = report.target ? Math.min(100, (report.reviewedMatches / report.target) * 100) : 0;

  let html = '<p class="stat-intro">Of the matches a reviewer could judge either way, <b>precision</b> is how many were ' +
    'right. <b>Recall</b> counts an "unsure" against the total, so it is the share of everything sampled that came back a ' +
    'confident yes. These describe the matches the algorithm <i>made</i> - a pair it never proposed cannot be sampled ' +
    'here, so this is not a statement about what it missed.</p>';

  html += '<div class="stat-kpis">' +
    kpiHtml('Precision', formatScore(o.precision), o.correct + ' right of ' + (o.correct + o.incorrect) + ' judged') +
    kpiHtml('Recall', formatScore(o.recall), o.unsure + ' unsure') +
    kpiHtml('F1', formatScore(o.f1), '') +
    kpiHtml('Sample', report.reviewedMatches + ' / ' + report.target, pctOfTarget.toFixed(0) + '% of target') +
    '</div>';

  if (o.smallSample) {
    html += '<div class="stat-warn">Fewer than ' + report.minSample + ' matches reviewed in total. ' +
      'Treat every figure on this page as provisional - one verdict either way still moves them by tens of per cent.</div>';
  }
  if (report.staleVerdicts) {
    html += '<div class="stat-warn">' + report.staleVerdicts + ' verdict(s) are excluded because the match they judged no ' +
      'longer exists - a later <code>match_*</code> run deleted or re-pointed the pair.</div>';
  }

  html += statTableHtml('By plant and match type', 'The cut that says which pairing at which plant is the weak one. A cell with no sample is a hole in the evidence, not a pass.', report.byPlantAndType, report.minSample);
  html += statTableHtml('By plant', '', report.byPlant, report.minSample);
  html += statTableHtml('By match type', '', report.byMatchType, report.minSample);
  if (report.byTier.length) {
    html += statTableHtml('By match type and tier', 'Tier-1 (po_number) matches rest on the MIR citing the PO itself; weighted ones rest on the score alone.', report.byTier, report.minSample);
  }

  html += '<div class="stat-block"><h3>Who reviewed</h3><p class="stat-sub">Not a leaderboard - it answers "is this one person\'s judgement?", which changes how much the figures above are worth.</p><ul class="stat-list">' +
    report.reviewers.map(r => '<li><span>' + escapeHtml(r.reviewer) + '</span><b>' + r.count + '</b></li>').join('') +
    '</ul>' + (report.lastReviewedAt ? '<p class="stat-sub">Last reviewed ' + escapeHtml(formatDateIN(report.lastReviewedAt.slice(0, 10))) + '.</p>' : '') + '</div>';

  if (report.notes.length) {
    html += '<div class="stat-block"><h3>Reviewer notes</h3>' +
      '<p class="stat-sub">What reviewers wrote while judging. These were being written into the database and read by nobody.</p>' +
      '<ul class="stat-notes">' + report.notes.map(n =>
        '<li><div class="note-head"><span class="note-verdict ' + escapeHtml(n.verdict) + '">' + escapeHtml(VERDICT_LABELS[n.verdict] || n.verdict) + '</span>' +
        '<span class="note-where">' + escapeHtml(n.plantLabel) + ' · ' + escapeHtml(n.matchTypeLabel) + ' · #' + n.matchId + '</span>' +
        '<span class="note-who">' + escapeHtml(n.reviewer) + ' · ' + escapeHtml(formatDateIN(n.reviewedAt.slice(0, 10))) + '</span></div>' +
        '<div class="note-body">' + escapeHtml(n.note) + '</div></li>').join('') +
      '</ul></div>';
  }

  html += '<div class="stat-actions"><a class="stat-export" href="/api/review/stats/export">Download these tables as CSV</a>' +
    '<span class="stat-sub">Same figures as <code>manage.py report_match_accuracy</code>, from the same code.</span></div>';

  area.innerHTML = html;
}

// ── Toast ─────────────────────────────────────────────────────────────────

let toastTimer = null;
function showToast(text) {
  const el = document.getElementById('reviewToast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}
