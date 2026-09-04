/**
 * frontend/js/auth.js — gates every protected page behind a valid session
 * and renders the shared top-nav chrome (user badge, nav tabs).
 *
 * Must load first on every protected page (index.html, home.html,
 * search-po.html, admin.html), before that page's own script. No imports -
 * plain script tag, same convention as the TDS Automation App's own
 * frontend/js/auth.js.
 *
 * Auth is cookie-based (httpOnly pt_access, set by the server on login/
 * device-verify) - there is no token for this script to hold or attach
 * itself; every fetch() already carries the cookie automatically since the
 * frontend and API are same-origin. The one thing the client genuinely
 * needs to check is "is my cookie still valid", which is what
 * GET /api/auth/me is for (apps/api/auth_views.py#whoami).
 */

// ── Session state ───────────────────────────────────────────────────────
let CURRENT_USER = null;

/**
 * Confirms the browser's httpOnly session cookie is still valid by asking
 * the server (GET /api/auth/me), populating CURRENT_USER on success.
 * Any failure - real 401, or a genuine network error - bounces to
 * /login.html, since there's nothing a protected page can usefully render
 * without a resolved user.
 */
async function requireAuth() {
  try {
    const res = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('not authenticated (HTTP ' + res.status + ')');
    CURRENT_USER = await res.json();
    return CURRENT_USER;
  } catch (e) {
    // Logged before redirecting - a plain 401 (no session) is expected and
    // not worth alarming about, but this also catches genuine network
    // failures, which look identical to the user (bounced to the login
    // page) but are a different problem worth being able to tell apart in
    // the console rather than silently redirecting either way.
    console.warn('requireAuth: not authenticated, redirecting to /login.html -', e.message);
    window.location.href = '/login.html';
    return null;
  }
}

/**
 * Best-effort server-side logout (clears the httpOnly cookies), then always
 * redirects to /login.html regardless of whether the call succeeded - the
 * cookies are server-controlled anyway, so there's nothing else useful to
 * do client-side if the request fails.
 */
async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch (e) {
    // Redirect to the login page regardless (the httpOnly cookies are
    // server-controlled anyway - this call is best-effort), but log the
    // failure rather than swallowing it, so a real network problem is at
    // least visible in the console instead of looking identical to success.
    console.warn('logout: server call failed, redirecting anyway -', e);
  }
  window.location.href = '/login.html';
}

// ── User badge (avatar + dropdown) ──────────────────────────────────────
// Circle-avatar user menu (initials + role-colored background, stacked
// name/role, click-to-open dropdown with Logout) - same visual pattern as
// the TDS Automation App's own top nav. Avatar color is keyed off role
// (admin=gold, editor=blue, viewer=navy) so it reads consistently with the
// role-pill colors used elsewhere (e.g. admin.html's user table) rather
// than an arbitrary per-user color.
const ROLE_AVATAR_COLOR = { admin: 'var(--gold-light)', editor: 'var(--blue)', viewer: 'var(--navy-mid)' };

/** Derives 1-2 uppercase initials from the user's full name (first+last
 * initial), or their first two characters if no name is set, falling back
 * to the email's first two characters as a last resort. */
function userInitials(user) {
  const name = (user.fullName || '').trim();
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean);
    const initials = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : parts[0].slice(0, 2);
    return initials.toUpperCase();
  }
  return (user.email || '?').slice(0, 2).toUpperCase();
}

/**
 * Renders the avatar/name/role/dropdown user menu into `container` and
 * wires its click-to-open behavior (a document-level click listener closes
 * it again, since it has no backdrop of its own). No-op until CURRENT_USER
 * has been populated by requireAuth().
 */
function renderUserBadge(container) {
  if (!container || !CURRENT_USER) return;
  const label = CURRENT_USER.fullName || CURRENT_USER.email;
  const color = ROLE_AVATAR_COLOR[CURRENT_USER.role] || 'var(--navy-mid)';
  container.innerHTML =
    '<div class="user-menu" id="userMenuWrap">' +
      '<div class="user-avatar-circle" style="background:' + color + '">' + escapeHtmlAuth(userInitials(CURRENT_USER)) + '</div>' +
      '<div class="user-info">' +
        '<div class="user-name">' + escapeHtmlAuth(label) + '</div>' +
        '<div class="user-role">' + escapeHtmlAuth(CURRENT_USER.role) + '</div>' +
      '</div>' +
      '<span class="user-chevron">&#9662;</span>' +
      '<div class="user-dropdown" id="userDropdown" hidden>' +
        '<button type="button" id="logoutBtn" class="user-dropdown-item">Logout</button>' +
      '</div>' +
    '</div>';

  const wrap = document.getElementById('userMenuWrap');
  const dropdown = document.getElementById('userDropdown');
  wrap.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.hidden = !dropdown.hidden;
  });
  document.addEventListener('click', () => { dropdown.hidden = true; });
  document.getElementById('logoutBtn').onclick = logout;
}

// ── Shared nav tabs ─────────────────────────────────────────────────────
// Shared top-nav tabs (Home / Dashboard / Search PO / Admin) - rendered the
// same way on every protected page (home.html, index.html, search-po.html,
// admin.html) so the header looks and behaves identically everywhere, same
// design as the TDS Automation App's own top nav. "Home" points at
// /home.html (the landing/overview page); "Dashboard" always points at "/"
// (the real PO<->MIR<->Stock reconciliation UI) - these are two distinct
// tabs/destinations, not aliases of each other.
//
// Call after requireAuth() has resolved (CURRENT_USER populated) - the Admin
// tab is hidden entirely for non-admin roles rather than shown-then-denied,
// admin.html's own access-denied state is defense in depth for anyone who
// navigates there directly, not the primary gate.
/**
 * Renders the 4 shared nav tabs into `container`, marking `activePage`
 * (e.g. 'home', 'dashboard', 'search', 'admin') as the active one. The
 * Admin tab is only added to the list at all for role === 'admin'.
 */
function renderNavTabs(container, activePage) {
  if (!container || !CURRENT_USER) return;
  const tabs = [
    { key: 'home', href: '/home.html', label: 'Home' },
    { key: 'dashboard', href: '/', label: 'Dashboard' },
    { key: 'search', href: '/search-po.html', label: 'Search PO' },
  ];
  if (CURRENT_USER.role === 'admin') tabs.push({ key: 'admin', href: '/admin.html', label: 'Admin' });
  container.innerHTML = tabs.map(t =>
    '<a class="nav-tab' + (t.key === activePage ? ' active' : '') + '" href="' + t.href + '">' + escapeHtmlAuth(t.label) + '</a>'
  ).join('');
}

// ── Local helpers ───────────────────────────────────────────────────────
// Local copy - auth.js loads before main.js and must have no imports/
// dependency on it (same rule the TDS app documents for its own auth.js).
/** Escapes a value for safe interpolation into innerHTML. */
function escapeHtmlAuth(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// Backstop for any promise rejection that never reached a try/catch -
// surfaces silently-swallowed failures in the console instead of nothing.
window.addEventListener('unhandledrejection', (e) => {
  console.warn('Unhandled promise rejection:', e.reason);
});
