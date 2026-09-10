"""
apps/core/management/commands/backfill_achhad_po_numbers.py — one-off,
read-only tool: proposes PO numbers for blank 'Purchase Order. No.' cells in
a real Achhad MIR xlsx, using the SAME identification logic the production
matcher uses (vendor mandatory + material token-overlap >=
config.material_match_threshold - see apps/services/matching_core.py's
_identification_pool()/_vendor_matches()/_material_matches(),
matching_achhad.MATCH_CONFIG for the real per-plant threshold), against the
real Achhad PO master CSV - fetched directly from Drive by default (the
exact same file/parser sync_achhad_po_csv.py uses:
settings.ACHHAD_PO_CSV_TITLE in settings.PURCHASE_TRACKER_DB_FOLDER_ID,
parsed with apps/services/parsers/po_csv.py's parse_po_csv()), not the
database - so this never depends on how recently (or whether) that CSV has
actually been synced into RTPAchhadDomesticPurchaseOrder, and needs no
database access or credentials at all beyond the same Google service
account this app's own sync commands already use.

Added 2026-09-10, project owner request: "is it possible that we add the PO
numbers from our csv database in this MIR file and it should be 100%
correct", then "take the po data from drive via csv files" (switching this
from an earlier DB-backed version - see git history if you need that shape
again - to fetching straight from Drive, since a local dev DB or an
out-of-date synced copy is a strictly worse source than the live CSV
itself). It still can't be 100% for every row - see this command's own
stdout summary and the "Backfill Status"/"Backfill Note" columns it writes
for why (some rows are genuinely ambiguous - two-or-more real concurrent
POs to the same vendor for the same material at the same rate - and
guessing there would be worse than leaving a blank cell, since a wrong PO
number is harder to catch than an empty one). Deliberately conservative:
only proposes a PO number when identification resolves to EXACTLY ONE
distinct PO, optionally after narrowing by rate proximity (same 2%
tolerance the shipment-group aggregation fix uses, and for the same reason
- a genuinely different PO to the same vendor/material usually differs in
rate).

**Never writes to any database and never overwrites the input file** - this
tool only reads the PO CSV (from Drive, or --po-csv-file for a local copy)
and the given local MIR xlsx, then writes a NEW workbook. It has no
business writing PO numbers back into RTPAchhadMIREntry (that's
sync_achhad_mir's job, from the real MIR file) or touching the source MIR
document itself - it produces a candidate, human-reviewable correction file
only. Column H is filled ONLY for rows that were originally blank in the
source file (an existing PO number is never second-guessed or overwritten).
Two new columns are appended after AG ("Backfill Status", "Backfill Note")
so every decision - filled, ambiguous, or no candidate at all - is
auditable before anyone trusts or re-uploads this file.

Usage:
    uv run python manage.py backfill_achhad_po_numbers \
        --mir-file "C:\\path\\to\\RTP ACHHAD MIR FILE 2026-27.xlsx" \
        --output "C:\\path\\to\\output - PO Numbers Backfilled.xlsx"
    # Add --po-csv-file path.csv to use a local CSV copy instead of Drive
    # (offline testing, or a CSV that hasn't been uploaded to Drive yet).
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import openpyxl

from apps.services.matching_achhad import MATCH_CONFIG
from apps.services.matching_core import _diff_pct, _material_matches, _uom_adjust, _vendor_matches
from apps.services.parsers.achhad_mir import HeaderMismatch, parse_achhad_mir_xlsx
from apps.services.parsers.common import normalize_vendor_for_matching
from apps.services.parsers.po_csv import HeaderMismatch as PoCsvHeaderMismatch, parse_po_csv

RATE_TOLERANCE_PCT = Decimal("2")


class Command(BaseCommand):
    help = (
        "Read-only: proposes PO numbers for blank cells in a real Achhad MIR xlsx, "
        "using the production matcher's own vendor+material identification logic against "
        "the real Achhad PO master CSV fetched from Drive. Writes a NEW workbook; never "
        "touches any database or the input file."
    )

    def add_arguments(self, parser):
        parser.add_argument("--mir-file", required=True, help="Path to the real MIR xlsx to read (never modified).")
        parser.add_argument("--output", required=True, help="Path to write the new, annotated workbook to.")
        parser.add_argument("--po-csv-file", help="Parse a local PO master CSV instead of fetching it from Drive.")

    def handle(self, *args, **options):
        mir_path = options["mir_file"]
        output_path = options["output"]

        po_lines = self._load_po_lines(options.get("po_csv_file"))
        self.stdout.write(f"Loaded {len(po_lines)} real Achhad PO line items.")

        try:
            with open(mir_path, "rb") as f:
                mir_bytes = f.read()
            entries = parse_achhad_mir_xlsx(mir_bytes)
        except FileNotFoundError:
            raise CommandError(f"MIR file not found: {mir_path}")
        except HeaderMismatch as exc:
            raise CommandError(f"MIR file header mismatch (wrong sheet/layout?): {exc}")

        blank_count = sum(1 for e in entries if not e.po_number_raw)
        self.stdout.write(f"Parsed {len(entries)} MIR rows ({blank_count} with a blank PO number).")

        results = [(e, *self._find_po_number(e, po_lines)) for e in entries]

        from collections import Counter
        counts = Counter(r[1] for r in results)
        self.stdout.write(self.style.SUCCESS(f"Backfill outcome counts: {dict(counts)}"))

        wb = openpyxl.load_workbook(mir_path, data_only=True)
        ws = wb["R.M. "]
        ws["AH2"] = "Backfill Status"
        ws["AI2"] = "Backfill Note"
        for entry, status, note, po_number in results:
            row = int(entry.source_row_ref)
            if status == "filled":
                ws[f"H{row}"] = po_number
            ws[f"AH{row}"] = status
            ws[f"AI{row}"] = note
        wb.save(output_path)
        self.stdout.write(self.style.SUCCESS(f"Wrote: {output_path}"))
        self.stdout.write(
            "This is a proposal, not a correction - review the 'Backfill Status'/'Backfill Note' "
            "columns before treating any filled cell as final, and re-upload only the source MIR "
            "file (with corrections applied by hand), never this annotated copy."
        )

    def _load_po_lines(self, local_csv_path: str | None) -> list[dict]:
        """--po-csv-file, or fetched from Drive by settings.ACHHAD_PO_CSV_TITLE
        in settings.PURCHASE_TRACKER_DB_FOLDER_ID - the exact same file
        sync_achhad_po_csv.py reads, parsed with the same parse_po_csv(). No
        database involved at all; this is the live source of truth, not
        whatever happens to already be synced."""
        try:
            if local_csv_path:
                with open(local_csv_path, encoding="utf-8") as f:
                    csv_text = f.read()
            else:
                from apps.services.google_client import download_file_bytes, find_file_id_by_title
                file_id = find_file_id_by_title(settings.ACHHAD_PO_CSV_TITLE, parent_id=settings.PURCHASE_TRACKER_DB_FOLDER_ID)
                csv_text = download_file_bytes(file_id).decode("utf-8")
            orders = parse_po_csv(csv_text)
        except FileNotFoundError:
            raise CommandError(f"PO CSV file not found: {local_csv_path}")
        except PoCsvHeaderMismatch as exc:
            raise CommandError(f"PO CSV header mismatch (wrong file/layout?): {exc}")

        po_lines = []
        for po in orders:
            vendor_norm = normalize_vendor_for_matching(po.vendor_name)
            for item in po.items:
                po_lines.append({
                    "po_number": po.po_number,
                    "vendor_norm": vendor_norm,
                    "description": item.description,
                    "qty": item.qty,
                    "uom": item.uom,
                    "rate": item.net_price,
                })
        return po_lines

    def _find_po_number(self, mir_entry, po_lines: list[dict]) -> tuple[str, str, str | None]:
        """Returns (status, note, po_number_or_None).
        status in {"filled", "ambiguous", "no_candidate", "skipped_non_blank"}."""
        if mir_entry.po_number_raw:
            return "skipped_non_blank", "already had a PO number", None

        mir_vendor = normalize_vendor_for_matching(mir_entry.party_name)
        vendor_candidates = [pl for pl in po_lines if _vendor_matches(pl["vendor_norm"], mir_vendor)]
        if not vendor_candidates:
            return (
                "no_candidate",
                "no PO from this vendor at all (likely an internal transfer/job-work entry, not a "
                "purchase - or this vendor's PO simply isn't in the database yet)",
                None,
            )

        id_candidates = [
            pl for pl in vendor_candidates
            if _material_matches(MATCH_CONFIG, mir_entry.material_description, pl["description"])
        ]
        if not id_candidates:
            return (
                "no_candidate",
                f"vendor matched ({len(vendor_candidates)} candidate line items) but no material "
                f"description scored >= {MATCH_CONFIG.material_match_threshold}",
                None,
            )

        distinct_pos = sorted(set(c["po_number"] for c in id_candidates))
        if len(distinct_pos) == 1:
            return "filled", f"vendor+material identification, {len(id_candidates)} line item(s), 1 distinct PO", distinct_pos[0]

        if mir_entry.rate is not None:
            rate_narrowed = []
            for c in id_candidates:
                if c["rate"] is None:
                    continue
                _, _, rate_a, rate_b, uom_mismatch = _uom_adjust(
                    c["qty"], c["uom"], mir_entry.qty, mir_entry.uom, c["rate"], mir_entry.rate,
                )
                if uom_mismatch or rate_a is None or rate_b is None:
                    continue
                diff = _diff_pct(rate_a, rate_b)
                if diff is not None and diff <= RATE_TOLERANCE_PCT:
                    rate_narrowed.append(c)
            distinct_after_rate = sorted(set(c["po_number"] for c in rate_narrowed))
            if len(distinct_after_rate) == 1:
                return "filled", f"vendor+material matched {len(distinct_pos)} distinct POs; rate proximity narrowed to 1", distinct_after_rate[0]

        return (
            "ambiguous",
            f"vendor+material identification resolved to {len(distinct_pos)} distinct POs "
            f"({', '.join(distinct_pos)}) - rate did not disambiguate",
            None,
        )
