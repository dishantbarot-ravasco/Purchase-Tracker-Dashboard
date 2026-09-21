"""
Pipeline tests for manual MIR pins on IMPORT PO line items (2026-09-21,
project owner: "yes do it for imports too").

The domestic half is covered in test_manual_mir_match.py. This file covers
what is genuinely different once both kinds exist:

  - an import pin reaches import line items at all,
  - `po_kind` keeps a domestic and an import PO that share a number apart,
  - domestic and import pins compete for ONE MIR table, settled in one pass
    against one claimed-row set (imports reconcile against the same MIR file
    as domestic - see HRSImportPOMirMatch's docstring), so an import pin can
    take a row a domestic line was holding and vice versa.

That last one is the reason `_load_pins()` loads both kinds together rather
than once per kind: two separate passes would each believe they held the
same row.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSImportPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    ManualMirMatch,
    SyncRun,
)
from apps.services.matching import run_full_match


def _import_po(po_number="IMP900", vendor_name="Global Polymers Inc"):
    return HRSImportPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name=vendor_name,
        currency="USD", total_value=Decimal("234.00"),
    )


def _import_item(po, description="PTFE Coated Fabric", qty=Decimal("100"),
                 net_price=Decimal("1.95"), exchange_rate=Decimal("93.80")):
    return HRSImportPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="5903",
        qty_as_per_po=qty, qty_as_per_boe=qty, uom="KG",
        net_price=net_price, net_value=qty * net_price,
        tax_type="IGST", currency_after_taxes="INR", exchange_rate=exchange_rate,
        total_inclusive_value=qty * net_price * exchange_rate,
    )


def _domestic_po(po_number="3000009500", vendor_name="Rubamin Private Limited"):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name=vendor_name,
        tax_type="IGST", total_value=Decimal("100000.00"),
        total_inclusive_value=Decimal("118000.00"),
    )


def _domestic_item(po, description="SBR 1502", qty=Decimal("1000"), net_price=Decimal("100.00")):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="4002",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _mir(mir_no, source_row_ref, *, party_name="Global Polymers Inc",
         material_description="PTFE Coated Fabric", qty=Decimal("100"),
         rate=Decimal("182.91"), po_number_raw=""):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no=mir_no, mir_date=datetime.date(2026, 5, 1),
        party_name=party_name, po_number_raw=po_number_raw,
        material_description=material_description, qty=qty, uom="KG", rate=rate,
        net=qty * rate, taxable_value=qty * rate, invoice_final_value=qty * rate,
        source_row_ref=source_row_ref, is_active=True,
    )


def _pin(po_number, item_ref, mir_no, *, kind, item_description):
    return ManualMirMatch.objects.create(
        plant=SyncRun.Plant.HRS, po_kind=kind, po_number=po_number, item_ref=item_ref,
        mir_no=mir_no, item_description=item_description, created_by_email="t@ravasco.com",
    )


def _import_match(item):
    return HRSImportPOMirMatch.objects.filter(po_line_item=item).first()


def _domestic_match(item):
    return HRSPOMirMatch.objects.filter(po_line_item=item).first()


@pytest.mark.django_db
class TestImportPins:
    def test_pin_wins_over_the_row_the_matcher_would_have_picked(self):
        po = _import_po()
        item = _import_item(po)
        auto = _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        pinned = _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")

        run_full_match()
        assert _import_match(item).mir_entry_id == auto.id, "baseline"

        _pin(po.po_number, "0", "MIR-HAND",
             kind=ManualMirMatch.POKind.IMPORT, item_description="PTFE Coated Fabric")
        run_full_match()
        match = _import_match(item)
        assert match.mir_entry_id == pinned.id
        assert match.manually_pinned is True

    def test_pin_survives_a_rematch_and_clears_when_removed(self):
        po = _import_po()
        item = _import_item(po)
        auto = _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        pinned = _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        _pin(po.po_number, "0", "MIR-HAND",
             kind=ManualMirMatch.POKind.IMPORT, item_description="PTFE Coated Fabric")

        for _ in range(3):
            run_full_match()
        assert _import_match(item).mir_entry_id == pinned.id

        ManualMirMatch.objects.all().delete()
        run_full_match()
        match = _import_match(item)
        assert match.mir_entry_id == auto.id
        assert match.manually_pinned is False

    def test_empty_mir_no_forces_an_import_line_unmatched(self):
        po = _import_po()
        item = _import_item(po)
        _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        run_full_match()
        assert _import_match(item) is not None, "baseline"

        _pin(po.po_number, "0", "", kind=ManualMirMatch.POKind.IMPORT,
             item_description="PTFE Coated Fabric")
        run_full_match()
        assert _import_match(item) is None

    def test_a_stale_import_pin_is_ignored_and_reported(self):
        po = _import_po()
        item = _import_item(po, description="PTFE Coated Fabric")
        _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        _pin(po.po_number, "0", "MIR-HAND", kind=ManualMirMatch.POKind.IMPORT,
             item_description="Something Else Entirely")

        result = run_full_match()
        assert result["manual_pins_applied"] == 0
        assert [p["poNumber"] for p in result["manual_pins_stale"]] == [po.po_number]
        assert ManualMirMatch.objects.count() == 1
        assert _import_match(item) is None or _import_match(item).manually_pinned is False


@pytest.mark.django_db
class TestPoKindKeepsTheTwoApart:
    def test_a_domestic_pin_does_not_reach_an_import_line_of_the_same_po_number(self):
        """Domestic and Import POs are separate tables, each with its own
        `po_number` unique constraint, so one number really can exist as
        both. Without po_kind in the key, one pin would address both."""
        shared_number = "SHARED-001"
        dom_po = _domestic_po(shared_number)
        dom_item = _domestic_item(dom_po, description="SBR 1502")
        imp_po = _import_po(shared_number)
        imp_item = _import_item(imp_po, description="PTFE Coated Fabric")

        dom_target = _mir("MIR-DOM", "10", party_name="Rubamin Private Limited",
                          material_description="SBR 1502", qty=Decimal("1000"),
                          rate=Decimal("100.00"))
        imp_target = _mir("MIR-IMP", "11", material_description="PTFE Coated Fabric")

        _pin(shared_number, "0", "MIR-DOM", kind=ManualMirMatch.POKind.DOMESTIC,
             item_description="SBR 1502")
        _pin(shared_number, "0", "MIR-IMP", kind=ManualMirMatch.POKind.IMPORT,
             item_description="PTFE Coated Fabric")
        run_full_match()

        assert _domestic_match(dom_item).mir_entry_id == dom_target.id
        assert _import_match(imp_item).mir_entry_id == imp_target.id

    def test_both_kinds_can_be_pinned_at_once(self):
        dom_po = _domestic_po()
        dom_item = _domestic_item(dom_po)
        imp_po = _import_po()
        imp_item = _import_item(imp_po)
        dom_row = _mir("MIR-DOM", "10", party_name="Rubamin Private Limited",
                       material_description="SBR 1502", qty=Decimal("1000"), rate=Decimal("100.00"))
        imp_row = _mir("MIR-IMP", "11")

        _pin(dom_po.po_number, "0", "MIR-DOM", kind=ManualMirMatch.POKind.DOMESTIC,
             item_description="SBR 1502")
        _pin(imp_po.po_number, "0", "MIR-IMP", kind=ManualMirMatch.POKind.IMPORT,
             item_description="PTFE Coated Fabric")
        result = run_full_match()

        assert result["manual_pins_applied"] == 2
        assert _domestic_match(dom_item).mir_entry_id == dom_row.id
        assert _import_match(imp_item).mir_entry_id == imp_row.id


@pytest.mark.django_db
class TestCrossKindCollision:
    def test_an_import_pin_takes_a_row_a_domestic_line_was_holding(self):
        """Both kinds claim from ONE MIR table against ONE claimed-row set -
        loading the two kinds of pin separately would let each believe it
        held this row."""
        dom_po = _domestic_po()
        dom_item = _domestic_item(dom_po, description="PTFE Coated Fabric")
        imp_po = _import_po()
        imp_item = _import_item(imp_po, description="PTFE Coated Fabric")
        # One row, reachable by the domestic line automatically (it names
        # that PO number) and by the import line only via a pin.
        contested = _mir("MIR-ONE", "10", party_name="Rubamin Private Limited",
                         material_description="PTFE Coated Fabric",
                         qty=Decimal("1000"), rate=Decimal("100.00"),
                         po_number_raw=dom_po.po_number)

        run_full_match()
        assert _domestic_match(dom_item).mir_entry_id == contested.id
        assert _import_match(imp_item) is None

        _pin(imp_po.po_number, "0", "MIR-ONE", kind=ManualMirMatch.POKind.IMPORT,
             item_description="PTFE Coated Fabric")
        run_full_match()
        assert _import_match(imp_item).mir_entry_id == contested.id
        assert _domestic_match(dom_item) is None

    def test_a_domestic_pin_takes_a_row_an_import_line_was_holding(self):
        dom_po = _domestic_po()
        dom_item = _domestic_item(dom_po, description="PTFE Coated Fabric")
        imp_po = _import_po()
        imp_item = _import_item(imp_po, description="PTFE Coated Fabric")
        contested = _mir("MIR-ONE", "10", po_number_raw=imp_po.po_number)

        run_full_match()
        assert _import_match(imp_item).mir_entry_id == contested.id

        _pin(dom_po.po_number, "0", "MIR-ONE", kind=ManualMirMatch.POKind.DOMESTIC,
             item_description="PTFE Coated Fabric")
        run_full_match()
        assert _domestic_match(dom_item).mir_entry_id == contested.id
        assert _import_match(imp_item) is None
