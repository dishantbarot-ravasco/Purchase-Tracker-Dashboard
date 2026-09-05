"""
Read-only API surface for the HRS Purchase Tracker dashboard, plus write
endpoints (sync_trigger, correct_field, correct_material_field, dismiss_*).

This file is the "reference" plant of a deliberate three-way sibling set -
achhad_views.py and vapi_views.py are byte-for-byte-shaped ports of this
file's behavior (same function names, same response field shapes), not
accidental duplication. See CLAUDE.md's "Per-plant models, not a shared
schema" for why HRS/Achhad/Vapi get their own model classes, matching
modules, and routers instead of one shared, plant-discriminated table.

**Domestic router de-duplication (2026-09-05)**: the actual view logic used
to be duplicated near-verbatim across this file, achhad_views.py, and
vapi_views.py (confirmed byte-for-byte identical except model classes, which
matching module to call, and a few real schema differences). It now lives
once in `apps/api/routers/_domestic_base.py`, parametrized by a `_PlantConfig`
per plant - this file just builds HRS's config and re-exports the same-named
view functions urls.py already points at, so urls.py and every endpoint's
external behavior/URL/response shape are unchanged by this refactor, only
where the code physically lives. See _domestic_base.py's own module
docstring for what's shared vs. still genuinely per-plant.
"""

from apps.api.routers import _domestic_base as _base
from apps.core.models import (
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOLineItem,
    HRSPOMirMatch,
    HRSPurchaseOrder,
    HRSStockLot,
    HRSStockSnapshot,
    SyncRun,
)
from apps.services.matching import run_full_match

_CONFIG = _base._PlantConfig(
    key="hrs",
    syncrun_plant=SyncRun.Plant.HRS,
    po_model=HRSPurchaseOrder,
    item_model=HRSPOLineItem,
    mir_model=HRSMIREntry,
    po_mir_match_model=HRSPOMirMatch,
    mir_stock_match_model=HRSMirStockMatch,
    stock_lot_model=HRSStockLot,
    stock_snapshot_model=HRSStockSnapshot,
    run_full_match=run_full_match,
    # See hrs_views.py's previous version / CLAUDE.md's "Inline Edit
    # Everywhere" section for why quantity/value/date columns are
    # deliberately excluded from this set.
    material_editable_fields=frozenset({"description", "category", "sub_category", "uom", "basic_rate", "party_name"}),
    material_decimal_fields=frozenset({"basic_rate"}),
    material_rematch_trigger_fields=frozenset({"description", "party_name", "basic_rate"}),
    lot_rate_field="basic_rate",
    lot_code_field="sap_item_code",
    lot_vendor_field="party_name",
)

purchase_orders = _base.make_purchase_orders(_CONFIG)
correct_field = _base.make_correct_field(_CONFIG)
materials = _base.make_materials(_CONFIG)
correct_material_field = _base.make_correct_material_field(_CONFIG)
stock_trend = _base.make_stock_trend(_CONFIG)
sync_status = _base.make_sync_status(_CONFIG)
sync_trigger = _base.make_sync_trigger(_CONFIG)
dismiss_po_mir_match = _base.make_dismiss_po_mir_match(_CONFIG)
dismiss_mir_stock_match = _base.make_dismiss_mir_stock_match(_CONFIG)
dismiss_flag = _base.make_dismiss_flag(_CONFIG)
