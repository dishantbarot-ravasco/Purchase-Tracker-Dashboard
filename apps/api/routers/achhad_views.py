"""
Read-only API surface for the RTP-Achhad Purchase Tracker dashboard, plus
sync_trigger/dismiss/correction write endpoints - byte-for-byte-shaped
sibling of apps/api/routers/hrs_views.py/vapi_views.py (same function names,
same response field shapes). See CLAUDE.md's "Per-plant models, not a shared
schema" for why HRS/Achhad/Vapi each get their own model set, and "Domestic
router de-duplication" for why the view/HTTP-layer logic itself (as opposed
to the genuinely different model classes) now lives once in
apps/api/routers/_domestic_base.py rather than being duplicated here.

Achhad is the plant with the most schema divergence from HRS/Vapi: no
vendor column on Stock (RTPAchhadRMLot has no party_name/supplier_name
field at all), no sub_category/uom columns either, and a real `msl`
(Minimum Stock Level) field the other two plants have no equivalent of -
see _CONFIG below and RTPAchhadRMLot's own docstring in apps/core/models/.
"""

from apps.api.routers import _domestic_base as _base
from apps.core.models import (
    RTPAchhadMIREntry,
    RTPAchhadMirStockMatch,
    RTPAchhadDomesticPOLineItem,
    RTPAchhadPOMirMatch,
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadRMDailyMovement,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
    SyncRun,
)
from apps.services.matching_achhad import MATCH_CONFIG, run_full_match

_CONFIG = _base._PlantConfig(
    key="achhad",
    syncrun_plant=SyncRun.Plant.RTP_ACHHAD,
    po_model=RTPAchhadDomesticPurchaseOrder,
    item_model=RTPAchhadDomesticPOLineItem,
    mir_model=RTPAchhadMIREntry,
    po_mir_match_model=RTPAchhadPOMirMatch,
    mir_stock_match_model=RTPAchhadMirStockMatch,
    stock_lot_model=RTPAchhadRMLot,
    stock_snapshot_model=RTPAchhadRMSnapshot,
    run_full_match=run_full_match,
    # This plant's matcher config, read by the mir_without_po view -
    # see _domestic_base._PlantConfig.match_config for why the drill-down
    # borrows the matcher's own PO-number logic rather than re-deriving it.
    match_config=MATCH_CONFIG,
    # Achhad's own daily Recp./Issue matrix. None for HRS/Vapi (no
    # equivalent in their Stock files). The read path no longer touches it -
    # consumption_ledger._dated_movements() reconciles it into the ledger at
    # build time - but _PlantConfig still carries it for the sync layer.
    daily_movement_model=RTPAchhadRMDailyMovement,
    # Genuinely differs from HRS's/Vapi's set, not a copy-paste: no
    # sub_category/uom/vendor field at all, and a real msl column neither
    # other plant has.
    material_editable_fields=frozenset({"description", "category", "rate", "msl"}),
    material_decimal_fields=frozenset({"rate", "msl"}),
    # Achhad's MIR<->Stock match gates on material description alone (no
    # vendor column to also gate on) - msl never feeds matching.
    material_rematch_trigger_fields=frozenset({"description", "rate"}),
    lot_rate_field="rate",
    lot_code_field="sap_code",
    lot_vendor_field=None,
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
