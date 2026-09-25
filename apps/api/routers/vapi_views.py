"""
Read-only API surface for the RTP-Vapi Purchase Tracker dashboard, plus
sync_trigger/dismiss/correction write endpoints - byte-for-byte-shaped
sibling of apps/api/routers/hrs_views.py/achhad_views.py (same function
names, same response field shapes). See CLAUDE.md's "Per-plant models, not a
shared schema" for why HRS/Achhad/Vapi each get their own model set, and
"Domestic router de-duplication" for why the view/HTTP-layer logic itself
(as opposed to the genuinely different model classes) now lives once in
apps/api/routers/_domestic_base.py rather than being duplicated here.
Vapi's Stock sheet is lot-shaped with a real vendor column (`supplier_name`,
not HRS's `party_name`), like HRS's, not material-shaped like Achhad's -
see the RTP-Vapi section header comment in apps/core/models/.
"""

from apps.api.routers import _domestic_base as _base
from apps.core.models import (
    RTPVapiMIREntry,
    RTPVapiMirStockMatch,
    RTPVapiDomesticPOLineItem,
    RTPVapiPOMirMatch,
    RTPVapiDomesticPurchaseOrder,
    RTPVapiRMLot,
    RTPVapiRMSnapshot,
    SyncRun,
)
from apps.services.matching_vapi import MATCH_CONFIG, run_full_match

_CONFIG = _base._PlantConfig(
    key="vapi",
    syncrun_plant=SyncRun.Plant.RTP_VAPI,
    po_model=RTPVapiDomesticPurchaseOrder,
    item_model=RTPVapiDomesticPOLineItem,
    mir_model=RTPVapiMIREntry,
    po_mir_match_model=RTPVapiPOMirMatch,
    mir_stock_match_model=RTPVapiMirStockMatch,
    stock_lot_model=RTPVapiRMLot,
    stock_snapshot_model=RTPVapiRMSnapshot,
    run_full_match=run_full_match,
    # This plant's matcher config, read by the mir_without_po view -
    # see _domestic_base._PlantConfig.match_config for why the drill-down
    # borrows the matcher's own PO-number logic rather than re-deriving it.
    match_config=MATCH_CONFIG,
    # Same category/uom/basic_rate shape as HRS's, plus its own real vendor
    # column (supplier_name, not party_name). `batch_no` sits where HRS has
    # `sub_category` - the Vapi Stock sheet renamed that column in place on
    # 2026-09-12 (see RTPVapiRMLot.batch_no); sub_category is no longer
    # sourced, so it is no longer offered for correction either.
    material_editable_fields=frozenset({"description", "category", "batch_no", "uom", "basic_rate", "supplier_name"}),
    material_decimal_fields=frozenset({"basic_rate"}),
    material_rematch_trigger_fields=frozenset({"description", "supplier_name", "basic_rate"}),
    lot_rate_field="basic_rate",
    lot_code_field="hsn_code",
    lot_vendor_field="supplier_name",
)

purchase_orders = _base.make_purchase_orders(_CONFIG)
purchase_order_summary = _base.make_purchase_order_summary(_CONFIG)
correct_field = _base.make_correct_field(_CONFIG)
# Manual MIR pin (2026-09-21) - see _domestic_base.make_set_mir_match().
mir_candidates = _base.make_mir_candidates(_CONFIG)
set_mir_match = _base.make_set_mir_match(_CONFIG)
materials = _base.make_materials(_CONFIG)
correct_material_field = _base.make_correct_material_field(_CONFIG)
stock_trend = _base.make_stock_trend(_CONFIG)
stock_snapshot_dates = _base.make_stock_snapshot_dates(_CONFIG)
stock_snapshots_for_date = _base.make_stock_snapshots_for_date(_CONFIG)
export_stock_snapshots = _base.make_export_stock_snapshots(_CONFIG)
mir_without_po = _base.make_mir_without_po(_CONFIG)
sync_status = _base.make_sync_status(_CONFIG)
sync_trigger = _base.make_sync_trigger(_CONFIG)
dismiss_po_mir_match = _base.make_dismiss_po_mir_match(_CONFIG)
dismiss_mir_stock_match = _base.make_dismiss_mir_stock_match(_CONFIG)
dismiss_flag = _base.make_dismiss_flag(_CONFIG)
