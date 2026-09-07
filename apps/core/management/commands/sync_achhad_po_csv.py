"""
apps/core/management/commands/sync_achhad_po_csv.py — syncs
Master_RTP_Achhad_Domestic_Purchase_Data.csv from Drive into
RTPAchhadDomesticPurchaseOrder / RTPAchhadDomesticPOLineItem.

Same shape as sync_po_csv.py for HRS - see that file for the general design
(whole-order hash for change detection, delete-and-rebuild line items,
--file for offline testing). What's different for this plant: the Drive
file title comes from settings.ACHHAD_PO_CSV_TITLE (still read from the same
shared settings.PURCHASE_TRACKER_DB_FOLDER_ID as HRS/Vapi - all three
plants' PO master CSVs live in one common folder), and the target models are
RTPAchhadDomesticPurchaseOrder/RTPAchhadDomesticPOLineItem plus SyncRun.Plant.RTP_ACHHAD.

Usage:
    python manage.py sync_achhad_po_csv
    python manage.py sync_achhad_po_csv --file path.csv  # parse a local file instead (dev/testing)
"""

import hashlib
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import DataQualityFlag, RTPAchhadDomesticPOLineItem, RTPAchhadDomesticPurchaseOrder, SyncRun
from apps.services.arithmetic_checks import check_po_line_item
from apps.services.data_quality import sync_data_quality_flags
from apps.services.parsers.po_csv import HeaderMismatch, parse_po_csv


# See sync_po_csv.py's _po_hash for why this is a whole-order hash rather
# than a per-field comparison.
def _po_hash(order) -> str:
    parts = [
        order.po_drive_folder_name, order.po_number, str(order.po_created_date),
        order.vendor_name, order.vendor_address, order.vendor_gstin, order.vendor_email,
        order.vendor_code, order.billing_address, order.ship_to, order.payment_terms,
        order.incoterms, order.currency, str(order.total_value), order.tax_type,
        str(order.total_inclusive_value), order.remarks, str(order.is_old_format_template),
    ]
    for item in order.items:
        parts += [
            item.item_id, item.description, item.hsn, str(item.qty), item.uom,
            str(item.delivery_date), str(item.net_price), str(item.net_value),
        ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class Command(BaseCommand):
    """Sync the RTP-Achhad PO master CSV from Drive (or --file) into
    RTPAchhadDomesticPurchaseOrder/RTPAchhadDomesticPOLineItem. See sync_po_csv.py's Command
    docstring for the idempotency design (unchanged from HRS)."""

    help = "Sync the RTP-Achhad Purchase Order master CSV from Drive into RTPAchhadDomesticPurchaseOrder/RTPAchhadDomesticPOLineItem."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local CSV file instead of fetching from Drive.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            csv_text = self._load_csv_text(options.get("file"))
            orders = parse_po_csv(csv_text)
            rows_seen = len(orders)

            with transaction.atomic():
                for parsed in orders:
                    if self._upsert_order(parsed):
                        rows_changed += 1

            self._sync_data_quality_flags()

            self.stdout.write(self.style.SUCCESS(
                f"sync_achhad_po_csv: {rows_seen} POs seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_po_csv: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_achhad_po_csv: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.RTP_ACHHAD,
                source=SyncRun.Source.PO_CSV,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)

    def _load_csv_text(self, local_path: str | None) -> str:
        """--file, or fetched from Drive by settings.ACHHAD_PO_CSV_TITLE."""
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.ACHHAD_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")

    def _upsert_order(self, parsed) -> bool:
        """See sync_po_csv.py's _upsert_order - same hash-and-skip logic."""
        row_hash = _po_hash(parsed)
        existing = RTPAchhadDomesticPurchaseOrder.objects.filter(po_number=parsed.po_number).first()
        if existing and existing.synced_from_row_hash == row_hash:
            return False

        order, _ = RTPAchhadDomesticPurchaseOrder.objects.update_or_create(
            po_number=parsed.po_number,
            defaults=dict(
                po_drive_folder_name=parsed.po_drive_folder_name,
                po_created_date=parsed.po_created_date,
                vendor_name=parsed.vendor_name,
                vendor_address=parsed.vendor_address,
                vendor_gstin=parsed.vendor_gstin,
                vendor_email=parsed.vendor_email,
                vendor_code=parsed.vendor_code,
                billing_address=parsed.billing_address,
                ship_to=parsed.ship_to,
                payment_terms=parsed.payment_terms,
                incoterms=parsed.incoterms,
                currency=parsed.currency,
                total_value=parsed.total_value,
                tax_type=parsed.tax_type,
                total_inclusive_value=parsed.total_inclusive_value,
                remarks=parsed.remarks,
                is_old_format_template=parsed.is_old_format_template,
                synced_from_row_hash=row_hash,
                last_synced_at=timezone.now(),
            ),
        )
        order.items.all().delete()
        RTPAchhadDomesticPOLineItem.objects.bulk_create([
            RTPAchhadDomesticPOLineItem(
                purchase_order=order,
                item_id=item.item_id,
                description=item.description,
                hsn=item.hsn,
                qty=item.qty,
                uom=item.uom,
                delivery_date=item.delivery_date,
                net_price=item.net_price,
                net_value=item.net_value,
            )
            for item in parsed.items
        ])
        return True

    def _sync_data_quality_flags(self) -> None:
        """See sync_po_csv.py's own _sync_data_quality_flags - Match
        Accuracy Programme fix 3.G, same check, this plant's model."""
        results = {
            item.id: check_po_line_item(item.qty, item.net_price, item.net_value)
            for item in RTPAchhadDomesticPOLineItem.objects.all()
        }
        sync_data_quality_flags(SyncRun.Plant.RTP_ACHHAD, DataQualityFlag.SourceType.PO_LINE_ITEM, results)
