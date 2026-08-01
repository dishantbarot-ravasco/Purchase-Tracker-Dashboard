from django.urls import path
from django.views.generic import TemplateView

from . import auth_views, views

urlpatterns = [
    # Auth
    path("auth/google", auth_views.login_start, name="login_start"),
    path("auth/google/callback", auth_views.login_callback, name="login_callback"),
    path("auth/logout", auth_views.logout, name="logout"),
    path("api/me", auth_views.me, name="me"),

    # Data (each of these enforces plant access server-side, see decorators.py)
    path("api/dashboard", views.dashboard, name="dashboard"),
    path("api/plants/<str:plant>/purchase-orders", views.plant_purchase_orders, name="plant_purchase_orders"),
    path("api/plants/<str:plant>/stock-trend", views.plant_stock_trend, name="plant_stock_trend"),
    path("api/purchase-orders/<str:po_number>/flags", views.submit_flag, name="submit_flag"),

    # Admin-only: manage the user/role list (GET list / POST upsert on the
    # collection path, DELETE on the per-email path)
    path("api/admin/users", views.admin_users, name="admin_users"),
    path("api/admin/users/<str:email>", views.admin_user_detail, name="admin_user_detail"),
    path("api/admin/diagnostics", views.admin_diagnostics, name="admin_diagnostics"),

    # Frontend shell - served for every other route (no client-side routing
    # beyond one screen, same pattern as the earlier Node prototype)
    path("", TemplateView.as_view(template_name="index.html"), name="index"),
]
