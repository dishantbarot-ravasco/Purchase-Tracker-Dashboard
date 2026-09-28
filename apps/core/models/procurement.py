"""
Procurement: purchase orders and the MIR (Material Inward Register) entered
in this app (2026-09-28, project owner).

Unlike the per-plant Drive mirrors in hrs.py/achhad.py/vapi.py, these tables
are the app's own records, so they are normalized: ONE table per entity with
a `plant` foreign key, because the form, the rules and the numbers are the
same at every plant, and an HRS store may receive against a Vapi PO. The
Drive-mirror models keep their per-plant shape (their source spreadsheets
genuinely differ - see this package's __init__ docstring); nothing here
depends on a spreadsheet's layout.

  Plant             the three plants, with the GST state code that decides
                    IGST vs CGST+SGST/UGST and the MIR number prefix.
  Vendor            one row per GSTIN (a vendor with none is keyed on its
                    cleaned name). PO and MIR point at it; vendor details
                    are never copied onto either.
  PurchaseOrder,    projected from each plant's PO master CSV by
  PurchaseOrderLine apps/services/procurement_sync.py. A line is identified
                    by its position in the order and is never deleted: a
                    line the CSV drops is deactivated, because MIR lines
                    point at it.
  PurchaseOrderLineChange
                    every value the CSV changed on a line, old and new.
  MirReasonCode     the fixed list a mismatch must pick a reason from.
  MirSequence       the per-plant, per-financial-year MIR counter.
  Mir, MirLine      a receipt and its lines, each line against one PO line.
  MirMismatch       every quantity, rate, invoice-total or tax-type
                    difference found at posting, with its reason and whether
                    a purchase manager has resolved it.

"Received so far" and "open quantity" are never stored: they are summed from
posted MIR lines (apps/services/mir_service.py), so a cancelled MIR can never
leave a stale figure behind. What IS stored on a MIR line is what was true
when it was posted - the PO rate and the open quantity it was compared
against, and the computed amounts - because a posted receipt is a record and
must not change when the PO is amended later.
"""

from django.db import models
from django.db.models import F, Q


class Plant(models.Model):
    """code is the lowercase key the frontend and PTUser.plants use."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    # First two digits of the plant's GSTIN - compared with the vendor's.
    state_code = models.CharField(max_length=2)
    # Dadra & Nagar Haveli (HRS) is a union territory: intra-state tax is
    # CGST + UGST there, CGST + SGST in a state.
    is_union_territory = models.BooleanField(default=False)
    mir_prefix = models.CharField(max_length=10, unique=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.code


class Vendor(models.Model):
    gstin = models.CharField(max_length=15, blank=True, default="")
    name = models.CharField(max_length=255)
    # procurement_rules.vendor_name_key(name) - the identity of a vendor with
    # no GSTIN on file.
    name_key = models.CharField(max_length=255)
    vendor_code = models.CharField(max_length=50, blank=True, default="")
    address = models.TextField(blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["gstin"], condition=~Q(gstin=""), name="uniq_vendor_gstin"),
            models.UniqueConstraint(fields=["name_key"], condition=Q(gstin=""), name="uniq_vendor_name_without_gstin"),
            models.CheckConstraint(condition=~Q(name_key=""), name="vendor_name_key_not_blank"),
        ]
        indexes = [models.Index(fields=["name_key"]), models.Index(fields=["vendor_code"])]

    def __str__(self):
        return self.name


class TaxTypeChoice(models.TextChoices):
    IGST = "IGST", "IGST"
    CGST_SGST = "CGST_SGST", "CGST + SGST"
    CGST_UGST = "CGST_UGST", "CGST + UGST"


class PurchaseOrder(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="purchase_orders")
    po_number = models.CharField(max_length=100)
    po_date = models.DateField(null=True, blank=True)
    # Null only for the handful of legacy HRS orders the CSV names no vendor
    # for; a MIR against one names its vendor itself.
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True, blank=True, related_name="purchase_orders")
    currency = models.CharField(max_length=10, default="INR")
    # "" when the sheet's own value is blank or mixed (tax_type_raw keeps it).
    tax_type = models.CharField(max_length=12, choices=TaxTypeChoice.choices, blank=True, default="")
    tax_type_raw = models.CharField(max_length=100, blank=True, default="")
    payment_terms = models.TextField(blank=True, default="")
    incoterms = models.TextField(blank=True, default="")
    ship_to = models.TextField(blank=True, default="")
    total_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    total_inclusive_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True, default="")
    # False once the master CSV stops listing the order. Its lines then take
    # no new receipts; everything already received stays.
    is_active = models.BooleanField(default=True)
    # The legacy row's own change hash - an unchanged order is skipped.
    source_hash = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One PO number can exist at two plants (1000001471 does).
            models.UniqueConstraint(fields=["plant", "po_number"], name="uniq_po_per_plant"),
        ]
        indexes = [models.Index(fields=["po_number"]), models.Index(fields=["is_active"])]

    def __str__(self):
        return f"{self.plant.code}:{self.po_number}"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    # 1-based position in the order - the line's identity (see module docstring).
    line_no = models.PositiveSmallIntegerField()
    item_code = models.CharField(max_length=50, blank=True, default="")
    description = models.CharField(max_length=500)
    hsn = models.CharField(max_length=20, blank=True, default="")
    # procurement_rules.canonical_uom(): KG / MT / L / NOS ..., or the
    # sheet's own spelling upper-cased when it names no known unit.
    uom = models.CharField(max_length=20, blank=True, default="")
    uom_raw = models.CharField(max_length=20, blank=True, default="")
    qty_ordered = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Set when the CSV changed what this line IS (material, item code or
    # unit) after receipts were posted against it: the receipts stay linked,
    # and a purchase manager has to confirm they still belong here.
    needs_review = models.BooleanField(default=False)
    review_note = models.TextField(blank=True, default="")
    # Short-closed: no more receipts expected. closed_by_mir_line is set when
    # a MIR's "close the line" reason did it, so cancelling that MIR reopens it.
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    closed_reason = models.ForeignKey("core.MirReasonCode", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    close_note = models.TextField(blank=True, default="")
    closed_by_mir_line = models.ForeignKey("core.MirLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["purchase_order", "line_no"], name="uniq_po_line_position"),
            models.CheckConstraint(condition=Q(line_no__gte=1), name="po_line_no_positive"),
        ]
        ordering = ["purchase_order_id", "line_no"]


class PurchaseOrderLineChange(models.Model):
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.CASCADE, related_name="changes")
    field = models.CharField(max_length=40)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]


class MirReasonCode(models.Model):
    class Kind(models.TextChoices):
        QTY_SHORT = "QTY_SHORT", "Quantity short"
        QTY_OVER = "QTY_OVER", "Quantity over"
        RATE = "RATE", "Rate differs"
        INVOICE_TOTAL = "INVOICE_TOTAL", "Invoice total differs"
        TAX_TYPE = "TAX_TYPE", "Tax type differs"

    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    label = models.CharField(max_length=120)
    # A short receipt with this reason closes the PO line (no balance to come).
    closes_line = models.BooleanField(default=False)
    note_required = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "sort_order", "id"]
        constraints = [
            # Only a shortfall can close a line.
            models.CheckConstraint(condition=Q(closes_line=False) | Q(kind="QTY_SHORT"), name="reason_closes_only_short"),
        ]

    def __str__(self):
        return self.code


class MirSequence(models.Model):
    """Per plant and financial year; the next MIR takes last_seq + 1 under a
    row lock, so numbers never repeat or skip (a cancelled MIR keeps its)."""

    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="+")
    fy = models.CharField(max_length=7)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plant", "fy"], name="uniq_mir_sequence")]


class Mir(models.Model):
    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    # The plant that RECEIVED the goods - not necessarily the PO's plant.
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="mirs")
    fy = models.CharField(max_length=7)
    seq = models.PositiveIntegerField()
    mir_no = models.CharField(max_length=30, unique=True)
    mir_date = models.DateField()
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="mirs")
    invoice_no = models.CharField(max_length=60)
    # procurement_rules.invoice_key() - what duplicates are checked on.
    invoice_key = models.CharField(max_length=60)
    invoice_date = models.DateField()
    # The invoice's own financial year: vendors restart numbering each April.
    invoice_fy = models.CharField(max_length=7)
    invoice_total = models.DecimalField(max_digits=16, decimal_places=2)
    tcs_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_type = models.CharField(max_length=12, choices=TaxTypeChoice.choices)
    # What the plant's and vendor's states imply (procurement_rules.
    # expected_tax_type()); a different tax_type is a TAX_TYPE mismatch.
    tax_type_expected = models.CharField(max_length=12, choices=TaxTypeChoice.choices, blank=True, default="")
    challan_no = models.CharField(max_length=60, blank=True, default="")
    lr_no = models.CharField(max_length=60, blank=True, default="")
    vehicle_no = models.CharField(max_length=30, blank=True, default="")
    eway_bill_no = models.CharField(max_length=30, blank=True, default="")
    gate_entry_no = models.CharField(max_length=40, blank=True, default="")
    weighbridge_slip_no = models.CharField(max_length=40, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED)
    # Who posted it. The email is kept as well because a user row can be
    # deleted; the record of who entered a receipt must survive that.
    created_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, related_name="+")
    created_by_email = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_by_email = models.CharField(max_length=255, blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "fy", "seq"], name="uniq_mir_seq_per_plant_fy"),
            # One posted MIR per vendor invoice, at ANY plant.
            models.UniqueConstraint(
                fields=["vendor", "invoice_key", "invoice_fy"], condition=Q(status="POSTED"),
                name="uniq_posted_mir_per_vendor_invoice",
            ),
            models.CheckConstraint(condition=Q(invoice_date__lte=F("mir_date")), name="mir_invoice_not_after_receipt"),
            models.CheckConstraint(condition=Q(invoice_total__gte=0) & Q(tcs_amount__gte=0), name="mir_amounts_not_negative"),
            models.CheckConstraint(
                condition=Q(status="POSTED") | (Q(cancelled_at__isnull=False) & ~Q(cancel_reason="")),
                name="mir_cancel_has_reason",
            ),
        ]
        indexes = [models.Index(fields=["plant", "mir_date"]), models.Index(fields=["invoice_key"])]
        ordering = ["-mir_date", "-id"]

    def __str__(self):
        return self.mir_no


class MirLine(models.Model):
    mir = models.ForeignKey(Mir, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    # PROTECT: a PO line with a receipt can never be deleted from under it.
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="mir_lines")
    qty_received = models.DecimalField(max_digits=14, decimal_places=3)
    # Rejected at the quality check. Accepted = received - rejected, and only
    # accepted quantity counts against the PO line.
    qty_rejected = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    rate = models.DecimalField(max_digits=14, decimal_places=4)
    # Snapshots of what the line was compared against when posted.
    po_rate = models.DecimalField(max_digits=14, decimal_places=4)
    open_qty_before = models.DecimalField(max_digits=14, decimal_places=3)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Freight, packing, loading - taxable charges on the invoice line.
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    # Computed on the server at posting (procurement_rules.line_amounts()).
    gross = models.DecimalField(max_digits=16, decimal_places=2)
    taxable = models.DecimalField(max_digits=16, decimal_places=2)
    igst = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=16, decimal_places=2)
    rolls = models.PositiveIntegerField(null=True, blank=True)
    batch_no = models.CharField(max_length=60, blank=True, default="")
    dept_use = models.CharField(max_length=60, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mir", "line_no"], name="uniq_mir_line_no"),
            models.UniqueConstraint(fields=["mir", "po_line"], name="uniq_po_line_once_per_mir"),
            models.CheckConstraint(condition=Q(qty_received__gt=0), name="mir_qty_received_positive"),
            models.CheckConstraint(
                condition=Q(qty_rejected__gte=0) & Q(qty_rejected__lte=F("qty_received")), name="mir_qty_rejected_within_received",
            ),
            models.CheckConstraint(
                condition=Q(rate__gte=0) & Q(discount__gte=0) & Q(other_charges__gte=0) & Q(gst_rate__gte=0) & Q(gst_rate__lte=40),
                name="mir_line_figures_valid",
            ),
        ]
        ordering = ["mir_id", "line_no"]


class MirMismatch(models.Model):
    class Kind(models.TextChoices):
        QTY_SHORT = "QTY_SHORT", "Quantity short"
        QTY_OVER = "QTY_OVER", "Quantity over"
        RATE_HIGH = "RATE_HIGH", "Rate higher than PO"
        RATE_LOW = "RATE_LOW", "Rate lower than PO"
        INVOICE_TOTAL = "INVOICE_TOTAL", "Invoice total differs"
        TAX_TYPE = "TAX_TYPE", "Tax type differs"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"
        # The MIR was cancelled; nothing left to resolve.
        VOID = "VOID", "Void"

    mir = models.ForeignKey(Mir, on_delete=models.CASCADE, related_name="mismatches")
    # Null for the invoice-level kinds (INVOICE_TOTAL, TAX_TYPE).
    mir_line = models.ForeignKey(MirLine, on_delete=models.CASCADE, null=True, blank=True, related_name="mismatches")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    expected = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    actual = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    difference_pct = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    reason = models.ForeignKey(MirReasonCode, on_delete=models.PROTECT, related_name="+")
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    resolved_by_email = models.CharField(max_length=255, blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mir_line", "kind"], condition=Q(mir_line__isnull=False), name="uniq_mismatch_per_line_kind"),
            models.UniqueConstraint(fields=["mir", "kind"], condition=Q(mir_line__isnull=True), name="uniq_mismatch_per_mir_kind"),
            models.CheckConstraint(
                condition=~Q(status="RESOLVED") | (Q(resolved_at__isnull=False) & ~Q(resolution_note="")),
                name="mismatch_resolution_has_note",
            ),
        ]
        indexes = [models.Index(fields=["status", "kind"])]
        ordering = ["id"]
