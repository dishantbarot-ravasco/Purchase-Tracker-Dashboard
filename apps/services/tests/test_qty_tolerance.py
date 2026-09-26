"""Which PO lines get the weighbridge over-delivery allowance
(apps/services/qty_tolerance.py). Recognition is deliberately not an exact
string, so these pin both sides: the real spellings on the three plants' PO
sheets, typos of them, and the live look-alikes that must NOT qualify."""
from decimal import Decimal

import pytest

from apps.services.qty_tolerance import (
    BULK_QTY_OVER_TOLERANCE_PCT,
    bulk_weight_material,
    is_madura_vendor,
    over_delivery_tolerance_pct,
    rolls_in,
    value_within_over_tolerance,
)


@pytest.mark.parametrize("description, label", [
    # Achhad PO sheet, verbatim
    ("Steam Coal Imported (Non Cooking)", "Steam coal"),
    ("Steam Coal Imported (Non Cooking) Of Size-20 to 50mm, 4200 Gar", "Steam coal"),
    ("HM PLASTIC 1600MMx51MICRON", "HM plastic"),
    ("HM PLASTIC 1100MMx51MICRON", "HM plastic"),
    ('HDPE LAMINATED FABRIC WHITE-72"', "HDPE"),
    # MIR's own wording of the same materials
    ("Imported Coal", "Steam coal"),
    ('HDPE Woven Fabric-72" White', "HDPE"),
    ("HDPE/PP WOVEN FABRIC", "HDPE"),
    # human variation
    ("steam coal", "Steam coal"),
    ("SteamCoal", "Steam coal"),
    ("Steam Col", "Steam coal"),
    ("Stem Coal", "Steam coal"),
    ("HM-Plastics roll", "HM plastic"),
    ("H.M. Plastic", "HM plastic"),
    ("H M Plastik", "HM plastic"),
    ("HMPLASTIC 1300MM", "HM plastic"),
    ("H.D.P.E. Granules", "HDPE"),
    ("HD PE fabric", "HDPE"),
    ("HPDE fabric", "HDPE"),
    ("High Density Polyethylene", "HDPE"),
])
def test_recognised(description, label):
    assert bulk_weight_material(description) == label
    assert over_delivery_tolerance_pct(description) == BULK_QTY_OVER_TOLERANCE_PCT


@pytest.mark.parametrize("description", [
    # Vapi: HM is a melamine resin here, not plastic
    "RUBBOND HM-65",
    "RUBBOND HM-65, Gujbond HMMM65",
    "PP-1890S / HM-65",
    # Vapi: HDPE is only the packaging of the sulphur
    "SULPHUR POWDER, HDPE Bags 50kg x 100",
    # plastic without the HM prefix
    "LD Plastic Bag 20X26X180G",
    "CP PLASTICIZER F-68",
    "LDPE Liner Bag",
    # one letter off coal is another word
    "Coating compound",
    "Cool roof sheet",
    "Charcoal powder",
    "Zinc Oxide",
    "",
])
def test_not_recognised(description):
    assert bulk_weight_material(description) is None
    assert over_delivery_tolerance_pct(description) is None


class TestValueWithinOverTolerance:
    EPS = Decimal("1.00")

    def test_up_to_the_allowance_is_accepted(self):
        assert value_within_over_tolerance(Decimal("1000"), Decimal("1100"), Decimal("10"), self.EPS)

    def test_rounding_past_the_allowance_is_accepted(self):
        assert value_within_over_tolerance(Decimal("1000"), Decimal("1100.90"), Decimal("10"), self.EPS)

    def test_past_the_allowance_is_not(self):
        assert not value_within_over_tolerance(Decimal("1000"), Decimal("1102"), Decimal("10"), self.EPS)

    def test_under_is_never_excused(self):
        assert not value_within_over_tolerance(Decimal("1000"), Decimal("990"), Decimal("10"), self.EPS)

    def test_a_missing_side_is_not_accepted(self):
        assert not value_within_over_tolerance(None, Decimal("1000"), Decimal("10"), self.EPS)


# ── Madura fabric: vendor-keyed allowance and roll counts ──


@pytest.mark.parametrize("vendor", [
    # every spelling on the three plants' PO and MIR sheets
    "Madura Industrial Textiles Ltd",
    "Madura Industrial Textiles Ltd.",
    "Madura Industrial Textile Ltd.",
    "Madura Industrial Textile",  # HRS MIR on Drive
    "MADURA INDL TEXTILES LTD",
    "Madura Technical Textiles Ltd",
    "MADURA TECHNICAL FABRICS LTD.",
    "Madhura Industrial Textiles",
])
def test_madura_vendor_recognised(vendor):
    assert is_madura_vendor(vendor)
    assert over_delivery_tolerance_pct("EE350 142CM", vendor) == BULK_QTY_OVER_TOLERANCE_PCT


@pytest.mark.parametrize("vendor", ["SRF Ltd", "Urja Products Private Limited", "Madurai Rubbers", "Mahavir", ""])
def test_other_vendors_are_not_madura(vendor):
    assert not is_madura_vendor(vendor)
    assert over_delivery_tolerance_pct("EE350 142CM", vendor) is None


@pytest.mark.parametrize("description, rolls", [
    # PO sheet: "roll" names the product first, the count comes last
    ("EE-200 fabric roll, width 102cm, GSM 720, length 510m, 4 rolls, total weight 1498.176", 4),
    ("EE-160 fabric roll, width 223cm, GSM 570, length 464m, 1 roll, total weight 589.79", 1),
    ("PP 250 fabric roll, width 107cm, length 660m, 2 rolls", 2),
    # MIR, as Achhad already writes it and as the plant will add it
    ("Rubberised Textile Fabrics  EEH-160,125Cm,1020 Mtrs - 3 Rolls", 3),
    ("EE350 142CM - 6 Rolls", 6),
    ("EE350 142CM 6rolls", 6),
    ("EE350 142CM Rolls: 6", 6),
    ("EE350 142CM Rolls - 6", 6),
    ("EE350 142CM 6 Rls", 6),
    ("EE350 142CM 6 nos rolls", 6),
    ("EE350 142CM 12 Roles", 12),
    # nothing stated, or not a count
    ("EE350 142CM", None),
    ("EE-200 fabric roll, width 102cm", None),
    ("EE350 142CM roll 142 cm", None),
    ("EE200 102CM 1.5 rolls", None),
    ("Roller chain 12 m", None),
    ("", None),
])
def test_rolls_in(description, rolls):
    assert rolls_in(description) == rolls
