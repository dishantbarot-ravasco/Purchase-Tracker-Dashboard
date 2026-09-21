"""
Integration tests for GET /api/[<plant>/]mir-without-po (2026-09-21) - the
drill-down behind the "purchased without a PO" / "waiting on a PO" badges.

The service-level bucket rules are covered in
apps/services/tests/test_mir_without_po.py. What can only break here is the
HTTP contract the panel depends on: the response shape, the plant gate
(this is a read, so it must be readable by a viewer and refused for a plant
the account is not scoped to), the `?bucket=` filter rejecting a typo instead
of returning an empty list that reads as "nothing to fix", and the CSV export
going through SafeCsvWriter rather than a bare csv.writer.
"""

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import HRSMIREntry

BUCKETS = {"no_po", "po_unknown", "po_known_unmatched"}


@pytest.mark.django_db
class TestMirWithoutPoEndpoint:
    def setup_method(self):
        # One receipt with no PO reference from an ordinary supplier - the
        # NO_PO bucket's canonical row.
        HRSMIREntry.objects.create(
            source_row_ref="r1", mir_no="1/09", po_number_raw="",
            party_name="Some Ordinary Supplier Pvt Ltd",
            material_description="Reclaim Rubber 8MPA", qty=100, uom="Kgs", net=5000,
        )
        # One naming an order nobody has ever heard of - PO_UNKNOWN.
        HRSMIREntry.objects.create(
            source_row_ref="r2", mir_no="2/09", po_number_raw="3000009999",
            party_name="Another Supplier Pvt Ltd",
            material_description="Carbon Black N330", qty=50, uom="Kgs", net=2500,
        )

    def _client(self, **kwargs):
        client = APIClient()
        client.force_authenticate(user=make_user(**kwargs))
        return client

    def test_a_viewer_can_read_it(self):
        """Read-gated like every other GET in this router, NOT IsEditor -
        it is a reorganisation of MIR data a viewer can already see, not a
        bulk history export."""
        response = self._client(email="v@ravasco.com", role="viewer").get("/api/mir-without-po")
        assert response.status_code == 200
        assert set(response.data["summary"]["buckets"]) == BUCKETS

    def test_every_row_carries_the_fields_the_panel_renders(self):
        response = self._client(email="v2@ravasco.com", role="viewer").get("/api/mir-without-po")
        row = next(r for r in response.data["rows"] if r["mirNo"] == "1/09")
        for field in ("bucket", "mirDate", "vendor", "description", "qty", "uom",
                      "value", "poNumberRaw", "invoiceNo", "matched", "registeredNoPoVendor"):
            assert field in row, field
        assert row["bucket"] == "no_po"

    def test_bucket_filters_the_rows_but_not_the_summary(self):
        """The panel's tabs show every bucket's count while one is open, so
        a narrowed request must still carry the full summary - otherwise the
        other two tabs read as empty."""
        response = self._client(email="v3@ravasco.com", role="viewer").get(
            "/api/mir-without-po?bucket=po_unknown")
        assert response.status_code == 200
        assert {r["bucket"] for r in response.data["rows"]} == {"po_unknown"}
        assert response.data["summary"]["buckets"]["no_po"]["rowCount"] >= 1

    def test_an_unknown_bucket_is_a_400_not_an_empty_list(self):
        """A typo'd bucket returning [] is indistinguishable from "nothing to
        fix here", which is the one wrong answer this whole feature exists to
        stop giving."""
        response = self._client(email="v4@ravasco.com", role="viewer").get(
            "/api/mir-without-po?bucket=nonsense")
        assert response.status_code == 400
        assert "nonsense" in response.data["error"]

    def test_a_viewer_scoped_to_another_plant_is_refused(self):
        client = self._client(email="v5@ravasco.com", role="viewer", plants=["achhad"])
        assert client.get("/api/mir-without-po").status_code == 403

    def test_csv_export_is_a_download_with_a_header_row(self):
        response = self._client(email="v6@ravasco.com", role="viewer").get(
            "/api/mir-without-po?download=csv")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert "attachment" in response["Content-Disposition"]
        body = response.content.decode()
        assert body.splitlines()[0].startswith("Plant,Why it is unreconciled,MIR No")
        assert "Some Ordinary Supplier Pvt Ltd" in body

    def test_csv_export_escapes_a_formula_cell(self):
        """Material descriptions and vendor names come from spreadsheets
        plant staff edit by hand, and "open this in Excel" is the export's
        whole purpose - so the payload reaches its execution context by
        design. Same guard, same reason, as the stock-snapshot export.
        """
        HRSMIREntry.objects.create(
            source_row_ref="r3", mir_no="3/09", po_number_raw="",
            party_name="=cmd|' /C calc'!A0", material_description="Rubber", qty=1, net=1,
        )
        response = self._client(email="v7@ravasco.com", role="viewer").get(
            "/api/mir-without-po?download=csv")
        body = response.content.decode()
        # The cell survives, neutered: csv_safe() prefixes an apostrophe so
        # Excel reads it as text. Asserting the substring is simply absent
        # would be wrong - it is still there, and has to be, or the export
        # would silently lose a vendor name.
        assert "'=cmd" in body
        assert ",=cmd" not in body

    def test_sync_status_carries_the_same_bucket_counts(self):
        """The badge reads these off /sync-status and the panel reads the
        rows off this endpoint - a reader who clicks a badge saying 4 must
        not land on a tab saying 7. /sync-status serves its copy from a
        60-second per-plant cache (see _cached_mir_without_po_summary), so
        this also pins that the cached shape is the same shape."""
        from django.core.cache import cache

        cache.clear()
        client = self._client(email="v8@ravasco.com", role="viewer")
        status = client.get("/api/sync-status").data["mirWithoutPo"]
        rows = client.get("/api/mir-without-po").data["rows"]
        for key, bucket in status["buckets"].items():
            assert bucket["rowCount"] == sum(1 for r in rows if r["bucket"] == key), key

    def test_sync_status_does_not_recompute_the_summary_on_every_poll(self):
        """main.js's freshness watcher polls /sync-status every 60 seconds per
        selected plant. Recomputing this there costs 50-165ms per plant on
        live data, against an endpoint that otherwise answers in ~20-30ms -
        so a second poll inside the TTL must be served from the cache, not
        recomputed. Asserted by counting real calls rather than by timing,
        which would be flaky."""
        from unittest.mock import patch

        from django.core.cache import cache

        cache.clear()
        client = self._client(email="v9@ravasco.com", role="viewer")
        with patch("apps.api.routers._domestic_base.mir_without_po_summary",
                   return_value={"total": 0, "openTotal": 0, "buckets": {}, "internalTransfer": 0}) as spy:
            client.get("/api/sync-status")
            client.get("/api/sync-status")
        assert spy.call_count == 1
