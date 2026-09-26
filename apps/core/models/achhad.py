"""
apps/core/models/achhad.py - RTP-Achhad: Domestic and Import POs, MIR, RM Stock (+ its dated daily movement matrix), and matches.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


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

    # Whether the master CSV still lists this order (2026-09-18).
    #
    # Purchase orders were the ONLY entity in this pipeline without this flag.
    # MIR entries and stock lots have always had one, and their syncs flip it
    # off for rows the source no longer contains; the PO sync upserted on
    # po_number as a natural key and never removed anything. So RENAMING a PO
    # upstream - which happens every time an annotation is added or cleaned
    # off, e.g. "3000001104 (Changed Purchase Order)" back to "3000001104" -
    # left the old spelling behind forever as a second order carrying a
    # duplicate set of line items. Measured 2026-09-12: 6 such ghosts for
    # HRS, 5 for Achhad, 11 for Vapi, every one a rename rather than a real
    # deletion. The project owner then cleaned the annotation off every PO
    # number at once, which turned each remaining annotated order into
    # another ghost - reported as "I removed the suffix and it still shows on
    # the dashboard", which is exactly what it looked like from outside.
    #
    # Deactivated rather than deleted, deliberately and for the same reason
    # sync_utils.orphaned_orders() refused to delete: an order withdrawn
    # upstream and one merely renamed are indistinguishable from here, and
    # deleting is irreversible. Everything downstream (matching, the API, the
    # KPIs) filters on is_active, so a deactivated order stops competing for
    # MIR rows immediately, and a re-appearing one reactivates on the next
    # sync with its history intact.
    is_active = models.BooleanField(default=True)
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
    material grid would be almost entirely zeros.

    Since 2026-09-21 these rows feed
    `apps/services/consumption_ledger.py`, which treats them as the
    AUTHORITATIVE per-day source for every date they cover and falls back
    to snapshot intervals only after the matrix's last date - see
    `_dated_movements()` for why (their dates are the real issue dates; a
    snapshot lags them by a day). They no longer reconstruct an implied
    stock-level series the way `stock_consumption.py` did.

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
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(RTPAchhadMIREntry, blank=True, related_name="po_group_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Over vs under delivery (2026-09-18, project owner). qty_diff_pct is an
    # absolute percentage, so until now "received more than ordered" and
    # "received less" were the same flag - a genuine over-receipt read
    # identically to a blanket order part-way through its schedule. True =
    # more received than ordered, False = less, NULL = no quantity
    # comparison was possible (UOM mismatch, or a missing qty on either
    # side), which is a different answer from False. Tolerance is unchanged
    # and still zero - this records the direction of a mismatch, it does not
    # decide whether one exists. See matching_core._diffs_and_flag().
    qty_over_delivered = models.BooleanField(null=True, blank=True)
    # Over the ordered quantity but inside the weighbridge allowance for
    # material bought by the truckload (steam coal, HM plastic, HDPE,
    # 2026-09-26) - counted as matched, shown with a tolerance note. See
    # apps/services/qty_tolerance.py.
    qty_within_tolerance = models.BooleanField(default=False)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09-07).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    # Identification 2-of-3 (2026-09-18, Achhad and HRS - see
    # matching_core.py's _MatchConfig.identification_two_of_three). Vendor
    # used to be a precondition for a match existing at all, so there was nothing to
    # record; now that a PO number plus material can identify a row whose
    # party name disagrees, False here is the signal that they disagreed.
    # That is a real data-entry error somewhere - a mistyped party name, a
    # placeholder left in the column, or the same supplier written two ways -
    # and surfacing it is the point: the match is still made (the PO number
    # and the money both say it belongs here), but someone should fix the
    # name at source. Defaults True so every pre-existing row, and every
    # plant still on the vendor-mandatory rule, reads correctly without a
    # backfill.
    vendor_matched = models.BooleanField(default=True)
    # Set by a human, not the matcher (2026-09-21) - this row exists because
    # someone used "Change MIR match" on the PO modal to name the MIR number
    # themselves, and the matcher then picked the best row within that
    # document. See ManualMirMatch. Recomputed on every run_full_match()
    # rather than preserved like dismissed_* - it is derived from whether a
    # pin currently exists, so removing the pin must clear the badge.
    manually_pinned = models.BooleanField(default=False)
    # Set when a "Keep both" pin shares this line's MIR row with another
    # line: each counts the row's qty and value times its share of the
    # holders' ordered quantity. NULL for an ordinary match. Same meaning as
    # the import match's receipt_share; read by _domestic_base._counted_mirs().
    receipt_share = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
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
    # Found missing during a full-codebase audit (2026-09-10): unlike
    # *POMirMatch (which always ran qty/rate through match_core.py's
    # _uom_adjust() before comparing), match_mir_entry_stock() compared
    # MIR's qty/rate directly against the Stock lot's own qty/rate with no
    # unit conversion at all - a material logged in MIR as MT against a
    # Stock lot recorded in KG would report a ~1000x "rate mismatch" that's
    # actually just a unit-mismatch artifact, not a real discrepancy. Same
    # meaning as *POMirMatch's own uom_mismatch: True when qty/rate's units
    # belong to different families - qty_diff_pct/rate_diff_pct are None in
    # that case (not a nonsense percentage), this flag is the real signal.
    uom_mismatch = models.BooleanField(default=False)

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

    # Whether the master CSV still lists this order (2026-09-18).
    #
    # Purchase orders were the ONLY entity in this pipeline without this flag.
    # MIR entries and stock lots have always had one, and their syncs flip it
    # off for rows the source no longer contains; the PO sync upserted on
    # po_number as a natural key and never removed anything. So RENAMING a PO
    # upstream - which happens every time an annotation is added or cleaned
    # off, e.g. "3000001104 (Changed Purchase Order)" back to "3000001104" -
    # left the old spelling behind forever as a second order carrying a
    # duplicate set of line items. Measured 2026-09-12: 6 such ghosts for
    # HRS, 5 for Achhad, 11 for Vapi, every one a rename rather than a real
    # deletion. The project owner then cleaned the annotation off every PO
    # number at once, which turned each remaining annotated order into
    # another ghost - reported as "I removed the suffix and it still shows on
    # the dashboard", which is exactly what it looked like from outside.
    #
    # Deactivated rather than deleted, deliberately and for the same reason
    # sync_utils.orphaned_orders() refused to delete: an order withdrawn
    # upstream and one merely renamed are indistinguishable from here, and
    # deleting is irreversible. Everything downstream (matching, the API, the
    # KPIs) filters on is_active, so a deactivated order stops competing for
    # MIR rows immediately, and a re-appearing one reactivates on the next
    # sync with its history intact.
    is_active = models.BooleanField(default=True)
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
        # MIR's invoice_no on an import receipt is the Bill of Entry number
        # (2026-09-25) - an exact, per-shipment join key. See
        # matching_core's BOE settlement in run_full_match().
        BOE_NUMBER = "boe_number", "Bill of Entry number match (exact)"

    po_line_item = models.OneToOneField(RTPAchhadImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPAchhadMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(RTPAchhadMIREntry, blank=True, related_name="import_po_group_matches")
    tier = models.CharField(max_length=20, choices=Tier.choices)
    match_score = models.DecimalField(max_digits=5, decimal_places=4)

    qty_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Over vs under delivery (2026-09-18, project owner). qty_diff_pct is an
    # absolute percentage, so until now "received more than ordered" and
    # "received less" were the same flag - a genuine over-receipt read
    # identically to a blanket order part-way through its schedule. True =
    # more received than ordered, False = less, NULL = no quantity
    # comparison was possible (UOM mismatch, or a missing qty on either
    # side), which is a different answer from False. Tolerance is unchanged
    # and still zero - this records the direction of a mismatch, it does not
    # decide whether one exists. See matching_core._diffs_and_flag().
    qty_over_delivered = models.BooleanField(null=True, blank=True)
    # Over the ordered quantity but inside the weighbridge allowance for
    # material bought by the truckload (steam coal, HM plastic, HDPE,
    # 2026-09-26) - counted as matched, shown with a tolerance note. See
    # apps/services/qty_tolerance.py.
    qty_within_tolerance = models.BooleanField(default=False)
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09, imports).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    # Identification 2-of-3 (2026-09-18, Achhad and HRS - see
    # matching_core.py's _MatchConfig.identification_two_of_three). Vendor
    # used to be a precondition for a match existing at all, so there was nothing to
    # record; now that a PO number plus material can identify a row whose
    # party name disagrees, False here is the signal that they disagreed.
    # That is a real data-entry error somewhere - a mistyped party name, a
    # placeholder left in the column, or the same supplier written two ways -
    # and surfacing it is the point: the match is still made (the PO number
    # and the money both say it belongs here), but someone should fix the
    # name at source. Defaults True so every pre-existing row, and every
    # plant still on the vendor-mandatory rule, reads correctly without a
    # backfill.
    vendor_matched = models.BooleanField(default=True)
    # Set by a human, not the matcher (2026-09-21) - see ManualMirMatch and
    # HRSPOMirMatch.manually_pinned. Recomputed on every run_full_match()
    # rather than preserved like dismissed_*.
    manually_pinned = models.BooleanField(default=False)
    # This line's share of a receipt that covers several lines of one Bill
    # of Entry (2026-09-25) - e.g. a 2,000 KG and a 14,000 KG line booked in
    # MIR as one 16,000 KG row. Each line counts the row's quantity and value
    # times its share (its BOE qty over the BOE's total). NULL for an
    # ordinary match, which counts its rows in full. See matching_core's BOE
    # settlement and _domestic_base._counted_mirs().
    receipt_share = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Exchange-rate difference (2026-09-25). The exchange rate MIR's receipt
    # implies (the CSV rate scaled by MIR's final rate over the landed rate),
    # set only on a cleared line with a rate gap. When it is a customs-style
    # rate (on the 0.05 grid every customs-notified rate sits on) different
    # from the CSV's own, the gap is an exchange-rate difference, not a
    # price one: exchange_rate_mismatched is True and rate_mismatched False.
    # See matching_core._exchange_rate_explains().
    mir_exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    exchange_rate_mismatched = models.BooleanField(default=False)
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
