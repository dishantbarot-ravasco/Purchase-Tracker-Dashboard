"""
Integration tests for Snapshot Pipeline Rebuild Phase C (see CLAUDE.md) -
GET /api/stock-snapshots/dates and GET /api/stock-snapshots?date=YYYY-MM-DD
(and the achhad/vapi-prefixed equivalents), added to
apps/api/routers/_domestic_base.py as make_stock_snapshot_dates()/
make_stock_snapshots_for_date(). Real Postgres (`@pytest.mark.django_db`),
no mocking - same convention as test_stock_matched_field.py/
test_read_endpoint_plant_scoping.py.

Also covers GET /api/stock-snapshots/export (Data Export, 2026-09-08,
make_export_stock_snapshots()) - the downloadable CSV of full daily RM
snapshot history.
"""

import csv
import datetime
import io

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSRMLot, HRSRMSnapshot

DATES_URL = "/api/stock-snapshots/dates"
SNAPSHOTS_URL = "/api/stock-snapshots"
EXPORT_URL = "/api/stock-snapshots/export"


def _make_lot(**kwargs):
    defaults = dict(description="Zinc Oxide", sap_item_code="H1", party_name="Kedar Metals", natural_key="H1|kedarmetals")
    defaults.update(kwargs)
    return HRSRMLot.objects.create(**defaults)


@pytest.mark.django_db
class TestStockSnapshotDates:
    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(email="v@ravasco.com", role="viewer"))

    def test_returns_distinct_dates_with_lot_counts(self):
        lot1 = _make_lot(natural_key="H1|kedarmetals")
        lot2 = _make_lot(natural_key="H2|ganesh", description="Stearic Acid", sap_item_code="H2", party_name="Ganesh")
        HRSRMSnapshot.objects.create(stock_lot=lot1, snapshot_date="2026-09-01", opening_stock=10, received=0, issued=0, todays_stock=10)
        HRSRMSnapshot.objects.create(stock_lot=lot2, snapshot_date="2026-09-01", opening_stock=5, received=0, issued=0, todays_stock=5)
        HRSRMSnapshot.objects.create(stock_lot=lot1, snapshot_date="2026-09-02", opening_stock=10, received=0, issued=1, todays_stock=9)

        response = self.client.get(DATES_URL)
        assert response.status_code == 200
        by_date = {d["date"]: d["lotCount"] for d in response.json()["dates"]}
        assert by_date == {"2026-09-01": 2, "2026-09-02": 1}


@pytest.mark.django_db
class TestStockSnapshotsForDate:
    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(email="v2@ravasco.com", role="viewer"))
        self.lot = _make_lot()
        HRSRMSnapshot.objects.create(
            stock_lot=self.lot, snapshot_date="2026-09-01",
            opening_stock=10, received=0, issued=1, todays_stock=9, basic_rate="105.5", value="949.5",
        )

    def test_known_date_returns_expected_rows(self):
        response = self.client.get(SNAPSHOTS_URL, {"date": "2026-09-01"})
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-09-01"
        assert len(body["lots"]) == 1
        lot = body["lots"][0]
        assert lot["lotId"] == self.lot.id
        assert lot["description"] == "Zinc Oxide"
        assert lot["materialCode"] == "H1"
        assert lot["vendor"] == "Kedar Metals"
        assert lot["qty"] == 9.0
        assert lot["rate"] == 105.5

    def test_omitted_date_defaults_to_latest(self):
        HRSRMSnapshot.objects.create(
            stock_lot=self.lot, snapshot_date="2026-09-03",
            opening_stock=9, received=0, issued=1, todays_stock=8,
        )
        response = self.client.get(SNAPSHOTS_URL)
        assert response.status_code == 200
        assert response.json()["date"] == "2026-09-03"

    def test_unknown_date_returns_404_with_alternatives(self):
        response = self.client.get(SNAPSHOTS_URL, {"date": "2026-01-01"})
        assert response.status_code == 404
        body = response.json()
        assert "2026-09-01" in body["availableDates"]

    def test_malformed_date_returns_400(self):
        response = self.client.get(SNAPSHOTS_URL, {"date": "not-a-date"})
        assert response.status_code == 400

    def test_no_snapshot_history_at_all_returns_404_not_empty_200(self):
        HRSRMSnapshot.objects.all().delete()
        response = self.client.get(SNAPSHOTS_URL)
        assert response.status_code == 404
        assert response.json()["availableDates"] == []


@pytest.mark.django_db
class TestStockSnapshotsPlantScoping:
    def test_plant_scoped_user_gets_403_on_another_plants_snapshots(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e@ravasco.com", role="editor", plants=["achhad"]))

        assert client.get(DATES_URL).status_code == 403
        assert client.get(SNAPSHOTS_URL).status_code == 403

    def test_plant_scoped_user_can_still_read_their_own_plant(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e2@ravasco.com", role="editor", plants=["hrs"]))

        assert client.get(DATES_URL).status_code == 200


@pytest.mark.django_db
class TestExportStockSnapshots:
    def setup_method(self):
        self.lot = _make_lot()
        HRSRMSnapshot.objects.create(
            stock_lot=self.lot, snapshot_date="2026-09-01",
            opening_stock=10, received=0, issued=1, todays_stock=9, basic_rate="105.5", value="949.5",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=self.lot, snapshot_date="2026-09-02",
            opening_stock=9, received=0, issued=1, todays_stock=8, basic_rate="105.5", value="844.0",
        )

    def _rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode("utf-8"))))

    def test_viewer_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="v3@ravasco.com", role="viewer"))
        assert client.get(EXPORT_URL).status_code == 403

    def test_editor_can_export_full_history(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e3@ravasco.com", role="editor"))
        response = client.get(EXPORT_URL)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert "attachment" in response["Content-Disposition"]
        rows = self._rows(response)
        assert rows[0][:3] == ["Plant", "Snapshot Date", "Material Description"]
        assert len(rows) == 3  # header + 2 snapshot rows
        assert rows[1][1] == "2026-09-01"
        assert rows[2][1] == "2026-09-02"

    def test_admin_can_narrow_by_date_range(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="a3@ravasco.com", role="admin"))
        response = client.get(EXPORT_URL, {"from": "2026-09-02", "to": "2026-09-02"})
        assert response.status_code == 200
        rows = self._rows(response)
        assert len(rows) == 2  # header + the one 2026-09-02 row
        assert rows[1][1] == "2026-09-02"

    def test_malformed_date_returns_400(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e4@ravasco.com", role="editor"))
        assert client.get(EXPORT_URL, {"from": "not-a-date"}).status_code == 400

    def test_plant_scoped_editor_forbidden_for_another_plant(self):
        client = APIClient()
        client.force_authenticate(user=make_user(email="e5@ravasco.com", role="editor", plants=["achhad"]))
        assert client.get(EXPORT_URL).status_code == 403
