"""
Behavioural plant-scoping check: a viewer scoped to HRS calls EVERY GET
endpoint in the URL table, and no response may carry another plant's data.

test_endpoint_permission_guard.py reads the source and can only confirm that a
scoping helper is CALLED somewhere in a view - not that it guards the data.
That is how the RoDTEP and Advance Licence ledgers served every plant's import
POs to a scoped account until 2026-09-25: they read plants through
license_links.collect_citations(), which the source scan never recognised as
handling a plant. This test checks the outcome instead: it seeds a PO at
every plant (domestic and import, the import lines naming a licence and a BOE)
with a number that appears nowhere else, fills every URL parameter with the
OTHER plants' values, and asserts none of those numbers comes back.

Responses that refuse (403/404) pass trivially; what fails is a 200 carrying
Achhad's or Vapi's numbers to an HRS-only account.
"""

import datetime
from decimal import Decimal

import pytest
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core import models as m

# Numbers that exist only in this test's seed data.
FOREIGN = {
    "achhad": {"po": "1199990001", "import_po": "1199990002", "boe": "7777001", "license": "0799990001"},
    "vapi": {"po": "1099990001", "import_po": "1099990002", "boe": "7777002", "license": "0899990001"},
}
HOME = {"po": "3099990001", "import_po": "4599990001", "boe": "7777000", "license": "0999990001"}

# GETs that are not plant data at all, or that are someone else's gate.
NOT_PLANT_DATA = {
    "health", "readiness",  # uptime probes
}


def _seed():
    plants = {
        "hrs": (m.HRSDomesticPurchaseOrder, m.HRSDomesticPOLineItem, m.HRSImportPurchaseOrder, m.HRSImportPOLineItem, HOME),
        "achhad": (m.RTPAchhadDomesticPurchaseOrder, m.RTPAchhadDomesticPOLineItem, m.RTPAchhadImportPurchaseOrder, m.RTPAchhadImportPOLineItem, FOREIGN["achhad"]),
        "vapi": (m.RTPVapiDomesticPurchaseOrder, m.RTPVapiDomesticPOLineItem, m.RTPVapiImportPurchaseOrder, m.RTPVapiImportPOLineItem, FOREIGN["vapi"]),
    }
    for _key, (po_model, item_model, imp_model, imp_item_model, ids) in plants.items():
        fields = {f.name for f in po_model._meta.fields}
        kwargs = dict(po_drive_folder_name="PO_" + ids["po"], po_number=ids["po"],
                      po_created_date=datetime.date(2026, 5, 1), vendor_name="Seed Vendor")
        for opt, val in (("tax_type", "IGST"), ("total_value", Decimal("1")), ("total_inclusive_value", Decimal("1"))):
            if opt in fields:
                kwargs[opt] = val
        po = po_model.objects.create(**kwargs)
        item_model.objects.create(purchase_order=po, item_id="1", description="Seed Material", hsn="1",
                                  qty=Decimal("1"), uom="KG", net_price=Decimal("1"), net_value=Decimal("1"))
        imp = imp_model.objects.create(po_drive_folder_name="PO_" + ids["import_po"], po_number=ids["import_po"],
                                       po_created_date=datetime.date(2026, 5, 1), vendor_name="Seed Vendor",
                                       currency="USD", total_value=Decimal("1"))
        imp_item_model.objects.create(purchase_order=imp, item_id="1", description="Seed Material", hsn="1",
                                      qty_as_per_po=Decimal("1"), qty_as_per_boe=Decimal("1"), uom="KG",
                                      net_price=Decimal("1"), net_value=Decimal("1"), boe_number=ids["boe"],
                                      license_type="RoDTEP", license_number=ids["license"])


def _get_patterns(resolver=None, prefix=""):
    """(route string, name) for every URL pattern, flattened."""
    resolver = resolver or get_resolver()
    out = []
    for p in resolver.url_patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            out.extend(_get_patterns(p, route))
        elif isinstance(p, URLPattern):
            out.append((route, p.name or ""))
    return out


def _fill(route, plant):
    """The route with every parameter set to `plant`'s values, or None when a
    parameter has no sensible foreign value (ids of rows this test does not
    own) - those endpoints are covered by their own scoping tests."""
    ids = FOREIGN[plant]
    values = {"plant": plant, "po_number": ids["import_po"] if "imports" in route else ids["po"],
              "script_no": ids["license"]}
    import re
    missing = []

    def sub(match):
        name = match.group(2)
        if name not in values:
            missing.append(name)
            return ""
        return values[name]
    filled = re.sub(r"<(?:(\w+):)?(\w+)>", sub, route)
    return None if missing else "/" + filled


@pytest.mark.django_db
def test_an_hrs_only_viewer_never_receives_another_plants_data():
    _seed()
    client = APIClient(HTTP_HOST="localhost")
    client.force_authenticate(user=make_user(email="scoped@ravasco.com", role="viewer", plants=["hrs"]))

    checked, leaks = 0, []
    for route, name in _get_patterns():
        if not route.startswith("api/") or name in NOT_PLANT_DATA or "auth/" in route:
            continue
        for plant in ("achhad", "vapi"):
            url = _fill(route, plant)
            if url is None:
                continue
            res = client.get(url)
            if res.status_code == 405:
                break  # not a GET endpoint
            checked += 1
            body = res.content.decode("utf-8", "replace")
            for other, ids in FOREIGN.items():
                for label, number in ids.items():
                    if number.lstrip("0") in body:
                        leaks.append(f"{url} -> {res.status_code} contains {other} {label} {number}")
    assert checked > 30, "the URL walk found too few endpoints - the scan itself broke"
    assert not leaks, "An HRS-only account received other plants' data:\n  " + "\n  ".join(sorted(set(leaks)))


@pytest.mark.django_db
def test_the_check_would_catch_an_unscoped_ledger():
    """Discrimination, built in rather than by reverting the fix: an
    unscoped account sees the foreign numbers on the same RoDTEP URL, so the
    assertion above genuinely looks at data that is there."""
    _seed()
    client = APIClient(HTTP_HOST="localhost")
    client.force_authenticate(user=make_user(email="all@ravasco.com", role="viewer"))
    body = client.get("/api/imports/rodtep").content.decode()
    assert FOREIGN["vapi"]["import_po"] in body
