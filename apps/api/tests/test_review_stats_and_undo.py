"""
Integration tests for the Match Accuracy Programme's management panel and the
review screen's undo (apps/services/match_accuracy.py, exposed by
apps/api/routers/review_views.py's review_stats/undo_review/export_review_stats
and rendered by frontend/js/review-page.js's Accuracy tab).

What is pinned here is what the figures MEAN, not just that an endpoint
answers 200:
  - precision ignores "unsure", recall counts it against the total;
  - only the latest verdict per match counts, so a correction really corrects;
  - a verdict whose match row has since been deleted or re-pointed is dropped
    rather than scored, and is reported as stale;
  - every (plant, match type) cell exists even at n=0, because an unsampled
    cell is a hole in the evidence and a table that omits it reads as covered;
  - a reviewer can undo only their OWN verdict - nobody gets to quietly
    rewrite someone else's judgement out of the sample.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    MatchReview,
    SyncRun,
)


def _match(n, tier="po_number"):
    po = HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name=f"PO-{n}", po_number=f"PO-{n}", vendor_name="Vendor",
    )
    item = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="1", net_price="1", net_value="1",
    )
    mir = HRSMIREntry.objects.create(
        mir_no=f"MIR-{n}", party_name="Vendor", material_description="Widget",
        qty="1", rate="1", net="1", source_row_ref=str(n),
    )
    return HRSPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier=tier, match_score="0.9",
    )


def _review(match, verdict, reviewer, note=""):
    return MatchReview.objects.create(
        plant=SyncRun.Plant.HRS,
        match_type=MatchReview.MatchType.PO_MIR,
        match_id=match.id,
        reviewer=reviewer,
        verdict=verdict,
        note=note,
    )


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestAccuracyPanel:
    def test_precision_and_recall_treat_unsure_differently(self):
        """3 correct, 1 incorrect, 1 unsure: precision is 3/4 (unsure is not
        evidence the match was wrong), recall is 3/5 (it is also not a
        confident yes). Getting these the same way round is the whole
        reason the definitions are written down."""
        reviewer = make_user(email="r1@ravasco.com")
        for i, verdict in enumerate(["correct", "correct", "correct", "incorrect", "unsure"]):
            _review(_match(i), verdict, reviewer)

        report = _client(reviewer).get("/api/review/stats").data
        assert report["overall"]["n"] == 5
        assert report["overall"]["precision"] == pytest.approx(0.75)
        assert report["overall"]["recall"] == pytest.approx(0.6)

    def test_only_the_latest_verdict_per_match_counts(self):
        """A reviewer correcting their own earlier call must not be stuck
        with it - and must not be double-counted either."""
        reviewer = make_user(email="r2@ravasco.com")
        match = _match(1)
        _review(match, "incorrect", reviewer)
        _review(match, "correct", reviewer)

        report = _client(reviewer).get("/api/review/stats").data
        assert report["overall"]["n"] == 1
        assert report["overall"]["correct"] == 1
        assert report["overall"]["incorrect"] == 0
        assert report["reviewsRecorded"] == 2

    def test_a_verdict_whose_match_is_gone_is_stale_not_scored(self):
        """`match_*` runs delete and re-point pairs. A verdict about a pair
        that no longer exists is a statement about the past, so it is
        reported separately instead of quietly moving the precision."""
        reviewer = make_user(email="r3@ravasco.com")
        kept, dropped = _match(1), _match(2)
        _review(kept, "correct", reviewer)
        _review(dropped, "incorrect", reviewer)
        dropped.delete()

        report = _client(reviewer).get("/api/review/stats").data
        assert report["overall"]["n"] == 1
        assert report["overall"]["precision"] == pytest.approx(1.0)
        assert report["staleVerdicts"] == 1

    def test_every_plant_and_type_cell_exists_even_unsampled(self):
        """3 plants x 3 match types. A cell nobody has reviewed is the point
        of the table, not a row to omit."""
        reviewer = make_user(email="r4@ravasco.com")
        _review(_match(1), "correct", reviewer)

        report = _client(reviewer).get("/api/review/stats").data
        assert len(report["byPlantAndType"]) == 9
        unsampled = [row for row in report["byPlantAndType"] if row["n"] == 0]
        assert len(unsampled) == 8

    def test_small_sample_is_flagged(self):
        reviewer = make_user(email="r5@ravasco.com")
        for i in range(3):
            _review(_match(i), "correct", reviewer)
        report = _client(reviewer).get("/api/review/stats").data
        assert report["overall"]["smallSample"] is True
        assert report["minSample"] == 5

    def test_notes_and_reviewers_are_surfaced(self):
        """Reviewer notes were being written to the database and read by
        nobody - the panel is the first thing that shows them."""
        reviewer = make_user(email="r6@ravasco.com", full_name="Asha R")
        _review(_match(1), "unsure", reviewer, note="MIR description is a warehouse nickname")

        report = _client(reviewer).get("/api/review/stats").data
        assert report["reviewers"] == [{"reviewer": "Asha R", "count": 1}]
        assert report["notes"][0]["note"] == "MIR description is a warehouse nickname"
        assert report["notes"][0]["reviewer"] == "Asha R"

    def test_export_is_a_csv_of_the_same_figures(self):
        reviewer = make_user(email="r7@ravasco.com")
        _review(_match(1), "correct", reviewer)
        response = _client(reviewer).get("/api/review/stats/export")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        body = response.content.decode()
        assert "section,group,n,correct,incorrect,unsure,precision,recall,f1,small_sample" in body
        assert "Overall,All reviewed matches,1,1,0,0,1.0,1.0,1.0,yes" in body


@pytest.mark.django_db
class TestUndo:
    def test_reviewer_can_undo_their_own_verdict(self):
        reviewer = make_user(email="u1@ravasco.com")
        review = _review(_match(1), "correct", reviewer)
        response = _client(reviewer).delete(f"/api/review/{review.id}")
        assert response.status_code == 200
        assert response.data["progress"]["reviewed"] == 0
        assert not MatchReview.objects.filter(id=review.id).exists()

    def test_nobody_can_undo_someone_elses_verdict(self):
        """Not even an admin. The sample is a measurement harness; a verdict
        someone else recorded is not this user's to delete."""
        author = make_user(email="u2@ravasco.com")
        admin = make_user(email="u3@ravasco.com", role="admin")
        review = _review(_match(1), "correct", author)

        response = _client(admin).delete(f"/api/review/{review.id}")
        assert response.status_code == 404
        assert MatchReview.objects.filter(id=review.id).exists()


@pytest.mark.django_db
class TestProgress:
    def test_progress_counts_distinct_matches_not_rows(self):
        """Re-reviewing the same match is a correction, not progress toward
        the ~200-match sample."""
        reviewer = make_user(email="p1@ravasco.com")
        match = _match(1)
        _review(match, "incorrect", reviewer)
        _review(match, "correct", reviewer)

        response = _client(reviewer).get("/api/review/next")
        assert response.data["progress"] == {"reviewed": 1, "target": 200}
