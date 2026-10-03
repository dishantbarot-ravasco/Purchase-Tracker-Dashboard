"""
RM stock entered in this app (2026-09-29, project owner: "the Raw Material
entry like we have for MIR entry ... directly connecting with MIR entry").

Normalized like procurement.py - one table per entity, plant as a foreign
key - and built on the same two rules as MIR entry: a figure that can be
worked out is never stored, and a posted document is never edited (a wrong
one is cancelled and entered again).

  StockReasonCode   the fixed list a return or an adjustment picks its
                    reason from (seeded by migration 0082; never deleted).
  StockSetting      per plant and material: whether the store keeps it at
                    all, and its minimum stock level.
  StockSequence     the per-plant, per-kind, per-financial-year counter.
  StockLot          one receipt into the store: a posted MIR line (2026-09-30,
                    project owner: "if a material has not been entered in the
                    MIR the RM can't issue it"). Its quantity is NOT stored - it
                    holds the MIR line's accepted quantity (received less
                    rejected, in the MIR line's own unit) for as long as the
                    MIR is posted, so a cancelled MIR or a rejection found
                    later changes stock at once, with nothing to keep in step.
                    Source ADJUSTMENT lots were opening balances added by hand
                    before that rule; none are made any more, the old ones
                    still count.
  StockVoucher,     an issue to production, a return to the store against an
  StockVoucherLine  issue, or a stock difference (a count or a write-off) -
                    numbered, dated, signed. Each line names the MIR receipt
                    (`lot`) it acts on: the storekeeper picks the MIR and the
                    rest comes from it.
  StockAllocation   how much a voucher line took from (or put back into) a
                    lot. A line made since 2026-09-30 has one, on its own lot.

A lot's balance = what it received - what posted vouchers took from it +
what posted vouchers put back (apps/services/stock_service.py). Only POSTED
vouchers count, so a cancelled one drops out at once, like a cancelled MIR.
"""

from django.db import models
from django.db.models import Q


class StockReasonCode(models.Model):
    class Kind(models.TextChoices):
        RETURN = "RETURN", "Returned to store"
        ADJUST_IN = "ADJUST_IN", "Stock added"
        ADJUST_OUT = "ADJUST_OUT", "Stock written off"

    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    label = models.CharField(max_length=120)
    note_required = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "sort_order", "id"]

    def __str__(self):
        return self.code


class StockLocation(models.Model):
    """Where in a plant's store a receipt physically sits - a godown, a shed,
    or the shared warehouse the HRS RM sheet tags "RTP-1" (owner, 2026-10-03:
    the RM file's location column). Named by the storekeepers themselves, one
    list per plant; never deleted, so a lot always keeps its place's name."""

    plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, related_name="stock_locations")
    name = models.CharField(max_length=60)
    # stock_service.location_key(): upper-cased, spaces collapsed - "rtp 1"
    # and "RTP  1" are one place.
    name_key = models.CharField(max_length=60)
    created_by_email = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "name_key"], name="uniq_stock_location_per_plant"),
            models.CheckConstraint(condition=~Q(name_key=""), name="stock_location_named"),
        ]
        ordering = ["plant_id", "name"]

    def __str__(self):
        return f"{self.plant_id}:{self.name}"


class PlantStockSource(models.Model):
    """Where a plant's Inventory / On Order / Stock & Orders tabs read from
    (owner, 2026-10-03): the Drive RM sheet and MIR register until the plant
    has moved in-app, then the app's own RM store and MIRs. Switched by an
    admin at any moment (app_stock_source.set_source()); a plant with no row
    reads the Drive sheets. The two sources are shown one at a time, never
    added together."""

    class Source(models.TextChoices):
        DRIVE = "drive", "Drive sheets"
        APP = "app", "In-app MIR and RM store"

    plant = models.OneToOneField("core.Plant", on_delete=models.PROTECT, related_name="stock_source")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.DRIVE)
    # The same switch for the Import Purchases page (2026-10-03): the import
    # CSV, or the app's import POs, shipments and import MIRs.
    import_source = models.CharField(max_length=10, choices=Source.choices, default=Source.DRIVE)
    updated_by_email = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.plant_id}:{self.source}"


class StockSetting(models.Model):
    """What a plant's store does with a material. A material with no row is
    stocked, with no minimum level. `is_stocked` False means receipts of it
    go straight to use (conveyor fabric, spares): its MIR lots are recorded
    but hold no stock. The flag is read when a MIR is posted and kept on the
    lot, so changing it never rewrites a receipt already made."""

    plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, related_name="+")
    material = models.ForeignKey("core.Material", on_delete=models.PROTECT, related_name="stock_settings")
    is_stocked = models.BooleanField(default=True)
    # In the material's stock unit (min_level_uom); null = none set.
    min_level = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    min_level_uom = models.CharField(max_length=20, blank=True, default="")
    updated_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by_email = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "material"], name="uniq_stock_setting"),
            models.CheckConstraint(condition=Q(min_level__isnull=True) | Q(min_level__gte=0), name="stock_min_level_not_negative"),
        ]


class StockSequence(models.Model):
    plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10)
    fy = models.CharField(max_length=7)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plant", "kind", "fy"], name="uniq_stock_sequence")]


class StockVoucher(models.Model):
    class Kind(models.TextChoices):
        ISSUE = "ISSUE", "Issue"
        RETURN = "RETURN", "Return to store"
        ADJUST = "ADJUST", "Stock difference"

    class Status(models.TextChoices):
        # A stock difference entered by an editor waits for an admin's
        # approval and moves no stock until then.
        PENDING = "PENDING", "Waiting for approval"
        POSTED = "POSTED", "Posted"
        REJECTED = "REJECTED", "Not approved"
        CANCELLED = "CANCELLED", "Cancelled"

    plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, related_name="stock_vouchers")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    fy = models.CharField(max_length=7)
    seq = models.PositiveIntegerField()
    voucher_no = models.CharField(max_length=40, unique=True)
    voucher_date = models.DateField()
    # Issue: the department the material went to, and the person who took it
    # (both optional - the store's own sheet never recorded them).
    department = models.CharField(max_length=60, blank=True, default="")
    issued_to = models.CharField(max_length=120, blank=True, default="")
    # Issue: a production order or batch, when the plant uses one.
    reference = models.CharField(max_length=60, blank=True, default="")
    # Return: the issue it returns material from.
    return_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="returns")
    remarks = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED)
    created_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, related_name="+")
    created_by_email = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    # Adjustments: who approved (or turned down) it, and what they said.
    decided_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_by_email = models.CharField(max_length=255, blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    cancelled_by = models.ForeignKey("core.PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_by_email = models.CharField(max_length=255, blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "kind", "fy", "seq"], name="uniq_stock_voucher_seq"),
            models.CheckConstraint(condition=Q(return_of__isnull=True) | Q(kind="RETURN"), name="stock_return_of_only_on_return"),
            models.CheckConstraint(condition=Q(kind="RETURN", return_of__isnull=False) | ~Q(kind="RETURN"), name="stock_return_names_its_issue"),
            models.CheckConstraint(condition=Q(status="PENDING") | ~Q(kind="ADJUST") | Q(decided_at__isnull=False),
                                   name="stock_adjustment_decided"),
            models.CheckConstraint(condition=~Q(status="CANCELLED") | (Q(cancelled_at__isnull=False) & ~Q(cancel_reason="")),
                                   name="stock_cancel_has_reason"),
            models.CheckConstraint(condition=Q(status="PENDING") | Q(status="POSTED") | Q(status="CANCELLED") | Q(kind="ADJUST"),
                                   name="stock_only_adjustments_rejected"),
        ]
        indexes = [models.Index(fields=["plant", "voucher_date"]), models.Index(fields=["status", "kind"])]
        ordering = ["-voucher_date", "-id"]

    def __str__(self):
        return self.voucher_no


class StockVoucherLine(models.Model):
    voucher = models.ForeignKey(StockVoucher, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    # The MIR receipt this line acts on. Null only on lines entered before
    # 2026-09-30, when an issue drew several lots oldest first.
    lot = models.ForeignKey("core.StockLot", on_delete=models.PROTECT, null=True, blank=True, related_name="voucher_lines")
    material = models.ForeignKey("core.Material", on_delete=models.PROTECT, related_name="+")
    # The unit - the MIR line's own (KG only for a lot migration 0084 could not convert back).
    uom = models.CharField(max_length=20, blank=True, default="")
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    # -1 takes stock out (issue, write-off, count short), +1 puts it back
    # (return, count found more).
    direction = models.SmallIntegerField()
    reason = models.ForeignKey(StockReasonCode, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    note = models.TextField(blank=True, default="")
    # An old opening-balance addition's value per stock unit (the lot it
    # created is valued at it). New lines leave it empty.
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    # A physical count: what was counted and what the books said that day;
    # qty is the difference between them.
    counted_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    book_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    # A return: the issue line the material comes back from.
    return_of_line = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="returned_by")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["voucher", "line_no"], name="uniq_stock_voucher_line_no"),
            models.CheckConstraint(condition=Q(qty__gt=0), name="stock_line_qty_positive"),
            models.CheckConstraint(condition=Q(direction=1) | Q(direction=-1), name="stock_line_direction"),
            models.CheckConstraint(condition=Q(rate__isnull=True) | Q(rate__gte=0), name="stock_line_rate_not_negative"),
        ]
        ordering = ["voucher_id", "line_no"]


class StockLot(models.Model):
    class Source(models.TextChoices):
        MIR = "MIR", "MIR receipt"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    # Where the goods sit: the plant that RECEIVED them (the MIR's plant).
    plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, related_name="stock_lots")
    material = models.ForeignKey("core.Material", on_delete=models.PROTECT, related_name="stock_lots")
    uom = models.CharField(max_length=20, blank=True, default="")
    source = models.CharField(max_length=12, choices=Source.choices)
    mir_line = models.OneToOneField("core.MirLine", on_delete=models.PROTECT, null=True, blank=True, related_name="stock_lot")
    voucher_line = models.OneToOneField(StockVoucherLine, on_delete=models.PROTECT, null=True, blank=True, related_name="created_lot")
    received_date = models.DateField()
    vendor = models.ForeignKey("core.Vendor", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # The plant whose PO paid for it, when that is not where it sits.
    bill_to_plant = models.ForeignKey("core.Plant", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # Stock units per unit of the MIR line: 1 - a lot keeps the MIR line's unit.
    # 1000 / 0.001 only on a lot from before migration 0084 that stayed in KG.
    factor = models.DecimalField(max_digits=12, decimal_places=6, default=1)
    # Value of one stock unit, before GST; null when the receipt had no value.
    rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=10, default="INR")
    # StockSetting.is_stocked when the lot was made. A lot not stocked went
    # straight to use and holds nothing.
    stocked = models.BooleanField(default=True)
    batch_no = models.CharField(max_length=60, blank=True, default="")
    # Where in the plant's store it sits (StockLocation, at this lot's plant);
    # null until the storekeeper says. Set and changed only by
    # stock_service.set_location(), which records who and when.
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, null=True, blank=True, related_name="lots")
    location_set_by_email = models.CharField(max_length=255, blank=True, default="")
    location_set_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(source="MIR", mir_line__isnull=False, voucher_line__isnull=True)
                           | Q(source="ADJUSTMENT", mir_line__isnull=True, voucher_line__isnull=False)),
                name="stock_lot_has_one_source",
            ),
            models.CheckConstraint(condition=Q(factor__gt=0), name="stock_lot_factor_positive"),
        ]
        indexes = [models.Index(fields=["plant", "material", "uom"]), models.Index(fields=["received_date"])]
        ordering = ["received_date", "id"]


class StockAllocation(models.Model):
    voucher_line = models.ForeignKey(StockVoucherLine, on_delete=models.CASCADE, related_name="allocations")
    lot = models.ForeignKey(StockLot, on_delete=models.PROTECT, related_name="allocations")
    qty = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="stock_allocation_qty_positive"),
            models.UniqueConstraint(fields=["voucher_line", "lot"], name="uniq_stock_allocation"),
        ]
