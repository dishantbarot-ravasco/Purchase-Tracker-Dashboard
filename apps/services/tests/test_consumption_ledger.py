"""
Integration tests for apps/services/consumption_ledger.py and
apps/services/consumption_periods.py - the DB layer over the pure engine
and the period rollups on top of it. Real Postgres, no mocking, same
convention as the other pipeline-level tests in this directory
(test_run_full_match_pipeline.py and friends).

The pure arithmetic is covered dependency-free in test_consumption_engine.py;
what is tested here is only what the database adds - lot-to-material
rollup, the Achhad dated-movement precedence, idempotency, and that a
month/quarter/year total is the sum of its days rather than a second,
independently-derived figure.
"""
import datetime
from decimal import Decimal

import pytest

from apps.core.models import (
    ConsumptionCoverage,
    ConsumptionEvent,
    HRSRMLot,
    HRSRMSnapshot,
    MaterialConsumptionDaily,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
    SyncRun,
)
from apps.services import consumption_periods as periods
from apps.services.consumption_engine import COUNTED
from apps.services.consumption_ledger import rebuild_plant_consumption

D = datetime.date
pytestmark = pytest.mark.django_db


def _hrs_lot(description, **kw):
    return HRSRMLot.objects.create(
        description=description,
        natural_key=kw.pop("natural_key", description.lower()),
        sap_item_code=kw.pop("sap_item_code", ""),
        category=kw.pop("category", "Rubber"),
        uom=kw.pop("uom", "KG"),
        party_name=kw.pop("party_name", ""),
        **kw,
    )


def _hrs_snap(lot, day, opening, received, issued, closing):
    return HRSRMSnapshot.objects.create(
        stock_lot=lot, snapshot_date=D(2026, 9, day),
        opening_stock=Decimal(str(opening)), received=Decimal(str(received)),
        issued=Decimal(str(issued)), todays_stock=Decimal(str(closing)),
    )


class TestLotToMaterialRollup:
    def test_two_vendor_lots_of_one_material_become_one_material_row(self):
        # The point of keying on the material rather than *RMLot.natural_key:
        # HRS buys SBR 1502 from several vendors, which is three lots and
        # one material.
        a = _hrs_lot("SBR 1502", natural_key="sbr-a", party_name="GPC International")
        b = _hrs_lot("SBR 1502", natural_key="sbr-b", party_name="Balaji Rubbers")
        for lot, issued in ((a, 100), (b, 250)):
            _hrs_snap(lot, 1, 1000, 0, 0, 1000)
            _hrs_snap(lot, 2, 1000, 0, issued, 1000 - issued)

        rebuild_plant_consumption("hrs")

        rows = MaterialConsumptionDaily.objects.filter(plant=SyncRun.Plant.HRS)
        assert rows.count() == 1
        row = rows.get()
        assert row.quantity == Decimal(350)
        assert row.lot_count == 2

    def test_a_reworded_description_is_a_different_material_key(self):
        # Honest documentation of the tradeoff rather than a claim it can't
        # happen: description IS the key, so a rewording forks the series.
        # The ledger is fully recomputable, which is what makes that
        # recoverable later - see MaterialConsumptionDaily's docstring.
        a = _hrs_lot("ZINC OXIDE", natural_key="zn-a")
        b = _hrs_lot("ZINC OXIDE (RUBBER GRADE)", natural_key="zn-b")
        for lot in (a, b):
            _hrs_snap(lot, 1, 1000, 0, 0, 1000)
            _hrs_snap(lot, 2, 1000, 0, 100, 900)

        rebuild_plant_consumption("hrs")
        assert MaterialConsumptionDaily.objects.filter(plant=SyncRun.Plant.HRS).count() == 2

    def test_punctuation_and_casing_do_not_fork_a_material(self):
        a = _hrs_lot("Carbon Black N-330", natural_key="cb-a")
        b = _hrs_lot("CARBON BLACK N 330", natural_key="cb-b")
        for lot in (a, b):
            _hrs_snap(lot, 1, 1000, 0, 0, 1000)
            _hrs_snap(lot, 2, 1000, 0, 100, 900)

        rebuild_plant_consumption("hrs")
        row = MaterialConsumptionDaily.objects.get(plant=SyncRun.Plant.HRS)
        assert row.quantity == Decimal(200)
        assert row.lot_count == 2


class TestDeactivatedLots:
    def test_an_inactive_lot_keeps_the_history_it_really_consumed(self):
        # The old Days-Left engine filtered on is_active=True and silently
        # lost these days. A lot that has since sold out still consumed
        # material while it was alive.
        lot = _hrs_lot("TMQ", is_active=False)
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 400, 600)

        rebuild_plant_consumption("hrs")
        assert MaterialConsumptionDaily.objects.get(plant=SyncRun.Plant.HRS).quantity == Decimal(400)


class TestExcludedEventsAreRecorded:
    def test_a_closeout_is_excluded_from_the_ledger_and_named_in_an_event(self):
        lot = _hrs_lot("SBR 1502")
        _hrs_snap(lot, 1, 75600, 0, 0, 75600)
        _hrs_snap(lot, 2, 75600, 0, 0, 75600)
        _hrs_snap(lot, 3, 75600, 0, 75600, 0)

        rebuild_plant_consumption("hrs")

        assert not MaterialConsumptionDaily.objects.filter(plant=SyncRun.Plant.HRS).exists()
        event = ConsumptionEvent.objects.get(plant=SyncRun.Plant.HRS)
        assert event.kind == "closeout"
        assert event.quantity == Decimal(75600)
        assert event.lot_ref == f"HRSRMLot#{lot.id}"

    def test_a_restatement_records_both_figures_so_the_gap_is_visible(self):
        # HRS lot 119 SACK CARBON, verbatim.
        lot = _hrs_lot("SACK CARBON")
        _hrs_snap(lot, 3, 450500, 0, 0, 450500)
        _hrs_snap(lot, 4, 17850, 0, 0, 17850)

        rebuild_plant_consumption("hrs")

        # Nothing was issued, so nothing lands in the ledger at all.
        assert not MaterialConsumptionDaily.objects.filter(plant=SyncRun.Plant.HRS).exists()


class TestIdempotency:
    def test_rebuilding_twice_produces_the_same_table(self):
        lot = _hrs_lot("6PPD")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 200, 800)

        first = rebuild_plant_consumption("hrs")
        before = list(MaterialConsumptionDaily.objects.values_list("material_key", "consumption_date", "quantity"))
        second = rebuild_plant_consumption("hrs")
        after = list(MaterialConsumptionDaily.objects.values_list("material_key", "consumption_date", "quantity"))

        assert before == after
        assert first["total_quantity"] == second["total_quantity"]

    def test_a_since_bound_rebuild_leaves_earlier_rows_alone(self):
        lot = _hrs_lot("6PPD")
        for day, issued in ((1, 0), (2, 100), (3, 250), (4, 400)):
            _hrs_snap(lot, day, 1000, 0, issued, 1000 - issued)
        rebuild_plant_consumption("hrs")
        assert MaterialConsumptionDaily.objects.count() == 3

        rebuild_plant_consumption("hrs", since=D(2026, 9, 4))
        # The Sep 2 and Sep 3 rows predate the bound and must survive.
        assert MaterialConsumptionDaily.objects.filter(consumption_date=D(2026, 9, 2)).exists()
        assert MaterialConsumptionDaily.objects.count() == 3


class TestAchhadDatedMovements:
    def _achhad(self):
        lot = RTPAchhadRMLot.objects.create(
            description="Imported Coal", natural_key="coal", sap_code="C1",
            category="Fuel", opening_stock=Decimal(10000),
        )
        for day, (received, issued, closing) in {
            1: (0, 0, 10000), 2: (0, 500, 9500), 5: (0, 1500, 8500), 6: (0, 2000, 8000),
        }.items():
            RTPAchhadRMSnapshot.objects.create(
                stock_lot=lot, snapshot_date=D(2026, 9, day),
                opening_stock=Decimal(10000), received=Decimal(received),
                issued=Decimal(issued), todays_stock=Decimal(closing),
            )
        return lot

    def test_the_dated_matrix_wins_for_the_days_it_covers(self):
        lot = self._achhad()
        # A real per-day issue on Sep 3, a date the snapshots don't isolate.
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=D(2026, 9, 3), received=Decimal(0), issued=Decimal(777),
        )
        rebuild_plant_consumption("achhad")

        row = MaterialConsumptionDaily.objects.get(plant=SyncRun.Plant.RTP_ACHHAD, consumption_date=D(2026, 9, 3))
        assert row.quantity == Decimal(777)
        assert row.quality == COUNTED

    def test_snapshot_intervals_do_not_also_claim_a_matrix_covered_day(self):
        lot = self._achhad()
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=D(2026, 9, 3), received=Decimal(0), issued=Decimal(777),
        )
        rebuild_plant_consumption("achhad")

        # Only the matrix may write on/before its own cutoff.
        covered = MaterialConsumptionDaily.objects.filter(
            plant=SyncRun.Plant.RTP_ACHHAD, consumption_date__lte=D(2026, 9, 3),
        )
        assert [r.quantity for r in covered] == [Decimal(777)]

    def test_the_first_day_after_the_cutoff_is_not_lost(self):
        # _points_after() keeps one anchor point at/before the cutoff so the
        # interval spanning the boundary still has something to difference.
        lot = self._achhad()
        RTPAchhadRMDailyMovement.objects.create(
            stock_lot=lot, movement_date=D(2026, 9, 2), received=Decimal(0), issued=Decimal(500),
        )
        rebuild_plant_consumption("achhad")

        after = MaterialConsumptionDaily.objects.filter(
            plant=SyncRun.Plant.RTP_ACHHAD, consumption_date__gt=D(2026, 9, 2),
        ).order_by("consumption_date")
        # Sep 2 -> Sep 5 issued 500 -> 1500, spread over Sep 3/4/5.
        assert sum(r.quantity for r in after) == Decimal(1500)


class TestPeriodRollups:
    def _month_of_data(self):
        lot = _hrs_lot("SULPHUR")
        running = 0
        _hrs_snap(lot, 1, 100000, 0, 0, 100000)
        for day in range(2, 31):
            running += 100
            _hrs_snap(lot, day, 100000, 0, running, 100000 - running)
        rebuild_plant_consumption("hrs")
        return lot

    def test_the_month_total_is_exactly_the_sum_of_its_days(self):
        self._month_of_data()
        start, end = periods.month_bounds(2026, 9)
        total = periods.period_summary(start, end, plant=SyncRun.Plant.HRS)["quantity"]
        day_sum = sum(
            r.quantity for r in MaterialConsumptionDaily.objects.filter(
                consumption_date__gte=start, consumption_date__lte=end)
        )
        assert total == day_sum == Decimal(2900)

    def test_a_quarter_total_is_the_sum_of_its_months(self):
        self._month_of_data()
        q_start, q_end = periods.quarter_bounds(2026, 2)  # Jul-Sep
        assert (q_start, q_end) == (D(2026, 7, 1), D(2026, 9, 30))
        quarter = periods.period_summary(q_start, q_end, plant=SyncRun.Plant.HRS)["quantity"]
        september = periods.period_summary(*periods.month_bounds(2026, 9), plant=SyncRun.Plant.HRS)["quantity"]
        assert quarter == september

    def test_the_financial_year_runs_april_to_march(self):
        assert periods.financial_year_bounds(2026) == (D(2026, 4, 1), D(2027, 3, 31))
        assert periods.financial_year_label(2026) == "2026-27"
        # A January date belongs to the financial year that began the prior April.
        assert periods.financial_year_of(D(2027, 1, 15)) == 2026

    def test_quarters_are_financial_not_calendar(self):
        assert periods.quarter_bounds(2026, 1) == (D(2026, 4, 1), D(2026, 6, 30))
        assert periods.quarter_bounds(2026, 4) == (D(2027, 1, 1), D(2027, 3, 31))

    def test_material_totals_carry_the_interpolated_share(self):
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 100, 900)     # observed
        _hrs_snap(lot, 6, 1000, 0, 500, 500)     # spread over 4 days
        rebuild_plant_consumption("hrs")

        row = periods.material_totals(D(2026, 9, 1), D(2026, 9, 30), plant=SyncRun.Plant.HRS)[0]
        assert row["quantity"] == Decimal(500)
        assert row["spreadQuantity"] == Decimal(400)
        assert row["spreadDays"] == 4

    def test_period_series_buckets_by_the_requested_grain(self):
        self._month_of_data()
        monthly = periods.period_series(periods.GRAIN_MONTH, D(2026, 1, 1), D(2026, 12, 31), plant=SyncRun.Plant.HRS)
        assert [b["label"] for b in monthly] == ["2026-09"]
        assert monthly[0]["quantity"] == Decimal(2900)

    def test_period_series_rejects_an_unknown_grain(self):
        with pytest.raises(ValueError):
            periods.period_series("fortnight", D(2026, 9, 1), D(2026, 9, 30))

    def test_a_company_wide_total_rolls_the_plants_together_on_one_key(self):
        # Only possible because material_key is uniform across plants.
        hrs = _hrs_lot("ZINC OXIDE")
        _hrs_snap(hrs, 1, 1000, 0, 0, 1000)
        _hrs_snap(hrs, 2, 1000, 0, 100, 900)
        achhad = RTPAchhadRMLot.objects.create(
            description="Zinc Oxide", natural_key="zn", opening_stock=Decimal(1000),
        )
        for day, issued in ((1, 0), (2, 250)):
            RTPAchhadRMSnapshot.objects.create(
                stock_lot=achhad, snapshot_date=D(2026, 9, day), opening_stock=Decimal(1000),
                received=Decimal(0), issued=Decimal(issued), todays_stock=Decimal(1000 - issued),
            )
        rebuild_plant_consumption("hrs")
        rebuild_plant_consumption("achhad")

        rows = periods.material_totals(D(2026, 9, 1), D(2026, 9, 30))
        assert len(rows) == 1
        assert rows[0]["quantity"] == Decimal(350)


class TestConsumptionRate:
    def test_coverage_reports_observed_days_not_the_calendar_span(self):
        # The old confidence band counted the SPAN between first and last
        # snapshot, so 7 real days inside a 17-day span read as `high`.
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 10000, 0, 0, 10000)
        _hrs_snap(lot, 2, 10000, 0, 100, 9900)
        _hrs_snap(lot, 18, 10000, 0, 1000, 9000)
        rebuild_plant_consumption("hrs")

        stats = periods.consumption_rate(
            "sulphur", plant=SyncRun.Plant.HRS, window_days=30, today=D(2026, 9, 21),
        )
        assert stats["observedDays"] == 1
        assert stats["spreadDays"] == 16
        assert stats["observedRatio"] < 0.6

    def test_the_rate_divides_by_the_window_not_by_days_with_data(self):
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 10000, 0, 0, 10000)
        _hrs_snap(lot, 2, 10000, 0, 300, 9700)
        rebuild_plant_consumption("hrs")

        stats = periods.consumption_rate(
            "sulphur", plant=SyncRun.Plant.HRS, window_days=30, today=D(2026, 9, 21),
        )
        assert stats["avgDaily"] == pytest.approx(10.0)


class TestCoverageIsTheDenominator:
    """ConsumptionCoverage exists because MaterialConsumptionDaily holds
    only days something moved, so it cannot tell a quiet-but-watched day
    apart from a day nobody looked at. Getting that wrong skews every rate."""

    def test_a_quiet_day_inside_coverage_still_dilutes_the_average(self):
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 100, 900)   # 100 issued
        _hrs_snap(lot, 3, 1000, 0, 100, 900)   # nothing issued, but watched
        rebuild_plant_consumption("hrs")

        covered, observed = periods.coverage_in_window(
            SyncRun.Plant.HRS, D(2026, 9, 1), D(2026, 9, 30))
        assert (covered, observed) == (2, 2)

        stats = periods.consumption_rates(SyncRun.Plant.HRS, today=D(2026, 9, 3))["sulphur"]
        # 100 units over 2 watched days, not over 1 day with a row.
        assert stats["avgDaily"] == pytest.approx(50.0)

    def test_days_nobody_looked_at_are_not_counted_as_zero_consumption(self):
        # Two snapshots one day apart at the start of a 30-day window.
        # Dividing by 30 would put the rate 15x low.
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 100, 900)
        rebuild_plant_consumption("hrs")

        stats = periods.consumption_rates(SyncRun.Plant.HRS, today=D(2026, 9, 30))["sulphur"]
        assert stats["coverageDays"] == 1
        assert stats["avgDaily"] == pytest.approx(100.0)

    def test_a_zero_quantity_interval_still_creates_coverage(self):
        # Nothing was issued at all, so there is no ledger row anywhere -
        # but the days were watched, and a later material's rate depends on
        # that being recorded. Reading coverage off the quantities instead
        # of the intervals would miss this entirely.
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 0, 1000)
        rebuild_plant_consumption("hrs")

        assert not MaterialConsumptionDaily.objects.exists()
        assert periods.coverage_in_window(SyncRun.Plant.HRS, D(2026, 9, 1), D(2026, 9, 30)) == (1, 1)

    def test_a_gap_spanned_day_counts_as_covered_but_not_observed(self):
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 5, 1000, 0, 400, 600)
        rebuild_plant_consumption("hrs")

        covered, observed = periods.coverage_in_window(
            SyncRun.Plant.HRS, D(2026, 9, 1), D(2026, 9, 30))
        assert (covered, observed) == (4, 0)

    def test_coverage_is_rebuilt_not_duplicated(self):
        lot = _hrs_lot("SULPHUR")
        _hrs_snap(lot, 1, 1000, 0, 0, 1000)
        _hrs_snap(lot, 2, 1000, 0, 100, 900)
        rebuild_plant_consumption("hrs")
        rebuild_plant_consumption("hrs")

        assert ConsumptionCoverage.objects.filter(plant=SyncRun.Plant.HRS).count() == 1
