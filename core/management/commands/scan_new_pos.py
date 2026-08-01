"""
Step 1 of the no-Anthropic-API extraction pipeline (Dishant's explicit call
to avoid per-call API billing): find new PO folders on Drive, queue them in
Postgres (the real source of truth for "what's pending"), and write a
uniquely-named request batch file to Drive for a Claude scheduled task to
pick up and extract.

This command NEVER deletes or overwrites a previous request file - each run
gets its own timestamped filename, so there's no race with the scheduled
task reading one mid-write. See ingest_extraction_results.py for the other
half of this loop.
"""
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from core import drive
from core.models import DocType, ExtractionQueue, Plant
from core.plant_config import (
    DOMESTIC_PO_FOLDER_IDS,
    DYNAMIC_IMPORT_PROBE_PLANTS,
    KNOWN_IMPORT_PO_FOLDER_IDS,
)


class Command(BaseCommand):
    help = "Scans Drive for new PO folders not yet known, queues them, and writes a request batch for extraction."

    def handle(self, *args, **options):
        from core.models import PurchaseOrder

        known_folder_ids = set(PurchaseOrder.objects.values_list("drive_folder_id", flat=True))
        known_folder_ids |= set(ExtractionQueue.objects.values_list("drive_folder_id", flat=True))

        new_items = []
        new_items += self._scan(Plant.HRS, DocType.DOMESTIC, DOMESTIC_PO_FOLDER_IDS[Plant.HRS], known_folder_ids)
        new_items += self._scan(Plant.RTP_ACHHAD, DocType.DOMESTIC, DOMESTIC_PO_FOLDER_IDS[Plant.RTP_ACHHAD], known_folder_ids)
        new_items += self._scan(Plant.RTP_VAPI, DocType.DOMESTIC, DOMESTIC_PO_FOLDER_IDS[Plant.RTP_VAPI], known_folder_ids)
        new_items += self._scan(Plant.RTP_VAPI, DocType.IMPORT, KNOWN_IMPORT_PO_FOLDER_IDS[Plant.RTP_VAPI], known_folder_ids)
        for plant in DYNAMIC_IMPORT_PROBE_PLANTS:
            new_items += self._scan_dynamic_import_folder(plant, known_folder_ids)

        if not new_items:
            self.stdout.write("No new PO folders found.")
            return

        queue_rows = []
        for item in new_items:
            row, _ = ExtractionQueue.objects.get_or_create(
                drive_folder_id=item["folder_id"],
                defaults={
                    "plant": item["plant"],
                    "doc_type": item["doc_type"],
                    "po_number_hint": item["name"],
                    "status": "pending",
                },
            )
            queue_rows.append(row)

        if not settings.DRIVE_EXTRACTION_QUEUE_FOLDER:
            self.stderr.write(self.style.WARNING(
                "DRIVE_EXTRACTION_QUEUE_FOLDER is not set - queued "
                f"{len(queue_rows)} PO(s) in Postgres but could not write a "
                "request batch file for Claude to pick up. Set that env var "
                "to a Drive folder ID shared with the service account."
            ))
            return

        batch = {
            "batch_created_at": datetime.utcnow().isoformat(),
            "items": [
                {
                    "queue_id": row.id,
                    "plant": row.plant,
                    "doc_type": row.doc_type,
                    "po_number_hint": row.po_number_hint,
                    "drive_folder_id": row.drive_folder_id,
                }
                for row in queue_rows
            ],
        }
        filename = f"extraction_request_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        file_id = drive.upload_json_file(settings.DRIVE_EXTRACTION_QUEUE_FOLDER, filename, batch)

        ExtractionQueue.objects.filter(id__in=[r.id for r in queue_rows]).update(
            status="batched", batch_request_file_id=file_id
        )
        self.stdout.write(self.style.SUCCESS(f"Queued {len(queue_rows)} new PO(s) into {filename} (file id {file_id})."))

    def _scan(self, plant, doc_type, folder_id, known_folder_ids):
        subfolders = drive.list_children(folder_id, mime_type="application/vnd.google-apps.folder")
        return [
            {"plant": plant, "doc_type": doc_type, "folder_id": f["id"], "name": f["name"]}
            for f in subfolders
            if f["id"] not in known_folder_ids
        ]

    def _scan_dynamic_import_folder(self, plant, known_folder_ids):
        root_id = settings.DRIVE_PLANT_ROOTS.get(plant)
        if not root_id:
            return []
        import_folders = [
            f for f in drive.list_children(root_id, mime_type="application/vnd.google-apps.folder")
            if f["name"].strip().lower() == "import"
        ]
        if not import_folders:
            self.stdout.write(f"{plant}: no Import folder yet, skipping (expected until it's set up).")
            return []
        return self._scan(plant, DocType.IMPORT, import_folders[0]["id"], known_folder_ids)
