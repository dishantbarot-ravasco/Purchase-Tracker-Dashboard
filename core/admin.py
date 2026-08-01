from django.contrib import admin

from .models import (
    AdvanceLicense,
    AuditLog,
    ExtractionQueue,
    LicenseItem,
    LicensePOUsage,
    POFlag,
    POItem,
    PurchaseOrder,
    StockSnapshot,
    UserAccess,
)

# Admin is the power-user fallback (Dishant/admins only, per the access
# decorator) - NOT the interface plant staff use for corrections. That's the
# inline pencil-icon edit flow in the actual dashboard frontend.


class POItemInline(admin.TabularInline):
    model = POItem
    extra = 0


class POFlagInline(admin.TabularInline):
    model = POFlag
    extra = 0


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "plant", "doc_type", "vendor_name", "total_incl_tax", "status", "created_date")
    list_filter = ("plant", "doc_type", "status", "extraction_confidence")
    search_fields = ("po_number", "vendor_name", "vendor_gstin")
    inlines = [POItemInline, POFlagInline]


class LicenseItemInline(admin.TabularInline):
    model = LicenseItem
    extra = 0


class LicensePOUsageInline(admin.TabularInline):
    model = LicensePOUsage
    extra = 0


@admin.register(AdvanceLicense)
class AdvanceLicenseAdmin(admin.ModelAdmin):
    list_display = ("license_number", "plant", "issue_date", "export_obligation_end", "eodc_status")
    list_filter = ("plant", "eodc_status")
    search_fields = ("license_number",)
    inlines = [LicenseItemInline, LicensePOUsageInline]


@admin.register(StockSnapshot)
class StockSnapshotAdmin(admin.ModelAdmin):
    list_display = ("plant", "material_code", "description", "qty", "rate", "value", "snapshot_date")
    list_filter = ("plant", "snapshot_date")
    search_fields = ("material_code", "description")


@admin.register(ExtractionQueue)
class ExtractionQueueAdmin(admin.ModelAdmin):
    list_display = ("po_number_hint", "plant", "doc_type", "status", "requested_at", "processed_at")
    list_filter = ("plant", "doc_type", "status")


@admin.register(UserAccess)
class UserAccessAdmin(admin.ModelAdmin):
    list_display = ("email", "role", "plants", "is_active", "created_at")
    list_filter = ("role", "is_active")
    search_fields = ("email",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("email", "action", "created_at")
    list_filter = ("action",)
    search_fields = ("email",)
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
