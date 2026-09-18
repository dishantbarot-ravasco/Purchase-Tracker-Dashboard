"""
Tests for retiring purchase orders the master CSV no longer lists
(sync_utils.deactivate_missing_orders(), 2026-09-18).

The bug these exist to prevent coming back: purchase orders were the only
entity in this pipeline without an is_active flag. MIR entries and stock lots
have always had one and their syncs flip it off for vanished rows; the PO sync
upserted on po_number as a natural key and never removed anything. So renaming
a PO upstream - which happens every time an annotation like
"3000001104 (Changed Purchase Order)" is added or cleaned off - left the old
spelling behind forever as a second order with duplicate line items, still
competing for the same MIR rows. Measured 2026-09-12: 6 ghosts for HRS, 5 for
Achhad, 11 for Vapi, every one a rename rather than a real deletion.

The rename case is the one worth reading first:
test_a_renamed_po_retires_the_old_spelling_and_keeps_the_new.
"""

import datetime

import pytest

from apps.core.models import (
    RTPAchhadDomesticPOLineItem,
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadMIREntry,
    RTPAchhadPOMirMatch,
)
from apps.services.sync_utils import deactivate_missing_orders


class _ParsedOrder:
    """The one attribute deactivate_missing_orders() reads off a parsed
    order. Kept deliberately minimal - a real ParsedPurchaseOrder carries
    ~18 fields, none of which this function has any business touching."""

    def __init__(self, po_number):
        self.po_number = po_number


def _order(po_number, *, is_active=True):
    return RTPAchhadDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=po_number, po_number=po_number,
        vendor_name="Ganesh Chemicals Ltd", is_active=is_active)


@pytest.mark.django_db
class TestDeactivateMissingOrders:
    def test_a_renamed_po_retires_the_old_spelling_and_keeps_the_new(self):
        """The exact reported symptom: the project owner cleaned
        "(Changed Purchase Order)" off every PO number, and the dashboard went
        on showing the old spelling - because the sync created the clean
        number as a NEW row and had no mechanism to retire the old one."""
        _order("3000001104 (Changed Purchase Order)")
        _order("3000001104")

        retired = deactivate_missing_orders(
            RTPAchhadDomesticPurchaseOrder, [_ParsedOrder("3000001104")])

        assert retired == ["3000001104 (Changed Purchase Order)"]
        assert RTPAchhadDomesticPurchaseOrder.objects.get(po_number="3000001104").is_active is True
        assert RTPAchhadDomesticPurchaseOrder.objects.get(
            po_number="3000001104 (Changed Purchase Order)").is_active is False

    def test_orders_still_in_the_csv_are_untouched(self):
        _order("1100000901")
        _order("1100000902")

        retired = deactivate_missing_orders(
            RTPAchhadDomesticPurchaseOrder,
            [_ParsedOrder("1100000901"), _ParsedOrder("1100000902")])

        assert retired == []
        assert RTPAchhadDomesticPurchaseOrder.objects.filter(is_active=True).count() == 2

    def test_nothing_is_deleted_only_deactivated(self):
        """Deactivating rather than deleting is the whole reason this can run
        automatically: an order withdrawn upstream and one merely renamed are
        indistinguishable from here, and a delete would be irreversible. The
        row and its line items stay put."""
        po = _order("1100000901")
        RTPAchhadDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="1", description="Stearic Acid", qty="500", net_price="80")

        deactivate_missing_orders(RTPAchhadDomesticPurchaseOrder, [])

        assert RTPAchhadDomesticPurchaseOrder.objects.filter(po_number="1100000901").exists()
        assert RTPAchhadDomesticPOLineItem.objects.filter(purchase_order=po).count() == 1

    def test_an_already_retired_order_is_not_reported_again(self):
        """What the sync reports has to mean "retired by THIS run". An
        earlier version listed everything absent from the CSV instead, which
        named the same orders on every subsequent sync forever and described
        them as still matching against MIR when they had just been retired."""
        _order("1100000901", is_active=False)

        assert deactivate_missing_orders(RTPAchhadDomesticPurchaseOrder, []) == []

    def test_an_order_that_comes_back_is_reactivated_by_the_sync(self):
        """Renames get reverted, and a withdrawn order can be reinstated
        upstream. _upsert_order() writes is_active=True, and its hash-skip is
        guarded on is_active for exactly this case - an order that returns
        UNCHANGED must not be skipped while still deactivated."""
        from apps.core.management.commands.sync_achhad_po_csv import Command

        _order("1100000901", is_active=False)
        parsed = type("P", (), {
            "po_number": "1100000901", "po_drive_folder_name": "PO_1100000901",
            "po_created_date": datetime.date(2026, 4, 1), "vendor_name": "Ganesh Chemicals Ltd",
            "vendor_address": "", "vendor_gstin": "", "vendor_email": "", "vendor_code": "",
            "billing_address": "", "ship_to": "", "payment_terms": "", "incoterms": "",
            "currency": "INR", "total_value": None, "tax_type": "", "total_inclusive_value": None,
            "remarks": "", "is_old_format_template": False, "items": [],
        })()

        Command()._upsert_order(parsed)

        assert RTPAchhadDomesticPurchaseOrder.objects.get(po_number="1100000901").is_active is True


@pytest.mark.django_db
class TestRetiredOrdersLeaveMatching:
    def test_a_retired_order_stops_competing_for_mir_rows(self):
        """The reason retiring matters at all. A ghost carries the same line
        items as the order that replaced it, so until it is retired both are
        in the pool for the same MIR row.

        The MIR row here has a BLANK PO-number column, which is the case that
        actually bites. When MIR *does* name the clean number, the
        contradiction gate already excludes the ghost for free - the ghost's
        own annotated number does not match what MIR wrote, while the clean
        number is a known order, so _po_number_contradicts() drops it. That
        protection is incidental and narrow: it depends on the cleaned form
        being in known_po_numbers(), and it disappears entirely the moment
        the receipt has no PO reference to contradict with. ~64% of Achhad's
        MIR rows have no PO number at all, so this is the common shape, not
        the exotic one."""
        from apps.services.matching_achhad import run_full_match

        ghost = _order("1100000901 (Changed Purchase Order)")
        RTPAchhadDomesticPOLineItem.objects.create(
            purchase_order=ghost, item_id="1", description="Stearic Acid",
            qty="500", uom="KG", net_price="80", net_value="40000")
        RTPAchhadMIREntry.objects.create(
            mir_no="M1", party_name="Ganesh Chemicals Ltd", po_number_raw="",
            material_description="Stearic Acid", qty="500", uom="KG", rate="80",
            net="40000", mir_date=datetime.date(2026, 4, 20), source_row_ref="3", is_active=True)

        run_full_match()
        assert RTPAchhadPOMirMatch.objects.count() == 1, "ghost claims the MIR row while active"

        deactivate_missing_orders(RTPAchhadDomesticPurchaseOrder, [])
        run_full_match()
        assert RTPAchhadPOMirMatch.objects.count() == 0, "retired order must not match"
