"""
apps/core/admin.py — Django Admin registrations for the core models.

This is a developer/debugging surface, not the app's real user-facing admin
UX - end users manage PTUsers and inline PO/material corrections through the
in-app Admin Panel (frontend/admin.html + apps/api/routers/*) instead (see
CLAUDE.md's "In-app user management"/"Inline 'Edit Everywhere'" sections for
why: the project owner explicitly rejected linking out to Django Admin for
that). Most models below get the plain `admin.site.register(Model)` default
(no custom ModelAdmin) since nobody is expected to browse them here day to
day; only the models where the default read/write behavior would actively
work against the model's own design (append-only audit logs, the password
hash field) get a custom ModelAdmin below.
"""
from django.contrib import admin

from apps.core.audit_log import PTAuditLog
from apps.core.models import (
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOLineItem,
    HRSPOMirMatch,
    HRSPurchaseOrder,
    HRSStockLot,
    HRSStockSnapshot,
    ImportPOCorrection,
    RTPAchhadImportPOLineItem,
    RTPAchhadImportPurchaseOrder,
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadPOLineItem,
    RTPAchhadPOMirMatch,
    RTPAchhadPurchaseOrder,
    RTPAchhadStockLot,
    RTPAchhadStockSnapshot,
    RTPVapiImportPOLineItem,
    RTPVapiImportPurchaseOrder,
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiPOLineItem,
    RTPVapiPOMirMatch,
    RTPVapiPurchaseOrder,
    RTPVapiStockLot,
    RTPVapiStockSnapshot,
    SyncRun,
    PTUser,
    OTPCode,
    TrustedDevice,
)

# ── Per-plant domestic PO / MIR / Stock / match tables ─────────────────────
# Plain default ModelAdmin for all of these - read-only browsing/spot-checks
# only, no bespoke list_display/search needed for a debugging surface.
admin.site.register(HRSPurchaseOrder)
admin.site.register(HRSPOLineItem)
admin.site.register(HRSMIREntry)
admin.site.register(HRSStockLot)
admin.site.register(HRSStockSnapshot)
admin.site.register(HRSPOMirMatch)
admin.site.register(HRSMirStockMatch)
admin.site.register(RTPAchhadPurchaseOrder)
admin.site.register(RTPAchhadPOLineItem)
admin.site.register(RTPAchhadMIREntry)
admin.site.register(RTPAchhadStockLot)
admin.site.register(RTPAchhadStockSnapshot)
admin.site.register(RTPAchhadPOMirMatch)
admin.site.register(RTPAchhadMirStockMatch)
admin.site.register(RTPVapiPurchaseOrder)
admin.site.register(RTPVapiPOLineItem)
admin.site.register(RTPVapiMIREntry)
admin.site.register(RTPVapiStockLot)
admin.site.register(RTPVapiStockSnapshot)
admin.site.register(RTPVapiPOMirMatch)
admin.site.register(RTPVapiMirStockMatch)
admin.site.register(SyncRun)

# ── Per-plant import PO tables ──────────────────────────────────────────────
admin.site.register(HRSImportPurchaseOrder)
admin.site.register(HRSImportPOLineItem)
admin.site.register(RTPAchhadImportPurchaseOrder)
admin.site.register(RTPAchhadImportPOLineItem)
admin.site.register(RTPVapiImportPurchaseOrder)
admin.site.register(RTPVapiImportPOLineItem)


# ── Append-only correction trail: real ModelAdmin, all writes disabled ────
# (DomesticPOCorrection/MaterialCorrection/FlagDismissal have no ModelAdmin
# at all yet - only ImportPOCorrection got one; browse those via the ORM/
# shell if needed, same as any other model with the plain default above.)
@admin.register(ImportPOCorrection)
class ImportPOCorrectionAdmin(admin.ModelAdmin):
    """Read-only in the admin UI - like PTAuditLogAdmin, this is an
    append-only correction trail, not something to hand-edit here."""

    list_display = ("corrected_at", "plant", "po_number", "item_id", "field_name", "old_value", "new_value", "corrected_by_email")
    list_filter = ("plant", "field_name")
    search_fields = ("po_number", "item_id", "corrected_by_email")
    ordering = ("-corrected_at",)
    readonly_fields = [f.name for f in ImportPOCorrection._meta.get_fields()]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── Auth: PTUser / TrustedDevice / OTPCode ─────────────────────────────────
@admin.register(PTUser)
class PTUserAdmin(admin.ModelAdmin):
    list_display = ("user_id", "email", "full_name", "role", "designation", "is_active", "created_at", "last_login_at")
    list_filter = ("role", "is_active")
    search_fields = ("email", "full_name")
    ordering = ("user_id",)
    # Never expose the bcrypt hash in the admin UI - there is no way to set a
    # usable password through THIS form. New users are normally created via
    # the in-app Admin Panel (/admin.html -> "+ Add User", apps/api/routers/
    # users_views.py::create_user) instead; `manage.py create_pt_user` is
    # still the only way to create the very first account (before any admin
    # exists to use the panel) and the only way to reset an existing user's
    # password (neither the panel nor this Django Admin form can do that -
    # see users_views.py's header comment for why), same reasoning as
    # TDSUserAdmin in the TDS app.
    exclude = ("password_hash",)
    readonly_fields = ("created_at", "last_login_at")


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    # device_token_hash is a one-way SHA-256 digest, not the bearer
    # credential itself (fixed 2026-09-04 - see TrustedDevice's own
    # docstring) - safe to display read-only since it can't be reversed
    # back into a usable cookie value, unlike the plaintext token this used
    # to store (which effectively let anyone with Django Admin read access
    # here copy a live device-trust credential for any account). Use the
    # in-app Admin Panel's Edit User > Trusted Devices instead of this page
    # to actually revoke a device - see users_views.py's revoke_user_device().
    list_display = ("user", "device_name", "ip_address", "created_at", "last_used_at")
    search_fields = ("user__email", "device_name", "ip_address")
    readonly_fields = ("device_token_hash", "created_at", "last_used_at")


admin.site.register(OTPCode)


# ── Audit log: read-only, same reasoning as ImportPOCorrectionAdmin above ──
@admin.register(PTAuditLog)
class PTAuditLogAdmin(admin.ModelAdmin):
    """Read-only in the admin UI - the audit log is append-only by design
    (see apps/core/audit_log.py's log_pt_action), so editing/deleting rows
    here would defeat the point of keeping one."""

    list_display = ("timestamp", "action", "actor_email", "ip_address", "detail")
    list_filter = ("action",)
    search_fields = ("actor_email", "ip_address", "detail")
    ordering = ("-timestamp",)
    readonly_fields = [f.name for f in PTAuditLog._meta.get_fields()]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
