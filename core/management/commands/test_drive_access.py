"""
One-off sanity check: confirms the service account key can actually read
each plant's Drive folder before you wire up the full pipeline. Run this
locally (or on Render, in a one-off shell) - not part of any scheduled job.

Usage: python manage.py test_drive_access
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core import drive


class Command(BaseCommand):
    help = "Verifies the service account can list files in each plant's Drive folder."

    def handle(self, *args, **options):
        for plant, folder_id in settings.DRIVE_PLANT_ROOTS.items():
            if not folder_id:
                self.stderr.write(self.style.WARNING(f"{plant}: no folder ID configured, skipping."))
                continue
            try:
                children = drive.list_children(folder_id)
                names = ", ".join(f["name"] for f in children[:5])
                self.stdout.write(self.style.SUCCESS(f"{plant}: OK - sees {len(children)} item(s): {names}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"{plant}: FAILED - {e}"))

        if settings.DRIVE_EXTRACTION_QUEUE_FOLDER:
            try:
                children = drive.list_children(settings.DRIVE_EXTRACTION_QUEUE_FOLDER)
                self.stdout.write(self.style.SUCCESS(f"Extraction Queue folder: OK - {len(children)} item(s)."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Extraction Queue folder: FAILED - {e}"))
