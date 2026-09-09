"""
apps/core/models.py — Django ORM models for every plant's PO/MIR/Stock data,
their reconciliation (*Match) tables, and the auth/audit/correction tables.

Deliberately NOT one shared schema with a `plant` discriminator column.
HRS, RTP-Vapi, and RTP-Achhad's MIR/Stock files have genuinely different
column layouts (Vapi/Achhad's Stock file covers several sub-plants in one
file with extra columns - Billing on Plant, Material Location, HSN Code -
that HRS's file doesn't have at all). Forcing them into one shared table
would mean a pile of always-null columns for whichever plants don't have
that field, and would tempt matching logic that silently assumes a field
exists uniformly across plants when it doesn't. Each plant gets its own
model classes instead - see CLAUDE.md's "Per-plant models, not a shared
schema" section for the full, confirmed-against-real-files reasoning.

Three independent Drive-sourced record types per plant (PurchaseOrder/
POLineItem, MIREntry, StockLot), each synced by its own management command,
plus the reconciliation layer that links them (*POMirMatch/*MirStockMatch)
and a daily snapshot table that replaces the "copy the whole xlsx to Drive
with today's date in the filename" habit with real queryable history.

Because HRS/Achhad/Vapi's Domestic PO, MIR, Stock, and match models are
near-identical triplets by design (same real-world shape, genuinely
different only where a plant's source spreadsheet is genuinely different -
see each section's own header comment), only the FIRST plant's version of
each model family (always HRS) carries a full field-by-field docstring/
comment set below. The other two plants' equivalent classes carry a short
"same shape as HRS<Model> - see that class" docstring and comment only the
fields that are genuinely different for that plant - re-deriving the same
explanation three times would drift out of sync with itself over time.

All three plants (HRS, RTP-Achhad, RTP-Vapi) are now built.
"""

from django.db import models


class SyncRun(models.Model):
    """One record per sync-job execution, so the dashboard can show
    "last synced" per data source instead of silently trusting stale data.
    Shared across plants - it's just a log, not a plant-shaped schema."""

    class Plant(models.TextChoices):
        HRS = "HRS", "Hindustan Rubbers, Silvassa"
        RTP_VAPI = "RTP-VAPI", "Ravasco Transmission and Packing, Vapi"
        RTP_ACHHAD = "RTP-ACHHAD", "Ravasco Transmission and Packing, Achhad"
        # RoDTEP scrips are a company-wide resource (one shared IEC, one
        # shared Drive folder - "Purchase Orders HO/RODTEP SCRIPT LICENSE" -
        # not split per plant the way MIR/Stock/PO data is), so its own
        # SyncRun rows need a non-plant value rather than forcing a pick
        # among HRS/Vapi/Achhad that wouldn't mean anything real.
        COMPANY = "COMPANY", "Company-wide (not plant-specific)"

    class Source(models.TextChoices):
        PO_CSV = "po_csv", "Purchase Order master CSV"
        MIR = "mir", "MIR (Material Inward Register)"
        STOCK = "stock", "Raw Material Stock"
        IMPORT_PO_CSV = "import_po_csv", "Import Purchase Order master CSV"
        # Added 2026-09-04: match_hrs/match_achhad/match_vapi previously had
        # no SyncRun tracking at all - a real failure inside run_full_match()
        # (apps/services/matching*.py) was only ever logged to logs/app.log,
        # completely invisible anywhere in the app itself. The sync badges
        # (frontend/js/main.js's loadSyncStatus(), admin.html's
        # loadSyncCards()) would show every source green even when matching
        # - the step that actually produces every discrepancy flag/badge on
        # the dashboard - had silently failed. See each match_*.py command's
        # own header comment for the fix.
        MATCH = "match", "PO<->MIR<->Stock matching"
        RODTEP = "rodtep", "RoDTEP scrip ledger"

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial (some rows failed)"
        FAILED = "failed", "Failed"

    plant = models.CharField(max_length=20, choices=Plant.choices)
    source = models.CharField(max_length=20, choices=Source.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    rows_seen = models.IntegerField(default=0)
    rows_changed = models.IntegerField(default=0)
    error_detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["plant", "source", "-started_at"])]

    def __str__(self):
        return f"{self.plant}/{self.source} @ {self.started_at:%Y-%m-%d %H:%M} ({self.status})"


# ── HRS: Purchase Orders, MIR, Stock, and their reconciliation matches ─────
# The reference plant - every other plant's equivalent model below is
# documented relative to this section rather than repeating itself.

class HRSDomesticPurchaseOrder(models.Model):
    """Synced from Master_HRS_SILVASSA_Domestic_Purchase_Data.csv (the CSV
    itself is populated the existing way: new PO folders detected,
    extracted by hand, appended to the CSV - this app only reads it)."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, default="INR")

    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_type = models.CharField(max_length=30, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)

    # Not a real SAP/vendor code in every case (see the Item Id 11287940
    # collision found during audit) - kept for reference but never used as a
    # sole join key by the matcher.
    is_old_format_template = models.BooleanField(
        default=False,
        help_text="True for the pre-SAP Excel-template POs (HRS/HO/26-27/xxx numbering).",
    )

    synced_from_row_hash = models.CharField(
        max_length=64, blank=True,
        help_text="Hash of the source CSV row(s) this PO was built from, to detect real changes on re-sync.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class HRSDomesticPOLineItem(models.Model):
    """One line of an HRSDomesticPurchaseOrder (one row per material/qty/price on
    the PO). `item_id` is the PO's own item/line identifier as printed on
    the source document - not a Django PK and, per the "Domestic line items
    have no stable natural key" note in CLAUDE.md, not guaranteed unique or
    even present; a plant's sync command deletes and recreates every line
    item belonging to a PO on any change rather than diffing item-by-item."""

    purchase_order = models.ForeignKey(HRSDomesticPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["item_id"]), models.Index(fields=["description"])]

    def __str__(self):
        return f"{self.description} x{self.qty} ({self.purchase_order.po_number})"


class HRSMIREntry(models.Model):
    """Material Inward Register. HRS's own PO-number field is unreliable
    (audit: ~30% blank, ~25% non-standard format) - never join on it as the
    only key, it's used only as a free "tier 1" shortcut when it happens to
    be present and valid."""

    month = models.CharField(max_length=20, blank=True)
    mir_no = models.CharField(max_length=20, blank=True)
    mir_date = models.DateField(null=True, blank=True)
    sap_grn_number = models.CharField(max_length=50, blank=True)

    po_number_raw = models.CharField(
        max_length=100, blank=True,
        help_text="As typed in MIR's own 'Purchase Order. No.' field - unreliable, see docstring above.",
    )

    party_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=10, blank=True)
    invoice_no = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    material_description = models.CharField(max_length=500, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    # decimal_places=3 (not 2) on every *_rate_pct field below - the source
    # sheet stores these as a fraction (2.5% GST as the cell value 0.025),
    # and 2 decimal places silently truncates that to 0.02/0.03. Confirmed
    # against a real row this session (CGST/SGST both 0.025 in the sheet).
    discount_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    discount_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    others = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    taxable_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    igst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    cgst_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    cgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    sgst_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    sgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    other_taxes_excl_gst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tcs_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    tcs_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    invoice_final_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    plant_tag = models.CharField(
        max_length=20, blank=True,
        help_text="MIR's own 'Plant' column (e.g. 'HRS', 'RTP VAPI') - this file mixes plants in one "
                   "register; only rows genuinely belonging to HRS should be trusted for HRS matching.",
    )
    dept_use = models.CharField(max_length=100, blank=True)
    material_category = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)

    source_row_ref = models.CharField(
        max_length=20, blank=True, help_text="Row number in the source MIR sheet, for traceability.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="False once a sync no longer sees this source_row_ref in the sheet (row deleted, or "
                   "the file shrank). Rows are identified by sheet row NUMBER, which shifts if a row is "
                   "inserted/deleted above it - deactivating instead of deleting on disappearance avoids "
                   "destroying PO<->MIR match history (CASCADE) for a row that a shift merely renumbered, "
                   "while still excluding stale rows from matching. See sync_mir.py's module docstring.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_hrs_mir_row")
        ]
        indexes = [
            models.Index(fields=["party_name"]),
            models.Index(fields=["material_description"]),
            models.Index(fields=["po_number_raw"]),
        ]

    def __str__(self):
        return f"MIR {self.mir_no}: {self.material_description} from {self.party_name}"


class HRSRMLot(models.Model):
    """One row per (material, vendor lot) as HRS's own sheet actually
    structures it - NOT one row per material. This is what makes the
    improved MIR<->Stock match possible: match on (material, vendor), not
    material alone."""

    sr_no = models.IntegerField(null=True, blank=True)
    description = models.CharField(max_length=500)
    sap_item_code = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=100, blank=True)
    sub_category = models.CharField(max_length=100, blank=True)
    uom = models.CharField(max_length=20, blank=True)

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    issued = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    todays_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    basic_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    received_date = models.DateField(null=True, blank=True)
    no_of_days = models.IntegerField(null=True, blank=True)
    party_name = models.CharField(max_length=255, blank=True)
    location_tag = models.CharField(
        max_length=50, blank=True,
        help_text="Raw value from the Stock sheet's unlabeled last column (e.g. 'HRS', 'RTP-1') - "
                   "HRS's own Stock file tracks some shared-warehouse lots under this tag.",
    )

    source_row_ref = models.CharField(
        max_length=20, blank=True,
        help_text="Sheet row number at last sync - diagnostic only. natural_key (below) is the real "
                   "identity; a row number shifts if a row is inserted/deleted above it, see "
                   "natural_key's own help_text.",
    )
    natural_key = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="Stable business identity - see apps/services/stock_identity.py. Replaces "
                   "source_row_ref as the sync upsert key so an inserted sheet row can't silently "
                   "re-label this lot as a different material.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="False once a sync no longer sees this natural_key in the sheet (lot sold out/"
                   "removed). Deactivating instead of deleting preserves this lot's "
                   "HRSRMSnapshot history (CASCADE) and excludes it from matching - see "
                   "HRSMIREntry.is_active's help_text for the same is_active/CASCADE reasoning "
                   "(that model still keys on source_row_ref; this one no longer does).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["natural_key"], condition=models.Q(natural_key__gt=""),
                name="uniq_hrs_stock_natural_key",
            )
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["party_name"]),
            models.Index(fields=["sap_item_code"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.party_name})"


class HRSRMSnapshot(models.Model):
    """One row per (stock lot, day). Captured once daily by a scheduled job
    - replaces dated whole-file copies with real history: trend a rate over
    time, see exactly when a discrepancy first appeared."""

    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(HRSRMLot, on_delete=models.CASCADE, related_name="snapshots")

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3)
    received = models.DecimalField(max_digits=14, decimal_places=3)
    issued = models.DecimalField(max_digits=14, decimal_places=3)
    todays_stock = models.DecimalField(max_digits=14, decimal_places=3)
    basic_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stock_lot", "snapshot_date"], name="uniq_hrs_snapshot_per_lot_per_day")
        ]
        indexes = [
            models.Index(fields=["snapshot_date"]),
            # Phase C (Snapshot Pipeline Rebuild) reads "every lot's position
            # on date D" - scans by date, joins by lot; the Days-Left Engine
            # reads the same shape.
            models.Index(fields=["snapshot_date", "stock_lot"]),
        ]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class HRSPOMirMatch(models.Model):
    """Reconciliation result linking one HRSDomesticPOLineItem to the HRSMIREntry
    it was matched against (see apps/services/matching.py's module
    docstring for the full scoring approach). `tier` records HOW the match
    was found - PO_NUMBER is a free exact/substring shortcut on MIR's own
    (unreliable) po_number_raw field, WEIGHTED is the vendor-gated scored
    match used whenever the shortcut isn't available or doesn't fire.
    OneToOneField on po_line_item (not ForeignKey) because a line item has
    at most one MIR match; mir_entry is a plain ForeignKey since one MIR row
    can, in principle, be the best match for more than one line item run
    (matching.py doesn't enforce MIR-side uniqueness).
    `dismissed_by_override` and friends let an editor manually mark a
    flagged match as reviewed-and-fine without changing the underlying
    data - see CLAUDE.md's "Dismiss/override a flagged match" section;
    `update_or_create()`'s `defaults` in matching.py never touches these
    columns, so a dismissal survives every re-match."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09-07)"

    po_line_item = models.OneToOneField(HRSDomesticPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Identification/Financial-Check redesign (2026-09-07, project owner
    # spec - see apps/services/matching_core.py's module docstring for the
    # full algorithm): identification is now "vendor mandatory plus one of
    # material/PO number", not a blended score - these two record which of
    # the latter two actually fired for the winning candidate (vendor is
    # implied True on every row here, it's the hard gate that built the
    # candidate pool in the first place).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    # Financial check now only raises a hard error for Qty/Rate - these two
    # ARE what `is_flagged` means now. Kept as their own columns (not just
    # derived from qty_diff_pct/rate_diff_pct at read time) so a reviewer-
    # facing "Qty Mismatched"/"Rate Mismatched" label doesn't have to
    # re-derive the zero-tolerance comparison from the raw percentage.
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    # Everything else the financial check compares - UOM family, Net-value,
    # Taxable Value (single-line-item POs only, see
    # matching_core.py's _po_matchable() docstring), GST-type structural
    # consistency, and Final/Invoice Value - folds into this one bucket
    # instead of being blended into is_flagged/severity.
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(
        default=False,
        help_text="True when the PO's declared Tax Type (IGST vs CGST+SGST) is structurally "
                   "inconsistent with which GST columns MIR actually populated.",
    )
    taxable_value_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="PO's Total Value vs MIR's Taxable Value - only computed for a single-line-item "
                   "PO, see matching_core.py's _po_matchable() docstring for why.",
    )
    final_value_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="PO's Total Inclusive Value vs MIR's Final/Invoice Value - same single-line-item "
                   "caveat as taxable_value_diff_pct.",
    )
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class HRSMirStockMatch(models.Model):
    """Reconciliation result linking one HRSMIREntry to the HRSRMLot it
    was matched against - see apps/services/matching_core.py's
    match_mir_entry_stock() docstring for the full identification/financial-
    check design (2026-09-08 extension, HRS only for now). No `tier`/
    `match_score` here unlike *POMirMatch - this pairing only ever has one
    matching strategy per candidate (not a scored pick among many).
    UniqueConstraint on (mir_entry, stock_lot) makes update_or_create()'s
    upsert idempotent across repeated match runs."""

    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(HRSRMLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Stock's REC vs MIR's Qty - only ever populated when REC is nonzero AND this "
                   "candidate matched via date_matched (see match_mir_entry_stock()'s docstring).",
    )
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="MIR's net/taxable value (config.mir_value) vs Stock's own Value column.",
    )

    # Identification (2026-09-08 extension): vendor mandatory (the existing
    # gate) plus one of these two - material stays the same normalized exact
    # comparison this model always used; date_matched (Rec. DT. == MIR's own
    # Date) is the new alternate identification path.
    material_matched = models.BooleanField(default=False)
    date_matched = models.BooleanField(default=False)
    # Financial check: only Rate produces a hard error
    # (rate_mismatched -> is_flagged); Qty and Value fold into data_mismatch.
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mir_entry", "stock_lot"], name="uniq_hrs_mir_stock_pair")
        ]

    def __str__(self):
        return f"{self.mir_entry} <-> {self.stock_lot}"


# ── RTP-Achhad: Purchase Orders, MIR, Stock, and their reconciliation matches
#
# Purchase Order CSV (Master_RTP_Achhad_Domestic_Purchase_Data.csv) has the
# identical column layout to HRS's PO CSV - same parser (apps/core/parsers/
# po_csv.py) is reused as-is, just pointed at a different Drive file/folder.
# MIR and Stock genuinely differ in shape from HRS (see
# apps/core/parsers/achhad_mir.py and achhad_stock.py docstrings), so they
# get their own parsers, and the models below reflect those real
# differences rather than force-fitting HRS's field set:
#   - RTPAchhadMIREntry has no sap_grn_number (Achhad's register never
#     records one) and only one PO-number/PO-date pair (no separate
#     "SAP P.O." columns HRS's file carries).
#   - RTPAchhadRMLot has no vendor/party_name at all - Achhad's Stock
#     sheet is one row per material, not one row per (material, vendor)
#     lot like HRS's - see apps/core/matching_achhad.py for what that means
#     for MIR<->Stock matching confidence.

class RTPAchhadDomesticPurchaseOrder(models.Model):
    """Synced from Master_RTP_Achhad_Domestic_Purchase_Data.csv - identical
    shape to HRSDomesticPurchaseOrder, see that model's docstring."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, default="INR")

    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_type = models.CharField(max_length=30, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)

    is_old_format_template = models.BooleanField(default=False)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class RTPAchhadDomesticPOLineItem(models.Model):
    """Same shape as HRSDomesticPOLineItem - see that class's docstring."""

    purchase_order = models.ForeignKey(RTPAchhadDomesticPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["item_id"]), models.Index(fields=["description"])]

    def __str__(self):
        return f"{self.description} x{self.qty} ({self.purchase_order.po_number})"


class RTPAchhadMIREntry(models.Model):
    """Material Inward Register for Achhad. See apps/core/parsers/
    achhad_mir.py's docstring for exactly how this differs from HRS's MIR
    columns - no SAP GRN number, single PO-number/PO-date pair."""

    month = models.CharField(max_length=20, blank=True)
    mir_no = models.CharField(max_length=20, blank=True)
    mir_date = models.DateField(null=True, blank=True)

    po_number_raw = models.CharField(
        max_length=100, blank=True,
        help_text="As typed/computed in MIR's own 'Purchase Order. No.' field - same reliability "
                   "caveat as HRSMIREntry.po_number_raw, used only as a tier-1 shortcut, never a sole join key.",
    )

    party_name = models.CharField(max_length=255, blank=True)
    # Achhad's own MIR spells out the full state name ("Uttar Pradesh"),
    # unlike HRS's MIR which uses short codes ("DNH & DD") - max_length=10
    # (HRSMIREntry.state's width) truncated real Achhad rows, confirmed
    # against the live file this session.
    state = models.CharField(max_length=50, blank=True)
    invoice_no = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    material_description = models.CharField(max_length=500, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    discount_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    discount_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    others = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    taxable_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    igst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    cgst_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    cgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    sgst_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    sgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    other_taxes_excl_gst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tcs_rate_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    tcs_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    invoice_final_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    plant_tag = models.CharField(max_length=20, blank=True)
    dept_use = models.CharField(max_length=100, blank=True)
    material_category = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)

    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSMIREntry.is_active's help_text - same row-shift/CASCADE reasoning, Achhad's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_achhad_mir_row")
        ]
        indexes = [
            models.Index(fields=["party_name"]),
            models.Index(fields=["material_description"]),
            models.Index(fields=["po_number_raw"]),
        ]

    def __str__(self):
        return f"MIR {self.mir_no}: {self.material_description} from {self.party_name}"


class RTPAchhadRMLot(models.Model):
    """One row per material - NOT per (material, vendor) lot like HRS's
    Stock sheet. Achhad's own Stock file has no vendor column at all, which
    is why RTPAchhadMirStockMatch (apps/core/matching_achhad.py) can only
    gate on normalized material description, a materially weaker match
    than HRS's (material, vendor) gate - see that module's docstring."""

    overall_sr_no = models.IntegerField(null=True, blank=True)
    category_sr_no = models.IntegerField(
        null=True, blank=True,
        help_text="Serial number within the row's category section (resets per category) - "
                   "the sheet's own unlabeled second serial column.",
    )
    description = models.CharField(max_length=500)
    category = models.CharField(
        max_length=100, blank=True,
        help_text="Backfilled from the nearest section-divider row above this one in the source "
                   "sheet (e.g. 'Natural Rubber', 'Synthetic Rubbers') - not a real column.",
    )
    sap_code = models.CharField(max_length=50, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    zone = models.CharField(max_length=50, blank=True)
    msl = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Minimum Stock Level - Achhad-specific column, HRS's Stock sheet has no equivalent.",
    )

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    issued = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    todays_stock = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Sheet's own 'Closing' column - named todays_stock to match HRSRMLot's "
                   "field name, since it plays the same role for matching/display.",
    )
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    physical_stock = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Sheet's own 'Physical' column - Achhad-specific, HRS's Stock sheet has no equivalent.",
    )

    received_date = models.DateField(null=True, blank=True)

    source_row_ref = models.CharField(
        max_length=20, blank=True,
        help_text="See HRSRMLot.source_row_ref's help_text - diagnostic only, Achhad's copy.",
    )
    natural_key = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="See HRSRMLot.natural_key's help_text. Achhad's key is weaker by necessity - "
                   "no vendor column, so two lots of the same material are separated only by the "
                   "occurrence counter (apps/services/stock_identity.py) - the same weaker-gate "
                   "precedent matching_achhad.py already sets, still strictly better than a row number.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSRMLot.is_active's help_text - same row-shift/CASCADE reasoning, Achhad's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["natural_key"], condition=models.Q(natural_key__gt=""),
                name="uniq_achhad_stock_natural_key",
            )
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["sap_code"]),
        ]

    def __str__(self):
        return self.description


class RTPAchhadRMSnapshot(models.Model):
    """Same shape/purpose as HRSRMSnapshot - see that class's docstring.
    Carries `rate` (Achhad's Stock sheet field name) instead of HRS's
    `basic_rate`, matching RTPAchhadRMLot's own field naming."""

    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(RTPAchhadRMLot, on_delete=models.CASCADE, related_name="snapshots")

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3)
    received = models.DecimalField(max_digits=14, decimal_places=3)
    issued = models.DecimalField(max_digits=14, decimal_places=3)
    todays_stock = models.DecimalField(max_digits=14, decimal_places=3)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stock_lot", "snapshot_date"], name="uniq_achhad_snapshot_per_lot_per_day")
        ]
        indexes = [
            models.Index(fields=["snapshot_date"]),
            models.Index(fields=["snapshot_date", "stock_lot"]),
        ]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class RTPAchhadRMDailyMovement(models.Model):
    """One real day's receipt/issue activity for one RTPAchhadRMLot, parsed
    from the Stock file's own daily Recp./Issue matrix (2026-09-08 addition -
    see apps/services/parsers/achhad_stock.py's module docstring for the
    full story: this reconciles exactly against the lot's own monthly
    Received/Issued summary, confirmed against live data, so it isn't a more
    "correct" number - it's the same number with a real calendar date
    attached, which the monthly summary alone can't give). Achhad-only: HRS's
    and Vapi's own Stock files have no equivalent day-by-day matrix.

    Only activity days are stored (received != 0 or issued != 0) - most
    days for most materials have neither, and a dense one-row-per-day-per-
    material grid would be almost entirely zeros. `apps/services/
    stock_consumption.py`'s consumption_stats() reconstructs the implied
    daily stock-level series from these sparse rows (see
    apps/api/routers/_domestic_base.py's _consumption_by_lot()) rather than
    needing a dense row per day here.

    UniqueConstraint on (stock_lot, movement_date) makes re-syncing the same
    month idempotent - a day's receipt/issue total in the live file only
    ever grows within that month (never revised downward, per the reconciled-
    totals check), so re-syncing simply overwrites with the latest figure."""

    stock_lot = models.ForeignKey(RTPAchhadRMLot, on_delete=models.CASCADE, related_name="daily_movements")
    movement_date = models.DateField()
    received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    issued = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    source_row_ref = models.CharField(
        max_length=20, blank=True,
        help_text="Sheet row number this came from at last sync - diagnostic only, same convention as "
                   "RTPAchhadRMLot.source_row_ref.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stock_lot", "movement_date"], name="uniq_achhad_daily_movement_per_lot_per_day")
        ]
        indexes = [
            models.Index(fields=["stock_lot", "movement_date"]),
        ]

    def __str__(self):
        return f"{self.stock_lot.description} movement @ {self.movement_date}"


class RTPAchhadPOMirMatch(models.Model):
    """Same shape as HRSPOMirMatch - see that class's docstring."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09-07)"

    po_line_item = models.OneToOneField(RTPAchhadDomesticPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09-07).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(default=False)
    taxable_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    final_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class RTPAchhadMirStockMatch(models.Model):
    """No vendor gate here, unlike HRSMirStockMatch - Achhad's Stock sheet
    carries no vendor column, so identification is material-or-date only
    (see apps/services/matching_core.py's match_mir_entry_stock() docstring
    for the full 2026-09-08 design, extended to Achhad this pass) - weaker
    confidence by construction than HRS's vendor-gated version; a false-
    positive material match is more likely here and should be read that way."""

    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(RTPAchhadRMLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Stock's Received vs MIR's Qty - only ever populated when Received is nonzero AND "
                   "this candidate matched via date_matched (see match_mir_entry_stock()'s docstring).",
    )
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="MIR's net value (config.mir_value) vs the derived Received x Rate for this lot.",
    )

    material_matched = models.BooleanField(default=False)
    date_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mir_entry", "stock_lot"], name="uniq_achhad_mir_stock_pair")
        ]

    def __str__(self):
        return f"{self.mir_entry} <-> {self.stock_lot}"


# ── RTP-Vapi: Purchase Orders, MIR, Stock, and their reconciliation matches
#
# Purchase Order CSV (Master_RTP_VAPI_Domestic_Purchase_Data.csv) has the
# identical column layout to HRS/Achhad's PO CSV - same parser (apps/core/
# parsers/po_csv.py) is reused as-is, confirmed against the live file this
# session (header matched EXPECTED_HEADER exactly, character for character).
#
# MIR ("RTP VAPI MIR FILE 2026-27.xlsx", sheet " MIR FILE 26-27 RM", header
# row 1, data from row 2 - confirmed against the live file this session) is
# genuinely different in shape from both HRS and Achhad, not a relabeling:
#   - No single "Purchase Order. No." field like Achhad has, and no 4-column
#     PO/SAP-PO split like HRS has. Instead there's a "SAP P.O" column (D)
#     that plays the same role but was empty on every one of the ~1,330 real
#     rows in the live file - RTPVapiMIREntry.po_number_raw still exists (so
#     tier-1 PO-number shortcut matching in matching_vapi.py "just works"
#     the day this column starts getting populated), but as of this build
#     Vapi's PO<->MIR matching can only ever land on the weighted tier.
#   - Three more columns HRS/Achhad don't have at all (E SAP GRN NO, F PARK
#     INV NO., G POST, H post no correction) were also empty on every real
#     row - modeled anyway for schema completeness/forward-compatibility,
#     same reasoning as po_number_raw above.
#   - "item code" (N) was empty on every real row too, unlike HRS's MIR
#     which has no item-code column at all and Achhad which doesn't either -
#     modeled for the same forward-compatibility reason.
#   - No "Net" / discount-rate / discount-amount columns at all - Vapi's own
#     register goes straight from RATE to TAXABLE VALUE with no pre-discount
#     value or discount breakout, unlike HRS/Achhad's Net+Discount Rate+
#     Discount Amt trio.
#   - GST is NOT split into per-component rate/amount pairs the way HRS/
#     Achhad's CGST/SGST columns are. There's exactly one overall rate
#     column ("GST ", stored as gst_rate_pct) plus three separate amount-
#     only columns (IGST/CGST/SGST) with no matching rate column of their
#     own - confirmed against the live file (e.g. GST=18, IGST=19665.0,
#     CGST=0, SGST=0 on one real row). Unlike HRS's *_rate_pct fields, this
#     GST rate is stored as a whole percentage (18.00, not 0.18) - confirmed
#     against real cell values, so gst_rate_pct uses decimal_places=2, not
#     HRS's decimal_places=3-for-a-stored-fraction convention.
#   - TCS (Y) is a single amount column, not HRS/Achhad's TCS-rate +
#     TCS-amount pair.
#   - Two extra date columns neither HRS nor Achhad has at all (AB DATE SEND
#     TO OFFICE, AC DATE SEND TO H O) - populated as dd.mm.yyyy text (e.g.
#     '04.04.2026') on ~all rows for AB, empty on every real row for AC as
#     of this build.
#   - MONTH (A) is always a real Excel date in the live file (no Achhad-style
#     "sometimes text, sometimes autoconverted" ambiguity to reverse-engineer)
#     and MIR NO. (B) is always plain text - see apps/core/parsers/vapi_mir.py.
#
# Stock ("RAVASCO VAPI RM STOCK FILE.xlsx", sheet 'Stock', header row 6, data
# from row 7 - confirmed against the live file this session) covers multiple
# sub-plants/warehouses in one sheet (a real 'PLANT' column per row, values
# 'HRS'/'RTP-1'/'RTP-2' in the live file) with three extra columns HRS's own
# Stock sheet doesn't have at all: Billing On Plant, Material Location, HSN
# Code - all three modeled as real fields, same judgment call Achhad's Stock
# parser made about its own extra MSL/Physical columns. No category-divider-
# row convention like Achhad's - plain single-header-row shape like HRS's,
# with a real per-row serial number (1-168 in the live file, no gaps).
#   - Unlike Achhad's Stock sheet (no vendor column at all), Vapi's DOES have
#     a real 'Supplier Name' column - confirmed by checking every material
#     Description in the live file for duplicates: zero descriptions repeat
#     under a different Supplier Name (168 unique descriptions, 168 rows),
#     so the live data happens to be effectively one-row-per-material today,
#     but the column is a genuine vendor field (not just an echo of PLANT -
#     only 16 of 168 rows have Supplier Name equal to their own PLANT value)
#     and should be trusted as such rather than assumed absent the way
#     Achhad's matcher has to. RTPVapiMirStockMatch therefore uses HRS's
#     stronger (material, vendor) gate, not Achhad's material-only gate -
#     see apps/core/matching_vapi.py.

class RTPVapiDomesticPurchaseOrder(models.Model):
    """Synced from Master_RTP_VAPI_Domestic_Purchase_Data.csv - identical
    shape to HRSDomesticPurchaseOrder, see that model's docstring."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, default="INR")

    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_type = models.CharField(max_length=30, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)

    is_old_format_template = models.BooleanField(default=False)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class RTPVapiDomesticPOLineItem(models.Model):
    """Same shape as HRSDomesticPOLineItem - see that class's docstring."""

    purchase_order = models.ForeignKey(RTPVapiDomesticPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["item_id"]), models.Index(fields=["description"])]

    def __str__(self):
        return f"{self.description} x{self.qty} ({self.purchase_order.po_number})"


class RTPVapiMIREntry(models.Model):
    """Material Inward Register for Vapi. See apps/core/parsers/vapi_mir.py's
    docstring and the big RTP-Vapi section header comment above for exactly
    how this differs from both HRS's and Achhad's MIR columns."""

    month = models.CharField(max_length=20, blank=True)
    mir_no = models.CharField(max_length=20, blank=True)
    mir_date = models.DateField(null=True, blank=True)

    po_number_raw = models.CharField(
        max_length=100, blank=True,
        help_text="MIR's own 'SAP P.O' field - empty on every row confirmed this session, kept for "
                   "forward compatibility (see the RTP-Vapi section header comment). Same reliability "
                   "caveat as HRSMIREntry.po_number_raw when it does start being populated: a tier-1 "
                   "shortcut only, never a sole join key.",
    )
    sap_grn_number = models.CharField(max_length=50, blank=True)
    park_invoice_no = models.CharField(max_length=50, blank=True)
    post = models.CharField(max_length=50, blank=True)
    post_no_correction = models.CharField(max_length=50, blank=True)

    party_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=50, blank=True)
    invoice_no = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    material_description = models.CharField(max_length=500, blank=True)
    item_code = models.CharField(max_length=50, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    taxable_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    others_with_gst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    # Whole percentage (18.00), not a stored fraction - see section header
    # comment for why this differs from HRS/Achhad's *_rate_pct convention.
    gst_rate_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    igst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    cgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    sgst_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    other_taxes_excl_gst = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tcs_amt = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    invoice_final_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    material_category = models.CharField(max_length=100, blank=True)
    date_sent_to_office = models.DateField(null=True, blank=True)
    date_sent_to_ho = models.DateField(null=True, blank=True)

    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSMIREntry.is_active's help_text - same row-shift/CASCADE reasoning, Vapi's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_vapi_mir_row")
        ]
        indexes = [
            models.Index(fields=["party_name"]),
            models.Index(fields=["material_description"]),
            models.Index(fields=["po_number_raw"]),
        ]

    def __str__(self):
        return f"MIR {self.mir_no}: {self.material_description} from {self.party_name}"


class RTPVapiRMLot(models.Model):
    """One row per material (and, structurally, per vendor lot - see the
    RTP-Vapi section header comment on why this is treated as lot-shaped
    like HRSRMLot rather than material-shaped like RTPAchhadRMLot,
    even though no duplicate Description currently appears under more than
    one Supplier Name)."""

    sr_no = models.IntegerField(null=True, blank=True)
    plant_tag = models.CharField(
        max_length=20, blank=True,
        help_text="Stock sheet's own 'PLANT' column (e.g. 'HRS', 'RTP-1', 'RTP-2') - this single "
                   "sheet covers several sub-plants/warehouses at once, same idea as HRSRMLot's "
                   "own location_tag field.",
    )
    description = models.CharField(max_length=500)
    category = models.CharField(max_length=100, blank=True)
    sub_category = models.CharField(max_length=100, blank=True)
    uom = models.CharField(max_length=20, blank=True)

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    issued = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    todays_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    basic_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    received_date = models.DateField(null=True, blank=True)
    supplier_name = models.CharField(max_length=255, blank=True)
    billing_on_plant = models.CharField(max_length=50, blank=True)
    material_location = models.CharField(max_length=100, blank=True)
    hsn_code = models.CharField(max_length=20, blank=True)

    source_row_ref = models.CharField(
        max_length=20, blank=True,
        help_text="See HRSRMLot.source_row_ref's help_text - diagnostic only, Vapi's copy.",
    )
    natural_key = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="See HRSRMLot.natural_key's help_text - Vapi's copy, keyed on hsn_code/"
                   "supplier_name (apps/services/stock_identity.py).",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSRMLot.is_active's help_text - same row-shift/CASCADE reasoning, Vapi's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["natural_key"], condition=models.Q(natural_key__gt=""),
                name="uniq_vapi_stock_natural_key",
            )
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["supplier_name"]),
            models.Index(fields=["hsn_code"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.supplier_name})"


class RTPVapiRMSnapshot(models.Model):
    """Same shape/purpose as HRSRMSnapshot - see that class's docstring."""

    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(RTPVapiRMLot, on_delete=models.CASCADE, related_name="snapshots")

    opening_stock = models.DecimalField(max_digits=14, decimal_places=3)
    received = models.DecimalField(max_digits=14, decimal_places=3)
    issued = models.DecimalField(max_digits=14, decimal_places=3)
    todays_stock = models.DecimalField(max_digits=14, decimal_places=3)
    basic_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stock_lot", "snapshot_date"], name="uniq_vapi_snapshot_per_lot_per_day")
        ]
        indexes = [
            models.Index(fields=["snapshot_date"]),
            models.Index(fields=["snapshot_date", "stock_lot"]),
        ]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class RTPVapiPOMirMatch(models.Model):
    """Same shape as HRSPOMirMatch - see that class's docstring. In
    practice this plant's matches are almost always tier WEIGHTED, since
    Vapi's own po_number_raw was 100% blank across every row checked (see
    the RTP-Vapi section header comment) - the PO_NUMBER tier is kept for
    forward compatibility, not because it currently fires."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09-07)"

    po_line_item = models.OneToOneField(RTPVapiDomesticPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09-07) - these
    # columns exist here purely so matching_core.py's shared
    # match_po_mir_line_item()/run_full_match() can write to this model too
    # (it's one shared function, not a per-plant branch); Vapi's own
    # business-logic wiring (tax_type/total_value/total_inclusive_value
    # checks) is real and running (Vapi's PO CSV has the same columns as
    # HRS/Achhad's), it just hasn't been separately validated against real
    # Vapi data the way HRS/Achhad have - see matching_core.py's module
    # docstring.
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(default=False)
    taxable_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    final_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class RTPVapiMirStockMatch(models.Model):
    """(material, vendor)-gated, like HRSMirStockMatch - see the RTP-Vapi
    section header comment for why this plant uses the stronger gate rather
    than Achhad's material-only one. Identification/financial-check
    extension (2026-09-08, see apps/services/matching_core.py's
    match_mir_entry_stock() docstring) applied here too - Vapi has a real
    vendor column (Supplier Name) to anchor the date-as-identification path
    the way HRS does, unlike Achhad which had to stay material-mandatory."""

    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(RTPVapiRMLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Stock's REC vs MIR's Qty - only ever populated when REC is nonzero AND this "
                   "candidate matched via date_matched (see match_mir_entry_stock()'s docstring).",
    )
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="MIR's net/taxable value (config.mir_value) vs the derived REC x Basic Rate for this lot.",
    )

    material_matched = models.BooleanField(default=False)
    date_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mir_entry", "stock_lot"], name="uniq_vapi_mir_stock_pair")
        ]

    def __str__(self):
        return f"{self.mir_entry} <-> {self.stock_lot}"


# ── Import Purchase Orders (HRS / RTP-Achhad / RTP-Vapi) ───────────────────
#
# Synced from each plant's own "Imports Purchase Data" master CSV - a
# genuinely different document from the domestic PO CSVs above (BOE/customs
# clearance, bill of lading, exchange rate, country of origin, export
# license). Confirmed live (2026-09-04): all three plants' Import CSVs share
# byte-for-byte identical headers, so one parser (apps/services/parsers/
# import_po_csv.py) is reused for all three, same reasoning po_csv.py
# already relies on for the domestic CSVs - but each plant still gets its
# own model classes, per this file's top-of-file "no shared plant column"
# convention. RTP-Vapi's file has real data (29 POs / 37 line items
# confirmed live); HRS's and RTP-Achhad's are header-only today.
#
# Field split: PO-level fields (vendor/billing/payment terms) are read from
# a PO's first CSV row, same as the domestic parser. BOE/shipment/customs
# fields (BOE Number, Bill of Lading, Laden on Board Date, Country of
# Origin, License Type/Number, Exchange Rate, Tax Type, Currency (After
# Taxes), Total Inclusive Value) live on the LINE ITEM instead of the PO,
# because a single PO can clear customs in multiple partial BOE shipments
# with different clearance data per item - putting these on the PO would
# silently collapse that real per-item variation to whichever row happened
# to be seen first.

class HRSImportPurchaseOrder(models.Model):
    """PO-level fields for an HRS import purchase, synced from that plant's
    Imports Purchase Data master CSV. Deliberately its own model, not a
    subtype/extension of HRSDomesticPurchaseOrder - `tax_type`/exchange-rate/BOE-
    customs fields live on the LINE ITEM instead of here (see this section's
    header comment for why: a single PO can clear customs in multiple
    partial BOE shipments with different clearance data per item), so this
    PO-level model is intentionally a subset of HRSDomesticPurchaseOrder's own
    field set, not a superset - sharing one table would mean always-null
    columns on whichever side doesn't have a given field."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, blank=True, help_text="Currency (As Per PO), e.g. USD.")
    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True, help_text="Total Value (As per PO), in `currency`.")
    remarks = models.TextField(blank=True)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class HRSImportPOLineItem(models.Model):
    """One line of an HRSImportPurchaseOrder, carrying both the ordered
    quantity (`qty_as_per_po`) and what customs actually cleared
    (`qty_as_per_boe`) - matching against MIR compares the latter, since a
    partial/split shipment means the two can legitimately differ (see
    CLAUDE.md's "Import PO <-> MIR reconciliation"). BOE/bill-of-lading/
    exchange-rate/license fields live here rather than on the PO for the
    same per-item-can-differ reason - see this section's header comment."""

    purchase_order = models.ForeignKey(HRSImportPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty_as_per_po = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    qty_as_per_boe = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_date_raw = models.CharField(
        max_length=100, blank=True,
        help_text="Verbatim source text when Delivery Date isn't a parseable calendar date "
                   "(e.g. free text like 'End Mar/Early Apr 2026') - delivery_date stays null "
                   "in that case rather than guessing a date (spec: delivery_date_status=Unknown).",
    )
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    tax_type = models.CharField(max_length=30, blank=True)
    currency_after_taxes = models.CharField(max_length=10, blank=True)
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    total_inclusive_value = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        help_text="Final landed INR value (source column: 'Total Inclusive Value (Final Bill Paid to get shipment from Port)').",
    )

    boe_number = models.CharField(max_length=50, blank=True)
    bill_of_lading_number = models.CharField(max_length=100, blank=True)
    laden_on_board_date = models.DateField(null=True, blank=True)
    country_of_origin = models.CharField(max_length=100, blank=True)
    license_type = models.CharField(max_length=100, blank=True)
    license_number = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["item_id"]),
            models.Index(fields=["boe_number"]),
            models.Index(fields=["country_of_origin"]),
        ]

    def __str__(self):
        return f"{self.description} x{self.qty_as_per_po} ({self.purchase_order.po_number})"


class HRSImportPOMirMatch(models.Model):
    """PO<->MIR match for an *import* line item, against the same
    HRSMIREntry table domestic matching already uses - MIR/Stock are shared
    Drive files across domestic and import purchases for a plant, only the
    PO-source CSV differs (confirmed 2026-09-04, corrects an earlier
    assumption that imports had no MIR-equivalent data source at all - see
    CLAUDE.md's "Import <-> MIR reconciliation" section). Compared against
    `qty_as_per_boe`, not `qty_as_per_po` - BOE qty is what customs recorded
    as actually clearing/arriving, the real-world equivalent of what MIR
    logs as physically received (project owner, 2026-09-04); `qty_as_per_po`
    is only the originally ordered amount and can legitimately differ from
    what a partial/split shipment's BOE records.

    A separate MIR<->Stock match table is NOT needed here - HRSMirStockMatch
    is already keyed on mir_entry alone, independent of whether that MIR row
    traces back to a domestic or an import PO, so the existing table already
    covers the Stock leg for both.

    Identification/Financial-Check redesign (2026-09, extended to Imports for
    HRS/Achhad only - project owner: "keep vapi out for now", see
    matching_vapi.py's own comment): same fields/reasoning as
    HRSPOMirMatch's identically-named columns below - see that model's
    docstring and matching_core.py's module docstring for the full
    algorithm. Compared against qty_as_per_boe (this is the BOE-vs-MIR qty
    check; the separate PO-vs-BOE qty check already existed independently
    in apps/services/import_flags.py and is unaffected by this redesign)."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09 imports redesign)"

    po_line_item = models.OneToOneField(HRSImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09, imports).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(default=False)
    taxable_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    final_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class RTPAchhadImportPurchaseOrder(models.Model):
    """Same shape as HRSImportPurchaseOrder - see that model's section docstring."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, blank=True)
    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class RTPAchhadImportPOLineItem(models.Model):
    """Same shape as HRSImportPOLineItem - see that class's docstring."""

    purchase_order = models.ForeignKey(RTPAchhadImportPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty_as_per_po = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    qty_as_per_boe = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_date_raw = models.CharField(max_length=100, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    tax_type = models.CharField(max_length=30, blank=True)
    currency_after_taxes = models.CharField(max_length=10, blank=True)
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    boe_number = models.CharField(max_length=50, blank=True)
    bill_of_lading_number = models.CharField(max_length=100, blank=True)
    laden_on_board_date = models.DateField(null=True, blank=True)
    country_of_origin = models.CharField(max_length=100, blank=True)
    license_type = models.CharField(max_length=100, blank=True)
    license_number = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["item_id"]),
            models.Index(fields=["boe_number"]),
            models.Index(fields=["country_of_origin"]),
        ]

    def __str__(self):
        return f"{self.description} x{self.qty_as_per_po} ({self.purchase_order.po_number})"


class RTPAchhadImportPOMirMatch(models.Model):
    """Same shape/reasoning as HRSImportPOMirMatch - see that model's
    docstring, including the 2026-09 imports identification/financial-check
    redesign extension (HRS/Achhad only, Vapi excluded for now)."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09 imports redesign)"

    po_line_item = models.OneToOneField(RTPAchhadImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09, imports).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(default=False)
    taxable_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    final_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class RTPVapiImportPurchaseOrder(models.Model):
    """Same shape as HRSImportPurchaseOrder - see that model's section docstring."""

    po_drive_folder_name = models.CharField(max_length=100)
    po_number = models.CharField(max_length=100, unique=True)
    po_created_date = models.DateField(null=True, blank=True)

    vendor_name = models.CharField(max_length=255)
    vendor_address = models.TextField(blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    vendor_email = models.CharField(max_length=255, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)

    billing_address = models.TextField(blank=True)
    ship_to = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, blank=True)
    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["vendor_name"]),
            models.Index(fields=["po_drive_folder_name"]),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vendor_name})"


class RTPVapiImportPOLineItem(models.Model):
    """Same shape as HRSImportPOLineItem - see that class's docstring. Vapi
    is the plant with real, non-header-only data in this table (29 POs / 37
    line items confirmed live 2026-09-04) - HRS's and Achhad's own Import
    CSVs were still header-only as of that date."""

    purchase_order = models.ForeignKey(RTPVapiImportPurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_id = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty_as_per_po = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    qty_as_per_boe = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_date_raw = models.CharField(max_length=100, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    tax_type = models.CharField(max_length=30, blank=True)
    currency_after_taxes = models.CharField(max_length=10, blank=True)
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    boe_number = models.CharField(max_length=50, blank=True)
    bill_of_lading_number = models.CharField(max_length=100, blank=True)
    laden_on_board_date = models.DateField(null=True, blank=True)
    country_of_origin = models.CharField(max_length=100, blank=True)
    license_type = models.CharField(max_length=100, blank=True)
    license_number = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["item_id"]),
            models.Index(fields=["boe_number"]),
            models.Index(fields=["country_of_origin"]),
        ]

    def __str__(self):
        return f"{self.description} x{self.qty_as_per_po} ({self.purchase_order.po_number})"


class RTPVapiImportPOMirMatch(models.Model):
    """Same shape/reasoning as HRSImportPOMirMatch - see that model's
    docstring, including the imports identification/financial-check
    redesign extension (2026-09, HRS/Achhad first; Vapi added the same day
    on project owner request - "use HRS/Achhad's settings for Vapi imports
    too"). Vapi's own MIR schema differences (no `net` column, `igst_amt`
    instead of `igst`, etc.) are handled generically already -
    matching_core.py's `_tax_type_mismatch()`/`_MatchConfig.mir_taxable_value`/
    `mir_final_value` all read via `getattr()` fallbacks - so no Vapi-specific
    field mapping was needed here beyond flipping `import_extended_fields=True`
    in matching_vapi.py. Vapi's *domestic* PO<->MIR matching config
    (MATERIAL_MATCH_THRESHOLD=0.2, mir_value=taxable_value) is deliberately
    left exactly as tuned against real Vapi data - "keep the domestic in
    reference" - since import_extended_fields only changes which columns
    get WRITTEN, not the identification thresholds/candidate scoring, which
    stay shared with Vapi's already-validated domestic matching."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09 imports redesign)"

    po_line_item = models.OneToOneField(RTPVapiImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09, imports).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    qty_mismatched = models.BooleanField(default=False)
    rate_mismatched = models.BooleanField(default=False)
    data_mismatch = models.BooleanField(default=False)
    tax_type_mismatch = models.BooleanField(default=False)
    taxable_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    final_value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Added 2026-09-08 (Data Quality Flags clarity pass): these three were
    # already being computed in matching_core.py's _diffs_and_flag() (as
    # value_flagged/taxable_value_flagged/final_value_flagged) and folded
    # into the single data_mismatch boolean above, discarding which
    # specific check actually fired. Kept individually now so the frontend
    # can show "Taxable Value Mismatch" vs "Final Amount Mismatch" as their
    # own distinct, filterable Data Quality Flag categories instead of one
    # opaque bucket - see flags.js's computePoFlags().
    net_value_mismatched = models.BooleanField(default=False)
    taxable_value_mismatched = models.BooleanField(default=False)
    final_value_mismatched = models.BooleanField(default=False)

    # Match Accuracy Programme fixes 2.C/2.D (apps/services/matching_core.py):
    # uom_mismatch is True when qty/rate's units belong to different
    # families (e.g. mass vs count) - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    # field_coverage is the summed weight (0..1) of the four scoring factors
    # actually present for this match - a sparse MIR row (blank rate, blank
    # taxable value) still matches on what IS present instead of being
    # scored as though missing data were a bad value, but the interface can
    # use this to distinguish a confident match from one resting on thin
    # evidence.
    uom_mismatch = models.BooleanField(default=False)
    field_coverage = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)

    class Severity(models.TextChoices):
        ROUNDING = "rounding", "Rounding"
        MINOR = "minor", "Minor"
        MATERIAL = "material", "Material"

    # Match Accuracy Programme fix 3.F: splits the single is_flagged boolean
    # into a severity band so a reviewer sees material discrepancies first
    # instead of hunting for them among rounding noise - nothing is
    # discarded, every flagged match is still shown, just groupable by
    # seriousness. None when there's no measurable discrepancy at all (every
    # diff is None or exactly zero). Bucketed at the same 5%/20% cut points
    # frontend/js/flags.js's rowTintClass() already uses for row-shading -
    # not a newly-invented threshold.
    severity = models.CharField(max_length=10, choices=Severity.choices, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


# ── Shared: corrections/dismissals audit trail (cross-plant, generic logs) ─
# Unlike everything above, these tables are NOT split per plant - each one
# carries its own `plant` CharField instead, same reasoning SyncRun already
# uses: a correction/dismissal row is a generic log entry, not a plant-
# shaped data table with per-plant column differences to protect.

class ImportPOCorrection(models.Model):
    """Audit trail for inline field edits made against an Import PO/line item
    from the "Flags & Corrections" tab. Shared across plants - a generic log,
    not a plant-shaped data table, same reasoning SyncRun already uses.
    Never mutates the correction away - the PATCH endpoint writes the new
    value onto the real PO/item row AND a row here, so "who changed what,
    when, from what" stays reconstructable even though the live row itself
    only ever holds the current value."""

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    po_number = models.CharField(max_length=100)
    item_id = models.CharField(
        max_length=50, blank=True,
        help_text="Blank for a PO-level field correction; set for a line-item field correction.",
    )
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.TextField(
        blank=True, default="",
        help_text="Why the requester believes the old value was wrong / what they verified the new value against - optional, entered in the 'Submit a Correction' panel on the Flags & Corrections tab.",
    )

    corrected_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="import_po_corrections")
    corrected_by_email = models.CharField(max_length=255, blank=True)
    corrected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-corrected_at"]
        indexes = [models.Index(fields=["plant", "po_number"])]

    def __str__(self):
        return f"{self.plant}/{self.po_number}/{self.field_name} -> {self.new_value!r} ({self.corrected_by_email})"


class DomesticPOCorrection(models.Model):
    """Audit trail for inline field edits made against a Domestic PO/line item
    from the PO detail modal's new "Flags & Corrections" tab. A separate model
    from ImportPOCorrection (not a generalized/shared table) - same reasoning
    this codebase already uses for keeping HRS/Achhad/Vapi's own models
    separate: Domestic and Import POs are genuinely different schemas (no BOE/
    BL/license/exchange-rate fields on the Domestic side), and reusing one
    table would mean either an always-blank `item_id`-shaped column set or a
    silent assumption that both PO types share fields they don't. Field-for-
    field identical to ImportPOCorrection otherwise - see that model's
    docstring for the "why append-only" rationale."""

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    po_number = models.CharField(max_length=100)
    item_id = models.CharField(
        max_length=50, blank=True,
        help_text="Blank for a PO-level field correction; set for a line-item field correction.",
    )
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.TextField(blank=True, default="", help_text="See ImportPOCorrection.reason's help_text - same field, same purpose.")

    corrected_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="domestic_po_corrections")
    corrected_by_email = models.CharField(max_length=255, blank=True)
    corrected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-corrected_at"]
        indexes = [models.Index(fields=["plant", "po_number"])]

    def __str__(self):
        return f"{self.plant}/{self.po_number}/{self.field_name} -> {self.new_value!r} ({self.corrected_by_email})"


class MaterialCorrection(models.Model):
    """Audit trail for inline field edits made against a Stock lot from the
    Raw Material Analysis modal's Stock by Plant table - same append-only
    mutate+audit pattern as DomesticPOCorrection/ImportPOCorrection (see
    ImportPOCorrection's docstring for the "why append-only" rationale).
    Shared across plants rather than split per plant model, same reasoning
    those two corrections already use: this is a generic log table, not a
    plant-shaped data table.

    `lot_id` is the target HRSRMLot/RTPAchhadRMLot/RTPVapiRMLot's
    own pk - unique only combined with `plant` (each plant's Stock lot table
    has its own independent autoincrement id space, so lot_id alone can
    collide across plants)."""

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    lot_id = models.IntegerField()
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.TextField(blank=True, default="", help_text="See ImportPOCorrection.reason's help_text - same field, same purpose.")

    corrected_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="material_corrections")
    corrected_by_email = models.CharField(max_length=255, blank=True)
    corrected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-corrected_at"]
        indexes = [models.Index(fields=["plant", "lot_id"])]

    def __str__(self):
        return f"{self.plant}/lot {self.lot_id}/{self.field_name} -> {self.new_value!r} ({self.corrected_by_email})"


class FlagDismissal(models.Model):
    """Manual dismiss/reinstate for a PO-level flag shown in the Flags &
    Corrections tab - the Quantity/Rate-Value Discrepancy critical flags and
    the Data Quality Flag category (main.js's computePoFlags()/
    categorizeFlag() for Domestic, apps/services/import_flags.py's
    po_flags() for Import). Unlike DomesticPOCorrection/ImportPOCorrection/
    MaterialCorrection (append-only logs of a mutation to a real row),
    there's no underlying row to mutate here - these flags are computed at
    read time, not stored - so this table IS the current dismissed state,
    upserted in place, same shape as `dismissed_by_override` on
    *POMirMatch/*MirStockMatch (see apps/services/match_dismiss.py), just
    for a flag that has no match row of its own to carry that column on.

    `flag_key` is a stable, opaque identifier for one flag on one PO -
    Domestic uses the flag's own display label directly (e.g. "Quantity
    Discrepancy", "Vendor GSTIN anomaly" - safe since computePoFlags() only
    ever produces at most one flag per label per PO); Import uses
    `<code>:<item_id>` (e.g. "F7:ITEM3", or "F3:" for a PO-level flag with no
    item_id) since apps/services/import_flags.py's flags are per-item and
    already carry a stable `code`. Either shape is just a string to this
    table - it never inspects or validates flag_key content."""

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    po_number = models.CharField(max_length=100)
    flag_key = models.CharField(max_length=150)

    dismissed = models.BooleanField(default=True)
    dismissed_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="flag_dismissals")
    dismissed_by_email = models.CharField(max_length=255, blank=True)
    dismissed_reason = models.TextField(blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "po_number", "flag_key"], name="uniq_flag_dismissal")
        ]
        indexes = [models.Index(fields=["plant", "po_number"])]

    def __str__(self):
        return f"{self.plant}/{self.po_number}/{self.flag_key} dismissed={self.dismissed}"


class MaterialCategoryReference(models.Model):
    """Canonical Category/Subcategory lookup, shared across all three plants
    (added 2026-09-08, project owner) - fixes the Raw Material Analysis
    dashboard showing near-one-category-per-material for HRS/Vapi. Root
    cause (confirmed by reading the actual parsers): HRS's and Vapi's own
    Stock files carry a free-text, per-row Category/Sub Category column that
    real-world data fills inconsistently - grouping by that raw string
    produces close to one bucket per material. Achhad's Stock file is
    different (its category comes from real section-divider headers), but
    this table applies uniformly across all three plants as one shared
    company-wide standard, not per-plant data.

    Match key is normalized MATERIAL DESCRIPTION, not SAP Item Code -
    project owner, 2026-09-08: "sap item code is not trust worthy as it's
    not maintain thoroughly". Uses the same normalize_material() every
    other material-identity check in this app already uses (MIR<->Stock
    matching), for consistency - EXACT match after normalization, not fuzzy
    scoring, since a wrong category assignment is worse than leaving
    something Uncategorized for a human to add to this table. HSN is stored
    for reference/audit only, never part of the match - it isn't
    consistently available across all three plants' Stock files (HRS's and
    Achhad's have none at all; only Vapi's does).

    Loaded/updated via `manage.py load_material_category_reference --file
    <csv>` (apps/services/parsers/material_category_reference.py) whenever
    the plant manager sends an updated list - not synced from a live Drive
    file on a schedule like PO/MIR/Stock, since this data changes rarely
    (new materials/categories only) rather than daily. Also editable
    directly via Django Admin for one-off corrections."""

    description = models.CharField(max_length=500, help_text="As given in the reference list, verbatim.")
    normalized_description = models.CharField(
        max_length=500, unique=True,
        help_text="normalize_material(description) with any trailing '(item code)' suffix stripped first - "
                   "the actual join key against every plant's own RM Lot description.",
    )
    category = models.CharField(max_length=200)
    subcategory = models.CharField(
        max_length=200, blank=True,
        help_text="The reference list's own 'Subcategory (SAP Product Group)' column, label portion only "
                   "(e.g. 'CARBON BLACK' from 'CARBON BLACK (RM-CB001)') - see subcategory_code for the code.",
    )
    subcategory_code = models.CharField(max_length=50, blank=True, help_text="e.g. 'RM-CB001' - reference/audit only.")
    hsn_code = models.CharField(max_length=20, blank=True)
    uom = models.CharField(max_length=50, blank=True)
    sap_item_code = models.CharField(
        max_length=50, blank=True,
        help_text="Stored for reference/audit only - NOT the match key (see class docstring for why).",
    )

    source_row_ref = models.CharField(max_length=20, blank=True, help_text="Row number in the source file at last load.")
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["category"]), models.Index(fields=["normalized_description"])]

    def __str__(self):
        return f"{self.description} -> {self.category} / {self.subcategory}"


class DataQualityFlag(models.Model):
    """Match Accuracy Programme, fix 3.G: one row per source-sheet
    arithmetic inconsistency found by apps/services/arithmetic_checks.py
    (qty x rate vs net_value on a PO line item, taxable+gst+tcs-discount vs
    final on a MIR entry, opening+received-issued vs closing on a Stock
    lot) - a real typo in the spreadsheet itself, not a matching artifact.
    Surfaced in the existing Flags & Corrections tab (frontend/js/flags.js)
    rather than a new screen, same rendering shape as FlagDismissal-backed
    flags, just a different data source.

    Generic like FlagDismissal above - `source_type` + `source_id` point at
    whichever of the 9 PO-line-item/MIR-entry/Stock-lot model classes
    `plant` + `source_type` implies, not a real FK, since the target lives
    in a different model class per plant. Upserted in place by
    apps/services/data_quality.py's sync_data_quality_flags() (keyed on
    plant+source_type+source_id+check_name) - a row that stops mismatching
    on a later sync is deleted, not left stale."""

    class SourceType(models.TextChoices):
        PO_LINE_ITEM = "po_line_item", "PO line item"
        MIR_ENTRY = "mir_entry", "MIR entry"
        STOCK_LOT = "stock_lot", "Stock lot"

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_id = models.PositiveIntegerField()
    check_name = models.CharField(max_length=50)

    expected = models.DecimalField(max_digits=16, decimal_places=4)
    actual = models.DecimalField(max_digits=16, decimal_places=4)
    detected_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "source_type", "source_id", "check_name"], name="uniq_data_quality_flag")
        ]
        indexes = [models.Index(fields=["plant", "source_type"])]

    def __str__(self):
        return f"{self.plant}/{self.source_type}/{self.source_id}: {self.check_name} expected={self.expected} actual={self.actual}"


class MatchReview(models.Model):
    """One human judgement on one algorithmic match, for the Match Accuracy
    Programme's measurement harness (doc 03, Phase 1) - CLAUDE.md's "Match
    accuracy: manual validation is required, not optional" section is the
    reason this exists: MATCH_THRESHOLD and the four scoring weights were
    picked by judgement, never validated, because there was no accuracy
    measurement anywhere in this codebase. report_match_accuracy (the
    management command that reads this table) is what finally makes that
    number falsifiable.

    Not a real FK to the underlying match row - `match_id` is a plain
    PositiveIntegerField, resolved against whichever of the 9 *POMirMatch/
    *ImportPOMirMatch/*MirStockMatch model classes `plant` + `match_type`
    implies (see review_views.py). Same "generic pointer by id" shape
    FlagDismissal's own `flag_key` already uses above, for the same reason:
    the target lives in one of several different model classes depending on
    context, not one fixed table a real FK could point at.

    `match_type` splits PO<->MIR, import PO<->MIR, and MIR<->Stock because
    their error profiles genuinely differ (doc 03, 1.1) - a single blended
    accuracy figure would hide that. One reviewer can review the same match
    more than once (no unique constraint) - report_match_accuracy uses the
    most recent verdict per match, so a reviewer correcting their own earlier
    call isn't stuck with it."""

    class MatchType(models.TextChoices):
        PO_MIR = "po_mir", "PO <-> MIR"
        IMPORT_PO_MIR = "import_po_mir", "Import PO <-> MIR"
        MIR_STOCK = "mir_stock", "MIR <-> Stock"

    class Verdict(models.TextChoices):
        CORRECT = "correct", "Correct"
        INCORRECT = "incorrect", "Incorrect"
        UNSURE = "unsure", "Unsure"

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    match_type = models.CharField(max_length=20, choices=MatchType.choices)
    match_id = models.PositiveIntegerField()

    reviewer = models.ForeignKey("PTUser", on_delete=models.CASCADE, related_name="match_reviews")
    verdict = models.CharField(max_length=10, choices=Verdict.choices)
    note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["plant", "match_type", "match_id"])]

    def __str__(self):
        return f"{self.plant}/{self.match_type}/{self.match_id} -> {self.verdict}"


# ── Auth: PTUser, OTPCode, TrustedDevice (device-aware 2FA) ────────────────
# Mirrors the TDS Automation App's own architecture (TDSUser/OTPCode/
# TrustedDevice in that app's apps/core/models.py). See apps/api/
# auth_backend.py's module docstring for why AUTH_USER_MODEL stays Django's
# default and every real auth path resolves PTUser directly instead of
# get_user_model() - PTUser is not an AbstractBaseUser, it's a plain model
# exactly like TDSUser, for the same reason.
#
# Unlike TDSUser/OTPCode/TrustedDevice, these tables have no pre-Django
# history to work around - plain AutoField PKs, no managed=False baggage.

class PTUser(models.Model):
    """Application user, completely independent of Django's auth.User.

    Roles: 'admin' (full access, incl. user management) | 'editor' (full
    dashboard access, plus dismissing/overriding a flagged match and the
    inline "Edit Everywhere" field corrections - see CLAUDE.md's "Dismiss/
    override a flagged match" and "Inline 'Edit Everywhere'" sections) |
    'viewer' (read-only dashboard access - view POs, materials, sync
    status). password_hash is bcrypt (see
    apps/api/auth_backend.py's _verify_password) - never returned by any API
    response and excluded from the Django Admin form (PTUserAdmin).

    `plants` scopes which plants an admin may edit via the inline "Edit
    Everywhere" feature (apps/api/permissions.py's user_can_edit_plant()) -
    an empty list means "all plants" (deliberate default so every admin that
    existed before this field was added keeps full access with no data
    backfill), a non-empty list restricts to just those plant keys (the
    lowercase `hrs`/`achhad`/`vapi` keys from frontend/js/shared.js's PLANTS
    map, NOT SyncRun.Plant's uppercase enum - the two are unrelated). Has no
    bearing on read access - every role can still read every plant's
    dashboard, this only gates writes."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    user_id = models.AutoField(primary_key=True)
    email = models.TextField(unique=True)
    password_hash = models.TextField()
    full_name = models.TextField(null=True, blank=True)
    role = models.TextField(choices=Role.choices, default=Role.VIEWER)
    designation = models.TextField(null=True, blank=True)
    plants = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # Account lockout (added 2026-09-05, hardening pass) - the existing
    # LoginRateThrottle (apps/api/auth_views.py, 5/minute per email) is a
    # rate limit, not a lockout: it slows down guessing but never actually
    # stops it, forever, with no signal that an account is under sustained
    # attack. failed_login_attempts increments on each wrong password
    # (apps/api/auth_backend.py::PTUserBackend.authenticate()) and resets on
    # a successful login; locked_until is set 15 minutes into the future
    # once failed_login_attempts reaches 5, at which point login is refused
    # outright (even with the correct password) until it elapses.
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # "Log out everywhere" (added 2026-09-05, hardening pass) - every JWT
    # issued for this user embeds the current value as a `ver` claim (see
    # apps/api/auth_serializers.py's PTTokenObtainPairSerializer.get_token()).
    # Both access-token auth (apps/api/auth_backend.py's
    # PTJWTAuthentication.get_user()) and refresh (PTTokenRefreshSerializer)
    # reject any token whose `ver` claim doesn't match the user's current
    # value. Incrementing this instantly invalidates every previously issued
    # access AND refresh token at once - a stronger, simpler guarantee than
    # RevokedRefreshToken alone provides (that table only knows about tokens
    # explicitly rotated-away or logged out, not every token ever issued;
    # this stamp needs no such registry, and covers live access tokens too,
    # which per-jti revocation never did). See
    # apps/services/token_revocation.py's revoke_all_sessions().
    token_version = models.PositiveIntegerField(default=0)

    # ── Django/DRF auth protocol ──────────────────────────────────────────
    # PTUser does NOT inherit from AbstractBaseUser, so these must be
    # declared explicitly - DRF's IsAuthenticated permission reads
    # request.user.is_authenticated and raises AttributeError without it.
    is_authenticated = True
    is_anonymous = False

    class Meta:
        db_table = "pt_users"

    def __str__(self):
        return f"{self.email} ({self.role})"


class OTPCode(models.Model):
    """One active email-OTP row per address (login 2FA on a new device).

    Security design (identical to the TDS app's OTPCode - see
    apps/services/otp_service.py): code_hash is a bcrypt hash, never the
    plaintext code; expires_at enforces a 10-minute TTL; attempts increments
    per wrong guess and the row is deleted at the cap; deleted on a
    successful verify (single-use); generate_otp() deletes any existing row
    for that email first (one active code at a time). `email` is unique at
    the DB level - generate_otp() wraps its delete-then-create in a single
    atomic, locked transaction so two concurrent requests for the same email
    can't both insert a row and make verify_otp()'s lookup ambiguous."""

    email = models.EmailField(unique=True)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pt_otp_codes"

    def __str__(self):
        return f"OTP({self.email}, expires={self.expires_at})"


class RevokedRefreshToken(models.Model):
    """Custom refresh-token revocation list (added 2026-09-05, hardening
    pass) - deliberately NOT rest_framework_simplejwt's built-in
    `rest_framework_simplejwt.token_blacklist` app. That app's own
    OutstandingToken model FKs its `user` field to `AUTH_USER_MODEL`
    (Django's default `auth.User`), which this app never uses for real
    accounts - PTUser is a separate, unrelated model (see
    apps/api/auth_backend.py's module docstring on why AUTH_USER_MODEL
    stays at Django's default here). Confirmed incompatible the hard way:
    enabling that app crashed device_verify with "OutstandingToken.user
    must be a User instance" the moment a PTUser was passed to
    RefreshToken.for_user(). This table sidesteps that entirely by keyed
    only on the token's own `jti` claim - no user FK needed at all to check
    or record a revocation.

    Used for two things (see apps/api/auth_serializers.py's
    PTTokenRefreshSerializer and apps/api/routers/device_views.py's
    logout_view): (1) refresh-token rotation revokes the just-spent token's
    jti so it can't be replayed after a successful /api/auth/token/refresh,
    and (2) POST /api/auth/logout revokes the caller's current refresh
    token's jti directly, so a copy made before logout stops working
    immediately rather than surviving up to its full 30-day
    REFRESH_TOKEN_LIFETIME."""

    jti = models.CharField(max_length=255, unique=True)
    revoked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="Mirrors the token's own exp claim - lets a future cleanup "
                   "command purge rows whose token would have expired naturally "
                   "anyway. Not purged automatically yet.",
    )

    class Meta:
        db_table = "pt_revoked_refresh_tokens"
        indexes = [models.Index(fields=["jti"])]

    def __str__(self):
        return f"revoked jti={self.jti}"


class TrustedDevice(models.Model):
    """A verified device token for a PTUser (Instagram-style device trust -
    see apps/services/device_service.py). The real bearer credential is a
    64-char hex string (secrets.token_hex(32), 256-bit entropy), set in an
    httpOnly SameSite=Lax pt_device cookie on the browser - only its SHA-256
    hex digest (also 64 chars, hence no column-width change) is ever
    persisted here, in device_token_hash.

    Security fix, 2026-09-04: this column used to store the raw plaintext
    token directly (named device_token) - inconsistent with this same
    module's OTPCode.code_hash, which was already correctly hash-only for
    exactly this reason ("even direct DB access cannot reveal a valid
    code" - see apps/services/otp_service.py's module docstring). A device-
    trust token is a MORE valuable target than a 6-digit OTP (it bypasses
    the OTP step entirely, for up to DEVICE_COOKIE_MAX_AGE = 1 year), so a
    leaked DB backup or a compromised Postgres instance used to hand an
    attacker a ready-to-use, no-cracking-required 2FA bypass for every
    trusted device. Plain SHA-256 (not bcrypt) is the right hash here,
    unlike OTPCode - a 6-digit OTP has only 10^6 possible values and MUST
    use a slow hash to resist brute-forcing the hash itself; this token
    already has 2^256 possible values, so a fast hash is not brute-
    forceable regardless of speed, and a fast hash is required anyway since
    is_trusted_device() does an equality lookup (WHERE device_token_hash =
    ...) on every authenticated request - bcrypt has no equivalent
    "look up by hash" operation without checking every stored row.
    Migration 0015 renamed the column and hashed every existing row's
    already-known plaintext value in place - no forced re-verification for
    already-trusted devices.

    last_used_at is bumped on every successful is_trusted_device() check.
    Revocable via apps/api/routers/users_views.py's revoke_user_device()
    (Admin Panel > Edit User > Trusted Devices) - previously the admin
    "new device" alert email promised a revoke option here that did not
    yet exist; it does now."""

    user = models.ForeignKey(PTUser, on_delete=models.CASCADE, related_name="trusted_devices")
    device_token_hash = models.CharField(max_length=64, unique=True)
    device_name = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pt_trusted_devices"

    def __str__(self):
        return f"{self.device_name} ({self.user.email})"


# ── RoDTEP scrip ledger (added 2026-09-09) ──────────────────────────────────
# Company-wide, not per-plant - RoDTEP scrips are issued against one shared
# IEC (see the Advance Authorisation letters' own IEC field), tracked in one
# shared Drive folder ("Purchase Orders HO/RODTEP SCRIPT LICENSE"), not
# split per plant the way MIR/Stock/PO data is - see SyncRun.Plant.COMPANY's
# own comment for the same reasoning applied to its SyncRun rows.

class RodtepScrollEntry(models.Model):
    """One row per Shipping Bill that contributed RoDTEP credit to a given
    Script Number - auto-synced from apps/services/parsers/rodtep.py
    (manage.py sync_rodtep), which lists every "RODTEP-JNPT-<N>.xlsx" file
    in the Drive folder and parses each one's own Shipping-Bill-level
    breakdown. See that parser's own module docstring for full context on
    what each column means and why this table has no import-side reference
    at all - RoDTEP credit is earned from EXPORTS (this table's `sb_number`
    is an export Shipping Bill, not an import document); see RodtepUsage for
    how the import side is tracked, since Drive has no structured link
    between a script and which import it was later used against.

    Keyed on (script_no, sb_number), not sr_no (a plain per-file row
    position, not a stable identity across re-syncs - same reasoning this
    app's other source_row_ref-vs-natural-key precedent already
    established, e.g. stock_identity.py) - a script/SB pair is the real,
    stable identity a Shipping Bill's credit belongs to, confirmed unique
    per row in both real files checked."""

    script_no = models.CharField(max_length=50)
    script_date = models.DateField(null=True, blank=True)
    sb_number = models.CharField(max_length=50)
    sb_date = models.DateField(null=True, blank=True)
    scroll_number = models.CharField(max_length=50, blank=True)
    scroll_date = models.DateField(null=True, blank=True)
    scroll_type = models.CharField(max_length=50, blank=True)
    location = models.CharField(max_length=50, blank=True)
    sanctioned_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    source_file_name = models.CharField(
        max_length=255, blank=True,
        help_text="The 'RODTEP-JNPT-<N>.xlsx' file this row was parsed from - lets a reviewer trace a row back to its source file.",
    )
    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rodtep_scroll_entry"
        constraints = [
            models.UniqueConstraint(fields=["script_no", "sb_number"], name="uniq_rodtep_script_sb"),
        ]
        indexes = [models.Index(fields=["script_no"])]

    def __str__(self):
        return f"Script {self.script_no} / SB {self.sb_number} - ₹{self.sanctioned_amount}"


class RodtepUsage(models.Model):
    """Manual entry: records that some of a script's sanctioned credit was
    actually debited against a specific import - Drive has no structured
    record of this side at all (see RodtepScrollEntry's own docstring), so
    this is entered by hand rather than synced. `boe_number` is the primary
    join key back to the Import PO tables (HRSImportPOLineItem.boe_number
    etc.) - matches the CSV structure already handed to the project owner
    for populating this by hand; `import_po_number` is a secondary,
    human-readable cross-check, not itself authoritative.

    Deliberately NOT a ForeignKey to RodtepScrollEntry - a usage entry can
    reference a script_no before that script's own ledger file has been
    synced yet (e.g. entered from the paperwork the same day, before the
    next sync_rodtep run), and a script_no is already the natural join key
    on both sides; forcing an FK would mean rejecting an otherwise-valid
    usage entry purely because of sync ordering."""

    script_no = models.CharField(max_length=50, db_index=True)
    used_amount = models.DecimalField(max_digits=14, decimal_places=2)
    boe_number = models.CharField(max_length=50, blank=True, help_text="Primary join key against *ImportPOLineItem.boe_number.")
    import_po_number = models.CharField(max_length=50, blank=True)
    used_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    entered_by = models.ForeignKey(PTUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="rodtep_usage_entries")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rodtep_usage"
        ordering = ["-used_date", "-created_at"]

    def __str__(self):
        return f"{self.script_no}: ₹{self.used_amount} used against BOE {self.boe_number or '-'}"
