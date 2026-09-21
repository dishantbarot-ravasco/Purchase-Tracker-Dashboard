"""
Integration tests for the days-of-cover figure wired into GET /materials
(_domestic_base.py's `_lot_dict()`/`make_materials()`/
`_consumption_by_material()`).

**Rewritten 2026-09-21 with the read-path migration.** This endpoint no
longer computes consumption from snapshot history per request; it reads the
materialised MaterialConsumptionDaily ledger, so every test here builds the
ledger first via `rebuild_plant_consumption()`, exactly as the plant's own
`compute_<plant>_consumption` pipeline step does. The arithmetic itself is
covered dependency-free in apps/services/tests/test_consumption_engine.py
and the ledger in test_consumption_ledger.py; what this file covers is only
the API-layer wiring.

The fixtures drive consumption through the **issue book** (`issued` rising
across snapshots), not through a falling balance with `issued=0` as the
previous version of this file did - that shape now correctly reports zero
consumption, because a balance moving while the issue book does not is a
restatement. See CLAUDE.md's "Consumption ledger" section.
"""
import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.routers._domestic_base import _consumption_by_material
from apps.api.routers.achhad_views import _CONFIG as ACHHAD_CONFIG
from apps.api.routers.hrs_views import _CONFIG as HRS_CONFIG
from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSRMLot,
    HRSRMSnapshot,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
)
from apps.services.consumption_ledger import rebuild_plant_consumption

TODAY = timezone.localdate()


def _hrs_lot_issuing(issues, **overrides):
    """issues: [(days_ago, cumulative_issued)] against a fixed period
    opening of 1000, so `closing` follows the sheet's own identity
    (opening + received - issued == closing) the way every real row does."""
    opening = Decimal("1000")
    defaults = dict(description="Natural Rubber", category="Rubber", basic_rate=Decimal("120.5000"),
                    opening_stock=opening, todays_stock=opening - Decimal(str(issues[-1][1])))
    defaults.update(overrides)
    lot = HRSRMLot.objects.create(**defaults)
    for days_ago, issued in issues:
        closing = opening - Decimal(str(issued))
        HRSRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
            opening_stock=opening, received=0, issued=Decimal(str(issued)), todays_stock=closing,
            basic_rate=Decimal("120.5000"), value=closing * Decimal("120.5"),
        )
    return lot


def _achhad_lot_issuing(issues, **overrides):
    opening = Decimal("1000")
    defaults = dict(description="Natural Rubber", category="Rubber", rate=Decimal("120.5000"),
                    opening_stock=opening, todays_stock=opening - Decimal(str(issues[-1][1])))
    defaults.update(overrides)
    lot = RTPAchhadRMLot.objects.create(**defaults)
    for days_ago, issued in issues:
        closing = opening - Decimal(str(issued))
        RTPAchhadRMSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
            opening_stock=opening, received=0, issued=Decimal(str(issued)), todays_stock=closing,
            rate=Decimal("120.5000"), value=closing * Decimal("120.5"),
        )
    return lot


def _get(path, email):
    client = APIClient()
    client.force_authenticate(user=make_user(email=email, role="viewer"))
    response = client.get(path)
    assert response.status_code == 200
    return response.json()["materials"]


@pytest.mark.django_db
class TestMaterialsConsumptionBlock:
    def test_a_lot_gets_a_consumption_block_from_the_ledger(self):
        # Issued 0 -> 100 -> 200 on consecutive days. Three snapshots give
        # two single-day intervals, so the plant has 2 covered days in the
        # 30-day window and the rate is 200/2 - NOT 200/30. Dividing by the
        # window would assert the 28 unwatched days were real zeros; see
        # ConsumptionCoverage's docstring.
        _hrs_lot_issuing([(2, 0), (1, 100), (0, 200)])
        rebuild_plant_consumption("hrs")

        [material] = _get("/api/materials", "v1@ravasco.com")
        assert material["consumption"]["avgDaily"] == pytest.approx(100.0)
        assert material["consumption"]["coverageDays"] == 2
        assert material["consumption"]["observedDays"] == 2
        # HRSRMLot has no msl column at all - daysToMsl must be None, never
        # a crash from a missing attribute.
        assert material["daysToMsl"] is None

    def test_days_left_is_this_lots_stock_at_the_materials_rate(self):
        _hrs_lot_issuing([(2, 0), (1, 100), (0, 200)])
        rebuild_plant_consumption("hrs")

        [material] = _get("/api/materials", "v2@ravasco.com")
        # 800 left at 100/day (200 units over 2 covered days).
        assert material["consumption"]["daysLeft"] == pytest.approx(8.0)

    def test_a_lot_with_no_ledger_rows_gets_a_null_consumption_block(self):
        # A brand-new lot with one snapshot has no interval to measure.
        _hrs_lot_issuing([(0, 0)])
        rebuild_plant_consumption("hrs")

        [material] = _get("/api/materials", "v3@ravasco.com")
        assert material["consumption"] is None

    def test_a_falling_balance_with_a_static_issue_book_is_not_consumption(self):
        # The restatement case, end to end: the sheet rewrote the figure and
        # nothing was issued. The old engine reported the whole drop.
        lot = HRSRMLot.objects.create(
            description="SACK CARBON", category="Packing", basic_rate=Decimal("10"),
            opening_stock=Decimal("450500"), todays_stock=Decimal("17850"),
        )
        for days_ago, opening in ((1, Decimal("450500")), (0, Decimal("17850"))):
            HRSRMSnapshot.objects.create(
                stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
                opening_stock=opening, received=0, issued=0, todays_stock=opening,
                basic_rate=Decimal("10"), value=opening * 10,
            )
        rebuild_plant_consumption("hrs")

        [material] = _get("/api/materials", "v4@ravasco.com")
        assert material["consumption"] is None


@pytest.mark.django_db
class TestConsumptionIsPerMaterialNotPerLot:
    def test_sibling_vendor_lots_share_one_rate(self):
        # The migration's central behaviour change. Both lots are the same
        # material from different vendors; each carries the MATERIAL's rate,
        # not its own fragment. materials.js must therefore count it once
        # per plant rather than summing across lots.
        _hrs_lot_issuing([(2, 0), (1, 100), (0, 200)], party_name="GPC International")
        _hrs_lot_issuing([(2, 0), (1, 50), (0, 100)], party_name="Balaji Rubbers",
                         natural_key="nr-balaji")
        rebuild_plant_consumption("hrs")

        materials = _get("/api/materials", "v5@ravasco.com")
        assert len(materials) == 2
        rates = {m["consumption"]["avgDaily"] for m in materials}
        assert len(rates) == 1
        # 200 + 100 issued across both lots, counted once for the material,
        # over the plant's 2 covered days.
        assert rates.pop() == pytest.approx(150.0)

    def test_a_differently_worded_lot_keeps_its_own_rate(self):
        _hrs_lot_issuing([(2, 0), (1, 100), (0, 200)])
        _hrs_lot_issuing([(2, 0), (1, 50), (0, 100)], description="Synthetic Rubber",
                         natural_key="sr")
        rebuild_plant_consumption("hrs")

        by_desc = {m["description"]: m["consumption"]["avgDaily"] for m in _get("/api/materials", "v6@ravasco.com")}
        assert by_desc["Natural Rubber"] == pytest.approx(100.0)
        assert by_desc["Synthetic Rubber"] == pytest.approx(50.0)


@pytest.mark.django_db
class TestDaysToMsl:
    def test_achhad_lot_below_msl_gets_days_to_msl_zero(self):
        # Already at/below the reorder point - flagged immediately,
        # independent of whether a consumption rate is known yet.
        _achhad_lot_issuing([(0, 500)], msl=Decimal("600"))
        rebuild_plant_consumption("achhad")

        [material] = _get("/api/achhad/materials", "v7@ravasco.com")
        assert material["daysToMsl"] == 0.0

    def test_achhad_lot_above_msl_with_a_known_rate_gets_an_eta(self):
        # 300 issued over the plant's 2 covered days -> 150/day. Stock 700,
        # msl 500, so 200 units of headroom.
        _achhad_lot_issuing([(2, 0), (1, 100), (0, 300)], msl=Decimal("500"))
        rebuild_plant_consumption("achhad")

        [material] = _get("/api/achhad/materials", "v8@ravasco.com")
        assert material["daysToMsl"] == pytest.approx(200 / 150)

    def test_achhad_lot_above_msl_with_no_rate_yet_gets_none(self):
        _achhad_lot_issuing([(0, 0)], msl=Decimal("500"))
        rebuild_plant_consumption("achhad")

        [material] = _get("/api/achhad/materials", "v9@ravasco.com")
        assert material["daysToMsl"] is None


@pytest.mark.django_db
class TestConsumptionQueryShape:
    def test_the_rate_lookup_is_a_constant_two_queries(self, django_assert_num_queries):
        """Reading the ledger is a fixed two queries for the whole plant -
        the material totals, then the coverage denominator - regardless of
        how many materials or lots there are. That constant is the point:
        the guard this replaces existed because `_consumption_by_lot()`
        could go N+1 across several hundred lots.

        It was one query until ConsumptionCoverage was added. The second is
        deliberate and worth it: without it every rate divides by the
        window length and reads low by however much of the window went
        unobserved (43% on HRS's live data)."""
        for i in range(3):
            _hrs_lot_issuing([(2, 0), (1, 100), (0, 200)],
                             description=f"Material {i}", natural_key=f"mat-{i}")
        rebuild_plant_consumption("hrs")

        with django_assert_num_queries(2):
            _consumption_by_material(HRS_CONFIG)

    def test_achhad_costs_the_same_two_queries(self, django_assert_num_queries):
        """Achhad needed an extra query before the migration, because
        `_daily_movement_points()` checked RTPAchhadRMDailyMovement at
        request time. That reconciliation happens once in the ledger build
        now, so every plant's read path costs the same."""
        for i in range(3):
            _achhad_lot_issuing([(2, 0), (1, 100), (0, 200)],
                                description=f"Material {i}", natural_key=f"mat-{i}")
        rebuild_plant_consumption("achhad")

        with django_assert_num_queries(2):
            _consumption_by_material(ACHHAD_CONFIG)
