"""
.dockerignore must exclude everything .gitignore excludes.

Added 2026-09-23 (audit pass). .dockerignore's header has always said it
"Mirrors .gitignore", and it did not: when .gitignore gained `*.sqlite3.*`
(after dev_smoke_test.sqlite3.bak_pre_vendorgate, carrying real bcrypt hashes
and emails, was found committed - see .gitignore's own comment), nobody
updated .dockerignore, so `docker build` would have copied that backup into
the image via the Dockerfile's `COPY . .`. Two lists kept in step by hand
drift; this makes the stated relationship a checked one.

The comparison is on normalised pattern text, not on glob semantics - a
deliberately simple, strict rule: every non-comment line of .gitignore
appears in .dockerignore. .dockerignore may exclude MORE (it also excludes
.git/ itself); it may never exclude less.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _patterns(path: Path) -> set:
    patterns = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.add(line.rstrip("/"))
    return patterns


def test_dockerignore_excludes_every_gitignored_pattern():
    missing = sorted(_patterns(REPO_ROOT / ".gitignore") - _patterns(REPO_ROOT / ".dockerignore"))
    assert not missing, (
        "Patterns ignored by git but NOT by docker - `COPY . .` would bake these into the image. "
        f"Add them to .dockerignore: {missing}"
    )


def test_the_sqlite_backup_pattern_specifically_is_excluded():
    """The pattern whose absence motivated this file, pinned by name so a
    future 'tidy-up' of either list cannot quietly drop it again."""
    assert "*.sqlite3.*" in _patterns(REPO_ROOT / ".dockerignore")
