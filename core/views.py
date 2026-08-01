"""
API views. Each view here does exactly two things: enforce access (via the
decorators in core/decorators.py) and fetch/mutate data - the actual JSON
shape is built by core/serializers.py, not inline here, so the two concerns
don't get tangled as more fields get added later.

Every view that touches plant-specific data uses @require_plant_access,
which re-checks the signed-in user's allowed_plants() server-side on every
single request - the frontend never being trusted as the security boundary
is the whole point (see core/decorators.py's module docstring).
"""
import json
import logging
import os

from django.conf import settings
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from . import mir_stock
from .decorators import require_admin, require_login, require_plant_access
from .models import AuditLog, POFlag, Plant, PurchaseOrder, StockSnapshot, UserAccess
from .serializers import (
    serialize_dashboard_plant_card,
    serialize_purchase_order,
    serialize_stock_snapshot,
    serialize_user_access,
)


@require_GET
@require_login
def dashboard(request):
    """One summary card per plant the signed-in user can see. PO counts/
    totals come from Postgres (fast); MIR/Stock row counts are read live off
    Drive (slower, and can legitimately fail if a file's momentarily
    unreachable) - failures there are surfaced per-plant in `errors` rather
    than taking down the whole dashboard response."""
    plants_out = [_build_plant_card(plant) for plant in request.user_access.allowed_plants()]
    return JsonResponse({"plants": plants_out})


def _build_plant_card(plant):
    errors = []
    po_agg = PurchaseOrder.objects.filter(plant=plant, doc_type="domestic").aggregate(
        count=Count("id"), total=Sum("total_incl_tax")
    )

    mir_row_count = _safe_call(mir_stock.get_mir_rm_row_count, plant, errors, "MIR file")
    stock_row_count = _safe_call(mir_stock.get_stock_row_count, plant, errors, "RM Stock file")

    return serialize_dashboard_plant_card(
        plant=plant,
        po_count=po_agg["count"] or 0,
        total_value=float(po_agg["total"] or 0),
        mir_row_count=mir_row_count,
        stock_row_count=stock_row_count,
        errors=errors,
    )


logger = logging.getLogger(__name__)


def _safe_call(fn, plant, errors_out, label):
    """Runs a Drive-dependent read and turns any failure into a short,
    non-technical message instead of raising - a bad/missing file for one
    plant shouldn't 500 the whole dashboard for every plant.

    The full exception (stack trace, file paths, etc.) is logged
    server-side only, via logger.exception() - visible in Render's Logs tab
    for whoever's debugging it, but a manager looking at the dashboard
    should never see a Python error message. If this starts showing up
    for every plant, check the Render logs for the real cause (common one:
    the GOOGLE_SERVICE_ACCOUNT_JSON_PATH secret file wasn't uploaded, or
    its path doesn't match the env var)."""
    try:
        return fn(plant)
    except Exception:
        logger.exception("Failed to read %s for plant %s", label, plant)
        errors_out.append(f"{label} isn't available right now.")
        return 0


@require_GET
@require_plant_access(lambda request, plant: plant)
def plant_purchase_orders(request, plant):
    """List a plant's purchase orders with their items/flags. Capped at 200
    rows for now - fine at current volume, proper offset/cursor pagination
    is the next step once a plant's PO count grows past that."""
    qs = PurchaseOrder.objects.filter(plant=plant).prefetch_related("items", "flags")[:200]
    data = [serialize_purchase_order(po) for po in qs]
    return JsonResponse({"plant": plant, "purchaseOrders": data})


@require_GET
@require_plant_access(lambda request, plant: plant)
def plant_stock_trend(request, plant):
    """Daily RM Stock history for a plant, optionally filtered to one
    material. Powers the consumption trend charts - this is the whole
    reason StockSnapshot exists (the raw Stock file itself has no date
    column, see core/mir_stock.py's module docstring)."""
    material_code = request.GET.get("material_code")
    qs = StockSnapshot.objects.filter(plant=plant)
    if material_code:
        qs = qs.filter(material_code=material_code)
    qs = qs.order_by("snapshot_date")[:730]  # ~2 years of daily points, plenty for weekly/monthly rollups

    data = [serialize_stock_snapshot(s) for s in qs]
    return JsonResponse({"plant": plant, "snapshots": data})


@csrf_protect
@require_http_methods(["POST"])
@require_login
def submit_flag(request, po_number):
    """The inline pencil-icon correction flow lands here: anyone with access
    to a PO's plant can flag it, no raw-column dropdown, just free text tied
    to whichever field they clicked (the frontend supplies the context)."""
    try:
        po = PurchaseOrder.objects.get(po_number=po_number)
    except PurchaseOrder.DoesNotExist:
        return JsonResponse({"error": "PO not found."}, status=404)
    if po.plant not in request.user_access.allowed_plants():
        return JsonResponse({"error": "You don't have access to this plant."}, status=403)

    body = json.loads(request.body or "{}")
    flag_text = (body.get("flagText") or "").strip()
    if not flag_text:
        return JsonResponse({"error": "flagText is required."}, status=400)

    POFlag.objects.create(
        purchase_order=po,
        flag_text=flag_text,
        source="manual_correction",
        submitted_by_email=request.user_access.email,
    )
    AuditLog.objects.create(
        email=request.user_access.email,
        action="submit_correction",
        detail={"po_number": po_number, "flag_text": flag_text},
    )
    return JsonResponse({"ok": True})


@csrf_protect
@require_http_methods(["GET", "POST"])
@require_admin
def admin_users(request):
    """Admin-only user/role management. GET lists everyone, POST creates or
    updates one person's role + allowed plants (upsert by email)."""
    if request.method == "GET":
        return JsonResponse(
            {
                "users": [serialize_user_access(u) for u in UserAccess.objects.all().order_by("email")],
                "validRoles": ["admin", "editor", "viewer"],
                "plants": [c[0] for c in Plant.choices],
            }
        )
    return _upsert_user_access(request)


def _upsert_user_access(request):
    body = json.loads(request.body or "{}")
    target_email = (body.get("email") or "").strip().lower()
    role = body.get("role")
    plants = body.get("plants") or []
    if not target_email or role not in ("admin", "editor", "viewer"):
        return JsonResponse({"error": "email and a valid role are required."}, status=400)

    UserAccess.objects.update_or_create(
        email=target_email, defaults={"role": role, "plants": plants, "is_active": True}
    )
    AuditLog.objects.create(
        email=request.user_access.email,
        action="upsert_user_access",
        detail={"target_email": target_email, "role": role, "plants": plants},
    )
    return JsonResponse({"ok": True})


@require_GET
@require_admin
def admin_diagnostics(request):
    """Admin-only, no shell access needed: lets you check from the browser
    whether the service account key and the Drive folder IDs are actually
    configured on this Render instance, without needing the paid Shell tab
    or digging through logs. Never returns the key's contents, only whether
    the file exists and how big it is."""
    key_path = settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    key_exists = os.path.isfile(key_path)
    return JsonResponse(
        {
            "serviceAccountKeyPath": key_path,
            "serviceAccountKeyFound": key_exists,
            "serviceAccountKeySizeBytes": os.path.getsize(key_path) if key_exists else None,
            "drivePlantRootsConfigured": {
                plant: bool(folder_id) for plant, folder_id in settings.DRIVE_PLANT_ROOTS.items()
            },
            "extractionQueueFolderConfigured": bool(settings.DRIVE_EXTRACTION_QUEUE_FOLDER),
        }
    )


@require_http_methods(["DELETE"])
@require_admin
def admin_user_detail(request, email):
    """Admin-only: revoke someone's access entirely (deletes their
    UserAccess row - they can still sign in with Google, they'll just see
    the 'no access configured' message until re-added)."""
    UserAccess.objects.filter(email=email.lower()).delete()
    AuditLog.objects.create(
        email=request.user_access.email, action="remove_user_access", detail={"target_email": email}
    )
    return JsonResponse({"ok": True})
