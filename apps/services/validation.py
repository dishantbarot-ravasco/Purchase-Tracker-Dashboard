"""
apps/services/validation.py — lightweight format checks for manually-corrected
fields (inline "Edit Everywhere" feature).

Kept dependency-free (no Django imports), same convention as
apps/services/parsers/common.py and apps/services/import_flags.py, so these can
be unit-tested with nothing but plain Python - see
apps/services/tests/test_validation.py.

These are deliberately warn-level, not hard blockers: a GSTIN or vendor email
typed in from a real invoice can be genuinely unusual (a non-standard GSTIN
format from an old registration, a shared/departmental email address) and the
build spec calls for flagging that, not refusing to save it. Callers (the
correct_field views) should surface the boolean result as a non-fatal
"warning" in the PATCH response, never as a 400.
"""

import re

# 2 state-code digits + 10-char PAN (5 letters, 4 digits, 1 letter) + 1 entity
# code (digit or letter) + literal 'Z' + 1 checksum char (digit or letter).
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")

# Basic shape check only (local@domain.tld) - not a full RFC 5322 validator,
# matching the build spec's "basic email regex" requirement.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_BLANK_PLACEHOLDERS = {"", "not available", "n/a", "na", "none"}


# ── Public API ───────────────────────────────────────────────────────────────

def is_valid_gstin(value: str | None) -> bool:
    """True for a well-formed 15-char GSTIN, or for a blank/"Not available"
    value (spec: "or allow 'Not available'"). False otherwise - callers treat
    False as a warning, not a save-blocking error."""
    if value is None or value.strip().lower() in _BLANK_PLACEHOLDERS:
        return True
    return bool(GSTIN_RE.match(value.strip().upper()))


def is_valid_email(value: str | None) -> bool:
    """True for a value that looks like `local@domain.tld`, or for a blank/
    "Not available" value. False otherwise - warning only, same as GSTIN."""
    if value is None or value.strip().lower() in _BLANK_PLACEHOLDERS:
        return True
    return bool(EMAIL_RE.match(value.strip()))
