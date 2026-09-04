"""
Thin wrapper around the Google Drive API for unattended (service-account)
access. Deliberately kept separate from parsing logic (apps/core/parsers/)
so the parsers can be tested against local files with no live credentials
at all - only this module needs a real Drive connection.
"""

import io
import json
import threading

from django.conf import settings
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

XLSX_EXPORT_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DriveNotConfigured(Exception):
    """Raised when no service account credential is available - callers
    should surface this as a clear SyncRun failure, not a stack trace."""


def _load_credentials():
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        return service_account.Credentials.from_service_account_file(
            settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    raise DriveNotConfigured(
        "Neither GOOGLE_SERVICE_ACCOUNT_JSON nor GOOGLE_SERVICE_ACCOUNT_FILE is set."
    )


# googleapiclient's default transport (httplib2.Http) is not thread-safe -
# it holds one underlying HTTP/SSL connection per instance, and reusing that
# connection concurrently across threads corrupts the TLS session. This app's
# sync_trigger.py runs each plant's sync pipeline on its own background
# thread, and the dashboard's "Refresh Data" button fires all 3 plants'
# triggers together - a single module-level service object was getting
# shared across those threads, producing intermittent SSL errors ("EOF
# occurred in violation of protocol", "DECRYPTION_FAILED_OR_BAD_RECORD_MAC",
# "WRONG_VERSION_NUMBER") on whichever plants' threads lost the race for the
# shared connection. Caching per-thread (threading.local) instead of
# globally gives each thread its own service/connection, so concurrent
# per-plant syncs stop corrupting each other's SSL state. Confirmed as the
# cause 2026-09-04: those exact errors appeared only when multiple plants'
# sync-trigger POSTs landed within the same second.
_thread_local = threading.local()


def get_drive_service():
    service = getattr(_thread_local, "service", None)
    if service is None:
        creds = _load_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        _thread_local.service = service
    return service


def find_file_id_by_title(title: str, parent_id: str | None = None, mime_type: str | None = None) -> str:
    """Returns the Drive file id for the first file matching `title`
    exactly, optionally scoped to a parent folder and/or mime type. Raises
    if nothing (or more than expected ambiguity isn't resolved) is found -
    callers should let this surface as a SyncRun failure rather than
    silently sync stale data against a wrong/missing file."""
    service = get_drive_service()
    # Drive API v3 (which this client is built against) uses "name", not
    # v2's "title" - a v2-style query silently returns HTTP 400 Invalid
    # Value, confirmed hitting this in practice.
    clauses = [f"name = '{_escape(title)}'", "trashed = false"]
    if parent_id:
        clauses.append(f"'{parent_id}' in parents")
    if mime_type:
        clauses.append(f"mimeType = '{mime_type}'")
    query = " and ".join(clauses)
    resp = service.files().list(q=query, fields="files(id, name, modifiedTime)", pageSize=5).execute()
    files = resp.get("files", [])
    if not files:
        raise FileNotFoundError(f"No Drive file found matching title={title!r} parent={parent_id!r}")
    return files[0]["id"]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def download_file_bytes(file_id: str, export_mime_type: str | None = None) -> bytes:
    """Downloads a file's raw bytes. For a native Google Sheet, pass
    export_mime_type (e.g. XLSX_EXPORT_MIME) to export it; for a plain
    uploaded file (like the master CSV), leave export_mime_type unset."""
    service = get_drive_service()
    if export_mime_type:
        request = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
    else:
        request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()
