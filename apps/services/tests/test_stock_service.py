"""
apps/services/stock_service.py and stock_rules.py - RM stock entry, on real
Postgres.

Each class is one rule of the design: a posted MIR is the only way stock
comes in (its accepted quantity, in the stock unit, while it stays posted);
an issue names the MIR receipt it comes out of (2026-09-30) and never takes
it below zero on any day; a return goes back into that receipt; stock
differences (counts, write-offs) wait for an admin; a MIR whose stock was
issued cannot be cancelled or have more rejected; the register; numbering;
and two storekeepers issuing the last of a receipt at once.
"""

import datetime
import importlib
import threading
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.db import connections
from django.utils import timezone

from apps.api.tests.factories import make_user
from apps.core.models import (
    Mir,
    Plant,
    PurchaseOrder,
    PurchaseOrderLine,
    StockLot,
    StockSetting,
    StockVoucher,
    Vendor,
)
from apps.services import materials, mir_service, stock_rules, stock_service
from apps.services.mir_service import MirValidationError
from apps.services.stock_service import StockValidationError

MH = "27AAACP5506B1ZW"
TODAY = timezone.localdate()


def _day(n):
    return TODAY - datetime.timedelta(days=n)


def _vendor():
    return Vendor.objects.filter(gstin=MH).first() or Vendor.objects.create(gstin=MH, name="Prime Chemicals", name_key="primechemicals")


_PO_SEQ = [0]


def _po(qty="100", rate="50", *, plant="vapi", description="SBR 1502", uom="KG"):
    _PO_SEQ[0] += 1
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code=plant), po_number=f"30000{_PO_SEQ[0]:05d}", vendor=_vendor(),
                                      po_date=_day(60), tax_type="IGST")
    # As the PO sync makes it: the material first seen with the line's unit.
    return PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description=description, uom=uom,
                                            qty_ordered=Decimal(qty), rate=Decimal(rate), material=materials.material_for(description, uom=uom))


def _receive(user, qty="100", rate="50", *, days_ago=0, plant="hrs", description="SBR 1502", uom="KG", rejected=None, po_plant="vapi"):
    """Post a MIR for a whole PO line at `plant`, dated `days_ago`."""
    line = _po(qty, rate, plant=po_plant, description=description, uom=uom)
    ln = {"po_line_id": line.id, "qty_received": qty, "rate": rate, "gst_rate": "18", "material_category": "Synthetic Rubber"}
    if rejected:
        ln.update(qty_rejected=rejected, reject_reason="REJ_QUALITY", qty_reason="PARTIAL_BALANCE_DUE")
    body = {"plant": plant, "mir_date": _day(days_ago).isoformat(), "invoice_no": f"INV-{line.id}",
            "invoice_date": _day(days_ago).isoformat(), "lines": [ln]}
    body["invoice_total"] = str(mir_service.evaluate(body)["computed_total"])
    return mir_service.post_mir(body, user)


def _material(description="SBR 1502"):
    return materials.material_for(description)


def _lot(mir):
    return StockLot.objects.get(mir_line__mir=mir)


def _issue(user, qty, lot, *, days_ago=0, plant="hrs", **extra):
    body = {"kind": "ISSUE", "plant": plant, "voucher_date": _day(days_ago).isoformat(),
            "lines": [{"lot_id": lot.id, "qty": qty}]}
    body.update(extra)
    return stock_service.post_voucher(body, user)


def _return(user, issue, qty, *, days_ago=0, reason="RETURN_UNUSED"):
    line = issue.lines.get()
    body = {"kind": "RETURN", "plant": issue.plant.code, "voucher_date": _day(days_ago).isoformat(), "return_of": issue.id,
            "lines": [{"issue_line_id": line.id, "qty": qty, "reason": reason}]}
    return stock_service.post_voucher(body, user)


def _adjust(user, lines, *, plant="hrs", days_ago=0):
    return stock_service.post_voucher({"kind": "ADJUST", "plant": plant, "voucher_date": _day(days_ago).isoformat(), "lines": lines}, user)


def _left(lot):
    return stock_service.lot_balances([StockLot.objects.get(pk=lot.pk)])[lot.pk]["balance"]


def _on_hand(plant="hrs", description="SBR 1502", uom="KG"):
    lots = list(StockLot.objects.filter(plant__code=plant, material=_material(description), uom=uom))
    return sum((b["balance"] for b in stock_service.lot_balances(lots).values()), Decimal("0"))


def _messages(exc):
    return " | ".join(e["message"] for e in exc.value.errors)


def _fields(exc):
    return {e["field"] for e in exc.value.errors}


@pytest.fixture
def user(db):
    return make_user(email="store@ravasco.com", role="editor")


@pytest.fixture
def admin(db):
    return make_user(email="boss@ravasco.com", role="admin")


# ── The pure rules ──────────────────────────────────────────────────────


class TestStockRules:
    def test_weight_is_held_in_kg_and_nothing_else_is_converted(self):
        assert stock_rules.stock_unit("MT") == ("KG", Decimal("1000"))
        assert stock_rules.stock_unit("G") == ("KG", Decimal("0.001"))
        assert stock_rules.stock_unit("KG") == ("KG", Decimal("1"))
        assert stock_rules.stock_unit("L") == ("L", Decimal("1"))
        assert stock_rules.stock_unit("NOS") == ("NOS", Decimal("1"))
        assert stock_rules.stock_unit("") == ("", Decimal("1"))

    def test_a_lot_is_valued_before_gst_per_stock_unit(self):
        # 2 MT billed Rs 1,00,000 taxable -> Rs 50 a KG.
        assert stock_rules.lot_rate(Decimal("100000"), Decimal("2"), Decimal("1000")) == Decimal("50.0000")
        assert stock_rules.lot_rate(Decimal("5"), Decimal("0"), Decimal("1")) is None

    def test_base_units_convert_exactly_or_by_the_materials_factor(self):
        assert stock_rules.to_base("MT", "KG", {}) == ("KG", Decimal("1000"))
        assert stock_rules.to_base("ML", "L", {}) == ("L", Decimal("0.001"))
        assert stock_rules.to_base("MM", "M", {}) == ("M", Decimal("0.001"))
        assert stock_rules.to_base("NOS", "NOS", {}) == ("NOS", Decimal("1"))
        # A unit of another kind is never forced into the base unit.
        assert stock_rules.to_base("L", "KG", {}) == ("L", Decimal("1"))
        assert stock_rules.to_base("ROLL", "M", {}) == ("ROLL", Decimal("1"))
        assert stock_rules.to_base("ROLL", "M", {"ROLL": Decimal("660")}) == ("M", Decimal("660"))
        assert stock_rules.to_base("MT", "", {}) == ("MT", Decimal("1"))
        assert (stock_rules.base_of("TO"), stock_rules.base_of("BQ2")) == ("", "")

    def test_voucher_numbers(self):
        assert stock_rules.voucher_number("HRS", "ISS", "2026-27", 7) == "HRS/ISS/26-27/0007"

    def test_the_lowest_balance_counts_every_day_from_the_given_one(self):
        d = datetime.date
        events = [(d(2026, 9, 1), Decimal("100")), (d(2026, 9, 5), Decimal("-60")), (d(2026, 9, 9), Decimal("50"))]
        assert stock_rules.min_running_balance(events) == Decimal("40")
        # From 3 Sep: 100 carried in, 40 after the 5th - a later receipt does not hide it.
        assert stock_rules.min_running_balance(events, d(2026, 9, 3)) == Decimal("40")
        # Before the lot existed it held nothing.
        assert stock_rules.min_running_balance(events, d(2026, 8, 30)) == Decimal("0")
        # After every movement: what it holds.
        assert stock_rules.min_running_balance(events, d(2026, 9, 20)) == Decimal("90")


# ── The MIR is the receipt ──────────────────────────────────────────────


@pytest.mark.django_db
class TestReceipts:
    def test_a_posted_mir_puts_its_accepted_quantity_into_the_receiving_plants_store(self, user):
        mir = _receive(user, "100", "50", plant="hrs", po_plant="vapi")
        lot = StockLot.objects.get(mir_line__mir=mir)
        assert (lot.plant.code, lot.uom, lot.rate, lot.stocked) == ("hrs", "KG", Decimal("50.0000"), True)
        # Vapi's PO, received at HRS: it sits at HRS, and says who paid.
        assert lot.bill_to_plant.code == "vapi"
        assert _on_hand("hrs") == Decimal("100") and _on_hand("vapi") == Decimal("0")

    def test_mt_is_received_into_the_materials_base_unit_kg(self, user):
        # Owner, 2026-09-30: base units KG, L, Nos, m.
        _receive(user, "2", "50000", uom="MT")
        lot = StockLot.objects.get()
        assert lot.material.base_uom == "KG"
        assert (lot.uom, lot.factor, lot.rate) == ("KG", Decimal("1000"), Decimal("50.0000"))
        assert _on_hand() == Decimal("2000")

    def test_a_pack_unit_converts_only_with_the_materials_factor(self, user, admin):
        _receive(user, "3", "9000", description="NN 250 fabric roll", uom="ROLL")
        lot = StockLot.objects.get()
        assert (lot.uom, lot.factor) == ("ROLL", Decimal("1"))  # no base unit, no factor: as the MIR had it
        materials.set_units(lot.material, "M", {"ROLLS": "660"}, "each roll is 660 m", admin)
        _receive(user, "2", "9000", description="NN 250 fabric roll", uom="ROLL")
        new = StockLot.objects.latest("id")
        assert (new.uom, new.factor, new.rate) == ("M", Decimal("660"), Decimal("13.6364"))
        assert _left(new) == Decimal("1320")
        # The receipt already in the store keeps its unit.
        assert StockLot.objects.get(pk=lot.pk).uom == "ROLL"

    def test_units_are_changed_with_a_reason_and_logged(self, admin):
        m = _material("Anti Tac")
        for base, factors, reason, message in (("KG", {}, "", "Say why"), ("TON", {}, "x", "KG, L, NOS or M"),
                                               ("KG", {"MT": "1000"}, "x", "converts exactly"), ("", {"SET": "2"}, "x", "Choose the base unit"),
                                               ("KG", {"BAG": "-1"}, "x", "more than zero")):
            with pytest.raises(materials.MaterialError, match=message):
                materials.set_units(m, base, factors, reason, admin)
        materials.set_units(m, "KG", {"BAG": "25"}, "25 kg bags", admin)
        m.refresh_from_db()
        assert m.base_uom == "KG" and {f.uom: f.factor for f in m.unit_factors.all()} == {"BAG": Decimal("25")}
        assert {c.field for c in m.changes.all()} == {"base_uom", "factor BAG"}
        # Plain figures in the log, never "2.5E+1".
        assert m.changes.get(field="factor BAG").new_value == "25"
        materials.set_units(m, "KG", {"BAG": ""}, "sold loose now", admin)
        assert not m.unit_factors.exists()
        with pytest.raises(materials.MaterialError, match="Nothing changed"):
            materials.set_units(m, "KG", {}, "again", admin)

    def test_rejected_at_the_gate_never_enters_stock(self, user):
        _receive(user, "100", rejected="30")
        assert _on_hand() == Decimal("70")

    def test_a_material_the_plant_does_not_stock_goes_straight_to_use(self, user):
        StockSetting.objects.create(plant=Plant.objects.get(code="hrs"), material=_material(), is_stocked=False)
        mir = _receive(user)
        lot = StockLot.objects.get(mir_line__mir=mir)
        assert lot.stocked is False and _on_hand() == Decimal("0")
        assert stock_service.lot_detail(lot)["movements"] == []

    def test_cancelling_an_unissued_mir_empties_its_lot(self, user):
        mir = _receive(user)
        mir_service.cancel_mir(mir, user, "entered twice")
        assert _on_hand() == Decimal("0")


# ── Issues: out of the MIR the storekeeper picks ────────────────────────


@pytest.mark.django_db
class TestIssues:
    def test_an_issue_takes_the_chosen_mir_receipt_and_is_valued_at_its_rate(self, user):
        older = _lot(_receive(user, "100", "50", days_ago=5))
        newer = _lot(_receive(user, "100", "60", days_ago=2))
        issue = _issue(user, "40", newer)
        line = issue.lines.get()
        assert line.lot == newer and line.material == newer.material and line.uom == "KG"
        assert [(a.lot_id, a.qty) for a in line.allocations.all()] == [(newer.id, Decimal("40"))]
        # Nothing is taken from the older receipt just because it is older.
        assert (_left(older), _left(newer)) == (Decimal("100.000"), Decimal("60.000"))
        preview = stock_service.evaluate({"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                                          "lines": [{"lot_id": newer.id, "qty": "10"}]})
        assert preview["ok"] and preview["lines"][0]["value"] == Decimal("600.00")

    def test_more_than_the_receipt_holds_is_refused_even_when_another_holds_plenty(self, user):
        lot = _lot(_receive(user, "100"))
        _receive(user, "500")
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "100.001", lot)
        assert "lines.0.qty" in _fields(exc) and "Only 100" in _messages(exc) and lot.mir_line.mir.mir_no in _messages(exc)
        assert not StockVoucher.objects.exists()

    def test_an_issue_dated_before_the_mir_came_in_is_refused(self, user):
        lot = _lot(_receive(user, "100", days_ago=1))
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "10", lot, days_ago=3)
        assert "came in on" in _messages(exc)

    def test_a_backdated_issue_that_fits_today_but_not_its_day_is_refused(self, user):
        # 100 in on day -5, 60 out on day -1: 40 today. 50 dated day -3 fits
        # the 100 held that day - but would leave -10 after day -1's issue.
        lot = _lot(_receive(user, "100", days_ago=5))
        _issue(user, "60", lot, days_ago=1)
        with pytest.raises(StockValidationError):
            _issue(user, "50", lot, days_ago=3)
        assert _issue(user, "40", lot, days_ago=3).status == "POSTED"
        assert _left(lot) == Decimal("0")

    def test_dates_are_today_or_up_to_the_backdating_window(self, user):
        lot = _lot(_receive(user, "100", days_ago=20))
        for days in (-1, stock_service.BACKDATE_DAYS + 1):
            with pytest.raises(StockValidationError) as exc:
                _issue(user, "1", lot, days_ago=days)
            assert "voucher_date" in _fields(exc)
        assert _issue(user, "1", lot, days_ago=stock_service.BACKDATE_DAYS).status == "POSTED"

    def test_department_is_optional_and_a_receipt_goes_on_one_line(self, user):
        lot = _lot(_receive(user, "100"))
        assert _issue(user, "1", lot).department == ""
        body = {"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                "lines": [{"lot_id": lot.id, "qty": "1"}, {"lot_id": lot.id, "qty": "2"}, {"qty": "1"}]}
        with pytest.raises(StockValidationError) as exc:
            stock_service.post_voucher(body, user)
        assert _fields(exc) == {"lines.1.lot_id", "lines.2.lot_id"} and "Choose the MIR" in _messages(exc)

    def test_a_receipt_at_another_plant_is_not_this_plants_stock(self, user):
        lot = _lot(_receive(user, "100", plant="vapi"))
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "1", lot, plant="hrs")
        assert "received at" in _messages(exc)

    def test_a_cancelled_or_straight_to_use_receipt_holds_nothing_to_issue(self, user):
        mir = _receive(user, "100")
        mir_service.cancel_mir(mir, user, "entered twice")
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "1", _lot(mir))
        assert "is cancelled" in _messages(exc)
        StockSetting.objects.create(plant=Plant.objects.get(code="hrs"), material=_material("Carbon Black N330"), is_stocked=False)
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "1", _lot(_receive(user, "100", description="Carbon Black N330")))
        assert "straight to use" in _messages(exc)

    def test_issues_are_numbered_per_plant_kind_and_year(self, user):
        lot = _lot(_receive(user, "100"))
        a, b = _issue(user, "1", lot), _issue(user, "1", lot)
        fy = a.fy
        assert (a.voucher_no, b.voucher_no) == (f"HRS/ISS/{fy[2:4]}-{fy[5:7]}/0001", f"HRS/ISS/{fy[2:4]}-{fy[5:7]}/0002")

    def test_going_below_the_minimum_level_is_a_notice_not_an_error(self, user):
        lot = _lot(_receive(user, "100"))
        StockSetting.objects.create(plant=Plant.objects.get(code="hrs"), material=_material(), min_level=Decimal("50"), min_level_uom="KG")
        result = stock_service.evaluate({"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                                         "lines": [{"lot_id": lot.id, "qty": "60"}]})
        assert result["ok"] and "below its minimum level" in result["notices"][0]


# ── Returns ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestReturns:
    def test_a_return_goes_back_into_the_receipt_it_came_from(self, user):
        lot = _lot(_receive(user, "100", "50", days_ago=5))
        issue = _issue(user, "60", lot, days_ago=2)
        ret = _return(user, issue, "25")
        line = ret.lines.get()
        assert line.lot == lot and [(a.lot_id, a.qty) for a in line.allocations.all()] == [(lot.id, Decimal("25"))]
        assert _left(lot) == Decimal("65.000")

    def test_no_more_than_is_still_out_can_come_back(self, user):
        issue = _issue(user, "40", _lot(_receive(user, "100")))
        _return(user, issue, "30")
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "11")
        assert "At most 10" in _messages(exc)

    def test_a_return_cannot_be_dated_before_its_issue_or_go_to_another_plant(self, user):
        issue = _issue(user, "40", _lot(_receive(user, "100", days_ago=5)), days_ago=2)
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "10", days_ago=3)
        assert "voucher_date" in _fields(exc)
        body = {"kind": "RETURN", "plant": "vapi", "voucher_date": TODAY.isoformat(), "return_of": issue.id,
                "lines": [{"issue_line_id": issue.lines.get().id, "qty": "1", "reason": "RETURN_UNUSED"}]}
        with pytest.raises(StockValidationError) as exc:
            stock_service.post_voucher(body, user)
        assert "return_of" in _fields(exc)

    def test_a_return_needs_a_reason_and_the_other_reason_a_note(self, user):
        issue = _issue(user, "40", _lot(_receive(user, "100")))
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "1", reason="")
        assert "lines.0.reason" in _fields(exc)
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "1", reason="RETURN_OTHER")
        assert "lines.0.note" in _fields(exc)


# ── Cancelling ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestCancelling:
    def test_an_issue_with_a_return_against_it_cannot_be_cancelled_until_the_return_is(self, user):
        lot = _lot(_receive(user, "100"))
        issue = _issue(user, "40", lot)
        ret = _return(user, issue, "10")
        with pytest.raises(StockValidationError) as exc:
            stock_service.cancel_voucher(issue, user, "wrong")
        assert ret.voucher_no in _messages(exc)
        stock_service.cancel_voucher(ret, user, "wrong")
        stock_service.cancel_voucher(issue, user, "wrong")
        assert _left(lot) == Decimal("100")

    def test_a_return_whose_stock_was_issued_again_cannot_be_cancelled(self, user):
        lot = _lot(_receive(user, "100"))
        issue = _issue(user, "100", lot)
        ret = _return(user, issue, "30")
        _issue(user, "30", lot)
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(ret, user, "wrong")

    def test_cancelling_needs_a_reason(self, user):
        issue = _issue(user, "1", _lot(_receive(user, "100")))
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(issue, user, " ")


# ── The MIR side of it ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestMirChangesAfterIssue:
    def test_a_mir_whose_stock_was_issued_cannot_be_cancelled(self, user):
        mir = _receive(user, "100")
        issue = _issue(user, "10", _lot(mir))
        with pytest.raises(MirValidationError) as exc:
            mir_service.cancel_mir(mir, user, "entered twice")
        assert issue.voucher_no in _messages(exc)
        assert Mir.objects.get(pk=mir.pk).status == "POSTED"
        stock_service.cancel_voucher(issue, user, "put back")
        mir_service.cancel_mir(mir, user, "entered twice")
        assert _on_hand() == Decimal("0")

    def test_a_late_rejection_may_take_only_what_is_still_in_the_store(self, user):
        mir = _receive(user, "100")
        _issue(user, "80", _lot(mir))
        line = mir.lines.get()
        with pytest.raises(MirValidationError):
            mir_service.record_rejection(line, user, "21", "REJ_QUALITY", "")
        mir_service.record_rejection(line, user, "20", "REJ_QUALITY", "")
        assert _on_hand() == Decimal("0")

    def test_the_mir_detail_says_what_each_line_put_into_stock(self, user):
        mir = _receive(user, "2", "50000", uom="MT")
        _issue(user, "500", _lot(mir))
        info = stock_service.mir_line_stock(mir)[mir.lines.get().id]
        assert (info["uom"], info["in"], info["balance"]) == ("KG", Decimal("2000.000"), Decimal("1500.000"))


# ── Stock differences: the store's open mismatches ─────────────────────


@pytest.mark.django_db
class TestDifferences:
    def test_an_editors_write_off_waits_for_an_admin_and_moves_nothing_until_then(self, user, admin):
        lot = _lot(_receive(user, "100"))
        v = _adjust(user, [{"mode": "remove", "lot_id": lot.id, "qty": "30", "reason": "DAMAGED_EXPIRED", "note": "wet"}])
        assert v.status == "PENDING" and _left(lot) == Decimal("100")
        stock_service.approve_adjustment(v, admin, "saw the bags")
        v.refresh_from_db()
        assert v.status == "POSTED" and v.decided_by == admin and _left(lot) == Decimal("70")

    def test_an_admins_difference_posts_at_once(self, admin, user):
        lot = _lot(_receive(user, "100"))
        v = _adjust(admin, [{"mode": "remove", "lot_id": lot.id, "qty": "5", "reason": "SAMPLE_TESTING"}])
        assert v.status == "POSTED" and _left(lot) == Decimal("95")

    def test_turning_one_down_needs_a_note(self, user, admin):
        lot = _lot(_receive(user, "100"))
        v = _adjust(user, [{"mode": "remove", "lot_id": lot.id, "qty": "5", "reason": "SAMPLE_TESTING"}])
        with pytest.raises(StockValidationError):
            stock_service.reject_adjustment(v, admin, "")
        stock_service.reject_adjustment(v, admin, "no sample was sent")
        v.refresh_from_db()
        assert v.status == "REJECTED" and _left(lot) == Decimal("100")

    def test_a_count_records_the_difference_from_that_receipts_books_on_its_day(self, user, admin):
        lot = _lot(_receive(user, "100", days_ago=3))
        _receive(user, "500", days_ago=3)  # another receipt of the same material: not counted here
        v = _adjust(admin, [{"mode": "count", "lot_id": lot.id, "counted": "92", "reason": "COUNT_LOSS", "note": "monthly count"}])
        line = v.lines.get()
        assert (line.lot, line.book_qty, line.counted_qty, line.qty, line.direction) == (lot, Decimal("100"), Decimal("92"), Decimal("8"), -1)
        assert _left(lot) == Decimal("92")

    def test_a_count_that_found_more_goes_back_into_the_same_receipt(self, user, admin):
        lot = _lot(_receive(user, "100", "50"))
        v = _adjust(admin, [{"mode": "count", "lot_id": lot.id, "counted": "104", "reason": "COUNT_GAIN", "note": "recount"}])
        assert _left(lot) == Decimal("104") and StockLot.objects.count() == 1
        _issue(user, "104", lot)
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(v, admin, "miscounted")

    def test_a_count_matching_the_books_or_with_a_reason_of_the_wrong_kind_is_refused(self, admin, user):
        lot = _lot(_receive(user, "100"))
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "count", "lot_id": lot.id, "counted": "100", "reason": "COUNT_LOSS", "note": "x"}])
        assert "no difference" in _messages(exc)
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "count", "lot_id": lot.id, "counted": "110", "reason": "COUNT_LOSS", "note": "x"}])
        assert "lines.0.reason" in _fields(exc)

    def test_stock_cannot_be_added_by_hand(self, admin):
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "rate": "1", "reason": "OPENING_BALANCE"}])
        assert "only through a MIR" in _messages(exc)

    def test_approval_re_checks_a_write_off_against_stock_that_moved_since(self, user, admin):
        lot = _lot(_receive(user, "100"))
        v = _adjust(user, [{"mode": "remove", "lot_id": lot.id, "qty": "30", "reason": "DAMAGED_EXPIRED", "note": "wet"}])
        _issue(user, "80", lot)
        with pytest.raises(StockValidationError):
            stock_service.approve_adjustment(v, admin, "ok")
        assert StockVoucher.objects.get(pk=v.pk).status == "PENDING"

    def test_the_person_who_entered_it_cannot_approve_it(self, user):
        lot = _lot(_receive(user, "100"))
        v = _adjust(user, [{"mode": "remove", "lot_id": lot.id, "qty": "5", "reason": "SAMPLE_TESTING"}])
        with pytest.raises(StockValidationError):
            stock_service.approve_adjustment(v, user, "self")

    def test_the_open_list_is_what_waits_for_an_admin(self, user, admin):
        lot = _lot(_receive(user, "100"))
        waiting = _adjust(user, [{"mode": "remove", "lot_id": lot.id, "qty": "5", "reason": "SAMPLE_TESTING"}])
        _adjust(admin, [{"mode": "remove", "lot_id": lot.id, "qty": "1", "reason": "SAMPLE_TESTING"}])
        assert [ln.voucher_id for ln in stock_service.differences(["hrs"], "OPEN")] == [waiting.id]
        assert len(stock_service.differences(["hrs"], "RESOLVED")) == 1 and stock_service.differences(["vapi"], "ALL") == []


# ── Reading: the register and a receipt's story ─────────────────────────


@pytest.mark.django_db
class TestRegister:
    def test_a_row_per_receipt_with_opening_movements_and_closing_for_the_period(self, user):
        lot = _lot(_receive(user, "100", "50", days_ago=6))
        _issue(user, "30", lot, days_ago=5)
        issue = _issue(user, "20", lot, days_ago=2)
        _return(user, issue, "5", days_ago=1)
        rows = stock_service.register_rows(["hrs"], _day(3), TODAY)
        assert len(rows) == 1
        r = rows[0]
        assert (r["lot"], r["opening"], r["received"], r["issued"], r["returned"], r["adjusted"], r["closing"]) == (
            lot, Decimal("70.000"), 0, Decimal("20"), Decimal("5"), 0, Decimal("55.000"))
        assert (r["value"], r["days"], r["last_issued"]) == (Decimal("2750.00"), 6, _day(2))

    def test_a_receipt_that_held_nothing_all_period_is_left_out_unless_asked_for(self, user):
        lot = _lot(_receive(user, "100", days_ago=6))
        _issue(user, "100", lot, days_ago=5)
        assert stock_service.register_rows(["hrs"], _day(3), TODAY) == []
        assert [r["lot"] for r in stock_service.register_rows(["hrs"], _day(3), TODAY, include_empty=True)] == [lot]
        # ... and a receipt made after the period is not in it.
        assert stock_service.register_rows(["hrs"], _day(10), _day(7), include_empty=True) == []

    def test_the_register_searches_mir_number_and_material(self, user):
        mir = _receive(user, "100")
        _receive(user, "100", description="Carbon Black N330")
        assert [r["lot"].material.name for r in stock_service.register_rows(["hrs"], TODAY, TODAY, q="carbon")] == ["Carbon Black N330"]
        assert [r["lot"] for r in stock_service.register_rows(["hrs"], TODAY, TODAY, q=mir.mir_no)] == [_lot(mir)]

    def test_a_receipts_story_runs_receipt_issue_return_in_date_order(self, user):
        lot = _lot(_receive(user, "100", days_ago=4))
        issue = _issue(user, "60", lot, days_ago=2, department="Mixing")
        _return(user, issue, "10")
        moves = stock_service.lot_detail(lot)["movements"]
        assert [(m["kind"], m["qty"], m["balance"], m["detail"]) for m in moves] == [
            ("RECEIPT", Decimal("100.000"), Decimal("100.000"), "Prime Chemicals"), ("ISSUE", Decimal("-60"), Decimal("40.000"), "Mixing"),
            ("RETURN", Decimal("10"), Decimal("50.000"), "Not used - returned to store")]

    def test_the_picker_offers_this_plants_receipts_with_stock_left_oldest_first(self, user):
        old = _lot(_receive(user, "100", days_ago=4))
        new = _lot(_receive(user, "100", days_ago=1))
        _issue(user, "100", _lot(_receive(user, "100", days_ago=2)))
        _receive(user, "100", plant="vapi")
        assert [lot for lot, _bal in stock_service.receipts_for_issue(["hrs"])] == [old, new]
        assert [lot for lot, _bal in stock_service.receipts_for_issue(["hrs"], new.mir_line.mir.mir_no)] == [new]
        # Across plants: the form takes its plant from the MIR picked.
        assert len(stock_service.receipts_for_issue(["hrs", "vapi"])) == 3

    def test_the_picker_reads_past_fully_issued_receipts_to_newer_stock(self, user, monkeypatch):
        """It used to take the oldest 3,000 lots and only then drop the
        empty ones, so a plant with thousands of fully issued receipts got a
        short or empty picker. Batches of 2 stand in for that here."""
        monkeypatch.setattr(stock_service, "_PICKER_BATCH", 2)
        for days in (9, 8, 7):
            _issue(user, "100", _lot(_receive(user, "100", days_ago=days)))
        newer = [_lot(_receive(user, "100", days_ago=d)) for d in (3, 2, 1)]
        assert [lot for lot, _bal in stock_service.receipts_for_issue(["hrs"])] == newer
        assert [lot for lot, _bal in stock_service.receipts_for_issue(["hrs"], limit=2)] == newer[:2]


@pytest.mark.django_db
class TestLotsBackInTheirMirUnit:
    """Migration 0084: lots made when MT and G were held in KG."""

    def _as_before(self, lot):
        StockLot.objects.filter(pk=lot.pk).update(uom="KG", factor=Decimal("1000"), rate=Decimal("50.0000"))

    def _migrate(self):
        importlib.import_module("apps.core.migrations.0084_stock_lot_mir_unit").lots_in_their_mir_unit(django_apps, None)

    def test_a_kg_lot_and_its_issue_go_back_to_mt(self, user):
        lot = _lot(_receive(user, "2", "50000", uom="MT"))
        self._as_before(lot)
        issue = _issue(user, "500", StockLot.objects.get(pk=lot.pk))
        self._migrate()
        lot.refresh_from_db()
        line = issue.lines.get()
        assert (lot.uom, lot.factor, lot.rate) == ("MT", Decimal("1"), Decimal("50000.0000"))
        assert (line.uom, line.qty, line.allocations.get().qty) == ("MT", Decimal("0.500"), Decimal("0.500"))
        assert _left(lot) == Decimal("1.500")

    def test_a_lot_whose_figures_would_round_stays_in_kg(self, user):
        lot = _lot(_receive(user, "2", "50000", uom="MT"))
        self._as_before(lot)
        _issue(user, "0.5", StockLot.objects.get(pk=lot.pk))  # 0.0005 MT - not exact at 3 decimals
        self._migrate()
        lot.refresh_from_db()
        assert (lot.uom, lot.factor) == ("KG", Decimal("1000"))


@pytest.mark.django_db
class TestLotsIntoBaseUnits:
    """Migration 0085: a base unit for every material, and lots held in the
    MIR's unit converted into it."""

    def _migrate(self):
        mod = importlib.import_module("apps.core.migrations.0085_material_base_unit")
        mod.base_units(django_apps, None)
        mod.lots_in_base_units(django_apps, None)

    def test_an_mt_lot_and_its_issue_go_into_kg(self, user):
        from apps.core.models import Material
        lot = _lot(_receive(user, "2", "50000", uom="MT"))
        Material.objects.filter(pk=lot.material_id).update(base_uom="")
        StockLot.objects.filter(pk=lot.pk).update(uom="MT", factor=1, rate=Decimal("50000"))
        issue = _issue(user, "0.5", StockLot.objects.get(pk=lot.pk))
        self._migrate()
        lot.refresh_from_db()
        line = issue.lines.get()
        assert lot.material.base_uom == "KG"
        assert (lot.uom, lot.factor, lot.rate) == ("KG", Decimal("1000"), Decimal("50.0000"))
        assert (line.uom, line.qty, line.allocations.get().qty) == ("KG", Decimal("500.000"), Decimal("500.000"))
        assert _left(lot) == Decimal("1500.000")

    def test_the_base_unit_comes_from_the_po_lines_or_the_reference_list(self):
        from apps.core.models import Material
        reference = Material.objects.create(name="Lube", name_key="lube", uom="Liter (L)")
        pack = Material.objects.create(name="Seal", name_key="seal", uom="SET")
        self._migrate()
        assert Material.objects.get(pk=reference.pk).base_uom == "L" and Material.objects.get(pk=pack.pk).base_uom == ""


@pytest.mark.django_db
class TestExistingMirs:
    def test_the_migration_gives_every_earlier_mir_line_its_lot(self, user):
        mir = _receive(user, "100")
        StockLot.objects.all().delete()
        importlib.import_module("apps.core.migrations.0082_stock_entry").lots_for_existing_mirs(django_apps, None)
        lot = StockLot.objects.get(mir_line__mir=mir)
        assert (lot.plant.code, lot.rate) == ("hrs", Decimal("50.0000")) and _on_hand() == Decimal("100")


# ── Two storekeepers at once ────────────────────────────────────────────


def _in_parallel(fn_a, fn_b):
    results = [None, None]
    barrier = threading.Barrier(2)

    def run(i, fn):
        try:
            barrier.wait(5)
            results[i] = fn()
        except Exception as exc:
            results[i] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate((fn_a, fn_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return results


@pytest.mark.django_db(transaction=True)
class TestConcurrentIssues:
    def test_the_last_of_a_receipt_issued_twice_at_once_is_issued_once(self):
        user = make_user(email="k1@ravasco.com", role="editor")
        lot = _lot(_receive(user, "100"))
        results = _in_parallel(lambda: _issue(user, "70", lot), lambda: _issue(user, "70", lot))
        assert sum(isinstance(r, StockVoucher) for r in results) == 1, results
        assert _on_hand() == Decimal("30")

    def test_a_return_and_the_issues_cancellation_at_once_do_not_both_commit(self):
        """The return read the issue without a lock, so it and the issue's
        cancellation could both pass their checks and commit - adding back
        stock that, with the issue cancelled, never left the store."""
        user = make_user(email="k2@ravasco.com", role="editor")
        lot = _lot(_receive(user, "100"))
        issue = _issue(user, "60", lot)
        results = _in_parallel(lambda: _return(user, issue, "10"),
                               lambda: stock_service.cancel_voucher(issue, user, "wrong entry"))
        assert sum(not isinstance(r, Exception) for r in results) == 1, results
        assert _on_hand() in (Decimal("50"), Decimal("100"))
