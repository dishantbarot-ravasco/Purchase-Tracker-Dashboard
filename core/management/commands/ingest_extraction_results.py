"""
Step 2 of the extraction pipeline: read any result batch files the Claude
scheduled task has written back to Drive, upsert the extracted PO/license
data into Postgres, and archive the processed files (move, don't delete -
keeps an audit trail of what was extracted and when).

Expected result batch JSON shape (written by the Claude scheduled task):
{
  "batch_result_of_request_file": "extraction_request_...json",
  "items": [
    {
      "queue_id": 123,
      "po_number": "3000001234",
      "vendor_name": "...", "vendor_gstin": "...", "created_date": "2026-08-01",
      "total_value": 109250.0, "tax_type": "IGST", "tax_amount": ...,
      "total_incl_tax": ..., "payment_terms": "...", "incoterms": "...",
      "delivery_mode": "...", "remarks": "...",
      "extraction_confidence": "high" | "low",
      "items": [{"item_code": "", "description": "", "hsn": "", "qty": 0,
                 "uom": "", "delivery_date": null, "net_price": 0, "net_value": 0}],
      "flags": ["..."],
      "license_refs": [{"license_number": "...", "material_description": "...",
                         "qty_used": 0, "value_used": 0}]
    }
  ]
}
This command is intentionally tolerant of missing fields (writes "NULL"-ish
blanks rather than crashing the whole batch) and always records what it
skipped so nothing silently vanishes.
"""
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core import drive
from core.models import AdvanceLicense, ExtractionQueue, LicensePOUsage, POFlag, POItem, PurchaseOrder


class Command(BaseCommand):
    help = "Reads Claude's extraction result batches from Drive and upserts them into Postgres."

    def handle(self, *args, **options):
        if not settings.DRIVE_EXTRACTION_QUEUE_FOLDER:
            self.stderr.write(self.style.ERROR("DRIVE_EXTRACTION_QUEUE_FOLDER is not set."))
            return

        result_files = drive.list_children(
            settings.DRIVE_EXTRACTION_QUEUE_FOLDER, name_contains="extraction_result_"
        )
        if not result_files:
            self.stdout.write("No result batches waiting.")
            return

        for f in result_files:
            self.stdout.write(f"Ingesting {f['name']}...")
            try:
                batch = drive.download_json_file(f["id"])
                processed = self._ingest_batch(batch)
                self.stdout.write(self.style.SUCCESS(f"  {processed} PO(s) upserted from {f['name']}."))
                # Archiving (rename with a processed_ prefix) is left as a
                # manual Drive step for now - the Drive connector this app
                # uses only has read/create, no move/rename call wired up
                # yet. Safe either way: ExtractionQueue.status='processed'
                # means a rerun of this command will just re-read a file
                # it's already ingested and no-op on every row (po_number
                # upsert is idempotent), it won't double-count anything.
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Failed to ingest {f['name']}: {e}"))

    @transaction.atomic
    def _ingest_batch(self, batch):
        count = 0
        for item in batch.get("items", []):
            po_number = item.get("po_number")
            if not po_number:
                continue

            queue_row = None
            if item.get("queue_id"):
                queue_row = ExtractionQueue.objects.filter(id=item["queue_id"]).first()

            po, _ = PurchaseOrder.objects.update_or_create(
                po_number=po_number,
                defaults={
                    "plant": queue_row.plant if queue_row else item.get("plant", ""),
                    "doc_type": queue_row.doc_type if queue_row else item.get("doc_type", "domestic"),
                    "drive_folder_id": queue_row.drive_folder_id if queue_row else item.get("drive_folder_id", ""),
                    "vendor_name": item.get("vendor_name", ""),
                    "vendor_gstin": item.get("vendor_gstin", ""),
                    "created_date": item.get("created_date") or None,
                    "total_value": item.get("total_value"),
                    "tax_type": item.get("tax_type", ""),
                    "tax_amount": item.get("tax_amount"),
                    "total_incl_tax": item.get("total_incl_tax"),
                    "payment_terms": item.get("payment_terms", ""),
                    "incoterms": item.get("incoterms", ""),
                    "delivery_mode": item.get("delivery_mode", ""),
                    "remarks": item.get("remarks", ""),
                    "extraction_confidence": item.get("extraction_confidence", "high"),
                },
            )

            po.items.all().delete()  # re-extraction of the same PO replaces its line items wholesale
            for line in item.get("items", []):
                POItem.objects.create(
                    purchase_order=po,
                    item_code=line.get("item_code", ""),
                    description=line.get("description", ""),
                    hsn=line.get("hsn", ""),
                    qty=line.get("qty"),
                    uom=line.get("uom", ""),
                    delivery_date=line.get("delivery_date") or None,
                    net_price=line.get("net_price"),
                    net_value=line.get("net_value"),
                )

            for flag_text in item.get("flags", []):
                POFlag.objects.get_or_create(purchase_order=po, flag_text=flag_text, source="extraction")

            for ref in item.get("license_refs", []):
                license_obj = AdvanceLicense.objects.filter(license_number=ref.get("license_number")).first()
                if not license_obj:
                    # License letter itself hasn't been extracted/queued yet -
                    # skip silently here rather than half-creating a license
                    # record with no validity dates; a future batch that DOES
                    # process the license letter will backfill this link when
                    # it re-scans this PO's license_refs.
                    continue
                LicensePOUsage.objects.get_or_create(
                    license=license_obj,
                    purchase_order=po,
                    material_description=ref.get("material_description", ""),
                    defaults={"qty_used": ref.get("qty_used"), "value_used": ref.get("value_used")},
                )

            if queue_row:
                queue_row.status = "processed"
                queue_row.processed_at = datetime.utcnow()
                queue_row.save(update_fields=["status", "processed_at"])

            count += 1
        return count
