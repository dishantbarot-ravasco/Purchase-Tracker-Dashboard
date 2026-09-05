// Applies a stored theme choice before first paint - extracted from an
// identical inline <script> that used to appear in the <head> of every page
// (index.html/home.html/admin.html/search-po.html/login.html), so
// script-src can drop 'unsafe-inline' (see config/security_headers.py) -
// pairs with js/auth.js's initThemeToggle()/applyTheme() toggle.
(function () {
  try {
    var t = localStorage.getItem('pt-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();

// Logo-fallback (hide the brand image if it 404s) - extracted from
// identical onerror="this.style.display='none'" HTML attributes previously
// on every page's logo <img> (an inline event-handler attribute, which
// CSP's script-src treats the same as an inline <script> block - see
// config/security_headers.py). Delegated so it works regardless of load
// order; the 'error' event on <img> doesn't bubble, so this must listen in
// the CAPTURE phase (the `true` third argument), not the default bubble
// phase, or it would never fire. Pages mark the image with
// data-hide-on-error instead of the old onclick-style attribute.
document.addEventListener('error', function (e) {
  if (e.target && e.target.matches && e.target.matches('img[data-hide-on-error]')) {
    e.target.style.display = 'none';
  }
}, true);
