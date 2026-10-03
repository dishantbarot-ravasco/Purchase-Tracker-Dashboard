"""
/api/po-extractions/... - reviewing what the extraction model read from an
uploaded PO file, and approving it into the procurement tables (owner,
2026-10-03). The rules live in apps/services/po_extraction.py; this file only
gates, parses and serializes.

Access, at the file's own plant, by what was read: a PO copy needs
Perm.PO_UPLOAD, a Bill of Entry Perm.IMPORT_DOCS - the same permissions that
upload them. Another plant's reading, or one of a kind the account does not
handle, is a 404.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.api.permissions import Perm, has_perm, requires, user_can_access_plant
from apps.core.models import Document, Plant, PoExtraction
from apps.services import po_extraction


def _not_found():
    return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)


def _bad(exc):
    return Response({"error": str(exc), "problems": getattr(exc, "problems", [])}, status=http.HTTP_400_BAD_REQUEST)


def _row(ext):
    doc = ext.document
    return {
        "id": ext.id, "kind": ext.kind, "status": ext.status, "statusLabel": ext.get_status_display(), "plant": ext.plant.code,
        "documentId": doc.id, "poNumber": doc.po_number, "reference": doc.reference, "shipmentId": ext.shipment_id, "fileName": doc.original_filename, "revision": doc.revision,
        "requestedBy": ext.requested_by_email, "createdAt": ext.created_at.isoformat(),
        "finishedAt": ext.finished_at.isoformat() if ext.finished_at else None, "error": ext.error,
        "reviewedBy": ext.reviewed_by_email, "reviewedAt": ext.reviewed_at.isoformat() if ext.reviewed_at else None,
        "reviewNote": ext.review_note, "purchaseOrderId": ext.purchase_order_id,
    }


READERS = (Perm.PO_UPLOAD, Perm.IMPORT_DOCS)
KIND_PERMISSION = {PoExtraction.Kind.PO: Perm.PO_UPLOAD, PoExtraction.Kind.BOE: Perm.IMPORT_DOCS}


def _kinds(user):
    return [k for k, perm in KIND_PERMISSION.items() if has_perm(user, perm)]


def _get(request, extraction_id):
    ext = get_object_or_404(PoExtraction.objects.select_related("plant", "document"), pk=extraction_id)
    ok = user_can_access_plant(request.user, ext.plant.code) and ext.kind in _kinds(request.user)
    return ext if ok else None


@api_view(["GET"])
@permission_classes([requires(*READERS)])
def extractions(request):
    """Readings at the caller's plants, newest first. ?plant= ?status=."""
    plants = [p.code for p in Plant.objects.all() if user_can_access_plant(request.user, p.code)]
    wanted = request.query_params.get("plant")
    if wanted:
        plants = [p for p in plants if p == wanted]
    qs = PoExtraction.objects.filter(plant__code__in=plants, kind__in=_kinds(request.user)).select_related("plant", "document")
    status = request.query_params.get("status")
    if status in PoExtraction.Status.values:
        qs = qs.filter(status=status)
    return Response({"extractions": [_row(e) for e in qs[:300]], "configured": po_extraction.is_configured()})


@api_view(["GET"])
@permission_classes([requires(*READERS)])
def extraction(request, extraction_id):
    """One reading with its draft, what blocks approval (`problems`), what
    does not add up (`checks`) and, when the PO sheet already holds the
    order, how they differ (`sheet`)."""
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    return Response(_detail(ext))


def _detail(ext):
    # The fields, what is compulsory (by order type for a PO), the PO-line
    # choices for a BOE item, and the problems / checks / sheet comparison.
    return {
        **_row(ext), "draft": ext.draft or {}, **po_extraction.review(ext),
        "tokens": {"input": ext.input_tokens, "output": ext.output_tokens}, "model": ext.model_name,
    }


@api_view(["POST"])
@permission_classes([requires(*READERS)])
def read_document(request, document_id):
    """Read an uploaded PO file (again). A new reading; earlier ones stay."""
    doc = get_object_or_404(Document.objects.select_related("plant"), pk=document_id)
    kind = PoExtraction.Kind.BOE if doc.kind == Document.Kind.BOE else PoExtraction.Kind.PO
    if not user_can_access_plant(request.user, doc.plant.code) or kind not in _kinds(request.user):
        return _not_found()
    try:
        ext = po_extraction.request(doc, request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_row(ext), status=http.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([requires(*READERS)])
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
@permission_classes([requires(*READERS)])
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
@permission_classes([requires(*READERS)])
def reject(request, extraction_id):
    ext = _get(request, extraction_id)
    if ext is None:
        return _not_found()
    try:
        ext = po_extraction.reject(ext, (request.data or {}).get("note"), request.user)
    except po_extraction.ExtractionError as exc:
        return _bad(exc)
    return Response(_row(ext))
