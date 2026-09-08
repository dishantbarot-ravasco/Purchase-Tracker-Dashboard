"""
Integration tests for the Import PO CSV sync pipeline
(sync_hrs_imports_po_csv.py + apps/services/import_sync.py's shared
sync_orders(), reused verbatim by sync_achhad_imports_po_csv.py/
sync_vapi_imports_po_csv.py) - previously zero coverage. Only HRS's command
is exercised directly since all three are thin wrappers around the same
sync_orders() helper (see that module's own docstring) - the plant-specific
wiring (Drive title, SyncRun.Plant tag, target models) is the only thing
that differs, and is already covered indirectly by every other plant's
sync_*_po_csv tests following this same call_command pattern.
"""

import csv
import io

import pytest
from django.core.management import call_command

from apps.core.models import HRSImportPOLineItem, HRSImportPurchaseOrder, SyncRun
from apps.services.parsers.import_po_csv import EXPECTED_HEADER

_ROW_TEMPLATE = {
    "PO Drive Folder Name": "PO_IMP001", "PO Number": "IMP001",
    "PO Created Date": "01-05-2026", "Vendor Name": "Global Polymers Inc",
    "Vendor Address": "123 Trade Ave, Singapore", "Vendor GSTIN": "",
    "Vendor Email": "sales@globalpoly.example", "Vendor Code": "GV001",
    "Billing Address": "Ravasco, Silvassa", "ShipTo": "Ravasco, Silvassa",
    "Item Id": "1", "Material Description": "PTFE Coated Fabric", "HSN": "5903",
    "QTY (As Per PO)": "120", "QTY (As Per BOE)": "100", "UOM": "KG",
    "Delivery Date": "15-05-2026", "Payment Terms": "60 Days", "IncoTerms": "CIF",
    "Currency (As Per PO)": "USD", "Net Price": "1.95", "Net Value": "234.00",
    "Total Value (As per PO)": "234.00", "Tax Type": "IGST",
    "Currency (After Taxes)": "INR", "Exchange Rate": "93.80",
    "Total Inclusive Value (Final Bill Paid to get shipment from Port)": "18291.00",
    "REMARKS": "", "BOE Number": "BOE001", "Bill Of Lading Number": "BL001",
    "Laden on Board Date": "10-05-2026", "Country of Origin": "Singapore",
    "License Type": "", "License Number": "",
    "PO Number | Item Id": "IMP001|1",
}


def _write_csv(rows: list[dict], header=EXPECTED_HEADER) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


@pytest.mark.django_db
class TestSyncHrsImportsPoCsv:
    def test_second_run_with_no_changes_updates_nothing(self, tmp_path):
        fixture_path = tmp_path / "imports.csv"
        fixture_path.write_text(_write_csv([_ROW_TEMPLATE]), encoding="utf-8")

        call_command("sync_hrs_imports_po_csv", file=str(fixture_path))
        first = SyncRun.objects.filter(source=SyncRun.Source.IMPORT_PO_CSV).latest("started_at")
        assert first.rows_changed == 1
        order = HRSImportPurchaseOrder.objects.get(po_number="IMP001")
        assert order.currency == "USD"
        item = order.items.get()
        assert item.qty_as_per_po == 120 and item.qty_as_per_boe == 100

        call_command("sync_hrs_imports_po_csv", file=str(fixture_path))
        second = SyncRun.objects.filter(source=SyncRun.Source.IMPORT_PO_CSV).latest("started_at")
        assert second.rows_changed == 0

    def test_a_real_field_change_is_picked_up_on_resync(self, tmp_path):
        fixture_path = tmp_path / "imports.csv"
        fixture_path.write_text(_write_csv([_ROW_TEMPLATE]), encoding="utf-8")
        call_command("sync_hrs_imports_po_csv", file=str(fixture_path))

        changed = dict(_ROW_TEMPLATE, **{"QTY (As Per BOE)": "95"})
        fixture_path.write_text(_write_csv([changed]), encoding="utf-8")
        call_command("sync_hrs_imports_po_csv", file=str(fixture_path))

        item = HRSImportPOLineItem.objects.get(purchase_order__po_number="IMP001")
        assert item.qty_as_per_boe == 95

    def test_trailing_blank_headers_are_tolerated_a_real_export_artifact(self, tmp_path):
        """Confirmed 2026-09-07 on a real RTP-Vapi sync failure: a spreadsheet
        export left 2 headerless trailing columns with no data intent behind
        them - the parser strips those before the strict header-equality
        check (see import_po_csv.py's own comment), so this must NOT raise."""
        fixture_path = tmp_path / "imports.csv"
        header_with_trailing_blanks = EXPECTED_HEADER + ["", ""]
        fixture_path.write_text(_write_csv([_ROW_TEMPLATE], header=header_with_trailing_blanks), encoding="utf-8")

        call_command("sync_hrs_imports_po_csv", file=str(fixture_path))

        run = SyncRun.objects.filter(source=SyncRun.Source.IMPORT_PO_CSV).latest("started_at")
        assert run.status == SyncRun.Status.SUCCESS
        assert HRSImportPurchaseOrder.objects.filter(po_number="IMP001").exists()

    def test_header_mismatch_records_a_failed_syncrun_and_raises(self, tmp_path):
        fixture_path = tmp_path / "imports.csv"
        fixture_path.write_text("Wrong,Header,Row\n1,2,3\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            call_command("sync_hrs_imports_po_csv", file=str(fixture_path))

        run = SyncRun.objects.filter(source=SyncRun.Source.IMPORT_PO_CSV).latest("started_at")
        assert run.status == SyncRun.Status.FAILED
        assert not HRSImportPurchaseOrder.objects.exists()
