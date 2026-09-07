"""
apps/api/routers/admin_overview_views.py — Admin Panel "Overview" tab data:
top correctors, top vendors, and recent correction activity across all 3
plants and both PO types.

Added 2026-09-07 (project owner: redesign this app's Admin Panel to match
the TDS Automation App's own Overview/Users layout - KPI cards, "Top
Creators"/"Top Customers" bar lists, "Recent Activity" table). TDS's own
concepts don't map onto this app's data as-is (a TDS document is CREATED by
a user; a Purchase Tracker PO is synced from a CSV, nobody "creates" one) -
adapted instead of copied literally:
  TDS "Top Creators" (TDS docs per user)  -> "Top Correctors" (inline field
    corrections per user, across DomesticPOCorrection/ImportPOCorrection/
    MaterialCorrection - the closest real analogue to "who's actively doing
    work in this system", since nobody authors a PO here).
  TDS "Top Customers" (TDS docs per customer) -> "Top Vendors" (PO count per
    vendor, across all 3 plants' domestic PO tables).
  TDS "Recent Activity" (last 10 TDS docs) -> "Recent Activity" (last 10
    corrections across all three correction tables combined).
  TDS's KPI row (Total TDS/Active Users/Customers/This Month) has a much
  closer analogue already built: home.html's own loadKpis() already computes
  Total PO's/Suppliers/This Month/This Week client-side from data every
  plant's /purchase-orders endpoint returns - reused as-is by admin-page.js
  rather than duplicated server-side here. This endpoint only covers the two
  things with no existing client-side source: the correction-audit-table
  aggregates (nothing caches those anywhere) and vendor-by-PO-count (cheap
  to aggregate here in the DB, expensive to reconstruct from an already-
  fetched PO list that wasn't shaped for this).
"""

from collections import Counter

from django.db.models import Count
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin
from apps.core.models import (
    DomesticPOCorrection,
    HRSDomesticPurchaseOrder,
    ImportPOCorrection,
    MaterialCorrection,
    PTUser,
    RTPAchhadDomesticPurchaseOrder,
    RTPVapiDomesticPurchaseOrder,
    SyncRun,
)

_PLANT_LABELS = {
    SyncRun.Plant.HRS: "HRS, Silvassa",
    SyncRun.Plant.RTP_ACHHAD: "RTP-Achhad",
    SyncRun.Plant.RTP_VAPI: "RTP-Vapi",
}
_DOMESTIC_PO_MODELS = [HRSDomesticPurchaseOrder, RTPAchhadDomesticPurchaseOrder, RTPVapiDomesticPurchaseOrder]
_CORRECTION_MODELS = [
    (DomesticPOCorrection, "domestic"),
    (ImportPOCorrection, "import"),
    (MaterialCorrection, "material"),
]

_TOP_N = 5
_RECENT_N = 10


def _target_label(correction_type, c):
    if correction_type == "material":
        return f"Stock Lot #{c.lot_id}"
    target = c.po_number
    if c.item_id:
        target += f" (item {c.item_id})"
    return target


def _correction_dict(correction_type, c):
    return {
        "type": correction_type,
        "plant": c.plant,
        "plantLabel": _PLANT_LABELS.get(c.plant, c.plant),
        "target": _target_label(correction_type, c),
        "fieldName": c.field_name,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "correctedByEmail": c.corrected_by_email,
        "correctedByName": (c.corrected_by.full_name if c.corrected_by_id and c.corrected_by.full_name else c.corrected_by_email) or "Unknown",
        "correctedAt": c.corrected_at.isoformat(),
    }


def correction_counts_by_email() -> Counter:
    """Total corrections per corrected_by_email, across all three
    correction tables - exposed (not module-private) so users_views.py's
    list_users() can attach each user's own count as `correctionsCount`
    (the closest real analogue this app has to TDS's per-user "TDS Made"
    stat on its own Users cards) without re-deriving this aggregation a
    second time."""
    counter = Counter()
    for model, _label in _CORRECTION_MODELS:
        rows = model.objects.exclude(corrected_by_email="").values("corrected_by_email").annotate(count=Count("id"))
        for row in rows:
            counter[row["corrected_by_email"]] += row["count"]
    return counter


def _top_correctors():
    counter = correction_counts_by_email()
    top = counter.most_common(_TOP_N)
    emails = [email for email, _ in top]
    full_names = dict(PTUser.objects.filter(email__in=emails).values_list("email", "full_name"))
    return [{"email": email, "fullName": full_names.get(email) or email, "count": count} for email, count in top]


def _top_vendors():
    counter = Counter()
    for model in _DOMESTIC_PO_MODELS:
        rows = model.objects.exclude(vendor_name="").values("vendor_name").annotate(count=Count("id"))
        for row in rows:
            counter[row["vendor_name"].strip()] += row["count"]
    return [{"vendor": vendor, "count": count} for vendor, count in counter.most_common(_TOP_N)]


def _recent_activity():
    combined = []
    for model, label in _CORRECTION_MODELS:
        combined.extend((label, c) for c in model.objects.select_related("corrected_by").order_by("-corrected_at")[:_RECENT_N])
    combined.sort(key=lambda pair: pair[1].corrected_at, reverse=True)
    return [_correction_dict(label, c) for label, c in combined[:_RECENT_N]]


@api_view(["GET"])
@permission_classes([IsAdmin])
def admin_overview(request):
    """GET /api/auth/admin-overview - see module docstring for what each
    field replaces from the TDS Admin Panel this was modeled on."""
    return Response({
        "topCorrectors": _top_correctors(),
        "topVendors": _top_vendors(),
        "recentActivity": _recent_activity(),
    })
