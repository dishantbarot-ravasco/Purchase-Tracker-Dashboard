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

    def test_net_value_is_compared_pre_duty_not_the_landed_figure(self):
        """MIR's net is pre-tax, so the import side's net value must be too:
        net_value x exchange_rate ($195 x 93.80 = Rs.18,291, exactly MIR's
        net), not total_inclusive_value, the landed figure with customs duty
        (here 18% on top). Comparing the landed figure read every import
        short by the duty rate. The landed figure keeps its own check, the
        final-value comparison, which does flag here."""
        po = _make_import_po()
        line_item = _make_import_line_item(po)
        line_item.total_inclusive_value = line_item.net_value * line_item.exchange_rate * Decimal("1.18")
        line_item.save()
        _make_mir_entry()

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.value_diff_pct is not None and match.value_diff_pct < Decimal("1")
        assert match.net_value_mismatched is False
        assert match.final_value_mismatched is True

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_import_po()
        line_item = _make_import_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert HRSImportPOMirMatch.objects.filter(po_line_item=line_item).count() == 1


def _cleared_duty_paid_line(po, *, boe_number="BOE1", duty=Decimal("1.0825"), igst=Decimal("1.18")):
    """A cleared line whose landed value carries duty and IGST, exactly as the
    Imports master CSV's total_inclusive_value does: $1.95 x 93.80 =
    Rs.182.91 pre-duty per KG, x 1.0825 (7.5% basic duty plus its 10%
    surcharge) x 1.18 IGST per KG landed, on the 100 KG BOE."""
    line_item = _make_import_line_item(po)
    line_item.boe_number = boe_number
    line_item.total_inclusive_value = (Decimal("100") * Decimal("182.91") * duty * igst).quantize(Decimal("0.01"))
    line_item.save()
    return line_item


def _duty_paid_mir(qty=Decimal("100"), rate=Decimal("198.00")):
    """MIR books an import at its duty-paid rate (182.91 x 1.0825 = 198.00),
    with IGST on top in the final value."""
    mir = _make_mir_entry(qty=qty, rate=rate)
    mir.invoice_final_value = (qty * rate * Decimal("1.18")).quantize(Decimal("0.01"))
    mir.save()
    return mir


@pytest.mark.django_db
class TestImportLandedRate:
    """The rate check also accepts the landed basis (2026-09-25): on Vapi 5
    lines read exactly 8.25% "rate mismatch", which was the customs duty
    itself. See matching_core._landed_rate_diff()."""

    def test_duty_paid_mir_rate_agrees_with_the_landed_rate(self):
        po = _make_import_po()
        line_item = _cleared_duty_paid_line(po)
        _duty_paid_mir()

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_diff_pct == Decimal("0")
        assert match.rate_mismatched is False

    def test_uncleared_line_is_still_compared_pre_duty_only(self):
        """No BOE yet means no landed figure to trust, so the same 8.25% gap
        is still reported."""
        po = _make_import_po()
        line_item = _cleared_duty_paid_line(po, boe_number="")
        _duty_paid_mir()

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is True
        assert Decimal("8") < match.rate_diff_pct < Decimal("8.5")

    def test_part_delivery_is_compared_per_unit_not_as_totals(self):
        """60 of 100 KG received: the totals are 40% apart, the rate is not."""
        po = _make_import_po()
        line_item = _cleared_duty_paid_line(po)
        _duty_paid_mir(qty=Decimal("60"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is False
        assert match.qty_mismatched is True

    def test_a_real_gap_on_both_bases_still_flags_with_the_closer_one(self):
        """MIR 3% above even the duty-paid rate: flagged, and the stored gap
        is the landed-basis 3%, not the pre-duty 11.5%."""
        po = _make_import_po()
        line_item = _cleared_duty_paid_line(po)
        _duty_paid_mir(rate=Decimal("203.94"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is True
        assert Decimal("2.9") < match.rate_diff_pct < Decimal("3.1")

    def test_exact_pre_duty_rate_still_agrees_when_landed_runs_high(self):
        """Either basis agreeing is enough: a MIR rate equal to the pre-duty
        rate is not flagged because the landed figure carries a charge MIR
        does not (seen on Vapi 1000001450, landed 0.46% high)."""
        po = _make_import_po()
        line_item = _cleared_duty_paid_line(po, duty=Decimal("1.0047"))
        _make_mir_entry(rate=Decimal("182.91"))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line_item)
        assert match.rate_mismatched is False


def _boe_line(po, *, boe, qty=Decimal("100"), net_price=Decimal("1.95"), fx=Decimal("93.80"), bl="", item_id="1"):
    """A cleared, duty-free line: landed = qty x price x fx x 1.18 IGST."""
    line = HRSImportPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, description="PTFE Coated Fabric", hsn="5903",
        qty_as_per_po=qty, qty_as_per_boe=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
        tax_type="IGST", currency_after_taxes="INR", exchange_rate=fx, boe_number=boe, bill_of_lading_number=bl,
        total_inclusive_value=(qty * net_price * fx * Decimal("1.18")).quantize(Decimal("0.01")),
    )
    return line


def _receipt(*, invoice_no, qty=Decimal("100"), rate=Decimal("182.91"), ref="7", mir_no="MIR001",
             party="Global Polymers Inc", material="PTFE Coated Fabric"):
    mir = _make_mir_entry(qty=qty, rate=rate, source_row_ref=ref, party_name=party, material_description=material)
    mir.mir_no = mir_no
    mir.invoice_no = invoice_no
    mir.invoice_final_value = (qty * rate * Decimal("1.18")).quantize(Decimal("0.01"))
    mir.save()
    return mir


@pytest.mark.django_db
class TestImportBoeNumberPairing:
    """MIR's invoice_no on an import receipt is the Bill of Entry number
    (2026-09-25) - see matching_core._boe_settlement()."""

    def test_the_receipt_citing_the_boe_wins_over_a_better_scoring_one(self):
        """Two identical 100 KG receipts from the same vendor: the matcher
        used to pick on money alone, which is how 9 of Vapi's 27 import
        pairings held another shipment's receipt."""
        po = _make_import_po()
        line = _boe_line(po, boe="8826527")
        _receipt(invoice_no="SUPPLIER-INV-1", ref="7", mir_no="MIR-OTHER")
        cited = _receipt(invoice_no="8826527", qty=Decimal("99"), ref="8", mir_no="MIR-BOE")

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line)
        assert match.mir_entry_id == cited.id
        assert match.tier == "boe_number"

    def test_swapped_receipts_go_to_the_shipment_they_cite(self):
        """1000001357 and 1000001446 held each other's receipts."""
        po_a, po_b = _make_import_po("IMPA"), _make_import_po("IMPB")
        line_a = _boe_line(po_a, boe="8826527")
        line_b = _boe_line(po_b, boe="8329085")
        for_a = _receipt(invoice_no="8826527", ref="7", mir_no="MIR-A")
        for_b = _receipt(invoice_no="8329085", ref="8", mir_no="MIR-B")

        run_full_match()

        assert HRSImportPOMirMatch.objects.get(po_line_item=line_a).mir_entry_id == for_a.id
        assert HRSImportPOMirMatch.objects.get(po_line_item=line_b).mir_entry_id == for_b.id

    def test_a_citing_row_from_another_vendor_for_another_material_does_not_attach(self):
        po = _make_import_po()
        line = _boe_line(po, boe="8826527")
        _receipt(invoice_no="8826527", party="Unrelated Traders", material="Steel Wire Rod")

        run_full_match()

        assert not HRSImportPOMirMatch.objects.filter(po_line_item=line, tier="boe_number").exists()

    def test_one_receipt_for_a_whole_boe_is_shared_by_its_lines(self):
        """Akros books a 2,000 KG and a 14,000 KG line as one 16,000 KG
        receipt at one blended rate. Each line counts its share and is
        compared at the BOE's blended rate - 1000001317's three lines read
        62%, 49% and 4% rate gaps against their own rates."""
        po = _make_import_po()
        small = _boe_line(po, boe="9055911", qty=Decimal("2000"), net_price=Decimal("6.00"), item_id="1")
        large = _boe_line(po, boe="9055911", qty=Decimal("14000"), net_price=Decimal("1.80"), item_id="2")
        blended = (Decimal("2000") * Decimal("6.00") + Decimal("14000") * Decimal("1.80")) / Decimal("16000") * Decimal("93.80")
        _receipt(invoice_no="9055911", qty=Decimal("16000"), rate=blended.quantize(Decimal("0.0001")))

        run_full_match()

        m_small = HRSImportPOMirMatch.objects.get(po_line_item=small)
        m_large = HRSImportPOMirMatch.objects.get(po_line_item=large)
        assert m_small.mir_entry_id == m_large.mir_entry_id
        assert m_small.receipt_share == Decimal("0.125000")
        assert m_large.receipt_share == Decimal("0.875000")
        for m in (m_small, m_large):
            assert m.qty_mismatched is False
            assert m.rate_mismatched is False

    def test_a_boe_shared_by_different_bills_of_lading_is_not_trusted(self):
        """1000001560: two shipments, two Bills of Lading, one BOE number
        in the CSV - a copy error, so the BOE is not used to pair them."""
        po = _make_import_po()
        a = _boe_line(po, boe="3587956", bl="BL-ONE", item_id="1")
        b = _boe_line(po, boe="3587956", bl="BL-TWO", item_id="2")
        _receipt(invoice_no="3587956", ref="7", mir_no="MIR-A")
        _receipt(invoice_no="3729236", ref="8", mir_no="MIR-B")

        run_full_match()

        tiers = set(HRSImportPOMirMatch.objects.filter(po_line_item__in=[a, b]).values_list("tier", flat=True))
        assert "boe_number" not in tiers


@pytest.mark.django_db
class TestImportExchangeRateDifference:
    """A rate gap that is a different customs exchange rate is reported as
    one, not as a rate mismatch (2026-09-25) - see
    matching_core._exchange_rate_explains()."""

    def test_a_customs_style_rate_difference_is_an_exchange_rate_flag(self):
        """1000001351: CSV 92.50, MIR works at 94.20."""
        po = _make_import_po()
        line = _boe_line(po, boe="8994955", fx=Decimal("92.50"))
        _receipt(invoice_no="8994955", rate=(Decimal("1.95") * Decimal("94.20")).quantize(Decimal("0.0001")))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line)
        assert match.exchange_rate_mismatched is True
        assert match.rate_mismatched is False
        assert match.rate_diff_pct == Decimal("0")
        assert match.mir_exchange_rate == Decimal("94.2000")

    def test_an_off_grid_gap_stays_a_rate_mismatch(self):
        """1000001360: MIR implies 100.08, not a customs rate - a real gap."""
        po = _make_import_po()
        line = _boe_line(po, boe="8751721", fx=Decimal("94.20"))
        _receipt(invoice_no="8751721", rate=(Decimal("1.95") * Decimal("100.076")).quantize(Decimal("0.0001")))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line)
        assert match.exchange_rate_mismatched is False
        assert match.rate_mismatched is True
        assert match.mir_exchange_rate is not None

    def test_a_move_too_large_for_an_exchange_rate_stays_a_rate_mismatch(self):
        """On the grid but 10% away - not a plausible exchange-rate move."""
        po = _make_import_po()
        line = _boe_line(po, boe="8751722", fx=Decimal("94.20"))
        _receipt(invoice_no="8751722", rate=(Decimal("1.95") * Decimal("103.60")).quantize(Decimal("0.0001")))

        run_full_match()

        match = HRSImportPOMirMatch.objects.get(po_line_item=line)
        assert match.exchange_rate_mismatched is False
        assert match.rate_mismatched is True
