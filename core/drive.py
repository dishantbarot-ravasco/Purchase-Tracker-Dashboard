"""
Google Drive access via a dedicated service account - NOT the signed-in
user's own Drive permissions. This is what lets access control be "who's in
the UserAccess table with which plants" rather than "whoever has Drive access
to the folder", per the security decisions made for this build.

Setup required (see .env.example): the service account's own email (looks
like ...@<project>.iam.gserviceaccount.com) must be explicitly shared as a
Viewer on every plant's Drive folder - service accounts don't inherit access
automatically, sharing is a one-time manual step per folder.

Credential loading deliberately supports TWO ways to supply the key,
because Render's "Secret Files" feature has known gotchas (community.render
.com has multiple threads of secret files not showing up at the documented
/etc/secrets/<name> path). Rather than depend on getting that UI step
exactly right:

  1. GOOGLE_SERVICE_ACCOUNT_JSON - a regular environment variable
     containing the ENTIRE contents of the key file, pasted as one value.
     This is the recommended path: it's just a normal env var, the same
     mechanism already confirmed working for GOOGLE_OAUTH_CLIENT_ID etc.,
     no separate Render feature to get right.
  2. GOOGLE_SERVICE_ACCOUNT_JSON_PATH - a file path (Secret File mount,
     or any other path). Only used if #1 isn't set. Kept for anyone who
     prefers the file-based approach or is running this outside Render.

If neither resolves to real credentials, the error raised lists exactly
what was tried, rather than a bare "file not found" three directories deep.
"""
import io
import json
import os

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
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but isn't valid JSON - make sure the "
                "ENTIRE contents of service_account.json were pasted in, including the "
                "surrounding { } braces, with nothing added or stripped."
            ) from e
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    configured_path = settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    candidates = [configured_path]
    # Render's own docs note that for non-Docker services, a Secret File is
    # ALSO copied into the service's project root, not just /etc/secrets/ -
    # worth trying both rather than assuming the configured path is exact.
    fallback = os.path.join(str(settings.BASE_DIR), os.path.basename(configured_path))
    if fallback not in candidates:
        candidates.append(fallback)

    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    raise FileNotFoundError(
        "Could not find the Google service account key. Tried the "
        f"GOOGLE_SERVICE_ACCOUNT_JSON env var (not set) and these file paths: "
        f"{', '.join(candidates)}. Easiest fix: set GOOGLE_SERVICE_ACCOUNT_JSON "
        "to the full contents of service_account.json as a regular environment "
        "variable instead of a Secret File."
    )


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


def find_file_by_title(folder_id, exact_title):
    """Resolve a file by its exact title within a folder - used for the
    master CSVs, which get re-uploaded under the same title on every update
    (see the CSV versioning policy in project_automation_routines_live), so
    the file ID isn't stable but the title always is.

    Deliberately lists ALL children and matches the exact name in Python,
    rather than filtering server-side with `name contains '...'`. That
    filter looked like a reasonable narrowing optimization, but Drive's
    `contains` operator for the name field does word/token-boundary
    matching, not a true substring search - a truncated fragment that cuts
    off mid-word (e.g. "..._Domestic_P" partway into "Purchase") can fail to
    match the real file even though the file is clearly right there. Listing
    everything and comparing in Python sidesteps that entirely, and these
    folders are small enough (a handful of files) that there's no real cost
    to not filtering server-side."""
    candidates = list_children(folder_id)
    for f in candidates:
        if f["name"] == exact_title:
            return f
    return None


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
