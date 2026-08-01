"""
PO-vs-MIR-vs-Stock reconciliation - the engine behind PurchaseOrder.status
("Ordered" -> "Material Inwarded" -> "Received in Inventory") and POItem.matched.

This is a direct Python port of the matching algorithm from the original
Purchase Tracker Cowork artifact (C:\\Users\\Admin\\Claude\\Artifacts\\
purchase-tracker\\index.html), per Dishant's explicit description of the real
workflow:
  - Vendor name is a HARD GATE, not a scored factor - two different vendors
    are never the same PO, no matter how well material/amount line up.
  - PO Number (when the MIR row has one - not every plant's MIR file records
    it) is checked first as a free, unambiguous top tier.
  - Otherwise, within the matching vendor's rows, each PO item line is
    scored against each MIR row on Material Description token overlap
    (weight 0.55) + closeness of pre-tax amount (weight 0.45) - PO Net Value
    (per item line, pre-tax) versus MIR Taxable Value (pre-tax), never the
    whole-PO Total Value and never a tax-inclusive figure.
  - Once an MIR row is used by one PO item line, it's marked consumed and
    removed from the pool for every other PO, so a recurring vendor/item
    can't have the same receipt silently satisfy two different POs.

"Inwarded" = at least one item line matched. "Received in Inventory"
("stocked") = inwarded AND that material shows a positive quantity in the
plant's current RM Stock snapshot - a material-level approximation, since RM
Stock isn't tracked per-PO, only per-material.

This module only computes; it never touches Drive or Postgres itself - see
core/management/commands/reconcile_purchase_orders.py for the command that
wires this to live data and writes the results back.
"""
import re
from datetime import date, datetime
from decimal import Decimal

STOPWORDS = {
    "with", "from", "free", "door", "delivery", "days", "after", "receipt",
    "material", "private", "limited", "company", "ravasco", "packing", "transmission",
}


def tokenize(text):
    text = (text or "").lower()
    words = re.sub(r"[^a-z0-9 ]", " ", text).split()
    return [w for w in words if len(w) >= 4 and w not in STOPWORDS]


def norm_vendor(name):
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"private limited|pvt\.?\s*ltd\.?|pvt\.?|ltd\.?|limited|llp|inc\.?|corp(oration)?\.?|co\.?\b|company", "", name)
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name.strip()


def _to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text == "-":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def build_mir_index(mir_rows):
    by_po_exact = {}
    by_vendor = {}
    for row in mir_rows:
        po = (row.get("po_number") or "").strip()
        if po:
            by_po_exact.setdefault(po, []).append(row)
        vn = norm_vendor(row.get("party_name"))
        if vn:
            by_vendor.setdefault(vn, []).append(row)
    return by_po_exact, by_vendor


def _score(item_desc, item_net_value, mir_row):
    item_toks = tokenize(item_desc)
    row_toks = tokenize(mir_row.get("material_desc"))
    overlap = len([t for t in item_toks if t in row_toks])
    material_score = (overlap / len(item_toks)) if item_toks else 0.0

    item_amt = _to_float(item_net_value)
    row_amt = _to_float(mir_row.get("taxable_value"))
    amount_score = 0.0
    if item_amt > 0 and row_amt > 0:
        amount_score = 1 - min(1.0, abs(item_amt - row_amt) / max(item_amt, row_amt))

    return 0.55 * material_score + 0.45 * amount_score, material_score


def _is_acceptable(combined, material_score):
    return material_score > 0 and combined >= 0.45


def match_item_to_mir(item_desc, item_net_value, vendor_name, po_number, by_po_exact, by_vendor):
    """Returns the matched MIR row (marked consumed) or None. Mutates the
    row's _consumed flag on success, same as the artifact's mark-and-remove
    approach, so a later item/PO in the same reconciliation run can't reuse
    the same MIR receipt."""
    v_norm = norm_vendor(vendor_name)
    if not v_norm:
        return None  # can't verify vendor identity - never guess a match

    pool = []
    if po_number:
        pool = [r for r in by_po_exact.get(po_number.strip(), []) if not r["_consumed"] and norm_vendor(r.get("party_name")) == v_norm]
    if not pool:
        pool = [r for r in by_vendor.get(v_norm, []) if not r["_consumed"]]
    if not pool:
        return None

    scored = []
    for row in pool:
        combined, material_score = _score(item_desc, item_net_value, row)
        if _is_acceptable(combined, material_score):
            scored.append((combined, row))
    if not scored:
        return None

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_row = scored[0][1]
    best_row["_consumed"] = True
    return best_row


def qty_rate_discrepancy_notes(item_desc, item_qty, item_net_value, mir_row):
    """Mirrors the artifact's >10% difference threshold for flagging
    quantity/rate discrepancies on an already-matched item line."""
    notes = []
    item_qty_f = _to_float(item_qty)
    mir_qty_f = _to_float(mir_row.get("qty"))
    if item_qty_f > 0 and mir_qty_f > 0:
        diff_pct = abs(item_qty_f - mir_qty_f) / item_qty_f * 100
        if diff_pct > 10:
            notes.append(
                f"{item_desc or 'Item'}: PO quantity {item_qty_f:,.2f} versus MIR received quantity "
                f"{mir_qty_f:,.2f}, a difference of {diff_pct:.1f} percent"
            )
    item_amt_f = _to_float(item_net_value)
    mir_amt_f = _to_float(mir_row.get("taxable_value"))
    if item_amt_f > 0 and mir_amt_f > 0:
        diff_pct = abs(item_amt_f - mir_amt_f) / item_amt_f * 100
        if diff_pct > 10:
            notes.append(
                f"{item_desc or 'Item'}: PO net value {item_amt_f:,.2f} (pre-tax) versus MIR taxable value "
                f"{mir_amt_f:,.2f} (pre-tax), a difference of {diff_pct:.1f} percent"
            )
    return notes


def reconcile_plant_pos(purchase_orders, mir_rows, latest_stock_by_material_desc):
    """Runs the full match for every domestic PO of one plant. Mutates each
    PurchaseOrder's .status and each POItem's .matched in place (callers are
    responsible for actually saving them), and returns a dict of
    {po_number: [discrepancy note, ...]} for callers to turn into POFlag rows.

    latest_stock_by_material_desc: {description.lower(): qty} from that
    plant's latest StockSnapshot rows - used only for the material-level
    "is this now in stock" check behind "Received in Inventory"."""
    by_po_exact, by_vendor = build_mir_index(mir_rows)
    discrepancies = {}

    for po in purchase_orders:
        items = list(po.items.all())
        matched_count = 0
        any_stocked = False
        notes = []

        for item in items:
            mir_row = match_item_to_mir(item.description, item.net_value, po.vendor_name, po.po_number, by_po_exact, by_vendor)
            item.matched = bool(mir_row)
            if mir_row:
                matched_count += 1
                notes.extend(qty_rate_discrepancy_notes(item.description, item.qty, item.net_value, mir_row))
                stock_qty = latest_stock_by_material_desc.get((item.description or "").strip().lower())
                if stock_qty and _to_float(stock_qty) > 0:
                    any_stocked = True

        if matched_count == 0:
            po.status = "ordered"
        elif any_stocked:
            po.status = "received_in_inventory"
        else:
            po.status = "material_inwarded"

        if notes:
            discrepancies[po.po_number] = notes

    return discrepancies
