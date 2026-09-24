"""
Pipeline tests for PO-number groups (matching_core.run_full_match(),
2026-09-24): every MIR row whose own PO column names an order counts toward
that order, however many there are and whatever their rate.

Reported against PO 3000001174 (HRS, Carbon Black N330, 200,000 KG): four
receipts named it, all four were summed into a "67.8% short" figure, and only
one was saved - the modal showed one MIR number, the other three read as
unmatched everywhere. The project owner's rule: "I specifically added the PO
number in MIR for these purpose ... one PO can match with multiple MIR number
based on PO numbers only."

Same real-row conventions as test_manual_mir_match.py. The four
TestEveryReceiptCitingThePoIsSaved tests that name a failure (all four
receipts, different rate, over 150%, contested receipt) and the no-PO-panel
test fail against the pre-2026-09-24 matcher. The rest guard the new logic's
own edges: the duplicate-line case broke the first version of it, and
identification and link rebuilding must not regress.
"""
from decimal import Decimal

import pytest

from apps.core.models import HRSPOMirMatch
from apps.services.matching import run_full_match
from apps.services.mir_without_po import PO_KNOWN_UNMATCHED, mir_without_po_rows
from apps.services.tests.test_manual_mir_match import _item, _mir, _po


def _match_for(item):
    return HRSPOMirMatch.objects.filter(po_line_item=item).first()


def _saved_mir_nos(match):
    return sorted(match.group_entries.values_list("mir_no", flat=True)) or [match.mir_entry.mir_no]


@pytest.mark.django_db
class TestEveryReceiptCitingThePoIsSaved:
    def test_four_receipts_are_all_saved_and_compared_as_one_total(self):
        po = _po("3000001174", vendor_name="B.P. Chemicals")
        item = _item(po, description="CARBON BLACK N330", qty=Decimal("200000"), net_price=Decimal("114"))
        for n, ref in (("33/09", "1"), ("35/09", "2"), ("48/09", "3"), ("55/09", "4")):
            _mir(n, ref, party_name="B.P. Chemicals", po_number_raw="3000001174.0",
                 material_description="Carbon Black N-330", qty=Decimal("16100"), rate=Decimal("114"))

        run_full_match()
        match = _match_for(item)

        assert _saved_mir_nos(match) == ["33/09", "35/09", "48/09", "55/09"]
        # 64,400 of 200,000 received.
        assert match.qty_diff_pct == Decimal("67.80")
        assert match.qty_over_delivered is False

    def test_a_receipt_at_a_different_rate_still_counts_and_is_flagged(self):
        """The 2% rate tolerance used to drop it silently. A price that moved
        between order and invoice is a rate mismatch to fix, not a reason the
        delivery did not happen."""
        po = _po("3000009100")
        item = _item(po, qty=Decimal("2000"), net_price=Decimal("100"))
        _mir("M-1", "1", po_number_raw=po.po_number, qty=Decimal("1000"), rate=Decimal("100"))
        _mir("M-2", "2", po_number_raw=po.po_number, qty=Decimal("1000"), rate=Decimal("112"))

        run_full_match()
        match = _match_for(item)

        assert _saved_mir_nos(match) == ["M-1", "M-2"]
        assert match.qty_diff_pct == Decimal("0.00")
        assert match.rate_mismatched is True

    def test_over_150_percent_counts_everything_and_flags_over_delivery(self):
        """Grouping used to switch itself off over 150% of ordered, leaving
        ONE receipt - Vapi 1000001573 read "90.9% short" on an order that had
        been over-delivered."""
        po = _po("3000009200")
        item = _item(po, qty=Decimal("1000"), net_price=Decimal("100"))
        for n in range(4):
            _mir(f"M-{n}", str(n), po_number_raw=po.po_number, qty=Decimal("500"), rate=Decimal("100"))

        run_full_match()
        match = _match_for(item)

        assert len(_saved_mir_nos(match)) == 4
        assert match.qty_diff_pct == Decimal("100.00")
        assert match.qty_over_delivered is True

    def test_a_receipt_another_line_wanted_does_not_collapse_the_group(self):
        """Silica 3000001085 on Render: a receipt with a typo'd PO number fitted
        two orders, the other order's group took it, and this order kept ONE
        of its own four receipts ("90% short")."""
        po_a = _po("3000009301")
        item_a = _item(po_a, qty=Decimal("4000"), net_price=Decimal("33"))
        po_b = _po("3000009302")
        _item(po_b, qty=Decimal("4000"), net_price=Decimal("33"))
        for n in range(4):
            _mir(f"A-{n}", f"a{n}", po_number_raw=po_a.po_number, qty=Decimal("1000"), rate=Decimal("33"))
        for n in range(3):
            _mir(f"B-{n}", f"b{n}", po_number_raw=po_b.po_number, qty=Decimal("1000"), rate=Decimal("33"))
        _mir("TYPO", "t", po_number_raw="30000093", qty=Decimal("1000"), rate=Decimal("33"))

        run_full_match()

        assert _saved_mir_nos(_match_for(item_a)) == ["A-0", "A-1", "A-2", "A-3"]

    def test_a_mistyped_number_from_another_supplier_does_not_attach(self):
        """Identification still applies: a row naming this PO but agreeing on
        neither vendor nor material (MIR 74/06's shape) is not taken."""
        po = _po("3000009400")
        item = _item(po, qty=Decimal("1000"), net_price=Decimal("100"))
        _mir("OK", "1", po_number_raw=po.po_number)
        _mir("WRONG", "2", po_number_raw=po.po_number, party_name="Unrelated Traders",
             material_description="Titanium Dioxide")

        run_full_match()

        assert _saved_mir_nos(_match_for(item)) == ["OK"]


@pytest.mark.django_db
class TestLinesOfOneOrderShareItsReceipts:
    def test_duplicate_lines_each_keep_a_receipt(self):
        """An order listing one material on two lines, one receipt each. The
        first version sent both receipts to whichever line's wording scored
        higher and left the other line empty (2 Achhad, 2 Vapi lines)."""
        po = _po("3000009500")
        line_1 = _item(po, qty=Decimal("1050"), item_id="1")
        line_2 = _item(po, qty=Decimal("1050"), item_id="2")
        _mir("R-1", "1", po_number_raw=po.po_number, qty=Decimal("1050"))
        _mir("R-2", "2", po_number_raw=po.po_number, qty=Decimal("1050"))

        run_full_match()

        assert len(_saved_mir_nos(_match_for(line_1))) == 1
        assert len(_saved_mir_nos(_match_for(line_2))) == 1
        assert set(_saved_mir_nos(_match_for(line_1)) + _saved_mir_nos(_match_for(line_2))) == {"R-1", "R-2"}

    def test_extra_receipts_go_to_the_line_whose_material_they_are(self):
        po = _po("3000009600")
        sbr = _item(po, description="SBR 1502", qty=Decimal("3000"), item_id="1")
        zinc = _item(po, description="Zinc Oxide", qty=Decimal("1000"), item_id="2")
        for n in range(3):
            _mir(f"S-{n}", f"s{n}", po_number_raw=po.po_number, material_description="SBR 1502")
        _mir("Z-0", "z0", po_number_raw=po.po_number, material_description="Zinc Oxide")

        run_full_match()

        assert _saved_mir_nos(_match_for(sbr)) == ["S-0", "S-1", "S-2"]
        assert _saved_mir_nos(_match_for(zinc)) == ["Z-0"]


@pytest.mark.django_db
class TestGroupedReceiptsReadAsMatchedEverywhere:
    def test_a_grouped_receipt_is_not_listed_as_waiting_on_a_match(self):
        po = _po("3000009700")
        _item(po, qty=Decimal("2000"))
        _mir("G-1", "1", po_number_raw=po.po_number)
        _mir("G-2", "2", po_number_raw=po.po_number)

        run_full_match()

        from apps.services import matching
        unmatched = [r for r in mir_without_po_rows(matching.MATCH_CONFIG.mir_model, matching.MATCH_CONFIG) if r["bucket"] == PO_KNOWN_UNMATCHED]
        assert unmatched == [], "both receipts are matched; the second used to be listed here"

    def test_links_are_rebuilt_not_accumulated(self):
        po = _po("3000009800")
        item = _item(po, qty=Decimal("2000"))
        _mir("K-1", "1", po_number_raw=po.po_number)
        second = _mir("K-2", "2", po_number_raw=po.po_number)

        run_full_match()
        second.is_active = False
        second.save(update_fields=["is_active"])
        run_full_match()

        assert _saved_mir_nos(_match_for(item)) == ["K-1"]


@pytest.mark.django_db
class TestAnnotatedOrders:
    def test_receipts_citing_the_bare_number_attach_to_an_annotated_order(self):
        """The master CSV can write "1000001462 (Changed Purchase Order)"
        while MIR writes 1000001462; 17 Vapi receipts linked to nothing."""
        po = _po("3000009900 (Changed Purchase Order)")
        item = _item(po, qty=Decimal("2000"))
        _mir("C-1", "1", po_number_raw="3000009900", party_name="Somebody Else Ltd")
        _mir("C-2", "2", po_number_raw="3000009900.0", party_name="Somebody Else Ltd")

        run_full_match()
        match = _match_for(item)

        # Vendor disagrees, so only PO number + material identify them - which
        # needs the PO number to count at all.
        assert _saved_mir_nos(match) == ["C-1", "C-2"]
        assert match.po_number_matched is True
