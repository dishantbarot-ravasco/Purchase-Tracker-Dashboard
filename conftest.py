"""Project-wide pytest fixtures."""

import importlib

import pytest

_SEED = "apps.core.migrations.0068_seed_procurement_reference"
_STOCK_SEED = "apps.core.migrations.0082_stock_entry"


@pytest.fixture(autouse=True)
def procurement_reference(request):
    """The three plants and the MIR reasons, exactly as migration 0068 seeds
    them, and the stock reasons migration 0082 seeds, present for every
    database test.

    A `transaction=True` test anywhere in the run flushes every table,
    migration data included, so anything relying on the migration alone -
    the MIR tests, and every PO sync, which projects into the procurement
    tables - passed or failed by test order (found 2026-09-28). Re-seeding is
    skipped when the rows are already there, so an ordinary test pays three
    COUNT queries; a test without database access pays nothing."""
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        return
    request.getfixturevalue("transactional_db" if marker.kwargs.get("transaction") else "db")
    from django.apps import apps

    seed = importlib.import_module(_SEED)
    stock_seed = importlib.import_module(_STOCK_SEED)
    Plant = apps.get_model("core", "Plant")
    Reason = apps.get_model("core", "MirReasonCode")
    StockReason = apps.get_model("core", "StockReasonCode")
    if not (Plant.objects.count() == len(seed.PLANTS) and Reason.objects.count() >= len(seed.REASONS)):
        seed.seed(apps, None)
    if StockReason.objects.count() < len(stock_seed.REASONS):
        stock_seed.seed(apps, None)
