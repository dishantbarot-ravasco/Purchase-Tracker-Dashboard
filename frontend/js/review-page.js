// review.html's page-bootstrap script - see review.html's own header
// comment for why this is deliberately self-contained (its own fetch
// wrapper, no dependency on js/main.js's per-plant caches/modal).

let CURRENT_MATCH = null;
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

async function loadNext() {
  const area = document.getElementById('reviewArea');
  const progress = document.getElementById('reviewProgress');
  progress.textContent = 'Loading next match...';
  try {
    const data = await apiReview('/next');
    if (data.done) {
      CURRENT_MATCH = null;
      progress.textContent = REVIEWED_COUNT + ' reviewed this session.';
      area.innerHTML = '<div class="review-done">Every current match has been reviewed. Nice work - run <code>manage.py report_match_accuracy</code> to see where things stand.</div>';
      return;
    }
    CURRENT_MATCH = data;
    progress.textContent = REVIEWED_COUNT + ' reviewed this session.';
    const metaBits = [
      '<span><b>Plant:</b> ' + escapeHtml(data.plantLabel) + '</span>',
      '<span><b>Type:</b> ' + escapeHtml(data.matchTypeLabel) + '</span>',
    ];
    if (data.tier) metaBits.push('<span><b>Tier:</b> ' + escapeHtml(data.tier) + '</span>');
    if (data.matchScore != null) metaBits.push('<span><b>Score:</b> ' + data.matchScore.toFixed(4) + '</span>');

    area.innerHTML =
      '<div class="review-card">' +
        '<div class="review-meta">' + metaBits.join('') + '</div>' +
        '<div class="review-sides">' + sideHtml('Left (PO / MIR)', data.left) + sideHtml('Right (MIR / Stock)', data.right) + '</div>' +
        '<div class="review-diffs">' +
          diffPill('qty', data.qtyDiffPct) + diffPill('rate', data.rateDiffPct) + diffPill('value', data.valueDiffPct) +
        '</div>' +
        '<div class="review-actions">' +
          '<button type="button" class="review-btn correct" data-verdict="correct">&#10003; Correct</button>' +
          '<button type="button" class="review-btn incorrect" data-verdict="incorrect">&#10007; Incorrect</button>' +
          '<button type="button" class="review-btn unsure" data-verdict="unsure">? Unsure</button>' +
        '</div>' +
        '<textarea class="review-note" id="reviewNote" placeholder="Optional note..."></textarea>' +
      '</div>';

    area.querySelectorAll('.review-btn').forEach(btn => {
      btn.onclick = () => submitVerdict(btn.dataset.verdict);
    });
  } catch (e) {
    area.innerHTML = '<div class="review-error">Could not load the next match: ' + escapeHtml(e.message) + '</div>';
    progress.textContent = '';
  }
}

async function submitVerdict(verdict) {
  if (!CURRENT_MATCH) return;
  const note = (document.getElementById('reviewNote') || {}).value || '';
  const buttons = document.querySelectorAll('.review-btn');
  buttons.forEach(b => b.disabled = true);
  try {
    await apiReview('', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plant: CURRENT_MATCH.plant,
        matchType: CURRENT_MATCH.matchType,
        matchId: CURRENT_MATCH.matchId,
        verdict,
        note,
      }),
    });
    REVIEWED_COUNT += 1;
    showToast('Recorded: ' + verdict);
    await loadNext();
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
