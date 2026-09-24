"""
The unknown-email / locked-account login paths must cost what a real
password check costs (found in the 2026-09-24 audit).

`_dummy_verify()` exists so an attacker cannot tell, from response time
alone, whether an email has an account. Its hash was "$2b$12$" plus 52
'a's - 59 bytes, one short of a valid bcrypt hash - so `bcrypt.checkpw()`
raised "Invalid salt" in ~0.04 ms instead of hashing for ~240 ms, and the
function's deliberate bare `except` hid that. Unknown emails and locked
accounts answered ~240 ms faster than a wrong password on a real account:
the exact enumeration signal the function was written to remove.

Nothing caught it because nothing asserted the hash was usable. Each test
below fails against the old value:

1. the hash must verify without raising (the old one raised);
2. it must carry the same bcrypt cost as a stored password, or the two
   paths drift apart the day either cost changes;
3. end to end, an unknown email must not answer much faster than a wrong
   password. Timing is measured with a wide margin (half) because bcrypt at
   cost 12 dominates both paths by two orders of magnitude over everything
   else in them; the old bug made the ratio ~0.
"""

import time

import bcrypt
import pytest

from apps.api import auth_backend
from apps.api.auth_backend import _DUMMY_HASH, PTUserBackend
from apps.api.routers.users_views import _hash_password
from apps.api.tests.factories import make_user


def _cost(hashed: bytes) -> int:
    # "$2b$12$<22-char salt><31-char digest>" -> 12
    return int(hashed.split(b"$")[2])


def test_dummy_hash_is_a_genuine_bcrypt_hash():
    assert len(_DUMMY_HASH) == 60
    # Must return (False), not raise - raising is what made the check free.
    assert bcrypt.checkpw(b"dummy", _DUMMY_HASH) is False


def test_dummy_hash_costs_the_same_as_a_stored_password():
    assert _cost(_DUMMY_HASH) == _cost(_hash_password("Str0ngPassw0rd!").encode())


def test_dummy_verify_really_runs_bcrypt(monkeypatch):
    """The bare except in _dummy_verify() must never be what ends the call."""
    outcomes = []
    real_checkpw = bcrypt.checkpw

    def recording_checkpw(pw, hashed):
        try:
            result = real_checkpw(pw, hashed)
        except Exception as exc:
            outcomes.append(exc)
            raise
        outcomes.append(result)
        return result

    monkeypatch.setattr(auth_backend.bcrypt, "checkpw", recording_checkpw)
    auth_backend._dummy_verify()
    assert outcomes == [False]


def _fastest(fn, runs=2):
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.django_db
def test_unknown_email_is_not_faster_than_wrong_password():
    make_user(email="real@ravasco.com", password="Str0ngPassw0rd!")
    backend = PTUserBackend()

    wrong_password = _fastest(lambda: backend.authenticate(None, email="real@ravasco.com", password="wrong-password"))
    unknown_email = _fastest(lambda: backend.authenticate(None, email="nobody@ravasco.com", password="wrong-password"))

    assert unknown_email > wrong_password * 0.5, (
        f"unknown email answered in {unknown_email * 1000:.1f} ms against "
        f"{wrong_password * 1000:.1f} ms for a wrong password - the dummy check is not doing bcrypt work"
    )
