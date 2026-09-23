/**
 * frontend/js/auth.js - gates every protected page behind a valid session
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

// ── Silent session renewal (2026-09-23, audit pass) ─────────────────────
// pt_access lives 12h; pt_refresh lives 30 days and the server has always
// been able to trade it for a fresh pair (PTTokenRefreshView reads it from
// the httpOnly cookie, rotates it, and re-cookies both). But nothing in the
// frontend ever called that endpoint: every 401 went straight to
// /login.html, so the 30-day "remember me" never worked in a browser and
// everyone was signed out 12 hours after signing in - mid-edit, if that is
// when it landed, with the freshness watcher's next poll doing the bouncing.
//
// authFetch() is a drop-in for fetch(): on a 401 it renews the session once
// and replays the request. Callers keep their existing 401 -> /login.html
// handling unchanged; it now only fires when renewal genuinely failed
// (refresh cookie expired, "log out everywhere", a password change - every
// case where the server already refuses the refresh token), so none of
// those security properties move. Replaying a PATCH/POST is safe: a 401
// means authentication failed before the view ran, so nothing was written.
let _sessionRefresh = null;

/**
 * Trades the pt_refresh cookie for a new pt_access/pt_refresh pair.
 * SINGLE-FLIGHT, and that is load-bearing: refresh tokens rotate and the
 * spent one is revoked (ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION),
 * so two concurrent refreshes would present the same token twice and the
 * loser would be refused - signing the user out precisely when several
 * requests expired together, which is the normal case (a page load fires
 * several fetches at once). Every caller that arrives while one is in
 * flight awaits that same promise instead.
 */
function refreshSession() {
  if (!_sessionRefresh) {
    _sessionRefresh = fetch('/api/auth/token/refresh', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
      .then(res => res.ok, () => false)
      .finally(() => { _sessionRefresh = null; });
  }
  return _sessionRefresh;
}

/**
 * fetch() that survives an expired access token. On a 401 it renews the
 * session and replays the request exactly once - and replays it even when
 * the renewal itself failed, deliberately: with two tabs open, the other tab
 * may have just rotated the (shared) refresh cookie, so this tab's renewal
 * is refused while the cookie jar already holds a valid new access token.
 * The replay picks that up. If the replay is still a 401, that response is
 * returned and the caller's normal login redirect takes over.
 */
async function authFetch(url, opts) {
  const res = await fetch(url, opts);
  if (res.status !== 401) return res;
  await refreshSession();
  return fetch(url, opts);
}

/**
 * Confirms the browser's httpOnly session cookie is still valid by asking
 * the server (GET /api/auth/me), populating CURRENT_USER on success.
 * Any failure - real 401, or a genuine network error - bounces to
 * /login.html, since there's nothing a protected page can usefully render
 * without a resolved user.
 */
async function requireAuth() {
  try {
    const res = await authFetch('/api/auth/me', { credentials: 'same-origin' });
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
// (admin=gold, editor=blue, viewer=navy) via the .avatar-role-* classes in
// brand.css, so it reads consistently with the role-pill colors used
// elsewhere (e.g. admin.html's user table) rather than an arbitrary
// per-user color.

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
  container.innerHTML =
    '<div class="user-menu" id="userMenuWrap">' +
      '<div class="user-avatar-circle avatar-role-' + escapeHtmlAuth(CURRENT_USER.role) + '">' + escapeHtmlAuth(userInitials(CURRENT_USER)) + '</div>' +
      '<div class="user-info">' +
        '<div class="user-name">' + escapeHtmlAuth(label) + '</div>' +
        '<div class="user-role">' + escapeHtmlAuth(CURRENT_USER.role) + '</div>' +
      '</div>' +
      '<span class="user-chevron">&#9662;</span>' +
      '<div class="user-dropdown" id="userDropdown" hidden>' +
        '<button type="button" id="changePasswordBtn" class="user-dropdown-item">Change Password</button>' +
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
  // openChangePasswordModal() lives in shared.js (loaded right after this
  // file on every protected page) - see that function's own header comment
  // for why it builds its own overlay rather than reusing index.html's
  // dashboard-only #modalBackdrop.
  document.getElementById('changePasswordBtn').onclick = () => openChangePasswordModal();
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
    // Match Accuracy Programme, Phase 1 (doc 03) - any authenticated role
    // can review, not just admin/editor, since throughput (~200 reviews)
    // matters more than gating here (see review_views.py's own docstring).
    { key: 'review', href: '/review.html', label: 'Review Matches' },
  ];
  if (CURRENT_USER.role === 'admin') tabs.push({ key: 'admin', href: '/admin.html', label: 'Admin' });
  container.innerHTML = tabs.map(t =>
    '<a class="nav-tab' + (t.key === activePage ? ' active' : '') + '" href="' + t.href + '">' + escapeHtmlAuth(t.label) + '</a>'
  ).join('');
}

// ── Theme toggle (dark mode) ────────────────────────────────────────────
// 'pt-theme' in localStorage holds an explicit override ('light'/'dark');
// absent, the page follows the OS/browser's prefers-color-scheme instead
// (see brand.css's/style.css's own dark-mode blocks - both the media-query-
// guarded and the [data-theme] tokens exist for exactly this reason). Every
// protected page's <head> also runs a tiny inline copy of applyTheme()'s
// localStorage read, synchronously, before first paint - this module-level
// version is for the toggle button's own click handling and for pages that
// load this file, not a replacement for that inline snippet.
const THEME_KEY = 'pt-theme';

function getStoredTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
}

function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') {
    document.documentElement.setAttribute('data-theme', theme);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
}

/** True if the page is currently rendering dark, whether that's from an
 * explicit override or the OS preference (no override stored). */
function isEffectivelyDark() {
  const stored = getStoredTheme();
  if (stored) return stored === 'dark';
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/**
 * Inserts a light/dark toggle button into the shared top nav (right before
 * the user badge) on every protected page - called once from each page's own
 * bootstrap script, alongside renderNavTabs()/renderUserBadge(). Self-locates
 * '.nav-user' rather than requiring every page to add its own placeholder
 * element, since the shared topnav markup is otherwise identical everywhere.
 */
function initThemeToggle() {
  const navUser = document.querySelector('.nav-user');
  if (!navUser || document.getElementById('themeToggleBtn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'themeToggleBtn';
  btn.className = 'theme-toggle-btn';
  const render = () => {
    const dark = isEffectivelyDark();
    btn.innerHTML = dark ? '&#9728;&#65039;' : '&#127769;'; // sun / crescent moon
    btn.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    btn.setAttribute('aria-label', btn.title);
  };
  btn.addEventListener('click', () => {
    const next = isEffectivelyDark() ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* best-effort */ }
    render();
  });
  render();
  navUser.parentNode.insertBefore(btn, navUser);
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
