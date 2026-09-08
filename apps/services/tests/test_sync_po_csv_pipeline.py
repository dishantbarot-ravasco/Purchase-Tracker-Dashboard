"""
Integration tests for apps/core/management/commands/sync_po_csv.py -
previously zero coverage (CLAUDE.md's "Known gaps": "the actual DB-touching
sync_*/match_* management commands... has no automated test coverage").
Real Postgres (@pytest.mark.django_db), no mocking - same convention as
test_sync_stock_row_insert.py, which this file's fixture-building style
mirrors (call_command against a local --file, not a real Drive call).

Exercises the command as a whole (parse -> upsert -> SyncRun row), not
parse_po_csv() in isolation - that parser's own header/row-shape edge cases
belong in a parser-level test if ever added; this file is about the
sync command's own behavior: idempotency, change-detection, and the
SyncRun bookkeeping every command shares.
"""

import csv
import io

import pytest
from django.core.management import call_command

from apps.core.models import HRSDomesticPOLineItem, HRSDomesticPurchaseOrder, SyncRun
from apps.services.parsers.po_csv import EXPECTED_HEADER

_ROW_TEMPLATE = {
    "PO Drive Folder Name": "PO_3000001104", "PO Number": "3000001104",
    "PO Created Date": "01-05-2026", "Vendor Name": "Rubamin Private Limited",
    "Vendor Address": "123 Industrial Estate", "Vendor GSTIN": "24AAAAA0000A1Z5",
    "Vendor Email": "sales@rubamin.example", "Vendor Code": "V001",
    "Billing Address": "Ravasco, Silvassa", "ShipTo": "Ravasco, Silvassa",
    "Item Id": "1", "Material Description": "SBR 1502", "HSN ": "4002",
    "QTY": "1000", "UOM": "KG", "Delivery Date ": "15-05-2026",
    "Payment Terms ": "60 Days", "IncoTerms": "DDP", "Currency ": "INR",
    "Net Price": "100.00", "Net Value": "100000.00", "Total Value": "100000.00",
    "Tax Type": "IGST", "Total Inclusive Value": "118000.00", "Remarks": "",
    "PO Number | Item Id": "3000001104|1",
}


def _write_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPECTED_HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


@pytest.mark.django_db
class TestSyncPoCsvIdempotency:
    def test_second_run_with_no_changes_updates_nothing(self, tmp_path):
        fixture_path = tmp_path / "po.csv"
        fixture_path.write_text(_write_csv([_ROW_TEMPLATE]), encoding="utf-8")

        call_command("sync_po_csv", file=str(fixture_path))
        first_run = SyncRun.objects.filter(source=SyncRun.Source.PO_CSV).latest("started_at")
        assert first_run.status == SyncRun.Status.SUCCESS
        assert first_run.rows_changed == 1

        order = HRSDomesticPurchaseOrder.objects.get(po_number="3000001104")
        original_hash = order.synced_from_row_hash

        call_command("sync_po_csv", file=str(fixture_path))
        second_run = SyncRun.objects.filter(source=SyncRun.Source.PO_CSV).latest("started_at")
        assert second_run.rows_seen == 1
        assert second_run.rows_changed == 0, "unchanged PO was re-written on a no-op re-sync"

        order.refresh_from_db()
        assert order.synced_from_row_hash == original_hash

    def test_a_real_field_change_is_picked_up_on_resync(self, tmp_path):
        fixture_path = tmp_path / "po.csv"
        fixture_path.write_text(_write_csv([_ROW_TEMPLATE]), encoding="utf-8")
        call_command("sync_po_csv", file=str(fixture_path))

        changed_row = dict(_ROW_TEMPLATE, **{"QTY": "1500"})
        fixture_path.write_text(_write_csv([changed_row]), encoding="utf-8")
        call_command("sync_po_csv", file=str(fixture_path))

        run = SyncRun.objects.filter(source=SyncRun.Source.PO_CSV).latest("started_at")
        assert run.rows_changed == 1, "a real qty change on re-sync was treated as a no-op"

        item = HRSDomesticPOLineItem.objects.get(purchase_order__po_number="3000001104")
        assert item.qty == 1500

    def test_header_mismatch_records_a_failed_syncrun_and_raises(self, tmp_path):
        fixture_path = tmp_path / "po.csv"
        fixture_path.write_text("Wrong,Header,Row\n1,2,3\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            call_command("sync_po_csv", file=str(fixture_path))

        run = SyncRun.objects.filter(source=SyncRun.Source.PO_CSV).latest("started_at")
        assert run.status == SyncRun.Status.FAILED
        assert "header" in run.error_detail.lower() or "does not match" in run.error_detail.lower()
        assert not HRSDomesticPurchaseOrder.objects.exists()
