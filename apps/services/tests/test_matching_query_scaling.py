"""
Regression tests for the two quadratic N+1 queries in `run_full_match()`,
found and fixed during the 2026-09-15 audit pass.

THE GAP
-------
Two full-table SELECTs were issued inside per-row loops:

  `_candidate_mir_entries()`   loaded the ENTIRE MIR table, once per PO line
                               item  ->  O(line items x MIR rows)
  `match_mir_entry_stock()`    loaded the ENTIRE Stock table, once per active
                               MIR entry  ->  O(MIR rows x stock lots)

plus a stale-row cleanup DELETE fired once per MIR entry, almost always
matching nothing.

Measured against the real dev database on HRS - the SMALLEST of the three
plants (120 domestic line items, 474 active MIR rows, 195 stock lots):

    before:  2,035 queries, 9.36 s
    after:     972 queries, 0.81 s     (11.5x faster, byte-identical results)

The absolute numbers mattered less than the shape: cost grew with the PRODUCT
of two independently growing tables, in a pipeline that runs 12x a day across
3 plants. Ten times the data meant roughly a hundred times the work.

THE FIX
-------
`_MirCandidateIndex` and `_StockLotPool` fetch once per pass and are threaded
through the batch loop; the cleanup DELETE is batched into one. All three are
pure caching - no scoring, gating or ordering logic changed. Verified by
dumping every resulting match row (86 PO<->MIR with 17 fields each, 145
MIR<->Stock with 10 each) before and after against the real dev DB and
diffing: byte-identical.

WHAT THESE TESTS LOCK IN
------------------------
`test_query_count_does_not_scale_with_row_count` is the real guard. Rather
than asserting a magic number (which would need editing on every unrelated
change and would eventually just be bumped), it runs the SAME pipeline against
two datasets of different sizes and asserts the query count barely moves. An
N+1 reintroduced anywhere in this path fails it by construction, because an
N+1 is precisely "query count grows with row count".
"""

import pytest

from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSRMLot,
)
from apps.services.matching import run_full_match

VENDOR = "Scaling Test Chemicals Pvt Ltd"


def _seed(n_pos, n_mir, n_lots):
    """Build a small but realistic dataset. Values are deliberately close
    enough to match so the pipeline does real work rather than bailing out
    early on an empty candidate pool."""
    for i in range(n_pos):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_number=f"SCALE-PO-{i:04d}",
            po_drive_folder_name=f"SCALE-PO-{i:04d}",
            vendor_name=VENDOR,
        )
        HRSDomesticPOLineItem.objects.create(
            purchase_order=po,
            item_id="1",
            description=f"SCALING TEST MATERIAL {i}",
            qty="100",
            uom="KG",
            net_price="10.00",
            net_value="1000.00",
        )
    for i in range(n_mir):
        HRSMIREntry.objects.create(
            source_row_ref=f"scale-mir-{i}",
            party_name=VENDOR,
            material_description=f"SCALING TEST MATERIAL {i}",
            qty="100",
            uom="KG",
            rate="10.00",
            taxable_value="1000.00",
            is_active=True,
        )
    for i in range(n_lots):
        HRSRMLot.objects.create(
            source_row_ref=f"scale-lot-{i}",
            description=f"SCALING TEST MATERIAL {i}",
            party_name=VENDOR,
            basic_rate="10.00",
            is_active=True,
        )


@pytest.mark.django_db
def test_query_count_does_not_scale_with_row_count(django_assert_num_queries):
    """The core anti-N+1 guard.

    Quadrupling every table must not meaningfully multiply the query count.
    Some growth is expected and legitimate - each MATCHED pair costs its own
    upsert, which is real per-row work, not an N+1. What must NOT happen is
    the per-row *lookup* queries returning.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _seed(n_pos=3, n_mir=3, n_lots=3)
    with CaptureQueriesContext(connection) as small:
        run_full_match()
    small_n = len(small.captured_queries)

    _seed_offset = 3
    for i in range(_seed_offset, 12):
        po = HRSDomesticPurchaseOrder.objects.create(
            po_number=f"SCALE-PO-{i:04d}",
            po_drive_folder_name=f"SCALE-PO-{i:04d}",
            vendor_name=VENDOR,
        )
        HRSDomesticPOLineItem.objects.create(
            purchase_order=po, item_id="1",
            description=f"SCALING TEST MATERIAL {i}",
            qty="100", uom="KG", net_price="10.00", net_value="1000.00",
        )
        HRSMIREntry.objects.create(
            source_row_ref=f"scale-mir-{i}", party_name=VENDOR,
            material_description=f"SCALING TEST MATERIAL {i}",
            qty="100", uom="KG", rate="10.00", taxable_value="1000.00", is_active=True,
        )
        HRSRMLot.objects.create(
            source_row_ref=f"scale-lot-{i}", description=f"SCALING TEST MATERIAL {i}",
            party_name=VENDOR, basic_rate="10.00", is_active=True,
        )

    with CaptureQueriesContext(connection) as large:
        run_full_match()
    large_n = len(large.captured_queries)

    # 4x the rows. Per-pair upserts legitimately scale, so allow generous
    # headroom - but an N+1 would multiply the LOOKUPS too and blow past it.
    # With the bug present this ratio was ~4x; with the fix it is well under 3x.
    growth = large_n / max(small_n, 1)
    assert growth < 3.0, (
        f"query count grew {growth:.1f}x for a 4x dataset ({small_n} -> {large_n}) - "
        f"this is the signature of a reintroduced N+1. Check that "
        f"_MirCandidateIndex/_StockLotPool are still threaded through "
        f"run_full_match()'s loops."
    )


@pytest.mark.django_db
def test_full_table_selects_are_issued_once_per_pass_not_per_row():
    """Directly pins the two specific queries that were the N+1s: the MIR
    candidate pool and the Stock lot pool must each be SELECTed a small,
    constant number of times regardless of how many rows exist."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _seed(n_pos=10, n_mir=10, n_lots=10)

    with CaptureQueriesContext(connection) as ctx:
        run_full_match()

    def count_selects_from(table):
        return sum(
            1 for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT") and f'FROM "{table}"' in q["sql"]
        )

    mir_selects = count_selects_from("core_hrsmirentry")
    lot_selects = count_selects_from("core_hrsrmlot")

    # A handful each (candidate pool, the IDF corpus, known-PO set, the
    # active-entry iteration) - emphatically NOT one per line item / per entry.
    assert mir_selects <= 6, (
        f"core_hrsmirentry SELECTed {mir_selects} times for 10 line items - "
        f"_MirCandidateIndex is not being shared across the batch loop"
    )
    assert lot_selects <= 3, (
        f"core_hrsrmlot SELECTed {lot_selects} times for 10 MIR entries - "
        f"_StockLotPool is not being shared across the batch loop"
    )


@pytest.mark.django_db
def test_single_entry_callers_still_work_without_a_pool():
    """The pool/index parameters are optional on purpose: `match_mir_entry_stock`
    is re-exported by name from matching.py / matching_achhad.py /
    matching_vapi.py and may be called for one entry at a time. Omitting the
    pool must behave exactly as before."""
    from apps.services.matching import match_mir_entry_stock

    _seed(n_pos=1, n_mir=1, n_lots=1)
    entry = HRSMIREntry.objects.get(source_row_ref="scale-mir-0")

    matches = match_mir_entry_stock(entry)

    assert len(matches) == 1, "single-entry matching regressed"
    assert matches[0].stock_lot.description == "SCALING TEST MATERIAL 0"


@pytest.mark.django_db
def test_stale_stock_matches_are_still_deleted():
    """The cleanup DELETE was batched, not dropped - a match that no longer
    identifies must still disappear."""
    from apps.core.models import HRSMirStockMatch

    _seed(n_pos=1, n_mir=1, n_lots=1)
    run_full_match()
    assert HRSMirStockMatch.objects.count() == 1

    # Rename the lot so it no longer identifies against the MIR entry.
    lot = HRSRMLot.objects.get(source_row_ref="scale-lot-0")
    lot.description = "SOMETHING COMPLETELY DIFFERENT"
    lot.save(update_fields=["description"])

    run_full_match()
    assert HRSMirStockMatch.objects.count() == 0, (
        "the now-stale MIR<->Stock match survived - batching the cleanup "
        "DELETE must not have stopped it running"
    )
