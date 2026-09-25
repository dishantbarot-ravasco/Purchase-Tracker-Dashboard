"""
apps/core/models/vapi.py - RTP-Vapi: Domestic and Import POs, MIR, RM Stock, and their matches.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


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
        help_text="Sourced from the MIR sheet's 'PURCHASE ORDER' column (added 2026-09-11 at the "
                   "project owner's request specifically for better PO<->MIR matching - column K, "
                   "inserted after 'STATE', shifting every later column right by one - see "
                   "vapi_mir.py's module docstring). Before this the sheet had no populated PO-number "
                   "column at all (the separate, still-present 'SAP P.O' field - see sap_po_number - "
                   "was 100% blank on every row checked). Same reliability caveat as "
                   "HRSMIREntry.po_number_raw: a tier-1 shortcut only, never a sole join key.",
    )
    sap_po_number = models.CharField(
        max_length=100, blank=True,
        help_text="MIR's own 'SAP P.O' field (column D) - empty on every row confirmed 2026-09-10, "
                   "kept for forward compatibility. Not the same column as po_number_raw (which reads "
                   "the newer, actually-populated 'PURCHASE ORDER' column added 2026-09-11) - don't "
                   "conflate the two.",
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
    sub_category = models.CharField(
        max_length=100, blank=True,
        help_text="No longer sourced from the Stock sheet. Column E held 'Sub Category' until "
                  "2026-09-12, when the Vapi plant renamed it to 'Batch No.' and repurposed its "
                  "contents - see batch_no. Existing values are the last real sub-categories the "
                  "sheet carried and are left in place (this field is audit/Edit-Everywhere only, "
                  "never displayed - the materials API shows MaterialCategoryReference's canonical "
                  "subcategory instead), but nothing writes to it now.",
    )
    batch_no = models.CharField(
        max_length=100, blank=True,
        help_text="Stock sheet's 'Batch No.' column (E), which replaced 'Sub Category' in place on "
                  "2026-09-12 - same column position, new meaning. Real values are vendor/lot batch "
                  "codes ('HRS-25', '230626GIFL2590') or a literal '-' placeholder; 166 of 168 live "
                  "rows are populated. Vapi only - HRS's Stock sheet still has a genuine "
                  "'Sub Category' at E, and Achhad's sheet has neither column.",
    )
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
    """Same shape as HRSPOMirMatch - see that class's docstring. The plant
    head started filling in a 'PURCHASE ORDER' MIR column 2026-09-11 (see
    parsers/vapi_mir.py); measured 2026-09-19 it names an order the master
    CSV actually holds on 29.0% of MIR rows, so the PO_NUMBER tier now fires
    for real. `sap_po_number`/RTPVapiMIREntry's original SAP P.O. column is
    the one that stayed 100% blank and remains unused for matching."""

    class Tier(models.TextChoices):
        PO_NUMBER = "po_number", "PO number match (exact)"
        MATERIAL = "material", "Material description match"
        WEIGHTED = "weighted", "Vendor-gated weighted match (legacy, pre-2026-09-07)"

    po_line_item = models.OneToOneField(RTPVapiDomesticPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="po_matches")
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(RTPVapiMIREntry, blank=True, related_name="po_group_matches")
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
    # Identification 2-of-3 (2026-09-19, Vapi - see matching_core.py's
    # _MatchConfig.identification_two_of_three). Vapi joined Achhad/HRS the
    # day its 'PURCHASE ORDER' column's coverage was actually measured (29.0%
    # usable+recognized of 1,489 MIR rows) - see matching_vapi.py's own
    # comment for the full A/B/C measurement (+11 matches, 0 lost, 0
    # re-pointed). Same column, same reasoning as HRSPOMirMatch.vendor_matched:
    # defaults True so every pre-existing row, and every plant still on the
    # vendor-mandatory rule, reads correctly without a backfill.
    vendor_matched = models.BooleanField(default=True)
    # Set by a human, not the matcher (2026-09-21) - this row exists because
    # someone used "Change MIR match" on the PO modal to name the MIR number
    # themselves, and the matcher then picked the best row within that
    # document. See ManualMirMatch. Recomputed on every run_full_match()
    # rather than preserved like dismissed_* - it is derived from whether a
    # pin currently exists, so removing the pin must clear the badge.
    manually_pinned = models.BooleanField(default=False)
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
            models.UniqueConstraint(fields=["mir_entry", "stock_lot"], name="uniq_vapi_mir_stock_pair")
        ]

    def __str__(self):
        return f"{self.mir_entry} <-> {self.stock_lot}"


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
        # MIR's invoice_no on an import receipt is the Bill of Entry number
        # (2026-09-25) - an exact, per-shipment join key. See
        # matching_core's BOE settlement in run_full_match().
        BOE_NUMBER = "boe_number", "Bill of Entry number match (exact)"

    po_line_item = models.OneToOneField(RTPVapiImportPOLineItem, on_delete=models.CASCADE, related_name="mir_match")
    mir_entry = models.ForeignKey(RTPVapiMIREntry, on_delete=models.CASCADE, related_name="import_po_matches")
    # EVERY MIR row this match counted, when it counted more than one
    # (2026-09-24). `mir_entry` is one row - the primary - but a PO filled
    # by several deliveries is compared against their SUM, and until this
    # field existed the other rows were counted and then thrown away: the
    # PO modal showed one MIR number beside a quantity built from four, and
    # every reader asking "is this MIR row matched?" said no for the rest.
    # Empty for an ordinary one-row match; readers fall back to mir_entry.
    # Rebuilt from scratch by every run_full_match(). See CLAUDE.md's
    # "One PO, many receipts".
    group_entries = models.ManyToManyField(RTPVapiMIREntry, blank=True, related_name="import_po_group_matches")
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
    # See RTPVapiPOMirMatch.vendor_matched (2026-09-19). Imports needs the
    # column for the same reason domestic does - _MatchConfig is one config
    # per plant, so match_import_po_mir_line_item() runs the same 2-of-3 rule
    # and _vendor_matched_field() hands this keyword to both models or
    # neither.
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
