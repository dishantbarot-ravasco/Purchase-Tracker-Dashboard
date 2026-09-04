"""
apps/api/routers/users_urls.py — URL routes for in-app user management.

Included in apps/api/urls.py under the /api/ prefix. See users_views.py's
module docstring for the endpoints' own behavior/history - this file only
wires paths to view functions.

Endpoints
---------
GET   /api/auth/users          -> users_views.list_users
POST  /api/auth/users/create   -> users_views.create_user
PATCH /api/auth/users/<id>     -> users_views.update_user
"""

from django.urls import path

from apps.api.routers import users_views

urlpatterns = [
    path("auth/users", users_views.list_users, name="users-list"),
    path("auth/users/create", users_views.create_user, name="users-create"),
    path("auth/users/<int:user_id>", users_views.update_user, name="users-update"),
]
