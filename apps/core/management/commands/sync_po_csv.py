"""
apps/core/management/commands/sync_po_csv.py — syncs
Master_HRS_SILVASSA_Domestic_Purchase_Data.csv from Drive into
HRSDomesticPurchaseOrder / HRSDomesticPOLineItem.

The PO master CSV format is identical across HRS/Achhad/Vapi (confirmed
byte-for-byte identical header against real files), so apps/services/
parsers/po_csv.py's parse_po_csv() is reused as-is for all three plants -
only the target model classes, the Drive file title
(settings.HRS_PO_CSV_TITLE), and the SyncRun.Plant tag differ per plant's
sync_*_po_csv command. See sync_achhad_po_csv.py / sync_vapi_po_csv.py for
those thin variants.

Change detection here is a whole-order SHA-256 hash (_po_hash), not
sync_utils.unchanged()'s per-field Decimal-quantized comparison used by
sync_mir.py/sync_stock.py - a PO and all its line items are always rewritten
together as one unit (line items are deleted and bulk_created fresh on any
change), so a single hash over every field that matters is enough to decide
whether to skip the write; there's no per-field "what changed" tracking to
lose. po_number is used as the natural key (POs don't shift rows the way
MIR/Stock sheet rows do), so no soft-deactivation logic is needed either.

Drive folder: PURCHASE_TRACKER_DB_FOLDER_ID - all three plants' PO master
CSVs live in one shared folder, distinct from each plant's own MIR/Stock
folder (see CLAUDE.md's "Two Drive folders per plant, not one").

--file lets this run against a local CSV copy instead of hitting Drive -
useful for offline dev/testing without live service-account credentials.

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

from apps.core.models import DataQualityFlag, HRSDomesticPOLineItem, HRSDomesticPurchaseOrder, SyncRun
from apps.services.arithmetic_checks import check_po_line_item
from apps.services.data_quality import sync_data_quality_flags
from apps.services.sync_utils import orphaned_orders
from apps.services.parsers.po_csv import HeaderMismatch, parse_po_csv


# ── Internal helpers ──────────────────────────────────────────────────────

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


# ── Command ────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    """Sync the HRS PO master CSV from Drive (or --file) into HRSDomesticPurchaseOrder/
    HRSDomesticPOLineItem, and record the outcome as a SyncRun row.

    Idempotent: an order whose _po_hash matches the stored
    synced_from_row_hash is left untouched; only orders that changed (or are
    new) get their line items deleted and rebuilt. Safe to re-run any time.
    """

    help = "Sync the HRS Purchase Order master CSV from Drive into HRSDomesticPurchaseOrder/HRSDomesticPOLineItem."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local CSV file instead of fetching from Drive.")

    def handle(self, *args, **options):
        """Parse the CSV, upsert every order in one transaction, then always
        record a SyncRun (SUCCESS or FAILED) regardless of outcome."""
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

            self._sync_data_quality_flags()
            self._report_orphans(orders)

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
        """Return the CSV's raw text: from --file if given, otherwise fetched
        from the shared PO-master Drive folder by file title."""
        if local_path:
            with open(local_path, encoding="utf-8") as f:
                return f.read()
        from apps.services.google_client import download_file_bytes, find_file_id_by_title
        file_id = find_file_id_by_title(settings.HRS_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
        return download_file_bytes(file_id).decode("utf-8")

    def _upsert_order(self, parsed) -> bool:
        """Upsert one parsed PO by po_number; returns False (no-op) when the
        whole-order hash matches what's already stored."""
        row_hash = _po_hash(parsed)
        existing = HRSDomesticPurchaseOrder.objects.filter(po_number=parsed.po_number).first()
        if existing and existing.synced_from_row_hash == row_hash:
            return False

        order, _ = HRSDomesticPurchaseOrder.objects.update_or_create(
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
        # Line items have no independent identity worth diffing - simplest
        # correct approach is delete-and-rebuild rather than per-item upsert.
        order.items.all().delete()
        HRSDomesticPOLineItem.objects.bulk_create([
            HRSDomesticPOLineItem(
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

    def _report_orphans(self, parsed_orders) -> None:
        """Warn about stored orders the master CSV no longer lists - almost
        always an upstream rename this sync cannot see (see
        orphaned_orders()). Reported, never deleted: each one still carries
        line items that compete for MIR rows, so they are worth acting on,
        but deleting a purchase order is irreversible and a withdrawn order
        is indistinguishable from a renamed one at this layer."""
        orphans = orphaned_orders(HRSDomesticPurchaseOrder, parsed_orders)
        if not orphans:
            return
        shown = ", ".join(sorted(orphans)[:5])
        more = f" (+{len(orphans) - 5} more)" if len(orphans) > 5 else ""
        self.stdout.write(self.style.WARNING(
            f"sync_po_csv: {len(orphans)} stored PO(s) are no longer in the master CSV "
            f"and are still matching against MIR: {shown}{more}"
        ))

    def _sync_data_quality_flags(self) -> None:
        """Match Accuracy Programme fix 3.G: qty x rate ~= net_value over
        every currently-active HRS PO line item (not just the ones this sync
        happened to touch - cheap, and keeps a flag correctly cleared if a
        row was fixed in the source sheet on some other path)."""
        results = {
            item.id: check_po_line_item(item.qty, item.net_price, item.net_value)
            for item in HRSDomesticPOLineItem.objects.all()
        }
        sync_data_quality_flags(SyncRun.Plant.HRS, DataQualityFlag.SourceType.PO_LINE_ITEM, results)
