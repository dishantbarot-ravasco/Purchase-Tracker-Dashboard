"""
apps/core/models/sync.py - The shared sync job log - the one table every plant's pipeline writes to.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
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
        ADVANCE_LICENSE = "advance_license", "Advance License ledger"
        # Added 2026-09-21 with the consumption ledger. Recorded exactly like
        # MATCH above and for the same reason: compute_<plant>_consumption is
        # a derive-from-DB step with no Drive fetch, so nothing else would
        # ever show that it ran, let alone that it failed - and unlike a
        # sync, its output (MaterialConsumptionDaily) looks perfectly healthy
        # when stale, because yesterday's rows are still sitting there.
        CONSUMPTION = "consumption", "Raw material consumption ledger"

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
