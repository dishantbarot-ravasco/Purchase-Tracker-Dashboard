"""
Server-side access enforcement. Every view that returns plant data MUST use
one of these, since hiding a plant in the frontend is not a real security
boundary, someone could still call the API directly.
"""
import functools

from django.http import JsonResponse

from .models import UserAccess


def require_login(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        email = request.session.get("email")
        if not email:
            return JsonResponse({"error": "Not signed in."}, status=401)
        try:
            request.user_access = UserAccess.objects.get(email=email, is_active=True)
        except UserAccess.DoesNotExist:
            return JsonResponse({"error": "No access has been configured for your account yet. Ask an admin."}, status=403)
        return view_func(request, *args, **kwargs)

    return wrapper


def require_admin(view_func):
    @require_login
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user_access.role != "admin":
            return JsonResponse({"error": "Admin access required."}, status=403)
        return view_func(request, *args, **kwargs)

    return wrapper


def require_plant_access(get_plant_from_kwargs):
    """Decorator factory: pass a function that extracts the requested plant
    code from the view's args/kwargs, e.g. `lambda request, plant, **kw: plant`.
    Rejects the request if the signed-in user isn't allowed that plant -
    this is the actual enforcement point, not a UI-level filter."""

    def decorator(view_func):
        @require_login
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            requested_plant = get_plant_from_kwargs(request, *args, **kwargs)
            if requested_plant not in request.user_access.allowed_plants():
                return JsonResponse({"error": "You don't have access to this plant."}, status=403)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
