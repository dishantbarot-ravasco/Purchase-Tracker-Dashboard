"""
Pipeline tests for manual MIR pins (apps/core/models/review.py's ManualMirMatch,
applied by matching_core.run_full_match()) - the "edit the MIR number"
feature, 2026-09-21.

Same shape and conventions as test_run_full_match_pipeline.py: real HRS
model rows, real run_full_match(), assertions on the match rows it writes.
Each behaviour here is one the feature would be broken without, and several
exist because the obvious implementation gets them wrong:

  - a pin must OUTRANK identification (the usual reason to reach for one is
    that identification got it wrong),
  - a pin must SURVIVE a re-match (otherwise the next sync silently undoes
    it - the trap `dismissed_by_override` already exists to avoid),
  - an empty mir_no must mean "deliberately unmatched", not "no pin",
  - a pin whose line item no longer holds the same material must be IGNORED
    rather than applied to whatever now sits at that position.
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
    SyncRun,
)
from apps.services.matching import run_full_match
from apps.services.matching_core import line_item_positions


def _po(po_number="3000009001", vendor_name="Rubamin Private Limited"):
    return HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO_{po_number}",
        po_number=po_number,
        po_created_date=datetime.date(2026, 4, 25),
        vendor_name=vendor_name,
        tax_type="IGST",
        total_value=Decimal("100000.00"),
        total_inclusive_value=Decimal("118000.00"),
    )


def _item(po, description="SBR 1502", qty=Decimal("1000"), net_price=Decimal("100.00"), item_id="1"):
    return HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id=item_id, description=description, hsn="4002",
        qty=qty, uom="KG", net_price=net_price, net_value=qty * net_price,
    )


def _mir(mir_no, source_row_ref, *, party_name="Rubamin Private Limited",
         po_number_raw="", material_description="SBR 1502",
         qty=Decimal("1000"), rate=Decimal("100.00"),
         mir_date=datetime.date(2026, 5, 1)):
    return HRSMIREntry.objects.create(
        month="May-26", mir_no=mir_no, mir_date=mir_date, party_name=party_name,
        po_number_raw=po_number_raw, material_description=material_description,
        qty=qty, uom="KG", rate=rate, net=qty * rate, taxable_value=qty * rate,
        invoice_final_value=qty * rate, source_row_ref=source_row_ref, is_active=True,
    )


def _pin(po_number, item_ref, mir_no, item_description="SBR 1502"):
    return ManualMirMatch.objects.create(
        plant=SyncRun.Plant.HRS, po_number=po_number, item_ref=item_ref,
        mir_no=mir_no, item_description=item_description, created_by_email="t@ravasco.com",
    )


def _match_for(item):
    return HRSPOMirMatch.objects.filter(po_line_item=item).first()


@pytest.mark.django_db
class TestManualPinOverridesTheMatcher:
    def test_pin_wins_over_the_row_the_matcher_would_have_picked(self):
        po = _po()
        item = _item(po)
        # The row the matcher prefers: names this exact PO number, so it is
        # PO-number-confirmed, the strongest automatic evidence there is.
        auto = _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        # The row a human says is actually right: no PO number, and a party
        # name that does NOT pass the vendor gate, so identification would
        # never have produced it. That is exactly the case a pin is for.
        pinned = _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")

        run_full_match()
        assert _match_for(item).mir_entry_id == auto.id, "baseline: matcher picks the PO-confirmed row"

        _pin(po.po_number, "0", "MIR-HAND")
        run_full_match()
        match = _match_for(item)
        assert match.mir_entry_id == pinned.id
        assert match.manually_pinned is True

    def test_pin_survives_a_rematch(self):
        po = _po()
        item = _item(po)
        _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        pinned = _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        _pin(po.po_number, "0", "MIR-HAND")

        for _ in range(3):
            run_full_match()
        assert _match_for(item).mir_entry_id == pinned.id

    def test_a_dismissal_survives_a_rematch_but_not_a_repointed_match(self):
        """A dismissal is a judgment on one pairing. Kept while the line still
        points at the same receipt; cleared once it points at another, whose
        flags nobody has looked at."""
        po = _po()
        item = _item(po)
        _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        run_full_match()
        HRSPOMirMatch.objects.filter(po_line_item=item).update(
            dismissed_by_override=True, dismissed_reason="qty gap agreed with vendor")

        run_full_match()
        assert _match_for(item).dismissed_by_override is True, "same MIR row: the dismissal stays"

        _pin(po.po_number, "0", "MIR-HAND")
        run_full_match()
        match = _match_for(item)
        assert match.dismissed_by_override is False
        assert match.dismissed_reason == ""

    def test_removing_the_pin_returns_the_line_to_automatic_matching(self):
        po = _po()
        item = _item(po)
        auto = _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        _pin(po.po_number, "0", "MIR-HAND")
        run_full_match()
        assert _match_for(item).manually_pinned is True

        ManualMirMatch.objects.all().delete()
        run_full_match()
        match = _match_for(item)
        assert match.mir_entry_id == auto.id
        # manually_pinned is DERIVED, never preserved - a stale badge would
        # claim a human stands behind a match nobody chose.
        assert match.manually_pinned is False

    def test_arithmetic_flags_still_apply_to_a_pinned_pair(self):
        """A pin overrides identification, never the financial check - a
        manual match that does not add up must still flag."""
        po = _po()
        item = _item(po, qty=Decimal("1000"), net_price=Decimal("100.00"))
        _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd",
             qty=Decimal("900"), rate=Decimal("120.00"))
        _pin(po.po_number, "0", "MIR-HAND")
        run_full_match()
        match = _match_for(item)
        assert match.manually_pinned is True
        assert match.qty_mismatched is True
        assert match.rate_mismatched is True


@pytest.mark.django_db
class TestPinToNoMir:
    def test_empty_mir_no_forces_the_line_unmatched(self):
        po = _po()
        item = _item(po)
        _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        run_full_match()
        assert _match_for(item) is not None, "baseline: it matches automatically"

        _pin(po.po_number, "0", "")
        run_full_match()
        assert _match_for(item) is None

    def test_a_forced_unmatched_line_releases_its_row_to_another_line(self):
        po = _po()
        first = _item(po, description="SBR 1502", item_id="1")
        second = _item(po, description="SBR 1502", item_id="2")
        only_row = _mir("MIR-ONE", "10", po_number_raw=po.po_number)
        run_full_match()
        holder = first if _match_for(first) else second
        other = second if holder is first else first
        assert _match_for(holder).mir_entry_id == only_row.id
        assert _match_for(other) is None

        refs = line_item_positions([first, second])
        _pin(po.po_number, refs[holder.id][1], "")
        run_full_match()
        assert _match_for(holder) is None
        assert _match_for(other).mir_entry_id == only_row.id


@pytest.mark.django_db
class TestPinCollision:
    def test_a_pin_takes_a_row_another_line_was_holding(self):
        po_a = _po("3000009001")
        item_a = _item(po_a, description="SBR 1502")
        po_b = _po("3000009002")
        item_b = _item(po_b, description="SBR 1502")
        contested = _mir("MIR-ONE", "10", po_number_raw=po_a.po_number)

        run_full_match()
        assert _match_for(item_a).mir_entry_id == contested.id
        assert _match_for(item_b) is None

        _pin(po_b.po_number, "0", "MIR-ONE")
        run_full_match()
        assert _match_for(item_b).mir_entry_id == contested.id
        # The displaced line is re-matched from scratch and, with nothing
        # else available, ends up unmatched - exactly what the UI's
        # collision popup warns will happen.
        assert _match_for(item_a) is None

    def test_two_pins_on_one_single_row_document_the_newer_one_wins(self):
        po_a = _po("3000009001")
        item_a = _item(po_a)
        po_b = _po("3000009002")
        item_b = _item(po_b)
        only_row = _mir("MIR-ONE", "10")

        older = _pin(po_a.po_number, "0", "MIR-ONE")
        newer = _pin(po_b.po_number, "0", "MIR-ONE")
        # updated_at is auto_now; make the intended order unambiguous rather
        # than relying on two creates landing on different microseconds.
        ManualMirMatch.objects.filter(pk=older.pk).update(
            updated_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc))
        ManualMirMatch.objects.filter(pk=newer.pk).update(
            updated_at=datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc))

        result = run_full_match()
        assert _match_for(item_b).mir_entry_id == only_row.id
        assert _match_for(item_a) is None
        # The losing pin is reported, not counted as applied - its author
        # was told "Matched" before this existed, for a line left unmatched.
        assert result["manual_pins_applied"] == 1
        assert [(p["poNumber"], p["mirNo"]) for p in result["manual_pins_unfilled"]] == [(po_a.po_number, "MIR-ONE")]


@pytest.mark.django_db
class TestPinStaleness:
    def test_a_pin_whose_line_changed_material_is_ignored_not_misapplied(self):
        po = _po()
        item = _item(po, description="SBR 1502")
        _mir("MIR-HAND", "11", party_name="Totally Different Supplier Ltd")
        # Pinned when position 0 held a different material entirely.
        _pin(po.po_number, "0", "MIR-HAND", item_description="Carbon Black N330")

        result = run_full_match()
        assert _match_for(item) is None or _match_for(item).manually_pinned is False
        assert result["manual_pins_applied"] == 0
        assert [p["poNumber"] for p in result["manual_pins_stale"]] == [po.po_number]
        # Never silently destroyed - it is the record of a human decision.
        assert ManualMirMatch.objects.count() == 1

    def test_a_pin_naming_a_mir_number_that_does_not_exist_leaves_it_unmatched(self):
        po = _po()
        item = _item(po)
        _mir("MIR-AUTO", "10", po_number_raw=po.po_number)
        _pin(po.po_number, "0", "MIR-NOPE")
        result = run_full_match()
        # NOT a silent fallback to the automatic pick, which would contradict
        # the instruction the reader gave - but not silent either.
        assert _match_for(item) is None
        assert [p["mirNo"] for p in result["manual_pins_unfilled"]] == ["MIR-NOPE"]


@pytest.mark.django_db
class TestMultiRowMirDocument:
    def test_the_matcher_picks_the_best_row_within_the_pinned_document(self):
        """A pin names a MIR NUMBER; one document routinely covers several
        material lines, and choosing among them is scoring's job."""
        po = _po()
        item = _item(po, description="SBR 1502", qty=Decimal("1000"), net_price=Decimal("100.00"))
        wrong_row = _mir("MIR-MULTI", "10", material_description="Carbon Black N330",
                         qty=Decimal("50"), rate=Decimal("9.00"))
        right_row = _mir("MIR-MULTI", "11", material_description="SBR 1502",
                         qty=Decimal("1000"), rate=Decimal("100.00"))
        _pin(po.po_number, "0", "MIR-MULTI")
        run_full_match()
        match = _match_for(item)
        assert match.mir_entry_id == right_row.id
        assert match.mir_entry_id != wrong_row.id
