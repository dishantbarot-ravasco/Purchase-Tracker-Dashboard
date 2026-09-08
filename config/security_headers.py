"""
config/security_headers.py — Adds hardened HTTP security headers to every response.

Ported from the TDS Automation App's config/security_headers.py.

Wire into settings.py MIDDLEWARE list BEFORE WhiteNoise: Django's middleware
list is outermost-first for the request phase, so a LATER entry is more
INNER — WhiteNoiseMiddleware, being earlier (outer), short-circuits
static-file requests (every frontend HTML/CSS/JS response) by returning
directly without ever calling further down the chain. Putting this
middleware before WhiteNoise makes it outer, so it wraps and can add
headers to WhiteNoise's response too — the TDS app shipped with this
ordering backwards for a while and every static HTML page (i.e. every page
a browser actually renders and executes) carried none of these headers,
so the CSP was providing zero real protection on the pages that mattered
most.

CSP notes:
  - 'self' covers all local assets (WhiteNoise-served JS/CSS/images).
  - 'unsafe-inline' was removed from script-src (2026-09-05, hardening pass).
    Every inline <script> block that used to appear directly in
    frontend/*.html was extracted to its own external file under
    frontend/js/ (theme-init.js, login-theme-toggle.js, home-page.js,
    admin-page.js, search-po-page.js), and every inline event-handler
    attribute (onerror="...", onclick="...") was replaced with a real
    addEventListener - a delegated listener in theme-init.js for the
    logo-fallback onerror pattern (every page loads this file), and one in
    charts.js for the close-modal-button onclick pattern (only the
    dashboard's own modals use it). 'self' already covers every external
    file, so no nonce/hash was needed - see CLAUDE.md's "Security hardening
    pass" for the full list of what moved where. If you ever add a new
    inline <script> block or inline event-handler attribute to any page,
    it will be silently blocked by this CSP with no visible error other
    than a browser console CSP violation message - extract it to an
    external file instead, following the same pattern.
  - 'unsafe-inline' was removed from style-src too (2026-09-08). Confirmed
    empirically first (against a real browser, before writing any fix) that
    CSP's style-src blocks a literal style="..." HTML attribute (whether
    written directly in markup or inserted via innerHTML) AND a page's own
    inline <style> block (every one of frontend/{admin,home,login,review,
    search-po}.html had one, in the <head> - each extracted verbatim to its
    own external css/<page>-page.css file, same pattern as the earlier
    script-src work) - it does NOT restrict setting an element's .style
    property from JS afterwards (el.style.color = ..., including
    el.style.cssText = ...), so that stayed untouched. Every literal
    style="..." site across frontend/*.html and frontend/js/*.js was
    replaced with one of the utility classes in
    brand.css's "Utility classes" comment (margins/colors/font-sizes/flex
    layouts that were always fixed values, not actually dynamic); the
    handful of genuinely per-row/per-value dynamic sites (an avatar/legend
    color, a bar-fill width, a chart panel's height) render a data-*
    attribute instead and get their real style applied by a small JS pass
    right after the innerHTML assignment (shared.js's applyDynamicStyles(),
    or a self-contained equivalent in admin-page.js's renderBarList()) -
    see that function's own comment for why this is safe under the new CSP
    when a literal style="..." attribute isn't. Every display:none/[hidden]
    toggle that used to be a literal inline style is now the real `hidden`
    HTML attribute + the .hidden IDL property in JS (brand.css's blanket
    [hidden]{display:none!important} rule - added this same pass - makes
    that reliable even against a class that sets its own display, the exact
    failure mode CLAUDE.md's "Edit Everywhere" section already documents
    for .edit-actions[hidden]). Verified in a real browser (Browser pane)
    against every page (dashboard, home, admin, login, search-po) with the
    flag actually removed, not just by code inspection - zero console CSP
    violations, zero regressions in manual click-through (login/OTP, PO/
    import/material modals, admin user create/edit, search).
  - cdn.jsdelivr.net is explicitly allowed on script-src: frontend/index.html
    loads Chart.js from there (no vendored/bundled copy, consistent with the
    no-build-step approach above). Without this, the browser silently drops
    the CSP-blocked <script> tag - Chart.js never loads, `Chart` stays
    undefined, and the first `new Chart(...)` call (inside renderPoList(),
    any time there is at least one dated PO to plot) throws a bare
    ReferenceError that unwinds all the way to loadDashboard()'s catch,
    replacing an already-correctly-rendered dashboard with the generic
    "Couldn't load the dashboard right now" message - a real, confirmed
    failure mode (2026-09-03), not a hypothetical one. If another CDN
    script is ever added, its origin needs the same treatment here or it
    will fail exactly the same way, silently, in every browser at once.
  - Google Fonts is explicitly allowed (fonts.googleapis.com, fonts.gstatic.com) —
    ported for parity even though this frontend doesn't currently reference one.
  - frame-ancestors 'none' blocks embedding in other pages.
  - object-src 'none' blocks Flash and other plugin objects entirely.
"""

from django.conf import settings


class SecurityHeadersMiddleware:
    """Attach hardened security headers to every response."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_prod = not settings.DEBUG

    def __call__(self, request):
        response = self.get_response(request)
        self._add_headers(response)
        return response

    def _add_headers(self, response):
        """Mutate `response` in place, adding every header this middleware owns."""
        h = response

        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "payment=(), usb=(), bluetooth=()"
        )

        if self._is_prod:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

        h["Content-Security-Policy"] = self._build_csp()

    def _build_csp(self):
        """Assemble the Content-Security-Policy header value (see module
        docstring's "CSP notes" for why each directive/origin is here)."""
        directives = [
            "default-src 'self'",
            "style-src 'self' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data: blob:",
            "script-src 'self' https://cdn.jsdelivr.net",
            "connect-src 'self'",
            "frame-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]

        extra = getattr(settings, "CSP_EXTRA_DIRECTIVES", "")
        if extra:
            directives.append(extra.rstrip(";"))

        return "; ".join(directives)
