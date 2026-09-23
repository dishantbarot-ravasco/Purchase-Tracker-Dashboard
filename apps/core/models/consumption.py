"""
apps/core/models/consumption.py - The raw-material consumption ledger: daily rows, excluded events, and observed-day coverage.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models
from .sync import SyncRun


# -- Raw-material consumption ledger (2026-09-21) ---------------------------
# A DERIVED ledger: every row here is recomputed from *RMSnapshot /
# RTPAchhadRMDailyMovement by `manage.py compute_<plant>_consumption`, so the
# whole table can be dropped and rebuilt at any time without data loss. That
# is what makes the material-key choice below a reversible decision rather
# than a permanent one, unlike *RMLot.natural_key (which cannot be rebuilt -
# the source sheets only ever hold today's position).


class MaterialConsumptionDaily(models.Model):
    """How much of one material one plant consumed on one calendar day.

    **Shared table with a `plant` column, NOT three per-plant models** -
    this is a deliberate exception to CLAUDE.md's "Per-plant models, not a
    shared schema" rule, and it is the SyncRun case, not the MIR/Stock case.
    That rule exists because the three plants' MIR/Stock *spreadsheets* have
    genuinely different column layouts, so a shared table would be a pile of
    always-null columns inviting logic that assumes a field exists uniformly.
    Nothing in this table comes from a sheet column: it is a uniform derived
    output (who, what, when, how much) with no plant-specific shape at all,
    and cross-plant rollups ("how much natural rubber did the company burn
    in Q2") are a first-class use case that three tables would make needless
    work. Don't "fix" this into three models.

    **`material_key` is normalized description only** - deliberately NOT
    `*RMLot.natural_key`, and deliberately not a material code:

    - `natural_key` is `<code>|<vendor>#<occurrence>`, shaped that way
      because MIR<->Stock matching needs (material, vendor). Consumption
      does not care who supplied a material, and keying on vendor forks one
      material's history across its suppliers - HRS buys SBR 1502 from three
      vendors, so a vendor-keyed rate answers a question nobody asked. Worse,
      the `#occurrence` suffix is assigned by sheet row order: measured
      2026-09-21, between Sep 2 and Sep 3 fifty-six HRS lots' `opening_stock`
      changed and 54% of those picked up the PREVIOUS lot's opening - a
      reshuffle that splices two materials' histories together. A wrong
      MATCH is visible and reviewable (MatchReview); a spliced consumption
      series just quietly produces a confident wrong number.
    - A material code would be better than a description if all three plants
      had one. They don't: Vapi's Stock sheet carries `hsn_code`, a tariff
      code, and 24 of its 73 distinct codes cover more than one material -
      keying on it would merge unrelated materials outright. HRS's
      `sap_item_code` (99% present) and Achhad's `sap_code` (95%) are real
      material codes and are stored below as `material_code` for reference,
      but the KEY stays uniform across plants so one ledger row means the
      same thing everywhere.

    `quality` is one of apps/services/consumption_engine.py's classification
    constants - `counted` for an ordinary observed day, `spread` for a day
    whose figure is one share of a multi-day interval averaged out (a
    snapshot outage). See that module's docstring. It is rendered, never
    used as a hidden filter - the same convention the Days-Left engine's
    confidence band already follows.
    """

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    material_key = models.CharField(
        max_length=255,
        help_text="parsers.common.normalize_material(description) - see this model's docstring "
                  "for why this is NOT *RMLot.natural_key and not a material code.",
    )
    consumption_date = models.DateField()

    quantity = models.DecimalField(max_digits=16, decimal_places=3)
    quality = models.CharField(
        max_length=20,
        help_text="consumption_engine classification: 'counted' (observed on a single day) or "
                  "'spread' (one day's share of a multi-day interval, i.e. a snapshot gap).",
    )

    # Denormalised for display and grouping, refreshed on every recompute.
    # Kept off the unique key on purpose: a description reworded in the sheet
    # must not fork this material's history, and `category` is itself a
    # backfilled section-divider label at Achhad (see RTPAchhadRMLot).
    material_description = models.CharField(max_length=500, blank=True)
    material_code = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=100, blank=True)
    uom = models.CharField(max_length=20, blank=True)

    lot_count = models.IntegerField(
        default=1,
        help_text="How many *RMLot rows contributed to this day's figure - 1 at Achhad/Vapi for "
                  "almost everything, more at HRS where one material is split across vendor lots.",
    )
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "material_key", "consumption_date"],
                name="uniq_consumption_per_material_per_day",
            )
        ]
        indexes = [
            # The rollup query shape: one plant, one date range, group by
            # material. Every period report (daily/monthly/quarterly/yearly)
            # in consumption_periods.py is this same scan.
            models.Index(fields=["plant", "consumption_date"]),
            models.Index(fields=["plant", "material_key", "consumption_date"]),
        ]
        ordering = ["-consumption_date", "material_key"]

    def __str__(self):
        return f"{self.plant}/{self.material_key} @ {self.consumption_date}: {self.quantity}"


class ConsumptionEvent(models.Model):
    """One interval the engine deliberately did NOT count as consumption,
    kept so an exclusion is visible rather than silent.

    This is the same convention sync_stock.py's `rows_skipped` -> PARTIAL
    SyncRun status already uses: a figure the pipeline drops must surface
    somewhere a human can see it, or the next person to compare the
    dashboard against the sheet has no way to explain the difference.

    `kind` is a consumption_engine classification - `period_roll`,
    `closeout`, `restatement` or `books_disagree`. Note that `restatement`
    and `books_disagree` are recorded here but are NOT excluded from the
    rate: their quantity comes from the issue book, which is correct in
    both cases. They are logged because the balance disagreed with the
    books, which is worth seeing.
    """

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    material_key = models.CharField(max_length=255)
    material_description = models.CharField(max_length=500, blank=True)

    # The lot, not just the material - an exclusion is a property of one
    # sheet row's history, and naming it is the difference between "something
    # was excluded" and a reviewable finding. Deliberately NOT a ForeignKey:
    # this table spans three plants with three separate *RMLot models, and
    # the row must survive its lot being deactivated and later removed.
    lot_ref = models.CharField(
        max_length=255, blank=True,
        help_text="'<model name>#<pk>' of the *RMLot this interval belongs to - a plain string, "
                  "not an FK, because this one table spans three per-plant lot models.",
    )

    kind = models.CharField(max_length=20)
    interval_start = models.DateField()
    interval_end = models.DateField()
    quantity = models.DecimalField(
        max_digits=16, decimal_places=3,
        help_text="The event's real size as the issue book reports it - recorded at full size even "
                  "when excluded, so 'what would this have added' is answerable.",
    )
    balance_quantity = models.DecimalField(
        max_digits=16, decimal_places=3,
        help_text="What the pre-2026-09-21 balance-drawdown method would have counted for this "
                  "same interval. On a restatement the two differ enormously - that gap is the "
                  "whole reason this column exists.",
    )
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "lot_ref", "interval_start", "interval_end", "kind"],
                name="uniq_consumption_event_per_lot_interval",
            )
        ]
        indexes = [
            models.Index(fields=["plant", "interval_end"]),
            models.Index(fields=["plant", "kind"]),
        ]
        ordering = ["-interval_end", "material_key"]

    def __str__(self):
        return f"{self.plant}/{self.material_key} {self.kind} {self.interval_start}..{self.interval_end}"


class ConsumptionCoverage(models.Model):
    """One row per (plant, day) the consumption ledger actually has
    observation for - i.e. a day falling inside some lot's snapshot
    interval, whether or not anything was consumed on it.

    **This is the denominator, and getting it wrong breaks the rate in
    whichever direction the error runs.** MaterialConsumptionDaily only
    holds days with a positive quantity, so it cannot distinguish "the
    plant was observed and this material didn't move" from "nobody looked".
    Dividing a material's total by the full window length silently asserts
    the second is the first: with HRS carrying 17 covered days in a 30-day
    window on 2026-09-21, every rate read 43% low and every days-of-cover
    figure correspondingly high. Dividing by covered days instead makes the
    rate "per day we actually watched", which is what a forward projection
    needs.

    Deliberately a table rather than a per-request query over *RMSnapshot:
    the read path must not go back to the snapshot models (that coupling is
    exactly what the ledger exists to remove), and the coverage set is a
    property of the build, known for free at the moment the intervals are
    classified. It is tiny - at most one row per plant per day, so ~1,100
    rows a year for all three plants - and rebuilt with the ledger.

    Coverage is plant-wide, not per material, and that is the point: a
    material with no row on a covered day genuinely consumed nothing that
    day, and must dilute its own average accordingly.
    """

    plant = models.CharField(max_length=20, choices=SyncRun.Plant.choices)
    coverage_date = models.DateField()
    observed = models.BooleanField(
        default=True,
        help_text="True when at least one lot had a single-day interval ending here (a directly "
                  "observed day). False when every interval covering it spanned several days, so "
                  "the day is only reachable by interpolation - see consumption_engine's `spread`.",
    )
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plant", "coverage_date"], name="uniq_consumption_coverage_per_day"),
        ]
        indexes = [models.Index(fields=["plant", "coverage_date"])]
        ordering = ["-coverage_date"]

    def __str__(self):
        return f"{self.plant} covered {self.coverage_date}"
