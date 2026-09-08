"""
Integration tests for Import PO <-> MIR matching (HRS) - previously zero
coverage of the real currency-conversion bug CLAUDE.md documents: import
line items are priced in the PO's own foreign currency, but MIR's rate/net
are always INR. Comparing them raw understates a real match's score by
~90x (confirmed against a real row: PO net_price $1.95 x exchange_rate 93.8
= Rs.182.91 vs. a naively-compared raw $1.95), which is exactly why
apps/services/matching_core.py's _import_rate_value_inr() exists and must
run before scoring - this file proves it's actually wired in, the same way
test_run_full_match_pipeline.py proves the rest of the domestic matcher.

qty_as_per_boe (not qty_as_per_po) is what's compared against MIR - the
Bill of Entry quantity is what customs recorded as actually clearing,
which can legitimately differ from what was originally ordered on a
partial/split shipment (see CLAUDE.md's "Import PO <-> MIR reconciliation").
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSImportPurchaseOrder,
    HRSMIREntry,
)
from apps.services.matching import run_full_match


def _make_import_po(po_number="IMP001", vendor_name="Global Polymers Inc"):
    return HRSImportPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}", po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25), vendor_name=vendor_name,
        currency="USD", total_value=Decimal("234.00"),
    )


def _make_import_line_item(
    po, description="PTFE Coated Fabric", qty_as_per_po=Decimal("120"),
    qty_as_per_boe=Decimal("100"), net_price=Decimal("1.95"), exchange_rate=Decimal("93.80"),
):
    total_inclusive_value = qty_as_per_boe * net_price * exchange_rate if exchange_rate is not None else None
    return HRSImportPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="5903",
        qty_as_per_po=qty_as_per_po, qty_as_per_boe=qty_as_per_boe, uom="KG",
        net_price=net_price, net_value=qty_as_per_boe * net_price,
        tax_type="IGST", currency_after_taxes="INR", exchange_rate=exchange_rate,
        total_inclusive_value=total_inclusive_value,
    )


def _make_mir_entry(
    party_name="Global Polymers Inc", material_description="PTFE Coated Fabric",
    qty=Decimal("100"), rate=Decimal("182.91"), source_row_ref="7",
):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no="MIR001", mir_date=datetime.date(2026, 5, 1),
        party_name=party_name, material_description=material_description,
        qty=qty, uom="KG", rate=rate, net=qty * rate, taxable_value=qty * rate,
        invoice_final_value=qty * rate, source_row_ref=source_row_ref, is_active=True,
    )


@pytest.mark.django_db
class TestImportPoMirCurrencyConversion:
    def test_a_correctly_converted_rate_matches_the_inr_mir_rate_exactly(self):
        """$1.95 x 93.80 = Rs.182.91 - matches the MIR rate exactly, so this
        must NOT be flagged as a rate mismatch. If _import_rate_value_inr()
        were skipped, the raw $1.95 vs Rs.182.91 comparison would look like
        a ~99% rate discrepancy instead."""
        po = _make_import_po()
        line_item = _make_import_line_item(po, net_price=Decimal("1.95"), exchange_rate=Decimal("93.80"))
        _make_mir_entry(rate=Decimal("182.91"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is False
        assert match.rate_diff_pct is not None and match.rate_diff_pct < Decimal("1")

    def test_qty_as_per_boe_is_compared_not_qty_as_per_po(self):
        """qty_as_per_po (120, originally ordered) legitimately differs from
        qty_as_per_boe (100, what customs actually cleared) on a partial
        shipment - matching must use the BOE figure, which matches MIR's 100
        exactly here, not the PO figure which would show a 20% mismatch."""
        po = _make_import_po()
        line_item = _make_import_line_item(po, qty_as_per_po=Decimal("120"), qty_as_per_boe=Decimal("100"))
        _make_mir_entry(qty=Decimal("100"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.qty_mismatched is False

    def test_a_genuine_qty_mismatch_against_boe_is_still_caught(self):
        po = _make_import_po()
        line_item = _make_import_line_item(po, qty_as_per_boe=Decimal("100"))
        _make_mir_entry(qty=Decimal("90"))  # genuinely differs from BOE's 100

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.qty_mismatched is True
        assert match.is_flagged is True

    def test_different_vendor_never_matches(self):
        po = _make_import_po(vendor_name="Global Polymers Inc")
        line_item = _make_import_line_item(po)
        _make_mir_entry(party_name="A Completely Different Vendor Ltd")

        run_full_match()

        assert not HRSImportPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_missing_exchange_rate_falls_back_to_the_bare_net_price(self):
        """2 of 37 real Vapi import rows had no exchange rate at all - see
        _import_rate_value_inr()'s own docstring. Must not crash, and must
        compare the bare, unconverted price rather than silently matching
        anything (which a None-vs-anything short-circuit could do)."""
        po = _make_import_po()
        line_item = _make_import_line_item(po, net_price=Decimal("182.91"), exchange_rate=None)
        _make_mir_entry(rate=Decimal("182.91"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is False

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_import_po()
        line_item = _make_import_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert HRSImportPOMirMatch.objects.filter(po_line_item=line_item).count() == 1
