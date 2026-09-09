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
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class DriveNotConfigured(Exception):
    """Raised when no service account credential is available - callers
    should surface this as a clear SyncRun failure, not a stack trace."""


# ── Credentials & per-thread service client ─────────────────────────────────

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
    """Returns this thread's cached Drive API client, building one on first
    use. Deliberately per-thread (not a module-level singleton) - see the
    threading.local comment above for the SSL corruption this avoids when
    multiple plants' syncs run concurrently on separate threads."""
    service = getattr(_thread_local, "service", None)
    if service is None:
        creds = _load_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        _thread_local.service = service
    return service


# ── Public API ───────────────────────────────────────────────────────────────

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
    # Every current caller passes settings.<X>_FOLDER_ID / a hardcoded mime
    # constant for parent_id/mime_type (never anything derived from a synced
    # file's own content), so this was never exploitable in practice - but
    # escaping all three clauses the same way, not just `title`, is the
    # correct defense-in-depth default so a future caller can't reintroduce
    # a query-injection gap by passing something less trusted here.
    clauses = [f"name = '{_escape(title)}'", "trashed = false"]
    if parent_id:
        clauses.append(f"'{_escape(parent_id)}' in parents")
    if mime_type:
        clauses.append(f"mimeType = '{_escape(mime_type)}'")
    query = " and ".join(clauses)
    resp = service.files().list(q=query, fields="files(id, name, modifiedTime)", pageSize=5).execute()
    files = resp.get("files", [])
    if not files:
        raise FileNotFoundError(f"No Drive file found matching title={title!r} parent={parent_id!r}")
    return files[0]["id"]


def list_files_in_folder(parent_id: str, name_prefix: str | None = None) -> list[dict]:
    """Returns [{id, name, modifiedTime}, ...] for every non-trashed file
    directly inside `parent_id`, optionally narrowed to names starting with
    `name_prefix`. Added for sync_rodtep.py - unlike every other sync
    command, RoDTEP's own Drive files are NOT one fixed title
    (find_file_id_by_title() doesn't apply): the "RODTEP SCRIPT LICENSE"
    folder holds one file per Script Number, named "RODTEP-JNPT-<N>.xlsx"
    with N incrementing as new scripts are issued - a real, ongoing count
    with no fixed final name to search for. Sorted by name so callers get a
    stable, predictable processing order run to run."""
    service = get_drive_service()
    clauses = [f"'{_escape(parent_id)}' in parents", "trashed = false"]
    if name_prefix:
        clauses.append(f"name contains '{_escape(name_prefix)}'")
    query = " and ".join(clauses)
    resp = service.files().list(q=query, fields="files(id, name, modifiedTime)", pageSize=100).execute()
    files = resp.get("files", [])
    # "name contains" is a substring match, not a prefix match - Drive API
    # has no prefix operator - so narrow further in Python for a real
    # prefix check when name_prefix was given.
    if name_prefix:
        files = [f for f in files if f["name"].startswith(name_prefix)]
    return sorted(files, key=lambda f: f["name"])


def download_spreadsheet_bytes_by_id(file_id: str) -> bytes:
    """Downloads a spreadsheet file as xlsx bytes, regardless of whether
    Drive holds it as a native Google Sheet (exported via XLSX_EXPORT_MIME)
    or an already-uploaded .xlsx file (downloaded as-is) - added for
    sync_advance_license.py, whose source file the project owner gave as a
    direct Drive share link (a file id, not a folder+title to search for,
    unlike every other sync command's source file). One metadata lookup to
    decide the mimeType, then one download."""
    service = get_drive_service()
    meta = service.files().get(fileId=file_id, fields="id, mimeType").execute()
    if meta["mimeType"] == GOOGLE_SHEET_MIME:
        return download_file_bytes(file_id, export_mime_type=XLSX_EXPORT_MIME)
    return download_file_bytes(file_id)


def _escape(value: str) -> str:
    """Escape backslash/single-quote for safe interpolation into a Drive
    API query string literal (the query is built by string formatting, not
    a parameterized API, so an unescaped title containing a quote would
    break out of the `name = '...'` clause)."""
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
