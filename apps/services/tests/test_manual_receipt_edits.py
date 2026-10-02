"""
Pipeline tests for receipts added to or removed from one PO line by hand
(apps/core/models/review.py's ManualReceiptEdit, applied by
matching_core.run_full_match()) - the "Edit receipts" panel, 2026-09-29.

Built on the real shape of HRS 3000001167, the order that prompted it: a
6MPA and a 7MPA reclaim rubber line of 50 t each, from one vendor at two
rates. Four 10 t receipts of 6MPA and two of 7MPA cite the order; a third
7MPA receipt (59/09) had its PO typed with an extra zero, 30000001167.

The matcher counts 20 t on 7MPA: 59/09 identifies on vendor and material,
but the line is already settled by the receipts citing its PO number, and
such a line takes nothing more. The old tool, a pin, REPLACED the line's
receipts with 59/09; an added receipt must join them instead.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    ManualMirMatch,
    ManualReceiptEdit,
    PTUser,
    HRSMirStockMatch,
    HRSRMLot,
    SyncRun,
)
from apps.services import receipt_preview
from apps.services.matching import run_full_match

PO = "3000001167"
VENDOR = "Jayam Revive Private Limited"
SIX, SEVEN = "Reclaim Rubber - 6 MPA", "Reclaim Rubber - 7 MPA"


def _order():
    po = HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{PO}", po_number=PO, po_created_date=datetime.date(2026, 9, 7),
        vendor_name=VENDOR, tax_type="IGST",
    )
    six = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="22001840", description=SIX, qty=Decimal("50000"), uom="KG",
        net_price=Decimal("48"), net_value=Decimal("2400000"))
    seven = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="22001840", description=SEVEN, qty=Decimal("50000"), uom="KG",
        net_price=Decimal("50"), net_value=Decimal("2500000"))
    for n, (mir_no, grade, rate, po_raw) in enumerate([
        ("19/09", "6MPA", "48", PO + ".0"), ("40/09", "6MPA", "48", PO + ".0"),
        ("41/09", "6MPA", "48", PO + ".0"), ("65/09", "6MPA", "48", PO + ".0"),
        ("20/09", "7MPA", "50", PO + ".0"), ("66/09", "7MPA", "50", PO + ".0"),
        ("59/09", "7MPA", "50", "30000001167.0"),
    ]):
        HRSMIREntry.objects.create(
            month="Sep-26", mir_no=mir_no, mir_date=datetime.date(2026, 9, 8 + n), party_name=VENDOR,
            po_number_raw=po_raw, material_description=f"Reclaim Rubber {grade}", qty=Decimal("10000"),
            uom="Kgs", rate=Decimal(rate), net=Decimal(rate) * 10000, taxable_value=Decimal(rate) * 10000,
            invoice_final_value=Decimal(rate) * 10000, source_row_ref=str(100 + n), is_active=True,
        )
    return po, six, seven


def _edit(item_ref, mir_no, action, description, *, shared=False, user=None):
    return ManualReceiptEdit.objects.create(
        plant=SyncRun.Plant.HRS, po_number=PO, item_ref=item_ref, item_description=description, mir_no=mir_no,
        action=action, shared=shared, created_by=user, created_by_email=getattr(user, "email", "t@ravasco.com"))


def _counted(item):
    m = HRSPOMirMatch.objects.filter(po_line_item=item).first()
    if m is None:
        return None, []
    return m, sorted(r.mir_no for r in (list(m.group_entries.all()) or [m.mir_entry]))


@pytest.mark.django_db
class TestAddedReceiptJoinsTheLine:
    def test_automatically_the_mistyped_receipt_is_on_no_line(self):
        """The starting point, so the tests below measure a real change."""
        _po, six, seven = _order()
        run_full_match()
        assert _counted(six)[1] == ["19/09", "40/09", "41/09", "65/09"]
        assert _counted(seven)[1] == ["20/09", "66/09"]

    def test_adding_59_09_keeps_the_line_s_other_receipts(self):
        _po, six, seven = _order()
        user = PTUser.objects.create(email="d@ravasco.com", password_hash="x", role="user",
                                     full_name="Dishant Barot")
        _edit("1", "59/09", "add", SEVEN, user=user)
        result = run_full_match()
        m7, rows7 = _counted(seven)
        assert rows7 == ["20/09", "59/09", "66/09"]
        assert m7.qty_diff_pct == Decimal("40.00") and m7.qty_over_delivered is False
        assert _counted(six)[1] == ["19/09", "40/09", "41/09", "65/09"], "the 6MPA line must not move"
        # Who put it there, for the modal - and only on the receipt added.
        assert m7.receipt_notes == [{
            "mirNo": "59/09", "how": "added", "by": "d@ravasco.com", "byName": "Dishant Barot",
            "at": m7.receipt_notes[0]["at"], "reason": "",
        }]
        assert m7.manually_pinned is False
        assert result["manual_edits_unfilled"] == [] and result["manual_edits_stale"] == []

    def test_a_pin_to_the_same_receipt_is_what_the_add_replaced(self):
        """Contrast, so the test above cannot pass by accident: a pin names
        the line's ONE receipt, and its settlement is its own."""
        _po, _six, seven = _order()
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="59/09",
                                      item_description=SEVEN, created_by_email="t@ravasco.com")
        run_full_match()
        m7, _rows = _counted(seven)
        assert m7.manually_pinned is True and m7.mir_entry.mir_no == "59/09"
        assert m7.receipt_notes[0]["how"] == "pinned"

    def test_adding_a_receipt_another_line_holds_moves_it(self):
        _po, six, seven = _order()
        _edit("1", "65/09", "add", SEVEN)
        run_full_match()
        assert "65/09" in _counted(seven)[1]
        assert "65/09" not in _counted(six)[1]

    def test_keep_both_leaves_it_on_the_other_line_too(self):
        _po, six, seven = _order()
        _edit("1", "65/09", "add", SEVEN, shared=True)
        run_full_match()
        assert "65/09" in _counted(seven)[1]
        assert "65/09" in _counted(six)[1]

    def test_a_receipt_a_pin_already_took_is_reported_unfilled(self):
        _po, _six, seven = _order()
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="0", mir_no="59/09",
                                      item_description=SIX, created_by_email="t@ravasco.com")
        _edit("1", "59/09", "add", SEVEN)
        result = run_full_match()
        assert "59/09" not in _counted(seven)[1]
        assert result["manual_edits_unfilled"] == [
            {"poNumber": PO, "itemRef": "1", "mirNo": "59/09", "poKind": "domestic"}]

    def test_an_edit_whose_line_changed_is_ignored_and_reported(self):
        _po, _six, seven = _order()
        _edit("1", "59/09", "add", "Some other material")
        result = run_full_match()
        assert "59/09" not in _counted(seven)[1]
        assert [e["mirNo"] for e in result["manual_edits_stale"]] == ["59/09"]

    def test_a_line_with_nothing_automatic_takes_the_added_receipt_alone(self):
        _po, six, seven = _order()
        HRSMIREntry.objects.exclude(mir_no="59/09").update(is_active=False)
        _edit("0", "59/09", "add", SIX)
        run_full_match()
        m6, rows6 = _counted(six)
        assert rows6 == ["59/09"] and m6.mir_entry.mir_no == "59/09"
        assert _counted(seven)[1] == [], "claimed for 6MPA, so 7MPA cannot also take it"


@pytest.mark.django_db
class TestRemovedReceipt:
    def test_a_removed_receipt_goes_to_the_line_it_fits_next_with_a_note(self):
        _po, six, seven = _order()
        _edit("1", "66/09", "remove", SEVEN)
        run_full_match()
        assert _counted(seven)[1] == ["20/09"]
        m6, rows6 = _counted(six)
        assert "66/09" in rows6
        # The 6MPA reader did nothing - the note says who moved it and from where.
        note = next(n for n in m6.receipt_notes if n["mirNo"] == "66/09")
        assert (note["how"], note["fromPoNumber"], note["fromLine"], note["fromKind"]) == ("moved", PO, 2, "domestic")

    def test_a_removed_receipt_is_never_counted_on_that_line(self):
        """A removal promises only that: the line may still be given some
        other receipt citing the order (here a 6MPA one, by the one-row-per-
        line step), which the reader can remove too or answer with "not
        received"."""
        _po, _six, seven = _order()
        _edit("1", "20/09", "remove", SEVEN)
        _edit("1", "66/09", "remove", SEVEN)
        run_full_match()
        assert not {"20/09", "66/09"} & set(_counted(seven)[1])

    def test_not_received_still_leaves_the_line_unmatched(self):
        _po, _six, seven = _order()
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_number=PO, item_ref="1", mir_no="",
                                      item_description=SEVEN, created_by_email="t@ravasco.com")
        run_full_match()
        assert _counted(seven)[1] == []


@pytest.mark.django_db
class TestPreview:
    def test_preview_reports_the_effect_and_saves_nothing(self):
        _po, six, seven = _order()
        run_full_match()
        before = sorted(HRSPOMirMatch.objects.values_list("po_line_item_id", "mir_entry_id"))
        result = receipt_preview.compute("hrs", dict(
            plant=SyncRun.Plant.HRS, po_kind="domestic", po_number=PO, item_ref="1", item_description=SEVEN,
            action="remove", mir_no="66/09", shared=False, reason="", undo_type="", undo_id=None))
        by_ref = {line["itemRef"]: line for line in result["lines"]}
        assert by_ref["1"]["editedLine"] and by_ref["1"]["before"]["receipts"] == ["20/09", "66/09"]
        assert by_ref["1"]["after"]["receipts"] == ["20/09"]
        assert by_ref["1"]["after"]["received"] == 10000.0 and by_ref["1"]["after"]["diffPct"] == -80.0
        assert by_ref["0"]["changed"] and "66/09" in by_ref["0"]["after"]["receipts"]
        # Rolled back: no edit, and the match tables as they were.
        assert ManualReceiptEdit.objects.count() == 0
        assert sorted(HRSPOMirMatch.objects.values_list("po_line_item_id", "mir_entry_id")) == before

    def test_a_dry_run_leaves_mir_stock_matches_alone(self):
        """dry_run skips the MIR<->Stock pass - measured by a stale row the
        full pass would delete."""
        _order()
        # A lot nothing like the receipt: the full pass would drop this pair.
        lot = HRSRMLot.objects.create(description="Carbon Black N330", natural_key="k1", is_active=True)
        HRSMirStockMatch.objects.create(mir_entry=HRSMIREntry.objects.get(mir_no="19/09"), stock_lot=lot,
                                        dismissed_reason="")
        run_full_match(dry_run=True)
        assert HRSMirStockMatch.objects.count() == 1
        run_full_match()
        assert HRSMirStockMatch.objects.count() == 0
