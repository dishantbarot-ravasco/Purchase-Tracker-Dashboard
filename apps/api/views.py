"""
apps/api/views.py — Misc top-level API views not specific to auth or a plant.

Currently just the liveness check. Everything plant-specific (purchase
orders, materials, sync status, matches) lives under apps/api/routers/
instead - this module intentionally stays small.
"""

from django.http import JsonResponse


def health(request):
    """Unauthenticated liveness check - deliberately not cached, mirrors
    the TDS app's /api/ health-check convention."""
    return JsonResponse({"status": "ok"})
