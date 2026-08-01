"""
Google Drive access via a dedicated service account - NOT the signed-in
user's own Drive permissions. This is what lets access control be "who's in
the UserAccess table with which plants" rather than "whoever has Drive access
to the folder", per the security decisions made for this build.

Setup required (see .env.example): the service account's own email (looks
like ...@<project>.iam.gserviceaccount.com) must be explicitly shared as a
Viewer on every plant's Drive folder - service accounts don't inherit access
automatically, sharing is a one-time manual step per folder.
"""
import io
import json

from django.conf import settings
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Needs read/write, not drive.readonly: the extraction hand-off flow creates
# new request/result files in a shared Drive folder (see upload_json_file
# below). drive.file scope wouldn't work here since that only covers files
# the app itself created, not the pre-shared plant folders it needs to read.
SCOPES = ["https://www.googleapis.com/auth/drive"]

_drive_service = None


def _load_credentials():
    path = settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    with open(path, "r", encoding="utf-8") as f:
        info = json.load(f)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def get_drive_service():
    global _drive_service
    if _drive_service is None:
        creds = _load_credentials()
        _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def list_children(folder_id, mime_type=None, name_contains=None):
    """List immediate children of a Drive folder. Returns Drive API file
    resources (id, name, mimeType, modifiedTime, ...)."""
    service = get_drive_service()
    clauses = [f"'{folder_id}' in parents", "trashed = false"]
    if mime_type:
        clauses.append(f"mimeType = '{mime_type}'")
    if name_contains:
        clauses.append(f"name contains '{name_contains}'")
    query = " and ".join(clauses)

    files, page_token = [], None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, parents)",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file_bytes(file_id):
    """Download a file's raw bytes (works for binary files like .xlsx/.pdf
    that were uploaded as-is, not native Google Docs/Sheets)."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()


def upload_json_file(folder_id, filename, data):
    """Upload a small JSON file (used for the extraction request/result
    hand-off batches). Always creates a new file - never overwrites, so
    concurrent runs never race on the same file, see ExtractionQueue."""
    from googleapiclient.http import MediaIoBaseUpload

    service = get_drive_service()
    payload = json.dumps(data, indent=2, default=str).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
    file_metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return created["id"]


def download_json_file(file_id):
    return json.loads(download_file_bytes(file_id).decode("utf-8"))
