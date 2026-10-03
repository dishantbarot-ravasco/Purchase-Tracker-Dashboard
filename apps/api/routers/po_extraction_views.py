"""
/api/po-extractions/... - reviewing what the extraction model read from an
uploaded PO file, and approving it into the procurement tables (owner,
2026-10-03). The rules live in apps/services/po_extraction.py; this file only
gates, parses and serializes.

Access: Perm.PO_UPLOAD at the file's own plant, for everything - listing,
reading again, editing the draft, approving and rejecting. Another plant's
reading is a 404.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import Perm, requires, user_can_access_plant
from apps.core.models import Document, Plant, PoExtraction
from apps.services import po_extraction


def _not_found():
    return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)


def _bad(exc):
    return Response({"error": str(exc), "problems": getattr(exc, "problems", [])}, status=http.HTTP_400_BAD_REQUEST)


def _row(ext):
    doc = ext.document
    return {
        "id": ext.id, "status": ext.status, "statusLabel": ext.get_status_display(), "plant": ext.plant.code,
        "documentId": doc.id, "poNumber": doc.po_number, "fileName": doc.original_filename, "revision": doc.revision,
        "requestedBy": ext.requested_by_email, "createdAt": ext.created_at.isoformat(),
        "finishedAt": ext.finished_at.isoformat() if ext.finished_at else None, "error": ext.error,
        "reviewedBy": ext.reviewed_by_email, "reviewedAt": ext.reviewed_at.isoformat() if ext.reviewed_at else None,
        "reviewNote": ext.review_note, "purchaseOrderId": ext.purchase_order_id,
    }


def _get(request, extraction_id):
    ext = get_object_or_404(PoExtraction.objects.select_related("plant", "document"), pk=extraction_id)
    return ext if user_can_access_plant(request.user, ext.plant.code) else None


@api_view(["GET"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def extractions(request):
    """Readings at the caller's plants, newest first. ?plant= ?status=."""
    plants = [p.code for p in Plant.objects.all() if user_can_access_plant(request.user, p.code)]
    wanted = request.query_params.get("plant")
    if wanted:
        plants = [p for p in plants if p == wanted]
    qs = PoExtraction.objects.filter(plant__code__in=plants).select_related("plant", "document")
    status = request.query_params.get("status")
    if status in PoExtraction.Status.values:
        qs = qs.filter(status=status)
    return Response({"extractions": [_row(e) for e in qs[:300]], "configured": po_extraction.is_configured()})


@api_view(["GET"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def extraction(request, extraction_id):
    """One reading with its draft, what blocks approval (`problems`), what
    does not add up (`checks`) and, when the PO sheet already holds the
    order, how they differ (`sheet`)."""
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    return Response(_detail(ext))


def _detail(ext):
    draft = ext.draft or {}
    return {
        **_row(ext), "draft": draft, "labels": po_extraction.LABELS,
        "headerFields": po_extraction.HEADER_FIELDS, "lineFields": po_extraction.LINE_FIELDS,
        "problems": po_extraction.problems(draft, ext.plant, ext.document.po_number) if ext.draft else [],
        "checks": po_extraction.checks(draft) if ext.draft else [],
        "sheet": po_extraction.sheet_differences(draft, ext.plant) if ext.draft else None,
        "tokens": {"input": ext.input_tokens, "output": ext.output_tokens}, "model": ext.model_name,
    }


@api_view(["POST"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def read_document(request, document_id):
    """Read an uploaded PO file (again). A new reading; earlier ones stay."""
    doc = get_object_or_404(Document.objects.select_related("plant"), pk=document_id)
    if not user_can_access_plant(request.user, doc.plant.code):
        return _not_found()
    try:
        ext = po_extraction.request(doc, request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_row(ext), status=http.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def save_draft(request, extraction_id):
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    try:
        ext = po_extraction.save_draft(ext, (request.data or {}).get("draft"), request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_detail(PoExtraction.objects.select_related("plant", "document").get(pk=ext.pk)))


@api_view(["POST"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def approve(request, extraction_id):
    """Body {"draft"}: the reviewed draft is written as the plant's PO."""
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    try:
        ext = po_extraction.approve(ext, (request.data or {}).get("draft"), request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_row(ext))


@api_view(["POST"])
@permission_classes([requires(Perm.PO_UPLOAD)])
def reject(request, extraction_id):
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    try:
        ext = po_extraction.reject(ext, (request.data or {}).get("note"), request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_row(ext))
