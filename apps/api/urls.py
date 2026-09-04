from django.urls import include, path

from apps.api import views
from apps.api.auth_views import PTLoginView, PTTokenRefreshView, PTTokenVerifyView, whoami
from apps.api.routers import achhad_views, hrs_views, imports_views, vapi_views

urlpatterns = [
    path("health", views.health, name="health"),

    # ── Authentication ────────────────────────────────────────────────────
    path("auth/login", PTLoginView.as_view(), name="auth-login"),
    path("auth/token/refresh", PTTokenRefreshView.as_view(), name="token-refresh"),
    path("auth/token/verify", PTTokenVerifyView.as_view(), name="token-verify"),
    path("auth/me", whoami, name="auth-me"),
    # Device trust (OTP verify + logout)
    path("", include("apps.api.routers.device_urls")),
    # Google OAuth 2.0
    path("", include("apps.api.routers.google_oauth_urls")),
    # In-app user management (Admin Panel) - list/create/update
    path("", include("apps.api.routers.users_urls")),

    path("purchase-orders", hrs_views.purchase_orders, name="hrs-purchase-orders"),
    path("purchase-orders/<str:po_number>/fields", hrs_views.correct_field, name="hrs-correct-field"),
    path("materials", hrs_views.materials, name="hrs-materials"),
    path("materials/<int:lot_id>/fields", hrs_views.correct_material_field, name="hrs-correct-material-field"),
    path("materials/<int:lot_id>/stock-trend", hrs_views.stock_trend, name="hrs-stock-trend"),
    path("sync-status", hrs_views.sync_status, name="hrs-sync-status"),
    # Admin-only - triggers a real Drive sync in the background, see
    # apps/services/sync_trigger.py. Added 2026-09-04 alongside
    # syncInProgress on sync-status above (frontend polls that to know when
    # a triggered run finishes).
    path("sync-trigger", hrs_views.sync_trigger, name="hrs-sync-trigger"),
    # Dismiss/override a flagged match - see apps/services/match_dismiss.py
    # for why this is one shared implementation wired per-plant here rather
    # than a shared router (matches HRS's own routing style, each plant's
    # own URL segment, not a ?plant= query param).
    path("matches/po-mir/<int:match_id>/dismiss", hrs_views.dismiss_po_mir_match, name="hrs-dismiss-po-mir"),
    path("matches/mir-stock/<int:match_id>/dismiss", hrs_views.dismiss_mir_stock_match, name="hrs-dismiss-mir-stock"),
    # Manual dismiss/reinstate for a PO-level flag (Quantity/Rate-Value
    # Discrepancy, Data Quality Flag category) shown in the Flags &
    # Corrections tab - see apps/services/flag_dismiss.py.
    path("purchase-orders/<str:po_number>/flags/dismiss", hrs_views.dismiss_flag, name="hrs-dismiss-flag"),
    # RTP-Achhad/RTP-Vapi - same shape as the HRS routes above, each under
    # its own /achhad/ or /vapi/ prefix (rather than a ?plant= query param)
    # so every plant's URLs stay trivially cacheable/greppable/bookmarkable
    # on their own.
    path("achhad/purchase-orders", achhad_views.purchase_orders, name="achhad-purchase-orders"),
    path("achhad/purchase-orders/<str:po_number>/fields", achhad_views.correct_field, name="achhad-correct-field"),
    path("achhad/materials", achhad_views.materials, name="achhad-materials"),
    path("achhad/materials/<int:lot_id>/fields", achhad_views.correct_material_field, name="achhad-correct-material-field"),
    path("achhad/materials/<int:lot_id>/stock-trend", achhad_views.stock_trend, name="achhad-stock-trend"),
    path("achhad/sync-status", achhad_views.sync_status, name="achhad-sync-status"),
    path("achhad/sync-trigger", achhad_views.sync_trigger, name="achhad-sync-trigger"),
    path("achhad/matches/po-mir/<int:match_id>/dismiss", achhad_views.dismiss_po_mir_match, name="achhad-dismiss-po-mir"),
    path("achhad/matches/mir-stock/<int:match_id>/dismiss", achhad_views.dismiss_mir_stock_match, name="achhad-dismiss-mir-stock"),
    path("achhad/purchase-orders/<str:po_number>/flags/dismiss", achhad_views.dismiss_flag, name="achhad-dismiss-flag"),
    path("vapi/purchase-orders", vapi_views.purchase_orders, name="vapi-purchase-orders"),
    path("vapi/purchase-orders/<str:po_number>/fields", vapi_views.correct_field, name="vapi-correct-field"),
    path("vapi/materials", vapi_views.materials, name="vapi-materials"),
    path("vapi/materials/<int:lot_id>/fields", vapi_views.correct_material_field, name="vapi-correct-material-field"),
    path("vapi/materials/<int:lot_id>/stock-trend", vapi_views.stock_trend, name="vapi-stock-trend"),
    path("vapi/sync-status", vapi_views.sync_status, name="vapi-sync-status"),
    path("vapi/sync-trigger", vapi_views.sync_trigger, name="vapi-sync-trigger"),
    path("vapi/matches/po-mir/<int:match_id>/dismiss", vapi_views.dismiss_po_mir_match, name="vapi-dismiss-po-mir"),
    path("vapi/matches/mir-stock/<int:match_id>/dismiss", vapi_views.dismiss_mir_stock_match, name="vapi-dismiss-mir-stock"),
    path("vapi/purchase-orders/<str:po_number>/flags/dismiss", vapi_views.dismiss_flag, name="vapi-dismiss-flag"),

    # Import Purchase Dashboard - cross-plant combined (see imports_views.py's
    # module docstring for why this doesn't split into per-plant routers).
    path("imports/purchase-orders", imports_views.purchase_orders, name="imports-purchase-orders"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>", imports_views.purchase_order_detail, name="imports-purchase-order-detail"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/fields", imports_views.correct_field, name="imports-correct-field"),
    path("imports/sync-status", imports_views.sync_status, name="imports-sync-status"),
    path("imports/sync-trigger/<str:plant>", imports_views.sync_trigger, name="imports-sync-trigger"),
    path("imports/matches/po-mir/<str:plant>/<int:match_id>/dismiss", imports_views.dismiss_import_po_mir_match, name="imports-dismiss-po-mir"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/flags/dismiss", imports_views.dismiss_flag, name="imports-dismiss-flag"),
]
