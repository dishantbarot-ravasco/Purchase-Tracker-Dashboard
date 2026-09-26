"""Which PO lines get the weighbridge over-delivery allowance
(apps/services/qty_tolerance.py). Recognition is deliberately not an exact
string, so these pin both sides: the real spellings on the three plants' PO
sheets, typos of them, and the live look-alikes that must NOT qualify."""
from decimal import Decimal

import pytest

from apps.services.qty_tolerance import (
    BULK_QTY_OVER_TOLERANCE_PCT,
    bulk_weight_material,
    over_delivery_tolerance_pct,
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
