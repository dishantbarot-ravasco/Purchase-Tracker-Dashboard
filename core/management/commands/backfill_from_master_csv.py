"""
Loads the 6 existing master CSVs (already-extracted 2026-2027 PO data,
built before this Django app existed) directly into Postgres, instead of
re-extracting the same PDFs from scratch through the Claude pipeline.

Run this once to get the dashboard populated fast. Going forward, NEW POs
not yet in these CSVs still flow through the normal pipeline
(scan_new_pos -> Claude scheduled task -> ingest_extraction_results) - this
command is specifically for the already-extracted backlog, not a
replacement for that pipeline.

Idempotent: re-running just re-upserts the same PO/item rows by PO number,
safe to run again (e.g. if you want a fresh backfill after re-running the
older CSV-based extraction process for some reason).

CSV column layout (confirmed via direct Drive inspection, 2026-08):
PO Drive Folder Name, PO Number, PO Created Date, Vendor Name, Vendor
Address, Vendor GSTIN, Vendor Email, Vendor Code, Billing Address, ShipTo,
Item Id, Material Description, HSN, QTY, UOM, Delivery Date, Payment Terms,
IncoTerms, Currency, Net Price, Net Value, Total Value, Tax Type, Total
Inclusive Value, Remarks, PO Number | Item Id

One CSV row = one PO line item. Multiple rows share the same "PO Number"
for multi-item POs - grouped here into one PurchaseOrder + several POItem
rows. Date formats vary by plant/era (ISO, M/D/YY, DD-MM-YYYY, DD.MM.YYYY)
- _parse_date tries each in turn rather than assuming one format.
"""
import csv
import io
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction

from core import drive
from core.models import POFlag, POItem, PurchaseOrder
from core.plant_config import MASTER_CSV_PARENT_FOLDER, MASTER_CSV_TITLES

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%y", "%m/%d/%Y", "%d-%m-%y"]


def _clean(raw):
    raw = (raw or "").strip()
    return "" if raw.upper() == "NULL" else raw


def _parse_number(raw):
    raw = _clean(raw).replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_date(raw):
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


class Command(BaseCommand):
    help = "Backfills PurchaseOrder/POItem from the 6 existing master CSVs (already-extracted current-year PO data)."

    def handle(self, *args, **options):
        for (plant, doc_type), title in MASTER_CSV_TITLES.items():
            self.stdout.write(f"Loading {title}...")
            file_meta = drive.find_file_by_title(MASTER_CSV_PARENT_FOLDER, title)
            if not file_meta:
                self.stderr.write(self.style.WARNING(f"  Not found on Drive - skipping ({title})"))
                continue
            try:
                raw_bytes = drive.download_file_bytes(file_meta["id"])
                text = raw_bytes.decode("utf-8-sig")  # utf-8-sig handles a possible leading BOM
                reader = csv.DictReader(io.StringIO(text))
                reader.fieldnames = [(fn or "").strip() for fn in reader.fieldnames]
                count = self._load_rows(reader, plant, doc_type)
                self.stdout.write(self.style.SUCCESS(f"  {plant} {doc_type}: {count} PO(s) upserted."))
            except Exception:
                logger.exception("Failed to backfill %s", title)
                self.stderr.write(self.style.ERROR(f"  Failed to load {title} - see server logs for detail."))

    @transaction.atomic
    def _load_rows(self, reader, plant, doc_type):
        pos = {}  # po_number -> {"row": first row seen (header fields), "items": [...], "remarks": {...}}
        for row in reader:
            po_number = _clean(row.get("PO Number"))
            if not po_number:
                continue
            bucket = pos.setdefault(po_number, {"row": row, "items": [], "remarks": set()})
            bucket["items"].append(row)
            remark = _clean(row.get("Remarks"))
            if remark:
                bucket["remarks"].add(remark)

        count = 0
        for po_number, bucket in pos.items():
            row = bucket["row"]
            total_value = _parse_number(row.get("Total Value"))
            total_incl = _parse_number(row.get("Total Inclusive Value"))
            tax_amount = (total_incl - total_value) if (total_incl is not None and total_value is not None) else None

            po, _ = PurchaseOrder.objects.update_or_create(
                po_number=po_number,
                defaults={
                    "plant": plant,
                    "doc_type": doc_type,
                    "vendor_name": _clean(row.get("Vendor Name")),
                    "vendor_gstin": _clean(row.get("Vendor GSTIN")),
                    "created_date": _parse_date(row.get("PO Created Date")),
                    "total_value": total_value,
                    "tax_type": _clean(row.get("Tax Type")),
                    "tax_amount": tax_amount,
                    "total_incl_tax": total_incl,
                    "payment_terms": _clean(row.get("Payment Terms")),
                    "incoterms": _clean(row.get("IncoTerms")),
                    "remarks": "; ".join(sorted(bucket["remarks"])),
                    "extraction_confidence": "high",
                },
            )

            po.items.all().delete()  # re-backfill of the same PO replaces its line items wholesale
            for item_row in bucket["items"]:
                POItem.objects.create(
                    purchase_order=po,
                    item_code=_clean(item_row.get("Item Id")),
                    description=_clean(item_row.get("Material Description")),
                    hsn=_clean(item_row.get("HSN")),
                    qty=_parse_number(item_row.get("QTY")),
                    uom=_clean(item_row.get("UOM")),
                    delivery_date=_parse_date(item_row.get("Delivery Date")),
                    net_price=_parse_number(item_row.get("Net Price")),
                    net_value=_parse_number(item_row.get("Net Value")),
                )

            for remark in bucket["remarks"]:
                POFlag.objects.get_or_create(purchase_order=po, flag_text=remark, source="extraction")

            count += 1
        return count
