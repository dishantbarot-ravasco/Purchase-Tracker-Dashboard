"""
apps/services/js_comments.py's strip_comments() - the Docker image serves
frontend/js through it, so a mistake ships a changed script. Each case pairs
"the comment is gone" with "the code that looks like a comment is not".
CI's frontend-lint job additionally parses every stripped script with acorn
and requires the same syntax tree as the original.
"""

from pathlib import Path

import pytest

from apps.services.js_comments import strip_comments

FRONTEND_JS = Path(__file__).resolve().parents[3] / "frontend" / "js"


@pytest.mark.parametrize("src, expected", [
    ("a = 1; // note\nb = 2;", "a = 1; \nb = 2;"),
    ("a = 1; /* note */ b = 2;", "a = 1;   b = 2;"),
    ("a/**/b", "a b"),
    ("a = 1; /* one\ntwo\nthree */ b;", "a = 1; \n\n b;"),
    ("const u = 'https://example.com'; // c", "const u = 'https://example.com'; "),
    ('const u = "/* not a comment */";', 'const u = "/* not a comment */";'),
    ("const s = 'it\\'s // fine';", "const s = 'it\\'s // fine';"),
    ("const re = /\\/\\//g; // c", "const re = /\\/\\//g; "),
    ("const re = /[/*]+/; x", "const re = /[/*]+/; x"),
    ("if (ok) return /a\\/b/.test(s);", "if (ok) return /a\\/b/.test(s);"),
    ("x = a / b / c; // c", "x = a / b / c; "),
    ("x = a++ / 2; // c", "x = a++ / 2; "),
    ("x = (a) / 2 /* c */;", "x = (a) / 2  ;"),
    ("t = `// kept ${a /* gone */ + 1} /* kept */`;", "t = `// kept ${a   + 1} /* kept */`;"),
    ("t = `a ${`b ${c} // kept`} d`; // gone", "t = `a ${`b ${c} // kept`} d`; "),
    ("t = `${ {a: 1}.a }`; // gone", "t = `${ {a: 1}.a }`; "),
    ("f = () => /x\\/y/; // c", "f = () => /x\\/y/; "),
])
def test_cases(src, expected):
    assert strip_comments(src) == expected


@pytest.mark.parametrize("src", ["'open", "/* open", "`open ${a", "x = /open"])
def test_unterminated_input_is_refused_not_guessed(src):
    with pytest.raises(ValueError):
        strip_comments(src)


@pytest.mark.parametrize("path", sorted(FRONTEND_JS.glob("*.js")), ids=lambda p: p.name)
class TestEveryRealScript:
    def test_lines_are_kept_and_a_second_pass_changes_nothing(self, path):
        src = path.read_text(encoding="utf-8")
        once = strip_comments(src)
        assert once.count("\n") == src.count("\n")
        assert strip_comments(once) == once
        assert len(once) < len(src) or "//" not in src
