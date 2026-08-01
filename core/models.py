"""
All persisted data for the dashboard, grouped into four concerns (see the
section headers below): Purchase Orders (the source of truth), Advance
Licenses (the real usage ledger, replacing the old live-OCR-guess
approach), RM Stock snapshots (daily history for a file that has no date
column), and access control (who can see which plant, enforced server-side
in core/decorators.py, never just hidden in the frontend).

MIR data is deliberately NOT modeled here - it's read live off Drive via
core/mir_stock.py purely to cross-check PO/Stock data, not stored as its
own system of record (Dishant's explicit call: MIR is a verification layer,
not something we need historical rows for).
"""
from django.db import models


class Plant(models.TextChoices):
    HRS = "HRS", "HRS - Silvassa"
    RTP_ACHHAD = "RTP_ACHHAD", "RTP - Achhad"
    RTP_VAPI = "RTP_VAPI", "RTP - Vapi"


class DocType(models.TextChoices):
    DOMESTIC = "domestic", "Domestic"
    IMPORT = "import", "Import"


# ---------------------------------------------------------------------------
# Purchase Orders - the single source of truth. MIR and RM Stock are only
# ever compared against this data to raise flags, never the other way round.
# ---------------------------------------------------------------------------

class PurchaseOrder(models.Model):
    po_number = models.CharField(max_length=50, unique=True, db_index=True)
    plant = models.CharField(max_length=20, choices=Plant.choices)
    doc_type = models.CharField(max_length=20, choices=DocType.choices)
    drive_folder_id = models.CharField(max_length=100, blank=True)
    vendor_name = models.CharField(max_length=255, blank=True)
    vendor_gstin = models.CharField(max_length=20, blank=True)
    ship_to = models.TextField(blank=True)
    created_date = models.DateField(null=True, blank=True)
    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    tax_type = models.CharField(max_length=20, blank=True)  # CGST_SGST or IGST
    tax_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    total_incl_tax = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    payment_terms = models.CharField(max_length=255, blank=True)
    incoterms = models.CharField(max_length=50, blank=True)
    delivery_mode = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    # Amazon-style status stepper, ported from the Cowork artifact
    status = models.CharField(
        max_length=30,
        choices=[
            ("ordered", "Ordered"),
            ("material_inwarded", "Material Inwarded"),
            ("received_in_inventory", "Received in Inventory"),
        ],
        default="ordered",
    )
    # set by the extraction pipeline when the OCR/parse was uncertain, surfaced
    # in the UI instead of silently guessing
    extraction_confidence = models.CharField(
        max_length=20, choices=[("high", "High"), ("low", "Low")], default="high"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["plant", "doc_type"])]
        ordering = ["-created_date"]

    def __str__(self):
        return f"{self.po_number} ({self.plant})"


class POItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, related_name="items", on_delete=models.CASCADE)
    item_code = models.CharField(max_length=50, blank=True)  # SAP item code, ties into the HSN master
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    net_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)


class POFlag(models.Model):
    """Auto-generated discrepancy flags (PO vs MIR, MIR vs Stock) plus manual
    correction requests submitted through the inline pencil-icon edit UX."""

    purchase_order = models.ForeignKey(PurchaseOrder, related_name="flags", on_delete=models.CASCADE)
    flag_text = models.CharField(max_length=500)
    source = models.CharField(
        max_length=30,
        choices=[
            ("extraction", "Extraction"),
            ("po_vs_mir", "PO vs MIR"),
            ("mir_vs_stock", "MIR vs Stock"),
            ("manual_correction", "Manual Correction"),
        ],
        default="extraction",
    )
    submitted_by_email = models.EmailField(blank=True)
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


# ---------------------------------------------------------------------------
# Advance Authorisation licenses (imports only). Replaces the old "re-parse
# the PDF live on every dashboard load" approach with a real stored ledger.
# ---------------------------------------------------------------------------

class AdvanceLicense(models.Model):
    license_number = models.CharField(max_length=50, unique=True)
    plant = models.CharField(max_length=20, choices=Plant.choices)
    issue_date = models.DateField(null=True, blank=True)
    # Base FTP 2023 rules: 12mo import validity, 18mo export obligation from
    # issue date, up to two 6mo extensions. auto_extension_end captures any
    # DGFT blanket-extension notice (these have been issued periodically and
    # push the real deadline later than the base calculation would suggest).
    import_validity_end = models.DateField(null=True, blank=True)
    export_obligation_end = models.DateField(null=True, blank=True)
    extension_1_end = models.DateField(null=True, blank=True)
    extension_2_end = models.DateField(null=True, blank=True)
    auto_extension_end = models.DateField(null=True, blank=True)
    eodc_status = models.CharField(
        max_length=20, choices=[("open", "Open"), ("closed", "EODC Closed")], default="open"
    )
    total_authorised_cif_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    letter_drive_file_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.license_number


class LicenseItem(models.Model):
    """One row per material listed in the license letter's Section 3 (inputs
    sought to be imported duty free) or its corresponding export output."""

    license = models.ForeignKey(AdvanceLicense, related_name="items", on_delete=models.CASCADE)
    item_type = models.CharField(max_length=10, choices=[("input", "Input"), ("output", "Output")])
    material_description = models.CharField(max_length=500)
    sion_norm = models.CharField(max_length=50, blank=True)
    authorised_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    consumed_qty = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    def remaining_qty(self):
        if self.authorised_qty is None:
            return None
        return self.authorised_qty - self.consumed_qty


class LicensePOUsage(models.Model):
    """The real many-to-many ledger: which PO drew on which license, for what
    material, how much. Updating this is what keeps LicenseItem.consumed_qty
    accurate, instead of re-guessing usage from PO text on every page load."""

    license = models.ForeignKey(AdvanceLicense, related_name="po_usages", on_delete=models.CASCADE)
    purchase_order = models.ForeignKey(PurchaseOrder, related_name="license_usages", on_delete=models.CASCADE)
    material_description = models.CharField(max_length=500, blank=True)
    qty_used = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    value_used = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# ---------------------------------------------------------------------------
# RM Stock snapshots - the fix for "the stock file has no date column and
# gets overwritten daily". One row per material per plant per day.
# ---------------------------------------------------------------------------

class StockSnapshot(models.Model):
    plant = models.CharField(max_length=20, choices=Plant.choices)
    material_code = models.CharField(max_length=50)
    description = models.CharField(max_length=500, blank=True)
    category = models.CharField(max_length=100, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    snapshot_date = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "material_code", "snapshot_date"], name="uniq_stock_snapshot"
            )
        ]
        indexes = [models.Index(fields=["plant", "snapshot_date"])]
        ordering = ["-snapshot_date"]


# ---------------------------------------------------------------------------
# Extraction hand-off queue: Django never calls the Anthropic API directly
# (cost reasons, per Dishant's explicit call). Instead this table is the
# source of truth for "what still needs extracting", and a Claude scheduled
# task reads/writes uniquely-named batch files on Drive against it. See
# core/management/commands/scan_new_pos.py and ingest_extraction_results.py.
# ---------------------------------------------------------------------------

class ExtractionQueue(models.Model):
    plant = models.CharField(max_length=20, choices=Plant.choices)
    doc_type = models.CharField(max_length=20, choices=DocType.choices)
    po_number_hint = models.CharField(max_length=50, blank=True)  # Drive folder name - may not be the final PO number
    drive_folder_id = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("batched", "Batched"),
            ("processed", "Processed"),
            ("failed", "Failed"),
        ],
        default="pending",
    )
    batch_request_file_id = models.CharField(max_length=100, blank=True)
    batch_result_file_id = models.CharField(max_length=100, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["drive_folder_id"], name="uniq_extraction_queue_folder")
        ]


# ---------------------------------------------------------------------------
# Access control. Role/plant checks must be re-verified server-side on every
# request that touches plant data (see core/decorators.py) - never rely on
# the frontend hiding a plant as the actual security boundary.
# ---------------------------------------------------------------------------

class UserAccess(models.Model):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    role = models.CharField(
        max_length=20,
        choices=[("admin", "Admin"), ("editor", "Editor"), ("viewer", "Viewer")],
        default="viewer",
    )
    plants = models.JSONField(default=list, blank=True)  # e.g. ["HRS", "RTP_VAPI"] - ignored for admin (sees all)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def allowed_plants(self):
        if not self.is_active:
            return []
        if self.role == "admin":
            return [c[0] for c in Plant.choices]
        return self.plants

    def __str__(self):
        return f"{self.email} ({self.role})"


class AuditLog(models.Model):
    """Every login and every data-changing action, per the security review -
    who changed what, when."""

    email = models.EmailField()
    action = models.CharField(max_length=100)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
