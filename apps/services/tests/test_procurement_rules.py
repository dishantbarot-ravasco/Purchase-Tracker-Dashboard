"""apps/services/procurement_rules.py - the pure rules MIR entry prices and
checks with. Each group pins both what a rule accepts and what it must not."""

import datetime
from decimal import Decimal

import pytest

from apps.services import procurement_rules as r


@pytest.mark.parametrize("raw, code, known", [
    ("KG", "KG", True), ("kgs", "KG", True), ("Kgs.", "KG", True), ("TO", "MT", True), ("MT.", "MT", True),
    ("Ltr", "L", True), ("LTRS", "L", True), ("Nos", "NOS", True), ("EA", "NOS", True), ("PC", "NOS", True),
    ("ROLLS", "ROLL", True), ("M2", "M2", True), ("SQMTR", "M2", True), ("MTRS", "M", True), ("BAG", "BAG", True),
    ("BQ2", "BQ2", False), ("", "", False),
])
def test_canonical_uom(raw, code, known):
    assert r.canonical_uom(raw) == (code, known)


def test_kg_and_tonne_stay_different_units():
    assert r.canonical_uom("KG")[0] != r.canonical_uom("TO")[0]


@pytest.mark.parametrize("raw, expected", [
    ("IGST", r.TaxType.IGST), ("CGST+SGST", r.TaxType.CGST_SGST), ("cgst + sgst", r.TaxType.CGST_SGST),
    ("CGST+UGST", r.TaxType.CGST_UGST), ("", ""), ("CGST+SGST+IGST (mixed - flag)", ""),
])
def test_canonical_tax_type(raw, expected):
    assert r.canonical_tax_type(raw) == expected


class TestExpectedTaxType:
    GJ_VENDOR = "24AABCP1234C1Z5"
    DN_VENDOR = "26AABFH7112D1ZX"

    def test_other_state_is_igst(self):
        assert r.expected_tax_type(self.GJ_VENDOR, "26", True, "") == r.TaxType.IGST

    def test_same_union_territory_is_cgst_ugst(self):
        assert r.expected_tax_type(self.DN_VENDOR, "26", True, "IGST") == r.TaxType.CGST_UGST

    def test_same_state_is_cgst_sgst(self):
        assert r.expected_tax_type(self.GJ_VENDOR, "24", False, "") == r.TaxType.CGST_SGST

    def test_no_gstin_falls_back_to_the_po(self):
        assert r.expected_tax_type("", "24", False, r.TaxType.IGST) == r.TaxType.IGST
        assert r.expected_tax_type("not-a-gstin", "24", False, "") == ""


def test_clean_gstin():
    assert r.clean_gstin(" 24aabcp1234c1z5 ") == "24AABCP1234C1Z5"
    assert r.clean_gstin("24AABCP1234C1Z") == ""
    assert r.gstin_state("27AAACP5506B1ZW") == "27"


@pytest.mark.parametrize("total, incl, rate", [
    (Decimal("1000"), Decimal("1180"), Decimal("18")), (Decimal("2825000"), Decimal("3333500"), Decimal("18")),
    (Decimal("1000"), Decimal("1050"), Decimal("5")), (Decimal("1000"), Decimal("1000"), Decimal("0")),
    (Decimal("1000"), Decimal("1130"), None), (None, Decimal("1180"), None), (Decimal("1000"), None, None),
])
def test_po_gst_rate(total, incl, rate):
    assert r.po_gst_rate(total, incl) == rate


@pytest.mark.parametrize("day, fy", [
    (datetime.date(2026, 4, 1), "2026-27"), (datetime.date(2027, 3, 31), "2026-27"), (datetime.date(2026, 3, 31), "2025-26"),
])
def test_financial_year(day, fy):
    assert r.financial_year(day) == fy


def test_mir_number():
    assert r.mir_number("HRS", "2026-27", 7) == "HRS/26-27/0007"
    assert r.mir_number("VAPI", "2026-27", 12345) == "VAPI/26-27/12345"


@pytest.mark.parametrize("a, b", [("INV-0042", "inv-42"), ("INV - 42", "INV-42"), ("F29020004230", "f29020004230")])
def test_invoice_key_treats_spellings_of_one_invoice_as_one(a, b):
    assert r.invoice_key(a) == r.invoice_key(b)


@pytest.mark.parametrize("a, b", [
    ("A/42", "B/42"), ("42", "43"), ("RTP-0003/26-27", "RTP-0003/25-26"),
    # Separators stay significant: dropping them would make these two collide.
    ("1-23", "12-3"),
])
def test_invoice_key_keeps_different_invoices_apart(a, b):
    assert r.invoice_key(a) != r.invoice_key(b)


def test_vendor_name_key():
    assert r.vendor_name_key("M/s. Prime Chemicals") == r.vendor_name_key("PRIME CHEMICALS")
    assert r.vendor_name_key("") == ""


class TestLineAmounts:
    def test_igst(self):
        a = r.line_amounts(Decimal("950"), Decimal("115"), 0, 0, Decimal("18"), r.TaxType.IGST)
        assert (a["gross"], a["taxable"], a["igst"], a["cgst"], a["total"]) == (
            Decimal("109250.00"), Decimal("109250.00"), Decimal("19665.00"), Decimal("0.00"), Decimal("128915.00"))

    def test_split_tax_rounds_each_half(self):
        a = r.line_amounts(Decimal("1"), Decimal("10.05"), 0, 0, Decimal("5"), r.TaxType.CGST_SGST)
        # 10.05 x 2.5% = 0.25125 each half -> 0.25
        assert (a["cgst"], a["sgst"], a["total"]) == (Decimal("0.25"), Decimal("0.25"), Decimal("10.55"))

    def test_discount_and_charges_move_the_taxable_value(self):
        a = r.line_amounts(Decimal("10"), Decimal("100"), Decimal("50"), Decimal("20"), Decimal("18"), r.TaxType.IGST)
        assert a["taxable"] == Decimal("970.00") and a["igst"] == Decimal("174.60")


def test_rate_differs_is_exact_at_four_places():
    assert not r.rate_differs(Decimal("56.5"), Decimal("56.5000"))
    assert r.rate_differs(Decimal("56.5"), Decimal("56.5001"))


def test_gst_slabs():
    assert r.is_gst_slab(Decimal("18")) and r.is_gst_slab(Decimal("18.00"))
    assert not r.is_gst_slab(Decimal("17")) and not r.is_gst_slab(None)
