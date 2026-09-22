"""
apps/services/rm_untracked.py - the DB-touching layer over the two registries
that keep rows out of MIR<->Stock matching: parsers/common.py's
NO_RM_STOCK_VENDORS (by vendor) and NOT_STOCKED_MATERIALS (by material class).

Same split, and for the same reason, as no_po_vendors.py over NO_PO_VENDORS
(and data_quality.py over arithmetic_checks.py): the registries stay
dependency-free and unit-testable as plain Python, the queries live here.

**This answers a different question from no_po_vendors.py, about a different
pairing.** That module reports what has no PURCHASE ORDER, excluded from
PO<->MIR. This one reports what the RM STOCK SHEET does not hold, excluded
from MIR<->Stock. A row can be in either, both, or neither, and Madura is the
clearest illustration of why they cannot be merged: it is properly PO'd - the
single biggest source of PO<->MIR matches at Vapi - and simply never appears
in a stock file. Tinna Rubber is the mirror image.

The rule both registries share is **suppress the match, never the row**. These
MIR rows stay in the table and still reconcile against their purchase orders;
all that changes is that MIR<->Stock stops looking for a stock lot that does
not exist. Counting them here is what keeps the exclusion visible - an
unmatched conveyor-belt row and a genuinely failed match look identical on a
dashboard otherwise, which is exactly the confusion these registries exist to
end, and exactly what `purchases_without_po_summary()` does for the PO side.

WHY BOTH HALVES SIT IN ONE SUMMARY. The dashboard shows them as one thing
("RM doesn't track these"), because to a reader they are one thing: receipts
this pairing is not expected to reconcile. They stay separate registries
underneath because they go stale for different reasons - a vendor entry
becomes wrong when that vendor's goods start being stocked, a material class
becomes wrong when a plant starts stocking that class. `byVendor` and
`byClass` are reported separately so either kind of drift is visible on its
own.
"""

from django.db.models import Count, Sum

from apps.services.parsers.common import (
    no_rm_stock_vendor_reason,
    not_stocked_material_entry,
)


def rm_untracked_summary(mir_model: type, plant_key: str) -> dict:
    """Count this plant's active MIR rows that MIR<->Stock deliberately skips,
    split by material class and by vendor.

    Takes the plant's MIR model AND its key, because the two registries are
    scoped differently on purpose: NO_RM_STOCK_VENDORS is one shared list
    (Madura delivers to all three plants and appears in none of their stock
    sheets), while NOT_STOCKED_MATERIALS is PER PLANT - what a warehouse
    stocks is a fact about that warehouse. Achhad holds no grease but does
    hold Lamor logo film; HRS and Vapi are the reverse on both.

    **A row counted under a vendor is never also counted under a class.** The
    matcher checks the vendor registry first, so the vendor is the reason that
    row was skipped, and double-counting would make `total` disagree with the
    number of rows actually excluded - which is the one number a reader
    subtracts from the MIR total to judge coverage.

    One grouped query per breakdown rather than fetching rows: this feeds
    `sync-status`, which the dashboard polls while a sync runs. The material
    grouping is by DESCRIPTION (a few hundred distinct values per plant, far
    fewer than rows), with the regex classification done in Python - the
    patterns cannot be expressed as an indexable SQL predicate, and doing it
    this way keeps the work proportional to distinct descriptions rather than
    to the MIR table.
    """
    vendor_rows = (
        mir_model.objects.filter(is_active=True)
        .exclude(party_name="")
        .values("party_name")
        .annotate(n=Count("id"), value=Sum("taxable_value"))
    )
    excluded_vendors = {}
    by_vendor = []
    for row in vendor_rows:
        reason = no_rm_stock_vendor_reason(row["party_name"])
        if reason is None:
            continue
        excluded_vendors[row["party_name"]] = True
        by_vendor.append({
            "vendor": row["party_name"],
            "reason": reason,
            "rowCount": row["n"],
            "value": float(row["value"] or 0),
        })
    by_vendor.sort(key=lambda v: (-v["rowCount"], v["vendor"]))

    material_rows = (
        mir_model.objects.filter(is_active=True)
        .exclude(material_description="")
        .values("material_description", "party_name")
        .annotate(n=Count("id"), value=Sum("taxable_value"))
    )
    classes: dict[str, dict] = {}
    for row in material_rows:
        # Already excluded by vendor - see the docstring on why this is not
        # counted twice.
        if row["party_name"] in excluded_vendors:
            continue
        entry = not_stocked_material_entry(row["material_description"], plant_key)
        if entry is None:
            continue
        label, reason = entry
        bucket = classes.setdefault(label, {
            "materialClass": label, "reason": reason, "rowCount": 0, "value": 0.0,
        })
        bucket["rowCount"] += row["n"]
        bucket["value"] += float(row["value"] or 0)

    by_class = sorted(classes.values(), key=lambda c: (-c["rowCount"], c["materialClass"]))
    total = sum(v["rowCount"] for v in by_vendor) + sum(c["rowCount"] for c in by_class)
    total_value = sum(v["value"] for v in by_vendor) + sum(c["value"] for c in by_class)
    return {
        "total": total,
        "value": total_value,
        "byClass": by_class,
        "byVendor": by_vendor,
    }
