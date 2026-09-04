"""
NoCacheMiddleware
-----------------
Development convenience: forces the browser to always fetch fresh assets
instead of serving stale cached versions.

AdminOnlyCsrfMiddleware
-----------------------
Restores real CSRF protection for Django Admin without touching the JWT API.

Every /api/ endpoint authenticates via a JWT bearer token or the httpOnly
pt_access cookie (SameSite=Lax), never via Django's session-based CSRF
token, so full CsrfViewMiddleware is not enabled app-wide — Django's
CsrfViewMiddleware doesn't know anything about JWT auth and would 403 every
unsafe-method (POST/PUT/PATCH/DELETE) API call regardless of how that view
authenticates (confirmed as a real failure mode in the TDS Automation App
this pattern is ported from).

Django Admin, meanwhile, IS a classic session + HTML-form app: its login
page and every model-add/edit form already render {% csrf_token %} and
expect the standard CSRF check to run. This class re-enables Django's
exact, unmodified CSRF check (by subclassing CsrfViewMiddleware, not
reimplementing it) but ONLY for requests under the admin path prefix -
every /api/ request, and everything else, skips it entirely.

Ported from the TDS Automation App's config/middleware.py.
"""
from django.middleware.csrf import CsrfViewMiddleware

# File extensions that define app behavior/contracts (which endpoints the
# frontend calls, how it parses responses, etc.) - see frontend_cache_headers()
# below for why these specifically need to always revalidate.
_ALWAYS_REVALIDATE_EXTENSIONS = (".html", ".js", ".mjs", ".css")


def frontend_cache_headers(headers, path, url):
    """WHITENOISE_ADD_HEADERS_FUNCTION hook (see WHITENOISE_ROOT in settings.py).

    WhiteNoise's default Cache-Control is `max-age=60, public` for every
    file under frontend/ - harmless for images/fonts, but for .html/.js/.css
    specifically a browser could keep serving a page/script calling a
    since-retired API endpoint for up to 60s after a deploy. Overriding
    Cache-Control to `no-cache` doesn't disable caching, it forces a
    conditional GET on every load, so a browser always finds out within one
    request whether the server has something newer."""
    if path.endswith(_ALWAYS_REVALIDATE_EXTENSIONS):
        headers["Cache-Control"] = "no-cache, public"


class NoCacheMiddleware:
    """Dev-only: stamp every response with headers that forbid caching at all.

    Only inserted into MIDDLEWARE when DEBUG=True (see settings.py) — in
    production, static assets should cache; in dev, a stale cached copy of a
    frontend .js/.css file masking an in-progress edit is a worse failure
    mode than always refetching.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response


class AdminOnlyCsrfMiddleware(CsrfViewMiddleware):
    """CSRF enforcement scoped to Django Admin (/admin/) only."""

    ADMIN_PATH_PREFIX = "/admin/"

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if not request.path.startswith(self.ADMIN_PATH_PREFIX):
            return None  # not an Admin request - skip CSRF checking entirely
        return super().process_view(request, callback, callback_args, callback_kwargs)
