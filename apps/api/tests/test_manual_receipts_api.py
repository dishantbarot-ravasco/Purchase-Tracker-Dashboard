"""
HTTP tests for the "Edit receipts" panel (2026-09-29): the mir-match PATCH's
actions (add / remove / notReceived / auto / undo), the manual-changes
listing, the preview, and the candidates' grouping and reasons.

The pipeline effect of an added or removed receipt is covered in
apps/services/tests/test_manual_receipt_edits.py; this file covers what the
endpoints write, who may call them, and the shapes the panel reads.
"""

import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    ManualMirMatch,
    ManualReceiptEdit,
    SyncRun,
)

PO = "3000001167"
VENDOR = "Jayam Revive Private Limited"


def _order():
    po = HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=PO, po_number=PO, vendor_name=VENDOR, po_created_date=datetime.date(2026, 9, 7))
    for desc, rate in (("Reclaim Rubber - 6 MPA", "48"), ("Reclaim Rubber - 7 MPA", "50")):
        HRSDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="22001840", description=desc, qty=Decimal("50000"), uom="KG",
            net_price=Decimal(rate), net_value=Decimal(rate) * 50000)
    for n, (mir_no, grade, rate, po_raw, party) in enumerate([
        ("19/09", "6MPA", "48", PO + ".0", VENDOR),
        ("20/09", "7MPA", "50", PO + ".0", VENDOR),
        ("59/09", "7MPA", "50", "30000001167.0", VENDOR),
        ("77/09", "Sulphur Powder", "75", "", "Someone Else Ltd"),
    ]):
        HRSMIREntry.objects.create(
            month="Sep-26", mir_no=mir_no, mir_date=datetime.date(2026, 9, 8 + n), party_name=party,
            po_number_raw=po_raw, material_description=f"Reclaim Rubber {grade}" if "MPA" in grade else grade,
            qty=Decimal("10000"), uom="Kgs", rate=Decimal(rate), net=Decimal(rate) * 10000,
            taxable_value=Decimal(rate) * 10000, source_row_ref=str(10 + n), is_active=True)
    return po


def _editor(email="d@ravasco.com", plants=None, full_name="Dishant Barot"):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role="editor", plants=plants or [], full_name=full_name))
    return client


URL = f"/api/purchase-orders/{PO}/mir-match"


@pytest.mark.django_db
class TestActions:
    def setup_method(self):
        _order()
        self.client = _editor()

    def patch(self, body):
        return self.client.patch(URL, {"itemRef": "1", **body}, format="json")

    def test_add_writes_an_edit_and_the_rematch_counts_it(self):
        res = self.patch({"action": "add", "mirNo": "59/09", "reason": "typo in PO column"})
        assert res.status_code == 200, res.data
        edit = ManualReceiptEdit.objects.get()
        assert (edit.action, edit.mir_no, edit.item_ref, edit.po_kind, edit.reason) == (
            "add", "59/09", "1", "domestic", "typo in PO column")
        assert edit.item_description == "Reclaim Rubber - 7 MPA"
        assert edit.created_by_email == "d@ravasco.com"
        assert res.data["action"] == "add" and res.data["unfilledEdits"] == []
        # The re-match ran inline under pytest: the receipt carries its note.
        items = self.client.get("/api/purchase-orders").data
        po = next(p for p in (items["purchaseOrders"] if isinstance(items, dict) else items) if p["poNumber"] == PO)
        mirs = po["items"][1]["matchedMirs"]
        note = next(m["manualNote"] for m in mirs if m["mirNo"] == "59/09")
        assert (note["how"], note["byName"]) == ("added", "Dishant Barot")

    def test_add_clears_a_not_received_pin_and_a_removal_of_the_same_receipt(self):
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="")
        ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="59/09",
                                         action="remove")
        assert self.patch({"action": "add", "mirNo": "59/09"}).status_code == 200
        assert not ManualMirMatch.objects.exists()
        assert list(ManualReceiptEdit.objects.values_list("action", flat=True)) == ["add"]

    def test_a_failed_add_leaves_the_line_s_decisions_as_they_were(self, monkeypatch):
        from apps.services import manual_receipts

        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="")

        def broken(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr(manual_receipts, "_save_edit", broken)
        with pytest.raises(RuntimeError):
            manual_receipts.apply_change(plant=SyncRun.Plant.HRS, po_kind="domestic", po_number=PO, item_ref="1",
                                         item_description="Reclaim Rubber - 7 MPA", action="add",
                                         mir_model=HRSMIREntry, mir_no="59/09")
        assert ManualMirMatch.objects.filter(mir_no="").exists()

    def test_remove_drops_a_pin_naming_that_receipt(self):
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="20/09")
        assert self.patch({"action": "remove", "mirNo": "20/09"}).status_code == 200
        assert not ManualMirMatch.objects.exists()
        assert ManualReceiptEdit.objects.get().action == "remove"

    def test_not_received_drops_the_line_s_added_receipts(self):
        ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="59/09",
                                         action="add")
        assert self.patch({"action": "notReceived"}).status_code == 200
        assert ManualMirMatch.objects.get().mir_no == ""
        assert not ManualReceiptEdit.objects.exists()

    def test_auto_clears_everything_on_the_line_and_nothing_else(self):
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="20/09")
        ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="59/09",
                                         action="add")
        other = ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="0",
                                                 mir_no="19/09", action="remove")
        assert self.patch({"action": "auto"}).status_code == 200
        assert not ManualMirMatch.objects.exists()
        assert list(ManualReceiptEdit.objects.values_list("pk", flat=True)) == [other.pk]

    def test_undo_deletes_exactly_that_change(self):
        edit = ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1",
                                                mir_no="59/09", action="add")
        assert self.patch({"action": "undo", "undoType": "edit", "undoId": edit.pk}).status_code == 200
        assert not ManualReceiptEdit.objects.exists()
        again = self.patch({"action": "undo", "undoType": "edit", "undoId": edit.pk})
        assert again.status_code == 400 and "already" in again.data["error"]

    def test_undo_cannot_reach_another_line_s_change(self):
        edit = ManualReceiptEdit.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="0",
                                                mir_no="19/09", action="remove")
        assert self.patch({"action": "undo", "undoType": "edit", "undoId": edit.pk}).status_code == 400
        assert ManualReceiptEdit.objects.filter(pk=edit.pk).exists()

    def test_unknown_mir_and_unknown_action_are_rejected(self):
        assert self.patch({"action": "add", "mirNo": "NOPE"}).status_code == 400
        assert self.patch({"action": "explode", "mirNo": "59/09"}).status_code == 400
        assert not ManualReceiptEdit.objects.exists()

    def test_the_older_body_still_pins(self):
        assert self.patch({"mirNo": "20/09"}).status_code == 200
        assert ManualMirMatch.objects.get().mir_no == "20/09"
        assert self.patch({"clear": True}).status_code == 200
        assert not ManualMirMatch.objects.exists()

    def test_viewer_and_out_of_scope_editor_cannot_change_or_preview(self):
        viewer = APIClient()
        viewer.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        scoped = _editor(email="s@ravasco.com", plants=["vapi"])
        for client in (viewer, scoped):
            assert client.patch(URL, {"itemRef": "1", "action": "add", "mirNo": "59/09"}, format="json").status_code == 403
            assert client.post(URL + "/preview", {"itemRef": "1", "action": "add", "mirNo": "59/09"},
                               format="json").status_code == 403
        assert not ManualReceiptEdit.objects.exists()


@pytest.mark.django_db
class TestManualChangesListing:
    def test_lists_the_order_s_changes_and_receipts_placed_elsewhere(self):
        _order()
        client = _editor()
        client.patch(URL, {"itemRef": "1", "action": "add", "mirNo": "59/09"}, format="json")
        other = HRSDomesticPurchaseOrder.objects.create(po_drive_folder_name="X", po_number="3000009999",
                                                        vendor_name=VENDOR)
        HRSDomesticPOLineItem.objects.create(purchase_order=other, description="Reclaim Rubber 6MPA",
                                             qty=Decimal("1"), uom="KG", net_price=Decimal("1"))
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number="3000009999", item_ref="0", mir_no="19/09",
                                      item_description="Reclaim Rubber 6MPA", created_by_email="x@ravasco.com")
        res = client.get(f"/api/purchase-orders/{PO}/manual-changes")
        assert res.status_code == 200
        (line,) = res.data["lines"]
        assert (line["itemRef"], line["line"], line["description"]) == ("1", 2, "Reclaim Rubber - 7 MPA")
        (change,) = line["changes"]
        assert (change["type"], change["action"], change["mirNo"], change["byName"], change["stale"]) == (
            "edit", "added", "59/09", "Dishant Barot", False)
        # 19/09 cites this order but a person put it on another one.
        (placed,) = res.data["elsewhere"]
        assert (placed["poNumber"], placed["mirNo"], placed["action"]) == ("3000009999", "19/09", "pinned")

    def test_a_viewer_may_read_it_and_another_plant_s_editor_may_not(self):
        _order()
        viewer = APIClient()
        viewer.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        assert viewer.get(f"/api/purchase-orders/{PO}/manual-changes").status_code == 200
        scoped = _editor(email="s@ravasco.com", plants=["vapi"])
        assert scoped.get(f"/api/purchase-orders/{PO}/manual-changes").status_code == 403


@pytest.mark.django_db
class TestPreviewEndpoints:
    def test_preview_runs_and_its_status_reports_the_lines(self):
        _order()
        client = _editor()
        res = client.post(URL + "/preview", {"itemRef": "1", "action": "add", "mirNo": "59/09"}, format="json")
        assert res.status_code == 200, res.data
        assert res.data["state"] == "done"
        status = client.get(f"/api/mir-match-previews/{res.data['previewId']}")
        assert status.status_code == 200
        edited = next(line for line in status.data["lines"] if line["editedLine"])
        assert "59/09" in edited["after"]["receipts"] and "59/09" not in edited["before"]["receipts"]
        assert not ManualReceiptEdit.objects.exists()

    def test_another_plant_s_preview_id_is_not_found(self):
        _order()
        client = _editor()
        res = client.post(URL + "/preview", {"itemRef": "1", "action": "remove", "mirNo": "20/09"}, format="json")
        assert client.get(f"/api/vapi/mir-match-previews/{res.data['previewId']}").status_code == 404
        assert client.get("/api/mir-match-previews/not-a-real-id").status_code == 404

    def test_a_change_that_cannot_be_made_is_reported_as_failed(self):
        _order()
        client = _editor()
        res = client.post(URL + "/preview", {"itemRef": "1", "action": "add", "mirNo": "NOPE"}, format="json")
        assert res.data["state"] == "failed" and "NOPE" in res.data["error"]


@pytest.mark.django_db
class TestCandidates:
    def test_grouped_most_likely_first_with_the_reasons(self):
        from apps.services.matching import run_full_match
        _order()
        run_full_match()
        client = _editor()
        res = client.get(f"/api/purchase-orders/{PO}/mir-candidates?itemRef=1")
        assert res.status_code == 200
        by_no = {c["mirNo"]: c for c in res.data["candidates"]}
        order = [c["group"] for c in res.data["candidates"]]
        assert order == sorted(order, key=["cites", "vendorMaterial", "vendor", "other"].index)
        assert by_no["20/09"]["group"] == "cites" and by_no["20/09"]["onThisLine"] is True
        assert by_no["20/09"]["why"] == []
        typo = by_no["59/09"]
        assert typo["group"] == "vendorMaterial" and typo["onThisLine"] is False
        assert any("30000001167" in w and "typo" in w for w in typo["why"])
        assert any("already counts the receipts that cite its PO number" in w for w in typo["why"])
        assert by_no["77/09"]["group"] == "other"
        assert any("not this PO's vendor" in w for w in by_no["77/09"]["why"])

    def test_without_an_item_ref_the_list_is_unannotated(self):
        _order()
        res = _editor().get(f"/api/purchase-orders/{PO}/mir-candidates")
        assert res.status_code == 200 and all("group" not in c for c in res.data["candidates"])


@pytest.mark.django_db
class TestImportActions:
    def test_an_import_line_takes_an_added_receipt_under_its_own_kind(self):
        po = HRSImportPurchaseOrder.objects.create(
            po_drive_folder_name="PO_IMP1", po_number="IMP1", po_created_date=datetime.date(2026, 4, 25),
            vendor_name="Global Polymers Inc", currency="USD", total_value=Decimal("234.00"))
        HRSImportPOLineItem.objects.create(
            purchase_order=po, item_id="1", description="PTFE Coated Fabric", hsn="5903",
            qty_as_per_po=Decimal("100"), qty_as_per_boe=Decimal("100"), uom="KG", net_price=Decimal("1.95"),
            net_value=Decimal("195.00"), tax_type="IGST", currency_after_taxes="INR",
            exchange_rate=Decimal("93.80"), total_inclusive_value=Decimal("18291.00"))
        HRSMIREntry.objects.create(
            month="May-26", mir_no="MIR-I", mir_date=datetime.date(2026, 5, 1), party_name="Someone",
            material_description="Something else", qty=Decimal("100"), uom="KG", rate=Decimal("182.91"),
            net=Decimal("18291.00"), taxable_value=Decimal("18291.00"), source_row_ref="1", is_active=True)
        client = _editor()
        base = "/api/imports/purchase-orders/hrs/IMP1"
        res = client.patch(base + "/mir-match", {"itemRef": "0", "action": "add", "mirNo": "MIR-I"}, format="json")
        assert res.status_code == 200, res.data
        assert ManualReceiptEdit.objects.get().po_kind == ManualMirMatch.POKind.IMPORT
        listing = client.get(base + "/manual-changes")
        assert listing.data["lines"][0]["changes"][0]["mirNo"] == "MIR-I"
        preview = client.post(base + "/mir-match/preview", {"itemRef": "0", "action": "remove", "mirNo": "MIR-I"},
                              format="json")
        assert preview.status_code == 200 and preview.data["state"] == "done"
        assert client.get(f"/api/imports/mir-match-previews/hrs/{preview.data['previewId']}").status_code == 200
        assert client.get(f"/api/imports/mir-match-previews/vapi/{preview.data['previewId']}").status_code == 404
