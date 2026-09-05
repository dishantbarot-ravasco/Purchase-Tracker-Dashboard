"""
apps/api/routers/device_urls.py — URL routes for device-aware 2FA.

Included in apps/api/urls.py under the /api/ prefix.

Endpoints
---------
POST /api/auth/device-verify      -> device_views.device_verify
POST /api/auth/logout             -> device_views.logout_view
POST /api/auth/logout-everywhere  -> device_views.logout_everywhere_view
"""

from django.urls import path

from . import device_views

urlpatterns = [
    path("auth/device-verify", device_views.device_verify, name="device-verify"),
    path("auth/logout", device_views.logout_view, name="auth-logout"),
    path("auth/logout-everywhere", device_views.logout_everywhere_view, name="auth-logout-everywhere"),
]
