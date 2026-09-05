/**
 * frontend/js/shared.js — cross-page constants and helpers.
 *
 * Loaded on every protected page (index.html, home.html, search-po.html,
 * admin.html) right after auth.js and before that page's own script. Holds
 * the one copy of things every page independently needed: the per-plant API
 * prefix map, the authenticated fetch wrapper, and the small formatting
 * helpers (escapeHtml/formatInr/formatDateIN). Previously each of these was
 * defined once inline in js/main.js only, since it was the only script that
 * needed them - now that the landing page, PO search, and admin pages all
 * need the same per-plant fetch/formatting logic, keeping one copy here
 * avoids four slightly-drifting duplicates (see CLAUDE.md's "modular"
 * requirement). No imports/exports - plain globals, same no-build-step
 * convention as every other script in this app.
 */

// ── Per-plant constants ─────────────────────────────────────────────────
const PLANTS = {
  hrs: { label: 'HRS, Silvassa', apiPrefix: '/api', hasVendorOnMaterials: true, syncCmdPoCsv: 'sync_po_csv', syncCmdStock: 'sync_stock' },
  achhad: { label: 'RTP-Achhad', apiPrefix: '/api/achhad', hasVendorOnMaterials: false, syncCmdPoCsv: 'sync_achhad_po_csv', syncCmdStock: 'sync_achhad_stock' },
  vapi: { label: 'RTP-Vapi', apiPrefix: '/api/vapi', hasVendorOnMaterials: true, syncCmdPoCsv: 'sync_vapi_po_csv', syncCmdStock: 'sync_vapi_stock' },
};
const PLANT_KEYS = Object.keys(PLANTS);
const ALL_PLANTS_LABEL = 'All Plants';

// A Stock lot's PATCH endpoint - see hrs_views.py/achhad_views.py/
// vapi_views.py's correct_material_field. Used by openMaterialModal's
// Stock by Plant table (main.js), where each row can be a different
// plant's own lot - see editableCell()'s data-plant below.
function materialFieldsUrl(plantKey, lotId) {
  return PLANTS[plantKey].apiPrefix + '/materials/' + encodeURIComponent(lotId) + '/fields';
}
// Achhad's Stock lot model names this column "rate" (its own Stock sheet
// has no vendor/uom columns at all - see RTPAchhadStockLot's docstring);
// HRS/Vapi both name it "basic_rate". Same reasoning as the backend's own
// per-plant _MATERIAL_EDITABLE_FIELDS sets - not a typo, a real per-plant
// schema difference.
function materialRateFieldName(plantKey) {
  return plantKey === 'achhad' ? 'rate' : 'basic_rate';
}

// ── API fetch helpers ───────────────────────────────────────────────────
// Every page's own fetches go through this - handles the 401-means-cookie-
// expired case identically everywhere (bounce to /login.html) instead of
// each page reinventing that check slightly differently.
/** Authenticated fetch wrapper scoped to one plant's API prefix. Redirects
 * to /login.html on a 401 (expired/missing session cookie) instead of
 * returning it to the caller, since every caller would otherwise have to
 * handle that case identically anyway. */
async function apiForPlant(plantKey, path, opts) {
  opts = opts || {};
  const res = await fetch(PLANTS[plantKey].apiPrefix + path, opts);
  if (res.status === 401) {
    window.location.href = '/login.html';
    throw new Error('Not authenticated');
  }
  // Real bug, found and fixed 2026-09-04: `await res.json()` used to run
  // unguarded here - every DRF endpoint always returns JSON (see apps/api/
  // exceptions.py's custom_exception_handler), but a response that never
  // reached Django at all (Render's edge/proxy returning a plain-text or
  // HTML error page during a deploy, a request too large/malformed to
  // reach a view, etc.) does not, and JSON.parse() on that threw a raw,
  // cryptic error ("Unexpected token '<'...") straight up to whatever
  // alert()/toast the caller shows - a confusing message for something
  // that's really just "couldn't reach the server". Every caller of this
  // function already expects a plain Error with a readable .message, so
  // this failure mode is normalized to look exactly like any other one.
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
    // Exposed so a caller can tell apart e.g. a 409 ("already in progress",
    // not really an error - see main.js's sync-trigger handling) from a
    // real failure, without parsing message text.
    err.status = res.status;
    throw err;
  }
  return data;
}

// ── Material↔PO linkage helpers ─────────────────────────────────────────
// Client-side ports of apps/services/parsers/common.py's normalize_material()/
// tokenize() and matching.py's _vendor_matches() containment check - kept in
// sync manually since the frontend never gets to run real Python (same
// "manually kept in sync" situation as main.js's own FLAG_PCT mirroring
// matching*.py's FLAG_DIFF_PCT). Used by the Raw Material Analysis view
// (frontend/js/main.js's materialLinksToItem()) to link a Stock lot to the
// PO line items that (probably) procured it - there's no persisted DB join
// for this (see that file's header comment), so it's computed here the same
// best-effort way the backend already computes PO<->MIR matches.
function normalizeMaterial(name) {
  if (!name) return '';
  return name.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean).join(' ');
}
function tokenizeMaterial(text) {
  const n = normalizeMaterial(text);
  return n ? n.split(' ') : [];
}
function normalizeVendor(name) {
  if (!name) return '';
  let n = name.toLowerCase();
  n = n.replace(/\b(private limited|pvt\.?\s*ltd\.?|pvt\.?|ltd\.?|limited|llp|inc\.?|corp(oration)?\.?|co\.?|company)\b/g, '');
  n = n.replace(/[^a-z0-9]+/g, '');
  return n.trim();
}
// Containment, not equality - same reasoning as matching.py's _vendor_matches
// (e.g. Stock sheets sometimes append a city suffix PO/MIR data doesn't
// carry). A length floor avoids a short/empty normalized name trivially
// matching everything.
function vendorContains(a, b) {
  if (!a || !b || a.length < 4 || b.length < 4) return false;
  const shorter = a.length <= b.length ? a : b;
  const longer = a.length <= b.length ? b : a;
  return longer.indexOf(shorter) !== -1;
}

// ── Password show/hide toggle ───────────────────────────────────────────
// Show/hide toggle for any password field on a protected page (project
// owner, 2026-09-05: "add eye thing whenever password is required") -
// delegated at the document level so it works for a field that doesn't
// exist in the DOM yet at page load (admin.html's create/edit-user password
// input lives inside a modal that's already static markup here, but
// delegation costs nothing and stays correct if that ever changes to a
// dynamically-rendered form). Keyed by `data-pw-target` (the input's own
// id), not a fixed selector, so the same snippet covers every password
// field on the page without hardcoding which one. Duplicated (not shared)
// in login.js's own copy - login.html is the unauthenticated pre-login page
// and deliberately loads no other scripts, so there's nothing to share this
// with; the two copies are kept in sync manually, same as this codebase's
// other deliberate small duplications (e.g. FLAG_PCT mirroring the backend's
// FLAG_DIFF_PCT).
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.pw-toggle');
  if (!btn) return;
  const input = document.getElementById(btn.dataset.pwTarget);
  if (!input) return;
  const nowShowing = input.type === 'password';
  input.type = nowShowing ? 'text' : 'password';
  btn.classList.toggle('active', nowShowing);
  const label = nowShowing ? 'Hide password' : 'Show password';
  btn.title = label;
  btn.setAttribute('aria-label', label);
});

// ── Formatting helpers ──────────────────────────────────────────────────
/** Escapes a value for safe interpolation into innerHTML. */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
// Indian numbering shorthand for large rupee amounts - 1,00,00,000+ as
// "X.XX Cr" and 1,00,000+ as "X.XX L", so a KPI/table cell reads "₹58.09 L"
// instead of "₹58,09,000". Below 1 lakh (per-unit rates, small line items)
// nothing changes - full comma-grouped value as before. This only affects
// display: every match/flag calculation elsewhere in this app still runs on
// the raw, unrounded numeric field, never on this formatted string.
function formatInr(n) {
  if (n == null || isNaN(n)) return '-';
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1e7) return sign + '₹' + (abs / 1e7).toFixed(2) + ' Cr';
  if (abs >= 1e5) return sign + '₹' + (abs / 1e5).toFixed(2) + ' L';
  return sign + '₹' + Math.round(abs).toLocaleString('en-IN');
}

// Every date reaching the frontend from the API is ISO 'YYYY-MM-DD' (Django's
// DateField.isoformat()) regardless of the source file's own format - MIR/PO/
// Stock's mixed dd/mm/yyyy, dd.mm.yyyy, etc. are already normalized to real
// date columns server-side (see apps/services/parsers/). This only reformats
// that ISO string to dd/mm/yyyy for display - it never touches stored data,
// so there is no data-corruption risk from doing this purely at render time.
function formatDateIN(iso) {
  if (!iso) return '-';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? (m[3] + '/' + m[2] + '/' + m[1]) : iso;
}

// ── Keyboard activation for div-based "buttons"/tabs ─────────────────────
// A lot of this app's clickable UI (KPI cards, the 3-level tab bars, modal
// tabs, search-result cards) is a plain <div data-*> with only a mouse
// .onclick assigned by its own render function - not a native <button>/<a>,
// so it was keyboard-invisible (no tab stop, no Enter/Space activation)
// despite being genuinely interactive. Each render site now adds
// tabindex="0" + a role attribute (see .kpi-card/.view-tab/etc in style.css
// for the matching :focus-visible ring) - this one delegated listener
// supplies the activation, so no per-file keydown wiring was needed. `role`
// isn't checked here on purpose: this just re-dispatches a real click event,
// which every one of these elements already has a working handler for.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target.closest('[data-kpi], [data-matkpi], .search-result-card, .view-tab, .plant-tab, .sub-tab, .modal-tab');
  if (!el) return;
  e.preventDefault();
  el.click();
});

// ── Empty / no-data state ────────────────────────────────────────────────
// Shared "nothing here yet" icon + message, used inside every empty-list/
// search-result panel across the app (po-list.js/materials.js/import-po.js's
// own .empty-state, search-po.html/admin.html's .search-empty) - one visual
// language instead of each call site being bare text. Callers keep their own
// outer wrapper div/class (.empty-state or .search-empty already carry the
// padding/border/centering); this just supplies the inner icon+message pair.
// `tone` picks 'muted' (default - informational, "nothing synced yet") vs
// 'error' (a real failure, e.g. "couldn't load the user list") - error tone
// swaps the icon color to --red so a real problem doesn't look identical to
// an empty-but-fine state.
function emptyStateHtml(message, tone) {
  const cls = tone === 'error' ? ' empty-state-icon-error' : '';
  return '<div class="empty-state-icon' + cls + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M3 10l2-5a2 2 0 0 1 2-1h10a2 2 0 0 1 2 1l2 5"/>' +
    '<path d="M3 10v8a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-8"/>' +
    '<path d="M3 10h5a1 1 0 0 1 1 1 3 3 0 0 0 6 0 1 1 0 0 1 1-1h5"/>' +
    '</svg></div><div class="empty-state-msg">' + message + '</div>';
}

// ── "Jump to page" control ───────────────────────────────────────────────
// Small number input + Go button appended to every paginated "View all"
// table's .pagination-row (po-list.js/materials.js/import-po.js) - the
// existing prev/next/page-number buttons top out at 10 direct page buttons
// before falling back to a plain "Page X of Y" label with no direct-jump
// affordance at all; this covers that gap regardless of result-set size.
// `idPrefix` namespaces the input/button ids so each view's own pagination
// (not simultaneously visible today, but each already namespaces its own
// prev/next/page-number ids the same way) never collides with another's.
function jumpToPageHtml(idPrefix, totalPages) {
  if (totalPages <= 1) return '';
  return '<span class="jump-to-page">' +
    '<label for="' + idPrefix + 'JumpInput">Go to</label>' +
    '<input type="number" id="' + idPrefix + 'JumpInput" min="1" max="' + totalPages + '" placeholder="1-' + totalPages + '">' +
    '<button type="button" id="' + idPrefix + 'JumpBtn" class="page-btn">Go</button>' +
  '</span>';
}

// Wires a jumpToPageHtml() control's Go button + Enter-in-input to call
// onGo(pageNumber) with a clamped/validated page number - shared so each of
// the 3 call sites doesn't reinvent parsing/validating slightly differently.
// Garbage or out-of-range input is silently ignored rather than clamped to
// the nearest valid page - a typo shouldn't unexpectedly jump somewhere the
// user didn't ask for.
function wireJumpToPage(idPrefix, totalPages, onGo) {
  const input = document.getElementById(idPrefix + 'JumpInput');
  const btn = document.getElementById(idPrefix + 'JumpBtn');
  if (!input || !btn) return;
  const go = () => {
    const n = Math.round(Number(input.value));
    if (!n || n < 1 || n > totalPages) return;
    onGo(n);
  };
  btn.onclick = go;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

// ── Info tooltip (KPI card definitions) ──────────────────────────────────
// Small "i" glyph + instant CSS tooltip (see .info-tooltip in style.css) -
// used next to a KPI card's label to explain what it actually counts/flags,
// since several of these (zero-tolerance discrepancy thresholds, tier-1 vs
// tier-2 matching, "overdue" vs "pending") are genuinely non-obvious from
// the label alone. tabindex=0 so it's reachable by keyboard focus, not only
// mouse hover - :focus-visible in the CSS shows the same tooltip.
// A KPI card's info-tooltip icon sits inside the same card element that has
// its own onclick (toggles state.statusFilter/matStatusFilter/importStatus-
// Filter - see po-list.js/materials.js/import-po.js). Clicking the icon (to
// show/dismiss the tooltip on a touch device that has no hover) must not
// also toggle that filter. Capture phase, not bubble - by the time a bubble-
// phase listener reached this far up the tree, the kpi-card's own onclick
// (attached directly on that element) would already have run.
document.addEventListener('click', (e) => {
  if (e.target.closest && e.target.closest('.info-tooltip')) e.stopPropagation();
}, true);

function infoTooltipHtml(tip) {
  return '<span class="info-tooltip kpi-info" data-tooltip="' + escapeHtml(tip) + '" tabindex="0" aria-label="' + escapeHtml(tip) + '">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><circle cx="12" cy="7.3" r=".6" fill="currentColor" stroke="none"/>' +
    '</svg></span>';
}

// ── KPI count-up ─────────────────────────────────────────────────────────
// Animates a KPI's displayed number counting up from 0 to its real value on
// load/re-render (home.html's Overview row, main.js's KPI cards) - purely a
// display animation, wired in after the real value is already known (never
// blocks or delays when the number is actually shown to a screen reader/
// no-JS fallback, since textContent is set to the final value immediately
// if prefers-reduced-motion is on). `formatter` lets a caller keep its own
// display formatting (e.g. formatInr()) rather than this always rendering a
// plain integer - called with the in-progress rounded value on every frame.
function animateCountUp(el, target, opts) {
  opts = opts || {};
  const duration = opts.duration || 700;
  const formatter = opts.formatter || (n => String(Math.round(n)));
  target = Number(target) || 0;
  if (!el || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) {
    if (el) el.textContent = formatter(target);
    return;
  }
  const start = performance.now();
  function tick(now) {
    const progress = Math.min(1, (now - start) / duration);
    // ease-out-cubic - fast start, settles gently rather than a linear count
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = formatter(target * eased);
    if (progress < 1) requestAnimationFrame(tick);
    else el.textContent = formatter(target);
  }
  requestAnimationFrame(tick);
}

// Finds every '.kpi-card .val[data-count-target]' rendered by po-list.js's/
// materials.js's own cardDef.map() and runs animateCountUp() on each, picking
// the display formatter from its 'data-count-fmt' attribute ('inr' ->
// formatInr, 'locale' -> comma-grouped, default/'int' -> plain rounded
// integer - matches KPIs that are already plain counts, e.g. Total PO's).
// Call once after the KPI row's innerHTML is set, same place each caller
// already wires its [data-kpi]/[data-matkpi] click handlers.
function wireKpiCountUps(root) {
  (root || document).querySelectorAll('.kpi-card .val[data-count-target]').forEach(v => {
    const fmt = v.dataset.countFmt;
    const formatter = fmt === 'inr' ? formatInr : fmt === 'locale' ? (n => Math.round(n).toLocaleString('en-IN')) : (n => String(Math.round(n)));
    animateCountUp(v, Number(v.dataset.countTarget), { formatter });
  });
}

// ── Inline "Edit Everywhere" ─────────────────────────────────────────────
// Shared by the Domestic PO modal (main.js's openPoModal()) and the Import
// PO modal (main.js's renderImportPoModalBody()) - each backed by its own
// plant's PATCH .../purchase-orders/<po>/fields endpoint (Domestic:
// hrs/achhad/vapi_views.correct_field; Import: imports_views.correct_field),
// but sharing one edit-widget implementation since the interaction is
// identical either way. CURRENT_USER comes from auth.js's requireAuth(),
// which must have already resolved before any of these are called.

// True only if the signed-in user may actually save a correction for
// plantKey - matches the IsEditor permission class + user_can_edit_plant()
// check the backend enforces independently, so a pencil is never rendered
// for an action that would just 403. Empty CURRENT_USER.plants means "all
// plants" (see PTUser.plants' own docstring).
function canEditField(plantKey) {
  if (!CURRENT_USER) return false;
  if (CURRENT_USER.role !== 'admin' && CURRENT_USER.role !== 'editor') return false;
  const plants = CURRENT_USER.plants || [];
  return !plants.length || plants.includes(plantKey);
}

/** Renders a static (non-editable) "Label: value" line, used for a field a
 * viewer can't edit at all, or as editableLine()'s fallback for a user who
 * lacks edit rights on that plant. */
function plainLine(label, value) {
  const display = (value === null || value === undefined || value === '') ? 'Not available' : escapeHtml(String(value));
  return '<div class="line">' + escapeHtml(label) + ': ' + display + '</div>';
}

// fieldType: 'text' (default) | 'date' (native date picker) | 'number'
// (non-negative) | 'select' (dropdown - `options` is an array of already-
// seen distinct values, see distinctFieldValues(); never hardcoded, per the
// build spec's "derive from existing data" rule). Renders a plain
// non-editable line (no pencil at all) when canEditField(plantKey) is
// false - a non-editing viewer never even sees the affordance.
//
// `data-label` carries the raw field label separately from the rendered
// "Label: value" text (which mixes both together) - selectFieldForCorrection()
// needs the label on its own for the "Correcting: X" banner, same as the
// artifact's fld() stashing `data-ov-label` for the same reason.
function editableLine(plantKey, label, value, fieldName, itemId, fieldType, options) {
  if (!canEditField(plantKey)) return plainLine(label, value);
  const display = (value === null || value === undefined || value === '') ? 'Not available' : escapeHtml(String(value));
  const optsAttr = options && options.length ? ' data-options="' + escapeHtml(encodeURIComponent(JSON.stringify(options))) + '"' : '';
  // data-plant lets a container whose lines span more than one plant (the
  // material modal's Overview tab, once its Category/Sub Category lines
  // target the anchor lot's own plant) resolve each line's own fieldsUrl -
  // see wireEditIcons()'s function-form fieldsUrl and editableCell()'s
  // identical attribute below. Harmless/unused for every other existing
  // caller (Domestic/Import PO modals), which always pass one fixed
  // fieldsUrl string for their whole container.
  return '<div class="line editable-line" data-field="' + escapeHtml(fieldName) + '"' +
    (itemId ? ' data-item="' + escapeHtml(itemId) + '"' : '') +
    ' data-plant="' + escapeHtml(plantKey) + '"' +
    ' data-label="' + escapeHtml(label) + '"' +
    ' data-field-type="' + (fieldType || 'text') + '"' + optsAttr + '>' +
    escapeHtml(label) + ': <span class="line-val">' + display + '</span>' +
    '<span class="edit-pencil" title="Correct this field">&#9998;</span>' +
  '</div>';
}

// ── Dismiss/override a flagged match ────────────────────────────────────
// Backs the "Dismiss"/"Reinstate" control matchStatusHtml() renders next to
// a flagged PO<->MIR badge (main.js) and the equivalent MIR<->Stock control
// in the Raw Material Analysis modal - both PATCH the same shape of
// endpoint (.../matches/po-mir/<id>/dismiss or .../matches/mir-stock/<id>/
// dismiss, see apps/services/match_dismiss.py), so one shared helper here
// instead of two near-duplicates.
async function dismissMatch(plantKey, matchType, matchId, dismissed, reason) {
  const body = JSON.stringify({ dismissed: dismissed, reason: reason || '' });
  const opts = { method: 'PATCH', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body };
  // Import PO<->MIR matches live under the cross-plant /api/imports router
  // (imports_views.py - see that module's docstring for why imports aren't
  // split per-plant like Domestic's matches/po-mir/<id> is), with `plant`
  // as a path segment rather than an apiPrefix - apiImports() (main.js) is
  // the matching fetch wrapper for that router, used here instead of
  // apiForPlant()'s per-plant prefix which wouldn't resolve to the right URL.
  if (matchType === 'import-po-mir') {
    return apiImports('/matches/po-mir/' + encodeURIComponent(plantKey) + '/' + encodeURIComponent(matchId) + '/dismiss', opts);
  }
  return apiForPlant(plantKey, '/matches/' + encodeURIComponent(matchType) + '/' + encodeURIComponent(matchId) + '/dismiss', opts);
}

// Manual dismiss/reinstate for a PO-level flag (Quantity/Rate-Value
// Discrepancy, Data Quality Flag category) in a PO detail modal's Flags &
// Corrections tab - see apps/services/flag_dismiss.py. `isImport` picks the
// cross-plant /api/imports router (plant as a path segment) the same way
// dismissMatch()'s matchType==='import-po-mir' branch does, since Import
// POs live under a different URL shape than Domestic's per-plant prefix.
async function dismissPoFlag(plantKey, poNumber, flagKey, dismissed, reason, isImport) {
  const body = JSON.stringify({ flagKey: flagKey, dismissed: dismissed, reason: reason || '' });
  const opts = { method: 'PATCH', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body };
  if (isImport) {
    return apiImports('/purchase-orders/' + plantKey + '/' + encodeURIComponent(poNumber) + '/flags/dismiss', opts);
  }
  return apiForPlant(plantKey, '/purchase-orders/' + encodeURIComponent(poNumber) + '/flags/dismiss', opts);
}

// Distinct, already-seen values for a field across an already-loaded PO
// list - backs 'select' fieldType dropdowns without a dedicated backend
// endpoint or any hardcoded option list (build spec: "derive the option
// list from existing data so new plants/vendors don't break it").
function distinctFieldValues(list, accessor) {
  const seen = new Set();
  (list || []).forEach(row => {
    const v = accessor(row);
    if (v !== null && v !== undefined && v !== '') seen.add(String(v));
  });
  return Array.from(seen).sort();
}

// `fieldsUrl` is the full PATCH endpoint for this PO - callers build it
// differently (Domestic: PLANTS[key].apiPrefix + '/purchase-orders/<po>/fields';
// Import: '/api/imports/purchase-orders/<plant>/<po>/fields', since Import's
// URL carries an explicit plant segment Domestic's per-plant-prefixed one
// doesn't need) so this stays URL-shape-agnostic. `reason` (2026-09-05,
// project owner: corrections should carry a reason "just like in that
// artifact") is optional free text explaining why the old value was wrong -
// stored on the DomesticPOCorrection/ImportPOCorrection/MaterialCorrection
// audit row alongside old/new value (see each *_views.py's correct_field/
// correct_material_field).
async function savePoField(fieldsUrl, itemId, field, value, reason) {
  const res = await fetch(fieldsUrl, {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ itemId: itemId || '', field: field, value: value, reason: reason || '' }),
  });
  if (res.status === 401) {
    window.location.href = '/login.html';
    throw new Error('Not authenticated');
  }
  // See apiForPlant()'s own comment (above in this file) for why res.json()
  // is guarded here - same fix, same reasoning.
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

// Compact inline sibling of editableLine() for a table cell (the Raw
// Material Analysis modal's Stock by Plant table renders one row per Stock
// lot, each cell needing just "value + pencil", not editableLine()'s
// block-level "Label: value" line) - see .cell-editable in style.css for why
// this needed its own CSS instead of reusing .editable-line's absolute
// positioning. `itemId` here is a lot id, `plantKey` is that row's own
// plant (materials can list lots from all 3 plants in one table - see
// openMaterialModal's siblingLots), stashed on the element as data-plant so
// a per-row caller can build the right per-plant fieldsUrl. `label` (added
// 2026-09-05 alongside the correction-panel rework below) disambiguates
// which row/plant a correction targets in the "Correcting: X" banner, since
// a table cell has no adjacent "Label:" text the way editableLine()'s block
// line does - callers should pass something like 'Category (RTP-Vapi)'.
function editableCell(plantKey, label, value, fieldName, itemId, fieldType, options) {
  const display = (value === null || value === undefined || value === '') ? '-' : escapeHtml(String(value));
  if (!canEditField(plantKey)) return display;
  const optsAttr = options && options.length ? ' data-options="' + escapeHtml(encodeURIComponent(JSON.stringify(options))) + '"' : '';
  return '<span class="editable-line cell-editable" data-field="' + escapeHtml(fieldName) + '" data-item="' + escapeHtml(String(itemId)) + '" data-plant="' + escapeHtml(plantKey) + '"' +
    ' data-label="' + escapeHtml(label) + '"' +
    ' data-field-type="' + (fieldType || 'text') + '"' + optsAttr + '>' +
    '<span class="line-val">' + display + '</span>' +
    '<span class="edit-pencil" title="Correct this field">&#9998;</span>' +
  '</span>';
}

// ── "Submit a Correction" panel ─────────────────────────────────────────
// Ported from the original "Purchase Tracker" Claude Artifact's fld()/
// override-box UX (project owner, 2026-09-05: "the edit icon should be
// taking us to flag and correction with reason, just like in that
// artifact, not up to the point yet"). Clicking a field's pencil no longer
// swaps it into an inline input the way startFieldEdit() used to - it
// selects that field and jumps to wherever this modal's own "Submit a
// Correction" panel lives (the Flags & Corrections tab for the Domestic/
// Import PO modals; the Stock by Plant tab for the Material modal, which
// has no separate Flags tab - see that modal's own flags-box placement),
// mirroring the artifact's fld()/ov*/submitOverride() flow structurally.
// The real difference: this panel actually PATCHes the live row (a real
// write path already existed here before this pass, see CLAUDE.md's
// "Inline 'Edit Everywhere'") and records the entered reason on the real
// correction audit row, instead of the artifact's fake "queues a Drive
// file for manual review" behavior.
//
// Only one modal is ever open at a time in this app, so SELECTED_FIELD is
// deliberately one shared module-level variable, not per-modal state -
// same simplification the artifact's own single global `ovSelectedLabel`
// made.
let SELECTED_FIELD = null;

/** Markup for the override box itself - identical shape across every modal
 * that has one (po-modal.js/import-po.js's Flags & Corrections tab,
 * material-modal.js's Stock by Plant tab), only `hintText` differs since
 * each modal's fields live on a different tab. */
function overrideBoxHtml(hintText) {
  return '<div class="override-box">' +
    '<h4>Submit a Correction</h4>' +
    '<div class="ov-hint" id="ovHint">' + escapeHtml(hintText) + '</div>' +
    '<div class="ov-selected-field" id="ovSelectedField" hidden></div>' +
    '<div class="row" id="ovValueRow"></div>' +
    '<textarea id="ovReason" placeholder="Why is this wrong / what did you verify it against? (optional)"></textarea>' +
    '<div class="row" style="margin-top:8px;"><button id="ovSubmit" disabled>Save Correction</button></div>' +
    '<div id="ovStatus"></div>' +
  '</div>';
}

/** Builds the right input control for `field.fieldType` into #ovValueRow -
 * same select/date/number/text branch startFieldEdit() used to build
 * inline, just targeting the override box instead of the field's own line. */
function renderOverrideValueInput(field) {
  const row = document.getElementById('ovValueRow');
  if (!row) return;
  row.innerHTML = '';
  let input;
  if (field.fieldType === 'select') {
    input = document.createElement('select');
    const opts = field.options || [];
    const withCurrent = field.currentValue && opts.indexOf(field.currentValue) === -1 ? [field.currentValue].concat(opts) : opts;
    withCurrent.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      if (opt === field.currentValue) o.selected = true;
      input.appendChild(o);
    });
  } else {
    input = document.createElement('input');
    input.type = field.fieldType === 'date' ? 'date' : (field.fieldType === 'number' ? 'number' : 'text');
    if (field.fieldType === 'number') input.min = '0';
    input.value = field.currentValue || '';
  }
  input.id = 'ovValue';
  row.appendChild(input);
  input.focus();
  if (input.select && field.fieldType !== 'select') input.select();
}

/** Selects one field for correction: stashes it on SELECTED_FIELD and
 * updates the override box's hint/selected-field banner + value input +
 * Submit button. Does NOT switch tabs itself - the caller (wireEditIcons()'s
 * pencil handler) does that immediately after, since only it knows which
 * tab id this modal should jump to. */
function selectFieldForCorrection(field) {
  SELECTED_FIELD = field;
  const hintEl = document.getElementById('ovHint');
  const selEl = document.getElementById('ovSelectedField');
  const reasonEl = document.getElementById('ovReason');
  const statusEl = document.getElementById('ovStatus');
  const submitBtn = document.getElementById('ovSubmit');
  if (!hintEl || !selEl) return; // this modal has no override box in the DOM (shouldn't happen for an edit-enabled field)
  hintEl.hidden = true;
  selEl.hidden = false;
  selEl.textContent = 'Correcting: ' + field.label + ' (currently: ' + (field.currentValue || 'Not available') + ')';
  if (reasonEl) reasonEl.value = '';
  if (statusEl) { statusEl.textContent = ''; statusEl.className = ''; }
  if (submitBtn) submitBtn.disabled = false;
  renderOverrideValueInput(field);
}

/** Wires the override box's Submit button, once per modal render. PATCHes
 * via savePoField() using whatever field is currently selected (set by
 * selectFieldForCorrection()), then calls that field's own `onSaved`
 * callback - each PO-type's caller re-fetches/re-renders differently
 * (Domestic invalidates the whole-plant list cache; Import invalidates its
 * own list + per-PO detail cache; Material invalidates every plant's
 * materials cache), so that stays a callback instead of baked in here. */
function wireOverrideBox(root) {
  const btn = root.querySelector('#ovSubmit');
  if (!btn) return;
  btn.onclick = async () => {
    if (!SELECTED_FIELD) return;
    const valueEl = document.getElementById('ovValue');
    const reasonEl = document.getElementById('ovReason');
    const statusEl = document.getElementById('ovStatus');
    const value = valueEl ? valueEl.value : '';
    const reason = reasonEl ? reasonEl.value.trim() : '';
    btn.disabled = true;
    statusEl.className = '';
    statusEl.textContent = 'Saving…';
    try {
      const result = await savePoField(SELECTED_FIELD.fieldsUrl, SELECTED_FIELD.itemId, SELECTED_FIELD.fieldName, value, reason);
      statusEl.className = 'override-status ok';
      statusEl.textContent = 'Saved.' + (result.warning ? ' ' + result.warning : '');
      if (SELECTED_FIELD.onSaved) await SELECTED_FIELD.onSaved(SELECTED_FIELD.fieldName, SELECTED_FIELD.itemId);
    } catch (e) {
      statusEl.className = 'override-status err';
      statusEl.textContent = 'Could not save: ' + e.message;
      btn.disabled = false;
    }
  };
}

// Wires every .editable-line/.cell-editable pencil under `container` to
// select that field for correction (see selectFieldForCorrection() above)
// and jump to wherever this modal's override box lives, then wires that
// override box's own Submit button. Replaces the old inline
// wireEditableLines()/startFieldEdit() swap-to-input behavior.
// `fieldsUrl` is either the one URL every line in `container` shares (a
// single PO's fields endpoint - Domestic/Import's own usage), or a function
// `(lineEl) => url` for a container whose lines target different endpoints
// (the material modal's Stock by Plant table, where each row is a different
// plant's own lot - see editableCell()'s data-plant above and
// materialFieldsUrl() in shared.js). `switchToTab()` is a callback the
// caller supplies, since each modal's tab id/attribute differs (po-modal.js
// uses data-tab="flags", import-po.js uses data-itab="flags",
// material-modal.js uses data-tab="stockplant" - it has no separate flags
// tab, see that file's own comment).
function wireEditIcons(container, fieldsUrl, switchToTab, onSaved) {
  SELECTED_FIELD = null;
  const resolveUrl = typeof fieldsUrl === 'function' ? fieldsUrl : () => fieldsUrl;
  container.querySelectorAll('.editable-line, .cell-editable').forEach(el => {
    const pencil = el.querySelector('.edit-pencil');
    if (!pencil) return;
    pencil.onclick = () => {
      const valueEl = el.querySelector('.line-val');
      const shown = valueEl ? valueEl.textContent : '';
      const currentValue = (shown === 'Not available' || shown === '-') ? '' : shown;
      const options = el.dataset.options ? JSON.parse(decodeURIComponent(el.dataset.options)) : [];
      selectFieldForCorrection({
        label: el.dataset.label || el.dataset.field,
        fieldName: el.dataset.field,
        itemId: el.dataset.item || '',
        fieldType: el.dataset.fieldType || 'text',
        options: options,
        currentValue: currentValue,
        fieldsUrl: resolveUrl(el),
        onSaved: onSaved,
      });
      if (switchToTab) switchToTab();
    };
  });
  wireOverrideBox(container);
}
