from django.urls import path

from apps.api.routers import users_views

urlpatterns = [
    path("auth/users", users_views.list_users, name="users-list"),
    path("auth/users/create", users_views.create_user, name="users-create"),
    path("auth/users/<int:user_id>", users_views.update_user, name="users-update"),
]
