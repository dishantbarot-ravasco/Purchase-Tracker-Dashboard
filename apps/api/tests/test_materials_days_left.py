"""
Integration tests for the "Days-Left Engine" wired into GET /materials
(_domestic_base.py's `_lot_dict()`/`make_materials()`/`_consumption_by_lot()`)
- the pure math itself is covered dependency-free in
apps/services/tests/test_stock_consumption.py; this file only covers the
API-layer wiring: the consumption block landing in the response, msl-driven
daysToMsl on Achhad vs. its absence on HRS, and the two-queries-not-N+1
prefetch shape.
"""
import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.api.routers._domestic_base import _consumption_by_lot
from apps.api.routers.hrs_views import _CONFIG as HRS_CONFIG
from apps.api.routers.achhad_views import _CONFIG as ACHHAD_CONFIG
from apps.api.tests.factories import make_user
from apps.core.models import HRSStockLot, HRSStockSnapshot, RTPAchhadStockLot, RTPAchhadStockSnapshot

TODAY = datetime.date.today()


def _hrs_lot_with_history(stocks, **overrides):
    """stocks: list of (days_ago, todays_stock) - a snapshot is created for
    each, dated relative to real "today" so it lands inside the consumption
    window's DB-level date filter regardless of when the test runs."""
    defaults = dict(description="Natural Rubber", category="Rubber", basic_rate=Decimal("120.5000"),
                     todays_stock=stocks[-1][1])
    defaults.update(overrides)
    lot = HRSStockLot.objects.create(**defaults)
    for days_ago, stock in stocks:
        HRSStockSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
            opening_stock=stock, received=0, issued=0, todays_stock=stock,
            basic_rate=Decimal("120.5000"), value=stock * Decimal("120.5"),
        )
    return lot


def _achhad_lot_with_history(stocks, **overrides):
    defaults = dict(description="Natural Rubber", category="Rubber", rate=Decimal("120.5000"),
                     todays_stock=stocks[-1][1])
    defaults.update(overrides)
    lot = RTPAchhadStockLot.objects.create(**defaults)
    for days_ago, stock in stocks:
        RTPAchhadStockSnapshot.objects.create(
            stock_lot=lot, snapshot_date=TODAY - datetime.timedelta(days=days_ago),
            opening_stock=stock, received=0, issued=0, todays_stock=stock,
            rate=Decimal("120.5000"), value=stock * Decimal("120.5"),
        )
    return lot


@pytest.mark.django_db
class TestMaterialsConsumptionBlock:
    def test_hrs_lot_gets_a_consumption_block_from_its_snapshot_history(self):
        # 1000 -> 900 -> 800, 1 day apart: steady 100/day drawdown.
        _hrs_lot_with_history([(2, Decimal("1000")), (1, Decimal("900")), (0, Decimal("800"))])
        client = APIClient()
        client.force_authenticate(user=make_user(email="v1@ravasco.com", role="viewer"))
        response = client.get("/api/materials")
        assert response.status_code == 200
        [material] = response.json()["materials"]
        assert material["consumption"]["avgDaily"] == 100.0
        assert material["consumption"]["daysLeft"] == 8.0
        assert material["consumption"]["intervalsUsed"] == 2
        # HRSStockLot has no msl column at all - daysToMsl must be None,
        # never a crash from a missing attribute.
        assert material["daysToMsl"] is None

    def test_lot_with_only_one_snapshot_gets_a_null_consumption_block(self):
        # A brand-new lot (or one whose lot identity just reset - see
        # CLAUDE.md's source_row_ref notes) has no drawdown to compute yet.
        _hrs_lot_with_history([(0, Decimal("500"))])
        client = APIClient()
        client.force_authenticate(user=make_user(email="v2@ravasco.com", role="viewer"))
        response = client.get("/api/materials")
        [material] = response.json()["materials"]
        assert material["consumption"]["avgDaily"] is None
        assert material["consumption"]["confidence"] == "none"

    def test_achhad_lot_below_msl_gets_days_to_msl_zero(self):
        # Current stock already at/below msl - flagged immediately (0),
        # independent of whether a consumption rate is known yet.
        _achhad_lot_with_history([(0, Decimal("500"))], msl=Decimal("600"))
        client = APIClient()
        client.force_authenticate(user=make_user(email="v3@ravasco.com", role="viewer"))
        response = client.get("/api/achhad/materials")
        [material] = response.json()["materials"]
        assert material["daysToMsl"] == 0.0

    def test_achhad_lot_above_msl_with_known_rate_gets_an_eta(self):
        # 1000 -> 900 -> 800: 100/day. msl=500 -> 300 units above msl -> 3 days.
        _achhad_lot_with_history(
            [(2, Decimal("1000")), (1, Decimal("900")), (0, Decimal("800"))], msl=Decimal("500"),
        )
        client = APIClient()
        client.force_authenticate(user=make_user(email="v4@ravasco.com", role="viewer"))
        response = client.get("/api/achhad/materials")
        [material] = response.json()["materials"]
        assert material["daysToMsl"] == 3.0

    def test_achhad_lot_above_msl_with_no_rate_yet_gets_none(self):
        _achhad_lot_with_history([(0, Decimal("1000"))], msl=Decimal("500"))
        client = APIClient()
        client.force_authenticate(user=make_user(email="v5@ravasco.com", role="viewer"))
        response = client.get("/api/achhad/materials")
        [material] = response.json()["materials"]
        assert material["daysToMsl"] is None


@pytest.mark.django_db
class TestConsumptionQueryShape:
    def test_consumption_prefetch_is_one_query_regardless_of_lot_count(self, django_assert_num_queries):
        """_consumption_by_lot() must fetch every active lot's window of
        snapshots in one query - not one query per lot, which would be an
        N+1 across however many hundred lots a plant has."""
        for i in range(3):
            _hrs_lot_with_history(
                [(2, Decimal("1000")), (1, Decimal("900")), (0, Decimal("800"))],
                description=f"Material {i}", source_row_ref=f"row-{i}",
            )
        with django_assert_num_queries(1):
            _consumption_by_lot(HRS_CONFIG)

    def test_achhad_consumption_prefetch_is_also_one_query(self, django_assert_num_queries):
        for i in range(3):
            _achhad_lot_with_history(
                [(2, Decimal("1000")), (1, Decimal("900")), (0, Decimal("800"))],
                description=f"Material {i}", source_row_ref=f"row-{i}", msl=Decimal("500"),
            )
        with django_assert_num_queries(1):
            _consumption_by_lot(ACHHAD_CONFIG)
