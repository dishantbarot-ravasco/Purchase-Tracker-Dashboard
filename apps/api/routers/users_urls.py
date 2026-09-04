"""
apps/api/routers/users_urls.py — URL routes for in-app user management.

Included in apps/api/urls.py under the /api/ prefix. See users_views.py's
module docstring for the endpoints' own behavior/history - this file only
wires paths to view functions.

Endpoints
---------
GET    /api/auth/users                          -> users_views.list_users
POST   /api/auth/users/create                    -> users_views.create_user
PATCH  /api/auth/users/<id>                       -> users_views.update_user
GET    /api/auth/users/<id>/devices               -> users_views.list_user_devices
DELETE /api/auth/users/<id>/devices/<device_id>   -> users_views.revoke_user_device
"""

from django.urls import path

from apps.api.routers import users_views

urlpatterns = [
    path("auth/users", users_views.list_users, name="users-list"),
    path("auth/users/create", users_views.create_user, name="users-create"),
    path("auth/users/<int:user_id>", users_views.update_user, name="users-update"),
    path("auth/users/<int:user_id>/devices", users_views.list_user_devices, name="users-devices-list"),
    path("auth/users/<int:user_id>/devices/<int:device_id>", users_views.revoke_user_device, name="users-devices-revoke"),
]
