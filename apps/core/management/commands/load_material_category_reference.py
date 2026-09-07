"""
apps/core/management/commands/load_material_category_reference.py — loads/
updates MaterialCategoryReference from a CSV the plant manager provides
(columns: SAP Item Code, Description, HSN Code, Category, Subcategory (SAP
Product Group), UOM).

Unlike every other sync_* command in this app, this is NOT pulled from Drive
on a schedule - this data changes rarely (new materials/categories only, not
daily transaction volume), so it's a manually-triggered load run whenever the
plant manager sends an updated list, per apps/core/models.py's
MaterialCategoryReference docstring. Idempotent (sync_utils.unchanged()) -
safe to re-run the same file, and re-running an updated file only touches
rows that actually changed.

Usage:
    python manage.py load_material_category_reference --file path.csv
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import MaterialCategoryReference
from apps.services.parsers.material_category_reference import HeaderMismatch, parse_material_category_reference_csv
from apps.services.sync_utils import unchanged

_FIELDS = ["description", "category", "subcategory", "subcategory_code", "hsn_code", "uom", "sap_item_code"]


class Command(BaseCommand):
    help = "Load/update MaterialCategoryReference from a plant-manager-provided CSV file."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the reference CSV file.")

    def handle(self, *args, **options):
        t0 = time.monotonic()
        try:
            with open(options["file"], "r", encoding="utf-8-sig") as f:
                csv_text = f.read()
            rows = parse_material_category_reference_csv(csv_text)
        except FileNotFoundError:
            raise CommandError(f"File not found: {options['file']}")
        except HeaderMismatch as exc:
            raise CommandError(f"Header mismatch: {exc}")

        rows_changed = 0
        duplicates = []
        with transaction.atomic():
            seen_keys = set()
            for parsed in rows:
                if parsed.normalized_description in seen_keys:
                    duplicates.append(parsed.description)
                    continue  # first occurrence in the file wins - see summary line below
                seen_keys.add(parsed.normalized_description)

                existing = MaterialCategoryReference.objects.filter(
                    normalized_description=parsed.normalized_description
                ).first()
                if existing and unchanged(MaterialCategoryReference, existing, parsed, _FIELDS):
                    continue

                MaterialCategoryReference.objects.update_or_create(
                    normalized_description=parsed.normalized_description,
                    defaults={f: getattr(parsed, f) for f in _FIELDS}
                    | {"source_row_ref": parsed.source_row_ref, "last_synced_at": timezone.now()},
                )
                rows_changed += 1

        summary = (
            f"load_material_category_reference: {len(rows)} rows seen, {rows_changed} created/updated "
            f"({time.monotonic() - t0:.1f}s)"
        )
        if duplicates:
            summary += f", {len(duplicates)} duplicate description(s) skipped (first occurrence kept)"
        self.stdout.write(self.style.SUCCESS(summary))
        if duplicates:
            self.stdout.write(self.style.WARNING(f"Duplicates: {', '.join(duplicates[:20])}"))
