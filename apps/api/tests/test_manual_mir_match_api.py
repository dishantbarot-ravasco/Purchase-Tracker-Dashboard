"""
Integration tests for the manual MIR-match endpoints (2026-09-21) -
`mir-candidates` (what the picker lists, and the collision information its
popup is built from) and `mir-match` (setting/clearing the pin), generated
per plant by apps/api/routers/_domestic_base.py's make_mir_candidates() /
make_set_mir_match().

Same conventions as test_hrs_correct_field.py: real HRS rows built with
Model.objects.create(), real APIClient, no mocking.

The pipeline behaviour of a pin (which row it actually wins, what happens to
the line it displaces) is covered separately and more thoroughly in
apps/services/tests/test_manual_mir_match.py - this file covers the HTTP
layer: permissions, plant scoping, validation, and the response shape the
frontend's collision popup depends on.
"""

import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    ManualMirMatch,
    SyncRun,
)


def _make_po(po_number="3000009100", vendor_name="Rubamin Private Limited"):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=po_number, po_number=po_number, vendor_name=vendor_name,
        po_created_date=datetime.date(2026, 4, 25), currency="INR",
    )


def _make_item(po, description="SBR 1502", item_id="1"):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, description=description,
        qty=Decimal("1000"), uom="KG", net_price=Decimal("100.00"), net_value=Decimal("100000.00"),
    )


def _make_mir(mir_no="MIR-A", source_row_ref="1", material_description="SBR 1502",
              party_name="Rubamin Private Limited", po_number_raw=""):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no=mir_no, mir_date=datetime.date(2026, 5, 1),
        party_name=party_name, po_number_raw=po_number_raw,
        material_description=material_description, qty=Decimal("1000"), uom="KG",
        rate=Decimal("100.00"), net=Decimal("100000.00"), taxable_value=Decimal("100000.00"),
        source_row_ref=source_row_ref, is_active=True,
    )


def _editor(email="e@ravasco.com", plants=None):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role="editor", plants=plants or []))
    return client


@pytest.mark.django_db
class TestSetMirMatchPermissions:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        _make_mir()
        self.url = f"/api/purchase-orders/{self.po.po_number}/mir-match"

    def test_viewer_cannot_pin(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        res = client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 403
        assert ManualMirMatch.objects.count() == 0

    def test_editor_scoped_to_another_plant_cannot_pin(self):
        client = _editor(plants=["vapi"])
        res = client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 403
        assert ManualMirMatch.objects.count() == 0

    def test_unauthenticated_is_rejected(self):
        res = APIClient().patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code in (401, 403)
        assert ManualMirMatch.objects.count() == 0


@pytest.mark.django_db
class TestSetMirMatch:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        self.mir = _make_mir()
        self.client = _editor()
        self.url = f"/api/purchase-orders/{self.po.po_number}/mir-match"

    def test_editor_pins_a_line_to_a_mir_number(self):
        res = self.client.patch(
            self.url, {"itemRef": "0", "mirNo": "MIR-A", "reason": "checked the GRN"}, format="json")
        assert res.status_code == 200
        pin = ManualMirMatch.objects.get()
        assert (pin.plant, pin.po_number, pin.item_ref, pin.mir_no) == (
            SyncRun.Plant.HRS, self.po.po_number, "0", "MIR-A")
        assert pin.reason == "checked the GRN"
        assert pin.created_by_email == "e@ravasco.com"
        # Captured for the staleness check - see ManualMirMatch's docstring.
        assert pin.item_description == "SBR 1502"

    def test_pinning_twice_updates_rather_than_duplicating(self):
        _make_mir(mir_no="MIR-B", source_row_ref="2")
        self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-B"}, format="json")
        assert ManualMirMatch.objects.count() == 1
        assert ManualMirMatch.objects.get().mir_no == "MIR-B"

    def test_empty_mir_no_records_a_deliberate_no_match(self):
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": ""}, format="json")
        assert res.status_code == 200
        # A row EXISTS with a blank number - that is the instruction, and is
        # a different thing from having no pin at all.
        assert ManualMirMatch.objects.get().mir_no == ""

    def test_clear_removes_the_pin(self):
        self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        res = self.client.patch(self.url, {"itemRef": "0", "clear": True}, format="json")
        assert res.status_code == 200
        assert res.json()["cleared"] is True
        assert ManualMirMatch.objects.count() == 0

    def _import_pin(self):
        # Same plant, PO number and line position as the domestic line - the
        # collision po_kind exists to keep apart.
        return ManualMirMatch.objects.create(
            plant=SyncRun.Plant.HRS, po_kind=ManualMirMatch.POKind.IMPORT,
            po_number=self.po.po_number, item_ref="0", mir_no="MIR-IMPORT",
        )

    def test_domestic_clear_leaves_an_import_pin_on_the_same_po_number_alone(self):
        import_pin = self._import_pin()
        self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        res = self.client.patch(self.url, {"itemRef": "0", "clear": True}, format="json")
        assert res.status_code == 200
        assert list(ManualMirMatch.objects.values_list("id", flat=True)) == [import_pin.id]

    def test_domestic_pin_never_overwrites_an_import_pin_on_the_same_po_number(self):
        import_pin = self._import_pin()
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 200
        domestic = ManualMirMatch.objects.get(po_kind=ManualMirMatch.POKind.DOMESTIC)
        assert domestic.mir_no == "MIR-A"
        import_pin.refresh_from_db()
        assert import_pin.mir_no == "MIR-IMPORT"

    def test_unknown_mir_number_is_rejected(self):
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": "NOPE"}, format="json")
        assert res.status_code == 400
        assert "NOPE" in res.json()["error"]
        assert ManualMirMatch.objects.count() == 0

    def test_unknown_item_ref_is_rejected(self):
        res = self.client.patch(self.url, {"itemRef": "7", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 404
        assert ManualMirMatch.objects.count() == 0

    def test_unknown_po_is_rejected(self):
        res = self.client.patch(
            "/api/purchase-orders/NOSUCHPO/mir-match", {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 404
        assert ManualMirMatch.objects.count() == 0

    def test_a_retired_po_cannot_be_pinned(self):
        self.po.is_active = False
        self.po.save(update_fields=["is_active"])
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.status_code == 404
        assert ManualMirMatch.objects.count() == 0

    def test_saving_re_runs_matching_so_the_badge_is_live(self):
        res = self.client.patch(self.url, {"itemRef": "0", "mirNo": "MIR-A"}, format="json")
        assert res.json()["manualPinsApplied"] == 1
        self.item.refresh_from_db()
        match = self.item.mir_match
        assert match.mir_entry_id == self.mir.id
        assert match.manually_pinned is True


@pytest.mark.django_db
class TestMirCandidates:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        self.client = _editor()
        self.url = f"/api/purchase-orders/{self.po.po_number}/mir-candidates"

    def test_search_matches_number_party_or_material(self):
        _make_mir(mir_no="MIR-A", source_row_ref="1", material_description="SBR 1502")
        _make_mir(mir_no="MIR-B", source_row_ref="2", material_description="Carbon Black N330",
                  party_name="Phillips Carbon")
        by_material = self.client.get(self.url, {"q": "Carbon Black"}).json()["candidates"]
        assert [c["mirNo"] for c in by_material] == ["MIR-B"]
        by_party = self.client.get(self.url, {"q": "Phillips"}).json()["candidates"]
        assert [c["mirNo"] for c in by_party] == ["MIR-B"]
        by_number = self.client.get(self.url, {"q": "MIR-A"}).json()["candidates"]
        assert [c["mirNo"] for c in by_number] == ["MIR-A"]

    def test_one_entry_per_mir_number_with_a_row_count(self):
        """A pin names a document, so the picker offers documents - two rows
        of one MIR must not read as two separate things to choose between."""
        _make_mir(mir_no="MIR-MULTI", source_row_ref="1", material_description="SBR 1502")
        _make_mir(mir_no="MIR-MULTI", source_row_ref="2", material_description="Carbon Black N330")
        candidates = self.client.get(self.url, {"q": "MIR-MULTI"}).json()["candidates"]
        assert len(candidates) == 1
        assert candidates[0]["rowCount"] == 2
        # Both rows' positions in the MIR Excel sheet, for finding it by hand.
        assert sorted(candidates[0]["sheetRows"]) == [1, 2]

    def test_inactive_mir_rows_are_not_offered(self):
        stale = _make_mir(mir_no="MIR-OLD", source_row_ref="1")
        stale.is_active = False
        stale.save(update_fields=["is_active"])
        assert self.client.get(self.url, {"q": "MIR-OLD"}).json()["candidates"] == []

    def test_claimed_by_names_the_line_currently_holding_the_document(self):
        """This is what the collision popup is built from - without it the
        frontend cannot tell the reader what taking this MIR would cost."""
        mir = _make_mir(mir_no="MIR-A", source_row_ref="1", po_number_raw=self.po.po_number)
        other_po = _make_po("3000009101")
        other_item = _make_item(other_po)
        # Pin the OTHER PO's line to it, so the document is genuinely held.
        _editor(email="e2@ravasco.com").patch(
            f"/api/purchase-orders/{other_po.po_number}/mir-match",
            {"itemRef": "0", "mirNo": "MIR-A"}, format="json")

        candidates = self.client.get(self.url, {"q": "MIR-A"}).json()["candidates"]
        assert len(candidates) == 1
        claimed = candidates[0]["claimedBy"]
        assert len(claimed) == 1
        assert claimed[0]["poNumber"] == other_po.po_number
        assert claimed[0]["itemRef"] == "0"
        assert claimed[0]["manuallyPinned"] is True
        assert claimed[0]["description"] == other_item.description
        assert mir.mir_no == candidates[0]["mirNo"]

    def test_viewer_may_read_candidates(self):
        """Read endpoints are role-open by design (CLAUDE.md) - only the
        write is IsEditor. A viewer looking at why a match is what it is has
        the same right to see the register as anyone else."""
        _make_mir(mir_no="MIR-A", source_row_ref="1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="v2@ravasco.com", role="viewer"))
        res = client.get(self.url, {"q": "MIR-A"})
        assert res.status_code == 200
        assert [c["mirNo"] for c in res.json()["candidates"]] == ["MIR-A"]

    def test_plant_scoping_is_enforced_on_the_read_too(self):
        _make_mir(mir_no="MIR-A", source_row_ref="1")
        client = APIClient()
        client.force_authenticate(
            user=make_user(email="v3@ravasco.com", role="viewer", plants=["vapi"]))
        assert client.get(self.url, {"q": "MIR-A"}).status_code == 403


@pytest.mark.django_db
class TestLineItemRefIsExposed:
    def test_purchase_orders_payload_carries_item_ref_and_pin_state(self):
        """The frontend can only address a line by `itemRef`; if the list
        endpoint stops emitting it the picker silently cannot open."""
        po = _make_po()
        first = _make_item(po, description="SBR 1502", item_id="1")
        second = _make_item(po, description="Carbon Black N330", item_id="2")
        client = _editor()
        client.patch(f"/api/purchase-orders/{po.po_number}/mir-match",
                     {"itemRef": "0", "mirNo": ""}, format="json")

        payload = client.get("/api/purchase-orders").json()
        row = next(p for p in payload["purchaseOrders"] if p["poNumber"] == po.po_number)
        refs = {i["description"]: i["itemRef"] for i in row["items"]}
        # Numbered in pk order, which is the master CSV's own row order.
        assert refs[first.description] == "0"
        assert refs[second.description] == "1"
        assert all("manuallyPinned" in i for i in row["items"])
