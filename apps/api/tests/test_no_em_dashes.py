"""
No em dashes anywhere in the repo (project owner, 2026-09-22 - see CLAUDE.md's
opening lines: "Not in code comments, docstrings, these markdown files, UI copy,
or emails - use a spaced hyphen").

The rule was applied as a one-time sweep (604 occurrences, 117 files), and a
sweep is only as good as the day it ran: the same batch of commits that did it
also added advance_license_report.py with an em dash in a LIVE EMAIL SUBJECT,
and the sweep never looked at .env.example at all. Both were found by the
2026-09-23 audit. This guard turns the sweep into a rule.

The character is spelled chr(0x2014) here so this file can never trip itself.
Migrations are excluded: they are generated, historical, and never read by a
person or sent anywhere.
"""

from pathlib import Path

EM_DASH = chr(0x2014)
REPO_ROOT = Path(__file__).resolve().parents[3]
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".sh", ".example"}
TEXT_NAMES = {"Dockerfile", ".dockerignore", ".gitignore", ".env.example"}
SKIP_DIRS = {".git", ".venv", "node_modules", "staticfiles", "logs", "migrations", "__pycache__", ".pytest_cache",
             ".ruff_cache", "htmlcov", ".claude"}


def _text_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def test_the_scan_covers_the_files_the_rule_is_about():
    """A scan that silently matched nothing would always pass."""
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _text_files()}
    for expected in ("CLAUDE.md", ".env.example", "apps/services/advance_license_report.py", "frontend/js/shared.js"):
        assert expected in scanned, f"{expected} is not being scanned"


def test_no_em_dashes_anywhere():
    offenders = []
    for path in _text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if EM_DASH in line:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{number}")
    assert not offenders, "Em dash found - use a spaced hyphen ' - ' instead (CLAUDE.md):\n  " + "\n  ".join(offenders)
