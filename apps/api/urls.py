"""
apps/api/urls.py - URL routing for the `apps.api` app, mounted under /api/.

Auth endpoints are defined directly here (auth_views.py); everything
plant-specific is deliberately split into its own router module under
apps/api/routers/ (hrs_views.py / achhad_views.py / vapi_views.py /
imports_views.py) with its own URL prefix per plant, rather than one
shared router keyed on a `?plant=` query param - see urlpatterns below and
the "Per-plant models, not a shared schema" note in this app's CLAUDE.md
for why each plant gets its own everything (models, parsers, matching
module, router) instead of one generic table/view with a plant column.
"""

from django.urls import include, path

from apps.api import views
from apps.api.auth_views import PTLoginView, PTTokenRefreshView, PTTokenVerifyView, whoami
from apps.api.routers import achhad_views, admin_overview_views, document_views, hrs_views, imports_views, mir_views, password_views, preferences_views, reports_views, review_views, stock_views, vapi_views

urlpatterns = [
    path("health", views.health, name="health"),
    path("health/ready", views.readiness, name="health-ready"),
    # Hit once a day (e.g. 20:30 IST) by an external free scheduler (cron-job.org)
    # - see reports_views.py's own module docstring for the shared-secret
    # auth scheme (no login session/JWT possible for that caller).
    path("internal/send-daily-report", reports_views.trigger_daily_report, name="trigger-daily-report"),
    path("internal/send-monthly-report", reports_views.trigger_monthly_report, name="trigger-monthly-report"),
    path("internal/send-mismatch-report", reports_views.trigger_mismatch_report, name="trigger-mismatch-report"),
    path("internal/prune-revoked-tokens", reports_views.trigger_prune_revoked_tokens, name="trigger-prune-revoked-tokens"),
    path(
        "internal/send-advance-license-expiry-report",
        reports_views.trigger_advance_license_expiry_report,
        name="trigger-advance-license-expiry-report",
    ),

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
    # Self-service "Change Password", OTP-gated like new-device login
    path("auth/change-password/request", password_views.request_password_change, name="change-password-request"),
    path("auth/change-password/confirm", password_views.confirm_password_change, name="change-password-confirm"),
    # Admin Panel Overview tab - top correctors/vendors + recent activity
    path("auth/admin-overview", admin_overview_views.admin_overview, name="admin-overview"),

    # Per-user sort presets (2026-09-28) - apps/api/routers/preferences_views.py.
    path("sort-presets", preferences_views.presets, name="sort-presets"),
    path("sort-presets/<int:preset_id>", preferences_views.preset, name="sort-preset"),

    # MIR entry against the normalized POs (2026-09-28) - apps/api/routers/mir_views.py.
    path("mir/meta", mir_views.meta, name="mir-meta"),
    path("mir/open-pos", mir_views.open_pos, name="mir-open-pos"),
    path("mir/purchase-orders/<int:po_id>", mir_views.purchase_order, name="mir-purchase-order"),
    path("mir/vendors", mir_views.vendors, name="mir-vendors"),
    path("mir/preview", mir_views.preview, name="mir-preview"),
    path("mir/entries", mir_views.entries, name="mir-entries"),
    path("mir/entries/new", mir_views.post_entry, name="mir-post"),
    path("mir/entries/<int:mir_id>", mir_views.entry, name="mir-entry"),
    path("mir/entries/<int:mir_id>/cancel", mir_views.cancel_entry, name="mir-cancel"),
    path("mir/entries/<int:mir_id>/edit", mir_views.edit_entry, name="mir-edit"),
    path("mir/entries/<int:mir_id>/lines/<int:line_no>/reject", mir_views.reject_line, name="mir-reject-line"),
    path("mir/mismatches", mir_views.mismatches, name="mir-mismatches"),
    path("mir/mismatches/<int:mismatch_id>/resolve", mir_views.resolve, name="mir-resolve"),
    path("mir/po-lines/<int:line_id>/close", mir_views.close_line, name="mir-close-line"),
    path("mir/materials/<int:material_id>/category", mir_views.material_category, name="mir-material-category"),
    path("mir/po-lines/<int:line_id>/reopen", mir_views.reopen_line, name="mir-reopen-line"),
    path("mir/po-lines/<int:line_id>/review", mir_views.review_line, name="mir-review-line"),
    # Uploaded PO and invoice files, stored in Cloudflare R2 (2026-09-30) - apps/api/routers/document_views.py.
    path("documents/po", document_views.po_documents, name="po-documents"),
    path("documents/po/upload", document_views.upload_po_document, name="po-document-upload"),
    path("documents/<int:document_id>/open", document_views.open_document, name="document-open"),
    path("documents/<int:document_id>/withdraw", document_views.withdraw_document, name="document-withdraw"),
    path("mir/entries/<int:mir_id>/invoice", document_views.mir_invoice, name="mir-invoice"),
    # RM store (2026-09-29; issue from a chosen MIR 2026-09-30) - apps/api/routers/stock_views.py.
    path("stock/meta", stock_views.meta, name="stock-meta"),
    path("stock/receipts", stock_views.receipts, name="stock-receipts"),
    path("stock/receipts/<int:lot_id>", stock_views.receipt, name="stock-receipt"),
    path("stock/register", stock_views.register, name="stock-register"),
    path("stock/differences", stock_views.differences, name="stock-differences"),
    path("stock/settings", stock_views.settings, name="stock-settings"),
    path("stock/materials/<int:material_id>/units", stock_views.material_units, name="stock-material-units"),
    path("stock/preview", stock_views.preview, name="stock-preview"),
    path("stock/vouchers", stock_views.vouchers, name="stock-vouchers"),
    path("stock/vouchers/new", stock_views.post_voucher, name="stock-post"),
    path("stock/vouchers/<int:voucher_id>", stock_views.voucher, name="stock-voucher"),
    path("stock/vouchers/<int:voucher_id>/cancel", stock_views.cancel_voucher, name="stock-cancel"),
    path("stock/vouchers/<int:voucher_id>/approve", stock_views.approve_voucher, name="stock-approve"),
    path("stock/vouchers/<int:voucher_id>/reject", stock_views.reject_voucher, name="stock-reject"),
    path("purchase-orders", hrs_views.purchase_orders, name="hrs-purchase-orders"),
    path("purchase-orders/summary", hrs_views.purchase_order_summary, name="hrs-purchase-order-summary"),
    path("purchase-orders/<str:po_number>/fields", hrs_views.correct_field, name="hrs-correct-field"),
    path("purchase-orders/<str:po_number>/mir-candidates", hrs_views.mir_candidates, name="hrs-mir-candidates"),
    path("purchase-orders/<str:po_number>/mir-match", hrs_views.set_mir_match, name="hrs-set-mir-match"),
    path("purchase-orders/<str:po_number>/mir-match/preview", hrs_views.preview_mir_match, name="hrs-preview-mir-match"),
    path("purchase-orders/<str:po_number>/manual-changes", hrs_views.manual_changes, name="hrs-manual-changes"),
    path("mir-match-previews/<str:preview_id>", hrs_views.preview_mir_match_status, name="hrs-preview-mir-match-status"),
    path("materials", hrs_views.materials, name="hrs-materials"),
    path("materials/<int:lot_id>/fields", hrs_views.correct_material_field, name="hrs-correct-material-field"),
    path("materials/<int:lot_id>/stock-trend", hrs_views.stock_trend, name="hrs-stock-trend"),
    # Snapshot Pipeline Rebuild, Phase C (see CLAUDE.md) - a whole plant's
    # stock position on one date, not just one lot's trend.
    path("stock-snapshots/dates", hrs_views.stock_snapshot_dates, name="hrs-stock-snapshot-dates"),
    path("stock-snapshots", hrs_views.stock_snapshots_for_date, name="hrs-stock-snapshots"),
    # Data Export (2026-09-08) - full daily RM stock snapshot history as a
    # downloadable CSV, IsEditor-gated (see make_export_stock_snapshots()'s
    # own docstring for why this endpoint is narrower than every other GET
    # here).
    path("stock-snapshots/export", hrs_views.export_stock_snapshots, name="hrs-export-stock-snapshots"),
    # The "purchased without a PO" drill-down (2026-09-21) - which receipts
    # have no order behind them, split by whether anything is actually
    # pending on them. `?bucket=` narrows, `?download=csv` downloads. See
    # _domestic_base.make_mir_without_po() and services/mir_without_po.py.
    path("mir-without-po", hrs_views.mir_without_po, name="hrs-mir-without-po"),
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
    path("achhad/purchase-orders/summary", achhad_views.purchase_order_summary, name="achhad-purchase-order-summary"),
    path("achhad/purchase-orders/<str:po_number>/fields", achhad_views.correct_field, name="achhad-correct-field"),
    path("achhad/purchase-orders/<str:po_number>/mir-candidates", achhad_views.mir_candidates, name="achhad-mir-candidates"),
    path("achhad/purchase-orders/<str:po_number>/mir-match", achhad_views.set_mir_match, name="achhad-set-mir-match"),
    path("achhad/purchase-orders/<str:po_number>/mir-match/preview", achhad_views.preview_mir_match, name="achhad-preview-mir-match"),
    path("achhad/purchase-orders/<str:po_number>/manual-changes", achhad_views.manual_changes, name="achhad-manual-changes"),
    path("achhad/mir-match-previews/<str:preview_id>", achhad_views.preview_mir_match_status, name="achhad-preview-mir-match-status"),
    path("achhad/materials", achhad_views.materials, name="achhad-materials"),
    path("achhad/materials/<int:lot_id>/fields", achhad_views.correct_material_field, name="achhad-correct-material-field"),
    path("achhad/materials/<int:lot_id>/stock-trend", achhad_views.stock_trend, name="achhad-stock-trend"),
    path("achhad/stock-snapshots/dates", achhad_views.stock_snapshot_dates, name="achhad-stock-snapshot-dates"),
    path("achhad/stock-snapshots", achhad_views.stock_snapshots_for_date, name="achhad-stock-snapshots"),
    path("achhad/stock-snapshots/export", achhad_views.export_stock_snapshots, name="achhad-export-stock-snapshots"),
    path("achhad/mir-without-po", achhad_views.mir_without_po, name="achhad-mir-without-po"),
    path("achhad/sync-status", achhad_views.sync_status, name="achhad-sync-status"),
    path("achhad/sync-trigger", achhad_views.sync_trigger, name="achhad-sync-trigger"),
    path("achhad/matches/po-mir/<int:match_id>/dismiss", achhad_views.dismiss_po_mir_match, name="achhad-dismiss-po-mir"),
    path("achhad/matches/mir-stock/<int:match_id>/dismiss", achhad_views.dismiss_mir_stock_match, name="achhad-dismiss-mir-stock"),
    path("achhad/purchase-orders/<str:po_number>/flags/dismiss", achhad_views.dismiss_flag, name="achhad-dismiss-flag"),
    path("vapi/purchase-orders", vapi_views.purchase_orders, name="vapi-purchase-orders"),
    path("vapi/purchase-orders/summary", vapi_views.purchase_order_summary, name="vapi-purchase-order-summary"),
    path("vapi/purchase-orders/<str:po_number>/fields", vapi_views.correct_field, name="vapi-correct-field"),
    path("vapi/purchase-orders/<str:po_number>/mir-candidates", vapi_views.mir_candidates, name="vapi-mir-candidates"),
    path("vapi/purchase-orders/<str:po_number>/mir-match", vapi_views.set_mir_match, name="vapi-set-mir-match"),
    path("vapi/purchase-orders/<str:po_number>/mir-match/preview", vapi_views.preview_mir_match, name="vapi-preview-mir-match"),
    path("vapi/purchase-orders/<str:po_number>/manual-changes", vapi_views.manual_changes, name="vapi-manual-changes"),
    path("vapi/mir-match-previews/<str:preview_id>", vapi_views.preview_mir_match_status, name="vapi-preview-mir-match-status"),
    path("vapi/materials", vapi_views.materials, name="vapi-materials"),
    path("vapi/materials/<int:lot_id>/fields", vapi_views.correct_material_field, name="vapi-correct-material-field"),
    path("vapi/materials/<int:lot_id>/stock-trend", vapi_views.stock_trend, name="vapi-stock-trend"),
    path("vapi/stock-snapshots/dates", vapi_views.stock_snapshot_dates, name="vapi-stock-snapshot-dates"),
    path("vapi/stock-snapshots", vapi_views.stock_snapshots_for_date, name="vapi-stock-snapshots"),
    path("vapi/stock-snapshots/export", vapi_views.export_stock_snapshots, name="vapi-export-stock-snapshots"),
    path("vapi/mir-without-po", vapi_views.mir_without_po, name="vapi-mir-without-po"),
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
    path("imports/purchase-orders/<str:plant>/<str:po_number>/mir-candidates", imports_views.mir_candidates, name="imports-mir-candidates"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/mir-match", imports_views.set_mir_match, name="imports-set-mir-match"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/mir-match/preview", imports_views.preview_mir_match, name="imports-preview-mir-match"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/manual-changes", imports_views.manual_changes_view, name="imports-manual-changes"),
    path("imports/mir-match-previews/<str:plant>/<str:preview_id>", imports_views.preview_mir_match_status, name="imports-preview-mir-match-status"),
    path("imports/sync-status", imports_views.sync_status, name="imports-sync-status"),
    path("imports/sync-trigger/<str:plant>", imports_views.sync_trigger, name="imports-sync-trigger"),
    path("imports/matches/po-mir/<str:plant>/<int:match_id>/dismiss", imports_views.dismiss_import_po_mir_match, name="imports-dismiss-po-mir"),
    path("imports/purchase-orders/<str:plant>/<str:po_number>/flags/dismiss", imports_views.dismiss_flag, name="imports-dismiss-flag"),
    path("imports/track-bl", imports_views.track_bl, name="imports-track-bl"),

    # RoDTEP scrip ledger (added 2026-09-09) - company-wide, not per-plant
    # (see RodtepScrollEntry's own docstring) - lives under imports/ since
    # it's specifically an import-duty-offset mechanism, same reasoning the
    # project owner gave for scoping this to Imports only.
    path("imports/rodtep", imports_views.rodtep_ledger, name="imports-rodtep-ledger"),
    path("imports/rodtep/sync-trigger", imports_views.rodtep_sync_trigger, name="imports-rodtep-sync-trigger"),
    path("imports/rodtep/<str:script_no>", imports_views.rodtep_script_detail, name="imports-rodtep-script-detail"),

    # Advance License ledger (added 2026-09-09) - company-wide, not per-plant
    # (see AdvanceLicense's own docstring), same "Imports only" scoping
    # reasoning as RoDTEP directly above.
    path("imports/advance-license", imports_views.advance_license_ledger, name="imports-advance-license-ledger"),
    path("imports/advance-license/sync-trigger", imports_views.advance_license_sync_trigger, name="imports-advance-license-sync-trigger"),

    # Match Accuracy Programme, Phase 1 - the review screen (doc 03, 1.2).
    # Cross-plant like imports_views.py above, not per-plant-prefixed - see
    # review_views.py's own module docstring.
    path("review/next", review_views.next_review, name="review-next"),
    # Accuracy panel + undo (2026-09-22). "review/stats" is declared BEFORE
    # the bare "review" route for readability only - they differ by path, not
    # by prefix, so order is not load-bearing here.
    path("review/stats", review_views.review_stats, name="review-stats"),
    path("review/stats/export", review_views.export_review_stats, name="review-stats-export"),
    path("review/<int:review_id>", review_views.undo_review, name="review-undo"),
    path("review", review_views.submit_review, name="review-submit"),
]
