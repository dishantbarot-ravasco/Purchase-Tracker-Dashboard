"""
Integration tests for the dismiss/override-a-flagged-match endpoint
(apps/services/match_dismiss.py, wired per-plant in hrs_views.py/
achhad_views.py/vapi_views.py) - added 2026-09-04 to close the gap
apps/api/permissions.py's IsEditor docstring had been describing since the
auth pass ("dismissing a flagged match once that endpoint exists").

HRS only - matches this codebase's convention of testing one plant's copy
directly for logic shared byte-for-byte across all three (see
test_matching.py's own module docstring for the same reasoning applied to
the matching engines).
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSMIREntry, HRSMirStockMatch, HRSPOLineItem, HRSPOMirMatch, HRSPurchaseOrder, HRSStockLot


def _make_po_mir_match():
    po = HRSPurchaseOrder.objects.create(
        po_drive_folder_name="1000009999", po_number="1000009999", vendor_name="Test Vendor Ltd",
    )
    # Description/qty/rate/value all line up with the MIR entry below so
    # run_full_match() actually re-forms this match above MATCH_THRESHOLD -
    # needed for test_editor_can_dismiss_with_reason_and_it_survives_rematch,
    # which asserts a dismissal isn't lost when the match is recomputed.
    item = HRSPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="100", net_price="1.5", net_value="150",
    )
    mir = HRSMIREntry.objects.create(
        mir_no="MIR-1", party_name="Test Vendor Ltd", material_description="Widget",
        qty="100", rate="1.5", taxable_value="150", source_row_ref="10",
    )
    return HRSPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9",
        qty_diff_pct="12.00", is_flagged=True,
    )


def _make_mir_stock_match():
    mir = HRSMIREntry.objects.create(mir_no="MIR-2", party_name="Vendor B", material_description="Gadget", source_row_ref="11")
    lot = HRSStockLot.objects.create(description="Gadget", party_name="Vendor B", source_row_ref="20")
    return HRSMirStockMatch.objects.create(mir_entry=mir, stock_lot=lot, rate_diff_pct="8.00", is_flagged=True)


@pytest.mark.django_db
class TestDismissPoMirMatch:
    def setup_method(self):
        self.match = _make_po_mir_match()
        self.url = f"/api/matches/po-mir/{self.match.id}/dismiss"

    def test_viewer_cannot_dismiss(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.patch(self.url, {"dismissed": True}, format="json")
        assert response.status_code == 403
        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is False

    def test_editor_can_dismiss_with_reason_and_it_survives_rematch(self):
        client = APIClient()
        editor = make_user(email="e@ravasco.com", role="editor")
        client.force_authenticate(user=editor)

        response = client.patch(self.url, {"dismissed": True, "reason": "Verified manually, partial delivery"}, format="json")
        assert response.status_code == 200
        assert response.json()["dismissedByOverride"] is True

        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is True
        assert self.match.dismissed_reason == "Verified manually, partial delivery"
        assert self.match.dismissed_by_id == editor.user_id
        assert self.match.dismissed_at is not None

        # run_full_match()'s update_or_create defaults dict never touches
        # dismissed_* - re-matching must not silently clear a dismissal.
        from apps.services.matching import run_full_match
        run_full_match()
        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is True

    def test_reinstate_clears_dismissal_fields(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))
        client.patch(self.url, {"dismissed": True, "reason": "test"}, format="json")

        response = client.patch(self.url, {"dismissed": False}, format="json")
        assert response.status_code == 200
        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is False
        assert self.match.dismissed_reason == ""
        assert self.match.dismissed_by is None
        assert self.match.dismissed_at is None

    def test_unknown_match_id_returns_404(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.patch("/api/matches/po-mir/999999/dismiss", {"dismissed": True}, format="json")
        assert response.status_code == 404

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor", plants=["achhad"]))
        response = client.patch(self.url, {"dismissed": True}, format="json")
        assert response.status_code == 403


@pytest.mark.django_db
class TestDismissMirStockMatch:
    def setup_method(self):
        self.match = _make_mir_stock_match()
        self.url = f"/api/matches/mir-stock/{self.match.id}/dismiss"

    def test_editor_can_dismiss(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor"))
        response = client.patch(self.url, {"dismissed": True, "reason": "Known rounding diff"}, format="json")
        assert response.status_code == 200
        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is True
        assert self.match.dismissed_reason == "Known rounding diff"
