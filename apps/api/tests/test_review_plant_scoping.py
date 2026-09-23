"""
Plant scoping on the review queue (apps/api/routers/review_views.py,
2026-09-23 audit pass).

Role stays open - any authenticated account may review, which is a recorded
decision (review_views.py's module docstring, CLAUDE.md's Match accuracy
section). Plant was simply never checked: next_review drew cards from every
plant regardless of PTUser.plants, so an account an admin had scoped to one
plant could read another plant's PO numbers, vendors, rates and MIR rows
through the review screen, and submit_review accepted a verdict on any plant.

Pinned here:
  - a scoped reviewer only ever receives cards for their own plants;
  - a scoped reviewer cannot record a verdict on another plant (403, no row);
  - an UNSCOPED account is unaffected - still sees and reviews every plant;
  - the SyncRun.Plant -> access-key mapping review_views keeps agrees with
    each plant's own _PlantConfig, so the two spellings cannot drift.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.routers import achhad_views, hrs_views, review_views, vapi_views
from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    MatchReview,
    RTPVapiDomesticPOLineItem,
    RTPVapiDomesticPurchaseOrder,
    RTPVapiMIREntry,
    RTPVapiPOMirMatch,
    SyncRun,
)


def _hrs_match(n):
    po = HRSDomesticPurchaseOrder.objects.create(po_drive_folder_name=f"H-{n}", po_number=f"H-{n}", vendor_name="Vendor")
    item = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="1", net_price="1", net_value="1",
    )
    mir = HRSMIREntry.objects.create(
        mir_no=f"HM-{n}", party_name="Vendor", material_description="Widget", qty="1", rate="1", net="1",
        source_row_ref=f"h{n}",
    )
    return HRSPOMirMatch.objects.create(po_line_item=item, mir_entry=mir, tier="po_number", match_score="0.9")


def _vapi_match(n):
    po = RTPVapiDomesticPurchaseOrder.objects.create(po_drive_folder_name=f"V-{n}", po_number=f"V-{n}", vendor_name="Vendor")
    item = RTPVapiDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="1", net_price="1", net_value="1",
    )
    mir = RTPVapiMIREntry.objects.create(
        mir_no=f"VM-{n}", party_name="Vendor", material_description="Widget", qty="1", rate="1",
        taxable_value="1", source_row_ref=f"v{n}",
    )
    return RTPVapiPOMirMatch.objects.create(po_line_item=item, mir_entry=mir, tier="po_number", match_score="0.9")


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_access_key_mapping_agrees_with_every_plant_config():
    """review_views keeps its own SyncRun.Plant -> access-key dict because it
    is a cross-plant router with no _PlantConfig of its own. Each plant's
    router already pairs the two spellings; if either side is ever renamed,
    this fails instead of the scoping silently checking the wrong key."""
    for router in (hrs_views, achhad_views, vapi_views):
        cfg = router._CONFIG
        assert review_views._ACCESS_KEY[cfg.syncrun_plant] == cfg.key
    assert set(review_views._ACCESS_KEY) == set(review_views._CONFIGS)


@pytest.mark.django_db
class TestReviewQueuePlantScoping:
    def test_a_scoped_reviewer_only_receives_their_own_plants_cards(self):
        for n in range(4):
            _hrs_match(n)
            _vapi_match(n)
        reviewer = make_user(email="hrs-only@ravasco.com", plants=["hrs"])

        response = _client(reviewer).get("/api/review/next")

        assert response.status_code == 200
        plants = {card["plant"] for card in response.json()["matches"]}
        assert plants == {SyncRun.Plant.HRS}, f"leaked cards from {plants - {SyncRun.Plant.HRS}}"

    def test_a_reviewer_scoped_away_from_every_populated_plant_is_told_done(self):
        """Only Vapi has matches and the reviewer may read only Achhad - there
        is genuinely nothing for them, which must read as 'done', not as a
        Vapi card."""
        _vapi_match(1)
        reviewer = make_user(email="achhad-only@ravasco.com", plants=["achhad"])

        response = _client(reviewer).get("/api/review/next")

        assert response.status_code == 200
        assert response.json()["done"] is True
        assert "matches" not in response.json()

    def test_a_scoped_reviewer_cannot_record_a_verdict_on_another_plant(self):
        match = _vapi_match(1)
        reviewer = make_user(email="hrs-only2@ravasco.com", plants=["hrs"])

        response = _client(reviewer).post(
            "/api/review",
            {"plant": SyncRun.Plant.RTP_VAPI, "matchType": MatchReview.MatchType.PO_MIR,
             "matchId": match.id, "verdict": "correct"},
            format="json",
        )

        assert response.status_code == 403
        assert not MatchReview.objects.exists()

    def test_a_scoped_reviewer_can_still_review_their_own_plant(self):
        match = _hrs_match(1)
        reviewer = make_user(email="hrs-only3@ravasco.com", plants=["hrs"])

        response = _client(reviewer).post(
            "/api/review",
            {"plant": SyncRun.Plant.HRS, "matchType": MatchReview.MatchType.PO_MIR,
             "matchId": match.id, "verdict": "correct"},
            format="json",
        )

        assert response.status_code == 201
        assert MatchReview.objects.filter(plant=SyncRun.Plant.HRS, match_id=match.id).count() == 1

    def test_an_unscoped_viewer_is_unaffected_and_sees_every_plant(self):
        """Empty plants means ALL plants - the common case must not change.
        A viewer, deliberately: role stays open on this screen."""
        for n in range(6):
            _hrs_match(n)
            _vapi_match(n)
        reviewer = make_user(email="everyone@ravasco.com", role="viewer")

        seen = set()
        for _ in range(8):  # the batch is random; a few draws make "both plants appear" reliable
            seen |= {card["plant"] for card in _client(reviewer).get("/api/review/next").json()["matches"]}

        assert seen == {SyncRun.Plant.HRS, SyncRun.Plant.RTP_VAPI}
