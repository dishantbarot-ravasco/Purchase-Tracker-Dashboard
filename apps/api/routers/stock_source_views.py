"""
/api/stock-source and /api/app-stock/<plant>/... - which records a plant's
Inventory / On Order / Stock & Orders tabs read (owner, 2026-10-03), and the
app-record payloads for a plant switched to "app". The rules and the payload
shapes live in apps/services/app_stock_source.py.

Access:
  - Reading the sources: any account with access (the tabs need it), for
    its own plants only.
  - Switching a plant: admins only.
  - The app payloads: the same permissions as the Drive /materials
    (STOCK_VIEWS) and /purchase-orders (ORDER_VIEWS), at a plant the account
    holds (403 otherwise, like the domestic routers).
"""

from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import ORDER_VIEWS, STOCK_VIEWS, HasAnyAccess, IsAdmin, is_admin, requires, user_can_access_plant
from apps.core.models import Plant
from apps.services import app_stock_source


def _forbidden():
    return Response({"error": "You do not have access to that plant."}, status=http.HTTP_403_FORBIDDEN)


@api_view(["GET"])
@permission_classes([HasAnyAccess])
def stock_source(request):
    """{"sources": {plant: "drive" | "app"}, "canChange"} for the caller's plants."""
    def mine(what):
        return {code: src for code, src in app_stock_source.sources(what).items() if user_can_access_plant(request.user, code)}

    return Response({"sources": mine("stock"), "importSources": mine("imports"), "canChange": is_admin(request.user)})


@api_view(["POST"])
@permission_classes([IsAdmin])
def set_stock_source(request):
    """Body {"plant", "source": "drive" | "app", "what": "stock" (default) |
    "imports"}. Nothing is copied or deleted, so switching back is instant."""
    data = request.data or {}
    plant = get_object_or_404(Plant, code=data.get("plant"))
    if not user_can_access_plant(request.user, plant.code):
        return _forbidden()
    try:
        what = data.get("what") or "stock"
        row = app_stock_source.set_source(plant, data.get("source"), request.user, what)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
    return Response({"plant": plant.code, "what": what, "source": getattr(row, app_stock_source.WHAT[what]),
                     "updatedBy": row.updated_by_email})


@api_view(["GET"])
@permission_classes([requires(*STOCK_VIEWS)])
def app_materials(request, plant):
    """The plant's RM store receipts in the Drive /materials row shape."""
    if not user_can_access_plant(request.user, plant):
        return _forbidden()
    return Response({"materials": app_stock_source.materials_payload(get_object_or_404(Plant, code=plant))})


@api_view(["GET"])
@permission_classes([requires(*ORDER_VIEWS)])
def app_purchase_orders(request, plant):
    """The plant's POs still on order, by posted MIRs, in the Drive
    /purchase-orders row shape."""
    if not user_can_access_plant(request.user, plant):
        return _forbidden()
    return Response({"purchaseOrders": app_stock_source.purchase_orders_payload(get_object_or_404(Plant, code=plant))})
