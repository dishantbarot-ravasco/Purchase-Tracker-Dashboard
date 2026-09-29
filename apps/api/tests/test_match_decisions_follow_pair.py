"""
Review verdicts and match dismissals are about the PAIR a match joins, not
the match row (apps/services/match_pairs.py, 2026-09-29).

run_full_match() deletes a match row when its line matches nothing on a run
and creates a new one, with a new id, when the same pair matches again. A
verdict keyed on the row id then scored as stale and the pair was served for
review again; a dismissal stored only on the row came back undismissed.
Every test here recreates a row for the same pair under a new id, so each
fails if a decision is tied to the row id again.
"""

import importlib

import pytest
from django.apps import apps as django_apps
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSRMLot,
    MatchDismissal,
    MatchReview,
    SyncRun,
)
from apps.services import match_pairs
from apps.services.match_accuracy import build_report
from apps.services.matching import MATCH_CONFIG, run_full_match

_MIGRATION = "apps.core.migrations.0075_match_decisions_keyed_on_pair"


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _po_mir_match():
    """A PO line and MIR row that run_full_match() pairs on its own (same
    shape as test_dismiss_match.py's fixture)."""
    po = HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name="1000009999", po_number="1000009999", vendor_name="Test Vendor Ltd",
    )
    item = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="100", net_price="1.5", net_value="150",
    )
    mir = HRSMIREntry.objects.create(
        mir_no="MIR-1", party_name="Test Vendor Ltd", material_description="Widget",
        qty="100", rate="1.5", taxable_value="150", source_row_ref="10",
    )
    return HRSPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier="po_number", match_score="0.9",
        qty_diff_pct="12.00", is_flagged=True,
    )


def _recreate(match, **changes):
    """Delete the row and create a fresh one (new id) - what a run that
    drops the pair and a later run that re-forms it leave behind."""
    fields = dict(po_line_item_id=match.po_line_item_id, mir_entry_id=match.mir_entry_id,
                  tier=match.tier, match_score=match.match_score, is_flagged=True)
    fields.update(changes)
    old_id = match.id
    match.delete()
    fresh = HRSPOMirMatch.objects.create(**fields)
    assert fresh.id != old_id
    return fresh


def _submit(client, match, verdict="correct"):
    response = client.post("/api/review", {
        "plant": SyncRun.Plant.HRS, "matchType": MatchReview.MatchType.PO_MIR,
        "matchId": match.id, "verdict": verdict,
    }, format="json")
    assert response.status_code == 201
    return response


@pytest.mark.django_db
class TestVerdictFollowsPair:
    def test_submit_records_the_pair(self):
        match = _po_mir_match()
        _submit(_client(make_user(email="r@ravasco.com")), match)
        review = MatchReview.objects.get()
        assert (review.left_id, review.right_id) == (match.po_line_item_id, match.mir_entry_id)

    def test_verdict_survives_its_row_being_recreated(self):
        match = _po_mir_match()
        client = _client(make_user(email="r@ravasco.com"))
        _submit(client, match)
        _recreate(match)

        report = build_report()
        assert report["staleVerdicts"] == 0
        assert report["overall"]["correct"] == 1

        # Still reviewed: the queue does not serve the same pair again.
        response = client.get("/api/review/next")
        assert response.json().get("done") is True
        assert response.json()["progress"]["reviewed"] == 1

    def test_verdict_on_a_pair_the_matcher_no_longer_makes_is_stale(self):
        match = _po_mir_match()
        _submit(_client(make_user(email="r@ravasco.com")), match)
        other = HRSMIREntry.objects.create(mir_no="MIR-9", material_description="Widget", source_row_ref="99")
        _recreate(match, mir_entry_id=other.id)

        report = build_report()
        assert report["staleVerdicts"] == 1
        assert report["overall"]["n"] == 0


@pytest.mark.django_db
class TestDismissalFollowsPair:
    def _dismiss(self, match, dismissed=True):
        client = _client(make_user(email=f"e{dismissed}@ravasco.com", role="editor"))
        response = client.patch(f"/api/matches/po-mir/{match.id}/dismiss",
                                {"dismissed": dismissed, "reason": "Partial delivery, fine"}, format="json")
        assert response.status_code == 200

    def test_dismissing_records_the_pair(self):
        match = _po_mir_match()
        self._dismiss(match)
        dismissal = MatchDismissal.objects.get()
        assert (dismissal.plant, dismissal.match_type) == (SyncRun.Plant.HRS, MatchReview.MatchType.PO_MIR)
        assert (dismissal.left_id, dismissal.right_id) == (match.po_line_item_id, match.mir_entry_id)
        assert dismissal.dismissed_reason == "Partial delivery, fine"

    def test_dismissal_survives_a_rematch_that_recreates_the_row(self):
        match = _po_mir_match()
        self._dismiss(match)
        pair = (match.po_line_item_id, match.mir_entry_id)
        match.delete()  # a run where the line matched nothing

        run_full_match()

        fresh = HRSPOMirMatch.objects.get(po_line_item_id=pair[0])
        assert fresh.id != match.id
        assert fresh.mir_entry_id == pair[1]
        assert fresh.dismissed_by_override is True
        assert fresh.dismissed_reason == "Partial delivery, fine"
        assert fresh.dismissed_at is not None

    def test_undismissing_deletes_the_record_so_it_is_not_restored(self):
        match = _po_mir_match()
        self._dismiss(match)
        self._dismiss(match, dismissed=False)
        assert not MatchDismissal.objects.exists()

        fresh = _recreate(match)
        match_pairs.restore_dismissals(MATCH_CONFIG, [MatchReview.MatchType.PO_MIR])
        fresh.refresh_from_db()
        assert fresh.dismissed_by_override is False

    def test_a_different_pair_is_not_dismissed(self):
        match = _po_mir_match()
        self._dismiss(match)
        other = HRSMIREntry.objects.create(mir_no="MIR-9", material_description="Widget", source_row_ref="99")
        fresh = _recreate(match, mir_entry_id=other.id)

        match_pairs.restore_dismissals(MATCH_CONFIG, [MatchReview.MatchType.PO_MIR])
        fresh.refresh_from_db()
        assert fresh.dismissed_by_override is False

    def test_mir_stock_dismissal_is_restored_on_a_recreated_row(self):
        mir = HRSMIREntry.objects.create(mir_no="MIR-2", party_name="Vendor B", material_description="Gadget", source_row_ref="11")
        lot = HRSRMLot.objects.create(description="Gadget", party_name="Vendor B", source_row_ref="20")
        match = HRSMirStockMatch.objects.create(mir_entry=mir, stock_lot=lot, rate_diff_pct="8.00", is_flagged=True)
        client = _client(make_user(email="e@ravasco.com", role="editor"))
        assert client.patch(f"/api/matches/mir-stock/{match.id}/dismiss", {"dismissed": True}, format="json").status_code == 200

        match.delete()
        fresh = HRSMirStockMatch.objects.create(mir_entry=mir, stock_lot=lot, rate_diff_pct="8.00", is_flagged=True)
        restored = match_pairs.restore_dismissals(MATCH_CONFIG, [MatchReview.MatchType.MIR_STOCK])
        fresh.refresh_from_db()
        assert restored == 1
        assert fresh.dismissed_by_override is True


@pytest.mark.django_db
class TestBackfill:
    def test_existing_reviews_and_dismissals_are_carried_over(self):
        match = _po_mir_match()
        reviewer = make_user(email="r@ravasco.com")
        kept = MatchReview.objects.create(plant=SyncRun.Plant.HRS, match_type=MatchReview.MatchType.PO_MIR,
                                          match_id=match.id, reviewer=reviewer, verdict="correct")
        gone = MatchReview.objects.create(plant=SyncRun.Plant.HRS, match_type=MatchReview.MatchType.PO_MIR,
                                          match_id=999999, reviewer=reviewer, verdict="incorrect")
        HRSPOMirMatch.objects.filter(pk=match.pk).update(dismissed_by_override=True, dismissed_reason="ok")

        importlib.import_module(_MIGRATION).backfill(django_apps, None)

        kept.refresh_from_db()
        gone.refresh_from_db()
        assert (kept.left_id, kept.right_id) == (match.po_line_item_id, match.mir_entry_id)
        assert gone.left_id is None
        dismissal = MatchDismissal.objects.get()
        assert (dismissal.left_id, dismissal.right_id) == (match.po_line_item_id, match.mir_entry_id)
        assert dismissal.dismissed_reason == "ok"
        assert dismissal.dismissed_at is not None
