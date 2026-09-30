"""
/api/documents/... and /api/mir/entries/<id>/invoice - uploaded PO and
invoice files (2026-09-30). The rules live in apps/services/documents.py;
this file only gates, parses and serializes.

Access:
  - Listing PO files and opening any file: any role, for the plants the
    account may read (user_can_access_plant()).
  - Uploading or withdrawing a PO file: Editor or Admin who may edit that
    plant. Attaching an invoice: Editor or Admin who may edit the MIR's
    receiving plant.

A file is opened through /api/documents/<id>/open, which redirects to a
presigned R2 link that lasts five minutes; the bucket itself is never
public.
"""

from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from apps.api.permissions import IsEditor, user_can_access_plant, user_can_edit_plant
from apps.core.models import Document, Mir, Plant, PurchaseOrder
from apps.services import documents, object_storage


def serialize(doc):
    return {
        "id": doc.id, "kind": doc.kind, "plant": doc.plant.code, "poNumber": doc.po_number, "mirId": doc.mir_id,
        "revision": doc.revision, "status": doc.status, "fileName": doc.original_filename,
        "contentType": doc.content_type, "sizeBytes": doc.size_bytes, "note": doc.note,
        "uploadedBy": doc.uploaded_by_email, "uploadedAt": doc.uploaded_at.isoformat(),
        "withdrawnBy": doc.withdrawn_by_email, "withdrawnAt": doc.withdrawn_at.isoformat() if doc.withdrawn_at else None,
        "withdrawReason": doc.withdraw_reason,
    }


def _refused(exc):
    return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)


def _not_configured(exc):
    return Response({"error": "File storage is not set up yet. Ask an admin to finish the Cloudflare setup.",
                     "detail": str(exc)}, status=http.HTTP_503_SERVICE_UNAVAILABLE)


@api_view(["POST"])
@permission_classes([IsEditor])
@parser_classes([MultiPartParser])
def upload_po_document(request):
    """Upload a PO file. Form fields: plant, poNumber, note, file."""
    plant = request.data.get("plant") or ""
    if not plant or not user_can_edit_plant(request.user, plant):
        return Response({"error": "You cannot upload POs for that plant."}, status=http.HTTP_403_FORBIDDEN)
    try:
        doc = documents.upload_po(plant, request.data.get("poNumber"), request.FILES.get("file"), request.user,
                                  request.data.get("note") or "")
    except object_storage.StorageNotConfigured as exc:
        return _not_configured(exc)
    except documents.DocumentError as exc:
        return _refused(exc)
    return Response(serialize(doc), status=http.HTTP_201_CREATED)


@api_view(["GET"])
def po_documents(request):
    """PO files at the plants the caller may read, newest first. Filters:
    ?plant= ?q= (PO number). Superseded and withdrawn revisions included."""
    plants = [p.code for p in Plant.objects.all() if user_can_access_plant(request.user, p.code)]
    wanted = request.query_params.get("plant")
    if wanted:
        plants = [p for p in plants if p == wanted]
    qs = Document.objects.filter(kind=Document.Kind.PO, plant__code__in=plants).select_related("plant")
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(po_number__icontains=q)
    docs = list(qs.order_by("-uploaded_at", "-id")[:500])
    known = set(PurchaseOrder.objects.filter(
        plant__code__in=plants, po_number__in={d.po_number for d in docs}).values_list("plant__code", "po_number"))
    return Response({"documents": [{**serialize(d), "poInSystem": (d.plant.code, d.po_number) in known} for d in docs],
                     "storageReady": object_storage.is_configured("po")})


@api_view(["GET"])
def open_document(request, document_id):
    """Redirects to a five-minute R2 link for the file. The page opens this
    URL in a new tab (the session cookie rides along), rather than fetching
    the link and pointing a blank tab at it: the app's
    Cross-Origin-Opener-Policy stops a script from navigating a tab it
    opened to another origin, and a direct open is never pop-up blocked."""
    doc = get_object_or_404(Document.objects.select_related("plant"), pk=document_id)
    if not user_can_access_plant(request.user, doc.plant.code):
        return HttpResponse("File not found.", status=http.HTTP_404_NOT_FOUND, content_type="text/plain")
    try:
        url = documents.open_link(doc)
    except object_storage.StorageNotConfigured:
        return HttpResponse("File storage is not set up yet.", status=http.HTTP_503_SERVICE_UNAVAILABLE,
                            content_type="text/plain")
    # ApiNoStoreMiddleware marks every /api/ response no-store, so the
    # short-lived link is never cached.
    return HttpResponseRedirect(url)


@api_view(["POST"])
@permission_classes([IsEditor])
def withdraw_document(request, document_id):
    """Body: {"reason": "..."}."""
    doc = get_object_or_404(Document.objects.select_related("plant"), pk=document_id)
    if not user_can_edit_plant(request.user, doc.plant.code):
        return Response({"error": "You are not allowed to do this for that plant."}, status=http.HTTP_403_FORBIDDEN)
    try:
        doc = documents.withdraw(doc, request.user, (request.data or {}).get("reason"))
    except documents.DocumentError as exc:
        return _refused(exc)
    return Response(serialize(doc))


@api_view(["POST"])
@permission_classes([IsEditor])
@parser_classes([MultiPartParser])
def mir_invoice(request, mir_id):
    """Attach the vendor's invoice to a posted MIR. Form fields: file, note."""
    mir = get_object_or_404(Mir.objects.select_related("plant"), pk=mir_id)
    if not user_can_edit_plant(request.user, mir.plant.code):
        return Response({"error": "You are not allowed to do this for that plant."}, status=http.HTTP_403_FORBIDDEN)
    try:
        doc = documents.upload_invoice(mir, request.FILES.get("file"), request.user, request.data.get("note") or "")
    except object_storage.StorageNotConfigured as exc:
        return _not_configured(exc)
    except documents.DocumentError as exc:
        return _refused(exc)
    return Response(serialize(doc), status=http.HTTP_201_CREATED)


def invoice_files(mir):
    """A MIR's invoice files, newest revision first (for the MIR detail)."""
    return [serialize(d) for d in mir.documents.select_related("plant").order_by("-revision")]


def po_files(po):
    """The files on record for a PO, current first (for the MIR form's PO view)."""
    qs = Document.objects.filter(kind=Document.Kind.PO, plant=po.plant, po_number=po.po_number).select_related("plant")
    return [serialize(d) for d in qs.filter(~Q(status=Document.Status.WITHDRAWN)).order_by("-revision")]
