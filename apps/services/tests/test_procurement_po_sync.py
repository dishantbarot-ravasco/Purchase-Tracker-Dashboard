"""
The PO CSV now writes as a DIFF (2026-09-28): the legacy per-plant mirror
through sync_utils.sync_line_items(), and the normalized procurement tables
through procurement_sync.project_plant_orders(). Driven through the real
sync_po_csv command on a local --file, as test_sync_po_csv_pipeline.py does.

What must hold, because MIR lines point at PO lines:
  - an unchanged line keeps its row (same pk) and its match rows;
  - a changed field is written in place and logged, old and new;
  - a dropped line is deleted from the legacy mirror but only DEACTIVATED
    in the procurement tables, and a line with receipts is flagged when the
    sheet changes what it is.
"""

import csv
import datetime
import io
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSPOMirMatch,
    Mir,
    MirLine,
    Plant,
    PurchaseOrder,
    PurchaseOrderLineChange,
    Vendor,
)
from apps.services.parsers.po_csv import EXPECTED_HEADER
from apps.services.procurement_sync import project_plant_orders, upsert_vendor


_BASE = {
    "PO Drive Folder Name": "PO_3000009001", "PO Number": "3000009001",
    "PO Created Date": "01-05-2026", "Vendor Name": "Prime Chemicals",
    "Vendor Address": "Mumbai", "Vendor GSTIN": "27AAACP5506B1ZW",
    "Vendor Email": "a@prime.example", "Vendor Code": "300001620",
    "Billing Address": "HRS", "ShipTo": "HRS", "HSN ": "4002", "UOM": "KG",
    "Delivery Date ": "15-05-2026", "Payment Terms ": "60 Days", "IncoTerms": "DDP",
    "Currency ": "INR", "Total Value": "300000.00", "Tax Type": "IGST",
    "Total Inclusive Value": "354000.00", "Remarks": "",
}


def _row(item_id, desc, qty, rate, **extra):
    row = dict(_BASE, **{"Item Id": item_id, "Material Description": desc, "QTY": str(qty), "Net Price": str(rate),
                         "Net Value": str(Decimal(qty) * Decimal(rate)), "PO Number | Item Id": f"3000009001|{item_id}"})
    row.update(extra)
    return row


def _sync(tmp_path, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPECTED_HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path = tmp_path / "po.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")
    call_command("sync_po_csv", file=str(path))


def _three_lines():
    return [_row("1", "SBR 1502", 1000, 100), _row("2", "Zinc Oxide", 500, 200), _row("3", "Carbon N330", 100, 1000)]


def _legacy_lines():
    return list(HRSDomesticPurchaseOrder.objects.get(po_number="3000009001").items.order_by("pk"))


def _lines():
    return list(PurchaseOrder.objects.get(plant__code="hrs", po_number="3000009001").lines.order_by("line_no"))


@pytest.mark.django_db
class TestLegacyMirrorIsADiff:
    def test_an_unchanged_line_keeps_its_row_and_its_match(self, tmp_path):
        _sync(tmp_path, _three_lines())
        first = _legacy_lines()
        mir = HRSMIREntry.objects.create(month="May-26", mir_no="M1", mir_date=datetime.date(2026, 5, 2), party_name="Prime",
                                         material_description="SBR 1502", qty=Decimal("1000"), uom="KG", rate=Decimal("100"),
                                         source_row_ref="r1", is_active=True)
        match = HRSPOMirMatch.objects.create(po_line_item=first[0], mir_entry=mir, tier="po_number", match_score=Decimal("1"))

        rows = _three_lines()
        rows[1] = _row("2", "Zinc Oxide", 600, 200)  # only line 2 changes
        _sync(tmp_path, rows)

        second = _legacy_lines()
        assert [x.pk for x in second] == [x.pk for x in first]
        assert second[1].qty == Decimal("600")
        assert HRSPOMirMatch.objects.filter(pk=match.pk).exists(), "an untouched line's match was deleted"

    def test_a_new_line_is_added_and_a_dropped_one_removed(self, tmp_path):
        _sync(tmp_path, _three_lines())
        before = _legacy_lines()
        _sync(tmp_path, _three_lines()[:2] + [_row("4", "Stearic Acid", 50, 150), _row("5", "Wax", 10, 90)])
        after = _legacy_lines()
        assert [x.pk for x in after[:3]] == [x.pk for x in before]
        assert [x.description for x in after] == ["SBR 1502", "Zinc Oxide", "Stearic Acid", "Wax"]

        _sync(tmp_path, _three_lines()[:1])
        assert [x.pk for x in _legacy_lines()] == [before[0].pk]
        assert HRSDomesticPOLineItem.objects.filter(purchase_order__po_number="3000009001").count() == 1


@pytest.mark.django_db
class TestProjection:
    def test_the_csv_is_projected_into_normalized_rows(self, tmp_path):
        _sync(tmp_path, [_row("1", "SBR 1502", 1000, 100, UOM="Kgs"), _row("2", "Oil", 5, 90, UOM="Ltr")])
        po = PurchaseOrder.objects.get(plant__code="hrs", po_number="3000009001")
        assert po.vendor.gstin == "27AAACP5506B1ZW" and po.tax_type == "IGST" and po.is_active
        assert [(ln.line_no, ln.uom, ln.uom_raw) for ln in _lines()] == [(1, "KG", "Kgs"), (2, "L", "Ltr")]

    def test_every_po_field_the_mir_form_shows_is_projected(self, tmp_path):
        _sync(tmp_path, [_row("1", "SBR 1502", 1000, 100, **{"Billing Address": "HRS, Silvassa", "ShipTo": "Vapi store"})])
        po = PurchaseOrder.objects.get(plant__code="hrs", po_number="3000009001")
        assert (po.billing_address, po.ship_to, po.payment_terms, po.incoterms) == ("HRS, Silvassa", "Vapi store", "60 Days", "DDP")
        assert (po.vendor.address, po.vendor.email, po.vendor.vendor_code) == ("Mumbai", "a@prime.example", "300001620")
        line = _lines()[0]
        assert (line.item_code, line.hsn, line.uom, line.rate, line.delivery_date.isoformat()) == ("1", "4002", "KG", Decimal("100"), "2026-05-15")

    def test_the_billing_plant_is_read_from_the_billing_address(self, tmp_path):
        """A PO belongs to the plant its billing address names (owner,
        2026-10-03); an HRS-sheet PO billed to Achhad is recorded as such."""
        _sync(tmp_path, [_row("1", "SBR 1502", 1000, 100, **{
            "Billing Address": "Ravasco Transmission and Packing, 95-99, Achhad Industrial estate, Talasari, Thane"})])
        po = PurchaseOrder.objects.get(plant__code="hrs", po_number="3000009001")
        assert po.billing_plant.code == "achhad"

    def test_a_second_run_writes_nothing(self, tmp_path):
        _sync(tmp_path, _three_lines())
        result = project_plant_orders("hrs")
        assert result.orders_written == 0 and result.lines_updated == 0

    def test_a_changed_line_is_updated_in_place_and_logged(self, tmp_path):
        _sync(tmp_path, _three_lines())
        before = _lines()
        rows = _three_lines()
        rows[2] = _row("3", "Carbon N330", 100, 1050)
        _sync(tmp_path, rows)
        after = _lines()
        assert [x.pk for x in after] == [x.pk for x in before]
        assert after[2].rate == Decimal("1050")
        # The rate and the net value it drives both changed; both are logged.
        logged = {c.field: (Decimal(c.old_value), Decimal(c.new_value))
                  for c in PurchaseOrderLineChange.objects.filter(po_line=after[2])}
        assert logged == {"rate": (Decimal("1000"), Decimal("1050")), "net_value": (Decimal("100000"), Decimal("105000"))}
        assert not after[2].needs_review, "a rate change without receipts needs no review"

    def test_a_sheet_change_does_not_undo_a_close_made_while_it_ran(self, tmp_path, monkeypatch):
        """The projection read the line unlocked and saved the whole row, so a
        short-close committed between that read and the save was put back to
        open. The close is committed here at exactly that point."""
        from apps.services import procurement_sync

        _sync(tmp_path, _three_lines())
        target = _lines()[2]
        real = procurement_sync._has_receipts

        def close_meanwhile(line):
            type(line).objects.filter(pk=target.pk).update(closed_at=datetime.datetime(2026, 9, 30, tzinfo=datetime.UTC))
            return real(line)

        monkeypatch.setattr(procurement_sync, "_has_receipts", close_meanwhile)
        rows = _three_lines()
        rows[2] = _row("3", "Carbon N330", 100, 1050)
        _sync(tmp_path, rows)

        line = _lines()[2]
        assert line.rate == Decimal("1050")
        assert line.closed_at is not None

    def test_a_dropped_line_is_deactivated_never_deleted(self, tmp_path):
        _sync(tmp_path, _three_lines())
        dropped = _lines()[2]
        _sync(tmp_path, _three_lines()[:2])
        dropped.refresh_from_db()
        assert dropped.is_active is False
        _sync(tmp_path, _three_lines())
        dropped.refresh_from_db()
        assert dropped.is_active is True, "a line the sheet lists again comes back"

    def test_a_line_with_receipts_is_flagged_when_the_sheet_changes_what_it_is(self, tmp_path):
        _sync(tmp_path, _three_lines())
        line = _lines()[0]
        _post_receipt(line)
        rows = _three_lines()
        rows[0] = _row("1", "SBR 1712", 1000, 100)
        _sync(tmp_path, rows)
        line.refresh_from_db()
        assert line.needs_review and "description" in line.review_note
        assert MirLine.objects.filter(po_line=line).exists(), "the receipt stays linked"

    def test_a_line_with_receipts_dropped_from_the_sheet_is_flagged(self, tmp_path):
        _sync(tmp_path, _three_lines())
        line = _lines()[2]
        _post_receipt(line)
        _sync(tmp_path, _three_lines()[:2])
        line.refresh_from_db()
        assert (line.is_active, line.needs_review) == (False, True)

    def test_the_command_projects_every_plant_and_reports_flagged_lines(self, tmp_path):
        _sync(tmp_path, _three_lines())
        line = _lines()[0]
        _post_receipt(line)
        HRSDomesticPOLineItem.objects.filter(purchase_order__po_number="3000009001", item_id="1").update(description="SBR 1712")
        HRSDomesticPurchaseOrder.objects.filter(po_number="3000009001").update(synced_from_row_hash="changed")
        out = io.StringIO()
        call_command("sync_procurement_pos", stdout=out)
        text = out.getvalue()
        assert "sync_procurement_pos hrs:" in text and "sync_procurement_pos vapi:" in text
        assert "need review - 3000009001 line 1" in text

    def test_a_retired_order_is_deactivated(self, tmp_path):
        _sync(tmp_path, _three_lines())
        other = [dict(r, **{"PO Number": "3000009002", "PO Drive Folder Name": "PO_3000009002",
                            "PO Number | Item Id": "3000009002|1"}) for r in _three_lines()[:1]]
        _sync(tmp_path, other)
        assert PurchaseOrder.objects.get(po_number="3000009001").is_active is False

    def test_an_order_the_app_owns_is_never_overwritten_or_duplicated(self, tmp_path):
        """The same PO number arriving from the CSV and from the app stays
        one order with the app's figures: its quantity is never counted
        twice, and the hourly sync does not undo what was confirmed."""
        _sync(tmp_path, _three_lines())
        po = PurchaseOrder.objects.get(plant__code="hrs", po_number="3000009001")
        po.source = PurchaseOrder.Source.APP
        po.save(update_fields=["source"])
        line = _lines()[0]
        line.qty_ordered = Decimal("900")
        line.save(update_fields=["qty_ordered"])

        rows = _three_lines()
        rows[0] = _row("1", "SBR 1502", 1200, 100)
        _sync(tmp_path, rows)

        assert PurchaseOrder.objects.filter(po_number="3000009001").count() == 1
        assert _lines()[0].qty_ordered == Decimal("900")
        assert len(_lines()) == 3
        assert project_plant_orders("hrs").orders_held == ["3000009001"]


@pytest.mark.django_db
class TestVendorMaster:
    def test_one_vendor_per_gstin_whatever_the_name(self):
        a = upsert_vendor("27AAACP5506B1ZW", "Prigo Engineers")
        b = upsert_vendor("27aaacp5506b1zw", "PRIGO ENGINEERS & CONSULTANTS PVT LTD", code="300001620")
        assert a.pk == b.pk
        b.refresh_from_db()
        assert b.vendor_code == "300001620" and b.name == "PRIGO ENGINEERS & CONSULTANTS PVT LTD"

    def test_a_blank_value_never_erases_one_on_file(self):
        upsert_vendor("27AAACP5506B1ZW", "Prigo", email="a@x.example")
        v = upsert_vendor("27AAACP5506B1ZW", "Prigo", email="")
        assert v.email == "a@x.example"

    def test_without_gstin_the_cleaned_name_is_the_key(self):
        a = upsert_vendor("", "M/s. Madura Industrial Textiles")
        b = upsert_vendor("", "MADURA INDUSTRIAL TEXTILES")
        assert a.pk == b.pk and a.gstin == ""

    def test_no_name_and_no_gstin_is_no_vendor(self):
        assert upsert_vendor("", "") is None
        assert Vendor.objects.count() == 0


def _post_receipt(line):
    """A minimal posted MIR against `line`, straight into the tables."""
    plant = Plant.objects.get(code="hrs")
    vendor = line.purchase_order.vendor
    mir = Mir.objects.create(plant=plant, fy="2026-27", seq=Mir.objects.count() + 1, mir_no=f"T/{Mir.objects.count() + 1}",
                             mir_date=datetime.date(2026, 5, 20), vendor=vendor, invoice_no="X", invoice_key=f"X{line.pk}",
                             invoice_date=datetime.date(2026, 5, 20), invoice_fy="2026-27", invoice_total=Decimal("1"),
                             tax_type="IGST", created_by_email="t@ravasco.com")
    MirLine.objects.create(mir=mir, line_no=1, po_line=line, qty_received=Decimal("1"), rate=line.rate, po_rate=line.rate,
                           open_qty_before=line.qty_ordered, gst_rate=Decimal("18"), gross=Decimal("1"), taxable=Decimal("1"),
                           line_total=Decimal("1"))
