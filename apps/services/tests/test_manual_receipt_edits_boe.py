"""
Receipts added to or removed from an IMPORT line by hand (ManualReceiptEdit)
must hold when the receipt is booked under the line's own Bill of Entry.

BOE settlement (matching_core._boe_settlement()) builds its own pool from the
MIR table, and it never saw the edits: a removed receipt came straight back
on its next re-match, and an added one was skipped in favour of settlement,
which then rejected it (no vendor or material vote) or handed it to a sibling
line - with nothing reported unfilled.
"""

from decimal import Decimal

import pytest

from apps.core.models import HRSImportPOMirMatch, ManualMirMatch, ManualReceiptEdit, SyncRun
from apps.services.matching import run_full_match
from apps.services.tests.test_run_full_match_import_pipeline import _boe_line, _make_import_po, _receipt

BOE = "8826527"


def _edit(po, item_ref, mir_no, action, description="PTFE Coated Fabric"):
    return ManualReceiptEdit.objects.create(
        plant=SyncRun.Plant.HRS, po_kind="import", po_number=po.po_number, item_ref=item_ref,
        item_description=description, mir_no=mir_no, action=action, created_by_email="t@ravasco.com")


def _counted(line):
    match = HRSImportPOMirMatch.objects.filter(po_line_item=line).first()
    if match is None:
        return []
    return sorted(r.mir_no for r in (list(match.group_entries.all()) or [match.mir_entry]))


@pytest.mark.django_db
class TestRemovedBoeReceipt:
    def test_automatically_the_boe_receipt_is_the_line_s(self):
        po = _make_import_po()
        line = _boe_line(po, boe=BOE)
        _receipt(invoice_no=BOE, mir_no="MIR-BOE")
        run_full_match()
        assert _counted(line) == ["MIR-BOE"]

    def test_a_removed_boe_receipt_stays_off_the_line(self):
        po = _make_import_po()
        line = _boe_line(po, boe=BOE)
        _receipt(invoice_no=BOE, mir_no="MIR-BOE")
        _edit(po, "0", "MIR-BOE", "remove")

        run_full_match()

        assert "MIR-BOE" not in _counted(line)


@pytest.mark.django_db
class TestAddedBoeReceipt:
    def test_an_added_receipt_with_no_vote_is_counted(self):
        """Neither the party name nor the material agrees with the PO -
        exactly when a person adds a receipt by hand. Settlement used to
        reject it for lacking a vote and the add vanished, unreported."""
        po = _make_import_po()
        line = _boe_line(po, boe=BOE)
        _receipt(invoice_no=BOE, mir_no="MIR-ODD", party="Unrelated Traders", material="Misc Goods")
        run_full_match()
        assert _counted(line) == []

        _edit(po, "0", "MIR-ODD", "add")
        result = run_full_match()

        assert _counted(line) == ["MIR-ODD"]
        assert result["manual_edits_unfilled"] == []

    def test_an_added_receipt_goes_to_its_line_not_a_sibling(self):
        """Two lines on one BOE, a receipt sized for each. Added to the other
        line, the receipt must land there - the one-per-line assignment used
        to hand it back to the line it fits best."""
        po = _make_import_po()
        big = _boe_line(po, boe=BOE, qty=Decimal("100"), bl="BL1", item_id="1")
        small = _boe_line(po, boe=BOE, qty=Decimal("50"), bl="BL1", item_id="2")
        _receipt(invoice_no=BOE, qty=Decimal("100"), ref="7", mir_no="MIR-100")
        _receipt(invoice_no=BOE, qty=Decimal("50"), ref="8", mir_no="MIR-50")
        run_full_match()
        assert (_counted(big), _counted(small)) == (["MIR-100"], ["MIR-50"])

        _edit(po, "0", "MIR-50", "add")
        result = run_full_match()

        assert "MIR-50" in _counted(big)
        assert "MIR-50" not in _counted(small)
        assert result["manual_edits_unfilled"] == []

    def test_an_add_settlement_cannot_place_is_reported_unfilled(self):
        """Lines on different Bills of Lading whose receipt does not meet them
        exactly: settlement leaves the BOE alone, so the add falls back to an
        ordinary claim - and when a pin on the other line already took the
        only row, it is reported unfilled rather than dropped."""
        po = _make_import_po()
        one = _boe_line(po, boe=BOE, qty=Decimal("100"), bl="BL1", item_id="1")
        _boe_line(po, boe=BOE, qty=Decimal("100"), bl="BL2", item_id="2")
        _receipt(invoice_no=BOE, qty=Decimal("70"), mir_no="MIR-BOE")
        ManualMirMatch.objects.create(plant=SyncRun.Plant.HRS, po_kind="import", po_number=po.po_number, item_ref="1",
                                      mir_no="MIR-BOE", item_description="PTFE Coated Fabric",
                                      created_by_email="t@ravasco.com")
        _edit(po, "0", "MIR-BOE", "add")

        result = run_full_match()

        assert "MIR-BOE" not in _counted(one)
        assert result["manual_edits_unfilled"] == [
            {"poNumber": po.po_number, "itemRef": "0", "mirNo": "MIR-BOE", "poKind": "import"}]
