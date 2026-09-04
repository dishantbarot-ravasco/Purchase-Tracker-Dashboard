"""
Integration tests for the Raw Material Analysis modal's inline "Edit
Everywhere" endpoint (correct_material_field, added to hrs_views.py/
achhad_views.py/vapi_views.py alongside the existing PO correct_field) -
same pattern as test_hrs_correct_field.py, against a Stock lot instead of a
PO/line item.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSStockLot, MaterialCorrection, RTPAchhadStockLot


def _make_hrs_lot(**overrides):
    defaults = dict(description="Natural Rubber", category="Rubber", sub_category="Natural", basic_rate="120.5000")
    defaults.update(overrides)
    return HRSStockLot.objects.create(**defaults)


@pytest.mark.django_db
class TestHrsCorrectMaterialField:
    def setup_method(self):
        self.lot = _make_hrs_lot()
        self.url = f"/api/materials/{self.lot.id}/fields"

    def test_viewer_cannot_correct(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))
        response = client.patch(self.url, {"field": "category", "value": "Chemicals"}, format="json")
        assert response.status_code == 403
        self.lot.refresh_from_db()
        assert self.lot.category == "Rubber"

    def test_editor_can_correct_and_audit_row_is_written(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "category", "value": "Chemicals"}, format="json")
        assert response.status_code == 200

        self.lot.refresh_from_db()
        assert self.lot.category == "Chemicals"

        correction = MaterialCorrection.objects.get()
        assert correction.plant == "HRS"
        assert correction.lot_id == self.lot.id
        assert correction.field_name == "category"
        assert correction.old_value == "Rubber"
        assert correction.new_value == "Chemicals"
        assert correction.corrected_by_email == "e@ravasco.com"

    def test_editor_can_correct_decimal_rate_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "basic_rate", "value": "150.75"}, format="json")
        assert response.status_code == 200
        self.lot.refresh_from_db()
        assert str(self.lot.basic_rate) == "150.7500"

    def test_invalid_decimal_value_returns_400_not_500(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "basic_rate", "value": "not-a-number"}, format="json")
        assert response.status_code == 400

    def test_rejects_non_editable_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "todays_stock", "value": "999"}, format="json")
        assert response.status_code == 400

    def test_editor_scoped_to_a_different_plant_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor", plants=["achhad"]))
        response = client.patch(self.url, {"field": "category", "value": "Nope"}, format="json")
        assert response.status_code == 403
        self.lot.refresh_from_db()
        assert self.lot.category == "Rubber"

    def test_unknown_lot_returns_404(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e6@ravasco.com", role="editor"))
        response = client.patch("/api/materials/999999/fields", {"field": "category", "value": "X"}, format="json")
        assert response.status_code == 404


@pytest.mark.django_db
class TestAchhadCorrectMaterialField:
    """Achhad's RTPAchhadStockLot has a genuinely different editable-field
    set (rate/msl instead of HRS's basic_rate, no sub_category/uom/vendor at
    all - see achhad_views.py's own _MATERIAL_EDITABLE_FIELDS comment), so
    this is not just a copy-paste of the HRS test above - it exercises the
    per-plant field-name difference directly."""

    def setup_method(self):
        self.lot = RTPAchhadStockLot.objects.create(description="Carbon Black", category="Chemicals", rate="88.25")
        self.url = f"/api/achhad/materials/{self.lot.id}/fields"

    def test_editor_can_correct_rate_field(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "rate", "value": "90.00"}, format="json")
        assert response.status_code == 200
        self.lot.refresh_from_db()
        assert str(self.lot.rate) == "90.0000"

        correction = MaterialCorrection.objects.get()
        assert correction.plant == "RTP-ACHHAD"

    def test_basic_rate_is_not_a_valid_field_name_here(self):
        # HRS/Vapi's decimal rate field is named basic_rate; Achhad's own
        # model calls it rate - confirms the two plants' allow-lists don't
        # accidentally cross-accept each other's field names.
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor"))
        response = client.patch(self.url, {"field": "basic_rate", "value": "1"}, format="json")
        assert response.status_code == 400
