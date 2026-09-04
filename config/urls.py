"""
config/urls.py — Root URLconf.

Deliberately tiny: this app has no server-rendered pages of its own beyond
Django Admin. Everything the frontend calls goes through /api/ (DRF views,
apps/api/urls.py); every other path falls through to the catch-all below and
is handled client-side by the static frontend in frontend/.
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    # Django Admin — used for the initial PTUser bootstrap (before any admin
    # exists to use the in-app Users panel) and as a browsable, read-only view
    # onto PTAuditLog. Gated by AdminOnlyCsrfMiddleware (config/middleware.py),
    # not the JWT auth the rest of this app uses.
    path("admin/", admin.site.urls),
    # All JSON API routes (auth, PO/MIR/stock data per plant, users, etc.)
    # live under apps/api/urls.py, mounted here under /api/.
    path("api/", include("apps.api.urls")),
    # Catch-all: serves frontend/index.html for the root path. WhiteNoise
    # (config/middleware.py's ordering note) serves every other static
    # frontend/ asset (css/js/other .html pages) directly — this entry only
    # covers "/" itself, since WhiteNoise has nothing to serve for that path
    # without an explicit index mapping.
    path("", TemplateView.as_view(template_name="index.html")),
]
