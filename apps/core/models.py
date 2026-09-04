"""
Core data model for the Purchase Tracker Dashboard.

Deliberately NOT one shared schema with a `plant` discriminator column.
HRS, RTP-Vapi, and RTP-Achhad's MIR/Stock files have genuinely different
column layouts (Vapi/Achhad's Stock file covers several sub-plants in one
file with extra columns - Billing on Plant, Material Location, HSN Code -
that HRS's file doesn't have at all). Forcing them into one shared table
would mean a pile of always-null columns for whichever plants don't have
that field. Each plant gets its own model classes instead.

Three independent Drive-sourced record types (PurchaseOrder/POLineItem,
MIREntry, StockLot), each synced by its own management command, plus the
reconciliation layer that links them (*Match) and a daily snapshot table
that replaces the "copy the whole xlsx to Drive with today's date in the
filename" habit with real queryable history.

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

    class Source(models.TextChoices):
        PO_CSV = "po_csv", "Purchase Order master CSV"
        MIR = "mir", "MIR (Material Inward Register)"
        STOCK = "stock", "Raw Material Stock"
        IMPORT_PO_CSV = "import_po_csv", "Import Purchase Order master CSV"

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


# ===========================================================================
# HRS
# ===========================================================================

class HRSPurchaseOrder(models.Model):
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


class HRSPOLineItem(models.Model):
    purchase_order = models.ForeignKey(HRSPurchaseOrder, on_delete=models.CASCADE, related_name="items")
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


class HRSStockLot(models.Model):
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

    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="False once a sync no longer sees this source_row_ref in the sheet (lot sold out/"
                   "removed, or a row shift). Deactivating instead of deleting preserves this lot's "
                   "HRSStockSnapshot history (CASCADE) and excludes it from matching - see "
                   "HRSMIREntry.is_active's help_text for the full row-shift reasoning.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_hrs_stock_row")
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["party_name"]),
            models.Index(fields=["sap_item_code"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.party_name})"


class HRSStockSnapshot(models.Model):
    """One row per (stock lot, day). Captured once daily by a scheduled job
    - replaces dated whole-file copies with real history: trend a rate over
    time, see exactly when a discrepancy first appeared."""

    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(HRSStockLot, on_delete=models.CASCADE, related_name="snapshots")

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
        indexes = [models.Index(fields=["snapshot_date"])]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class HRSPOMirMatch(models.Model):
    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(HRSPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


class HRSMirStockMatch(models.Model):
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(HRSStockLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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


# ===========================================================================
# RTP-Achhad
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
#   - RTPAchhadStockLot has no vendor/party_name at all - Achhad's Stock
#     sheet is one row per material, not one row per (material, vendor)
#     lot like HRS's - see apps/core/matching_achhad.py for what that means
#     for MIR<->Stock matching confidence.
# ===========================================================================

class RTPAchhadPurchaseOrder(models.Model):
    """Synced from Master_RTP_Achhad_Domestic_Purchase_Data.csv - identical
    shape to HRSPurchaseOrder, see that model's docstring."""

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


class RTPAchhadPOLineItem(models.Model):
    purchase_order = models.ForeignKey(RTPAchhadPurchaseOrder, on_delete=models.CASCADE, related_name="items")
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


class RTPAchhadStockLot(models.Model):
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
        help_text="Sheet's own 'Closing' column - named todays_stock to match HRSStockLot's "
                   "field name, since it plays the same role for matching/display.",
    )
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    physical_stock = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Sheet's own 'Physical' column - Achhad-specific, HRS's Stock sheet has no equivalent.",
    )

    received_date = models.DateField(null=True, blank=True)

    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSStockLot.is_active's help_text - same row-shift/CASCADE reasoning, Achhad's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_achhad_stock_row")
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["sap_code"]),
        ]

    def __str__(self):
        return self.description


class RTPAchhadStockSnapshot(models.Model):
    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(RTPAchhadStockLot, on_delete=models.CASCADE, related_name="snapshots")

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
        indexes = [models.Index(fields=["snapshot_date"])]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class RTPAchhadPOMirMatch(models.Model):
    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(RTPAchhadPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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
    carries no vendor column, so matching is on normalized material
    description alone. Weaker confidence by construction; is_flagged still
    uses the same qty/rate diff threshold, but a false-positive material
    match is more likely here than for HRS and should be read that way."""

    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(RTPAchhadStockLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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


# ===========================================================================
# RTP-Vapi
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
# ===========================================================================

class RTPVapiPurchaseOrder(models.Model):
    """Synced from Master_RTP_VAPI_Domestic_Purchase_Data.csv - identical
    shape to HRSPurchaseOrder, see that model's docstring."""

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


class RTPVapiPOLineItem(models.Model):
    purchase_order = models.ForeignKey(RTPVapiPurchaseOrder, on_delete=models.CASCADE, related_name="items")
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


class RTPVapiStockLot(models.Model):
    """One row per material (and, structurally, per vendor lot - see the
    RTP-Vapi section header comment on why this is treated as lot-shaped
    like HRSStockLot rather than material-shaped like RTPAchhadStockLot,
    even though no duplicate Description currently appears under more than
    one Supplier Name)."""

    sr_no = models.IntegerField(null=True, blank=True)
    plant_tag = models.CharField(
        max_length=20, blank=True,
        help_text="Stock sheet's own 'PLANT' column (e.g. 'HRS', 'RTP-1', 'RTP-2') - this single "
                   "sheet covers several sub-plants/warehouses at once, same idea as HRSStockLot's "
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

    source_row_ref = models.CharField(max_length=20, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="See HRSStockLot.is_active's help_text - same row-shift/CASCADE reasoning, Vapi's copy.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_row_ref"], name="uniq_vapi_stock_row")
        ]
        indexes = [
            models.Index(fields=["description"]),
            models.Index(fields=["supplier_name"]),
            models.Index(fields=["hsn_code"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.supplier_name})"


class RTPVapiStockSnapshot(models.Model):
    snapshot_date = models.DateField()
    stock_lot = models.ForeignKey(RTPVapiStockLot, on_delete=models.CASCADE, related_name="snapshots")

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
        indexes = [models.Index(fields=["snapshot_date"])]

    def __str__(self):
        return f"{self.stock_lot.description} @ {self.snapshot_date}"


class RTPVapiPOMirMatch(models.Model):
    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(RTPVapiPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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
    than Achhad's material-only one."""

    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="stock_matches")
    stock_lot = models.ForeignKey(RTPVapiStockLot, on_delete=models.CASCADE, related_name="mir_matches")

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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


# ===========================================================================
# Import Purchase Orders (HRS / RTP-Achhad / RTP-Vapi)
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
# ===========================================================================

class HRSImportPurchaseOrder(models.Model):
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
    covers the Stock leg for both."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(HRSImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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
    docstring."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(RTPAchhadImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

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
    docstring."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        WEIGHTED = "weighted", "Vendor-gated weighted match"

    po_line_item = models.OneToOneField(RTPVapiImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    is_flagged = models.BooleanField(default=False)
    dismissed_by_override = models.BooleanField(default=False)
    dismissed_by = models.ForeignKey("PTUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.TextField(blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_line_item} <-> {self.mir_entry} ({self.tier})"


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

    corrected_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="domestic_po_corrections")
    corrected_by_email = models.CharField(max_length=255, blank=True)
    corrected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-corrected_at"]
        indexes = [models.Index(fields=["plant", "po_number"])]

    def __str__(self):
        return f"{self.plant}/{self.po_number}/{self.field_name} -> {self.new_value!r} ({self.corrected_by_email})"


# ===========================================================================
# Auth: device-aware 2FA, mirroring the TDS Automation App's architecture
# (TDSUser/OTPCode/TrustedDevice in that app's apps/core/models.py). See
# apps/api/auth_backend.py's module docstring for why AUTH_USER_MODEL stays
# Django's default and every real auth path resolves PTUser directly instead
# of get_user_model() - PTUser is not an AbstractBaseUser, it's a plain model
# exactly like TDSUser, for the same reason.
#
# Unlike TDSUser/OTPCode/TrustedDevice, these tables have no pre-Django
# history to work around - plain AutoField PKs, no managed=False baggage.
# ===========================================================================

class PTUser(models.Model):
    """Application user, completely independent of Django's auth.User.

    Roles: 'admin' (full access, incl. user management) | 'editor' (full
    dashboard access, plus dismissing/overriding a flagged match once that
    endpoint exists) | 'viewer' (read-only dashboard access - view POs,
    materials, sync status). password_hash is bcrypt (see
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


class TrustedDevice(models.Model):
    """A verified device token for a PTUser (Instagram-style device trust -
    see apps/services/device_service.py). device_token is a 64-char hex
    string (secrets.token_hex(32), 256-bit entropy), set in an httpOnly
    SameSite=Lax pt_device cookie on the browser. last_used_at is bumped on
    every successful is_trusted_device() check."""

    user = models.ForeignKey(PTUser, on_delete=models.CASCADE, related_name="trusted_devices")
    device_token = models.CharField(max_length=64, unique=True)
    device_name = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pt_trusted_devices"

    def __str__(self):
        return f"{self.device_name} ({self.user.email})"
