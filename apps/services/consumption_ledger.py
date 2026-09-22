"""
apps/services/consumption_ledger.py - the DB layer over
apps/services/consumption_engine.py: reads one plant's stock history, runs
the pure engine over it, and materialises the result into
MaterialConsumptionDaily + ConsumptionEvent.

Same split as data_quality.py over arithmetic_checks.py - the arithmetic is
dependency-free and unit-tested as plain Python, this module is the only
part that touches Django. Called by `manage.py compute_<plant>_consumption`
(one thin command per plant, mirroring the three sync_*_stock / match_*
commands), which is wired into sync_trigger's per-plant pipeline right
after the plant's own match step.

---------------------------------------------------------------------------
Why a materialised ledger instead of computing on request
---------------------------------------------------------------------------

The Days-Left engine it replaces recomputed everything inside the materials
API call, and apps/services/consumption_report.py kept a second, separate
copy of the same query because apps/services must not import from apps/api.
Those two copies had already drifted apart by the time this was written:
_domestic_base.py's `_daily_movement_points()` fed *cumulative* received
into a function reading it as per-day, while consumption_report.py's
`_consumption_stats_by_lot()` built the same points with *per-day* received.
Two implementations of one number is a bug waiting to happen, and this one
had already happened.

Materialising also buys three things a per-request computation can't:

- **Auditability.** A figure someone disputes can be traced to the rows it
  came from, months later. A recomputed number can only be re-derived from
  whatever the data looks like *now*.
- **Rollups.** Monthly/quarterly/yearly totals become a SUM over a date
  range (see consumption_periods.py) instead of replaying every snapshot.
- **Survivability.** The Stock xlsx only ever holds today's position, so
  *RMSnapshot is already the only record that a given day happened. The
  ledger is derived from it and fully rebuildable, but it means a
  correction to the engine can be re-applied to history rather than only
  affecting tomorrow.

The whole table is safe to drop and rebuild - `rebuild_plant_consumption()`
is idempotent over the date range it is given.
"""
from __future__ import annotations

import datetime
import itertools
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    ConsumptionCoverage,
    ConsumptionEvent,
    HRSRMLot,
    HRSRMSnapshot,
    MaterialConsumptionDaily,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
    RTPVapiRMLot,
    RTPVapiRMSnapshot,
    SyncRun,
)
from apps.services.consumption_engine import (
    COUNTED,
    SPREAD,
    ConsumptionPoint,
    daily_from_points,
    intervals_for_lot,
)
from apps.services.parsers.common import normalize_material

log = logging.getLogger(__name__)

# How far before a bounded rebuild's `since` to read snapshots, purely to
# give the first in-range interval a predecessor to difference against.
# Generous on purpose - it costs one extra row per lot per day read and
# nothing is written from it, whereas too short a reach silently drops the
# first day of every scheduled rebuild.
_ANCHOR_LOOKBACK_DAYS = 30


class PlantLedgerConfig:
    """Per-plant wiring for the ledger. A lighter sibling of
    _domestic_base.py's `_PlantConfig` and consumption_report.py's own
    `_PLANTS` dict - deliberately its own copy rather than an import,
    because apps/services must not import from apps/api (CLAUDE.md's
    "Layering"), and because the fields this needs are a different set.

    `code_field` differs per plant and is stored for reference only - it is
    NEVER part of `material_key`; see MaterialConsumptionDaily's docstring
    for why (Vapi's is an HSN tariff code covering multiple materials).
    `uom_field` is None at Achhad, whose Stock sheet has no UOM column -
    the same `getattr(..., None)` no-branching pattern the Days-Left engine
    already uses for msl/no_of_days/sub_category.
    """

    def __init__(self, *, key, plant, lot_model, snapshot_model, code_field, uom_field=None,
                 daily_movement_model=None):
        self.key = key
        self.plant = plant
        self.lot_model = lot_model
        self.snapshot_model = snapshot_model
        self.code_field = code_field
        self.uom_field = uom_field
        self.daily_movement_model = daily_movement_model


PLANT_CONFIGS = {
    "hrs": PlantLedgerConfig(
        key="hrs", plant=SyncRun.Plant.HRS,
        lot_model=HRSRMLot, snapshot_model=HRSRMSnapshot,
        code_field="sap_item_code", uom_field="uom",
    ),
    "achhad": PlantLedgerConfig(
        key="achhad", plant=SyncRun.Plant.RTP_ACHHAD,
        lot_model=RTPAchhadRMLot, snapshot_model=RTPAchhadRMSnapshot,
        code_field="sap_code", uom_field=None,
        daily_movement_model=RTPAchhadRMDailyMovement,
    ),
    "vapi": PlantLedgerConfig(
        key="vapi", plant=SyncRun.Plant.RTP_VAPI,
        lot_model=RTPVapiRMLot, snapshot_model=RTPVapiRMSnapshot,
        code_field="hsn_code", uom_field="uom",
    ),
}


def _lot_meta(cfg: PlantLedgerConfig) -> dict[int, dict]:
    """Every lot's display/grouping fields in one query, keyed by lot id.

    **Deliberately not filtered to `is_active=True`.** A lot that has since
    been deactivated still genuinely consumed material on the days it was
    alive, and its *RMSnapshot rows survive deactivation by design (see
    HRSRMLot.is_active's help_text). The old Days-Left engine filtered
    these out and silently lost that history; a derived ledger has no
    reason to.
    """
    fields = ["id", "description", "category", cfg.code_field]
    if cfg.uom_field:
        fields.append(cfg.uom_field)
    out = {}
    for row in cfg.lot_model.objects.values(*fields):
        out[row["id"]] = {
            "description": row["description"] or "",
            "category": row["category"] or "",
            "code": row[cfg.code_field] or "",
            "uom": (row.get(cfg.uom_field) or "") if cfg.uom_field else "",
            "key": normalize_material(row["description"] or ""),
        }
    return out


def _snapshot_points(cfg: PlantLedgerConfig, since: datetime.date | None) -> dict[int, list[ConsumptionPoint]]:
    """Every lot's snapshot history, grouped by lot id, in ONE query.

    Same `values_list` + `itertools.groupby` shape (and the same reason) as
    _domestic_base.py's `_consumption_by_lot()`: one query for every lot,
    not one per lot, which would be an N+1 across several hundred lots per
    plant. `order_by` on (lot, date) is what makes groupby correct - it
    groups consecutive runs only.
    """
    qs = cfg.snapshot_model.objects.all()
    if since is not None:
        qs = qs.filter(snapshot_date__gte=since)
    rows = qs.values_list(
        "stock_lot_id", "snapshot_date", "opening_stock", "received", "issued", "todays_stock",
    ).order_by("stock_lot_id", "snapshot_date")
    return {
        lot_id: [ConsumptionPoint(d, o, r, i, c) for _, d, o, r, i, c in group]
        for lot_id, group in itertools.groupby(rows, key=lambda row: row[0])
    }


def _dated_movements(cfg: PlantLedgerConfig, since: datetime.date | None) -> tuple[dict[int, dict[datetime.date, Decimal]], datetime.date | None]:
    """Achhad only: real per-day issue quantities from the Stock file's own
    daily Recp./Issue matrix, plus the last date that matrix covers.

    This is a genuinely better source than a snapshot interval and is
    preferred wherever it exists. Two reasons, both confirmed against real
    data (2026-09-21):

    - It is already per-day and already dated, so nothing has to be
      inferred from two balances.
    - Its dates are the *real issue dates*. A snapshot lags them by a day:
      lot "Isnr - (Svr - 10)" shows a movement dated 09-03 whose effect
      first appears in the 09-04 snapshot. Attributing that issue to 09-04
      would put it on the wrong day.

    Returns ({lot_id: {date: issued}}, last_covered_date). Snapshot-derived
    intervals are then used only for dates strictly AFTER
    last_covered_date - see `_points_after()` - so the two sources can
    never both claim the same day.
    """
    if cfg.daily_movement_model is None:
        return {}, None
    qs = cfg.daily_movement_model.objects.all()
    if since is not None:
        qs = qs.filter(movement_date__gte=since)
    rows = list(qs.values_list("stock_lot_id", "movement_date", "issued"))
    if not rows:
        return {}, None
    by_lot: dict[int, dict[datetime.date, Decimal]] = {}
    for lot_id, date, issued in rows:
        if issued and issued > 0:
            per_date = by_lot.setdefault(lot_id, {})
            per_date[date] = per_date.get(date, Decimal(0)) + issued
    return by_lot, max(r[1] for r in rows)


def _points_after(
    points: list[ConsumptionPoint],
    cutoff: datetime.date | None,
    matrix_days: dict[datetime.date, Decimal] | None = None,
) -> list[ConsumptionPoint]:
    """Trims a lot's snapshot series to the part the dated-movement matrix
    does not already cover, **synthesising an anchor point dated exactly at
    the cutoff** so the first surviving interval starts where the matrix
    stops.

    Simply keeping the last real snapshot at or before the cutoff is not
    enough, and the difference is a double-count. Say the matrix covers
    through Sep 3 and the snapshots fall on Sep 2 and Sep 5: the interval
    Sep 2 -> Sep 5 spreads across Sep 3, 4 and 5, so Sep 3 receives a share
    from the snapshots **on top of** what the matrix already recorded for
    it. In the live data the two sources happened to abut exactly (matrix
    through Sep 7, next snapshot Sep 8) so nothing overlapped, which is
    precisely why this needed a test rather than an inspection.

    The synthetic point carries the anchor's own issue total plus whatever
    the matrix recorded between the anchor and the cutoff, so the interval
    that follows it measures only the part the matrix did not:
    `(1500 - 500) = 1000` total becomes `777` from the matrix plus `223`
    from the snapshots, allocated to Sep 4 and Sep 5 alone. The sum is
    preserved exactly; only the attribution changes.

    If the two sources contradict each other - the matrix claiming more
    issued by the cutoff than the next snapshot's running total shows - the
    synthetic point's issue figure exceeds the one after it and the engine
    classifies that interval as a period roll, recording it as a visible
    ConsumptionEvent. That is the right outcome: two sources disagreeing is
    a finding, not something to quietly average away.
    """
    if cutoff is None:
        return points
    after = [p for p in points if p.date > cutoff]
    if not after:
        return []
    at_or_before = [p for p in points if p.date <= cutoff]
    if not at_or_before:
        return after
    anchor = at_or_before[-1]
    if anchor.date == cutoff:
        return [anchor] + after
    covered = sum(
        (qty for date, qty in (matrix_days or {}).items() if anchor.date < date <= cutoff),
        Decimal(0),
    )
    synthetic = ConsumptionPoint(
        cutoff,
        anchor.opening,
        anchor.received,
        anchor.issued + covered,
        anchor.closing - covered,
    )
    return [synthetic] + after


def rebuild_plant_consumption(
    plant_key: str,
    *,
    since: datetime.date | None = None,
    max_dated_gap_days: int = 1,
) -> dict:
    """Recomputes one plant's consumption ledger and replaces it in place.

    `since` limits both the source read and the rows replaced; None means
    the plant's entire snapshot history. The default caller
    (compute_<plant>_consumption, run every sync) passes a short lookback
    rather than None - re-deriving all of history on every one of twelve
    daily syncs would be wasted work, but a lookback wider than one day is
    still needed because a late-arriving snapshot changes the interval on
    either side of it, not just its own day.

    Idempotent: the affected range is deleted and rewritten inside one
    transaction, so a re-run produces exactly the same table and a crashed
    run leaves the previous ledger intact rather than a half-written one.
    """
    cfg = PLANT_CONFIGS[plant_key]
    now = timezone.now()

    # Read further back than the range being rewritten. An interval needs
    # the snapshot BEFORE it to difference against, so reading from `since`
    # itself leaves the first day of the range with no predecessor and it
    # silently disappears from the rebuilt ledger - an off-by-one that only
    # shows up on a bounded rebuild, which is the path every scheduled run
    # takes. Rows dated before `since` are computed but not written (see
    # the `date >= since` filter below), so they cannot duplicate the
    # existing ones that were deliberately left in place.
    read_from = since - datetime.timedelta(days=_ANCHOR_LOOKBACK_DAYS) if since else None

    lot_meta = _lot_meta(cfg)
    points_by_lot = _snapshot_points(cfg, read_from)
    movements_by_lot, movement_cutoff = _dated_movements(cfg, read_from)

    # (material_key, date) -> [quantity, quality, {lot ids}]
    daily: dict[tuple[str, datetime.date], list] = {}
    events: list[ConsumptionEvent] = []
    # date -> was any lot's interval ending here a single observed day?
    # Collected for EVERY counted interval, including zero-quantity ones -
    # a day on which nothing moved is still a day that was watched, and it
    # is exactly the day MaterialConsumptionDaily cannot record. See
    # ConsumptionCoverage's docstring for why this is the denominator.
    coverage: dict[datetime.date, bool] = {}

    def _add(material_key: str, date: datetime.date, qty: Decimal, quality: str, lot_id: int) -> None:
        if qty <= 0:
            return
        slot = daily.get((material_key, date))
        if slot is None:
            daily[(material_key, date)] = [qty, quality, {lot_id}]
            return
        slot[0] += qty
        # Worst contributing quality wins, the same "one thin lot makes the
        # whole group thin" rule the Days-Left engine's own group
        # aggregation uses - a material whose figure is part observed and
        # part interpolated is an interpolated figure.
        if quality == SPREAD:
            slot[1] = SPREAD
        slot[2].add(lot_id)

    for lot_id, lot in lot_meta.items():
        material_key = lot["key"]
        if not material_key:
            # No description to key on - the same "visible, not silently
            # dropped" problem sync_stock.py's rows_skipped handles, but a
            # lot with no description at all can't even be named in an
            # event row, so it is counted in the result dict instead.
            continue

        for date, issued in movements_by_lot.get(lot_id, {}).items():
            _add(material_key, date, issued, COUNTED, lot_id)
            coverage[date] = True

        points = _points_after(
            points_by_lot.get(lot_id, []), movement_cutoff, movements_by_lot.get(lot_id),
        )
        if len(points) < 2:
            continue
        lot_daily, excluded = daily_from_points(points, max_dated_gap_days=max_dated_gap_days)
        for date, (qty, quality) in lot_daily.items():
            _add(material_key, date, qty, quality, lot_id)
        # Coverage is derived from the INTERVALS, not from `lot_daily` -
        # allocate_daily() drops a zero-quantity interval entirely (there is
        # no row to write), but the days it spans were still observed and
        # must count against the denominator. Reading coverage off the
        # quantities instead would make a quiet plant look unwatched and
        # inflate every rate it has.
        for interval in intervals_for_lot(points, max_dated_gap_days=max_dated_gap_days):
            if not interval.counts_toward_rate:
                continue
            for n in range(1, interval.span_days + 1):
                date = interval.start + datetime.timedelta(days=n)
                coverage[date] = coverage.get(date, False) or interval.span_days == 1
        for interval in excluded:
            if since is not None and interval.end < since:
                continue
            events.append(ConsumptionEvent(
                plant=cfg.plant,
                material_key=material_key,
                material_description=lot["description"][:500],
                lot_ref=f"{cfg.lot_model.__name__}#{lot_id}",
                kind=interval.classification,
                interval_start=interval.start,
                interval_end=interval.end,
                quantity=interval.quantity,
                balance_quantity=interval.balance_quantity,
                computed_at=now,
            ))

    # Rebuild the display fields from whichever lot is currently the best
    # source for this material - a material with several lots should show
    # one description, not whichever lot happened to be iterated last.
    meta_by_key: dict[str, dict] = {}
    for lot in lot_meta.values():
        if lot["key"] and (lot["key"] not in meta_by_key or (lot["code"] and not meta_by_key[lot["key"]]["code"])):
            meta_by_key[lot["key"]] = lot

    rows = [
        MaterialConsumptionDaily(
            plant=cfg.plant,
            material_key=material_key,
            consumption_date=date,
            quantity=qty,
            quality=quality,
            material_description=meta_by_key.get(material_key, {}).get("description", "")[:500],
            material_code=meta_by_key.get(material_key, {}).get("code", "")[:50],
            category=meta_by_key.get(material_key, {}).get("category", "")[:100],
            uom=meta_by_key.get(material_key, {}).get("uom", "")[:20],
            lot_count=len(lot_ids),
            computed_at=now,
        )
        for (material_key, date), (qty, quality, lot_ids) in daily.items()
        # Days before the rebuild's own range were computed only to anchor
        # the first in-range interval - their existing rows were never
        # deleted, so writing them again would duplicate.
        if since is None or date >= since
    ]

    coverage_rows = [
        ConsumptionCoverage(plant=cfg.plant, coverage_date=date, observed=observed, computed_at=now)
        for date, observed in coverage.items()
        if since is None or date >= since
    ]

    with transaction.atomic():
        stale_daily = MaterialConsumptionDaily.objects.filter(plant=cfg.plant)
        stale_events = ConsumptionEvent.objects.filter(plant=cfg.plant)
        stale_coverage = ConsumptionCoverage.objects.filter(plant=cfg.plant)
        if since is not None:
            stale_daily = stale_daily.filter(consumption_date__gte=since)
            stale_events = stale_events.filter(interval_end__gte=since)
            stale_coverage = stale_coverage.filter(coverage_date__gte=since)
        stale_daily.delete()
        stale_events.delete()
        stale_coverage.delete()
        MaterialConsumptionDaily.objects.bulk_create(rows, batch_size=1000)
        ConsumptionEvent.objects.bulk_create(events, batch_size=1000, ignore_conflicts=True)
        ConsumptionCoverage.objects.bulk_create(coverage_rows, batch_size=1000, ignore_conflicts=True)

    spread_days = sum(1 for r in rows if r.quality == SPREAD)
    return {
        "plant": cfg.plant,
        "lots_read": len(points_by_lot),
        "material_days": len(rows),
        "coverage_days": len(coverage_rows),
        "spread_days": spread_days,
        "events": len(events),
        "total_quantity": sum((r.quantity for r in rows), Decimal(0)),
    }
