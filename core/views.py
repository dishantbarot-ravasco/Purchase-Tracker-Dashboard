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
import io
import json
import logging
import os

from django.conf import settings
from django.core.management import call_command
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from .decorators import require_admin, require_login, require_plant_access
from .models import AuditLog, PlantFileStatus, POFlag, Plant, PurchaseOrder, StockSnapshot, UserAccess
from .serializers import (
    serialize_dashboard_plant_card,
    serialize_purchase_order,
    serialize_stock_snapshot,
    serialize_user_access,
)

logger = logging.getLogger(__name__)


@require_GET
@require_login
def dashboard(request):
    """One summary card per plant the signed-in user can see. Everything
    here reads from Postgres only - no live Drive calls in this request.

    MIR/Stock row counts used to be read live off Drive on every page load,
    which meant any Drive slowness (or a cold-started Render instance) could
    time out the whole dashboard request. They now come from PlantFileStatus,
    a small cache refreshed in the background by the
    refresh_plant_file_status management command (run on a schedule, e.g.
    every 15-30 min via Render Cron Job) - so this view is always fast and
    never depends on Drive's response time, only the background job does."""
    plants_out = [_build_plant_card(plant) for plant in request.user_access.allowed_plants()]
    return JsonResponse({"plants": plants_out})


def _build_plant_card(plant):
    po_agg = PurchaseOrder.objects.filter(plant=plant, doc_type="domestic").aggregate(
        count=Count("id"), total=Sum("total_incl_tax")
    )

    status = PlantFileStatus.objects.filter(plant=plant).first()
    mir_row_count = status.mir_row_count if status else 0
    stock_row_count = status.stock_row_count if status else 0
    errors = [status.last_error] if (status and status.last_error) else []
    if not status:
        errors.append("MIR/Stock data hasn't been checked yet - the background refresh job hasn't run.")

    return serialize_dashboard_plant_card(
        plant=plant,
        po_count=po_agg["count"] or 0,
        total_value=float(po_agg["total"] or 0),
        mir_row_count=mir_row_count,
        stock_row_count=stock_row_count,
        errors=errors,
    )


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


@require_GET
@require_plant_access(lambda request, plant: plant)
def plant_materials(request, plant):
    """Latest RM Stock snapshot per material for a plant - one row per
    material_code, using whichever snapshot_date is most recent for it.
    Backs the Raw Material Analysis view. Deliberately does NOT include
    "open PO lines" or "MIR vs Stock mismatch" figures - those need the
    PO-vs-MIR reconciliation engine (a separate, not-yet-built piece), and
    showing fabricated zeros for them would be worse than leaving them out."""
    # Deduped in Python rather than Postgres' DISTINCT ON: that's a
    # Postgres-only feature and this code should behave the same in the
    # local sqlite fallback (see settings.py) as it does in production.
    latest_by_material = {}
    for snap in StockSnapshot.objects.filter(plant=plant).order_by("material_code", "-snapshot_date"):
        latest_by_material.setdefault(snap.material_code, snap)
    ordered = sorted(latest_by_material.values(), key=lambda s: (s.value or 0), reverse=True)
    return JsonResponse({"plant": plant, "materials": [serialize_stock_snapshot(s) for s in ordered]})


@require_GET
@require_plant_access(lambda request, plant: plant)
def plant_licenses(request, plant):
    """Advance Authorisation licenses for a plant. Works the same for any
    plant - RTP-Vapi is just the only one with import data extracted so far
    this financial year (HRS/RTP-Achhad's Import folders are empty, not
    architecturally excluded). Empty until the extraction pipeline actually
    ingests license letters (see ingest_extraction_results' _ingest_licenses)
    - an empty list here means "nothing extracted yet", not a bug."""
    from .models import AdvanceLicense
    from .serializers import serialize_advance_license

    qs = AdvanceLicense.objects.filter(plant=plant).prefetch_related("items", "po_usages__purchase_order")
    return JsonResponse({"plant": plant, "licenses": [serialize_advance_license(lic) for lic in qs]})


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
    something usable was found and where it came from."""
    env_json_set = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    key_path = settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    path_candidates = [key_path, os.path.join(str(settings.BASE_DIR), os.path.basename(key_path))]
    found_path = next((p for p in path_candidates if os.path.isfile(p)), None)

    try:
        from . import drive
        drive.get_drive_service()
        credentials_load_ok = True
        credentials_error = None
    except Exception as e:
        credentials_load_ok = False
        credentials_error = str(e)

    return JsonResponse(
        {
            "credentialsLoadedSuccessfully": credentials_load_ok,
            "credentialsError": credentials_error,
            "loadedFrom": "GOOGLE_SERVICE_ACCOUNT_JSON env var" if env_json_set else (found_path or "not found"),
            "drivePlantRootsConfigured": {
                plant: bool(folder_id) for plant, folder_id in settings.DRIVE_PLANT_ROOTS.items()
            },
            "extractionQueueFolderConfigured": bool(settings.DRIVE_EXTRACTION_QUEUE_FOLDER),
        }
    )


# Whitelisted management commands that are safe to trigger over HTTP - never
# accept an arbitrary command name from the request, only look up against
# this fixed dict. Each maps a short public name to the real command.
RUNNABLE_TASKS = {
    "backfill_from_master_csv": "backfill_from_master_csv",
    "scan_new_pos": "scan_new_pos",
    "ingest_extraction_results": "ingest_extraction_results",
    "snapshot_rm_stock": "snapshot_rm_stock",
    "refresh_plant_file_status": "refresh_plant_file_status",
    "reconcile_purchase_orders": "reconcile_purchase_orders",
    # Temporary diagnostic for the master-CSV title mismatch - see
    # list_master_csv_folder.py's docstring. Safe to remove once fixed.
    "list_master_csv_folder": "list_master_csv_folder",
}


@csrf_exempt
@require_http_methods(["POST"])
def run_task(request, task_name):
    """Runs one of the whitelisted management commands over HTTP - the fix
    for not having Render's paid Shell or one-off Jobs features. Two ways
    in, since this needs to work both for a human clicking a button in the
    Admin tab AND for a free external scheduler (e.g. cron-job.org) hitting
    this on a timer with no browser session at all:

      1. A signed-in admin session (same cookie-based auth as everything
         else in core/decorators.py).
      2. A shared secret token, passed as ?token=... or an
         X-Task-Token header, checked against the RUN_TASKS_TOKEN env var.
         This is what an external scheduler uses instead of logging in.

    Output (stdout/stderr from the command) is returned in the response so
    you can see what happened without needing logs or shell access either.
    RUN_TASKS_TOKEN must be set to a long random value for path 2 to work
    at all - if it's not set, only the admin-session path is available."""
    if task_name not in RUNNABLE_TASKS:
        return JsonResponse({"error": f"Unknown task '{task_name}'."}, status=404)

    token = request.GET.get("token") or request.headers.get("X-Task-Token", "")
    token_ok = bool(settings.RUN_TASKS_TOKEN) and token == settings.RUN_TASKS_TOKEN

    if not token_ok:
        email = request.session.get("email")
        if not email:
            return JsonResponse({"error": "Not signed in, and no valid task token was provided."}, status=401)
        from .models import UserAccess
        access = UserAccess.objects.filter(email=email, is_active=True).first()
        if not access or access.role != "admin":
            return JsonResponse({"error": "Admin access required."}, status=403)

    out, err = io.StringIO(), io.StringIO()
    try:
        call_command(RUNNABLE_TASKS[task_name], stdout=out, stderr=err)
        success = True
    except Exception as e:
        logger.exception("Task %s failed", task_name)
        err.write(f"\n(unhandled exception: {e})")
        success = False

    AuditLog.objects.create(
        email=(request.session.get("email") or "external-scheduler"),
        action="run_task",
        detail={"task": task_name, "success": success},
    )
    return JsonResponse({"task": task_name, "success": success, "stdout": out.getvalue(), "stderr": err.getvalue()})


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
