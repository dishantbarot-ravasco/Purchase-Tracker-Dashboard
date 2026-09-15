"""
Guard against a CSS custom property being defined with DIFFERENT values in
both brand.css and style.css - found live during the 2026-09-15 audit pass.

THE BUG
-------
index.html is the only page that loads both stylesheets, and it loads
style.css second, so style.css's `:root` block silently won every name the
two files shared - including for brand.css's OWN rules.

brand.css owns the shared top navigation (.topnav / .nav-tabs / .nav-user,
see its header comment) and referenced the shared names 16 times. Four of
them genuinely disagreed:

    --blue     brand #1D64C7   style #2563eb
    --green    brand #1A7A4A   style #16a34a
    --purple   brand #6B46C1   style #7c3aed
    --shadow   brand 0 4px 12px ...   style 0 1px 3px ...

So the navigation bar that is supposed to be pixel-identical on every page
rendered in a different blue, green and purple, with a different shadow, on
the dashboard than on home / admin / search-po / review. Verified in a real
browser by reading the computed values both ways, not inferred from the CSS.

This was a REPEAT: style.css's own comment at the top of its :root block
already records an earlier instance (--navy defined as #0f1b2d here vs
brand.css's #1A2535). It got fixed in place, and nothing stopped the next
one - which is what this test is for.

THE FIX
-------
style.css's four copies were renamed to --dash-*, keeping their values
byte-for-byte, so this file's own rules render identically and brand.css's
nav rules now resolve to brand.css's values on the dashboard too.

WHAT THIS TEST ALLOWS
---------------------
A shared name whose value is IDENTICAL in both files is fine - that is
harmless duplication, not a collision, and --navy/--navy-solid/--border are
in that category today (--border differs only in hex letter case). Only a
genuine disagreement fails, because only a disagreement can change how
something renders depending on which page it is on.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BRAND_CSS = REPO_ROOT / "frontend" / "css" / "brand.css"
STYLE_CSS = REPO_ROOT / "frontend" / "css" / "style.css"

# Only the default :root block is compared. Theme blocks (dark mode,
# [data-theme]) legitimately redefine the same names with different values -
# that is what a theme IS - so including them would flag correct code.
_ROOT_BLOCK = re.compile(r"(?<![\w\-\[])\:root\s*\{(.*?)\}", re.DOTALL)
_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", re.IGNORECASE)


def _default_root_tokens(path):
    """Custom properties from the FIRST bare `:root { }` block in a file -
    the unconditional defaults, before any media query or theme selector."""
    text = path.read_text(encoding="utf-8")
    # Strip comments so a token mentioned in prose is not mistaken for a
    # declaration (these files are heavily commented, including with
    # example values).
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    match = _ROOT_BLOCK.search(text)
    assert match, f"no :root block found in {path.name}"
    return {name: value.strip() for name, value in _DECL.findall(match.group(1))}


def _normalize(value):
    """Hex case and incidental whitespace are not real differences."""
    return re.sub(r"\s+", " ", value).strip().lower()


def test_no_css_token_is_defined_with_conflicting_values_in_both_stylesheets():
    brand = _default_root_tokens(BRAND_CSS)
    style = _default_root_tokens(STYLE_CSS)

    shared = set(brand) & set(style)
    conflicts = {
        name: (brand[name], style[name])
        for name in sorted(shared)
        if _normalize(brand[name]) != _normalize(style[name])
    }

    assert not conflicts, (
        "These custom properties are defined with DIFFERENT values in both "
        "brand.css and style.css. index.html loads both (style.css second), so "
        "style.css's value silently wins there - including for brand.css's own "
        "shared-top-nav rules, which means the nav renders differently on the "
        "dashboard than on every other page.\n\n"
        + "\n".join(
            f"  {name}\n      brand.css: {b}\n      style.css: {s}"
            for name, (b, s) in conflicts.items()
        )
        + "\n\nFix by renaming style.css's copy to a --dash-* name (keeping its "
          "value), the way --blue/--green/--purple/--shadow were handled on "
          "2026-09-15 - not by editing brand.css, which owns the shared nav."
    )


def test_the_shared_nav_tokens_still_come_from_brand_css():
    """Positive assertion of the intended end state, so a future 'cleanup'
    that re-adds these to style.css fails with a clear reason even if the
    values happened to match at the time."""
    style = _default_root_tokens(STYLE_CSS)
    for name in ("--blue", "--green", "--purple", "--shadow"):
        assert name not in style, (
            f"{name} is defined in style.css again. brand.css owns it (the "
            f"shared top nav uses it); style.css's dashboard-scoped copy is "
            f"called --dash-{name.lstrip('-')} precisely so the two cannot "
            f"collide."
        )


def test_the_dashboard_scoped_copies_exist_and_are_used():
    """The rename must not have left dead tokens or dangling references."""
    text = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    style = _default_root_tokens(STYLE_CSS)
    for name in ("--dash-blue", "--dash-green", "--dash-shadow"):
        assert name in style, f"{name} is no longer defined in style.css"
        assert f"var({name})" in text, (
            f"{name} is defined but never used - either it should be removed "
            f"or a var() reference was missed during the rename"
        )
