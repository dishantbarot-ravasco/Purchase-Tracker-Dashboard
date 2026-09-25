"""
API surface for the Import Purchase Dashboard - unlike the domestic
hrs_views.py/achhad_views.py/vapi_views.py trio (one plant per URL prefix),
this dashboard is cross-plant by design (spec: "Combine into a single
dataset with an added Plant field", global Plant filter) so it gets one
shared views module parametrized by a `plant` URL segment instead of three
near-duplicate router files. This is a deliberate deviation from the
per-plant-router convention documented in CLAUDE.md - justified there
because MIR/Stock genuinely differ in shape per plant; here all three
plants' Import CSVs are byte-for-byte identical (confirmed live
2026-09-04), so there is no real per-plant divergence to protect against by
splitting the read/derive logic into three copies.

Every read view requires authentication (any role). The PATCH correction
endpoint requires IsEditor (admin/editor) - a viewer can look but not touch,
same role split IsEditor's own docstring describes for a "real mutating
endpoint" beyond what this app has had until now (see apps/core/audit_log.py's
docstring on this app previously being read-only; this endpoint is the first
exception, deliberately, per the build spec's inline-correction requirement).
"""

import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from apps.api.permissions import IsAdmin, IsEditor, SyncTriggerThrottle, user_can_access_plant, user_can_edit_plant
from apps.core.models import (
    AdvanceLicense,
    FlagDismissal,
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
    HRSImportPurchaseOrder,
    ImportPOCorrection,
    ManualMirMatch,
    RTPAchhadImportPOLineItem,
    RTPAchhadImportPOMirMatch,
    RTPAchhadImportPurchaseOrder,
    RTPVapiImportPOLineItem,
    RTPVapiImportPOMirMatch,
    RTPVapiImportPurchaseOrder,
    RodtepScrollEntry,
    RodtepUsage,
    SyncRun,
)
from apps.api.routers._domestic_base import (
    _category_reference_map,
    _candidate_rows,
    _claims_for_mir_numbers,
    _counted_mirs,
    _held_mir_numbers,
    _match_config_for,
    _mir_row_dict,
    _po_material_categories,
    _request_bool,
    _sheet_row,
)
# Each plant's own MIR model, for the manual-MIR-match picker. Imports
# reconcile against the SAME MIR table as that plant's domestic POs (see
# HRSImportPOMirMatch's docstring) - this router simply never had cause to
# read it directly before.
from apps.core.models import HRSMIREntry, RTPAchhadMIREntry, RTPVapiMIREntry
# The matcher's own line numbering, imported rather than re-derived so the
# API and matching_core can never disagree about what a pin addresses.
from apps.services.matching_core import _boe_key, _import_rate_value_inr, import_landed_rates, line_item_positions
from apps.services.parsers.common import normalize_material
from apps.services import bl_tracking, data_stamp
from apps.services import import_flags as flags
from apps.services import license_links
from apps.services.flag_dismiss import dismiss_po_flag
from apps.services.match_dismiss import dismiss_match
from apps.services.matching import run_full_match as _hrs_run_full_match
from apps.services.matching_achhad import run_full_match as _achhad_run_full_match
from apps.services.matching_vapi import run_full_match as _vapi_run_full_match
from apps.services.sync_trigger import (
    is_advance_license_sync_in_progress,
    is_imports_sync_in_progress,
    is_rodtep_sync_in_progress,
    trigger_plant_imports_sync,
)

# plant URL segment -> that plant's run_full_match() - re-run synchronously
# by correct_field() below when an import line item edit could change its
# MIR match outcome, same reasoning as hrs_views.py's/achhad_views.py's/
# vapi_views.py's own _REMATCH_TRIGGER_FIELDS. run_full_match() covers both
# domestic and import PO line items in one idempotent pass (see
# matching.py's own docstring), so this doesn't disturb domestic matches.
_RUN_FULL_MATCH = {"hrs": _hrs_run_full_match, "achhad": _achhad_run_full_match, "vapi": _vapi_run_full_match}
_MIR_MODEL = {"hrs": HRSMIREntry, "achhad": RTPAchhadMIREntry, "vapi": RTPVapiMIREntry}

# Editing any of these can change an import line item's PO<->MIR match
# outcome (vendor/material text used for candidate gating, or a value
# compared against MIR) - mirrors _REMATCH_TRIGGER_FIELDS in hrs_views.py/
# achhad_views.py/vapi_views.py, with qty_as_per_boe in place of domestic's
# single `qty` field (see HRSImportPOMirMatch's docstring for why BOE qty,
# not PO qty, is the one compared against MIR).
# exchange_rate, boe_number, bill_of_lading_number and total_inclusive_value
# joined 2026-09-25: the matcher converts to INR with the first, pairs
# receipts by the second, gates that pairing on the third and checks the
# landed rate with the fourth - so a correction to any of them changes a
# match, and without a re-match it sat invisible until the next sync.
_REMATCH_TRIGGER_FIELDS = {
    "vendor_name", "vendor_gstin", "description", "qty_as_per_boe", "net_price", "net_value",
    "exchange_rate", "boe_number", "bill_of_lading_number", "total_inclusive_value",
}

# plant URL segment -> (PO model, line item model, SyncRun.Plant, display label, import PO<->MIR match model)
_PLANTS = {
    "hrs": (HRSImportPurchaseOrder, HRSImportPOLineItem, SyncRun.Plant.HRS, "HRS-Silvassa", HRSImportPOMirMatch),
    "achhad": (RTPAchhadImportPurchaseOrder, RTPAchhadImportPOLineItem, SyncRun.Plant.RTP_ACHHAD, "RTP-Achhad", RTPAchhadImportPOMirMatch),
    "vapi": (RTPVapiImportPurchaseOrder, RTPVapiImportPOLineItem, SyncRun.Plant.RTP_VAPI, "RTP-Vapi", RTPVapiImportPOMirMatch),
}

# Every field a pencil icon in the mockup can edit. PO-level fields are
# stored on the PO model; item-level fields need an itemId in the PATCH body.
# Identity fields (po_number, po_drive_folder_name, item_id) are deliberately
# absent - those come from the sync, not a manual correction.
_PO_EDITABLE_FIELDS = {
    "po_created_date", "vendor_name", "vendor_address", "vendor_gstin", "vendor_email", "vendor_code",
    "billing_address", "ship_to", "payment_terms", "incoterms", "currency", "total_value", "remarks",
}
_ITEM_EDITABLE_FIELDS = {
    "description", "hsn", "qty_as_per_po", "qty_as_per_boe", "uom", "delivery_date", "net_price", "net_value",
    "tax_type", "currency_after_taxes", "exchange_rate", "total_inclusive_value", "boe_number",
    "bill_of_lading_number", "laden_on_board_date", "country_of_origin", "license_type", "license_number",
}
_DATE_FIELDS = {"po_created_date", "delivery_date", "laden_on_board_date"}
_DECIMAL_FIELDS = {
    "total_value", "qty_as_per_po", "qty_as_per_boe", "net_price", "net_value", "exchange_rate",
    "total_inclusive_value",
}


# ── Serialization helpers ─────────────────────────────────────────────────────

def _f(v):
    return float(v) if v is not None else None


def _d(v):
    return v.isoformat() if v else None


def _mir_match_dict(item):
    """MIR-match info for one import line item, mirroring the domestic
    dashboards' matchStatusHtml() payload shape (matchScore/tier/diff
    percentages) - see HRSImportPOMirMatch's docstring for why this is
    matched against the same MIR table domestic PO line items use.
    `stockMatched` is true only when the specific MIR entry this item
    matched to itself has a real MIR<->Stock match, same "real per-line-item
    chain" semantics as domestic's own stockMatched field, not just "this
    material exists somewhere in stock." Returns None (no `mir_match`
    payload at all) when the item never crossed MATCH_THRESHOLD - same as
    a domestic line item with no match."""
    match = getattr(item, "mir_match", None)
    if match is None:
        return None
    counted_mirs, received = _counted_mirs(match, item.uom)
    ordered_rate_inr, ordered_value_inr = _import_rate_value_inr(item)
    landed_ordered, landed_received = import_landed_rates(
        _match_config_for(match), item, list(match.group_entries.all()) or [match.mir_entry],
    )
    return {
        "matchId": match.id,
        "tier": match.tier,
        "matchScore": float(match.match_score),
        # Every MIR receipt this match counted and what they total, in this
        # line's own unit - see _domestic_base._counted_mirs().
        "matchedMirs": counted_mirs,
        "received": received,
        # The ordered side in INR, exactly as the matcher compares it (MIR is
        # always INR) - see matching_core._import_rate_value_inr().
        "orderedRateInr": _f(ordered_rate_inr),
        "orderedValueInr": _f(ordered_value_inr),
        # The landed-rate pair the rate check also accepts (2026-09-25): the
        # CSV's landed value per BOE unit (duty + IGST in) against MIR's
        # final value per unit received. Null until the line is cleared. See
        # matching_core._landed_rate_diff().
        "landedRateInr": _f(landed_ordered),
        "receivedFinalRate": _f(landed_received),
        # Exchange-rate difference (2026-09-25): the rate MIR's receipt
        # implies and whether the rate gap is that difference rather than a
        # price one - see matching_core._exchange_rate_explains().
        "mirExchangeRate": _f(getattr(match, "mir_exchange_rate", None)),
        "exchangeRateMismatched": bool(getattr(match, "exchange_rate_mismatched", False)),
        # This line's share of a receipt covering several lines of its BOE,
        # or null when it counts its receipts in full.
        "receiptShare": _f(getattr(match, "receipt_share", None)),
        "qtyDiffPct": _f(match.qty_diff_pct),
        # Over vs under delivery (2026-09-18). True = more received than
        # ordered, false = less, null = no quantity comparison was possible.
        # Null is NOT the same as false, so this passes the three states
        # through rather than coercing to a boolean - the frontend needs to
        # tell "under-delivered" from "we could not tell".
        "qtyOverDelivered": getattr(match, "qty_over_delivered", None),
        "rateDiffPct": _f(match.rate_diff_pct),
        "valueDiffPct": _f(match.value_diff_pct),
        "isFlagged": match.is_flagged,
        # Match Accuracy Programme fixes 2.C/3.F - see matching_core.py and
        # _domestic_base.py's _line_item_dict() (same two fields, same
        # reasoning - the frontend must not re-derive a value flag from
        # valueDiffPct alone, that would ignore the value epsilon).
        "uomMismatch": match.uom_mismatch,
        "severity": match.severity,
        "dismissedByOverride": match.dismissed_by_override,
        "dismissedReason": match.dismissed_reason,
        # Who dismissed it - the reconciliation card's "dismissed by"
        # tooltip read it, and it was never sent. Prefetched with the match.
        "dismissedBy": match.dismissed_by.email if match.dismissed_by_id and match.dismissed_by else None,
        # Relies on purchase_orders()'s prefetch_related including
        # "items__mir_match__mir_entry__stock_matches" so this reads the
        # prefetch cache, not a new query per line item - same pattern as
        # hrs_views.py's/achhad_views.py's/vapi_views.py's own _line_item_dict().
        "stockMatched": len(match.mir_entry.stock_matches.all()) > 0,
        # Identification/Financial-Check redesign (2026-09, imports - HRS/
        # Achhad only for now, see HRSImportPOMirMatch's docstring). Read via
        # getattr() with a default rather than a direct attribute access -
        # this router is shared across all 3 plants and RTPVapiImportPOMirMatch
        # has none of these columns at all (project owner: "keep vapi out for
        # now"), so a Vapi match just reports these as never-set/false instead
        # of raising AttributeError. Same key names/shapes as
        # _domestic_base.py's _line_item_dict() for frontend consistency.
        "materialMatched": bool(getattr(match, "material_matched", False)),
        "poNumberMatched": bool(getattr(match, "po_number_matched", False)),
        # vendorMatched is FALSE only on a plant running identification
        # 2-of-3 (Achhad, 2026-09-18) - everywhere else vendor is still the
        # mandatory gate, so a match cannot exist without it and this is
        # always True. False means the match was identified by its PO number
        # and material while the party name disagreed: a real name error at
        # source, surfaced as its own Data Quality Flag rather than silently
        # accepted. Defaults to True (not False) for a plant/row that has no
        # such column, so the flag never fires where the concept does not
        # apply.
        "vendorMatched": bool(getattr(match, "vendor_matched", True)),
        "qtyMismatched": bool(getattr(match, "qty_mismatched", False)),
        "rateMismatched": bool(getattr(match, "rate_mismatched", False)),
        "dataMismatch": bool(getattr(match, "data_mismatch", False)),
        "taxTypeMismatch": bool(getattr(match, "tax_type_mismatch", False)),
        "taxableValueDiffPct": (
            float(match.taxable_value_diff_pct) if getattr(match, "taxable_value_diff_pct", None) is not None else None
        ),
        "finalValueDiffPct": (
            float(match.final_value_diff_pct) if getattr(match, "final_value_diff_pct", None) is not None else None
        ),
        # Added 2026-09-08 (Data Quality Flags clarity pass) - see
        # _domestic_base.py's _line_item_dict() for the same fields.
        "netValueMismatched": bool(getattr(match, "net_value_mismatched", False)),
        "taxableValueMismatched": bool(getattr(match, "taxable_value_mismatched", False)),
        "finalValueMismatched": bool(getattr(match, "final_value_mismatched", False)),
    }


def _item_dict(item, item_ref="", boe_total=None):
    # boe_total: the line's BOE qty to judge its order against - a split
    # shipment's summed rows (import_flags.boe_totals()).
    is_disc, disc_pct = flags.qty_discrepancy(item, boe_total)
    match = getattr(item, "mir_match", None)
    return {
        "itemId": item.item_id,
        # The line's address for a manual MIR pin (matching_core's
        # line_item_positions()). Import line items DO carry a real item_id,
        # unlike domestic ones, but the sync deletes and recreates every line
        # on any change just the same - so a pin uses the same position-based
        # ref at both, rather than two rules to keep straight.
        "itemRef": item_ref,
        "manuallyPinned": bool(match and getattr(match, "manually_pinned", False)),
        "description": item.description,
        "hsn": item.hsn,
        "qtyAsPerPo": _f(item.qty_as_per_po),
        "qtyAsPerBoe": _f(item.qty_as_per_boe),
        "uom": item.uom,
        "deliveryDate": _d(item.delivery_date),
        "deliveryDateRaw": item.delivery_date_raw,
        "netPrice": _f(item.net_price),
        "netValue": _f(item.net_value),
        "taxType": item.tax_type,
        "currencyAfterTaxes": item.currency_after_taxes,
        "exchangeRate": _f(item.exchange_rate),
        "totalInclusiveValue": _f(item.total_inclusive_value),
        "boeNumber": item.boe_number,
        "billOfLadingNumber": item.bill_of_lading_number,
        "ladenOnBoardDate": _d(item.laden_on_board_date),
        "countryOfOrigin": item.country_of_origin,
        "licenseType": item.license_type,
        "licenseNumber": item.license_number,
        "shipmentStage": flags.shipment_stage(item),
        "qtyDiscrepancy": is_disc,
        "qtyDiscrepancyPct": disc_pct,
        "deliveryDateStatus": flags.delivery_date_status(item, timezone.localdate(), boe_total),
        "mirMatch": _mir_match_dict(item),
    }


def _correction_dict(c):
    return {
        "fieldName": c.field_name,
        "itemId": c.item_id,
        "itemRef": c.item_ref,
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


def _po_dict(po, plant_key, plant_label, detail=False, sr_plant=None, category_reference=None):
    items = list(po.items.all())
    today = timezone.localdate()
    # Numbered per PO in pk order - the master CSV's own row order, and the
    # same numbering the matcher uses to resolve a manual MIR pin.
    ordered_items = sorted(items, key=lambda x: x.id)
    totals = flags.boe_totals(items)
    item_dicts = [_item_dict(i, str(n), totals.get(id(i))) for n, i in enumerate(ordered_items)]
    # Each line's own canonical category, looked up exactly as a Stock lot's
    # is. Raw Material Analysis now counts open import lines (2026-09-24), and
    # an import-only material gets a row of its own there that can sit in the
    # right Category filter only if it knows its category - materialCategories
    # below is de-duplicated per PO, so it cannot say which line holds which.
    # Same reasoning as _domestic_base._categorized_line_item_dict().
    for item_dict, item in zip(item_dicts, ordered_items, strict=True):
        ref = (category_reference or {}).get(normalize_material(item.description))
        item_dict["category"] = ref.category if ref else "Uncategorized"
        item_dict["subCategory"] = ref.subcategory if ref else ""
    total_incl_value = sum((i.total_inclusive_value or 0) for i in items)
    # PO-list rows show one BL/country - real POs in the live data ship as
    # one BOE per PO today, so "first item with a value" is representative;
    # the Items tab in the detail payload always shows every item's own value.
    bl_number = next((i.bill_of_lading_number for i in items if i.bill_of_lading_number), "")
    country = next((i.country_of_origin for i in items if i.country_of_origin), "")

    d = {
        "plant": plant_key,
        "plantLabel": plant_label,
        "poNumber": po.po_number,
        "poDriveFolderName": po.po_drive_folder_name,
        "vendorName": po.vendor_name,
        "createdDate": _d(po.po_created_date),
        "countryOfOrigin": country,
        "totalInclusiveValue": float(total_incl_value) if total_incl_value else 0.0,
        "billOfLadingNumber": bl_number,
        "shipmentStage": flags.po_shipment_stage(items),
        # Receipt-based, from each line's MIR match (import_flags.py) - the
        # same rules Domestic's status buckets use, not customs clearance.
        "deliveryDateStatus": flags.po_delivery_date_status(items, today),
        "partialDelivery": flags.partial_delivery(items),
        "materialInwarded": flags.material_inwarded(items),
        "qtyDiscrepancy": flags.po_has_qty_discrepancy(items),
        "dataQualityFlags": flags.po_flags(po.po_number, items),
        # Always included (not detail-only) - the combined list is the only
        # payload the dashboard's charts/KPIs read from (spec's charts need
        # per-item qty-discrepancy-pct, HSN, per-item country, etc.), and the
        # dataset is small enough (tens of POs today) that duplicating this
        # into every list row costs nothing worth optimizing away.
        "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms,
        "items": item_dicts,
        # Same canonical Category/Sub Category lookup the domestic Purchase
        # Orders page uses (see _domestic_base.py's _po_material_categories
        # docstring) - added 2026-09-07 so Import POs' own "Filter by
        # Category"/"Sub Category" dropdowns mean material data too, not
        # just the domestic page.
        "materialCategories": _po_material_categories(items, category_reference),
    }
    if detail:
        corrections = (
            ImportPOCorrection.objects.filter(plant=sr_plant, po_number=po.po_number)
            if sr_plant else ImportPOCorrection.objects.none()
        )
        flag_dismissals = (
            FlagDismissal.objects.filter(plant=sr_plant, po_number=po.po_number)
            if sr_plant else FlagDismissal.objects.none()
        )
        d.update({
            "vendorAddress": po.vendor_address,
            "vendorGstin": po.vendor_gstin,
            "vendorEmail": po.vendor_email,
            "vendorCode": po.vendor_code,
            "billingAddress": po.billing_address,
            "shipTo": po.ship_to,
            "currency": po.currency,
            "totalValue": _f(po.total_value),
            "remarks": po.remarks,
            "corrections": [_correction_dict(c) for c in corrections],
            "flagDismissals": [_flag_dismissal_dict(fd) for fd in flag_dismissals],
        })
    return d


def _resolve_plant(plant_key):
    return _PLANTS.get(plant_key)


# ── Purchase order list/detail ────────────────────────────────────────────────

@api_view(["GET"])
def purchase_orders(request):
    """GET /api/imports/purchase-orders - all three plants combined, list
    shape (see _po_dict's non-detail fields). Spec section 1's "single
    dataset with a Plant field". Two of three plants have 0 rows today;
    that's fine, they just contribute nothing to the combined list rather
    than erroring. IsAuthenticated by default (any role) - additionally
    narrowed per-plant by user_can_access_plant() (added 2026-09-05,
    hardening pass): a plant the caller isn't scoped to is silently
    excluded from the combined list rather than erroring the whole
    request, same "you see less, not an error" shape as the rest of this
    filter already has for plants with zero rows."""
    category_reference = _category_reference_map()
    result = []
    for plant_key, (po_model, _item_model, _sr_plant, label, _match_model) in _PLANTS.items():
        if not user_can_access_plant(request.user, plant_key):
            continue
        # is_active=True - see _domestic_base.py's own note.
        qs = po_model.objects.filter(is_active=True).prefetch_related(
            "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches", "items__mir_match__group_entries", "items__mir_match__dismissed_by",
        )
        result.extend(_po_dict(po, plant_key, label, category_reference=category_reference) for po in qs)
    result.sort(key=lambda d: d["createdDate"] or "", reverse=True)
    return Response({"purchaseOrders": result})


@api_view(["GET"])
def purchase_order_detail(request, plant, po_number):
    """GET /api/imports/purchase-orders/<plant>/<po_number> - one PO, detail
    shape (adds vendor/billing fields, corrections, flag dismissals - see
    _po_dict's detail=True branch). 404s on an unknown `plant` segment or a
    po_number that doesn't exist for that plant."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    # 404, not 403 - matches the "unknown plant" response above rather than
    # confirming a PO exists for a plant the caller isn't scoped to. See
    # apps/api/permissions.py's user_can_access_plant() docstring.
    if not user_can_access_plant(request.user, plant):
        return Response({"error": "Unknown plant."}, status=404)
    po_model, _item_model, sr_plant, label, _match_model = resolved
    po = po_model.objects.prefetch_related(
        "items", "items__mir_match", "items__mir_match__mir_entry", "items__mir_match__mir_entry__stock_matches", "items__mir_match__group_entries", "items__mir_match__dismissed_by",
    # is_active=True: a retired order is gone from the list, so it must not
    # stay reachable by URL either - same as the domestic detail view.
    ).filter(po_number=po_number, is_active=True).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)
    return Response(_po_dict(po, plant, label, detail=True, sr_plant=sr_plant, category_reference=_category_reference_map()))


# ── Inline field corrections ──────────────────────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def correct_field(request, plant, po_number):
    """Inline-edit endpoint backing every pencil icon in the detail modal.
    Body: {"itemId": "<optional>", "field": "<model field name>", "value": "<new value>"}.
    Writes the new value onto the real row AND an ImportPOCorrection audit
    row, in one transaction - see ImportPOCorrection's docstring."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    po_model, item_model, sr_plant, _label, _match_model = resolved

    item_id = (request.data.get("itemId") or "").strip()
    field_name = (request.data.get("field") or "").strip()
    raw_value = request.data.get("value")
    reason = (request.data.get("reason") or "").strip()

    # A retired order cannot be corrected: the next sync would never rewrite
    # it, and nothing lists it any more.
    po = po_model.objects.filter(po_number=po_number, is_active=True).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    item_ref = ""
    if item_id:
        if field_name not in _ITEM_EDITABLE_FIELDS:
            return Response({"error": f"{field_name!r} is not an editable item field."}, status=400)
        target, item_ref, error = _resolve_import_line(item_model, po, item_id)
        if error:
            return Response({"error": error}, status=404 if target is None else 400)
        item_id = target.item_id
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
        ImportPOCorrection.objects.create(
            plant=sr_plant,
            po_number=po_number,
            item_id=item_id,
            item_ref=item_ref,
            field_name=field_name,
            old_value="" if old_value is None else str(old_value),
            new_value="" if new_value is None else str(new_value),
            reason=reason,
            corrected_by=request.user if getattr(request.user, "pk", None) else None,
            corrected_by_email=getattr(request.user, "email", ""),
        )

    if field_name in _REMATCH_TRIGGER_FIELDS:
        _RUN_FULL_MATCH[plant]()

    response = {"status": "ok", "field": field_name, "value": _serialize(new_value)}
    warning = _field_warning(field_name, new_value)
    if warning:
        response["warning"] = warning
    data_stamp.touch(sr_plant)
    return Response(response)


# _field_warning/_serialize are field-set-independent and byte-for-byte
# identical to _domestic_base's own copies (see CLAUDE.md's "Domestic router
# de-duplication") - shared from there rather than duplicated a fourth time.
# _coerce_value is NOT shared: this router's _DATE_FIELDS/_DECIMAL_FIELDS are
# a strict superset of the domestic routers' (BOE/exchange-rate fields the
# domestic plants don't have), so _domestic_base._coerce_value (which reads
# its own module-level field sets) can't be reused here without a further
# refactor to parametrize it - left as its own copy rather than force a
# fragile shared function for this pass.
from apps.api.routers._domestic_base import _field_warning, _serialize  # noqa: E402


def _resolve_import_line(item_model, po, address):
    """(line, item_ref, error) for an edit's line address.

    "#<n>" is the line's position (line_item_positions(), the same ref pins
    use) and is what the modal sends. A bare item_id is still accepted from
    older callers, but only when it names exactly one line of the order:
    12 Vapi orders repeat one item_id across their shipment lines, and
    `.first()` silently wrote line 2's correction onto line 1."""
    items = list(item_model.objects.filter(purchase_order=po).select_related("purchase_order"))
    refs = {item_id: ref for item_id, (_po, ref, _d) in line_item_positions(items).items()}
    if address.startswith("#"):
        want = address[1:]
        line = next((i for i in items if refs.get(i.id) == want), None)
        if line is None:
            return None, "", "Line item not found."
        return line, want, ""
    same = [i for i in items if i.item_id == address]
    if not same:
        return None, "", "Line item not found."
    if len(same) > 1:
        return same[0], "", (f"Item {address} appears on {len(same)} lines of this order; "
                             "refresh the page and edit it again.")
    return same[0], refs.get(same[0].id, ""), ""


def _coerce_value(field_name, raw_value):
    if raw_value is None or raw_value == "":
        return None
    if field_name in _DATE_FIELDS:
        return datetime.date.fromisoformat(raw_value)
    if field_name in _DECIMAL_FIELDS:
        import decimal
        from decimal import Decimal
        try:
            return Decimal(str(raw_value))
        except decimal.InvalidOperation:
            raise ValueError(f"{raw_value!r} is not a valid decimal")
    return str(raw_value)


# ── Sync status/trigger ────────────────────────────────────────────────────────

@api_view(["GET"])
def sync_status(request):
    """GET /api/imports/sync-status - latest IMPORT_PO_CSV SyncRun per plant,
    plus each plant's own in-progress flag (see is_imports_sync_in_progress)
    - same "last synced, not just trust stale data" reasoning as the domestic
    routers' own sync_status views.

    Also exposes the two company-wide (not per-plant) sync flags,
    rodtepInProgress/advanceLicenseInProgress (added 2026-09-10, alongside
    wiring "Refresh Data" to actually trigger these two - see
    triggerRealSyncAndRefresh() in frontend/js/main.js) - is_rodtep_sync_in_
    progress()/is_advance_license_sync_in_progress() (apps/services/
    sync_trigger.py) already existed but were never read by anything until
    now, so the frontend had no way to know when to stop polling for these."""
    latest_by_plant = {}
    for plant_key, (_po_model, _item_model, sr_plant, _label, _match_model) in _PLANTS.items():
        if not user_can_access_plant(request.user, plant_key):
            continue
        run = SyncRun.objects.filter(plant=sr_plant, source=SyncRun.Source.IMPORT_PO_CSV).order_by("-started_at").first()
        latest_by_plant[plant_key] = {
            "status": run.status if run else None,
            "startedAt": run.started_at.isoformat() if run else None,
            "finishedAt": run.finished_at.isoformat() if run and run.finished_at else None,
            "rowsSeen": run.rows_seen if run else 0,
            "rowsChanged": run.rows_changed if run else 0,
            "errorDetail": (run.error_detail or None) if run else None,
            "syncInProgress": is_imports_sync_in_progress(plant_key),
        }
    resp = Response({
        "sync": latest_by_plant,
        "rodtepInProgress": is_rodtep_sync_in_progress(),
        "advanceLicenseInProgress": is_advance_license_sync_in_progress(),
    })
    # See _domestic_base.py's make_sync_status() for why this is here -
    # same live-state/heuristic-caching reasoning, same fix.
    resp["Cache-Control"] = "no-store"
    return resp


@api_view(["GET"])
def track_bl(request):
    """GET /api/imports/track-bl?bl=<BL number> - live shipment lookup via
    SafeCube's Container Tracking API (apps/services/bl_tracking.py),
    backing the "Track" link next to a PO's BL Number on the Import
    Purchases page. IsAuthenticated only (any role) - same as every other
    read endpoint on this page; there's nothing plant-scoped or writeable
    here; a BL number isn't itself a plant-restricted concept the way a PO
    row is. Returns SafeCube's own response payload as-is on success (the
    frontend picks out the fields it wants to show) - a 502 with a
    human-readable {"error": ...} on any failure (unconfigured key,
    SafeCube downtime, or SafeCube's own "can't find/auto-detect this
    document" response, which is a normal, expected outcome for an older or
    uncovered shipment, not a bug)."""
    bl_number = (request.query_params.get("bl") or "").strip()
    if not bl_number:
        return Response({"error": "Missing 'bl' query parameter."}, status=400)
    result = bl_tracking.track_bl(bl_number)
    if not result["ok"]:
        return Response({"error": result["error"]}, status=502)
    return Response(result["data"])


@api_view(["POST"])
@permission_classes([IsAdmin])
@throttle_classes([SyncTriggerThrottle])
def sync_trigger(request, plant):
    """POST /api/imports/sync-trigger/<plant> - kicks off that plant's
    Imports CSV sync (+ match_<plant>, see apps/services/sync_trigger.py's
    _IMPORT_PLANT_COMMANDS) on a background thread. Admin-only: a real Drive
    API call, not just a read."""
    if plant not in _PLANTS:
        return Response({"error": "Unknown plant."}, status=404)
    started = trigger_plant_imports_sync(plant)
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "started"}, status=202)


# ── Match dismiss / flag dismiss ──────────────────────────────────────────────

@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_import_po_mir_match(request, plant, match_id: int):
    """PATCH /api/imports/matches/po-mir/<plant>/<match_id>/dismiss
    Body: {"dismissed": true/false, "reason": "<optional>"}. Same shape as
    hrs_views.py's/achhad_views.py's/vapi_views.py's own dismiss_po_mir_match
    - see that function's docstring - parametrized by `plant` here instead
    of one copy per plant, matching this module's own cross-plant
    convention (see module docstring)."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's matches."}, status=403)
    _po_model, _item_model, _sr_plant, _label, match_model = resolved
    dismissed = _request_bool(request.data.get("dismissed"), True)
    reason = (request.data.get("reason") or "").strip()
    match = dismiss_match(match_model, match_id, request.user, dismissed, reason)
    if not match:
        return Response({"error": "Match not found."}, status=404)
    data_stamp.touch(_sr_plant)
    return Response({
        "status": "ok",
        "matchId": match.id,
        "dismissedByOverride": match.dismissed_by_override,
        "dismissedReason": match.dismissed_reason,
    })


@api_view(["PATCH"])
@permission_classes([IsEditor])
def dismiss_flag(request, plant, po_number):
    """PATCH /api/imports/purchase-orders/<plant>/<po_number>/flags/dismiss
    Body: {"flagKey": "<code>:<item_id|''>", "dismissed": true/false, "reason": "<optional>"}.
    See hrs_views.dismiss_flag's docstring for why this is a separate
    FlagDismissal row rather than a column on a match model - identical
    reasoning, just for apps/services/import_flags.py's per-item flags
    (`code`/`item_id`) instead of Domestic's per-category label. The
    frontend builds flagKey as `f.code + ':' + (f.item_id || '')` from each
    dataQualityFlags entry - this view treats it as an opaque string either way."""
    resolved = _resolve_plant(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    _po_model, _item_model, sr_plant, _label, _match_model = resolved
    flag_key = (request.data.get("flagKey") or "").strip()
    if not flag_key:
        return Response({"error": "flagKey is required."}, status=400)
    if not _po_model.objects.filter(po_number=po_number, is_active=True).exists():
        return Response({"error": "Purchase order not found."}, status=404)
    dismissed = _request_bool(request.data.get("dismissed"), True)
    reason = (request.data.get("reason") or "").strip()
    fd = dismiss_po_flag(sr_plant, po_number, flag_key, request.user, dismissed, reason)
    return Response(_flag_dismissal_dict(fd))


# ── RoDTEP scrip ledger (added 2026-09-09) ──────────────────────────────────
# Company-wide (see RodtepScrollEntry's own docstring) - lives under
# /api/imports/rodtep, not per-plant, same reasoning imports_views.py's own
# cross-plant purchase_orders()/sync_status() above already established for
# genuinely shared (not per-plant) data.
#
# BOTH ledgers below are joined to the import side through
# services/license_links.py (2026-09-22), reading the `License Type` /
# `License Number` columns each plant's Imports Purchase Data master CSV has
# always carried. Read that module's header before changing anything here:
# the two panels were originally built believing no such link existed, and
# it is the reason RodtepUsage was ever hand-entered.

_BOE_LINE_ITEM_MODELS = [HRSImportPOLineItem, RTPAchhadImportPOLineItem, RTPVapiImportPOLineItem]


def _boe_exists(boe_number: str, known: frozenset | None = None) -> bool:
    """Whether `boe_number` appears on any plant's Import PO line items -
    the real, checkable cross-reference a RodtepUsage entry's boe_number
    should have; surfaced to the frontend as `boeVerified` so a reviewer can
    spot a typo'd/unrecognized BOE number without a separate lookup."""
    if not boe_number:
        return False
    return boe_number in (known if known is not None else _known_boe_numbers())


def _known_boe_numbers() -> frozenset:
    """Every BOE number on any plant's import lines - three queries. The
    ledgers read it once and pass it to _boe_exists(): per citation it was
    up to three one-row lookups each, 36 per Advance Licence load."""
    return frozenset(n for model in _BOE_LINE_ITEM_MODELS
                     for n in model.objects.exclude(boe_number="").values_list("boe_number", flat=True))


def _rodtep_entry_dict(e: RodtepScrollEntry) -> dict:
    return {
        "id": e.id,
        "scriptNo": e.script_no,
        "scriptDate": e.script_date.isoformat() if e.script_date else None,
        "sbNumber": e.sb_number,
        "sbDate": e.sb_date.isoformat() if e.sb_date else None,
        "scrollNumber": e.scroll_number,
        "scrollDate": e.scroll_date.isoformat() if e.scroll_date else None,
        "location": e.location,
        "sanctionedAmount": e.sanctioned_amount,
    }


def _rodtep_usage_dict(u: RodtepUsage, known: frozenset | None = None) -> dict:
    return {
        "id": u.id,
        "scriptNo": u.script_no,
        "usedAmount": u.used_amount,
        "boeNumber": u.boe_number,
        "boeVerified": _boe_exists(u.boe_number, known),
        "importPoNumber": u.import_po_number,
        "usedDate": u.used_date.isoformat() if u.used_date else None,
        "notes": u.notes,
        "enteredBy": u.entered_by.email if u.entered_by else None,
        "createdAt": u.created_at.isoformat(),
    }


# ── The import side of both licence ledgers ─────────────────────────────────

def _citation_dict(c) -> dict:
    """One import PO line item's claim on one licence, as the panels show it.
    `sharedWith` is what stops `landedValue` being read as this licence's own
    share of the line - see license_links.citation_totals()."""
    return {
        "plantKey": c.plant_key,
        "plant": c.plant_label,
        "poNumber": c.po_number,
        "itemId": c.item_id,
        "description": c.description,
        "boeNumber": c.boe_number,
        "qty": c.qty,
        "uom": c.uom,
        "landedValue": c.landed_value,
        "licenseNumberRaw": c.license_number_raw,
        "licenseTypeRaw": c.license_type_raw,
        "sharedWith": list(c.shared_with),
    }


def _license_citations(scheme: str, user) -> tuple[dict, list]:
    """Every import-side citation for one scheme, grouped by normalised
    licence number, plus the citations naming a licence under NO recognised
    scheme at all.

    One pass over the three plants' line items serves both - the unclassified
    list is returned by each ledger rather than living in a third endpoint,
    because it is the same one action either reader can take (fill in the
    `License Type` column) and neither panel can be sure the other was
    opened."""
    # Only the plants `user` may read (2026-09-25): each citation names an
    # import PO, its plant and its landed value, and these ledgers are
    # company-wide, so without this a plant-scoped account saw every plant's
    # orders here while every other endpoint refused them.
    citations = [c for c in license_links.collect_citations() if user_can_access_plant(user, c.plant_key)]
    scoped = [c for c in citations if c.scheme == scheme]
    unclassified = [c for c in citations if c.scheme == license_links.SCHEME_UNKNOWN]
    return license_links.citations_by_license(scoped), unclassified


def _unrecognised_license_rows(by_license: dict, known_numbers: set) -> list:
    """Licences the imports CSV cites that the ledger does not hold - the
    RoDTEP/Advance-Licence analogue of `mir_without_po`'s PO_UNKNOWN bucket,
    and actionable in the same upstream direction: either the scrip/licence
    file has not been added to Drive yet, or the number in the CSV is wrong.
    Reported as its own list, never folded in among real ledger rows with
    zero sanctioned against them."""
    rows = []
    for number in sorted(set(by_license) - known_numbers):
        citations = by_license[number]
        rows.append({
            "licenseNumber": number,
            "licenseNumbersRaw": sorted({c.license_number_raw for c in citations}),
            **license_links.citation_totals(citations),
            "citations": [_citation_dict(c) for c in citations],
        })
    return rows


@api_view(["GET"])
def rodtep_ledger(request):
    """GET /api/imports/rodtep - one row per Script Number: the credit
    sanctioned against it (auto-synced RodtepScrollEntry), and which imports
    were actually cleared under it (derived from the imports master CSV via
    license_links.py). Any role can read, same as every other GET here.

    There is deliberately NO "balance remaining" derived from the import
    side: the CSV says WHICH scrip was applied to a line, never how much
    credit that debited, and no synced source carries the amount. The
    `totalUsed`/`balance` pair below reads the legacy hand-entered
    RodtepUsage table only, and `summary.hasLoggedUsage` tells the frontend
    whether those two columns mean anything at all - with that table empty
    they would otherwise render a Balance equal to Sanctioned on every row,
    which reads as "none of this scrip has been used" when in fact several
    imports have been cleared under it."""
    from django.db.models import Count, Max, Min, Sum

    # One row per script - Min("location")/Min("script_date") rather than a
    # per-script follow-up query, since one RODTEP-JNPT-<N>.xlsx file holds
    # exactly one script and repeats both values down every row of it (see
    # parsers/rodtep.py's own header).
    ledger_by_script = {
        row["script_no"]: row
        for row in RodtepScrollEntry.objects.values("script_no").annotate(
            total_sanctioned=Sum("sanctioned_amount"),
            entry_count=Count("id"),
            script_date=Min("script_date"),
            sb_from=Min("sb_date"),
            sb_to=Max("sb_date"),
            location=Min("location"),
        )
    }
    used_by_script = {
        row["script_no"]: row["total"]
        for row in RodtepUsage.objects.values("script_no").annotate(total=Sum("used_amount"))
    }
    by_license, unclassified = _license_citations(license_links.SCHEME_RODTEP, request.user)

    # A script might have usage entered before its own ledger file has been
    # synced yet (see RodtepUsage's own docstring on why there's no FK) -
    # union both key sets so that script still shows up, with 0 sanctioned
    # rather than being silently dropped. Scrips the imports CSV cites and
    # the ledger doesn't hold are NOT unioned in here; they go to
    # `unknownScrips`, where the fix (add the file to Drive, or correct the
    # CSV) is a different one from anything a ledger row implies.
    all_scripts = sorted(set(ledger_by_script) | set(used_by_script))

    scripts = []
    for script_no in all_scripts:
        ledger = ledger_by_script.get(script_no) or {}
        sanctioned = ledger.get("total_sanctioned") or 0
        used = used_by_script.get(script_no) or 0
        citations = by_license.get(script_no, [])
        script_date = ledger.get("script_date")
        sb_from, sb_to = ledger.get("sb_from"), ledger.get("sb_to")
        scripts.append({
            "scriptNo": script_no,
            "scriptDate": script_date.isoformat() if script_date else None,
            "location": ledger.get("location") or "",
            "entryCount": ledger.get("entry_count") or 0,
            "sbDateFrom": sb_from.isoformat() if sb_from else None,
            "sbDateTo": sb_to.isoformat() if sb_to else None,
            "totalSanctioned": sanctioned,
            "totalUsed": used,
            "balance": sanctioned - used,
            "imports": license_links.citation_totals(citations),
        })

    total_sanctioned = sum((s["totalSanctioned"] for s in scripts), Decimal("0"))
    last_run = SyncRun.objects.filter(plant=SyncRun.Plant.COMPANY, source=SyncRun.Source.RODTEP).order_by("-finished_at").first()
    return Response({
        "scripts": scripts,
        "summary": {
            "scripCount": len(scripts),
            "totalSanctioned": total_sanctioned,
            # Idle credit: a scrip we hold that no import has been cleared
            # under. The one number here somebody can act on directly.
            "scripsCited": len([s for s in scripts if s["imports"]["lineCount"]]),
            "scripsNeverCited": len([s for s in scripts if not s["imports"]["lineCount"]]),
            "importLines": sum(s["imports"]["lineCount"] for s in scripts),
            "importLandedValue": sum((s["imports"]["landedValue"] for s in scripts), Decimal("0")),
            "hasLoggedUsage": bool(used_by_script),
            "totalLoggedUsed": sum(used_by_script.values(), Decimal("0")),
        },
        "unknownScrips": _unrecognised_license_rows(by_license, set(ledger_by_script)),
        "unclassifiedCitations": [_citation_dict(c) for c in unclassified],
        "lastSync": {
            "status": last_run.status,
            "finishedAt": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None,
            # errorDetail (added 2026-09-09) - was always recorded server-side
            # on a failed SyncRun, but never returned here, so a silently
            # failing RoDTEP sync (e.g. Drive folder not shared with the
            # service account) was invisible short of Django Admin/server
            # log access - same fix as _domestic_base.py's make_sync_status().
            "errorDetail": last_run.error_detail or None,
        } if last_run else None,
    })


@api_view(["GET"])
def rodtep_script_detail(request, script_no):
    """GET /api/imports/rodtep/<script_no> - the export side (every Shipping
    Bill row that earned this scrip's credit), the import side (every line
    item the CSV says was cleared under it), and any legacy hand-entered
    usage rows, for the drill-down panel.

    A scrip the CSV cites but the ledger has no file for still resolves here
    rather than 404ing - that row is reachable from the ledger's own
    `unknownScrips` list, and answering "not found" for something the panel
    just linked to would be the wrong answer to the question being asked."""
    entries = RodtepScrollEntry.objects.filter(script_no=script_no).order_by("sb_date")
    usages = RodtepUsage.objects.filter(script_no=script_no)
    by_license, _ = _license_citations(license_links.SCHEME_RODTEP, request.user)
    citations = by_license.get(license_links.normalize_license_number(script_no), [])
    if not entries.exists() and not usages.exists() and not citations:
        return Response({"error": "No RoDTEP data found for this Script Number."}, status=404)
    known_boes = _known_boe_numbers()
    return Response({
        "scriptNo": script_no,
        "entries": [_rodtep_entry_dict(e) for e in entries],
        "usages": [_rodtep_usage_dict(u, known_boes) for u in usages],
        "imports": [_citation_dict(c) for c in citations],
        "importTotals": license_links.citation_totals(citations),
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def rodtep_sync_trigger(request):
    """POST /api/imports/rodtep/sync-trigger - runs manage.py sync_rodtep
    via apps/services/sync_trigger.py's trigger_rodtep_sync(), same
    lock-protected "already running -> 409" contract as every other
    sync-trigger endpoint in this app (so a manual click here can't race
    the daily scheduled RoDTEP sync - see run_daily_sync_all_plants()'s own
    RoDTEP block). IsAdmin-gated, same as every other sync_trigger.

    Run synchronously (not queued via django-q's async_task() the way
    trigger_plant_sync() is) rather than backgrounded - this syncs 1-2
    small xlsx files (confirmed a few seconds end-to-end against real
    Drive data), nowhere near gunicorn's 30s worker timeout, so the added
    complexity of a background task + polling isn't justified here the way
    it is for a full plant's multi-file MIR/Stock pipeline (see
    sync_trigger.py's own module docstring for why THAT one needs to be
    async).

    No longer reachable from the RoDTEP panel itself (2026-09-22) - both
    ledgers sync with everything else through the dashboard's "Refresh
    Data" button, which fires this endpoint and its Advance Licence
    counterpart in parallel. The endpoint stays: it is what that button,
    and run_daily_sync_all_plants(), actually call."""
    from apps.services.sync_trigger import trigger_rodtep_sync

    started = trigger_rodtep_sync()
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "ok"})


# ── Advance License ledger (added 2026-09-09) ───────────────────────────────
# Company-wide (see AdvanceLicense's own docstring), lives under
# /api/imports/advance-license - same reasoning as RoDTEP directly above:
# scoped to Import Purchases only, not per-plant, not a new top-level tab.
#
# Two independent accounts of the same thing, deliberately kept apart rather
# than reconciled into one number:
#
#   `usage`   the project owner's own hand-maintained workbook, which carries
#             BOE/PO/qty/value per material row. The ONLY source with the
#             VALUE drawn against a licence, so it is the only thing
#             utilisation can be computed from.
#   `imports` the imports master CSV's own `License Number` column, via
#             license_links.py. Has no value-drawn column, but is generated
#             from the same file the whole Import dashboard runs on.
#
# `boeCrossCheck` is what makes having both worth it: a BOE in one and not
# the other is a real bookkeeping gap in whichever side is missing it, and
# nothing else in this app could see it.

# Fewer than 90 days of export obligation left. Not a threshold with a
# measurement behind it - a stated review horizon, which is why it is named
# here rather than inlined at the comparison.
_LICENSE_EXPIRY_SOON_DAYS = 90


def _advance_license_material_dict(m, known: frozenset | None = None) -> dict:
    return {
        "materialDescription": m.material_description,
        "itchsCode": m.itchs_code,
        "qtyAuthorized": m.qty_authorized,
        "cifValueAuthorized": m.cif_value_authorized,
        "dutySavedPct": m.duty_saved_pct,
        "boeNumber": m.boe_number,
        "boeDate": m.boe_date.isoformat() if m.boe_date else None,
        "importPoNumber": m.import_po_number,
        "qtyImported": m.qty_imported,
        "valueImported": m.value_imported,
        # Whether this workbook row's BOE is one the Import dashboard
        # actually holds - the same check RodtepUsage's own boeVerified
        # makes, and the reason a typo'd BOE stops being invisible here.
        "boeVerified": _boe_exists(m.boe_number, known),
    }


def _material_rollup(materials) -> list:
    """One row per input material, with its usage rows folded in - the
    workbook repeats a material once per import drawn against it (see
    AdvanceLicenseMaterial's own docstring), so the authorised qty is taken
    from ONE of those rows and never summed, while the imported qty/value
    are summed across all of them. Summing the authorisation instead would
    multiply it by however many times the material has been imported."""
    rollup: dict[str, dict] = {}
    for m in materials:
        row = rollup.setdefault(m.material_description, {
            "materialDescription": m.material_description,
            "itchsCode": m.itchs_code,
            "qtyAuthorized": m.qty_authorized,
            "cifValueAuthorized": m.cif_value_authorized,
            "dutySavedPct": m.duty_saved_pct,
            "qtyImported": Decimal("0"),
            "valueImported": Decimal("0"),
            "usageRows": 0,
        })
        # A later row of the same material can carry the authorisation where
        # the first left it blank; take the first non-null rather than
        # letting a blank first row report the material as unauthorised.
        for field, value in (
            ("qtyAuthorized", m.qty_authorized),
            ("cifValueAuthorized", m.cif_value_authorized),
            ("dutySavedPct", m.duty_saved_pct),
            ("itchsCode", m.itchs_code),
        ):
            if not row[field]:
                row[field] = value
        if m.qty_imported is not None:
            row["qtyImported"] += m.qty_imported
        if m.value_imported is not None:
            row["valueImported"] += m.value_imported
        if m.boe_number or m.qty_imported is not None or m.value_imported is not None:
            row["usageRows"] += 1

    for row in rollup.values():
        authorized = row["qtyAuthorized"]
        row["qtyRemaining"] = (authorized - row["qtyImported"]) if authorized is not None else None
    return list(rollup.values())


def _advance_license_dict(lic, citations: list, today, known_boes: frozenset | None = None) -> dict:
    # Field order matches the project owner's own requested view order:
    # License Number -> Export Product Description -> CIF Value Authorized
    # -> FOB Export Target -> Export Validity -> Material Description(s).
    materials = list(lic.materials.all())
    material_rows = _material_rollup(materials)
    value_imported = sum((m.value_imported for m in materials if m.value_imported is not None), Decimal("0"))
    cif_authorized = lic.cif_value_authorized or Decimal("0")
    workbook_boes = {m.boe_number for m in materials if m.boe_number}
    csv_boes = {c.boe_number for c in citations if c.boe_number}

    def _days_left(date_value):
        return (date_value - today).days if date_value else None

    export_days_left = _days_left(lic.export_validity_date)
    import_days_left = _days_left(lic.import_validity_date)
    return {
        "licenseNumber": lic.license_number,
        "exportProductDescription": lic.export_product_description,
        "cifValueAuthorized": lic.cif_value_authorized,
        "fobValueExportTarget": lic.fob_value_export_target,
        "exportValidityDate": lic.export_validity_date.isoformat() if lic.export_validity_date else None,
        "materials": [_advance_license_material_dict(m, known_boes) for m in materials],
        # Extra fields kept alongside (not part of the requested view, but
        # already computed by the sync - no reason to withhold them from the
        # payload; the frontend simply doesn't render them today).
        "issueDate": lic.issue_date.isoformat() if lic.issue_date else None,
        "iec": lic.iec,
        "importValidityDate": lic.import_validity_date.isoformat() if lic.import_validity_date else None,
        "status": lic.status,
        # ── Derived (2026-09-22) ──
        "materialRollup": material_rows,
        "usage": {
            "valueImported": value_imported,
            "cifRemaining": cif_authorized - value_imported,
            # None, not 0, when nothing was authorised - a percentage of zero
            # is not "0% used", it is a question about the source row.
            "cifUtilisedPct": (value_imported / cif_authorized * 100) if cif_authorized else None,
            "materialCount": len(material_rows),
            "usageRows": sum(r["usageRows"] for r in material_rows),
        },
        "validity": {
            "exportDaysLeft": export_days_left,
            "importDaysLeft": import_days_left,
            "exportExpired": export_days_left is not None and export_days_left < 0,
            "importExpired": import_days_left is not None and import_days_left < 0,
            "exportExpiringSoon": export_days_left is not None and 0 <= export_days_left <= _LICENSE_EXPIRY_SOON_DAYS,
        },
        "imports": license_links.citation_totals(citations),
        "importCitations": [_citation_dict(c) for c in citations],
        "boeCrossCheck": {
            # A BOE the workbook records against this licence that no import
            # line cites it for, and the reverse. Either direction is a real
            # gap: the first says the CSV's License Number column was left
            # blank, the second that the workbook has not been updated.
            "workbookOnly": sorted(workbook_boes - csv_boes),
            "csvOnly": sorted(csv_boes - workbook_boes),
        },
    }


@api_view(["GET"])
def advance_license_ledger(request):
    """GET /api/imports/advance-license - every synced Advance License with
    its own fields, its input materials rolled up (authorised vs imported
    qty), CIF utilisation from the workbook's own usage columns, validity
    countdowns, and the import lines the master CSV says were cleared under
    it. Any role can read (same as every other GET in this file)."""
    # timezone.localdate(), never date.today() - the server runs UTC on
    # Render while TIME_ZONE is Asia/Kolkata, so between 00:00 and 05:29 IST
    # the server's date is still yesterday and every countdown here would be
    # a day out. See CLAUDE.md's timezone trap.
    today = timezone.localdate()
    by_license, unclassified = _license_citations(license_links.SCHEME_ADVANCE, request.user)
    licenses = AdvanceLicense.objects.prefetch_related("materials").order_by("license_number")

    rows, known_numbers = [], set()
    known_boes = _known_boe_numbers()
    for lic in licenses:
        number = license_links.normalize_license_number(lic.license_number)
        known_numbers.add(number)
        rows.append(_advance_license_dict(lic, by_license.get(number, []), today, known_boes))

    last_run = (
        SyncRun.objects.filter(plant=SyncRun.Plant.COMPANY, source=SyncRun.Source.ADVANCE_LICENSE)
        .order_by("-finished_at").first()
    )
    cif_authorized = sum((r["cifValueAuthorized"] or Decimal("0") for r in rows), Decimal("0"))
    cif_imported = sum((r["usage"]["valueImported"] for r in rows), Decimal("0"))
    return Response({
        "licenses": rows,
        "summary": {
            "licenseCount": len(rows),
            "cifAuthorized": cif_authorized,
            "cifImported": cif_imported,
            "cifRemaining": cif_authorized - cif_imported,
            "cifUtilisedPct": (cif_imported / cif_authorized * 100) if cif_authorized else None,
            "fobExportTarget": sum((r["fobValueExportTarget"] or Decimal("0") for r in rows), Decimal("0")),
            "exportExpired": len([r for r in rows if r["validity"]["exportExpired"]]),
            "exportExpiringSoon": len([r for r in rows if r["validity"]["exportExpiringSoon"]]),
            "neverCited": len([r for r in rows if not r["imports"]["lineCount"]]),
            "boeGaps": sum(len(r["boeCrossCheck"]["workbookOnly"]) + len(r["boeCrossCheck"]["csvOnly"]) for r in rows),
            "expirySoonDays": _LICENSE_EXPIRY_SOON_DAYS,
        },
        "unknownLicenses": _unrecognised_license_rows(by_license, known_numbers),
        "unclassifiedCitations": [_citation_dict(c) for c in unclassified],
        "lastSync": {
            "status": last_run.status,
            "finishedAt": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None,
            # Surfaced so "why isn't this syncing" is answerable from this
            # panel directly (e.g. the file not shared with the service
            # account) instead of needing server log/Django Admin access.
            "errorDetail": last_run.error_detail or None,
        } if last_run else None,
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def advance_license_sync_trigger(request):
    """POST /api/imports/advance-license/sync-trigger - runs manage.py
    sync_advance_license via trigger_advance_license_sync(), same
    lock-protected "already running -> 409" contract as rodtep_sync_trigger
    above, for the same reasons (one small file, well under gunicorn's 30s
    worker timeout, run synchronously rather than backgrounded). Reached
    from "Refresh Data", not from the panel - see rodtep_sync_trigger."""
    from apps.services.sync_trigger import trigger_advance_license_sync

    started = trigger_advance_license_sync()
    if not started:
        return Response({"status": "already_running"}, status=409)
    return Response({"status": "ok"})

# ── Manual MIR match (imports) ────────────────────────────────────────────────
# The Domestic version of this feature, extended to Import POs on request
# (project owner, 2026-09-21: "yes do it for imports too"). Same model, same
# matcher pass, same picker UI - the only real differences are this router's
# cross-plant URL shape (plant is a path segment, see the module docstring)
# and ManualMirMatch.po_kind, which keeps an import pin from ever addressing a
# domestic line that happens to share a PO number.
#
# Deliberately NOT factored into _domestic_base's make_* factories: those take
# a _PlantConfig and build one view per plant, while this router is one view
# for all three. The two genuinely shared parts - _mir_row_dict() and
# _claims_for_mir_numbers() - are imported rather than copied.


@api_view(["GET"])
def mir_candidates(request, plant, po_number):
    """GET /api/imports/purchase-orders/<plant>/<po>/mir-candidates?q=<text>"""
    resolved = _PLANTS.get(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_access_plant(request.user, plant):
        return Response({"error": "Unknown plant."}, status=404)
    po_model, item_model, sr_plant, _label, match_model = resolved
    po = po_model.objects.filter(po_number=po_number, is_active=True).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    mir_model = _MIR_MODEL[plant]
    q = (request.query_params.get("q") or "").strip()
    rows = _candidate_rows(mir_model, match_model, po, q)

    # BOTH kinds of claim, because both compete for the same MIR rows. A
    # domestic line holding the document is exactly as much a collision as an
    # import one, and showing only half of that would let a reader take a row
    # believing nothing was using it.
    numbers = {r.mir_no for r in rows if r.mir_no}
    claims = _claims_for_mir_numbers(_domestic_cfg_for(plant), numbers)
    for mir_no, holders in _import_claims_for_mir_numbers(plant, numbers).items():
        claims.setdefault(mir_no, []).extend(holders)

    # A line of THIS order on the same Bill of Entry as the line being edited
    # does not lose a receipt booked under that BOE when this line is pinned
    # to it - BOE settlement shares it between them (matching_core's
    # _pin_defers_to_boe()). Marked, so the picker does not warn about it.
    item_ref = (request.query_params.get("itemRef") or "").strip()
    items = list(item_model.objects.filter(purchase_order=po).select_related("purchase_order"))
    positions = line_item_positions(items)
    boe_by_ref = {positions[i.id][1]: _boe_key(i.boe_number) for i in items}
    own_boe = boe_by_ref.get(item_ref, "")
    rows_by_no: dict = {}
    for r in rows:
        rows_by_no.setdefault(r.mir_no, []).append(r)
    if own_boe:
        for mir_no, holders in claims.items():
            if not all(_boe_key(r.invoice_no) == own_boe for r in rows_by_no.get(mir_no, [])):
                continue
            for h in holders:
                if h.get("isImport") and h["poNumber"] == po.po_number and boe_by_ref.get(h["itemRef"]) == own_boe:
                    h["sharesReceipt"] = True

    seen: dict = {}
    for row in rows:
        if not row.mir_no:
            continue
        entry = seen.get(row.mir_no)
        if entry is None:
            entry = dict(_mir_row_dict(row))
            entry["rowCount"] = 0
            entry["sheetRows"] = []
            entry["claimedBy"] = claims.get(row.mir_no, [])
            seen[row.mir_no] = entry
        entry["rowCount"] += 1
        if _sheet_row(row) is not None:
            entry["sheetRows"].append(_sheet_row(row))
    return Response({"candidates": list(seen.values())})


def _import_claims_for_mir_numbers(plant, mir_numbers):
    """The import-side half of _claims_for_mir_numbers()."""
    if not mir_numbers:
        return {}
    _po_model, item_model, _sr, _label, match_model = _PLANTS[plant]
    # Grouped rows count as held too - see _domestic_base._held_mir_numbers().
    matches = list(
        match_model.objects
        .filter(Q(mir_entry__mir_no__in=mir_numbers) | Q(group_entries__mir_no__in=mir_numbers),
                po_line_item__purchase_order__is_active=True)
        .select_related("mir_entry", "po_line_item", "po_line_item__purchase_order")
        .prefetch_related("group_entries")
        .distinct()
    )
    po_ids = {m.po_line_item.purchase_order_id for m in matches}
    refs = {}
    if po_ids:
        items = list(item_model.objects.filter(purchase_order_id__in=po_ids).select_related("purchase_order"))
        refs = {item_id: ref for item_id, (_po, ref, _desc) in line_item_positions(items).items()}
    out: dict = {}
    for m, mir_no in _held_mir_numbers(matches, mir_numbers):
        out.setdefault(mir_no, []).append({
            "poNumber": m.po_line_item.purchase_order.po_number,
            "itemRef": refs.get(m.po_line_item_id, ""),
            "description": m.po_line_item.description,
            "manuallyPinned": bool(getattr(m, "manually_pinned", False)),
            "isImport": True,
        })
    return out


def _domestic_cfg_for(plant):
    """That plant's domestic _PlantConfig, so the import picker can report
    domestic claims too. Imported lazily to avoid a circular import at module
    load (each plant's *_views module imports _domestic_base, which this
    module already imports from)."""
    from apps.api.routers import achhad_views, hrs_views, vapi_views
    return {"hrs": hrs_views, "achhad": achhad_views, "vapi": vapi_views}[plant]._CONFIG


@api_view(["PATCH"])
@permission_classes([IsEditor])
def set_mir_match(request, plant, po_number):
    """PATCH /api/imports/purchase-orders/<plant>/<po>/mir-match

    Body: {itemRef, mirNo, reason, clear} - identical to the Domestic
    endpoint's, see _domestic_base.make_set_mir_match()."""
    resolved = _PLANTS.get(plant)
    if not resolved:
        return Response({"error": "Unknown plant."}, status=404)
    if not user_can_edit_plant(request.user, plant):
        return Response({"error": "You are not permitted to edit this plant's purchase orders."}, status=403)
    po_model, item_model, sr_plant, _label, _match_model = resolved

    item_ref = str(request.data.get("itemRef") or "").strip()
    mir_no = (request.data.get("mirNo") or "").strip()
    reason = (request.data.get("reason") or "").strip()
    clear = _request_bool(request.data.get("clear"), False)
    # "Keep both": use the document without taking it from its current
    # holder - see ManualMirMatch.shared. Meaningless without a mirNo.
    shared = _request_bool(request.data.get("share"), False) and bool(mir_no)

    po = po_model.objects.filter(po_number=po_number, is_active=True).first()
    if not po:
        return Response({"error": "Purchase order not found."}, status=404)

    items = list(item_model.objects.filter(purchase_order=po).select_related("purchase_order"))
    refs = line_item_positions(items)
    target = next((i for i in items if refs[i.id][1] == item_ref), None)
    if target is None:
        return Response({"error": "Line item not found on this purchase order."}, status=404)

    if clear:
        ManualMirMatch.objects.filter(
            plant=sr_plant, po_kind=ManualMirMatch.POKind.IMPORT,
            po_number=po_number, item_ref=item_ref).delete()
    else:
        if mir_no and not _MIR_MODEL[plant].objects.filter(is_active=True, mir_no=mir_no).exists():
            return Response({"error": f"No active MIR entry numbered {mir_no!r} at this plant."}, status=400)
        ManualMirMatch.objects.update_or_create(
            plant=sr_plant,
            po_kind=ManualMirMatch.POKind.IMPORT,
            po_number=po_number,
            item_ref=item_ref,
            defaults=dict(
                mir_no=mir_no,
                shared=shared,
                item_description=target.description or "",
                reason=reason,
                created_by=request.user if getattr(request.user, "pk", None) else None,
                created_by_email=getattr(request.user, "email", ""),
            ),
        )

    result = _RUN_FULL_MATCH[plant]()
    return Response({
        "status": "ok",
        "itemRef": item_ref,
        "mirNo": "" if clear else mir_no,
        "cleared": clear,
        "shared": False if clear else shared,
        "manualPinsApplied": result.get("manual_pins_applied"),
        "stalePins": result.get("manual_pins_stale", []),
        "unfilledPins": result.get("manual_pins_unfilled", []),
    })
