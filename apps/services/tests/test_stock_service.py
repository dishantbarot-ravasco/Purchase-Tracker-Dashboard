"""
apps/services/stock_service.py and stock_rules.py - RM stock entry, on real
Postgres.

Each class is one rule of the design (2026-09-29): a posted MIR is the
receipt (its accepted quantity, in the stock unit, while it stays posted);
issues draw the oldest lot first and never take stock below zero on any
day; returns go back to the lots their issue drew; adjustments wait for an
admin; a MIR whose stock was issued cannot be cancelled or have more
rejected; numbering; and two storekeepers issuing the last of a material at
once.
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
    return PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description=description, uom=uom,
                                            qty_ordered=Decimal(qty), rate=Decimal(rate), material=materials.material_for(description))


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


def _issue(user, qty, *, days_ago=0, plant="hrs", description="SBR 1502", uom="KG", **extra):
    body = {"kind": "ISSUE", "plant": plant, "voucher_date": _day(days_ago).isoformat(), "department": "Mixing",
            "issued_to": "Ramesh", "lines": [{"material_id": _material(description).id, "uom": uom, "qty": qty}]}
    body.update(extra)
    return stock_service.post_voucher(body, user)


def _return(user, issue, qty, *, days_ago=0, reason="RETURN_UNUSED"):
    line = issue.lines.get()
    body = {"kind": "RETURN", "plant": issue.plant.code, "voucher_date": _day(days_ago).isoformat(), "return_of": issue.id,
            "lines": [{"issue_line_id": line.id, "qty": qty, "reason": reason}]}
    return stock_service.post_voucher(body, user)


def _adjust(user, lines, *, plant="hrs", days_ago=0):
    return stock_service.post_voucher({"kind": "ADJUST", "plant": plant, "voucher_date": _day(days_ago).isoformat(), "lines": lines}, user)


def _on_hand(plant="hrs", description="SBR 1502", uom="KG"):
    rows = [r for r in stock_service.stock_rows([plant]) if r["material"] == _material(description) and r["uom"] == uom]
    return rows[0]["qty"] if rows else Decimal("0")


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

    def test_mt_is_received_into_kg(self, user):
        _receive(user, "2", "50000", uom="MT")
        lot = StockLot.objects.get()
        assert (lot.uom, lot.factor, lot.rate) == ("KG", Decimal("1000"), Decimal("50.0000"))
        assert _on_hand() == Decimal("2000")

    def test_rejected_at_the_gate_never_enters_stock(self, user):
        _receive(user, "100", rejected="30")
        assert _on_hand() == Decimal("70")

    def test_a_material_the_plant_does_not_stock_goes_straight_to_use(self, user):
        StockSetting.objects.create(plant=Plant.objects.get(code="hrs"), material=_material(), is_stocked=False)
        mir = _receive(user)
        lot = StockLot.objects.get(mir_line__mir=mir)
        assert lot.stocked is False and _on_hand() == Decimal("0")
        ledger = stock_service.material_detail(lot.plant, lot.material, "KG")["ledger"]
        assert ledger[0]["note"] == "Went straight to use - not stocked"

    def test_cancelling_an_unissued_mir_empties_its_lot(self, user):
        mir = _receive(user)
        mir_service.cancel_mir(mir, user, "entered twice")
        assert _on_hand() == Decimal("0")


# ── Issues ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestIssues:
    def test_an_issue_takes_the_oldest_lot_first_and_is_valued_at_its_rates(self, user):
        _receive(user, "100", "50", days_ago=5)
        _receive(user, "100", "60", days_ago=2)
        issue = _issue(user, "150")
        allocs = sorted((a.lot.rate, a.qty) for a in issue.lines.get().allocations.select_related("lot"))
        assert allocs == [(Decimal("50.0000"), Decimal("100")), (Decimal("60.0000"), Decimal("50"))]
        preview = stock_service.evaluate({"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(), "department": "M",
                                          "issued_to": "R", "lines": [{"material_id": _material().id, "uom": "KG", "qty": "40"}]})
        assert preview["lines"][0]["value"] == Decimal("2400.00")  # all from the Rs 60 lot now
        assert _on_hand() == Decimal("50")

    def test_more_than_is_in_stock_is_refused(self, user):
        _receive(user, "100")
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "100.001")
        assert "lines.0.qty" in _fields(exc) and "Only 100" in _messages(exc)
        assert not StockVoucher.objects.exists()

    def test_an_issue_dated_before_the_material_arrived_is_refused(self, user):
        _receive(user, "100", days_ago=1)
        with pytest.raises(StockValidationError) as exc:
            _issue(user, "10", days_ago=3)
        assert "arrived after that date" in _messages(exc)

    def test_a_backdated_issue_that_fits_today_but_not_its_day_is_refused(self, user):
        # 100 in on day -5, 60 out on day -1: 40 today. 50 dated day -3 fits
        # the 100 held that day - but would leave -10 after day -1's issue.
        _receive(user, "100", days_ago=5)
        _issue(user, "60", days_ago=1)
        with pytest.raises(StockValidationError):
            _issue(user, "50", days_ago=3)
        assert _issue(user, "40", days_ago=3).status == "POSTED"
        assert _on_hand() == Decimal("0")

    def test_dates_are_today_or_up_to_the_backdating_window(self, user):
        _receive(user, "100", days_ago=20)
        for days in (-1, stock_service.BACKDATE_DAYS + 1):
            with pytest.raises(StockValidationError) as exc:
                _issue(user, "1", days_ago=days)
            assert "voucher_date" in _fields(exc)
        assert _issue(user, "1", days_ago=stock_service.BACKDATE_DAYS).status == "POSTED"

    def test_department_and_receiver_are_required_and_one_line_per_material(self, user):
        _receive(user, "100")
        body = {"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(),
                "lines": [{"material_id": _material().id, "uom": "KG", "qty": "1"}, {"material_id": _material().id, "uom": "KG", "qty": "2"}]}
        with pytest.raises(StockValidationError) as exc:
            stock_service.post_voucher(body, user)
        assert {"department", "issued_to", "lines.1.material_id"} <= _fields(exc)

    def test_a_lot_at_another_plant_is_not_this_plants_stock(self, user):
        _receive(user, "100", plant="vapi")
        with pytest.raises(StockValidationError):
            _issue(user, "1", plant="hrs")

    def test_issues_are_numbered_per_plant_kind_and_year(self, user):
        _receive(user, "100")
        a, b = _issue(user, "1"), _issue(user, "1")
        fy = a.fy
        assert (a.voucher_no, b.voucher_no) == (f"HRS/ISS/{fy[2:4]}-{fy[5:7]}/0001", f"HRS/ISS/{fy[2:4]}-{fy[5:7]}/0002")

    def test_going_below_the_minimum_level_is_a_notice_not_an_error(self, user):
        _receive(user, "100")
        StockSetting.objects.create(plant=Plant.objects.get(code="hrs"), material=_material(), min_level=Decimal("50"), min_level_uom="KG")
        result = stock_service.evaluate({"kind": "ISSUE", "plant": "hrs", "voucher_date": TODAY.isoformat(), "department": "M",
                                         "issued_to": "R", "lines": [{"material_id": _material().id, "uom": "KG", "qty": "60"}]})
        assert result["ok"] and "below its minimum level" in result["notices"][0]


# ── Returns ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestReturns:
    def test_a_return_goes_back_into_the_lots_its_issue_drew_newest_first(self, user):
        _receive(user, "100", "50", days_ago=5)
        _receive(user, "100", "60", days_ago=4)
        issue = _issue(user, "150", days_ago=2)
        ret = _return(user, issue, "70")
        back = sorted((a.lot.rate, a.qty) for a in ret.lines.get().allocations.select_related("lot"))
        # 50 back to the Rs 60 lot it last drew from, the other 20 to the Rs 50 lot.
        assert back == [(Decimal("50.0000"), Decimal("20")), (Decimal("60.0000"), Decimal("50"))]
        assert _on_hand() == Decimal("120")

    def test_no_more_than_is_still_out_can_come_back(self, user):
        _receive(user, "100")
        issue = _issue(user, "40")
        _return(user, issue, "30")
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "11")
        assert "At most 10" in _messages(exc)

    def test_a_return_cannot_be_dated_before_its_issue_or_go_to_another_plant(self, user):
        _receive(user, "100", days_ago=5)
        issue = _issue(user, "40", days_ago=2)
        with pytest.raises(StockValidationError) as exc:
            _return(user, issue, "10", days_ago=3)
        assert "voucher_date" in _fields(exc)
        body = {"kind": "RETURN", "plant": "vapi", "voucher_date": TODAY.isoformat(), "return_of": issue.id,
                "lines": [{"issue_line_id": issue.lines.get().id, "qty": "1", "reason": "RETURN_UNUSED"}]}
        with pytest.raises(StockValidationError) as exc:
            stock_service.post_voucher(body, user)
        assert "return_of" in _fields(exc)

    def test_a_return_needs_a_reason_and_the_other_reason_a_note(self, user):
        _receive(user, "100")
        issue = _issue(user, "40")
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
        _receive(user, "100")
        issue = _issue(user, "40")
        ret = _return(user, issue, "10")
        with pytest.raises(StockValidationError) as exc:
            stock_service.cancel_voucher(issue, user, "wrong")
        assert ret.voucher_no in _messages(exc)
        stock_service.cancel_voucher(ret, user, "wrong")
        stock_service.cancel_voucher(issue, user, "wrong")
        assert _on_hand() == Decimal("100")

    def test_a_return_whose_stock_was_issued_again_cannot_be_cancelled(self, user):
        _receive(user, "100")
        issue = _issue(user, "100")
        ret = _return(user, issue, "30")
        _issue(user, "30")
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(ret, user, "wrong")

    def test_cancelling_needs_a_reason(self, user):
        _receive(user, "100")
        issue = _issue(user, "1")
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(issue, user, " ")


# ── The MIR side of it ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestMirChangesAfterIssue:
    def test_a_mir_whose_stock_was_issued_cannot_be_cancelled(self, user):
        mir = _receive(user, "100")
        issue = _issue(user, "10")
        with pytest.raises(MirValidationError) as exc:
            mir_service.cancel_mir(mir, user, "entered twice")
        assert issue.voucher_no in _messages(exc)
        assert Mir.objects.get(pk=mir.pk).status == "POSTED"
        stock_service.cancel_voucher(issue, user, "put back")
        mir_service.cancel_mir(mir, user, "entered twice")
        assert _on_hand() == Decimal("0")

    def test_a_late_rejection_may_take_only_what_is_still_in_the_store(self, user):
        mir = _receive(user, "100")
        _issue(user, "80")
        line = mir.lines.get()
        with pytest.raises(MirValidationError):
            mir_service.record_rejection(line, user, "21", "REJ_QUALITY", "")
        mir_service.record_rejection(line, user, "20", "REJ_QUALITY", "")
        assert _on_hand() == Decimal("0")

    def test_the_mir_detail_says_what_each_line_put_into_stock(self, user):
        mir = _receive(user, "2", "50000", uom="MT")
        _issue(user, "500")
        info = stock_service.mir_line_stock(mir)[mir.lines.get().id]
        assert (info["uom"], info["in"], info["balance"]) == ("KG", Decimal("2000.000"), Decimal("1500.000"))


# ── Adjustments ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdjustments:
    def test_an_editors_adjustment_waits_for_an_admin_and_moves_nothing_until_then(self, user, admin):
        v = _adjust(user, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "500", "rate": "48", "reason": "OPENING_BALANCE"}])
        assert v.status == "PENDING" and _on_hand() == Decimal("0")
        stock_service.approve_adjustment(v, admin, "checked the sheet")
        v.refresh_from_db()
        assert v.status == "POSTED" and v.decided_by == admin
        assert _on_hand() == Decimal("500")
        assert StockLot.objects.get(voucher_line__voucher=v).rate == Decimal("48.0000")

    def test_an_admins_adjustment_posts_at_once(self, admin):
        v = _adjust(admin, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "rate": "1", "reason": "OPENING_BALANCE"}])
        assert v.status == "POSTED" and _on_hand() == Decimal("5")

    def test_turning_one_down_needs_a_note(self, user, admin):
        v = _adjust(user, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "rate": "1", "reason": "OPENING_BALANCE"}])
        with pytest.raises(StockValidationError):
            stock_service.reject_adjustment(v, admin, "")
        stock_service.reject_adjustment(v, admin, "no count sheet")
        v.refresh_from_db()
        assert v.status == "REJECTED" and _on_hand() == Decimal("0")

    def test_a_count_writes_off_the_difference_from_the_books_on_its_day(self, user, admin):
        _receive(user, "100", days_ago=3)
        v = _adjust(admin, [{"mode": "count", "material_id": _material().id, "uom": "KG", "counted": "92", "reason": "COUNT_LOSS", "note": "monthly count"}])
        line = v.lines.get()
        assert (line.book_qty, line.counted_qty, line.qty, line.direction) == (Decimal("100"), Decimal("92"), Decimal("8"), -1)
        assert _on_hand() == Decimal("92")

    def test_a_count_matching_the_books_or_with_a_reason_of_the_wrong_kind_is_refused(self, admin, user):
        _receive(user, "100")
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "count", "material_id": _material().id, "uom": "KG", "counted": "100", "reason": "COUNT_LOSS", "note": "x"}])
        assert "nothing to adjust" in _messages(exc)
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "count", "material_id": _material().id, "uom": "KG", "counted": "110", "reason": "COUNT_LOSS", "note": "x"}])
        assert "lines.0.reason" in _fields(exc)

    def test_an_addition_without_a_rate_takes_the_latest_receipts_or_must_be_given_one(self, admin, user):
        with pytest.raises(StockValidationError) as exc:
            _adjust(admin, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "reason": "OPENING_BALANCE"}])
        assert "lines.0.rate" in _fields(exc)
        _receive(user, "100", "55")
        v = _adjust(admin, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "reason": "COUNT_GAIN", "note": "found"}])
        assert v.lines.get().rate == Decimal("55.0000")

    def test_approval_re_checks_a_write_off_against_stock_that_moved_since(self, user, admin):
        _receive(user, "100")
        v = _adjust(user, [{"mode": "remove", "material_id": _material().id, "uom": "KG", "qty": "30", "reason": "DAMAGED_EXPIRED", "note": "wet"}])
        _issue(user, "80")
        with pytest.raises(StockValidationError):
            stock_service.approve_adjustment(v, admin, "ok")
        assert StockVoucher.objects.get(pk=v.pk).status == "PENDING"

    def test_an_opening_balance_already_issued_from_cannot_be_cancelled(self, admin, user):
        v = _adjust(admin, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "50", "rate": "40", "reason": "OPENING_BALANCE"}])
        _issue(user, "10")
        with pytest.raises(StockValidationError):
            stock_service.cancel_voucher(v, admin, "wrong figure")

    def test_the_person_who_entered_it_cannot_approve_it(self, user):
        v = _adjust(user, [{"mode": "add", "material_id": _material().id, "uom": "KG", "qty": "5", "rate": "1", "reason": "OPENING_BALANCE"}])
        with pytest.raises(StockValidationError):
            stock_service.approve_adjustment(v, user, "self")


# ── Reading ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLedger:
    def test_the_ledger_runs_receipt_issue_return_in_date_order(self, user):
        _receive(user, "100", days_ago=4)
        issue = _issue(user, "60", days_ago=2)
        _return(user, issue, "10")
        detail = stock_service.material_detail(Plant.objects.get(code="hrs"), _material(), "KG")
        assert [(e["kind"], e["qty"], e["balance"]) for e in detail["ledger"]] == [
            ("RECEIPT", Decimal("100.000"), Decimal("100.000")), ("ISSUE", Decimal("-60"), Decimal("40.000")),
            ("RETURN", Decimal("10"), Decimal("50.000"))]

    def test_the_stock_row_values_what_is_left_at_each_lots_rate(self, user):
        _receive(user, "100", "50", days_ago=3)
        _receive(user, "100", "60", days_ago=1)
        _issue(user, "120")
        row = stock_service.stock_rows(["hrs"])[0]
        assert (row["qty"], row["value"], row["open_lots"]) == (Decimal("80"), Decimal("4800.00"), 1)


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
    def test_the_last_of_a_material_issued_twice_at_once_is_issued_once(self):
        user = make_user(email="k1@ravasco.com", role="editor")
        _receive(user, "100")
        results = _in_parallel(lambda: _issue(user, "70"), lambda: _issue(user, "70"))
        assert sum(isinstance(r, StockVoucher) for r in results) == 1, results
        assert _on_hand() == Decimal("30")
