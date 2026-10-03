"""
Import phase 3 (owner, 2026-10-03): Advance Authorisation and RoDTEP debits
read from a Bill of Entry's licence section, and the balances they give.
Amounts come only from approved BOE readings - never from the import CSV or
the Advance License sheet's usage columns.
"""

import copy
import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.api.tests.test_po_extraction import _fake_read, _read, configured  # noqa: F401
from apps.core.models import AdvanceLicense, AdvanceLicenseMaterial, LicenceDebit, RodtepScrollEntry
from apps.services import licences, po_extraction
from apps.services.tests.test_import_shipments import BOE_READ, _boe_document, _setup

DEBITS = [
    {"item": "1", "license_type": "ADVANCE", "license_number": "311051817", "qty": "100800", "value": "9800000", "duty": ""},
    {"item": "1", "license_type": "RODTEP", "license_number": "2609004872", "qty": "", "value": "", "duty": "200000"},
]


def _ledgers(import_validity=None, cif="10000000"):
    lic = AdvanceLicense.objects.create(license_number="0311051817", cif_value_authorized=Decimal(cif),
                                        import_validity_date=import_validity or datetime.date(2027, 3, 31))
    AdvanceLicenseMaterial.objects.create(license=lic, material_description="SBR 1502", itchs_code="40021990",
                                          qty_authorized=Decimal("300000"))
    RodtepScrollEntry.objects.create(script_no="2609004872", sb_number="SB1", sanctioned_amount=Decimal("500000"))
    return lic


@pytest.fixture
def clerk():
    return make_user(email="imports@ravasco.com", role="editor", plants=["vapi"])


def _approve(monkeypatch, capture, clerk, debits=DEBITS, **change):
    _fake_read(monkeypatch, {**copy.deepcopy(BOE_READ), "debits": copy.deepcopy(debits), **change})
    ext = _read(capture, doc=_boe_document(reference=change.get("boe_number", "4026152")), user=clerk)
    return ext


@pytest.mark.django_db
class TestDebits:
    def test_an_approved_boe_records_its_debits_and_the_balances_follow(self, configured, monkeypatch,  # noqa: F811
                                                                        django_capture_on_commit_callbacks, clerk):
        _setup()
        _ledgers()
        ext = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk)
        assert po_extraction.review(ext)["checks"] == []
        po_extraction.approve(ext, None, clerk)
        rows = list(LicenceDebit.objects.order_by("license_type"))
        assert [(d.license_type, d.license_number, d.value_inr, d.duty_foregone) for d in rows] == [
            ("ADVANCE", "0311051817", Decimal("9800000.00"), None), ("RODTEP", "2609004872", None, Decimal("200000.00"))]
        assert licences.debited("ADVANCE", "311051817")["value"] == Decimal("9800000")
        c = APIClient()
        c.force_authenticate(user=clerk)
        adv = c.get("/api/imports/advance-license").json()["licenses"][0]["boeDebits"]
        assert Decimal(str(adv["cifLeft"])) == Decimal("200000") and adv["debits"][0]["boeNumber"] == "4026152"
        rod = c.get("/api/imports/rodtep").json()
        assert rod["summary"]["hasBoeDebits"] is True
        assert Decimal(str(rod["scripts"][0]["boeDebits"]["left"])) == Decimal("300000")

    def test_a_reapproved_boe_replaces_its_debits_never_adds_them(self, configured, monkeypatch,  # noqa: F811
                                                                  django_capture_on_commit_callbacks, clerk):
        _setup()
        _ledgers()
        po_extraction.approve(_approve(monkeypatch, django_capture_on_commit_callbacks, clerk), None, clerk)
        again = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk, debits=DEBITS[:1])
        po_extraction.approve(again, None, clerk)
        assert licences.debited("ADVANCE", "0311051817")["value"] == Decimal("9800000")
        assert licences.debited("RODTEP", "2609004872")["duty"] == Decimal("0")
        assert LicenceDebit.objects.count() == 3 and LicenceDebit.objects.filter(is_active=True).count() == 1

    @pytest.mark.parametrize("setup, debits, check", [
        ({}, [{**DEBITS[0], "license_number": "0399999999"}], "licence_unknown"),
        ({"cif": "5000000"}, DEBITS[:1], "licence_exceeded"),
        ({"import_validity": datetime.date(2026, 9, 1)}, DEBITS[:1], "licence_expired"),
        ({}, [{**DEBITS[1], "duty": "600000"}], "licence_exceeded"),
    ])
    def test_the_reviewer_is_warned_before_approving(self, configured, monkeypatch,  # noqa: F811
                                                     django_capture_on_commit_callbacks, clerk, setup, debits, check):
        _setup()
        _ledgers(**setup)
        ext = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk, debits=debits)
        assert check in {c["check"] for c in po_extraction.review(ext)["checks"]}
        # A warning, not a block.
        assert po_extraction.review(ext)["problems"] == []

    def test_an_item_outside_the_licences_materials_is_flagged(self, configured, monkeypatch,  # noqa: F811
                                                               django_capture_on_commit_callbacks, clerk):
        _setup()
        _ledgers()
        read_lines = copy.deepcopy(BOE_READ["lines"])
        read_lines[0]["hsn"] = "38123990"
        ext = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk, debits=DEBITS[:1], lines=read_lines)
        assert "licence_item" in {c["check"] for c in po_extraction.review(ext)["checks"]}

    @pytest.mark.parametrize("debit, field", [
        ({**DEBITS[0], "value": ""}, "debits.value"),
        ({**DEBITS[1], "duty": ""}, "debits.duty"),
        ({**DEBITS[0], "item": "4"}, "debits.item"),
        ({**DEBITS[0], "license_number": ""}, "debits.license_number"),
    ])
    def test_an_incomplete_debit_blocks_approval(self, configured, monkeypatch,  # noqa: F811
                                                 django_capture_on_commit_callbacks, clerk, debit, field):
        _setup()
        _ledgers()
        ext = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk, debits=[debit])
        assert field in {p["field"] for p in po_extraction.review(ext)["problems"]}

    def test_a_licence_named_without_amounts_is_pointed_out(self, configured, monkeypatch,  # noqa: F811
                                                            django_capture_on_commit_callbacks, clerk):
        _setup()
        _ledgers()
        ext = _approve(monkeypatch, django_capture_on_commit_callbacks, clerk, debits=[])
        assert "licence_amount" in {c["check"] for c in po_extraction.review(ext)["checks"]}
