"""
Integration tests for apps/services/matching.py's run_full_match() (HRS) -
previously zero coverage at the "runs against real DB rows and writes real
match rows" level. apps/services/tests/test_matching.py already covers the
pure, dependency-free scoring helpers (_closeness/_diff_pct/_vendor_matches/
etc, imported straight from matching_core with no DB) - this file is the
other half CLAUDE.md's "Known gaps" flags as missing: the actual pipeline
effect of calling run_full_match() against real HRSDomesticPOLineItem/
HRSMIREntry/HRSRMLot rows, which needs a real Postgres DB
(@pytest.mark.django_db) since matching_core.run_full_match() queries the
ORM directly rather than accepting in-memory objects.

No factories exist for these models (only apps/api/tests/factories.py's
make_user()) - built directly via Model.objects.create(), same convention
apps/api/tests/test_hrs_correct_field.py already uses for this app's other
HRS models.
"""

import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSRMLot,
)
from apps.services.matching import run_full_match


def _make_po(po_number="3000001104", vendor_name="Rubamin Private Limited"):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}",
        po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25),
        vendor_name=vendor_name,
        tax_type="IGST",
        total_value=Decimal("100000.00"),
        total_inclusive_value=Decimal("118000.00"),
    )


def _make_po_line_item(po, description="SBR 1502", qty=Decimal("1000"), net_price=Decimal("100.00")):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description=description, hsn="4002",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _make_mir_entry(
    party_name="Rubamin Private Limited", po_number_raw="3000001104",
    material_description="SBR 1502", qty=Decimal("1000"), rate=Decimal("100.00"),
    mir_date=datetime.date(2026, 5, 1), source_row_ref="7", uom="KG",
):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no="MIR001", mir_date=mir_date, party_name=party_name,
        po_number_raw=po_number_raw, material_description=material_description,
        qty=qty, uom=uom, rate=rate, net=qty * rate, taxable_value=qty * rate,
        invoice_final_value=qty * rate, source_row_ref=source_row_ref, is_active=True,
    )


def _make_stock_lot(
    description="SBR 1502", party_name="Rubamin Private Limited - Vadodara",
    basic_rate=Decimal("100.00"), received=Decimal("0"), received_date=None, sap_item_code="H1",
    uom="KG",
):
    return HRSRMLot.objects.create(
        description=description, sap_item_code=sap_item_code, party_name=party_name,
        basic_rate=basic_rate, received=received, todays_stock=Decimal("5000"),
        received_date=received_date, natural_key=f"hrs:{sap_item_code}:{description}",
        is_active=True, uom=uom,
    )


@pytest.mark.django_db
class TestPoMirMatching:
    def test_exact_po_number_match_with_a_qty_mismatch_is_flagged(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("1000"))
        _make_mir_entry(qty=Decimal("990"))  # 10 units short of the PO's 1000

        run_full_match()

        match = HRSPOMirMatch.objects.get(po_line_item=line_item)
        assert match.tier == HRSPOMirMatch.Tier.PO_NUMBER
        assert match.po_number_matched is True
        assert match.qty_mismatched is True
        assert match.is_flagged is True
        assert match.qty_diff_pct is not None and match.qty_diff_pct > 0
        # Over/under direction is written by every plant's matcher, not just
        # the one it was designed against - this is what makes "re-run
        # matching for that plant" the fix when a plant's KPI cards read zero
        # (a match row written before migration 0050 has NULL here, and NULL
        # is counted as neither direction). 990 received against 1000 ordered.
        assert match.qty_over_delivered is False

    def test_an_exact_match_on_every_field_is_never_flagged(self):
        po = _make_po()
        line_item = _make_po_line_item(po, qty=Decimal("1000"), net_price=Decimal("100.00"))
        _make_mir_entry(qty=Decimal("1000"), rate=Decimal("100.00"))

        run_full_match()

        match = HRSPOMirMatch.objects.get(po_line_item=line_item)
        assert match.qty_mismatched is False
        assert match.rate_mismatched is False
        assert match.is_flagged is False

    # ── Identification 2-of-3 (2026-09-18, HRS joined Achhad) ──────────────
    # HRS is on matching_core._MatchConfig.identification_two_of_three now,
    # so vendor is a VOTE here, not a veto - the assertion that used to live
    # in this block ("a matching PO number and material must not be enough on
    # their own") no longer states the rule. What makes the flip safe on HRS
    # is data, not code: no row in HRS's live MIR file both names an order we
    # hold and disagrees on vendor, so the first two tests below pin behavior
    # that today's file never exercises. They are here precisely because that
    # is a property of one file and not of the plant - see matching.py's own
    # comment for the counts, and the Achhad twins of these tests for the
    # live rows each edge stands for.

    def test_different_vendor_is_outvoted_by_po_number_plus_material(self):
        po = _make_po(vendor_name="Rubamin Private Limited")
        line_item = _make_po_line_item(po)
        _make_mir_entry(party_name="Some Totally Different Vendor Ltd")

        run_full_match()

        match = HRSPOMirMatch.objects.get(po_line_item=line_item)
        assert match.po_number_matched is True
        assert match.material_matched is True
        assert match.vendor_matched is False, (
            "the match is made on PO number plus material, and vendor_matched False is what "
            "reports the party name disagreeing so someone can fix it at source"
        )

    def test_different_vendor_with_only_a_po_number_never_matches(self):
        """The refusal half of the rule: a PO number pointing at another
        supplier's order with the material disagreeing too. One of three is
        not identification."""
        po = _make_po(vendor_name="Rubamin Private Limited")
        line_item = _make_po_line_item(po, description="SBR 1502")
        _make_mir_entry(
            party_name="Some Totally Different Vendor Ltd",
            material_description="Zinc Oxide",
        )

        run_full_match()

        assert not HRSPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_different_vendor_with_only_a_material_never_matches(self):
        """The mirror: the same common material bought from two suppliers is
        not evidence on its own, and never was."""
        po = _make_po(vendor_name="Rubamin Private Limited")
        line_item = _make_po_line_item(po, description="SBR 1502")
        _make_mir_entry(
            party_name="Some Totally Different Vendor Ltd",
            po_number_raw="4000000999",
        )

        run_full_match()

        assert not HRSPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_no_po_vendor_row_is_reconciled_once_it_names_an_order_we_hold(self):
        """The half of the flag that actually changes HRS's numbers today.
        Tinna Rubber is registered NO_PO_SUPPLIER, so parsers.common's
        NO_PO_VENDORS registry dropped its MIR rows from the PO<->MIR pool
        entirely - including the four rows naming 3000001081/3000001098,
        orders the master CSV really does hold against it. See
        _MirCandidateIndex's docstring and matching.py's own comment."""
        po = _make_po(
            po_number="3000001081", vendor_name="Tinna Rubber and Infrastructure Ltd"
        )
        line_item = _make_po_line_item(po, description="Crumb Rubber 80 Mesh")
        _make_mir_entry(
            party_name="Tinna Rubber and Infrastructure Ltd",
            po_number_raw="3000001081",
            material_description="Crumb Rubber 80 Mesh",
        )

        run_full_match()

        assert HRSPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_no_po_vendor_row_without_a_po_reference_stays_excluded(self):
        """The other half of that rule - the ordinary no-PO purchase, which
        is what the registry is actually for, is untouched."""
        po = _make_po(
            po_number="3000001081", vendor_name="Tinna Rubber and Infrastructure Ltd"
        )
        line_item = _make_po_line_item(po, description="Crumb Rubber 80 Mesh")
        _make_mir_entry(
            party_name="Tinna Rubber and Infrastructure Ltd",
            po_number_raw="",
            material_description="Crumb Rubber 80 Mesh",
        )

        run_full_match()

        assert not HRSPOMirMatch.objects.filter(po_line_item=line_item).exists()

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        po = _make_po()
        line_item = _make_po_line_item(po)
        _make_mir_entry()

        run_full_match()
        run_full_match()

        assert HRSPOMirMatch.objects.filter(po_line_item=line_item).count() == 1


@pytest.mark.django_db
class TestMirStockMatching:
    def test_rate_mismatch_is_flagged_only_when_dates_confirm_the_same_delivery(self):
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("100.00"))
        _make_stock_lot(basic_rate=Decimal("105.00"), received_date=datetime.date(2026, 5, 1))

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is True
        assert match.rate_mismatched is True
        assert match.is_flagged is True
        assert match.rate_diff_pct is not None and match.rate_diff_pct > 0

    def test_rate_mismatch_is_not_flagged_when_dates_do_not_confirm_the_same_delivery(self):
        """A stock lot's own rate reflects its MOST RECENT receipt, which can
        be months apart from the specific delivery an MIR row recorded (see
        match_mir_entry_stock()'s own docstring, with a real HRS example of
        commodity price drift being misread as a data error) - rate is only
        compared when Rec. DT. actually confirms it's the same event."""
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), rate=Decimal("100.00"))
        _make_stock_lot(basic_rate=Decimal("105.00"), received_date=datetime.date(2026, 8, 13))

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.date_matched is False
        assert match.rate_mismatched is False
        assert match.is_flagged is False
        assert match.rate_diff_pct is None

    def test_vendor_gate_uses_containment_not_equality(self):
        """HRS's Stock sheet appends a city suffix its MIR party_name never
        carries (e.g. '... - Vadodara') - confirmed live-data quirk per
        _vendor_matches()'s own docstring; the gate must still pass."""
        mir_entry = _make_mir_entry(party_name="Rubamin Private Limited")
        _make_stock_lot(party_name="Rubamin Private Limited - Vadodara")

        run_full_match()

        assert HRSMirStockMatch.objects.filter(mir_entry=mir_entry).exists()

    def test_rerunning_is_idempotent_no_duplicate_match_rows(self):
        mir_entry = _make_mir_entry()
        _make_stock_lot()

        run_full_match()
        run_full_match()

        assert HRSMirStockMatch.objects.filter(mir_entry=mir_entry).count() == 1

    def test_rate_and_qty_are_unit_converted_before_comparing_not_raw(self):
        """Regression test for a real bug found during a full-codebase audit
        (2026-09-10): this function used to compare MIR's rate/qty directly
        against the Stock lot's own rate/received with no unit conversion at
        all - a material logged in MIR as MT against a Stock lot recorded in
        KG would report a ~1000x 'rate mismatch' that's actually just a unit
        artifact. 1.5 MT at Rs.40,000/MT is the same delivery as 1500 KG at
        Rs.40/KG - once correctly converted to a common base unit, this must
        NOT be flagged as a rate or qty mismatch."""
        mir_entry = _make_mir_entry(
            mir_date=datetime.date(2026, 5, 1), qty=Decimal("1.5"), uom="MT", rate=Decimal("40000.00"),
        )
        _make_stock_lot(
            received_date=datetime.date(2026, 5, 1), received=Decimal("1500"), uom="KG", basic_rate=Decimal("40.00"),
        )

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.uom_mismatch is False
        assert match.rate_mismatched is False
        assert match.qty_mismatched is False
        assert match.is_flagged is False

    def test_incompatible_units_are_flagged_as_uom_mismatch_not_a_nonsense_percentage(self):
        """A mass unit against a count unit (e.g. KG vs PCS) can never be
        converted - must be reported as uom_mismatch, with qty/rate diffs
        left None, rather than comparing raw numbers from unrelated unit
        families."""
        mir_entry = _make_mir_entry(mir_date=datetime.date(2026, 5, 1), qty=Decimal("1000"), uom="KG")
        _make_stock_lot(received_date=datetime.date(2026, 5, 1), received=Decimal("1000"), uom="PCS")

        run_full_match()

        match = HRSMirStockMatch.objects.get(mir_entry=mir_entry)
        assert match.uom_mismatch is True
        assert match.rate_diff_pct is None
        assert match.qty_diff_pct is None
        assert match.rate_mismatched is False
        assert match.data_mismatch is True
