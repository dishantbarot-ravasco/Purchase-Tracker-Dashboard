"""
Guard against the password-policy minimum drifting between the server, the
two forms that collect a password, and the docs - found during the
2026-09-15 audit pass, where it had already drifted.

THE BUG
-------
`_validate_password_strength()` enforces 10 characters. But the Admin Panel's
Create/Edit User form told the admin "Min. 8 characters" AND validated at 8
client-side, so a 9-character password passed the form's own check and was
then rejected by the server with a message contradicting what the form had
just said. The self-service password-change form (shared.js) correctly said
10, so two forms in the same app disagreed with each other.

`users_views.py`'s own module docstring said 8 as well - the same stale
figure in a third place. The 8 was correct historically: the minimum was
raised from 8 to 10 in the 2026-09-05 hardening pass, and only the server
was updated.

WHY A TEST RATHER THAN JUST FIXING IT
-------------------------------------
This is drift, not a one-off typo: the number lives in five places across
three languages, and the next person to change the policy will update the
validator (where the behavior is) and plausibly miss the copy. A unit test on
the validator alone would not have caught any of this, because the validator
was never wrong. The check has to be cross-file, so that is what this is.
"""

import pathlib
import re

import pytest
from rest_framework.exceptions import ValidationError

from apps.api.routers.users_views import _validate_password_strength

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _enforced_minimum():
    """Binary-search the real minimum out of the validator itself, rather
    than hardcoding it here - otherwise this test becomes a fourth place the
    number has to be kept in sync, which is the very problem it exists to
    prevent."""
    for length in range(1, 65):
        try:
            _validate_password_strength("Aa1!" + "x" * (length - 4), "someone@ravasco.com")
        except ValidationError:
            continue
        return length
    raise AssertionError("no accepted password length found below 65 characters")


def test_the_enforced_minimum_is_what_we_think_it_is():
    """Sanity anchor - if this changes deliberately, the assertions below
    follow automatically because they all read _enforced_minimum()."""
    assert _enforced_minimum() == 10


@pytest.mark.parametrize("relative_path, pattern, description", [
    ("frontend/admin.html", r'id="uf-password"[^>]*placeholder="Min\. (\d+) characters"',
     "Admin Panel user-form password placeholder"),
    ("frontend/js/admin-page.js", r"password\.length < (\d+)",
     "Admin Panel client-side password length check"),
    ("frontend/js/shared.js", r'id="cpwNew"[^>]*placeholder="Min\. (\d+) characters"',
     "self-service password-change placeholder"),
    ("frontend/js/shared.js", r"pw\.length < (\d+)",
     "self-service password-change client-side check"),
])
def test_every_stated_minimum_matches_the_server(relative_path, pattern, description):
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    found = re.findall(pattern, source)
    assert found, (
        f"could not find the {description} in {relative_path} - if it was "
        f"renamed or restructured, update this test's pattern rather than "
        f"deleting the check"
    )
    expected = str(_enforced_minimum())
    for value in found:
        assert value == expected, (
            f"{description} ({relative_path}) states a minimum of {value}, but "
            f"_validate_password_strength() enforces {expected}. A user told "
            f"{value} who types {value} characters gets a server rejection "
            f"contradicting the form they just filled in."
        )


def test_user_facing_error_messages_quote_the_enforced_minimum():
    """The error text itself must not drift either - being told 'at least 8'
    after a rejection is worse than the original mistake."""
    expected = str(_enforced_minimum())
    for relative_path in ("frontend/js/admin-page.js", "frontend/js/shared.js"):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for stated in re.findall(r"Password must be at least (\d+) characters", source):
            assert stated == expected, (
                f"{relative_path} tells the user 'at least {stated} characters' "
                f"but the server enforces {expected}"
            )


def test_the_create_pt_user_command_agrees_too():
    """The CLI bootstrap path enforces its own copy of the rule (it runs
    before any admin exists, so it cannot call the API) - it must not drift
    from the API's copy either."""
    source = (REPO_ROOT / "apps/core/management/commands/create_pt_user.py").read_text(encoding="utf-8")
    found = re.findall(r"len\(password\) < (\d+)", source)
    assert found, "create_pt_user.py no longer has a length check"
    for value in found:
        assert value == str(_enforced_minimum()), (
            f"create_pt_user.py enforces {value} but the API enforces "
            f"{_enforced_minimum()} - the first admin account would be held to "
            f"a different standard than every account created after it"
        )
