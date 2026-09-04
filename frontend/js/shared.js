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
  return PLANTS[plantKey].apiPrefix + '/materials/' + lotId + '/fields';
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
  const data = await res.json();
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
function editableLine(plantKey, label, value, fieldName, itemId, fieldType, options) {
  if (!canEditField(plantKey)) return plainLine(label, value);
  const display = (value === null || value === undefined || value === '') ? 'Not available' : escapeHtml(String(value));
  const optsAttr = options && options.length ? ' data-options="' + escapeHtml(encodeURIComponent(JSON.stringify(options))) + '"' : '';
  // data-plant lets a container whose lines span more than one plant (the
  // material modal's Overview tab, once its Category/Sub Category lines
  // target the anchor lot's own plant) resolve each line's own fieldsUrl -
  // see wireEditableLines()'s function-form fieldsUrl and editableCell()'s
  // identical attribute below. Harmless/unused for every other existing
  // caller (Domestic/Import PO modals), which always pass one fixed
  // fieldsUrl string for their whole container.
  return '<div class="line editable-line" data-field="' + escapeHtml(fieldName) + '"' +
    (itemId ? ' data-item="' + escapeHtml(itemId) + '"' : '') +
    ' data-plant="' + escapeHtml(plantKey) + '"' +
    ' data-field-type="' + (fieldType || 'text') + '"' + optsAttr + '>' +
    escapeHtml(label) + ': <span class="line-val">' + display + '</span>' +
    '<span class="edit-pencil" title="Edit">&#9998;</span>' +
    '<span class="edit-actions" hidden><span class="edit-save" title="Save">&#10003;</span><span class="edit-cancel" title="Cancel">&#10005;</span></span>' +
    '<div class="edit-warning" hidden></div>' +
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
    return apiImports('/matches/po-mir/' + plantKey + '/' + matchId + '/dismiss', opts);
  }
  return apiForPlant(plantKey, '/matches/' + matchType + '/' + matchId + '/dismiss', opts);
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
// doesn't need) so this stays URL-shape-agnostic.
async function savePoField(fieldsUrl, itemId, field, value) {
  const res = await fetch(fieldsUrl, {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ itemId: itemId || '', field: field, value: value }),
  });
  if (res.status === 401) {
    window.location.href = '/login.html';
    throw new Error('Not authenticated');
  }
  const data = await res.json();
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
// a per-row caller can build the right per-plant fieldsUrl.
function editableCell(plantKey, value, fieldName, itemId, fieldType, options) {
  const display = (value === null || value === undefined || value === '') ? '-' : escapeHtml(String(value));
  if (!canEditField(plantKey)) return display;
  const optsAttr = options && options.length ? ' data-options="' + escapeHtml(encodeURIComponent(JSON.stringify(options))) + '"' : '';
  return '<span class="editable-line cell-editable" data-field="' + escapeHtml(fieldName) + '" data-item="' + escapeHtml(String(itemId)) + '" data-plant="' + escapeHtml(plantKey) + '"' +
    ' data-field-type="' + (fieldType || 'text') + '"' + optsAttr + '>' +
    '<span class="line-val">' + display + '</span>' +
    '<span class="edit-pencil" title="Edit">&#9998;</span>' +
    '<span class="edit-actions" hidden><span class="edit-save" title="Save">&#10003;</span><span class="edit-cancel" title="Cancel">&#10005;</span></span>' +
    '<div class="edit-warning" hidden></div>' +
  '</span>';
}

// Wires every .editable-line under `container` to swap into edit mode on
// pencil click. `onSaved(fieldName, itemId)` runs after a successful PATCH -
// each PO-type's caller re-fetches/re-renders differently (Domestic
// invalidates the whole-plant list cache; Import invalidates its own list +
// per-PO detail cache), so that stays a callback instead of baked in here.
// `fieldsUrl` is either the one URL every line in `container` shares (a
// single PO's fields endpoint - Domestic/Import's own usage), or a function
// `(lineEl) => url` for a container whose lines target different endpoints
// (the material modal's Stock by Plant table, where each row is a different
// plant's own lot - see editableCell()'s data-plant above and
// materialFieldsUrl() in main.js).
function wireEditableLines(container, fieldsUrl, onSaved) {
  const resolveUrl = typeof fieldsUrl === 'function' ? fieldsUrl : () => fieldsUrl;
  container.querySelectorAll('.editable-line').forEach(lineEl => {
    lineEl.querySelector('.edit-pencil').onclick = () => startFieldEdit(lineEl, resolveUrl(lineEl), onSaved);
  });
}

/** Swaps one wired .editable-line/.cell-editable element into edit mode:
 * builds the right input control for its fieldType, shows the save/cancel
 * icons, and wires Enter/Escape/blur plus the icons themselves to save() or
 * revert(). save() PATCHes via savePoField() and calls `onSaved` on
 * success; a failed save alerts the error and reverts back to display mode
 * rather than leaving the input stuck mid-edit. */
function startFieldEdit(lineEl, fieldsUrl, onSaved) {
  const valueEl = lineEl.querySelector('.line-val');
  const pencil = lineEl.querySelector('.edit-pencil');
  const actions = lineEl.querySelector('.edit-actions');
  const warningEl = lineEl.querySelector('.edit-warning');
  const fieldType = lineEl.dataset.fieldType || 'text';
  const current = valueEl.textContent === 'Not available' ? '' : valueEl.textContent;

  let input;
  if (fieldType === 'select') {
    input = document.createElement('select');
    const options = lineEl.dataset.options ? JSON.parse(decodeURIComponent(lineEl.dataset.options)) : [];
    const withCurrent = current && options.indexOf(current) === -1 ? [current].concat(options) : options;
    withCurrent.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      if (opt === current) o.selected = true;
      input.appendChild(o);
    });
  } else {
    input = document.createElement('input');
    input.type = fieldType === 'date' ? 'date' : (fieldType === 'number' ? 'number' : 'text');
    if (fieldType === 'number') input.min = '0';
    input.value = current;
  }
  input.className = 'line-input';
  valueEl.replaceWith(input);
  input.focus();
  if (input.select && fieldType !== 'select') input.select();
  pencil.style.visibility = 'hidden';
  actions.hidden = false;
  warningEl.hidden = true;
  // Clicking the Save/Cancel icons would otherwise blur the input first
  // (mousedown moves focus before the click handler runs), triggering the
  // blur-save path before the intended save()/revert() ever fires.
  actions.addEventListener('mousedown', (e) => e.preventDefault());

  let done = false;
  const revert = () => {
    if (done) return;
    done = true;
    input.replaceWith(valueEl);
    pencil.style.visibility = '';
    actions.hidden = true;
  };
  const save = async () => {
    if (done) return;
    done = true;
    const fieldName = lineEl.dataset.field;
    const itemId = lineEl.dataset.item || '';
    try {
      const result = await savePoField(fieldsUrl, itemId, fieldName, input.value);
      if (result.warning) {
        warningEl.textContent = result.warning;
        warningEl.hidden = false;
      }
      if (onSaved) await onSaved(fieldName, itemId);
    } catch (e) {
      alert('Could not save this change: ' + e.message);
      done = false;
      revert();
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') save();
    if (e.key === 'Escape') revert();
  });
  input.addEventListener('blur', () => { if (!done) save(); });
  actions.querySelector('.edit-save').onclick = save;
  actions.querySelector('.edit-cancel').onclick = revert;
}
