from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from . import auth_views, views

# ensure_csrf_cookie guarantees the csrftoken cookie actually gets set on
# first page load. Without this, Django's CsrfViewMiddleware (active
# globally) has nothing to compare against, and every POST/DELETE from the
# frontend (Admin tab add/remove, PO corrections) fails with a 403 - the
# frontend's fetch calls read this cookie and send it back as the
# X-CSRFToken header, see public/index.html's api() helper.
index_view = ensure_csrf_cookie(TemplateView.as_view(template_name="index.html"))

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
    path("", index_view, name="index"),
]
