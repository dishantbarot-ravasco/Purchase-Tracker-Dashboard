"""
apps/services/mir_service.py - MIR entry's rules, on real Postgres.

Each class is one rule of the design (2026-09-28): the quantity and rate
mismatches that need a reason, partial and split deliveries, rejected
quantity and its reason, an invoice number that may repeat (with a notice), the
GST rate against the PO's, the material category, receiving another
plant's PO, one vendor per MIR, the date and number checks, numbering,
cancelling, and two clerks posting at the same moment.
"""

import datetime
import threading
from decimal import Decimal

import pytest
from django.db import connections
from django.utils import timezone

from apps.api.tests.factories import make_user
from apps.core.models import Mir, MirMismatch, MirSequence, Plant, PurchaseOrder, PurchaseOrderLine, Vendor
from apps.services import materials, mir_service
from apps.services.mir_service import MirValidationError

MH = "27AAACP5506B1ZW"  # Maharashtra vendor
DN = "26AABFH7112D1ZX"  # Dadra & Nagar Haveli vendor (HRS's own state)
GJ = "24AABCP1234C1Z5"  # Gujarat vendor (Vapi's state)
TODAY = timezone.localdate()



def _vendor(gstin=MH, name="Prime Chemicals"):
    return Vendor.objects.create(gstin=gstin, name=name, name_key=name.lower().replace(" ", ""))


def _po(plant="vapi", number="1000009001", vendor="default", lines=((Decimal("100"), Decimal("50")),), po_date=None, tax_type="IGST"):
    if vendor == "default":
        vendor = Vendor.objects.filter(gstin=MH).first() or _vendor()
    po = PurchaseOrder.objects.create(plant=Plant.objects.get(code=plant), po_number=number, vendor=vendor,
                                      po_date=po_date or TODAY - datetime.timedelta(days=30), tax_type=tax_type)
    for n, (qty, rate) in enumerate(lines, start=1):
        PurchaseOrderLine.objects.create(purchase_order=po, line_no=n, description=f"Material {n}", uom="KG",
                                         qty_ordered=qty, rate=rate, material=materials.material_for(f"Material {n}"))
    return po


def _line(po, n=1):
    return po.lines.get(line_no=n)


def _payload(lines, *, plant="hrs", invoice_no="INV-42", invoice_total=None, **extra):
    body = {"plant": plant, "mir_date": TODAY.isoformat(), "invoice_no": invoice_no,
            "invoice_date": TODAY.isoformat(), "lines": lines}
    body.update(extra)
    if invoice_total is None:
        preview = mir_service.evaluate(body)
        invoice_total = str(preview["computed_total"]) if preview["computed_total"] is not None else "0"
    body["invoice_total"] = invoice_total
    return body


def _ln(line, qty="100", rate=None, gst="18", **extra):
    return {"po_line_id": line.id, "qty_received": qty, "rate": str(rate if rate is not None else line.rate), "gst_rate": gst,
            "material_category": "Carbon Black", **extra}


def _fields(exc):
    return {e["field"] for e in exc.value.errors}


@pytest.fixture
def user(db):
    return make_user(email="store@ravasco.com", role="editor")


@pytest.mark.django_db
class TestExactReceipt:
    def test_posts_with_no_mismatch_and_every_figure_computed(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line)]), user)
        fy = mir.fy
        assert mir.mir_no == f"HRS/{fy[2:4]}-{fy[5:7]}/0001"
        assert mir.created_by == user and mir.created_by_email == "store@ravasco.com"
        ml = mir.lines.get()
        # Maharashtra vendor into HRS (DNH): IGST
        assert (ml.gross, ml.taxable, ml.igst, ml.cgst, ml.line_total) == (
            Decimal("5000.00"), Decimal("5000.00"), Decimal("900.00"), Decimal("0.00"), Decimal("5900.00"))
        assert mir.tax_type == "IGST" and not mir.mismatches.exists()
        state = mir_service.line_state(line, mir_service.accepted_by_line([line.id])[line.id])
        assert state["status"] == "received" and not state["receivable"]

    def test_preview_saves_nothing(self, user):
        line = _line(_po())
        result = mir_service.evaluate(_payload([_ln(line)]))
        assert result["ok"] and Mir.objects.count() == 0 and MirSequence.objects.count() == 0


@pytest.mark.django_db
class TestTaxType:
    def test_same_union_territory_is_cgst_ugst(self, user):
        line = _line(_po(vendor=_vendor(DN, "Local Supplier")))
        mir = mir_service.post_mir(_payload([_ln(line)]), user)
        ml = mir.lines.get()
        assert mir.tax_type == "CGST_UGST" and (ml.cgst, ml.sgst, ml.igst) == (Decimal("450.00"), Decimal("450.00"), Decimal("0.00"))

    def test_same_state_is_cgst_sgst(self, user):
        line = _line(_po(vendor=_vendor(GJ, "Gujarat Supplier")))
        mir = mir_service.post_mir(_payload([_ln(line)], plant="vapi"), user)
        assert mir.tax_type == "CGST_SGST"

    def test_a_different_tax_type_needs_a_reason(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], tax_type="CGST_UGST"), user)
        assert "tax_type_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(line)], tax_type="CGST_UGST", tax_type_reason="VENDOR_WRONG_TAX"), user)
        mm = mir.mismatches.get()
        assert (mm.kind, mm.mir_line, mm.status) == ("TAX_TYPE", None, "OPEN")


@pytest.mark.django_db
class TestQuantity:
    def test_short_needs_a_reason_and_keeps_the_line_open(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="60")]), user)
        assert "lines.0.qty_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(line, qty="60", qty_reason="PARTIAL_BALANCE_DUE")]), user)
        mm = mir.mismatches.get()
        assert (mm.kind, mm.expected, mm.actual, mm.difference_pct) == ("QTY_SHORT", Decimal("100"), Decimal("60"), Decimal("-40.00"))
        state = mir_service.line_state(line, mir_service.accepted_by_line([line.id])[line.id])
        assert (state["status"], state["open_qty"], state["receivable"]) == ("partial", Decimal("40.000"), True)

    def test_split_deliveries_add_up_and_the_last_one_needs_no_reason(self, user):
        line = _line(_po())
        mir_service.post_mir(_payload([_ln(line, qty="60", qty_reason="PARTIAL_BALANCE_DUE")], invoice_no="A1"), user)
        second = mir_service.post_mir(_payload([_ln(line, qty="40")], invoice_no="A2"), user)
        assert not second.mismatches.exists()
        assert second.lines.get().open_qty_before == Decimal("40")
        assert mir_service.accepted_by_line([line.id])[line.id] == Decimal("100")

    def test_a_close_the_line_reason_closes_it(self, user):
        line = _line(_po())
        mir_service.post_mir(_payload([_ln(line, qty="90", qty_reason="VENDOR_SHORT_CLOSE")]), user)
        line.refresh_from_db()
        assert line.closed_at is not None and line.closed_reason.code == "VENDOR_SHORT_CLOSE"
        state = mir_service.line_state(line, Decimal("90"))
        assert state["status"] == "closed" and not state["receivable"]

    def test_over_needs_a_reason(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="108")]), user)
        assert "lines.0.qty_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(line, qty="108", qty_reason="WEIGHBRIDGE_VARIANCE")]), user)
        assert mir.mismatches.get().kind == "QTY_OVER"

    def test_a_reason_of_the_wrong_kind_is_refused(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="108", qty_reason="PARTIAL_BALANCE_DUE")]), user)
        assert "lines.0.qty_reason" in _fields(exc)

    def test_a_reason_that_needs_a_note_gets_one(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="90", qty_reason="SHORT_OTHER")]), user)
        assert "lines.0.qty_note" in _fields(exc)
        mir_service.post_mir(_payload([_ln(line, qty="90", qty_reason="SHORT_OTHER", qty_note="Truck broke down")]), user)

    def test_rejected_quantity_does_not_count_as_received(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line, qty="100", qty_rejected="10", qty_reason="QC_REJECTED",
                                                 reject_reason="REJ_QUALITY")]), user)
        assert mir.mismatches.get(kind="QTY_SHORT").actual == Decimal("90")
        rejected = mir.mismatches.get(kind="QTY_REJECTED")
        assert rejected.actual == Decimal("10") and rejected.reason.code == "REJ_QUALITY" and rejected.status == "OPEN"
        assert mir_service.accepted_by_line([line.id])[line.id] == Decimal("90")

    def test_nothing_accepted_is_still_a_receipt(self, user):
        line = _line(_po())
        mir_service.post_mir(_payload([_ln(line, qty="100", qty_rejected="100", qty_reason="QC_REJECTED",
                                          reject_reason="REJ_DAMAGED")]), user)
        assert mir_service.accepted_by_line([line.id])[line.id] == Decimal("0")

    def test_a_rejected_quantity_needs_a_rejection_reason(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="100", qty_rejected="10", qty_reason="QC_REJECTED")]), user)
        assert "lines.0.reject_reason" in _fields(exc)
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="100", qty_rejected="10", qty_reason="QC_REJECTED",
                                               reject_reason="PARTIAL_BALANCE_DUE")]), user)
        assert "lines.0.reject_reason" in _fields(exc)

    @pytest.mark.parametrize("qty, rejected, field", [
        ("0", None, "qty_received"), ("-5", None, "qty_received"), ("abc", None, "qty_received"),
        ("1.2345", None, "qty_received"), ("10", "11", "qty_rejected"), ("10", "-1", "qty_rejected"),
    ])
    def test_bad_quantities(self, user, qty, rejected, field):
        line = _line(_po())
        extra = {"qty_rejected": rejected} if rejected is not None else {}
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty=qty, **extra)], invoice_total="1"), user)
        assert f"lines.0.{field}" in _fields(exc)


@pytest.mark.django_db
class TestOutsizedFigures:
    def test_a_huge_over_receipt_posts_with_its_percentage_capped(self, user):
        """0.010 KG open against 1000.02 KG received is ~1e7 %, past
        MirMismatch.difference_pct's numeric(9, 2) - preview passed and the
        post raised a numeric overflow (a 500)."""
        line = _line(_po(lines=((Decimal("0.010"), Decimal("50")),)))
        mir = mir_service.post_mir(_payload([_ln(line, qty="1000.02", qty_reason="EXCESS_ACCEPTED")]), user)
        assert MirMismatch.objects.get(mir=mir).difference_pct == Decimal("9999999.99")

    def test_header_fields_are_cut_to_their_own_column(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line)], vehicle_no="V" * 50, eway_bill_no="E" * 50,
                                            gate_entry_no="G" * 50, weighbridge_slip_no="W" * 50), user)
        mir.refresh_from_db()
        assert (len(mir.vehicle_no), len(mir.eway_bill_no), len(mir.gate_entry_no), len(mir.weighbridge_slip_no)) == (30, 30, 40, 40)


@pytest.mark.django_db
class TestRate:
    def test_higher_and_lower_rates_need_a_reason(self, user):
        po = _po(lines=((Decimal("100"), Decimal("50")), (Decimal("100"), Decimal("50"))))
        l1, l2 = _line(po, 1), _line(po, 2)
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(l1, rate="52"), _ln(l2, rate="48")]), user)
        assert {"lines.0.rate_reason", "lines.1.rate_reason"} <= _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(l1, rate="52", rate_reason="VENDOR_BILLING_ERROR"),
                                             _ln(l2, rate="48", rate_reason="DISCOUNT_NOT_APPLIED")]), user)
        kinds = {(m.mir_line.line_no, m.kind) for m in mir.mismatches.all()}
        assert kinds == {(1, "RATE_HIGH"), (2, "RATE_LOW")}
        assert mir.lines.get(line_no=1).po_rate == Decimal("50")

    def test_the_same_rate_written_differently_is_not_a_mismatch(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line, rate="50.0000")]), user)
        assert not mir.mismatches.exists()

    def test_free_material(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line, rate="0", rate_reason="FREE_REPLACEMENT")]), user)
        assert mir.mismatches.get().kind == "RATE_LOW" and mir.lines.get().line_total == Decimal("0.00")


@pytest.mark.django_db
class TestInvoiceTotal:
    def test_the_rounding_rupee_is_not_a_mismatch(self, user):
        line = _line(_po(lines=((Decimal("3"), Decimal("33.33")),)))
        # 99.99 + 18% = 117.99 (17.998 -> 18.00); printed as 118
        mir = mir_service.post_mir(_payload([_ln(line, qty="3")], invoice_total="118.00"), user)
        assert not mir.mismatches.exists()

    def test_more_than_a_rupee_needs_a_reason(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], invoice_total="6100.00"), user)
        assert "invoice_total_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(line)], invoice_total="6100.00", invoice_total_reason="INVOICE_EXTRA_CHARGES",
                                            invoice_total_note="Loading charges"), user)
        mm = mir.mismatches.get()
        assert (mm.kind, mm.expected, mm.actual) == ("INVOICE_TOTAL", Decimal("5900.00"), Decimal("6100.00"))

    def test_tcs_counts_in_the_computed_total(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line)], tcs_amount="5.90", invoice_total="5905.90"), user)
        assert not mir.mismatches.exists()


@pytest.mark.django_db
class TestRepeatedInvoice:
    """The invoice number is required but not unique (owner, 2026-09-29):
    one invoice can arrive as several deliveries, each its own MIR, as the
    Drive MIR files show (one BST invoice on five HRS MIRs)."""

    def test_the_same_invoice_is_saved_again_with_a_notice_naming_the_earlier_mir(self, user):
        line_a = _line(_po(number="1000009001"))
        line_b = _line(_po(plant="achhad", number="1100009001", vendor=Vendor.objects.get(gstin=MH)))
        first = mir_service.post_mir(_payload([_ln(line_a)], invoice_no="INV-0042"), user)
        body = _payload([_ln(line_b)], plant="vapi", invoice_no="inv - 42")
        preview = mir_service.evaluate(body)
        assert preview["ok"] is True
        assert len(preview["notices"]) == 1 and first.mir_no in preview["notices"][0]
        second = mir_service.post_mir(body, user)
        assert second.mir_no != first.mir_no

    def test_the_invoice_number_is_required(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], invoice_no="  ", invoice_total="1"), user)
        assert "invoice_no" in _fields(exc)

    def test_another_vendor_may_use_the_same_invoice_number(self, user):
        line_a = _line(_po(number="1000009001"))
        line_b = _line(_po(number="1000009002", vendor=_vendor(GJ, "Other Vendor")))
        mir_service.post_mir(_payload([_ln(line_a)], invoice_no="42"), user)
        mir_service.post_mir(_payload([_ln(line_b)], invoice_no="42"), user)

    def test_a_cancelled_mir_frees_its_invoice(self, user):
        line = _line(_po())
        first = mir_service.post_mir(_payload([_ln(line)]), user)
        mir_service.cancel_mir(first, user, "Entered twice")
        mir_service.post_mir(_payload([_ln(line)]), user)


@pytest.mark.django_db
class TestCrossPlantAndVendor:
    def test_hrs_receives_a_vapi_po(self, user):
        line = _line(_po(plant="vapi"))
        mir = mir_service.post_mir(_payload([_ln(line)], plant="hrs"), user)
        assert mir.plant.code == "hrs" and mir.lines.get().po_line.purchase_order.plant.code == "vapi"

    def test_lines_of_two_pos_of_one_vendor_share_a_mir(self, user):
        a = _line(_po(number="1000009001"))
        b = _line(_po(number="1000009002"))
        mir = mir_service.post_mir(_payload([_ln(a), _ln(b)]), user)
        assert mir.lines.count() == 2

    def test_two_vendors_cannot_share_a_mir(self, user):
        a = _line(_po(number="1000009001"))
        b = _line(_po(number="1000009002", vendor=_vendor(GJ, "Other Vendor")))
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(a), _ln(b)], invoice_total="1"), user)
        assert "lines" in _fields(exc)

    def test_a_po_with_no_vendor_needs_the_vendor_chosen(self, user):
        line = _line(_po(vendor=None))
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], invoice_total="1"), user)
        assert "vendor_id" in _fields(exc)
        vendor = _vendor(GJ, "Madura")
        mir = mir_service.post_mir(_payload([_ln(line)], vendor_id=vendor.id), user)
        assert mir.vendor == vendor

    def test_a_chosen_vendor_cannot_override_the_pos(self, user):
        line = _line(_po())
        other = _vendor(GJ, "Someone Else")
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], vendor_id=other.id, invoice_total="1"), user)
        assert "vendor_id" in _fields(exc)


@pytest.mark.django_db
class TestWhatCannotBeReceived:
    def _blocked(self, user, line):
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], invoice_total="1"), user)
        return next(e["message"] for e in exc.value.errors if e["field"] == "lines.0.po_line_id")

    def test_a_retired_po(self, user):
        po = _po()
        PurchaseOrder.objects.filter(pk=po.pk).update(is_active=False)
        assert "no longer in the master PO sheet" in self._blocked(user, _line(po))

    def test_a_dropped_line(self, user):
        line = _line(_po())
        PurchaseOrderLine.objects.filter(pk=line.pk).update(is_active=False)
        assert "no longer on the PO" in self._blocked(user, line)

    def test_a_line_waiting_for_review(self, user):
        line = _line(_po())
        PurchaseOrderLine.objects.filter(pk=line.pk).update(needs_review=True)
        assert "review" in self._blocked(user, line)

    def test_a_closed_line(self, user):
        line = _line(_po())
        PurchaseOrderLine.objects.filter(pk=line.pk).update(closed_at=timezone.now())
        assert "closed" in self._blocked(user, line)

    def test_a_line_received_in_full(self, user):
        line = _line(_po())
        mir_service.post_mir(_payload([_ln(line)], invoice_no="A1"), user)
        assert "received in full" in self._blocked(user, line)

    def test_a_line_with_no_quantity_or_rate(self, user):
        line = _line(_po())
        PurchaseOrderLine.objects.filter(pk=line.pk).update(rate=None)
        assert "no rate" in self._blocked(user, line)

    def test_the_same_line_twice(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="50"), _ln(line, qty="50")], invoice_total="1"), user)
        assert "lines.1.po_line_id" in _fields(exc)


@pytest.mark.django_db
class TestHeaderChecks:
    def test_dates(self, user):
        line = _line(_po(po_date=TODAY - datetime.timedelta(days=5)))
        future = (TODAY + datetime.timedelta(days=1)).isoformat()
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], mir_date=future, invoice_total="1"), user)
        assert "mir_date" in _fields(exc)
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], invoice_date=future, invoice_total="1"), user)
        assert "invoice_date" in _fields(exc)
        before_po = (TODAY - datetime.timedelta(days=6)).isoformat()
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line)], mir_date=before_po, invoice_date=before_po, invoice_total="1"), user)
        assert "mir_date" in _fields(exc)

    def test_required_fields(self, user):
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir({"plant": "hrs", "lines": []}, user)
        assert {"mir_date", "invoice_no", "invoice_date", "invoice_total", "lines"} <= _fields(exc)

    def test_gst_must_be_a_slab(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, gst="17")], invoice_total="1"), user)
        assert "lines.0.gst_rate" in _fields(exc)

    def test_a_discount_above_the_line_value(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, discount="6000")], invoice_total="1"), user)
        assert "lines.0.discount" in _fields(exc)


@pytest.mark.django_db
class TestNumberingAndCancelling:
    def test_numbers_run_per_plant_and_a_cancelled_mir_keeps_its_number(self, user):
        po = _po(lines=[(Decimal("10"), Decimal("50"))] * 4)
        m1 = mir_service.post_mir(_payload([_ln(_line(po, 1), qty="10")], invoice_no="1"), user)
        mir_service.cancel_mir(m1, user, "Wrong vendor invoice")
        m2 = mir_service.post_mir(_payload([_ln(_line(po, 2), qty="10")], invoice_no="2"), user)
        v1 = mir_service.post_mir(_payload([_ln(_line(po, 3), qty="10")], invoice_no="3", plant="vapi"), user)
        assert (m1.seq, m2.seq, v1.seq) == (1, 2, 1)
        assert v1.mir_no.startswith("VAPI/")

    def test_cancelling_restores_the_open_quantity_voids_mismatches_and_reopens_a_closed_line(self, user):
        line = _line(_po())
        mir = mir_service.post_mir(_payload([_ln(line, qty="90", qty_reason="VENDOR_SHORT_CLOSE")]), user)
        mir_service.cancel_mir(mir, user, "Posted against the wrong PO")
        line.refresh_from_db()
        assert line.closed_at is None
        assert mir_service.accepted_by_line([line.id]).get(line.id, Decimal("0")) == Decimal("0")
        assert set(mir.mismatches.values_list("status", flat=True)) == {"VOID"}
        mir.refresh_from_db()
        assert (mir.status, mir.cancelled_by, mir.cancel_reason) == ("CANCELLED", user, "Posted against the wrong PO")

    def test_cancel_needs_a_reason_and_happens_once(self, user):
        mir = mir_service.post_mir(_payload([_ln(_line(_po()))]), user)
        with pytest.raises(MirValidationError):
            mir_service.cancel_mir(mir, user, " ")
        mir_service.cancel_mir(mir, user, "x")
        with pytest.raises(MirValidationError):
            mir_service.cancel_mir(mir, user, "again")


@pytest.mark.django_db
class TestResolutionAndLineUpkeep:
    def test_resolving_a_mismatch(self, user):
        mir = mir_service.post_mir(_payload([_ln(_line(_po()), qty="60", qty_reason="PARTIAL_BALANCE_DUE")]), user)
        mm = mir.mismatches.get()
        with pytest.raises(MirValidationError):
            mir_service.resolve_mismatch(mm, user, "")
        mir_service.resolve_mismatch(mm, user, "Balance received on the next truck")
        mm.refresh_from_db()
        assert (mm.status, mm.resolved_by) == ("RESOLVED", user)
        with pytest.raises(MirValidationError):
            mir_service.resolve_mismatch(mm, user, "again")

    def test_closing_and_reopening_a_line_by_hand(self, user):
        line = _line(_po())
        with pytest.raises(MirValidationError):
            mir_service.close_po_line(line, user, "PARTIAL_BALANCE_DUE", "")  # does not close lines
        mir_service.close_po_line(line, user, "VENDOR_SHORT_CLOSE", "")
        line.refresh_from_db()
        assert line.closed_at and line.close_note
        mir_service.reopen_po_line(line)
        line.refresh_from_db()
        assert line.closed_at is None

    def test_clearing_a_review_flag_is_logged(self, user):
        line = _line(_po())
        PurchaseOrderLine.objects.filter(pk=line.pk).update(needs_review=True, review_note="sheet changed")
        mir_service.clear_line_review(line, user, "Same material, typo fixed")
        line.refresh_from_db()
        assert not line.needs_review and line.changes.get().field == "needs_review"


# ── Two clerks at once ──────────────────────────────────────────────────


def _in_parallel(fn_a, fn_b):
    """Runs both on their own connections, released together."""
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

    threads = [threading.Thread(target=run, args=(0, fn_a)), threading.Thread(target=run, args=(1, fn_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return results


@pytest.mark.django_db(transaction=True)
class TestConcurrentPosting:
    def test_two_mirs_posted_at_once_at_one_plant_get_different_numbers(self):
        user = make_user(email="c1@ravasco.com", role="editor")
        line = _line(_po(lines=((Decimal("100"), Decimal("50")), (Decimal("100"), Decimal("50")))))
        po = line.purchase_order
        a = _payload([_ln(_line(po, 1))])
        b = _payload([_ln(_line(po, 2))])
        results = _in_parallel(lambda: mir_service.post_mir(a, user), lambda: mir_service.post_mir(b, user))
        assert all(isinstance(r, Mir) for r in results), results
        assert sorted(r.seq for r in results) == [1, 2]

    def test_one_line_received_in_full_twice_at_once_is_received_once(self):
        user = make_user(email="c2@ravasco.com", role="editor")
        line = _line(_po())
        a = _payload([_ln(line)], invoice_no="X1")
        b = _payload([_ln(line)], invoice_no="X2")
        results = _in_parallel(lambda: mir_service.post_mir(a, user), lambda: mir_service.post_mir(b, user))
        assert sum(isinstance(r, Mir) for r in results) == 1, results
        assert mir_service.accepted_by_line([line.id])[line.id] == Decimal("100")
        assert not MirMismatch.objects.exists()


@pytest.mark.django_db
class TestGstRate:
    def _po_at_18(self):
        po = _po()
        po.total_value, po.total_inclusive_value = Decimal("5000"), Decimal("5900")
        po.save()
        return _line(po)

    def test_a_gst_rate_other_than_the_pos_needs_a_reason(self, user):
        line = self._po_at_18()
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, gst="12")]), user)
        assert "lines.0.gst_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(line, gst="12", gst_reason="GST_HSN_DIFFERENT")]), user)
        mm = mir.mismatches.get()
        assert (mm.kind, mm.expected, mm.actual) == ("GST_RATE", Decimal("18"), Decimal("12"))

    def test_the_pos_own_rate_is_no_mismatch(self, user):
        mir = mir_service.post_mir(_payload([_ln(self._po_at_18(), gst="18")]), user)
        assert not mir.mismatches.exists()

    def test_a_po_without_both_totals_is_not_compared(self, user):
        mir = mir_service.post_mir(_payload([_ln(_line(_po()), gst="5")]), user)
        assert not mir.mismatches.exists()


@pytest.mark.django_db
class TestMaterialCategory:
    """The category lives on the material master: an unfiled material takes
    the clerk's pick at its first MIR, and every later receipt reads it from
    the master, whatever the form sends."""

    def _reference(self):
        from apps.core.models import MaterialCategoryReference
        MaterialCategoryReference.objects.create(description="Zinc", normalized_description="zinc",
                                                 category="Carbon Black", subcategory="N330", sap_item_code="")

    def test_category_is_required_for_an_unfiled_material(self, user):
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(_line(_po()), material_category="")], invoice_total="1"), user)
        assert "lines.0.material_category" in _fields(exc)

    def test_the_first_mir_files_the_material_and_later_ones_read_it(self, user):
        self._reference()
        line = _line(_po(lines=((Decimal("100"), Decimal("50")),)))
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="50", material_category="Carbon")], invoice_total="1"), user)
        assert "lines.0.material_category" in _fields(exc)
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(line, qty="50", material_subcategory="Nope")], invoice_total="1"), user)
        assert "lines.0.material_subcategory" in _fields(exc)
        mir_service.post_mir(_payload([_ln(line, qty="50", qty_reason="PARTIAL_BALANCE_DUE", material_subcategory="N330")]), user)
        line.material.refresh_from_db()
        assert (line.material.category, line.material.subcategory) == ("Carbon Black", "N330")
        assert line.material.category_set_by_email == user.email
        # A second receipt sends another category: the master's stands.
        preview = mir_service.evaluate(_payload([_ln(line, qty="50", material_category="Something else")], invoice_no="INV-2"))
        assert not any(e["field"].endswith("material_category") for e in preview["errors"])
        assert preview["lines"][0]["material_category"] == "Carbon Black"
        assert preview["lines"][0]["category_from_master"] is True


@pytest.mark.django_db
class TestSapGrnNumber:
    def test_it_is_optional_and_stored(self, user):
        line = _line(_po())
        assert mir_service.post_mir(_payload([_ln(line, qty="50", qty_reason="PARTIAL_BALANCE_DUE")]), user).sap_grn_number == ""
        mir = mir_service.post_mir(_payload([_ln(line, qty="50")], invoice_no="INV-43", sap_grn_number="5000123456"), user)
        assert mir.sap_grn_number == "5000123456"


@pytest.mark.django_db
class TestInvoiceBeforePo:
    """An invoice dated before its PO (owner, 2026-09-29) needs a reason
    rather than being refused: in the Drive MIRs 22 of 430 Vapi receipts
    with a known PO do this (verbal orders, advance billing)."""

    def test_it_needs_a_reason_and_is_recorded_in_days(self, user):
        po = _po(po_date=TODAY - datetime.timedelta(days=10))
        early = (TODAY - datetime.timedelta(days=16)).isoformat()
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(_line(po))], invoice_date=early), user)
        assert "invoice_date_reason" in _fields(exc)
        mir = mir_service.post_mir(_payload([_ln(_line(po))], invoice_date=early, invoice_date_reason="VERBAL_ORDER_PO_LATER"), user)
        mm = mir.mismatches.get()
        assert (mm.kind, mm.actual, mm.reason.code, mm.status) == ("INVOICE_BEFORE_PO", Decimal("6"), "VERBAL_ORDER_PO_LATER", "OPEN")

    def test_on_or_after_the_po_date_is_no_difference(self, user):
        po = _po(po_date=TODAY - datetime.timedelta(days=10))
        mir = mir_service.post_mir(_payload([_ln(_line(po))], invoice_date=(TODAY - datetime.timedelta(days=10)).isoformat()), user)
        assert not mir.mismatches.exists()

    def test_the_latest_po_on_the_mir_is_the_one_compared(self, user):
        a = _line(_po(number="1000009001", po_date=TODAY - datetime.timedelta(days=30)))
        b = _line(_po(number="1000009002", po_date=TODAY - datetime.timedelta(days=5)))
        preview = mir_service.evaluate(_payload([_ln(a), _ln(b)], invoice_date=(TODAY - datetime.timedelta(days=8)).isoformat()))
        assert [m["actual"] for m in preview["mismatches"] if m["kind"] == "INVOICE_BEFORE_PO"] == [Decimal("3")]

    def test_goods_received_before_the_po_are_still_refused(self, user):
        po = _po(po_date=TODAY)
        with pytest.raises(MirValidationError) as exc:
            mir_service.post_mir(_payload([_ln(_line(po))], mir_date=(TODAY - datetime.timedelta(days=1)).isoformat(),
                                          invoice_date=(TODAY - datetime.timedelta(days=1)).isoformat(), invoice_total="1"), user)
        assert "mir_date" in _fields(exc)

    def test_an_edit_cannot_move_the_invoice_before_the_po(self, user):
        po = _po(po_date=TODAY - datetime.timedelta(days=10))
        mir = mir_service.post_mir(_payload([_ln(_line(po))]), user)
        with pytest.raises(MirValidationError) as exc:
            mir_service.edit_mir(mir, user, {"invoice_date": (TODAY - datetime.timedelta(days=12)).isoformat()}, {}, "typo")
        assert "before the PO date" in exc.value.errors[0]["message"]
