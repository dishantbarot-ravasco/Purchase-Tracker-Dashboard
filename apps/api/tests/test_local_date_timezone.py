"""
Regression tests for the naive-`date.today()` bug found by ruff's DTZ rules
during the 2026-09-15 audit pass.

THE BUG
-------
Four sites computed "today" with `datetime.date.today()`, which returns the
date in the *server process's* local timezone. Render runs its containers in
UTC; this app's `TIME_ZONE` is `Asia/Kolkata` (UTC+5:30) with `USE_TZ=True`.
So for the 5.5 hours from 00:00 to 05:29 IST every single day, the server's
own date was still *yesterday* in IST terms:

    18:30 UTC  ->  date.today() = 2026-09-15   (server/UTC)
                   IST calendar = 2026-09-16

Consequences: an Import PO that became overdue "today" reported
`deliveryDateStatus: "On Order"` until 05:30 IST, and the consumption
window in `_daily_movement_points()` / `consumption_stats()` started a day
late. It looked correct in local dev only because the developer's machine is
already on IST - the divergence only ever appears on the UTC deployment.

THE FIX
-------
All four sites now use `django.utils.timezone.localdate()`, which resolves
against `settings.TIME_ZONE` rather than the process's local clock.

WHAT THESE TESTS LOCK IN
------------------------
1. `test_delivery_status_follows_settings_timezone_not_server_clock` -
   behavioral: patches `timezone.localdate` and asserts the API's answer
   actually moves with it. If anyone reverts to `datetime.date.today()`, the
   patch stops taking effect and this fails.
2. `test_no_app_module_uses_naive_date_today` - a source guard, so a NEW
   naive call site added anywhere in app code fails CI immediately rather
   than waiting to be re-found by the next audit. Deliberately a source scan
   rather than a lint config, so it holds even if the ruff DTZ rule is ever
   relaxed or the linter is skipped.
"""

import datetime
import pathlib
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import RTPVapiImportPOLineItem, RTPVapiImportPurchaseOrder

LIST_URL = "/api/imports/purchase-orders"

# The exact instant the bug bites: 18:30 UTC on the 15th is 00:00 IST on the
# 16th. A delivery date of the 15th is overdue on the IST calendar but NOT on
# the server's own UTC calendar.
_SERVER_UTC_DATE = datetime.date(2026, 9, 15)
_IST_DATE = datetime.date(2026, 9, 16)


def _make_po_due_on(delivery_date):
    po = RTPVapiImportPurchaseOrder.objects.create(
        po_drive_folder_name="1000009999",
        po_number="1000009999",
        vendor_name="Timezone Test Vendor Ltd",
        currency="USD",
        total_value="1000.00",
    )
    RTPVapiImportPOLineItem.objects.create(
        purchase_order=po,
        item_id="1",
        description="TZ TEST MATERIAL",
        hsn="12345678",
        qty_as_per_po="100",
        uom="KG",
        net_price="1.5",
        net_value="150",
        country_of_origin="China",
        delivery_date=delivery_date,
    )
    return po


def _delivery_status(client):
    resp = client.get(LIST_URL)
    assert resp.status_code == 200, resp.data
    pos = [p for p in resp.data["purchaseOrders"] if p["poNumber"] == "1000009999"]
    assert pos, "test PO missing from the response"
    return pos[0]["items"][0]["deliveryDateStatus"]


@pytest.mark.django_db
def test_delivery_status_follows_settings_timezone_not_server_clock():
    """A PO due on the 15th, evaluated at 00:00 IST on the 16th (= 18:30 UTC
    on the 15th), must read Overdue - the IST calendar has rolled over even
    though the server's own UTC date has not."""
    client = APIClient()
    client.force_authenticate(user=make_user(role="admin"))
    _make_po_due_on(_SERVER_UTC_DATE)

    # timezone.localdate() resolves against settings.TIME_ZONE (Asia/Kolkata),
    # so at this instant it is already the 16th.
    with patch("django.utils.timezone.localdate", return_value=_IST_DATE):
        assert _delivery_status(client) == "Overdue"

    # Sanity check the other direction: while it is genuinely still the 15th
    # in IST, the same PO is not yet overdue. This is what proves the first
    # assertion is reading the patched value rather than passing by accident.
    with patch("django.utils.timezone.localdate", return_value=_SERVER_UTC_DATE):
        assert _delivery_status(client) == "On Order"


def test_no_app_module_uses_naive_date_today():
    """Source guard - no application module may call `date.today()`/
    `datetime.now()` without a timezone. Tests and migrations are exempt
    (migrations are generated; tests pin their own clock deliberately)."""
    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for path in list((root / "apps").rglob("*.py")) + list((root / "config").rglob("*.py")):
        parts = set(path.parts)
        if "migrations" in parts or "tests" in parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "noqa: DTZ" in line:
                continue
            if "date.today()" in line or "datetime.now()" in line or "datetime.today()" in line:
                offenders.append(f"{path.relative_to(root)}:{lineno}: {stripped}")

    assert not offenders, (
        "Naive date/time call(s) found in application code. Use "
        "django.utils.timezone.localdate() / timezone.now() instead - see this "
        "module's docstring for the production bug this guards against:\n  "
        + "\n  ".join(offenders)
    )
