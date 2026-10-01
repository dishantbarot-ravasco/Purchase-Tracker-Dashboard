"""
apps/services/documents.py - uploaded PO and invoice files (2026-09-30).

The purchase team uploads a PO's file for its plant; the store attaches the
vendor's invoice to the MIR it posted. The file goes to Cloudflare R2
(object_storage.py, buckets "po" and "invoice"); the Document row records
it. This module is the only writer of Document rows.

Revisions, withdrawals - the PO-number edge cases:
  - Uploading a PO number the plant already has files for makes the next
    revision CURRENT and the previous one SUPERSEDED. A revised PO is just
    that: upload the new copy.
  - The same file (same SHA-256) is refused if it is already on record for
    that PO or MIR, active or superseded - an accidental second click is not
    a new revision.
  - A cancelled PO, or a file uploaded under the wrong number, is WITHDRAWN
    with a reason. Withdrawing the current revision brings back the newest
    superseded one, so undoing a wrong upload restores what was there. A PO
    that was renumbered is withdrawn under the old number and uploaded
    under the new one.
  - Nothing is deleted, in the database or in R2.

Files are accepted by their content, not their name: the first bytes must
be a PDF, JPEG or PNG. At most MAX_BYTES. A PDF with active content
(JavaScript, a launch action, an embedded file) is refused - _check_pdf().
"""

from __future__ import annotations

import hashlib
import re
import uuid

from django.db import transaction
from django.utils import timezone

from apps.services import object_storage

MAX_BYTES = 20 * 1024 * 1024
_SIGNATURES = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# PDF features that run something rather than show something (2026-10-01,
# security pass): JavaScript, launching a program, an embedded file (the
# usual way malware rides inside a PDF), Flash-era rich media, and an
# automatic action on open that names JavaScript. A PO or vendor invoice
# never needs any of them, so a PDF carrying one is refused rather than
# stored and later opened by staff. Names are matched as PDF name tokens
# (/JS must not match /JSmith), including the #xx hex-escaped spellings
# used to hide them.
_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia)(?![A-Za-z0-9])")
_PDF_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")


class DocumentError(ValueError):
    """A user-facing refusal; the message is shown as it is."""


def _sniff(head: bytes):
    for magic, content_type, ext in _SIGNATURES:
        if head.startswith(magic):
            return content_type, ext
    raise DocumentError("Upload a PDF, JPEG or PNG file.")


def _check_pdf(data: bytes) -> None:
    """Refuse a PDF with active content (_PDF_ACTIVE). A heuristic over the
    raw bytes: content inside compressed object streams is not inflated, so
    this stops the common case, not a determined attacker - files still open
    only from R2's own origin, never the app's."""
    plain = _PDF_HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), data)
    found = _PDF_ACTIVE.search(plain)
    if found:
        raise DocumentError(
            "This PDF contains active content (" + found.group(1).decode() + ") that a PO or invoice never "
            "needs, so it was not accepted. Save it again as a plain PDF (print to PDF) and upload that.")


def _read_upload(upload) -> tuple[bytes, str, str]:
    if upload is None:
        raise DocumentError("Choose a file to upload.")
    if upload.size == 0:
        raise DocumentError("The file is empty.")
    if upload.size > MAX_BYTES:
        raise DocumentError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    data = upload.read()
    content_type, ext = _sniff(data[:16])
    if content_type == "application/pdf":
        _check_pdf(data)
    return data, content_type, ext


def _clean_po_number(po_number) -> str:
    value = re.sub(r"\s+", "", str(po_number or ""))
    if not value:
        raise DocumentError("Enter the PO number.")
    if len(value) > 100:
        raise DocumentError("The PO number is too long.")
    return value


def _safe(part: str) -> str:
    return _KEY_UNSAFE.sub("_", part).strip("_")[:80] or "x"


def _store(bucket_kind: str, key: str, data: bytes, content_type: str) -> None:
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        object_storage.upload_file(bucket_kind, key, path, content_type)
    finally:
        os.unlink(path)


def _create(*, kind, plant, po_number, mir, siblings, data, content_type, ext, filename, note, user, key_prefix):
    """Common tail of both uploads. `siblings` is the locked queryset of the
    same PO's (or MIR's) earlier files."""
    from apps.core.models import Document

    digest = hashlib.sha256(data).hexdigest()
    same = siblings.filter(sha256=digest).exclude(status=Document.Status.WITHDRAWN).first()
    if same is not None:
        raise DocumentError(f"This file is already on record as revision {same.revision}.")
    revision = max((d.revision for d in siblings), default=0) + 1
    bucket = "po" if kind == Document.Kind.PO else "invoice"
    key = f"{key_prefix}/r{revision}-{uuid.uuid4().hex[:12]}.{ext}"
    # R2 first: a failed upload leaves no row pointing at nothing.
    _store(bucket, key, data, content_type)
    siblings.filter(status=Document.Status.CURRENT).update(status=Document.Status.SUPERSEDED)
    return Document.objects.create(
        kind=kind, plant=plant, po_number=po_number, mir=mir, revision=revision, status=Document.Status.CURRENT,
        storage_key=key, original_filename=(filename or "")[:255] or f"upload.{ext}", content_type=content_type,
        size_bytes=len(data), sha256=digest, note=(note or "").strip()[:2000],
        uploaded_by=user if getattr(user, "pk", None) else None, uploaded_by_email=getattr(user, "email", "") or "",
    )


@transaction.atomic
def upload_po(plant_code: str, po_number, upload, user, note: str = ""):
    from apps.core.models import Document, Plant

    object_storage.require_configured("po")
    # Locking the plant row serialises revision numbering per plant.
    plant = Plant.objects.select_for_update().filter(code=plant_code).first()
    if plant is None:
        raise DocumentError("Pick the plant the PO belongs to.")
    number = _clean_po_number(po_number)
    data, content_type, ext = _read_upload(upload)
    siblings = Document.objects.filter(kind=Document.Kind.PO, plant=plant, po_number=number)
    return _create(kind=Document.Kind.PO, plant=plant, po_number=number, mir=None, siblings=siblings, data=data,
                   content_type=content_type, ext=ext, filename=getattr(upload, "name", ""), note=note, user=user,
                   key_prefix=f"{plant.code}/{_safe(number)}")


@transaction.atomic
def upload_invoice(mir, upload, user, note: str = ""):
    from apps.core.models import Document, Mir

    object_storage.require_configured("invoice")
    mir = Mir.objects.select_for_update().select_related("plant").get(pk=mir.pk)
    if mir.status != Mir.Status.POSTED:
        raise DocumentError("A cancelled MIR takes no new files.")
    data, content_type, ext = _read_upload(upload)
    siblings = Document.objects.filter(kind=Document.Kind.INVOICE, mir=mir)
    return _create(kind=Document.Kind.INVOICE, plant=mir.plant, po_number="", mir=mir, siblings=siblings, data=data,
                   content_type=content_type, ext=ext, filename=getattr(upload, "name", ""), note=note, user=user,
                   key_prefix=f"{mir.plant.code}/{_safe(mir.mir_no)}")


@transaction.atomic
def withdraw(document, user, reason):
    """Withdraw a file (cancelled PO, wrong upload). Withdrawing the current
    revision makes the newest superseded one current again."""
    from apps.core.models import Document

    reason = (reason or "").strip()
    if not reason:
        raise DocumentError("Say why the file is being withdrawn.")
    doc = Document.objects.select_for_update().get(pk=document.pk)
    if doc.status == Document.Status.WITHDRAWN:
        raise DocumentError("This file is already withdrawn.")
    was_current = doc.status == Document.Status.CURRENT
    doc.status = Document.Status.WITHDRAWN
    doc.withdrawn_by_email = getattr(user, "email", "") or ""
    doc.withdrawn_at = timezone.now()
    doc.withdraw_reason = reason[:2000]
    doc.save(update_fields=["status", "withdrawn_by_email", "withdrawn_at", "withdraw_reason"])
    if was_current:
        siblings = (Document.objects.filter(kind=doc.kind, plant=doc.plant, po_number=doc.po_number)
                    if doc.kind == Document.Kind.PO else Document.objects.filter(kind=doc.kind, mir=doc.mir_id))
        previous = siblings.filter(status=Document.Status.SUPERSEDED).order_by("-revision").first()
        if previous is not None:
            previous.status = Document.Status.CURRENT
            previous.save(update_fields=["status"])
    return doc


def open_link(document, expires_seconds: int = 300) -> str:
    bucket = "po" if document.kind == "PO" else "invoice"
    return object_storage.presigned_url(bucket, document.storage_key, expires_seconds)
