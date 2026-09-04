from django.http import JsonResponse


def health(request):
    """Unauthenticated liveness check - deliberately not cached, mirrors
    the TDS app's /api/ health-check convention."""
    return JsonResponse({"status": "ok"})
