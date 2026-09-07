"""
Characterization tests for apps/api/routers/achhad_views.py's
dismiss_po_mir_match/dismiss_mir_stock_match - written against today's
UNMODIFIED code as a safety net before the planned _domestic_base.py
extraction. Mirrors test_dismiss_match.py's HRS-only suite (that file's own
docstring explains why only HRS was tested directly for logic shared
byte-for-byte across all three plants) - this file closes that gap for
Achhad specifically, ahead of the refactor that will make all three share
one implementation. Note RTPAchhadRMLot has no vendor column at all (see
CLAUDE.md's "Per-plant models" section) - the MIR<->Stock match fixture
below omits any vendor field accordingly.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import (
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadDomesticPOLineItem,
    RTPAchhadPOMirMatch,
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadRMLot,
)


def _make_po_mir_match():
    po = RTPAchhadDomesticPurchaseOrder.objects.create(
        po_drive_folder_name="3000009999", po_number="3000009999", vendor_name="Test Vendor Ltd",
    )
    item = RTPAchhadDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="100", net_price="1.5", net_value="150",
    )
    mir = RTPAchhadMIREntry.objects.create(
        mir_no="MIR-1", party_name="Test Vendor Ltd", material_description="Widget",
        qty="100", rate="1.5", taxable_value="150", source_row_ref="10",
    )
    return RTPAchhadPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9",
        qty_diff_pct="12.00", is_flagged=True,
    )


def _make_mir_stock_match():
    mir = RTPAchhadMIREntry.objects.create(mir_no="MIR-2", party_name="Vendor B", material_description="Gadget", source_row_ref="11")
    lot = RTPAchhadRMLot.objects.create(description="Gadget", source_row_ref="20")
    return RTPAchhadMirStockMatch.objects.create(mir_entry=mir, stock_lot=lot, rate_diff_pct="8.00", is_flagged=True)


@pytest.mark.django_db
class TestAchhadDismissPoMirMatch:
    def setup_method(self):
        self.match = _make_po_mir_match()
        self.url = f"/api/achhad/matches/po-mir/{self.match.id}/dismiss"

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

        from apps.services.matching_achhad import run_full_match
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
        response = client.patch("/api/achhad/matches/po-mir/999999/dismiss", {"dismissed": True}, format="json")
        assert response.status_code == 404

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor", plants=["hrs"]))
        response = client.patch(self.url, {"dismissed": True}, format="json")
        assert response.status_code == 403


@pytest.mark.django_db
class TestAchhadDismissMirStockMatch:
    def setup_method(self):
        self.match = _make_mir_stock_match()
        self.url = f"/api/achhad/matches/mir-stock/{self.match.id}/dismiss"

    def test_editor_can_dismiss(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor"))
        response = client.patch(self.url, {"dismissed": True, "reason": "Known rounding diff"}, format="json")
        assert response.status_code == 200
        self.match.refresh_from_db()
        assert self.match.dismissed_by_override is True
        assert self.match.dismissed_reason == "Known rounding diff"
