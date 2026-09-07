// review.html's page-bootstrap script - see review.html's own header
// comment for why this is deliberately self-contained (its own fetch
// wrapper, no dependency on js/main.js's per-plant caches/modal).

// CURRENT_BATCH holds the batch _BATCH_SIZE (review_views.py) matches
// currently on screen, one .review-card per entry - reviewing one no longer
// immediately fetches a replacement (project owner, 2026-09-07: "add 5
// instead of 1" for throughput); the next batch is only fetched once every
// card in the current one has a verdict recorded (see maybeLoadNextBatch()).
let CURRENT_BATCH = [];
let REVIEWED_COUNT = 0;

(async function () {
  const user = await requireAuth();
  if (!user) return;
  renderNavTabs(document.getElementById('navTabs'), 'review');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
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

function fieldRow(label, value) {
  return '<div class="review-field"><span class="label">' + escapeHtml(label) + '</span><span class="value">' + escapeHtml(value == null ? '-' : String(value)) + '</span></div>';
}

function sideHtml(title, side) {
  return '<div class="review-side"><h4>' + escapeHtml(title) + '</h4>' +
    fieldRow('Description', side.description) +
    fieldRow('Qty', side.qty) +
    fieldRow('Rate', side.rate != null ? formatInr(side.rate) : null) +
    fieldRow('Value', side.value != null ? formatInr(side.value) : null) +
    fieldRow('Vendor', side.vendor) +
    '</div>';
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

  return '<div class="review-card" id="reviewCard' + idx + '" data-idx="' + idx + '">' +
      '<div class="review-meta">' + metaBits.join('') + '</div>' +
      '<div class="review-sides">' + sideHtml('Left (PO / MIR)', data.left) + sideHtml('Right (MIR / Stock)', data.right) + '</div>' +
      '<div class="review-diffs">' +
        diffPill('qty', data.qtyDiffPct) + diffPill('rate', data.rateDiffPct) + diffPill('value', data.valueDiffPct) +
      '</div>' +
      '<div class="review-actions" id="reviewActions' + idx + '">' +
        '<button type="button" class="review-btn correct" data-verdict="correct">&#10003; Correct</button>' +
        '<button type="button" class="review-btn incorrect" data-verdict="incorrect">&#10007; Incorrect</button>' +
        '<button type="button" class="review-btn unsure" data-verdict="unsure">? Unsure</button>' +
      '</div>' +
      '<textarea class="review-note" id="reviewNote' + idx + '" placeholder="Optional note..."></textarea>' +
    '</div>';
}

function updateProgress() {
  const remaining = CURRENT_BATCH.filter(m => !m._verdict).length;
  document.getElementById('reviewProgress').textContent =
    REVIEWED_COUNT + ' reviewed this session' + (remaining ? ' · ' + remaining + ' left in this batch' : '');
}

async function loadNext() {
  const area = document.getElementById('reviewArea');
  const progress = document.getElementById('reviewProgress');
  progress.textContent = 'Loading next batch...';
  try {
    const data = await apiReview('/next');
    if (data.done) {
      CURRENT_BATCH = [];
      progress.textContent = REVIEWED_COUNT + ' reviewed this session.';
      area.innerHTML = '<div class="review-done">Every current match has been reviewed. Nice work - run <code>manage.py report_match_accuracy</code> to see where things stand.</div>';
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
    });
  } catch (e) {
    area.innerHTML = '<div class="review-error">Could not load the next batch: ' + escapeHtml(e.message) + '</div>';
    progress.textContent = '';
  }
}

async function submitVerdict(idx, verdict) {
  const match = CURRENT_BATCH[idx];
  if (!match || match._verdict) return;
  const noteEl = document.getElementById('reviewNote' + idx);
  const note = noteEl ? noteEl.value : '';
  const actionsEl = document.getElementById('reviewActions' + idx);
  const buttons = actionsEl.querySelectorAll('.review-btn');
  buttons.forEach(b => b.disabled = true);
  try {
    await apiReview('', {
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
    REVIEWED_COUNT += 1;
    showToast('Recorded: ' + verdict);
    // Leave the reviewed card visible (buttons stay disabled, its verdict
    // shown) rather than removing it - a reviewer working through 5 at once
    // benefits from seeing what they've already done in this batch, same
    // reasoning a form doesn't erase a field the instant you fill it in.
    if (noteEl) noteEl.disabled = true;
    const card = document.getElementById('reviewCard' + idx);
    if (card) card.classList.add('reviewed-' + verdict);
    updateProgress();
    if (CURRENT_BATCH.every(m => m._verdict)) await loadNext();
  } catch (e) {
    buttons.forEach(b => b.disabled = false);
    showToast('Could not save: ' + e.message);
  }
}

let toastTimer = null;
function showToast(text) {
  const el = document.getElementById('reviewToast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}
