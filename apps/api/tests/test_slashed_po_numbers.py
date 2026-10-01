"""
PO numbers that contain "/" (HRS's legacy series HRS/HO/26-27/003, Achhad's
RTP2/HO/26-27/001, Vapi's 0014/2026-27) must reach every per-PO endpoint.

The frontend sends encodeURIComponent(poNumber), but the WSGI server decodes
%2F back to "/" before Django resolves the URL, so a <str:po_number> segment
never matched and every edit, pin, receipt change, flag dismissal and the
manual-changes history 404'd for these orders. The routes use <path:> now,
and the bare import detail route sits after its suffixed siblings so it
cannot read "X/fields" as a PO number.
"""

from urllib.parse import quote

import pytest
from django.urls import resolve
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    RTPAchhadDomesticPOLineItem,
    RTPAchhadDomesticPurchaseOrder,
    RTPVapiImportPOLineItem,
    RTPVapiImportPurchaseOrder,
)

SLASHED = "RTP2/HO/26-27/001"

DOMESTIC_SUFFIXES = {
    "fields": "correct-field",
    "mir-candidates": "mir-candidates",
    "mir-match": "set-mir-match",
    "mir-match/preview": "preview-mir-match",
    "manual-changes": "manual-changes",
    "flags/dismiss": "dismiss-flag",
}

IMPORT_SUFFIXES = {
    "fields": "imports-correct-field",
    "mir-candidates": "imports-mir-candidates",
    "mir-match": "imports-set-mir-match",
    "mir-match/preview": "imports-preview-mir-match",
    "manual-changes": "imports-manual-changes",
    "flags/dismiss": "imports-dismiss-flag",
}


@pytest.mark.parametrize("prefix,plant", [("", "hrs"), ("achhad/", "achhad"), ("vapi/", "vapi")])
@pytest.mark.parametrize("suffix", list(DOMESTIC_SUFFIXES))
def test_domestic_routes_resolve_a_slashed_po_number(prefix, plant, suffix):
    match = resolve(f"/api/{prefix}purchase-orders/{SLASHED}/{suffix}")
    assert match.url_name == f"{plant}-{DOMESTIC_SUFFIXES[suffix]}"
    assert match.kwargs["po_number"] == SLASHED


@pytest.mark.parametrize("suffix", list(IMPORT_SUFFIXES))
def test_import_routes_resolve_a_slashed_po_number(suffix):
    match = resolve(f"/api/imports/purchase-orders/vapi/{SLASHED}/{suffix}")
    assert match.url_name == IMPORT_SUFFIXES[suffix]
    assert match.kwargs == {"plant": "vapi", "po_number": SLASHED}


def test_import_detail_resolves_a_slashed_po_number_without_swallowing_suffixes():
    match = resolve(f"/api/imports/purchase-orders/vapi/{SLASHED}")
    assert match.url_name == "imports-purchase-order-detail"
    assert match.kwargs == {"plant": "vapi", "po_number": SLASHED}


@pytest.mark.django_db
def test_editor_corrects_a_slashed_domestic_po_through_the_encoded_url():
    po = RTPAchhadDomesticPurchaseOrder.objects.create(
        po_drive_folder_name="RTP2-HO-26-27-001", po_number=SLASHED, vendor_name="Old Vendor", currency="INR")
    RTPAchhadDomesticPOLineItem.objects.create(purchase_order=po, item_id="1", description="Widget", qty="10", net_price="1")
    client = APIClient()
    client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))

    # The URL exactly as po-modal.js builds it.
    response = client.patch(f"/api/achhad/purchase-orders/{quote(SLASHED, safe='')}/fields",
                            {"field": "vendor_name", "value": "New Vendor"}, format="json")

    assert response.status_code == 200
    po.refresh_from_db()
    assert po.vendor_name == "New Vendor"


@pytest.mark.django_db
def test_slashed_import_po_detail_loads():
    po = RTPVapiImportPurchaseOrder.objects.create(
        po_drive_folder_name="0014-2026-27", po_number="0014/2026-27", vendor_name="V", currency="USD", total_value="10")
    RTPVapiImportPOLineItem.objects.create(purchase_order=po, item_id="1", description="Widget", hsn="1", qty_as_per_po="1",
                                           uom="KG", net_price="1", net_value="1", country_of_origin="China")
    client = APIClient()
    client.force_authenticate(user=make_user(role="viewer"))

    response = client.get(f"/api/imports/purchase-orders/vapi/{quote(po.po_number, safe='')}")

    assert response.status_code == 200
    assert response.json()["poNumber"] == "0014/2026-27"
