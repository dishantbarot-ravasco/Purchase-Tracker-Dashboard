"""
apps/core/management/commands/sync_advance_license.py — syncs the
Advance License Data Google Sheet into AdvanceLicense / AdvanceLicenseMaterial.

Company-wide, not per-plant (see SyncRun.Plant.COMPANY's own comment on
RodtepScrollEntry) - one fixed file, unlike sync_rodtep's whole-folder
listing (RoDTEP has one file per script; this app has one Advance License
ledger file, maintained by hand). settings.ADVANCE_LICENSE_FILE_ID names it
directly by Drive file id (the project owner's own share link, confirmed
2026-09-09) rather than a folder+title to search for - this command
downloads it via google_client.download_spreadsheet_bytes_by_id(), which
looks up the file's real mimeType and exports (native Google Sheet) or
downloads (uploaded .xlsx) accordingly.

Change detection: a whole-license SHA-256 hash (_license_hash), same
reasoning as sync_po_csv.py's _po_hash - a license and all its material
rows are always rewritten together as one unit (materials are deleted and
bulk_created fresh on any change) since material rows have no independent
identity worth diffing (same as PO line items - see po_csv.py's own module
docstring). license_number is the natural key.

No deactivation step - like RoDTEP, this is a ledger the project owner
maintains by hand, not a current-state snapshot; a license that stops
appearing in a re-download isn't treated as "no longer real" here.

Usage:
    python manage.py sync_advance_license                # fetch from Drive
    python manage.py sync_advance_license --file path.xlsx  # parse a local file instead (dev/testing)
"""

import hashlib
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import AdvanceLicense, AdvanceLicenseMaterial, SyncRun
from apps.services.parsers.advance_license import HeaderMismatch, parse_advance_license_xlsx


def _license_hash(lic) -> str:
    """Hashes every field that would need a re-save, including material
    rows, so an unchanged license is skipped instead of touching
    last_synced_at for no reason. Same shape as sync_po_csv.py's _po_hash."""
    parts = [
        lic.license_number, str(lic.issue_date), lic.iec, str(lic.cif_value_authorized),
        str(lic.fob_value_export_target), lic.export_product_description,
        str(lic.export_validity_date), str(lic.import_validity_date), lic.status,
    ]
    for m in lic.materials:
        parts += [
            m.material_description, m.itchs_code, str(m.qty_authorized), str(m.cif_value_authorized),
            str(m.duty_saved_pct), m.boe_number, str(m.boe_date), m.import_po_number,
            str(m.qty_imported), str(m.value_imported),
        ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class Command(BaseCommand):
    """Sync the Advance License workbook from Drive (or --file) into
    AdvanceLicense/AdvanceLicenseMaterial, and record the outcome as a
    SyncRun row.

    Idempotent: a license whose _license_hash matches the stored
    synced_from_row_hash is left untouched; only licenses that changed (or
    are new) get their materials deleted and rebuilt. Safe to re-run any time.
    """

    help = "Sync the Advance License workbook from Drive into AdvanceLicense/AdvanceLicenseMaterial."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Parse a local xlsx file instead of fetching from Drive.")

    def handle(self, *args, **options):
        started_at = timezone.now()
        t0 = time.monotonic()
        rows_seen = 0
        rows_changed = 0
        status = SyncRun.Status.SUCCESS
        error_detail = ""

        try:
            file_bytes = self._load_file_bytes(options.get("file"))
            licenses = parse_advance_license_xlsx(file_bytes)
            rows_seen = len(licenses)

            with transaction.atomic():
                for parsed in licenses:
                    if self._upsert_license(parsed):
                        rows_changed += 1

            self.stdout.write(self.style.SUCCESS(
                f"sync_advance_license: {rows_seen} licenses seen, {rows_changed} created/updated "
                f"({time.monotonic() - t0:.1f}s)"
            ))
        except HeaderMismatch as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_advance_license: header mismatch - {exc}"))
        except Exception as exc:
            status = SyncRun.Status.FAILED
            error_detail = str(exc)
            self.stderr.write(self.style.ERROR(f"sync_advance_license: failed - {exc}"))
        finally:
            SyncRun.objects.create(
                plant=SyncRun.Plant.COMPANY,
                source=SyncRun.Source.ADVANCE_LICENSE,
                status=status,
                started_at=started_at,
                finished_at=timezone.now(),
                rows_seen=rows_seen,
                rows_changed=rows_changed,
                error_detail=error_detail,
            )

        if status == SyncRun.Status.FAILED:
            raise SystemExit(1)

    def _load_file_bytes(self, local_path: str | None) -> bytes:
        if local_path:
            with open(local_path, "rb") as f:
                return f.read()
        from apps.services.google_client import download_spreadsheet_bytes_by_id
        return download_spreadsheet_bytes_by_id(settings.ADVANCE_LICENSE_FILE_ID)

    def _upsert_license(self, parsed) -> bool:
        """Upsert one parsed license by license_number; returns False (no-op)
        when the whole-license hash matches what's already stored."""
        row_hash = _license_hash(parsed)
        existing = AdvanceLicense.objects.filter(license_number=parsed.license_number).first()
        if existing and existing.synced_from_row_hash == row_hash:
            return False

        lic, _ = AdvanceLicense.objects.update_or_create(
            license_number=parsed.license_number,
            defaults=dict(
                issue_date=parsed.issue_date,
                iec=parsed.iec,
                cif_value_authorized=parsed.cif_value_authorized,
                fob_value_export_target=parsed.fob_value_export_target,
                export_product_description=parsed.export_product_description,
                export_validity_date=parsed.export_validity_date,
                import_validity_date=parsed.import_validity_date,
                status=parsed.status,
                synced_from_row_hash=row_hash,
                last_synced_at=timezone.now(),
            ),
        )
        # Material rows have no independent identity worth diffing - same
        # delete-and-rebuild approach as HRSDomesticPOLineItem (po_csv.py).
        lic.materials.all().delete()
        AdvanceLicenseMaterial.objects.bulk_create([
            AdvanceLicenseMaterial(
                license=lic,
                material_description=m.material_description,
                itchs_code=m.itchs_code,
                qty_authorized=m.qty_authorized,
                cif_value_authorized=m.cif_value_authorized,
                duty_saved_pct=m.duty_saved_pct,
                boe_number=m.boe_number,
                boe_date=m.boe_date,
                import_po_number=m.import_po_number,
                qty_imported=m.qty_imported,
                value_imported=m.value_imported,
            )
            for m in parsed.materials
        ])
        return True
