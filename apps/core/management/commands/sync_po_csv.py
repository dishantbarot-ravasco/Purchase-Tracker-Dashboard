"""
Syncs Master_HRS_SILVASSA_Domestic_Purchase_Data.csv from Drive into
HRSPurchaseOrder / HRSPOLineItem.

Usage:
    python manage.py sync_po_csv                # fetch from Drive
    python manage.py sync_po_csv --file path.csv  # parse a local file instead (dev/testing)
"""

import hashlib
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import HRSPOLineItem, HRSPurchaseOrder, SyncRun
from apps.services.parsers.po_csv import HeaderMismatch, parse_po_csv


def _po_hash(order) -> str:
    """Hashes every field that would need a re-save, including line items, so
    an unchanged PO is skipped instead of touching last_synced_at for no reason."""
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
    help = "Sync the HRS Purchase Order master CSV from Drive into HRSPurchaseOrder/HRSPOLineItem."

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
                    changed = self._upsert_order(parsed)
                    if changed:
                        rows_changed += 1

            self.stdout.write(self.style.SUCCESS(
                f"sync_po_csv: {rows_seen} POs seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_po_csv: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_po_csv: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.HRS,
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
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.HRS_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")

    def _upsert_order(self, parsed) -> bool:
        row_hash = _po_hash(parsed)
        existing = HRSPurchaseOrder.objects.filter(po_number=parsed.po_number).first()
        if existing and existing.synced_from_row_hash == row_hash:
            return False

        order, _ = HRSPurchaseOrder.objects.update_or_create(
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
        HRSPOLineItem.objects.bulk_create([
            HRSPOLineItem(
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
