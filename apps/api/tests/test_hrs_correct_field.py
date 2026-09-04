"""
Integration tests for the Domestic PO inline "Edit Everywhere" endpoint
(apps/api/routers/hrs_views.py's correct_field) - the same pattern as
test_imports_api.py's TestCorrectField, extended to also cover the new
per-plant PTUser.plants scoping (apps/api/permissions.py's
user_can_edit_plant()) that Import's own correct_field also now enforces.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import DomesticPOCorrection, HRSPOLineItem, HRSPurchaseOrder


def _make_po(po_number="1000001511", **overrides):
    """Build a minimal real HRSPurchaseOrder for correct_field to edit."""
    defaults = dict(
        po_drive_folder_name=po_number,
        po_number=po_number,
        vendor_name="Test Vendor Ltd",
        vendor_gstin="27AAPFU0939F1ZV",
        currency="INR",
    )
    defaults.update(overrides)
    return HRSPurchaseOrder.objects.create(**defaults)


def _make_item(po, item_id="1", **overrides):
    """Build a minimal real HRSPOLineItem attached to `po`, for correct_field
    tests that target a line-item field instead of a PO-level field."""
    defaults = dict(purchase_order=po, item_id=item_id, description="Widget", qty="100", net_price="1.5")
    defaults.update(overrides)
    return HRSPOLineItem.objects.create(**defaults)


@pytest.mark.django_db
class TestHrsCorrectField:
    def setup_method(self):
        self.po = _make_po()
        self.item = _make_item(self.po)
        self.url = f"/api/purchase-orders/{self.po.po_number}/fields"

    def test_viewer_cannot_correct(self):
        """A viewer must be forbidden from editing a PO field, and the field
        must remain unchanged after the rejected attempt."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.patch(self.url, {"field": "vendor_name", "value": "New Name"}, format="json")
        assert response.status_code == 403
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Test Vendor Ltd"

    def test_editor_can_correct_po_field_and_audit_row_is_written(self):
        """An editor can correct a PO-level field, and the change is recorded
        in a DomesticPOCorrection audit row with old/new values and who made it."""
        client = APIClient()
        editor = make_user(email="e@ravasco.com", role="editor")
        client.force_authenticate(user=editor)

        response = client.patch(self.url, {"field": "vendor_name", "value": "Corrected Vendor"}, format="json")
        assert response.status_code == 200

        self.po.refresh_from_db()
        assert self.po.vendor_name == "Corrected Vendor"

        correction = DomesticPOCorrection.objects.get()
        assert correction.plant == "HRS"
        assert correction.field_name == "vendor_name"
        assert correction.old_value == "Test Vendor Ltd"
        assert correction.new_value == "Corrected Vendor"
        assert correction.corrected_by_email == "e@ravasco.com"

    def test_editor_can_correct_item_field(self):
        """An editor can also correct a field on a line item (identified by
        itemId), not just PO-level fields."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))

        response = client.patch(self.url, {"itemId": self.item.item_id, "field": "qty", "value": "95"}, format="json")
        assert response.status_code == 200
        self.item.refresh_from_db()
        assert self.item.qty == 95

    def test_invalid_decimal_value_returns_400_not_500(self):
        """Regression test for a real bug, found and fixed 2026-09-04:
        Decimal(str(raw_value)) raises decimal.InvalidOperation for
        non-numeric input, which is an ArithmeticError, not a ValueError -
        _coerce_value's caller only caught (TypeError, ValueError), so typing
        "abc" into a qty field crashed with an unhandled 500 instead of a
        clean 400. Fixed by translating InvalidOperation into ValueError
        inside _coerce_value itself, so the existing outer except clause
        catches it like any other bad input. Also confirms the qty value is
        left unchanged after the rejected write."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2b@ravasco.com", role="editor"))
        response = client.patch(self.url, {"itemId": self.item.item_id, "field": "qty", "value": "not-a-number"}, format="json")
        assert response.status_code == 400
        self.item.refresh_from_db()
        assert self.item.qty == 100

    def test_rejects_non_editable_field(self):
        """po_number is not in the editable-field allow-list (it's an
        identity field, not a correctable one) - attempting to set it must
        400 rather than silently succeed."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "po_number", "value": "hacked"}, format="json")
        assert response.status_code == 400

    def test_invalid_gstin_saves_with_a_warning(self):
        """A malformed GSTIN is still saved (a human may be correcting toward
        a genuinely unusual real value) but the response carries a warning so
        the editor knows the format check failed."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "vendor_gstin", "value": "NOT-A-GSTIN"}, format="json")
        assert response.status_code == 200
        assert "warning" in response.json()
        self.po.refresh_from_db()
        assert self.po.vendor_gstin == "NOT-A-GSTIN"

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        """An editor whose plants list is ["achhad"] must not be able to
        correct an HRS PO's field."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor", plants=["achhad"]))
        response = client.patch(self.url, {"field": "vendor_name", "value": "Nope"}, format="json")
        assert response.status_code == 403
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Test Vendor Ltd"

    def test_editor_scoped_to_this_plant_can_correct(self):
        """The mirror case of the test above: an editor explicitly scoped to
        ["hrs"] can correct this HRS PO's field - confirms scoping isn't
        accidentally denying everyone, only the wrong plant."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e6@ravasco.com", role="editor", plants=["hrs"]))
        response = client.patch(self.url, {"field": "vendor_name", "value": "Scoped OK"}, format="json")
        assert response.status_code == 200
        self.po.refresh_from_db()
        assert self.po.vendor_name == "Scoped OK"

    def test_rematch_trigger_field_does_not_error_with_no_mir_data(self):
        """Correcting a field that's in _REMATCH_TRIGGER_FIELDS (qty) fires
        run_full_match() as a side effect - this must not blow up even when
        this plant otherwise has no MIR data to match against."""
        client = APIClient()
        client.force_authenticate(user=make_user(email="e7@ravasco.com", role="editor"))
        response = client.patch(
            self.url, {"itemId": self.item.item_id, "field": "qty", "value": "42"}, format="json"
        )
        assert response.status_code == 200
