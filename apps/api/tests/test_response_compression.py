"""
Response compression (config/middleware.py's SelectiveGZipMiddleware,
2026-09-23 audit pass).

Pinned here:
  - API JSON is gzip-compressed when the client accepts it, and decompresses
    back to exactly the payload an uncompressed client gets - compression is
    transport only, never a change in what the API returns;
  - /api/auth/ responses are NEVER compressed (they carry access tokens in the
    body - the BREACH exclusion), and neither is /admin/;
  - a client that does not ask for gzip gets the plain response;
  - static frontend files are untouched (WhiteNoise stays in charge of them).
"""

import gzip
import json

import pytest
from django.core.cache import cache
from django.test import Client
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSDomesticPOLineItem, HRSDomesticPurchaseOrder, TrustedDevice
from apps.services.device_service import _hash_device_token

GZIP = {"HTTP_ACCEPT_ENCODING": "gzip, deflate, br"}
PASSWORD = "A-Str0ng-Test-Passphrase"


def _seed_purchase_orders(n=40):
    """Enough repetitive rows to clear GZipMiddleware's 200-byte floor by a
    wide margin, shaped like the real order book."""
    for i in range(n):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_drive_folder_name=f"PO-{i}", po_number=f"30000010{i:02d}", vendor_name="Rubamin Private Limited",
        )
        HRSDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="10", description="SBR 1502 Synthetic Rubber", qty="1000", net_price="120",
            net_value="120000",
        )


@pytest.mark.django_db
class TestSelectiveCompression:
    def setup_method(self):
        cache.clear()

    def _authed(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="gz@ravasco.com"))
        return client

    def test_api_json_is_compressed_and_round_trips_exactly(self):
        _seed_purchase_orders()
        client = self._authed()

        plain = client.get("/api/purchase-orders")
        compressed = client.get("/api/purchase-orders", **GZIP)

        assert plain.status_code == compressed.status_code == 200
        assert "Content-Encoding" not in plain
        assert compressed["Content-Encoding"] == "gzip"
        assert "Accept-Encoding" in compressed["Vary"]
        assert len(compressed.content) < len(plain.content) / 3
        assert json.loads(gzip.decompress(compressed.content)) == json.loads(plain.content)

    def test_auth_responses_carrying_a_token_are_never_compressed(self):
        """The BREACH exclusion. Login returns an access token in its body
        next to the submitted email - compress that and its length becomes a
        side channel on the token."""
        user = make_user(email="gz-login@ravasco.com", password=PASSWORD)
        token = "g" * 64
        TrustedDevice.objects.create(user=user, device_token_hash=_hash_device_token(token), device_name="T")
        client = APIClient()
        client.cookies["pt_device"] = token

        login = client.post("/api/auth/login", {"email": user.email, "password": PASSWORD}, format="json", **GZIP)
        refresh = client.post("/api/auth/token/refresh", data="{}", content_type="application/json", **GZIP)

        assert login.status_code == 200 and "access_token" in login.data
        assert "Content-Encoding" not in login
        assert refresh.status_code == 200 and "access" in refresh.data
        assert "Content-Encoding" not in refresh

    def test_admin_is_never_compressed(self):
        response = Client().get("/admin/login/", **GZIP)

        assert "Content-Encoding" not in response

    def test_static_frontend_files_are_left_to_whitenoise(self):
        """WhiteNoise short-circuits above SelectiveGZipMiddleware in
        MIDDLEWARE, so a frontend page never reaches it - its ETag/304
        revalidation (config/middleware.py's frontend_cache_headers) is
        exactly as before. frontend/ holds no pre-compressed .gz siblings, so
        any gzip encoding here could only have come from the new middleware."""
        response = Client().get("/login.html", **GZIP)

        assert response.status_code == 200
        assert "Content-Encoding" not in response
