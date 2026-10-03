"""
manage.py strip_js_comments - remove comments from frontend/js/*.js in place.

Run by the Dockerfile only, before collectstatic, so the image serves the
scripts without their comments while the repository keeps them (2026-10-03).
/js/*.js is public - the sign-in page needs its scripts before anyone has
signed in - and the comments described how the app matches records, which
the owner does not want on show (see CLAUDE.md, "The UI never explains how
matching works"). Never run it on a working copy: it rewrites the files.
The tokenizer is apps/services/js_comments.py.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.services.js_comments import strip_comments


class Command(BaseCommand):
    help = "Remove comments from frontend/js/*.js in place (Docker build only)."
    # Runs in the Docker build, which has no secrets on purpose, so the
    # system checks (apps.core.E001 among them) would stop it. It only
    # rewrites text files; the checks run again at release.sh's migrate.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(Path(settings.BASE_DIR) / "frontend" / "js"))

    def handle(self, *args, **options):
        files = sorted(Path(options["dir"]).glob("*.js"))
        before = after = 0
        for path in files:
            src = path.read_text(encoding="utf-8")
            stripped = strip_comments(src)
            path.write_text(stripped, encoding="utf-8", newline="")
            before += len(src)
            after += len(stripped)
        self.stdout.write(f"strip_js_comments: {len(files)} files, {before:,} -> {after:,} bytes")
