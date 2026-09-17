"""
Unit tests for apps/services/parsers/po_csv.py's parse_po_csv() - pure
Python, no Django DB, same convention as test_parsers_common.py. Real
sync_*_po_csv command behaviour (idempotency, SyncRun bookkeeping) is
covered separately in test_sync_po_csv_pipeline.py; this file is about the
parser's own header/row-shape edge cases, which that file's docstring
explicitly defers to "a parser-level test if ever added". This is that test.

Real incident, 2026-09-17: HRS's live PO master CSV lost the trailing space
on its "Payment Terms" column header (an invisible edit - the file was
presumably just resaved in Excel/Sheets). parse_po_csv()'s header check
strips whitespace before comparing, so it accepted the drifted header, but
the per-row field lookups right below it were still keyed on the literal,
un-stripped EXPECTED_HEADER strings ("Payment Terms " with a trailing
space) - so every row raised `KeyError('Payment Terms ')`, and
sync_po_csv's blanket `except Exception` turned that into a FAILED SyncRun
with error_detail "'Payment Terms '", taking down that day's whole HRS PO
sync. The fix re-keys every row by its stripped header name once, so the
row lookups can never again disagree with what the header check already
accepted.
"""

import pytest

from apps.services.parsers.po_csv import EXPECTED_HEADER, HeaderMismatch, parse_po_csv

_HEADER_ROW = ",".join(f'"{h}"' for h in EXPECTED_HEADER)


def _row_csv(values: dict) -> str:
    """Builds a one-data-row CSV using EXPECTED_HEADER's own column order,
    substituting `values` (keyed on the STRIPPED column name) for any column
    the test cares about and leaving every other cell blank."""
    stripped = [h.strip() for h in EXPECTED_HEADER]
    cells = [str(values.get(h, "")) for h in stripped]
    return _HEADER_ROW + "\n" + ",".join(f'"{c}"' for c in cells)


class TestTrailingSpaceHeaderDrift:
    """The exact incident: one of the four trailing-space EXPECTED_HEADER
    columns loses its trailing space in the live file. This must now parse
    successfully rather than raising a KeyError - see this file's own
    module docstring."""

    def test_payment_terms_losing_its_trailing_space_still_parses(self):
        header = [h[:-1] if h == "Payment Terms " else h for h in EXPECTED_HEADER]
        csv_text = ",".join(f'"{h}"' for h in header) + "\n" + _row_csv({
            "PO Number": "3000001104", "Payment Terms": "60 Days",
        }).split("\n", 1)[1]
        orders = parse_po_csv(csv_text)
        assert orders[0].payment_terms == "60 Days"

    def test_every_trailing_space_column_survives_losing_its_space(self):
        """HSN, Delivery Date, Payment Terms, Currency all carry the same
        trailing-space quirk in EXPECTED_HEADER - the fix has to cover all
        four, not just the one that happened to fail first."""
        drifted = ["HSN ", "Delivery Date ", "Payment Terms ", "Currency "]
        header = [h[:-1] if h in drifted else h for h in EXPECTED_HEADER]
        csv_text = ",".join(f'"{h}"' for h in header) + "\n" + _row_csv({
            "PO Number": "3000001104", "HSN": "3926", "Delivery Date": "15-05-2026",
            "Payment Terms": "60 Days", "Currency": "USD",
        }).split("\n", 1)[1]
        orders = parse_po_csv(csv_text)
        order = orders[0]
        assert order.payment_terms == "60 Days"
        assert order.currency == "USD"
        assert order.items[0].hsn == "3926"
        assert order.items[0].delivery_date is not None

    def test_a_genuinely_missing_column_still_raises_header_mismatch(self):
        """The fix must not paper over a REAL schema change - dropping a
        column entirely (not just trimming whitespace on one) still has to
        fail loudly rather than silently parsing with a missing field."""
        header = [h for h in EXPECTED_HEADER if h != "Payment Terms "]
        csv_text = ",".join(f'"{h}"' for h in header) + "\n" + ",".join('""' for _ in header)
        with pytest.raises(HeaderMismatch):
            parse_po_csv(csv_text)


class TestOrdinaryParsing:
    def test_the_unchanged_expected_header_still_parses(self):
        """Baseline: today's real, unmodified header must keep working -
        confirms the re-keying fix is a no-op when there's no drift."""
        csv_text = _row_csv({
            "PO Number": "3000001104", "Vendor Name": "Atul Limited",
            "Payment Terms": "60 Days", "Currency": "INR", "QTY": "100",
        })
        orders = parse_po_csv(csv_text)
        assert len(orders) == 1
        assert orders[0].po_number == "3000001104"
        assert orders[0].vendor_name == "Atul Limited"
        assert orders[0].payment_terms == "60 Days"

    def test_a_blank_currency_still_defaults_to_inr(self):
        csv_text = _row_csv({"PO Number": "3000001104", "Currency": ""})
        assert parse_po_csv(csv_text)[0].currency == "INR"

    def test_a_blank_po_number_row_is_skipped(self):
        csv_text = _row_csv({"PO Number": ""})
        assert parse_po_csv(csv_text) == []

    def test_two_rows_same_po_number_group_under_one_order(self):
        stripped = [h.strip() for h in EXPECTED_HEADER]

        def row(item_id):
            vals = {h: "" for h in stripped}
            vals["PO Number"] = "3000001104"
            vals["Item Id"] = item_id
            return ",".join(f'"{vals[h]}"' for h in stripped)

        csv_text = "\n".join([_HEADER_ROW, row("ITEM1"), row("ITEM2")])
        orders = parse_po_csv(csv_text)
        assert len(orders) == 1
        assert [i.item_id for i in orders[0].items] == ["ITEM1", "ITEM2"]
