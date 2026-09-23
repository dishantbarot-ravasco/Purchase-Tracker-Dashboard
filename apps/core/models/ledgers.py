"""
apps/core/models/ledgers.py - Company-wide licence ledgers: RoDTEP scrips/usage and Advance Licences/materials.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models
from .auth import PTUser


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


# ── Advance License ledger (added 2026-09-09) ───────────────────────────────
# Company-wide, not per-plant (see SyncRun.Plant.COMPANY's own comment) -
# one shared IEC, one shared Drive file ("Advance License data", maintained
# by hand by the project owner and synced via manage.py sync_advance_license,
# apps/services/parsers/advance_license.py). Scoped to Import Purchases only
# per the project owner's own instruction, same as RoDTEP above.
#
# Two-model shape, mirroring the CSV/xlsx layout the project owner already
# populated by hand: AdvanceLicense holds the license-level fields (CIF
# Value Authorized, FOB Export Target, validity dates, etc. - repeated
# identically across every row of that license in the source file), and
# AdvanceLicenseMaterial holds one row per (license, input material, usage
# instance) - a material already imported against more than once (e.g. drawn
# from multiple BOEs) appears as more than one row for the same material,
# exactly like the source file. No independent identity worth diffing at
# the material level (same reasoning as HRSDomesticPOLineItem's own
# docstring / po_csv.py's sync command) - sync_advance_license.py deletes
# and rebuilds a license's whole materials list on any change, keyed by a
# whole-license content hash, rather than per-material upserts.
class AdvanceLicense(models.Model):
    license_number = models.CharField(max_length=50, unique=True)
    issue_date = models.DateField(null=True, blank=True)
    iec = models.CharField(max_length=50, blank=True)
    cif_value_authorized = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    fob_value_export_target = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    export_product_description = models.TextField(blank=True)
    # "Export Obligation Period End Date" in the source file - the deadline
    # to have exported enough to meet fob_value_export_target.
    export_validity_date = models.DateField(null=True, blank=True)
    import_validity_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, blank=True)

    synced_from_row_hash = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "advance_license"
        ordering = ["license_number"]

    def __str__(self):
        return f"Advance License {self.license_number}"


class AdvanceLicenseMaterial(models.Model):
    license = models.ForeignKey(AdvanceLicense, on_delete=models.CASCADE, related_name="materials")
    material_description = models.CharField(max_length=255, blank=True)
    itchs_code = models.CharField(max_length=20, blank=True)
    qty_authorized = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    cif_value_authorized = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    duty_saved_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # Usage side - blank until this material has actually been drawn against
    # for a real import. A material imported against more than once (e.g.
    # multiple BOEs) gets one AdvanceLicenseMaterial row per usage instance,
    # same material_description repeated - matches the source file exactly.
    boe_number = models.CharField(max_length=50, blank=True)
    boe_date = models.DateField(null=True, blank=True)
    import_po_number = models.CharField(max_length=50, blank=True)
    qty_imported = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    value_imported = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "advance_license_material"
        ordering = ["id"]

    def __str__(self):
        return f"{self.license.license_number}: {self.material_description}"
