"""
Integration tests for the canonical Category/Subcategory lookup wired into
GET /api/materials (_domestic_base.py's `_category_reference_map()`/
`_lot_dict()`) - see MaterialCategoryReference's own docstring (apps/core/
models.py) for the full design/rationale (2026-09-08).
"""
import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSRMLot, MaterialCategoryReference, RTPAchhadRMLot


@pytest.mark.django_db
class TestCategoryReferenceLookup:
    def test_matched_lot_gets_canonical_category_not_raw_value(self):
        """The lot's own raw category ("Misc") is noisy/wrong - the
        canonical reference value must win, not just fill a gap."""
        MaterialCategoryReference.objects.create(
            description="Carbon Black N220", normalized_description="carbon black n220",
            category="Carbon Black", subcategory="CARBON BLACK", subcategory_code="RM-CB001",
        )
        HRSRMLot.objects.create(description="Carbon Black N220", category="Misc", basic_rate="10")
        client = APIClient()
        client.force_authenticate(user=make_user(email="cat1@ravasco.com", role="viewer"))
        [material] = client.get("/api/materials").json()["materials"]
        assert material["category"] == "Carbon Black"
        assert material["subCategory"] == "CARBON BLACK"

    def test_matching_is_case_and_punctuation_insensitive(self):
        """Reuses normalize_material() - the same loose normalization
        MIR<->Stock matching already uses, so real-world casing/punctuation
        differences between the reference list and a plant's own Stock file
        don't produce false misses."""
        MaterialCategoryReference.objects.create(
            description="Natural Rubber ISNR-20", normalized_description="natural rubber isnr 20",
            category="Natural Rubber", subcategory="NATURAL RUBBER ISNR", subcategory_code="RM-NR001",
        )
        HRSRMLot.objects.create(description="natural   rubber, isnr 20!!", category="", basic_rate="1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="cat2@ravasco.com", role="viewer"))
        [material] = client.get("/api/materials").json()["materials"]
        assert material["category"] == "Natural Rubber"

    def test_unmatched_lot_falls_back_to_uncategorized_not_raw_noise(self):
        """No reference row at all for this material - even a populated raw
        category must not leak through, since that's exactly the noisy
        per-row data this feature replaces."""
        HRSRMLot.objects.create(description="Some Brand New Material", category="XK-91-random", basic_rate="1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="cat3@ravasco.com", role="viewer"))
        [material] = client.get("/api/materials").json()["materials"]
        assert material["category"] == "Uncategorized"
        assert material["subCategory"] == ""

    def test_reference_table_is_shared_across_plants(self):
        """One MaterialCategoryReference row matches lots from two different
        plants - it's shared company-wide data, not per-plant."""
        MaterialCategoryReference.objects.create(
            description="Zinc Oxide", normalized_description="zinc oxide",
            category="Rubber Chemicals & Additives", subcategory="RUBBER CHEMICAL ACTI", subcategory_code="RM-RC004",
        )
        HRSRMLot.objects.create(description="Zinc Oxide", category="", basic_rate="1")
        RTPAchhadRMLot.objects.create(description="Zinc Oxide", category="", rate="1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="cat4@ravasco.com", role="viewer"))
        [hrs_material] = client.get("/api/materials").json()["materials"]
        [achhad_material] = client.get("/api/achhad/materials").json()["materials"]
        assert hrs_material["category"] == achhad_material["category"] == "Rubber Chemicals & Additives"

    def test_editing_raw_category_via_edit_everywhere_does_not_change_the_canonical_display(self):
        """The 'Edit Everywhere' correction (apps/api/tests/test_material_correct_field.py)
        still writes to the lot's own raw `category` column - it must keep
        working (this test doesn't touch that endpoint), but the LIST
        display stays governed by the reference table regardless of what
        the raw column holds, matched or not."""
        MaterialCategoryReference.objects.create(
            description="Sulphur Powder", normalized_description="sulphur powder",
            category="Rubber Chemicals & Additives", subcategory="RUBBER CHEMICAL CURA", subcategory_code="RM-RC006",
        )
        lot = HRSRMLot.objects.create(description="Sulphur Powder", category="whatever a human typed", basic_rate="1")
        client = APIClient()
        client.force_authenticate(user=make_user(email="cat5@ravasco.com", role="viewer"))
        [material] = client.get("/api/materials").json()["materials"]
        assert material["category"] == "Rubber Chemicals & Additives"
        lot.refresh_from_db()
        assert lot.category == "whatever a human typed"  # raw DB value untouched
