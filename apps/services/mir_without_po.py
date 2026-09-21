"""
apps/services/mir_without_po.py — the drill-down behind the "purchased
without a PO" number: WHICH receipts have no purchase order behind them,
and, for each one, whether that is the final answer or only the current one.

Why this exists. no_po_vendors.py already counts these rows
(purchases_without_po_summary()), and main.js already shows the count as a
badge with a per-vendor tooltip. The count is what makes the process gap
visible; it is not enough to act on. The project owner's question
(2026-09-21) was the obvious next one: "we have orders without a PO in MIR,
which might be true or waiting for a PO to be matched with them - can we
show info about them?" - i.e. a receipt with no order against it is not one
condition but THREE, and they need completely different people to do
completely different things:

  NO_PO              MIR names no order at all. Nothing is pending; either
                     the material was genuinely bought without raising a PO
                     (a purchasing process gap), or somebody left the cell
                     blank (a data-entry gap). Nobody downstream can fix
                     this by waiting.

  PO_UNKNOWN         MIR names an order we have never received. The receipt
                     is not the problem - our PO master is incomplete, and
                     the fix is upstream, in the generator that produces the
                     master CSV (see the PO_MIR_Audit/ gap reports). This is
                     the bucket that is genuinely "waiting for a PO".

  PO_KNOWN_UNMATCHED MIR names an order we DO hold, and the matcher still
                     did not link this receipt to any of its line items.
                     That is ours to fix: a quantity/description drift, a
                     line item already claimed by an earlier receipt, or a
                     vendor spelling the vendor gate rejects.

Rolling those three into one number is what made the whole thing
unactionable - "348 unmatched" reads as one large failure, when it is a
purchasing question, an upstream data question and a matcher question
sitting in one pile. Split, each bucket has an owner.

What decides the bucket, and why it is the matcher's own logic. Membership
is NOT a fresh interpretation of po_number_raw - it reuses
matching_core._names_known_po()/known_po_numbers(), the same pair the
contradiction gate uses. That matters: HRS writes its legacy PO series five
different ways (see PO_MIR_Audit/PO_number_convention.md), so a plain
string comparison against the PO table puts ~120 HRS rows in PO_UNKNOWN
that the matcher itself considers perfectly well known. A drill-down that
disagrees with the matcher about what "we have this order" means would send
somebody upstream to chase POs that are already in the database.

INTERNAL_TRANSFER parties are excluded throughout, for the same reason
purchases_without_po_summary() excludes them: an inter-plant movement is not
a purchase, will never have a PO, and at ~390 rows at Achhad alone would
bury every other bucket. They are counted separately (`internalTransfer`)
rather than silently dropped - the rule this whole area works under is
"suppress the match, never the row" (see no_po_vendors.py's header).

Two deliberate scope choices:

  - A NO_PO row that the matcher nonetheless matched (material-tier - no PO
    number is needed for that) is still listed, flagged `matched`. It is
    reconciled, so it is not a matching problem, but it IS still a purchase
    made without an order, which is the thing being driven down. Keeping it
    here is also what makes this module's NO_PO total reconcile exactly with
    the badge from purchases_without_po_summary(); if the two disagreed, the
    drill-down would look broken.
  - PO_UNKNOWN/PO_KNOWN_UNMATCHED list only UNMATCHED rows. A receipt that
    cites a PO and got matched to it is simply reconciled, and there is
    nothing to show.
"""

from django.db.models import Count

from apps.services.matching_core import known_po_numbers, _names_known_po
from apps.services.parsers.common import (
    INTERNAL_TRANSFER,
    NO_PO_SUPPLIER,
    is_usable_po_reference,
    no_po_vendor_entry,
)

# Bucket ids. Strings, not an enum: they cross the API boundary into
# main.js/no-po-panel.js as-is and appear in the CSV export, so the wire
# name and the Python name must not be able to drift apart.
NO_PO = "no_po"
PO_UNKNOWN = "po_unknown"
PO_KNOWN_UNMATCHED = "po_known_unmatched"

BUCKET_LABELS = {
    NO_PO: "No PO number in MIR",
    PO_UNKNOWN: "Names a PO we don't hold",
    PO_KNOWN_UNMATCHED: "PO on file, not yet matched",
}

# The order the panel shows them in, and the order the CSV is sorted by.
# Deliberately not alphabetical: it runs from "nobody is coming to fix this"
# to "this is ours to fix", which is also roughly increasing actionability.
BUCKET_ORDER = (NO_PO, PO_UNKNOWN, PO_KNOWN_UNMATCHED)


def _matched_mir_ids(match_config) -> set:
    """Every MIR row id this plant has a PO<->MIR match for, domestic or
    import.

    Both models, not just the domestic one: a receipt matched to an IMPORT
    purchase order is reconciled just as completely, and omitting them would
    park real, fully-matched rows in PO_KNOWN_UNMATCHED where somebody would
    go looking for a matcher bug that does not exist."""
    ids = set()
    for model in (match_config.po_mir_match_model, match_config.import_po_mir_match_model):
        ids.update(model.objects.values_list("mir_entry_id", flat=True))
    return ids


def classify_mir_row(po_number_raw: str, party_name: str, matched: bool, known_pos) -> str | None:
    """Which bucket this one MIR row belongs in, or None if it belongs in
    none of them (reconciled, or an inter-plant transfer).

    Split out from the queryset walk below so the classification rule - the
    part with the business meaning - is testable without a database, and so
    the CSV export and the JSON summary cannot drift into disagreeing about
    what a bucket means."""
    entry = no_po_vendor_entry(party_name or "")
    if entry and entry[0] == INTERNAL_TRANSFER:
        return None
    if not is_usable_po_reference(po_number_raw or ""):
        # Listed whether or not it matched - see the module docstring's
        # first scope choice for why.
        return NO_PO
    if matched:
        return None
    return PO_KNOWN_UNMATCHED if _names_known_po(po_number_raw, known_pos) else PO_UNKNOWN


def mir_without_po_rows(mir_model: type, match_config) -> list[dict]:
    """Every active MIR row that has no purchase order standing behind it,
    one dict per row, already bucketed.

    One pass over the MIR table and two id-only queries, no per-row lookups:
    `known_pos` is a frozenset built once (known_po_numbers()) and
    `_matched_mir_ids()` is a single values_list per match model. Vapi's MIR
    is ~1,500 active rows today and grows monthly, and this endpoint is hit
    by a panel the user can reopen freely, so it must not be O(rows) in
    queries.

    Sorted newest-first within each bucket: a receipt booked last week with
    no order behind it is still fixable, one from eighteen months ago
    usually is not."""
    known_pos = known_po_numbers(match_config)
    matched_ids = _matched_mir_ids(match_config)

    rows = []
    fields = (
        "id", "mir_no", "mir_date", "po_number_raw", "party_name", "material_description",
        "qty", "uom", "invoice_no", "invoice_date", "material_category", "source_row_ref",
    )
    for r in mir_model.objects.filter(is_active=True).values(*fields, *_value_fields(mir_model)):
        matched = r["id"] in matched_ids
        bucket = classify_mir_row(r["po_number_raw"], r["party_name"], matched, known_pos)
        if bucket is None:
            continue
        registered = no_po_vendor_entry(r["party_name"] or "")
        rows.append({
            "mirId": r["id"],
            "bucket": bucket,
            "mirNo": r["mir_no"] or "",
            "mirDate": r["mir_date"].isoformat() if r["mir_date"] else None,
            "poNumberRaw": r["po_number_raw"] or "",
            "vendor": r["party_name"] or "",
            "description": r["material_description"] or "",
            "qty": r["qty"],
            "uom": r["uom"] or "",
            "value": _row_value(r),
            "invoiceNo": r["invoice_no"] or "",
            "invoiceDate": r["invoice_date"].isoformat() if r["invoice_date"] else None,
            "category": r["material_category"] or "",
            "sourceRowRef": r["source_row_ref"] or "",
            # Only ever true in the NO_PO bucket (the other two are
            # unmatched by definition) - it is what distinguishes "bought
            # without a PO but we know what it was" from "bought without a
            # PO and nothing has been reconciled against it".
            "matched": matched,
            # A vendor already on the NO_PO_SUPPLIER registry is a KNOWN
            # process gap; one that is not is a surprise nobody has looked
            # at, and the more urgent of the two. Same distinction
            # purchases_without_po_summary() draws, carried down to the row.
            "registeredNoPoVendor": bool(registered and registered[0] == NO_PO_SUPPLIER),
        })

    # Bucket ascending, then date DESCENDING within it - hence the two
    # passes rather than one key: Python's sort is stable, so sorting by
    # date first and by bucket second gives newest-first inside each bucket
    # without needing a negatable date key. A row with no mir_date sorts
    # last, which is where an undated row belongs.
    rows.sort(key=lambda r: (r["mirDate"] or "", r["mirNo"]), reverse=True)
    rows.sort(key=lambda r: BUCKET_ORDER.index(r["bucket"]))
    return rows


def _value_fields(mir_model: type) -> tuple:
    """Vapi's MIR has no `net` column at all - it carries `taxable_value`
    instead. Same real schema difference _MatchConfig.mir_value already
    exists for (see matching_vapi.py); resolved by asking the model rather
    than branching on a plant key, so a fourth plant needs no change here."""
    names = {f.name for f in mir_model._meta.get_fields() if hasattr(f, "name")}
    return tuple(n for n in ("net", "taxable_value", "total_amount") if n in names)


def _row_value(row: dict):
    """The rupee value of one receipt line, from whichever column this
    plant's MIR actually has. `net` first (HRS/Achhad's own pre-discount
    column, the one matching compares against), then `taxable_value`
    (Vapi's equivalent), then `total_amount` as a last resort - a number
    that is tax-inclusive is still far more use than a blank."""
    for name in ("net", "taxable_value", "total_amount"):
        value = row.get(name)
        if value is not None:
            return value
    return None


def mir_without_po_summary(mir_model: type, match_config, rows: list | None = None) -> dict:
    """Bucket counts, rupee value and a per-vendor breakdown, for the badge
    and the panel header.

    Built from mir_without_po_rows() rather than its own aggregate queries:
    the row list is at most a few hundred rows per plant (290/157/451 across
    all three at the time of writing), so a second set of GROUP BYs would
    buy nothing and would give this module two places where a bucket rule
    could be spelled differently. If this ever grows to the point where
    walking the rows is too slow, the rows themselves are the thing to
    paginate - not this function to re-derive.

    `internalTransfer` is reported even though those rows are excluded from
    every bucket, so a reader can see the exclusion rather than wonder where
    the rest of the MIR table went.

    `rows` lets a caller that already has the row list (the API endpoint
    returns both in one response) pass it in rather than paying for a second
    identical pass."""
    if rows is None:
        rows = mir_without_po_rows(mir_model, match_config)

    buckets = {}
    for key in BUCKET_ORDER:
        in_bucket = [r for r in rows if r["bucket"] == key]
        vendors: dict[str, dict] = {}
        for r in in_bucket:
            v = vendors.setdefault(r["vendor"], {"vendor": r["vendor"], "rowCount": 0, "value": 0})
            v["rowCount"] += 1
            v["value"] += float(r["value"] or 0)
        buckets[key] = {
            "label": BUCKET_LABELS[key],
            "rowCount": len(in_bucket),
            "value": sum(float(r["value"] or 0) for r in in_bucket),
            "vendors": sorted(vendors.values(), key=lambda v: (-v["rowCount"], v["vendor"])),
        }

    internal = 0
    for party_name, n in (
        mir_model.objects.filter(is_active=True)
        .exclude(party_name="")
        .values("party_name")
        .annotate(n=Count("id"))
        .values_list("party_name", "n")
    ):
        entry = no_po_vendor_entry(party_name)
        if entry and entry[0] == INTERNAL_TRANSFER:
            internal += n

    return {
        "total": len(rows),
        # Excludes the NO_PO rows the matcher reconciled anyway - the number
        # of receipts a human still has to do something about, which is not
        # the same as the number booked without an order.
        "openTotal": sum(1 for r in rows if not r["matched"]),
        "buckets": buckets,
        "internalTransfer": internal,
    }
