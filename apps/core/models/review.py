"""
apps/core/models/review.py - Cross-plant human decisions and reference data: corrections, dismissals, manual MIR pins,
the material category reference, data-quality flags and match reviews.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models
from .sync import SyncRun


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


class ManualMirMatch(models.Model):
    """A human's decision about which MIR entry a Domestic PO line item was
    actually received against, overriding whatever the matcher would pick
    (2026-09-21, project owner: "it would be great if we can edit the MIR
    number too").

    **It names a MIR NUMBER, not a MIR row, and that is deliberate.** One MIR
    document routinely covers several material lines, so `mir_no` is not
    unique in a plant's MIR table - `source_row_ref` is, but that is the
    openpyxl ROW INDEX and shifts the moment a row is inserted above it (the
    same instability that made `stock_identity.lot_natural_key()` necessary
    for stock lots). Pinning a row number would therefore silently re-point
    itself at a different material on the next sync. Naming the number
    instead says exactly what a person actually knows - "this line came in
    under MIR 96/05" - and leaves the existing qty/rate/material scoring to
    choose among that document's own rows, which is a judgement the matcher
    is better at than a human reading a dropdown.

    An empty `mir_no` is a real, distinct instruction: "no MIR entry matches
    this line, leave it unmatched." Without it there would be no way to
    correct a confidently-wrong match except by pointing it at some other
    wrong row.

    `item_ref` addresses the line item. Domestic line items have no stable
    natural key at all (see *POLineItem's own docstring - `item_id` is
    neither required nor unique, and a real PO reuses one code across
    chemically unrelated materials), and a plant's sync DELETES AND RECREATES
    every line item on any change, so a primary key is no good either. It is
    the line's zero-based position within its PO's items ordered by pk -
    which is the master CSV's own row order, stable for as long as that PO's
    lines do not change. `item_description` is stored alongside purely as a
    tripwire: when the description at that position no longer matches, the
    PO's lines HAVE changed, the pin is stale, and it is ignored and
    reported rather than silently applied to whatever material now occupies
    that slot. That is the `source_row_ref` trap again, caught by design
    instead of by incident.

    Shared table with a `plant` column, like FlagDismissal and
    MaterialConsumptionDaily rather than the per-plant MIR/Stock models -
    nothing in it comes from a plant's spreadsheet, so the reason those are
    split (genuinely different column layouts) does not apply.
    """

    class POKind(models.TextChoices):
        DOMESTIC = "domestic", "Domestic purchase order"
        IMPORT = "import", "Import purchase order"

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    # Domestic and Import POs live in separate tables, each with its own
    # `po_number` unique constraint - so one number can exist as both, and
    # (plant, po_number) alone would let a domestic pin silently address an
    # import line or the reverse. Part of the unique key rather than a bare
    # tag for exactly that reason.
    po_kind = models.CharField(max_length=10, choices=POKind.choices, default=POKind.DOMESTIC)
    po_number = models.CharField(max_length=100)
    item_ref = models.CharField(
        max_length=50,
        help_text="Zero-based position of the line item within its PO, as a string. See docstring.",
    )
    item_description = models.CharField(
        max_length=500, blank=True,
        help_text="The line's description when the pin was made - a staleness tripwire, not a key.",
    )

    mir_no = models.CharField(
        max_length=20, blank=True,
        help_text="MIR document number to match this line against. Blank means 'leave this line unmatched'.",
    )
    reason = models.TextField(blank=True)

    created_by = models.ForeignKey("PTUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="manual_mir_matches")
    created_by_email = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "po_kind", "po_number", "item_ref"], name="uniq_manual_mir_match")
        ]
        indexes = [
            models.Index(fields=["plant", "po_kind", "po_number"]),
            models.Index(fields=["plant", "mir_no"]),
        ]

    def __str__(self):
        target = self.mir_no or "(unmatched)"
        return f"{self.plant}/{self.po_kind}/{self.po_number}#{self.item_ref} -> {target}"


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
