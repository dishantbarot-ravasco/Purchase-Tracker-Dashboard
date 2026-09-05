// login.html's theme-toggle button wiring - extracted from an inline
// <script> so script-src can drop 'unsafe-inline' (see
// config/security_headers.py). Self-contained/standalone rather than
// reusing js/auth.js's initThemeToggle(): login.html is the one page that
// doesn't call requireAuth()/load the shared nav (there's no session yet),
// so it wires its own toggle button independently - same behavior, kept as
// its own copy rather than refactored during this extraction.
(function () {
  var KEY = 'pt-theme';
  var btn = document.getElementById('themeToggleBtn');
  function isDark() {
    var stored = null;
    try { stored = localStorage.getItem(KEY); } catch (e) {}
    if (stored) return stored === 'dark';
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function render() {
    var dark = isDark();
    btn.innerHTML = dark ? '&#9728;&#65039;' : '&#127769;';
    btn.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    btn.setAttribute('aria-label', btn.title);
  }
  btn.addEventListener('click', function () {
    var next = isDark() ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
    render();
  });
  render();
})();
