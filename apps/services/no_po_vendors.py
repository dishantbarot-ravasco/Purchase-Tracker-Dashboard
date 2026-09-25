"""
apps/services/no_po_vendors.py - the DB-touching layer over
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
from django.db.models.functions import TruncMonth

from apps.services.matching_core import cites_a_po
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


def purchases_without_po_summary(mir_model: type, known_pos=frozenset()) -> dict:
    """Every active MIR row that is a REAL third-party purchase with no
    purchase order behind it, counted per vendor and per month.

    THIS IS A DIFFERENT QUESTION FROM no_po_vendor_summary() ABOVE, and the
    difference is the whole point. That function answers "how many rows did
    the registry exclude, and why" - a statement about a list. This one
    answers "how many things did we buy without raising a PO" - a statement
    about the business, and a number somebody is supposed to drive DOWN.

    Why the registry cannot answer it. NO_PO_VENDORS models "no PO" as a
    property of the VENDOR, which is the wrong shape for what actually
    happens here (project owner, 2026-09-18: "sometimes they create a PO,
    sometimes they don't, mostly they don't"). A vendor-level flag has only
    two settings - suppress everything or suppress nothing - so a supplier
    who is PO'd half the time is misrepresented either way, and the registry
    silently goes stale the moment one starts being PO'd. Counting ROWS
    sidesteps that entirely: a row either has an order behind it or it does
    not, and that is knowable per row without anyone maintaining a list.

    Membership, deliberately narrow:
      - INTERNAL_TRANSFER parties are excluded. An inter-plant movement is
        not a purchase and will never generate a PO; including it would bury
        the real number under ~390 rows of noise at Achhad alone.
      - Everyone else with no PO behind the row is IN, whether or not they
        are registered. "No PO behind it" is matching_core.cites_a_po()
        answering False against `known_pos` (the plant's
        known_po_numbers()) - the same test mir_without_po.py's NO_PO bucket
        uses, so the badge and the drill-down cannot disagree. A short HRS
        legacy number naming one of our orders is a PO, not a gap. `registered` distinguishes the two on the way out:
        a registered vendor is a KNOWN process gap, an unregistered one is a
        surprise nobody has looked at yet, and the second is the more urgent
        of the two.

    Returns totals plus `vendors` (descending row count) and `byMonth`
    (chronological) - the second is what makes this a trend rather than a
    number, since the only interesting question about a process gap is
    whether it is closing.
    """
    rows = (
        mir_model.objects.filter(is_active=True)
        .exclude(party_name="")
        .values("party_name", "po_number_raw")
        .annotate(n=Count("id"))
    )
    per_vendor: dict[str, int] = {}
    for row in rows:
        if cites_a_po(row["po_number_raw"], known_pos):
            continue
        entry = no_po_vendor_entry(row["party_name"])
        if entry and entry[0] == INTERNAL_TRANSFER:
            continue
        per_vendor[row["party_name"]] = per_vendor.get(row["party_name"], 0) + row["n"]

    vendors, registered, unregistered = [], 0, 0
    for name, n in per_vendor.items():
        entry = no_po_vendor_entry(name)
        is_registered = entry is not None and entry[0] == NO_PO_SUPPLIER
        registered += n if is_registered else 0
        unregistered += 0 if is_registered else n
        vendors.append({
            "vendor": name,
            "rowCount": n,
            "registered": is_registered,
            "reason": entry[1] if entry else "",
        })
    vendors.sort(key=lambda v: (-v["rowCount"], v["vendor"]))

    # Second pass for the trend. Kept as its own aggregate rather than
    # widening the query above: adding the month to that GROUP BY would
    # multiply its row count by the number of months for a figure the
    # per-vendor breakdown does not need.
    month_rows = (
        mir_model.objects.filter(is_active=True)
        .exclude(party_name="")
        .annotate(m=TruncMonth("mir_date"))
        .values("m", "party_name", "po_number_raw")
        .annotate(n=Count("id"))
    )
    per_month: dict[str, int] = {}
    for row in month_rows:
        if row["m"] is None or cites_a_po(row["po_number_raw"], known_pos):
            continue
        entry = no_po_vendor_entry(row["party_name"])
        if entry and entry[0] == INTERNAL_TRANSFER:
            continue
        key = row["m"].strftime("%Y-%m")
        per_month[key] = per_month.get(key, 0) + row["n"]

    return {
        "total": registered + unregistered,
        "registered": registered,
        "unregistered": unregistered,
        "vendors": vendors,
        "byMonth": [{"month": k, "rowCount": per_month[k]} for k in sorted(per_month)],
    }
