// admin.html's page-bootstrap script - extracted from an inline <script> so
// script-src can drop 'unsafe-inline' (see config/security_headers.py).
//
// Redesigned 2026-09-07 to match the TDS Automation App's own Admin Panel
// layout (sidebar nav: Overview / Users / Sync Status / System Info) -
// project owner: "Design the admin panel like this one we did for tds as
// per our requirements and data." TDS's own Overview concepts don't map
// onto this app's data as literally created/owned records (a TDS document
// is authored by a user; a Purchase Tracker PO is synced from a CSV, nobody
// "creates" one here) - adapted instead of copied verbatim. See
// apps/api/routers/admin_overview_views.py's module docstring for exactly
// what replaces each TDS concept ("Top Creators" -> "Top Correctors" on
// inline field corrections, "Top Customers" -> "Top Vendors" by PO count,
// "Recent Activity" -> recent corrections). The KPI row (Total PO's/
// Suppliers/This Month/This Week) reuses home.html's own loadKpis()
// (moved to shared.js 2026-09-07 for exactly this reuse) rather than a
// second near-duplicate aggregation.
//
// User management itself (create/edit/activate/deactivate) is unchanged
// from before this redesign - only which tab it lives under, and a new
// per-card stats row, changed.
let USERS = [];
let editingUserId = null;
let CURRENT_ADMIN_TAB = 'overview';
let CURRENT_ADMIN_EMAIL = '';

// Delete is a real, irreversible destructive action (unlike Deactivate,
// which just blocks sign-in) - project owner asked for it restricted to
// this one account only, hardcoded. The backend (users_views.py's
// update_user()) enforces this for real; this is just so nobody else even
// sees a Delete button to click in the first place.
const DELETE_USER_ALLOWED_EMAIL = 'dishant.barot@ravasco.com';

(async function () {
  const user = await requireAuth();
  if (!user) return;
  CURRENT_ADMIN_EMAIL = (user.email || '').toLowerCase();
  renderNavTabs(document.getElementById('navTabs'), 'admin');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();

  document.getElementById('loadingOverlay').style.display = 'none';
  if (user.role !== 'admin') {
    document.getElementById('deniedContent').hidden = false;
    return;
  }
  document.getElementById('mainContent').hidden = false;
  document.getElementById('sysinfoEmail').textContent = user.email;
  document.getElementById('sysinfoRole').textContent = user.role;

  document.getElementById('addUserBtn').onclick = () => openForm(null);
  document.getElementById('uf-close').onclick = closeForm;
  document.getElementById('uf-cancel').onclick = closeForm;
  document.getElementById('uf-submit').onclick = submitForm;
  document.getElementById('uf-role').onchange = updatePlantsRowVisibility;

  document.querySelectorAll('[data-admin-tab]').forEach(btn => btn.onclick = () => switchAdminTab(btn.dataset.adminTab));
  renderSystemInfoPlants();

  await Promise.all([loadSyncCards(), loadUsers(), loadOverviewData(), loadKpis()]);
})();

// ── Sidebar tab switching ────────────────────────────────────────
function switchAdminTab(tab) {
  if (tab === CURRENT_ADMIN_TAB) return;
  CURRENT_ADMIN_TAB = tab;
  document.querySelectorAll('[data-admin-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.adminTab === tab));
  ['overview', 'users', 'sync', 'system'].forEach(key => {
    document.getElementById('adminTab' + key.charAt(0).toUpperCase() + key.slice(1)).hidden = key !== tab;
  });
}

// ── Sync status cards ────────────────────────────────────────────
async function loadSyncCards() {
  const el = document.getElementById('syncPlantCards');
  // 'match' added 2026-09-04 - see js/main.js's loadSyncStatus() /
  // SyncRun.Source.MATCH's own comment (apps/core/models.py).
  const labels = { po_csv: 'PO Updated', mir: 'MIR', stock: 'RM', match: 'Matching' };
  const rows = await Promise.all(PLANT_KEYS.map(async key => {
    try {
      const data = await apiForPlant(key, '/sync-status');
      return { key, sync: data.sync, failed: false };
    } catch (e) {
      console.error('admin: sync-status failed for ' + key + ':', e);
      return { key, sync: null, failed: true };
    }
  }));

  el.innerHTML = rows.map(r => {
    if (r.failed) {
      return '<div class="sync-plant-card"><div class="sync-plant-name">' + escapeHtml(PLANTS[r.key].label) + '</div>' +
        '<div class="sync-item failed">Sync status unavailable right now.</div></div>';
    }
    const items = Object.keys(labels).map(src => {
      const run = r.sync[src];
      if (!run) return '<span class="sync-item">' + labels[src] + ': never synced</span>';
      const when = new Date(run.startedAt).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
      const cls = run.status === 'success' ? '' : 'failed';
      // errorDetail (SyncRun.error_detail) was always recorded on a
      // sync failure but never surfaced anywhere until now - an admin
      // used to see only a red "failed" label with no way to find out
      // why short of server log/Django Admin access. This is exactly
      // the page an admin would be looking at that failure from, so a
      // title tooltip on hover is enough - no need for a dedicated
      // error panel for a diagnostic aid.
      const titleAttr = cls === 'failed' && run.errorDetail ? ' title="' + escapeHtml(run.errorDetail) + '"' : '';
      return '<span class="sync-item ' + cls + '"' + titleAttr + '>' + labels[src] + ': <b>' + escapeHtml(when) + '</b></span>';
    }).join('');
    return '<div class="sync-plant-card"><div class="sync-plant-name">' + escapeHtml(PLANTS[r.key].label) + '</div>' +
      '<div class="sync-row-list">' + items + '</div></div>';
  }).join('');
}

// ── Overview: top correctors / top vendors / recent activity ────
async function loadOverviewData() {
  try {
    const res = await fetch('/api/auth/admin-overview', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderBarList('topCorrectorsList', data.topCorrectors, c => c.fullName, c => c.count);
    renderBarList('topVendorsList', data.topVendors, v => v.vendor, v => v.count);
    renderRecentActivity(data.recentActivity || []);
  } catch (e) {
    console.error('admin: failed to load overview data:', e);
    document.getElementById('topCorrectorsList').innerHTML = '<div class="no-data-note">Couldn\'t load right now.</div>';
    document.getElementById('topVendorsList').innerHTML = '<div class="no-data-note">Couldn\'t load right now.</div>';
    document.getElementById('recentActivityArea').innerHTML = '<div class="no-data-note">Couldn\'t load right now.</div>';
  }
}

// Shared renderer for both "Top Correctors" and "Top Vendors" - same
// label/bar/count row shape, only which fields feed it differ.
function renderBarList(elId, rows, labelFn, countFn) {
  const el = document.getElementById(elId);
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="no-data-note">No data yet.</div>';
    return;
  }
  const max = Math.max(...rows.map(countFn));
  el.innerHTML = rows.map(row => {
    const count = countFn(row);
    const pct = max > 0 ? Math.max(4, Math.round((count / max) * 100)) : 0;
    return '<div class="bar-list-row">' +
      '<div class="bar-list-label" title="' + escapeHtml(labelFn(row)) + '">' + escapeHtml(labelFn(row)) + '</div>' +
      '<div class="bar-track"><div class="bar-fill" data-pct="' + pct + '"></div></div>' +
      '<div class="bar-count">' + count + '</div>' +
    '</div>';
  }).join('');
  // width is a continuous, per-row value - can't be a fixed CSS class the
  // way the avatar's role color below can, and a literal style="width:...%"
  // attribute is blocked once style-src drops 'unsafe-inline' (setting the
  // property via JS afterwards, like this, is NOT restricted the same way -
  // see brand.css's "Utility classes" comment for why).
  el.querySelectorAll('.bar-fill[data-pct]').forEach(bar => {
    bar.style.width = bar.dataset.pct + '%';
  });
}

const _ACTIVITY_TYPE_LABELS = { domestic: 'Domestic PO', import: 'Import PO', material: 'Material' };
function renderRecentActivity(rows) {
  const el = document.getElementById('recentActivityArea');
  if (!rows.length) {
    el.innerHTML = '<div class="no-data-note">No corrections recorded yet.</div>';
    return;
  }
  el.innerHTML = '<div class="table-wrap"><table class="admin-activity-table"><thead><tr>' +
    '<th>Type</th><th>Plant</th><th>Target</th><th>Field</th><th>Change</th><th>By</th><th>When</th>' +
    '</tr></thead><tbody>' +
    rows.map(r => '<tr>' +
      '<td><span class="activity-type-pill ' + escapeHtml(r.type) + '">' + escapeHtml(_ACTIVITY_TYPE_LABELS[r.type] || r.type) + '</span></td>' +
      '<td>' + escapeHtml(r.plantLabel) + '</td>' +
      '<td>' + escapeHtml(r.target) + '</td>' +
      '<td>' + escapeHtml(r.fieldName) + '</td>' +
      '<td>' + escapeHtml(r.oldValue || 'blank') + ' &rarr; ' + escapeHtml(r.newValue || 'blank') + '</td>' +
      '<td>' + escapeHtml(r.correctedByName) + '</td>' +
      '<td>' + escapeHtml(formatDateIN(r.correctedAt ? r.correctedAt.slice(0, 10) : null)) + '</td>' +
    '</tr>').join('') +
    '</tbody></table></div>';
}

// ── System Info ───────────────────────────────────────────────────
// Plants Covered - moved here from home.html (2026-09-07, project owner:
// "remove this from homepage and restructure") - this admin-only tab is a
// more fitting home for static plant/data-source reference info than the
// landing page every role sees.
const _SYSINFO_PLANTS = [
  { label: 'HRS, Silvassa' },
  { label: 'RTP-Achhad' },
  { label: 'RTP-Vapi' },
];
function renderSystemInfoPlants() {
  const el = document.getElementById('sysinfoPlants');
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21V11l6 4v-4l6 4V7l6 4v10H3Z"/><line x1="7" y1="21" x2="7" y2="17"/><line x1="12" y1="21" x2="12" y2="17"/><line x1="17" y1="21" x2="17" y2="17"/></svg>';
  el.innerHTML = _SYSINFO_PLANTS.map(p =>
    '<div class="sysinfo-plant-card"><div class="sysinfo-plant-icon">' + icon + '</div>' +
      '<div><div class="sysinfo-plant-name">' + escapeHtml(p.label) + '</div>' +
      '<div class="sysinfo-plant-value">PO &middot; MIR &middot; RM</div></div></div>'
  ).join('');
}

// ── Users: list/render ──────────────────────────────────────────
async function loadUsers() {
  const el = document.getElementById('usersArea');
  try {
    const res = await fetch('/api/auth/users', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    USERS = data.users || [];
    renderUsers();
  } catch (e) {
    console.error('admin: failed to load users:', e);
    el.innerHTML = '<div class="search-empty">' + emptyStateHtml('Couldn\'t load the user list right now. Refresh to retry.', 'error') + '</div>';
  }
}

function renderUsers() {
  const el = document.getElementById('usersArea');
  updateUserCounts();
  if (!USERS.length) {
    el.innerHTML = '<div class="search-empty">' + emptyStateHtml('No users found.') + '</div>';
    return;
  }
  el.innerHTML = '<div class="users-grid">' + USERS.map(u => {
    const initials = userInitials(u);
    return '<div class="user-card' + (u.isActive ? '' : ' inactive') + '">' +
      '<div class="user-card-head">' +
        '<div class="user-card-avatar avatar-role-' + escapeHtml(u.role) + '">' + escapeHtml(initials) + '</div>' +
        '<div><div class="user-card-name">' + escapeHtml(u.fullName || u.email) + '</div>' +
        '<div class="user-card-email">' + escapeHtml(u.email) + '</div></div>' +
      '</div>' +
      '<div class="user-card-meta"><span class="role-pill ' + escapeHtml(u.role) + '">' + escapeHtml(u.role) + '</span>' +
        '<span class="status-pill ' + (u.isActive ? 'active' : 'inactive') + '">' + (u.isActive ? 'Active' : 'Inactive') + '</span>' +
        '<span class="plants-pill">' + escapeHtml(plantsLabel(u.plants)) + '</span></div>' +
      '<div class="user-card-desig">' + escapeHtml(u.designation || '') + '</div>' +
      '<div class="user-card-stats">' +
        '<div><div class="user-card-stat-val">' + (u.correctionsCount || 0) + '</div><div class="user-card-stat-label">Corrections Made</div></div>' +
        '<div><div class="user-card-stat-val fs-13">' + escapeHtml(u.lastLoginAt ? formatDateIN(u.lastLoginAt.slice(0, 10)) : 'Never') + '</div><div class="user-card-stat-label">Last Login</div></div>' +
      '</div>' +
      '<div class="user-card-foot">' +
        '<span class="fs-11 text-muted">Since ' + escapeHtml(formatDateIN(u.createdAt ? u.createdAt.slice(0, 10) : null)) + '</span>' +
        '<div class="user-card-actions">' +
          '<button type="button" class="icon-btn" data-edit="' + u.userId + '">Edit</button>' +
          '<button type="button" class="icon-btn ' + (u.isActive ? 'danger' : 'go') + '" data-toggle="' + u.userId + '">' + (u.isActive ? 'Deactivate' : 'Activate') + '</button>' +
          (CURRENT_ADMIN_EMAIL === DELETE_USER_ALLOWED_EMAIL
            ? '<button type="button" class="icon-btn danger" data-delete="' + u.userId + '">Delete</button>'
            : '') +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('') + '</div>';

  el.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => openForm(USERS.find(u => u.userId === Number(btn.dataset.edit))));
  el.querySelectorAll('[data-toggle]').forEach(btn => btn.onclick = () => toggleActive(Number(btn.dataset.toggle)));
  el.querySelectorAll('[data-delete]').forEach(btn => btn.onclick = () => deleteUser(Number(btn.dataset.delete)));
}

function updateUserCounts() {
  document.getElementById('sidebarUserCount').textContent = String(USERS.length);
  document.getElementById('sidebarRoleLine').textContent = 'Admin - full access';
  document.getElementById('sysinfoUserCount').textContent = String(USERS.length);
  document.getElementById('sysinfoActiveCount').textContent = String(USERS.filter(u => u.isActive).length);
}

function plantsLabel(plants) {
  if (!plants || !plants.length) return 'All plants';
  return plants.map(k => (PLANTS[k] || { label: k }).label).join(', ');
}

function userInitials(u) {
  const name = (u.fullName || '').trim();
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean);
    return (parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : parts[0].slice(0, 2)).toUpperCase();
  }
  return (u.email || '?').slice(0, 2).toUpperCase();
}

// ── Users: create/edit form ─────────────────────────────────────
function renderPlantsCheckboxes(selected) {
  const el = document.getElementById('uf-plants');
  const sel = selected || [];
  el.innerHTML = PLANT_KEYS.map(key =>
    '<label><input type="checkbox" value="' + key + '"' + (sel.includes(key) ? ' checked' : '') + '> ' + escapeHtml(PLANTS[key].label) + '</label>'
  ).join('');
}

function readPlantsCheckboxes() {
  return Array.from(document.querySelectorAll('#uf-plants input[type="checkbox"]:checked')).map(cb => cb.value);
}

// An admin's own access is never plant-scoped - every admin-only endpoint
// (sync_trigger, every users_views.py view) ignores PTUser.plants entirely,
// and users_views.py's create_user()/update_user() now force plants=[]
// server-side for role=admin regardless of what's submitted (see that
// file's own comment) - so showing the checkboxes for an admin account
// would just be a control that quietly does nothing, which reads as a bug
// ("I checked HRS only, why can this admin still edit every plant?") more
// than a feature. Hidden here for exactly that reason, not to save space.
function updatePlantsRowVisibility() {
  const isAdmin = document.getElementById('uf-role').value === 'admin';
  document.getElementById('uf-plants-row').style.display = isAdmin ? 'none' : 'block';
}

function openForm(user) {
  editingUserId = user ? user.userId : null;
  const edit = !!user;
  document.getElementById('uf-title').textContent = edit ? 'Edit User' : 'Add User';
  document.getElementById('uf-submit').textContent = edit ? 'Save Changes' : 'Create User';
  document.getElementById('uf-fullname').value = user ? (user.fullName || '') : '';
  document.getElementById('uf-email').value = user ? user.email : '';
  document.getElementById('uf-email').readOnly = edit;
  document.getElementById('uf-designation').value = user ? (user.designation || '') : '';
  document.getElementById('uf-role').value = user ? user.role : 'viewer';
  document.getElementById('uf-password').value = '';
  renderPlantsCheckboxes(user ? user.plants : []);
  updatePlantsRowVisibility();
  // Password row now stays visible in edit mode too (added 2026-09-04,
  // in-app reset) - just optional there: label/hint/required-asterisk
  // swap to make "blank = leave unchanged" obvious.
  document.getElementById('uf-pw-label').innerHTML = edit
    ? 'New Password <span class="text-muted fw-400">(leave blank to keep current)</span>'
    : 'Password <span class="req-mark">*</span>';
  document.getElementById('uf-pw-hint').textContent = edit
    ? 'Only fill this in to reset the password - leave it blank to leave the current password untouched.'
    : 'Share this password with the user directly, or ask them to use "Change Password" from their own account menu afterwards.';
  document.getElementById('uf-active-row').hidden = !edit;
  if (edit) document.getElementById('uf-active').value = String(user.isActive);
  // Trusted Devices: only meaningful for an existing account (a new
  // user has no devices yet) - fetched fresh on every open rather than
  // cached, since another admin session or the user's own next login
  // could have added/changed devices since this modal last opened.
  document.getElementById('uf-devices-row').hidden = !edit;
  if (edit) loadDevices(user.userId);
  document.getElementById('uf-err').classList.remove('show');
  document.getElementById('uf-overlay').classList.add('open');
  document.getElementById('uf-fullname').focus();
}

// ── Users: trusted devices (Edit User modal only) ───────────────
async function loadDevices(userId) {
  const el = document.getElementById('uf-devices-list');
  el.innerHTML = '<p class="uf-hint my-4">Loading&hellip;</p>';
  try {
    const res = await fetch('/api/auth/users/' + userId + '/devices', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderDevices(userId, data.devices || []);
  } catch (e) {
    console.error('admin: failed to load devices for user ' + userId + ':', e);
    el.innerHTML = '<p class="uf-hint my-4 text-red">Couldn\'t load trusted devices right now.</p>';
  }
}

function renderDevices(userId, devices) {
  const el = document.getElementById('uf-devices-list');
  // Bail out if the modal was closed/switched to a different user while
  // this fetch was in flight (same "don't paint a stale response into
  // the current view" reasoning the dashboard's own openImportPoModal()/
  // openMaterialModal() use for their own request-token guards).
  if (!editingUserId || editingUserId !== userId) return;
  if (!devices.length) {
    el.innerHTML = '<p class="uf-hint my-4">No trusted devices - this account still verifies by email code on every sign-in.</p>';
    return;
  }
  el.innerHTML = devices.map(d => {
    const seen = d.lastUsedAt ? formatDateIN(d.lastUsedAt.slice(0, 10)) : 'never';
    return '<div class="device-row" data-device-id="' + d.id + '">' +
      '<div><div class="device-row-name">' + escapeHtml(d.deviceName || 'Unknown device') + '</div>' +
      '<div class="device-row-meta">' + escapeHtml(d.ipAddress || 'unknown IP') + ' &middot; last used ' + escapeHtml(seen) + '</div></div>' +
      '<button type="button" class="icon-btn danger" data-revoke-device="' + d.id + '">Revoke</button>' +
    '</div>';
  }).join('');
  el.querySelectorAll('[data-revoke-device]').forEach(btn => btn.onclick = () => revokeDevice(userId, Number(btn.dataset.revokeDevice)));
}

async function revokeDevice(userId, deviceId) {
  if (!window.confirm('Revoke this device? It will need to verify by email code again on its next sign-in.')) return;
  try {
    const res = await fetch('/api/auth/users/' + userId + '/devices/' + deviceId, { method: 'DELETE', credentials: 'same-origin' });
    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'HTTP ' + res.status);
    }
    showToast('Device revoked.', 'success');
    await loadDevices(userId);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

function closeForm() {
  document.getElementById('uf-overlay').classList.remove('open');
  editingUserId = null;
}

async function submitForm() {
  const errEl = document.getElementById('uf-err');
  errEl.classList.remove('show');

  const fullName = document.getElementById('uf-fullname').value.trim();
  const email = document.getElementById('uf-email').value.trim();
  const password = document.getElementById('uf-password').value;
  const designation = document.getElementById('uf-designation').value.trim();
  const role = document.getElementById('uf-role').value;
  const isActive = document.getElementById('uf-active').value === 'true';
  // Server-side already forces plants=[] for role=admin regardless of what's
  // sent (users_views.py) - zeroed here too so a role switched to Admin
  // after checking some plants doesn't submit stale, now-meaningless values.
  const plants = role === 'admin' ? [] : readPlantsCheckboxes();

  if (!fullName) { return showFormError('Full name is required.'); }
  if (!editingUserId) {
    if (!email) { return showFormError('Email is required.'); }
    if (password.length < 8) { return showFormError('Password must be at least 8 characters.'); }
  } else if (password && password.length < 8) {
    // Blank is fine (means "don't change it") - only validate length
    // when the admin actually typed a new one.
    return showFormError('Password must be at least 8 characters.');
  }

  const btn = document.getElementById('uf-submit');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    if (editingUserId) {
      const payload = { fullName, designation, role, isActive, plants };
      if (password) payload.password = password;
      await patchUser(editingUserId, payload);
      showToast('User updated.', 'success');
    } else {
      await createUserApi({ email, password, fullName, designation, role, plants });
      showToast('User created.', 'success');
    }
    closeForm();
    await loadUsers();
  } catch (e) {
    showFormError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = editingUserId ? 'Save Changes' : 'Create User';
  }
}

function showFormError(msg) {
  const errEl = document.getElementById('uf-err');
  errEl.textContent = msg;
  errEl.classList.add('show');
}

async function toggleActive(userId) {
  const user = USERS.find(u => u.userId === userId);
  if (!user) return;
  try {
    await patchUser(userId, { isActive: !user.isActive });
    showToast('User ' + (user.isActive ? 'deactivated' : 'activated') + '.', 'success');
    await loadUsers();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function deleteUser(userId) {
  const user = USERS.find(u => u.userId === userId);
  if (!user) return;
  if (!window.confirm('Permanently delete ' + (user.fullName || user.email) + ' (' + user.email + ')? This cannot be undone - consider Deactivate instead if you just want to block sign-in.')) return;
  try {
    const res = await fetch('/api/auth/users/' + userId, { method: 'DELETE', credentials: 'same-origin' });
    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'HTTP ' + res.status);
    }
    showToast('User deleted.', 'success');
    await loadUsers();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function createUserApi(payload) {
  const res = await fetch('/api/auth/users/create', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  // See frontend/js/shared.js's apiForPlant() for why res.json() is
  // guarded - same fix, same reasoning (a non-JSON response, e.g. a
  // proxy error page during a deploy, used to throw a raw, cryptic
  // parse error straight into showFormError() instead of something
  // readable).
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error('The server sent an unexpected response. Please try again, or contact IT if this keeps happening.');
  }
  if (!res.ok) throw new Error(data.detail || 'Something went wrong. Please try again.');
  return data;
}

async function patchUser(userId, payload) {
  const res = await fetch('/api/auth/users/' + userId, {
    method: 'PATCH', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  // See createUserApi() above for why res.json() is guarded.
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error('The server sent an unexpected response. Please try again, or contact IT if this keeps happening.');
  }
  if (!res.ok) throw new Error(data.detail || 'Something went wrong. Please try again.');
  return data;
}

// ── Toast ────────────────────────────────────────────────────────
function showToast(message, kind) {
  const stack = document.getElementById('toastStack');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}
