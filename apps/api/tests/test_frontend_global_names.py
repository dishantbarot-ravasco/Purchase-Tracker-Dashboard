"""
Source guard: no two scripts on one page declare the same top-level
`let` / `const` / `class` name.

Every page's scripts share one global scope (no bundler - CLAUDE.md,
"load order is the dependency graph"). A repeated FUNCTION name silently
overrides; a repeated `let`/`const` is a SyntaxError that stops the whole
later script from running. That happened on 2026-09-29: mir-page.js
declared `let CURRENT_USER`, which auth.js already declares, and the MIR
page loaded but did nothing - not one API call. Browser checks that stubbed
auth.js out could not see it, so the rule is checked here from the source.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
_SCRIPT = re.compile(r'<script[^>]+src="(?:\{% static \')?/?(js/[^"\'?]+)')
_DECL = re.compile(r"^(?:let|const|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _page_scripts():
    for page in sorted(FRONTEND.glob("*.html")):
        scripts = [FRONTEND / src for src in _SCRIPT.findall(page.read_text(encoding="utf-8"))]
        yield page.name, [s for s in scripts if s.exists()]


def test_no_page_declares_a_top_level_name_twice():
    clashes = []
    for page, scripts in _page_scripts():
        seen = {}
        for script in scripts:
            for name in _DECL.findall(script.read_text(encoding="utf-8")):
                if name in seen and seen[name] != script.name:
                    clashes.append(f"{page}: `{name}` in both {seen[name]} and {script.name}")
                seen.setdefault(name, script.name)
    assert not clashes, "Top-level let/const/class declared twice on one page:\n" + "\n".join(clashes)


def test_the_guard_reads_the_mir_page():
    scripts = dict(_page_scripts())["mir.html"]
    assert [s.name for s in scripts] == ["theme-init.js", "auth.js", "shared.js", "mir-page.js"]
