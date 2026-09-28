"""
apps/api/routers/preferences_views.py - Per-user sort presets (2026-09-28).

Endpoints
---------
GET    /api/sort-presets?view=<view>      the caller's own presets for a view
                                          (materials | purchase_orders | import_purchases)
POST   /api/sort-presets                  {view, name, levels} - create, or save over the
                                          caller's preset of the same name (201 / 200)
PATCH  /api/sort-presets/<id>             {name?, levels?} - rename / replace levels
DELETE /api/sort-presets/<id>             delete

Every role may use these, viewers included: a preset is the caller's own
display preference, not a write to business data, and it holds no plant
data (so there is no plant scope to apply). Every query filters on
`user=request.user`; another user's preset id answers 404, the same as one
that does not exist. Validation lives in apps/services/sort_presets.py.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import SortPreset
from apps.services import sort_presets


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def presets(request):
    if request.method == "GET":
        return Response({"presets": sort_presets.list_presets(request.user, request.query_params.get("view"))})
    body = request.data if isinstance(request.data, dict) else {}
    preset, created = sort_presets.save_preset(request.user, body.get("view"), body.get("name"), body.get("levels"))
    return Response(preset, status=201 if created else 200)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def preset(request, preset_id):
    found = SortPreset.objects.filter(id=preset_id, user=request.user).first()
    if found is None:
        return Response({"detail": "Preset not found."}, status=404)
    if request.method == "DELETE":
        found.delete()
        return Response(status=204)
    body = request.data if isinstance(request.data, dict) else {}
    return Response(sort_presets.update_preset(found, name=body.get("name"), levels=body.get("levels")))
