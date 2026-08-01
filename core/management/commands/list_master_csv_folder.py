"""
Temporary diagnostic: prints every file's exact name in
MASTER_CSV_PARENT_FOLDER. backfill_from_master_csv reported all 6 expected
titles as "Not found on Drive" - rather than guess at the mismatch (extra
space, different casing, a renamed file, or the service account not actually
having access to this specific folder), just list what's really there and
compare directly. Safe to delete once the real names are confirmed and
plant_config.py's MASTER_CSV_TITLES is corrected to match.
"""
from django.core.management.base import BaseCommand

from core import drive
from core.plant_config import MASTER_CSV_PARENT_FOLDER


class Command(BaseCommand):
    help = "Lists every file's exact name in the master CSV parent folder, for debugging title mismatches."

    def handle(self, *args, **options):
        try:
            files = drive.list_children(MASTER_CSV_PARENT_FOLDER)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Could not list folder {MASTER_CSV_PARENT_FOLDER}: {e}"))
            return

        if not files:
            self.stdout.write(self.style.WARNING(
                "Folder returned zero files. Either it's genuinely empty, the folder ID is wrong, "
                "or the service account hasn't been shared access to it."
            ))
            return

        self.stdout.write(f"Found {len(files)} file(s) in folder {MASTER_CSV_PARENT_FOLDER}:")
        for f in files:
            self.stdout.write(f"  - '{f['name']}'  (id={f['id']}, mimeType={f['mimeType']})")
