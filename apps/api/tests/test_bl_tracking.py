"""
The BL "Track" lookup (GET /api/imports/track-bl) makes a synchronous
SafeCube call that can hold one of production's two gunicorn workers for
its whole timeout. A successful lookup is cached per BL number and the
endpoint has its own throttle, so repeated clicks cannot tie the app up.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.services import bl_tracking


class _Resp:
    status_code = 200
    content = b"{}"

    def json(self):
        return {"metadata": {"shipmentNumber": "BL1"}}


@pytest.fixture
def calls(monkeypatch, settings):
    settings.SAFECUBE_API_KEY = "test-key"
    cache.clear()
    made = []

    def fake_get(*args, **kwargs):
        made.append(kwargs["params"]["shipmentNumber"])
        return _Resp()

    monkeypatch.setattr(bl_tracking.requests, "get", fake_get)
    yield made
    cache.clear()


@pytest.mark.django_db
class TestTrackBl:
    def _client(self):
        client = APIClient()
        client.force_authenticate(user=make_user(role="viewer"))
        return client

    def test_a_second_lookup_of_the_same_bl_is_served_from_the_cache(self, calls):
        client = self._client()
        assert client.get("/api/imports/track-bl", {"bl": "BL1"}).status_code == 200
        assert client.get("/api/imports/track-bl", {"bl": "BL1"}).status_code == 200
        assert calls == ["BL1"]

    def test_a_failure_is_not_cached(self, calls, monkeypatch):
        class _Down(_Resp):
            status_code = 503

        monkeypatch.setattr(bl_tracking.requests, "get", lambda *a, **k: calls.append("x") or _Down())
        assert bl_tracking.track_bl("BL2")["ok"] is False
        assert bl_tracking.track_bl("BL2")["ok"] is False
        assert calls == ["x", "x"]

    def test_the_endpoint_is_throttled(self, calls):
        client = self._client()
        codes = [client.get("/api/imports/track-bl", {"bl": f"BL{n}"}).status_code for n in range(11)]
        assert codes[:10] == [200] * 10
        assert codes[10] == 429
