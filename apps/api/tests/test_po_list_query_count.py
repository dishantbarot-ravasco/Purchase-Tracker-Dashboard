"""
The domestic PO list reads each dismissed match's `dismissed_by.email`, and
its prefetch left that relation out - one extra query per dismissed match
on every purchase-orders load. The import list already prefetched it.
"""

import datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSDomesticPOLineItem, HRSDomesticPurchaseOrder, HRSMIREntry, HRSPOMirMatch


def _dismissed_match(n, user):
    po = HRSDomesticPurchaseOrder.objects.create(po_drive_folder_name=f"P{n}", po_number=f"30000090{n:02d}", vendor_name="V")
    item = HRSDomesticPOLineItem.objects.create(purchase_order=po, item_id="1", description="SBR", qty=Decimal("10"),
                                                net_price=Decimal("1"))
    mir = HRSMIREntry.objects.create(month="Sep-26", mir_no=f"M{n}", mir_date=datetime.date(2026, 9, 1), party_name="V",
                                     material_description="SBR", qty=Decimal("10"), uom="KG", rate=Decimal("1"),
                                     source_row_ref=str(n), is_active=True)
    HRSPOMirMatch.objects.create(po_line_item=item, mir_entry=mir, tier="material", match_score=Decimal("0.5"),
                                 dismissed_by_override=True, dismissed_by=user, dismissed_at=timezone.now())


def _queries(client):
    with CaptureQueriesContext(connection) as ctx:
        assert client.get("/api/purchase-orders").status_code == 200
    return len(ctx.captured_queries)


@pytest.mark.django_db
def test_dismissed_matches_cost_no_query_each():
    user = make_user(role="viewer")
    client = APIClient()
    client.force_authenticate(user=user)
    _dismissed_match(1, user)
    one = _queries(client)
    for n in (2, 3, 4):
        _dismissed_match(n, user)
    assert _queries(client) == one
