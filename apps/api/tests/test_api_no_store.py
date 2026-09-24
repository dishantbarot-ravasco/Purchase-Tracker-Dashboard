"""
API responses are uncacheable by default (config/middleware.py's
ApiNoStoreMiddleware, 2026-09-24).

Reported as "whether the data refreshed or not until I hard reload it": a
hard reload bypasses the browser's HTTP cache and a plain one does not, so an
API response with no Cache-Control could be replayed from cache on a plain
reload.

The middleware is exercised directly as well as through the stack, because
the stack alone cannot discriminate: with DEBUG on in `.env`, settings.py
also installs NoCacheMiddleware, which stamps no-store on everything and
would make an end-to-end assertion pass with this middleware deleted.
"""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from config.middleware import ApiNoStoreMiddleware


def _run(path, response):
    return ApiNoStoreMiddleware(lambda request: response)(RequestFactory().get(path))


class TestApiNoStoreMiddleware:
    def test_api_response_without_cache_control_becomes_no_store(self):
        assert _run("/api/purchase-orders", HttpResponse("{}"))["Cache-Control"] == "no-store"

    def test_a_view_that_sets_its_own_cache_control_keeps_it(self):
        response = HttpResponse("{}")
        response["Cache-Control"] = "private, max-age=30"

        assert _run("/api/anything", response)["Cache-Control"] == "private, max-age=30"

    def test_non_api_paths_are_untouched(self):
        """Frontend files are WhiteNoise's (revalidate-on-load, see
        frontend_cache_headers); this middleware must never overrule that."""
        assert "Cache-Control" not in _run("/login.html", HttpResponse("<html>"))
        assert "Cache-Control" not in _run("/apix/lookalike", HttpResponse(""))


@pytest.mark.django_db
def test_middleware_is_installed_and_reaches_data_endpoints():
    client = APIClient()
    client.force_authenticate(user=make_user(email="nostore@ravasco.com"))

    response = client.get("/api/purchase-orders")

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_middleware_is_listed_in_settings():
    """The end-to-end test above cannot tell this middleware apart from the
    DEBUG-only NoCacheMiddleware, so pin its presence in MIDDLEWARE itself."""
    from django.conf import settings

    assert "config.middleware.ApiNoStoreMiddleware" in settings.MIDDLEWARE
