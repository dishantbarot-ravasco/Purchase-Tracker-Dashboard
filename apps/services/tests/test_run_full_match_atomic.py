"""
run_full_match() is one transaction: a failure part-way through the pass
must roll back the writes it had already made, not leave the plant half
re-matched.

The pass deletes the matches of retired POs near its start and builds the
MIR<->Stock pool near its end. Making the pool raise therefore proves the
earlier delete was undone - without the transaction it would have been
committed already.
"""

import pytest

from apps.core.models import HRSDomesticPOLineItem, HRSDomesticPurchaseOrder, HRSMIREntry, HRSPOMirMatch
from apps.services import matching, matching_core


def _match_on_retired_po():
    po = HRSDomesticPurchaseOrder.objects.create(
        po_drive_folder_name="1000007777", po_number="1000007777", vendor_name="Test Vendor Ltd",
        is_active=False,
    )
    item = HRSDomesticPOLineItem.objects.create(
        purchase_order=po, item_id="1", description="Widget", qty="100", net_price="1.5", net_value="150",
    )
    mir = HRSMIREntry.objects.create(
        mir_no="MIR-9", party_name="Test Vendor Ltd", material_description="Widget",
        qty="100", rate="1.5", taxable_value="150", source_row_ref="90",
    )
    return HRSPOMirMatch.objects.create(po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9")


@pytest.mark.django_db
def test_a_completed_pass_removes_a_retired_pos_match():
    match = _match_on_retired_po()
    matching.run_full_match()
    assert not HRSPOMirMatch.objects.filter(pk=match.pk).exists()


@pytest.mark.django_db
def test_a_failed_pass_rolls_back_the_writes_it_already_made(monkeypatch):
    match = _match_on_retired_po()

    def boom(*args, **kwargs):
        raise RuntimeError("stock pool failed")

    monkeypatch.setattr(matching_core, "_StockLotPool", boom)
    with pytest.raises(RuntimeError, match="stock pool failed"):
        matching.run_full_match()

    assert HRSPOMirMatch.objects.filter(pk=match.pk).exists()
