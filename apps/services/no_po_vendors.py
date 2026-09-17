"""
apps/services/no_po_vendors.py — the DB-touching layer over
parsers/common.py's NO_PO_VENDORS registry (same split as data_quality.py
over arithmetic_checks.py: the registry itself stays dependency-free and
unit-testable as plain Python, the queries live here).

Why this exists at all. The registry's job in the matcher is subtractive -
_MirCandidateIndex drops these vendors' MIR rows from the PO<->MIR candidate
pool, because there is no purchase order for them to match and never will
be. On its own that is indistinguishable, from the outside, from the rows
having quietly disappeared, which is exactly the failure this whole change
set out to fix: before it, these rows looked like matching had failed on
them; a silent exclusion would make them look like nothing at all.

So the rule for the registry is "suppress the match, never the row", and
this module is the other half of that rule - it counts what was excluded and
why, per vendor and per category, so `sync_status` can report it and a
viewer can see that (say) 400 of Achhad's 622 MIR rows are accounted for
rather than unreconciled.

The NO_PO_SUPPLIER half in particular has to stay visible. Those are real
third-party purchases that simply are not PO'd today - a process gap, not a
fact about the data model. The moment one of them starts being PO'd, its
registry entry has to be removed or its orders will be silently excluded
from reconciliation. A number on screen is what makes that moment
noticeable.
"""

from django.db.models import Count

from apps.services.parsers.common import (
    INTERNAL_TRANSFER,
    NO_PO_SUPPLIER,
    no_po_vendor_entry,
)


def no_po_vendor_summary(mir_model: type) -> dict:
    """Count this plant's active MIR rows that belong to registered no-PO
    vendors, split by category and broken out per vendor.

    Takes only the plant's MIR model: NO_PO_VENDORS is one list shared by all
    three plants (see its header), so there is no plant key to pass.

    Grouped in SQL by party_name and classified in Python, rather than
    fetching rows: there are ~240 distinct vendor names across all three
    plants against thousands of MIR rows, so this is one small aggregate
    query regardless of how far the MIR table grows. That matters because
    `sync_status` is polled by the dashboard while a sync runs, not fetched
    once per page load.

    Returns totals plus a `vendors` list ordered by descending row count -
    the order a reader wants, since the few large internal-transfer parties
    dominate and the long tail of one-off suppliers is what needs checking.
    """
    counts = {INTERNAL_TRANSFER: 0, NO_PO_SUPPLIER: 0}
    vendors = []

    rows = (
        mir_model.objects.filter(is_active=True)
        .exclude(party_name="")
        .values("party_name")
        .annotate(n=Count("id"))
        .values_list("party_name", "n")
    )
    for party_name, n in rows:
        entry = no_po_vendor_entry(party_name)
        if entry is None:
            continue
        category, reason = entry
        counts[category] += n
        vendors.append({
            "vendor": party_name,
            "category": category,
            "reason": reason,
            "mirEntryCount": n,
        })

    vendors.sort(key=lambda v: (-v["mirEntryCount"], v["vendor"]))
    return {
        "total": counts[INTERNAL_TRANSFER] + counts[NO_PO_SUPPLIER],
        "internalTransfer": counts[INTERNAL_TRANSFER],
        "noPoSupplier": counts[NO_PO_SUPPLIER],
        "vendors": vendors,
    }
