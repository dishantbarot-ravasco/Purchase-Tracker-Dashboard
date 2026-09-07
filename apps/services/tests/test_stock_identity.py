"""
Pure unit tests for apps/services/stock_identity.py (dependency-free, no
Django DB touched - same scope as test_parsers_common.py/test_validation.py,
see CLAUDE.md's "Test coverage is split into two deliberately different
scopes"). Covers lot_natural_key()'s composition rules and
OccurrenceCounter's sheet-order suffixing - see that module's own docstring
for the row-shift incident this replaces.
"""

from apps.services.stock_identity import OccurrenceCounter, lot_natural_key


class TestLotNaturalKey:
    def test_code_wins_over_description(self):
        """A real code (SAP/HSN) is used verbatim over the description -
        it survives a description being reworded in the sheet."""
        key = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        assert key == "SAP123|kedarmetals"

    def test_falls_back_to_normalized_description_when_code_blank(self):
        key = lot_natural_key(code="", description="Zinc Oxide", vendor="Kedar Metals")
        assert key == "zinc oxide|kedarmetals"

    def test_falls_back_to_normalized_description_when_code_none(self):
        key = lot_natural_key(code=None, description="Zinc Oxide", vendor="Kedar Metals")
        assert key == "zinc oxide|kedarmetals"

    def test_casing_and_punctuation_drift_collapses_to_one_key(self):
        """Two different renderings of the same code/vendor must produce
        the identical key - a rewording in the sheet can't fork one lot's
        history into two."""
        a = lot_natural_key(code="SAP-123", description="Zinc Oxide", vendor="Kedar Metals Pvt Ltd")
        b = lot_natural_key(code="SAP-123", description="ZINC   OXIDE", vendor="KEDAR METALS PVT. LTD.")
        assert a == b

    def test_location_is_never_folded_into_the_key(self):
        """A lot relocating between zones/locations must keep one
        continuous history - location is accepted for interface
        completeness but never part of the composed key."""
        a = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals", location="RTP-1")
        b = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals", location="RTP-2")
        assert a == b

    def test_empty_code_and_description_returns_empty_string(self):
        assert lot_natural_key(code="", description="", vendor="Kedar Metals") == ""
        assert lot_natural_key(code=None, description=None, vendor="Kedar Metals") == ""

    def test_no_vendor_column_plant_still_produces_a_key(self):
        """Achhad's Stock sheet has no vendor column - an empty vendor
        segment must not blank out the whole key."""
        key = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="")
        assert key == "SAP123|"

    def test_occurrence_zero_has_no_suffix(self):
        key = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals", occurrence=0)
        assert "#" not in key

    def test_occurrence_appends_suffix(self):
        first = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals", occurrence=0)
        second = lot_natural_key(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals", occurrence=1)
        assert second == f"{first}#2"


class TestOccurrenceCounter:
    def test_first_occurrence_gets_no_suffix(self):
        counter = OccurrenceCounter()
        key = counter.key_for(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        assert key == "SAP123|kedarmetals"

    def test_duplicate_rows_receive_suffixes_in_sheet_order(self):
        """Two genuinely separate lots of the same material/vendor (a real,
        legitimate case per stock_identity.py's docstring) get
        distinguishable keys in the order they're seen."""
        counter = OccurrenceCounter()
        first = counter.key_for(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        second = counter.key_for(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        third = counter.key_for(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        assert first == "SAP123|kedarmetals"
        assert second == "SAP123|kedarmetals#2"
        assert third == "SAP123|kedarmetals#3"

    def test_different_base_keys_are_independent(self):
        counter = OccurrenceCounter()
        a1 = counter.key_for(code="A", description="Material A", vendor="Vendor 1")
        b1 = counter.key_for(code="B", description="Material B", vendor="Vendor 1")
        a2 = counter.key_for(code="A", description="Material A", vendor="Vendor 1")
        assert a1 == "A|vendor1"
        assert b1 == "B|vendor1"
        assert a2 == "A|vendor1#2"

    def test_empty_key_never_consumes_an_occurrence_slot(self):
        """A row with no identity at all must not be counted alongside real
        rows sharing the same (blank) base - callers skip it entirely."""
        counter = OccurrenceCounter()
        assert counter.key_for(code="", description="", vendor="") == ""
        assert counter.key_for(code="", description="", vendor="") == ""
        # A real row afterwards is unaffected by the two empty calls above.
        key = counter.key_for(code="SAP123", description="Zinc Oxide", vendor="Kedar Metals")
        assert key == "SAP123|kedarmetals"
