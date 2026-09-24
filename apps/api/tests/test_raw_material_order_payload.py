"""
Two payload fields Raw Material Analysis reads (2026-09-24).

- `locationTag` on each Stock lot: which warehouse the Stock sheet itself
  files the lot under (HRS's location_tag, Vapi's plant_tag). HRS's imported
  SBR 1502 lots are tagged RTP-1, so "HRS, Silvassa" alone - the file the lot
  came from - does not say where it sits. Achhad's sheet has neither column.
- `category`/`subCategory` on each IMPORT line item: Raw Material Analysis now
  counts open import orders, and an import-only material gets a row of its own
  that can sit in the right Category filter only if its line knows its
  category (the PO-level materialCategories is de-duplicated per PO).
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    HRSRMLot,
    MaterialCategoryReference,
    RTPAchhadRMLot,
    RTPVapiRMLot,
)


def _client():
    client = APIClient()
    client.force_authenticate(user=make_user(email="rmorders@ravasco.com", role="viewer"))
    return client


@pytest.mark.django_db
class TestLotLocationTag:
    def test_hrs_lot_carries_the_sheets_location_column(self):
        HRSRMLot.objects.create(description="SBR 1502", basic_rate="166.69", location_tag="RTP-1")
        [lot] = _client().get("/api/materials").json()["materials"]
        assert lot["locationTag"] == "RTP-1"

    def test_vapi_lot_carries_the_sheets_plant_column(self):
        RTPVapiRMLot.objects.create(description="SBR 1502", basic_rate="166.69", plant_tag="HRS")
        [lot] = _client().get("/api/vapi/materials").json()["materials"]
        assert lot["locationTag"] == "HRS"

    def test_blank_tag_is_null_not_an_empty_string(self):
        """The modal prints "Sheet location: ..." only when there is one."""
        HRSRMLot.objects.create(description="SBR 1502", basic_rate="166.69")
        [lot] = _client().get("/api/materials").json()["materials"]
        assert lot["locationTag"] is None

    def test_achhad_has_no_location_column_and_reports_null(self):
        RTPAchhadRMLot.objects.create(description="SBR 1502", rate="166.69")
        [lot] = _client().get("/api/achhad/materials").json()["materials"]
        assert lot["locationTag"] is None


@pytest.mark.django_db
class TestImportLineCategory:
    def _order(self):
        po = HRSImportPurchaseOrder.objects.create(
            po_drive_folder_name="3000001141", po_number="3000001141", vendor_name="Kumho Petrochemical",
        )
        HRSImportPOLineItem.objects.create(
            purchase_order=po, item_id="10", description="Synthetic Rubber SBR", qty_as_per_po="201600", uom="KG",
            net_price="1.85", exchange_rate="88.50",
        )
        HRSImportPOLineItem.objects.create(
            purchase_order=po, item_id="20", description="Something Unlisted", qty_as_per_po="10", uom="KG",
        )

    def _items(self):
        [po] = _client().get("/api/imports/purchase-orders").json()["purchaseOrders"]
        return {i["description"]: i for i in po["items"]}

    def test_each_line_gets_its_own_canonical_category(self):
        MaterialCategoryReference.objects.create(
            description="Synthetic Rubber SBR", normalized_description="synthetic rubber sbr",
            category="Synthetic Rubber", subcategory="SBR", subcategory_code="RM-SR001",
        )
        self._order()
        items = self._items()
        assert items["Synthetic Rubber SBR"]["category"] == "Synthetic Rubber"
        assert items["Synthetic Rubber SBR"]["subCategory"] == "SBR"
        # Not smeared across the PO: the other line has no reference entry.
        assert items["Something Unlisted"]["category"] == "Uncategorized"
        assert items["Something Unlisted"]["subCategory"] == ""

    def test_fields_the_inr_conversion_needs_are_served(self):
        """materials.js converts at netPrice x exchangeRate - both must be
        present on the list payload, not only on the detail one."""
        self._order()
        line = self._items()["Synthetic Rubber SBR"]
        assert line["netPrice"] == 1.85
        assert line["exchangeRate"] == 88.5
        assert line["qtyAsPerPo"] == 201600.0
