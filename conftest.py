"""Project-wide pytest fixtures."""

import importlib

import pytest

_SEED = "apps.core.migrations.0068_seed_procurement_reference"


@pytest.fixture(autouse=True)
def procurement_reference(request):
    """The three plants and the MIR reasons, exactly as migration 0068 seeds
    them, present for every database test.

    A `transaction=True` test anywhere in the run flushes every table,
    migration data included, so anything relying on the migration alone -
    the MIR tests, and every PO sync, which projects into the procurement
    tables - passed or failed by test order (found 2026-09-28). Re-seeding is
    skipped when the rows are already there, so an ordinary test pays two
    COUNT queries; a test without database access pays nothing."""
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        return
    request.getfixturevalue("transactional_db" if marker.kwargs.get("transaction") else "db")
    from django.apps import apps

    seed = importlib.import_module(_SEED)
    Plant = apps.get_model("core", "Plant")
    Reason = apps.get_model("core", "MirReasonCode")
    if Plant.objects.count() == len(seed.PLANTS) and Reason.objects.count() >= len(seed.REASONS):
        return
    seed.seed(apps, None)
