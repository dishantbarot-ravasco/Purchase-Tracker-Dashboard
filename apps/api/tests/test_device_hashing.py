"""
Regression tests for the 2026-09-04 fix: TrustedDevice used to store the
raw plaintext device-trust token (device_token); it now stores only a
SHA-256 hex digest (device_token_hash) - see TrustedDevice's own docstring
(apps/core/models.py) for the full reasoning. These tests lock in that the
stored value is never the plaintext token, and that the hashed lookup path
(register -> cookie -> is_trusted_device) still round-trips correctly.
"""
import hashlib

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from apps.api.tests.factories import make_user
from apps.core.models import TrustedDevice
from apps.services.device_service import (
    DEVICE_COOKIE_NAME,
    is_trusted_device,
    register_device,
)


@pytest.mark.django_db
class TestDeviceTokenHashing:
    def setup_method(self):
        self.rf = RequestFactory()
        self.user = make_user(email="hash-test@ravasco.com")

    def test_register_device_never_stores_the_plaintext_token(self):
        """The returned plaintext token must not equal what's persisted -
        the whole point of the fix. The stored value must instead be
        exactly sha256(token)."""
        response = HttpResponse()
        request = self.rf.get("/")
        token = register_device(response, self.user.user_id, request)

        row = TrustedDevice.objects.get(user_id=self.user.user_id)
        assert row.device_token_hash != token
        assert row.device_token_hash == hashlib.sha256(token.encode()).hexdigest()

    def test_registered_device_is_trusted_via_its_plaintext_cookie(self):
        """The plaintext token handed back by register_device() (the same
        value set in the pt_device cookie) must still authenticate via
        is_trusted_device(), even though only its hash is stored."""
        response = HttpResponse()
        request = self.rf.get("/")
        token = register_device(response, self.user.user_id, request)

        check_request = self.rf.get("/")
        check_request.COOKIES[DEVICE_COOKIE_NAME] = token
        assert is_trusted_device(check_request, self.user.user_id) is True

    def test_wrong_token_is_not_trusted(self):
        """A cookie value that doesn't match any stored hash must not be
        trusted - guards against a naive implementation that accidentally
        matches on a prefix or an unhashed comparison."""
        response = HttpResponse()
        request = self.rf.get("/")
        register_device(response, self.user.user_id, request)

        check_request = self.rf.get("/")
        check_request.COOKIES[DEVICE_COOKIE_NAME] = "0" * 64
        assert is_trusted_device(check_request, self.user.user_id) is False

    def test_device_trusted_for_one_user_is_not_trusted_for_another(self):
        """A real device token must only ever authenticate the user it was
        issued to - the user_id filter in is_trusted_device()'s lookup is
        load-bearing, not redundant."""
        other_user = make_user(email="other-hash-test@ravasco.com")
        response = HttpResponse()
        request = self.rf.get("/")
        token = register_device(response, self.user.user_id, request)

        check_request = self.rf.get("/")
        check_request.COOKIES[DEVICE_COOKIE_NAME] = token
        assert is_trusted_device(check_request, other_user.user_id) is False
