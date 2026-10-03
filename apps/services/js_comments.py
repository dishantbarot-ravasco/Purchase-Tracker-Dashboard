"""
apps/services/js_comments.py - strip_comments(): a JavaScript source without
its comments. Used by `manage.py strip_js_comments` at Docker build time, and
by CI's frontend-lint job, which runs it with no Django set up - so this file
must never import Django.

A small tokenizer, not a regex: it knows strings, template literals (with
nested ${...}) and regex literals, so a "//" inside "https://..." or a regex
is left alone. Whitespace and line breaks are kept (a block comment becomes
the same number of newlines, or one space), so line numbers in a browser's
error still match the repository's file.

Whether a "/" starts a regex or divides is decided from the token before it,
the usual heuristic. CI parses every stripped script and compares its syntax
tree with the original's, and `test_strip_js_comments.py` covers the cases.
"""

# After one of these, a "/" begins a regex literal; after anything else (a
# name, a number, a string, ")" or "]") it divides.
_REGEX_AFTER_PUNCT = set("(,=:[!&|?{};+-*%<>~^}")
_REGEX_AFTER_WORDS = {
    "return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
    "throw", "instanceof", "yield", "await",
}


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def strip_comments(src: str) -> str:
    """The script without its comments; raises ValueError on an
    unterminated string, template, regex or comment rather than guessing."""
    out = []
    i, n = 0, len(src)
    prev = ""  # the last token: a punctuator, a keyword, or "value"
    braces = []  # open ${ ... } expressions, each with its own { depth
    in_template = False

    while i < n:
        c = src[i]

        if in_template:
            if c == "\\":
                out.append(src[i:i + 2])
                i += 2
            elif c == "`":
                out.append(c)
                i += 1
                in_template = False
                prev = "value"
            elif src.startswith("${", i):
                out.append("${")
                i += 2
                braces.append(0)
                in_template = False
                prev = "("
            else:
                out.append(c)
                i += 1
            continue

        if c in " \t\r\n\f\v﻿":
            out.append(c)
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end == -1:
                raise ValueError(f"unterminated block comment at offset {i}")
            out.append("\n" * src.count("\n", i, end) or " ")
            i = end + 2
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise ValueError(f"unterminated string at offset {i}")
                j += 1
            if j >= n:
                raise ValueError(f"unterminated string at offset {i}")
            out.append(src[i:j + 1])
            i = j + 1
            prev = "value"
        elif c == "`":
            out.append(c)
            i += 1
            in_template = True
        elif c == "/" and (prev == "" or prev in _REGEX_AFTER_PUNCT or prev in _REGEX_AFTER_WORDS):
            j = i + 1
            in_class = False
            while True:
                if j >= n or src[j] == "\n":
                    raise ValueError(f"unterminated regex at offset {i}")
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if in_class:
                    in_class = ch != "]"
                elif ch == "[":
                    in_class = True
                elif ch == "/":
                    break
                j += 1
            j += 1
            while j < n and _is_word_char(src[j]):  # flags
                j += 1
            out.append(src[i:j])
            i = j
            prev = "value"
        elif _is_word_char(c):
            j = i
            while j < n and _is_word_char(src[j]):
                j += 1
            word = src[i:j]
            out.append(word)
            i = j
            prev = word if word in _REGEX_AFTER_WORDS else "value"
        elif c in "+-" and src.startswith(c * 2, i):
            out.append(c * 2)  # ++ / --: an operand, so a "/" after it divides
            i += 2
            prev = "value"
        else:
            if c == "{" and braces:
                braces[-1] += 1
            elif c == "}" and braces:
                if braces[-1] == 0:
                    braces.pop()
                    out.append(c)
                    i += 1
                    in_template = True
                    continue
                braces[-1] -= 1
            out.append(c)
            i += 1
            prev = c

    if in_template or braces:
        raise ValueError("unterminated template literal")
    return "".join(out)
