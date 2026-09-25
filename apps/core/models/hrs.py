"""
apps/core/models/hrs.py - HRS (Hindustan Rubbers, Silvassa): Domestic and Import POs, MIR, RM Stock, and their matches.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


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

    # See RTPAchhadDomesticPurchaseOrder.is_active for the full rationale -
    # purchase orders were the only entity in this pipeline that never got
    # deactivated, so an upstream RENAME left the old spelling behind forever
    # as a second order competing for the same MIR rows.
    is_active = models.BooleanField(default=True)

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
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(HRSMIREntry, blank=True, related_name="po_group_matches")
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
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Identification/Financial-Check redesign (2026-09-07, project owner
    # spec - see apps/services/matching_core.py's module docstring for the
    # full algorithm): identification is now "vendor mandatory plus one of
    # material/PO number", not a blended score - these two record which of
    # the latter two actually fired for the winning candidate (vendor is
    # implied True on every row here - no longer so as of 2026-09-18, see
    # vendor_matched below).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    # Identification 2-of-3 (2026-09-18, Achhad and HRS - see
    # matching_core.py's _MatchConfig.identification_two_of_three). Vendor
    # used to be a precondition for a match existing at all, so there was
    # nothing to record; now that a PO number plus material can identify a
    # row whose party name disagrees, False here is the signal that they
    # disagreed. That is a real data-entry error somewhere - a mistyped
    # party name, a placeholder left in the column, or the same supplier
    # written two ways - and surfacing it is the point: the match is still
    # made (the PO number and the money both say it belongs here), but
    # someone should fix the name at source. Defaults True so every
    # pre-existing row, and every plant still on the vendor-mandatory rule,
    # reads correctly without a backfill. No HRS row produces False on
    # today's MIR file (matching.py's own comment has the measurement) - the
    # column exists because the rule can produce one, not because the
    # current file does.
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
            models.UniqueConstraint(fields=["mir_entry", "stock_lot"], name="uniq_hrs_mir_stock_pair")
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
        # MIR's invoice_no on an import receipt is the Bill of Entry number
        # (2026-09-25) - an exact, per-shipment join key. See
        # matching_core's BOE settlement in run_full_match().
        BOE_NUMBER = "boe_number", "Bill of Entry number match (exact)"

    po_line_item = models.OneToOneField(HRSImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(HRSMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(HRSMIREntry, blank=True, related_name="import_po_group_matches")
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
    rate_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    value_diff_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # See HRSPOMirMatch's identically-named fields for the full
    # identification/financial-check redesign rationale (2026-09, imports).
    material_matched = models.BooleanField(default=False)
    po_number_matched = models.BooleanField(default=False)
    # See HRSPOMirMatch.vendor_matched (2026-09-18). Imports needs the column
    # for the same reason domestic does - _MatchConfig is one config per
    # plant, so match_import_po_mir_line_item() runs the same 2-of-3 rule and
    # _vendor_matched_field() hands this keyword to both models or neither.
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
