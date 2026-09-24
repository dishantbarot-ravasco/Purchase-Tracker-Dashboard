"""Keep docs/ and CLAUDE.md in step with the code.

Two modes, wired in .claude/settings.json:

  post  - PostToolUse on Write|Edit. When a code file is edited, injects a reminder naming
          the doc that describes it, so the doc is updated in the same change.
  stop  - Stop. If the working tree has code changes but no doc changes, blocks once and asks
          for the docs to be updated (or for an explicit statement that none are needed).
          stop_hook_active stops it from looping.

Stdlib only; never raises - a broken hook must not wedge a session.
"""

import json
import os
import subprocess
import sys

CODE_EXT = {".py", ".js", ".html", ".css", ".toml", ".yml", ".yaml", ".sh"}
CODE_NAMES = {"Dockerfile", "docker-compose.yml", "render.yaml", ".env.example"}
SKIP_PARTS = ("staticfiles/", "/migrations/", ".venv/", "docs/", ".claude/", "__pycache__/")

# First matching prefix wins; order from most to least specific.
DOC_MAP = [
    ("apps/services/tests/", "docs/testing-deployment.md"),
    ("apps/api/tests/", "docs/testing-deployment.md"),
    ("apps/services/consumption", "docs/consumption.md"),
    ("apps/services/stock_consumption", "docs/consumption.md"),
    ("apps/core/management/commands/compute_", "docs/consumption.md"),
    ("apps/services/matching", "docs/matching-engine.md"),
    ("apps/services/stock_identity", "docs/matching-engine.md"),
    ("apps/services/no_po_vendors", "docs/matching-engine.md"),
    ("apps/services/mir_without_po", "docs/matching-engine.md"),
    ("apps/services/rm_untracked", "docs/matching-engine.md"),
    ("apps/services/import_flags", "docs/matching-engine.md"),
    ("apps/core/management/commands/match_", "docs/matching-engine.md"),
    ("apps/services/parsers/", "docs/data-sync.md"),
    ("apps/services/sync_", "docs/data-sync.md"),
    ("apps/services/import_sync", "docs/data-sync.md"),
    ("apps/services/google_client", "docs/data-sync.md"),
    ("apps/services/bl_tracking", "docs/data-sync.md"),
    ("apps/services/validation", "docs/data-sync.md"),
    ("apps/services/data_quality", "docs/data-sync.md"),
    ("apps/services/arithmetic_checks", "docs/data-sync.md"),
    ("apps/core/management/commands/create_pt_user", "docs/auth-security-email.md"),
    ("apps/core/management/commands/prune_revoked", "docs/auth-security-email.md"),
    ("apps/core/management/commands/report_", "docs/api-and-features.md"),
    ("apps/core/management/", "docs/data-sync.md"),
    ("apps/api/auth_", "docs/auth-security-email.md"),
    ("apps/api/permissions", "docs/auth-security-email.md"),
    ("apps/api/routers/device_", "docs/auth-security-email.md"),
    ("apps/api/routers/google_oauth", "docs/auth-security-email.md"),
    ("apps/api/routers/password_", "docs/auth-security-email.md"),
    ("apps/services/otp_service", "docs/auth-security-email.md"),
    ("apps/services/password_service", "docs/auth-security-email.md"),
    ("apps/services/device_service", "docs/auth-security-email.md"),
    ("apps/services/token_revocation", "docs/auth-security-email.md"),
    ("apps/services/security_alerts", "docs/auth-security-email.md"),
    ("apps/services/email_service", "docs/auth-security-email.md"),
    ("config/middleware", "docs/auth-security-email.md"),
    ("config/security_headers", "docs/auth-security-email.md"),
    ("apps/api/", "docs/api-and-features.md"),
    ("apps/services/", "docs/api-and-features.md"),
    ("frontend/", "docs/frontend.md"),
    ("apps/core/", "docs/architecture.md"),
    ("config/", "docs/architecture.md"),
]
DEPLOY_FILES = ("Dockerfile", "docker-compose.yml", "docker-entrypoint.sh", "release.sh",
                "render.yaml", "pyproject.toml", ".env.example", ".github/")


def project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def rel(path):
    try:
        r = os.path.relpath(path, project_dir())
    except ValueError:
        return None
    r = r.replace("\\", "/")
    return None if r.startswith("../") else r


def is_code(r):
    if not r or any(p in "/" + r for p in SKIP_PARTS) or r.startswith(SKIP_PARTS):
        return False
    name = r.rsplit("/", 1)[-1]
    return name in CODE_NAMES or os.path.splitext(name)[1] in CODE_EXT or r.startswith(".github/")


def is_doc(r):
    return r == "CLAUDE.md" or (r.startswith("docs/") and r.endswith(".md"))


def doc_for(r):
    if r.startswith(DEPLOY_FILES):
        return "docs/testing-deployment.md"
    for prefix, doc in DOC_MAP:
        if r.startswith(prefix):
            return doc
    return "docs/architecture.md"


def post(data):
    ti = data.get("tool_input") or {}
    r = rel(ti.get("file_path") or "")
    if not is_code(r):
        return
    msg = (
        f"Docs rule: you edited {r}. Before finishing, update its section in {doc_for(r)} "
        f"(and CLAUDE.md if a rule, trap, or command changed) so it describes the code as it is "
        f"now - rewrite or delete the text describing the old behaviour, do not append a changelog."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))


def changed_files():
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_dir(), capture_output=True, text=True, timeout=20,
    ).stdout
    files = []
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        files.append(path)
    return files


def stop(data):
    if data.get("stop_hook_active"):
        return
    files = changed_files()
    code = [f for f in files if is_code(f)]
    if not code or any(is_doc(f) for f in files):
        return
    docs = sorted({doc_for(f) for f in code})
    shown = ", ".join(code[:8]) + (" ..." if len(code) > 8 else "")
    reason = (
        f"Code changed ({shown}) but no docs changed. Update {', '.join(docs)} (and CLAUDE.md if "
        f"a rule, trap, or command changed) to describe the current code, removing descriptions "
        f"of replaced code. If the change genuinely needs no doc update, say so and stop."
    )
    print(json.dumps({"decision": "block", "reason": reason}))


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    try:
        {"post": post, "stop": stop}[sys.argv[1]](data)
    except Exception:
        pass


if __name__ == "__main__":
    main()
