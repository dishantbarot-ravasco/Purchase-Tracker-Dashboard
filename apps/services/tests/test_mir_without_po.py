"""
Tests for apps/services/mir_without_po.py - the three-bucket split behind the
"purchased without a PO" drill-down.

Weighted towards `classify_mir_row()`, which is where the business meaning
lives and which is deliberately dependency-free so it tests as plain Python
(same split as test_no_po_vendors.py over the registry). The DB-touching half
gets the two properties a summary can silently break: that the NO_PO bucket
still reconciles exactly with the badge `purchases_without_po_summary()`
feeds, and that the whole thing costs a fixed number of queries rather than
one per row.
"""

import pytest

from apps.services.mir_without_po import (
    NO_PO,
    PO_KNOWN_UNMATCHED,
    PO_UNKNOWN,
    classify_mir_row,
    mir_without_po_rows,
    mir_without_po_summary,
)

KNOWN = frozenset({"3000001081", "1000001445"})


class TestClassifyMirRow:
    def test_a_blank_po_column_is_no_po(self):
        assert classify_mir_row("", "Some Supplier Pvt Ltd", False, KNOWN) == NO_PO

    def test_a_junk_po_reference_is_no_po(self):
        """`is_usable_po_reference()` rejects the literal sentinels plant
        staff type into that column. Those rows have no order named, so they
        belong with the blanks - not in a "waiting" bucket, where somebody
        would sit watching for a PO that is never coming."""
        for junk in ("VERBAL", "NIL", "-", "N/A"):
            assert classify_mir_row(junk, "Some Supplier Pvt Ltd", False, KNOWN) == NO_PO, junk

    def test_a_no_po_row_stays_listed_even_when_the_matcher_matched_it(self):
        """The material tier needs no PO number, so a NO_PO row can be fully
        reconciled and still be a purchase made without an order - which is
        the thing being driven down. Dropping it here would also make this
        module's NO_PO total stop agreeing with the badge."""
        assert classify_mir_row("", "Some Supplier Pvt Ltd", True, KNOWN) == NO_PO

    def test_an_order_we_do_not_hold_is_po_unknown(self):
        assert classify_mir_row("3000009999", "Some Supplier Pvt Ltd", False, KNOWN) == PO_UNKNOWN

    def test_an_order_we_hold_is_po_known_unmatched(self):
        assert classify_mir_row("3000001081", "Some Supplier Pvt Ltd", False, KNOWN) == PO_KNOWN_UNMATCHED

    def test_a_matched_row_that_names_a_po_is_in_no_bucket(self):
        """Reconciled and it names its order - there is nothing to show."""
        assert classify_mir_row("3000001081", "Some Supplier Pvt Ltd", True, KNOWN) is None

    def test_an_inter_plant_transfer_is_in_no_bucket_at_all(self):
        """Not a purchase, will never have a PO, and at ~390 rows at Achhad
        alone it would bury every other bucket. Counted separately by the
        summary rather than dropped silently."""
        assert classify_mir_row("", "Ravasco Transmission & Packing Pvt Ltd", False, KNOWN) is None

    def test_the_matcher_decides_what_a_known_po_is_not_string_equality(self):
        """HRS writes its legacy slashed series several ways, so the drill-down
        borrows `_names_known_po()` rather than comparing strings. If this ever
        regresses to `raw in known_pos`, ~120 HRS rows move into PO_UNKNOWN and
        somebody goes upstream to chase orders already in the database."""
        known = frozenset({"HRS/HO/26-27/003"})
        assert classify_mir_row("HRS/HO/26-27/003", "A Supplier", False, known) == PO_KNOWN_UNMATCHED
        assert classify_mir_row("Hrs/0003/2026-27", "A Supplier", False, known) == PO_KNOWN_UNMATCHED


@pytest.mark.django_db
class TestSummaryAgainstTheDatabase:
    def _config(self):
        from apps.services.matching import MATCH_CONFIG

        return MATCH_CONFIG

    def test_the_no_po_bucket_matches_the_badge_exactly(self):
        """The badge (`purchases_without_po_summary`) and this drill-down
        answer the same question about the same rows and are computed by two
        different functions. A reader who clicks a badge saying 193 and lands
        on a tab saying 187 has no way to tell which is wrong, so this pins
        them together."""
        from apps.core.models import HRSMIREntry
        from apps.services.no_po_vendors import purchases_without_po_summary

        badge = purchases_without_po_summary(HRSMIREntry)
        summary = mir_without_po_summary(HRSMIREntry, self._config())
        assert summary["buckets"][NO_PO]["rowCount"] == badge["total"]

    def test_every_returned_row_carries_a_known_bucket(self):
        from apps.core.models import HRSMIREntry

        rows = mir_without_po_rows(HRSMIREntry, self._config())
        assert all(r["bucket"] in (NO_PO, PO_UNKNOWN, PO_KNOWN_UNMATCHED) for r in rows)

    def test_the_query_count_does_not_grow_with_the_table(self, django_assert_max_num_queries):
        """A fixed budget, not one query per row: this backs a panel a user
        can reopen freely, and the MIR tables grow every month. Asserted as a
        ceiling rather than an exact number so an ordinary refactor does not
        fail it while a reintroduced N+1 still does."""
        from apps.core.models import HRSMIREntry

        with django_assert_max_num_queries(8):
            mir_without_po_rows(HRSMIREntry, self._config())

    def test_rows_and_summary_agree_on_every_bucket_count(self):
        """The summary is built from the rows, and the API returns both - so
        a panel tab labelled "(41)" must hold 41 rows."""
        from apps.core.models import HRSMIREntry

        rows = mir_without_po_rows(HRSMIREntry, self._config())
        summary = mir_without_po_summary(HRSMIREntry, self._config(), rows)
        for key, bucket in summary["buckets"].items():
            assert bucket["rowCount"] == sum(1 for r in rows if r["bucket"] == key), key
        assert summary["total"] == len(rows)
