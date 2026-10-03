"""
Licence balances from Bill of Entry debits (owner, 2026-10-03). Advance
Authorisations and RoDTEP scrips only.

What a licence sanctioned comes from its own ledger (AdvanceLicense /
AdvanceLicenseMaterial, RodtepScrollEntry - synced from the licence files);
what it has given comes ONLY from LicenceDebit rows, written when a BOE
reading is approved. The Advance License sheet's own usage columns are not
used (one BOE was pasted onto six licences there), and the import CSV names a
licence without the amount, so neither is ever a debit.

check_debits() gives the warnings a BOE reviewer sees before approving: an
unknown licence, one expired on the BOE date, an item outside the
licence's materials, or a debit beyond what is left.
"""

from __future__ import annotations

import re
from decimal import Decimal

from apps.services.license_links import normalize_license_number

ZERO = Decimal("0")


def _active(license_type: str, number: str):
    from apps.core.models import LicenceDebit

    return LicenceDebit.objects.filter(is_active=True, shipment_line__is_active=True, license_type=license_type,
                                       license_number=normalize_license_number(number))


def debited(license_type: str, number: str, *, exclude_shipment_id=None) -> dict:
    """{"qty", "value", "duty"} debited so far from one licence."""
    qs = _active(license_type, number)
    if exclude_shipment_id:
        qs = qs.exclude(shipment_line__shipment_id=exclude_shipment_id)
    out = {"qty": ZERO, "value": ZERO, "duty": ZERO}
    for d in qs.only("qty", "value_inr", "duty_foregone"):
        out["qty"] += d.qty or ZERO
        out["value"] += d.value_inr or ZERO
        out["duty"] += d.duty_foregone or ZERO
    return out


def advance_licence(number: str):
    from apps.core.models import AdvanceLicense

    wanted = normalize_license_number(number)
    return next((lic for lic in AdvanceLicense.objects.prefetch_related("materials")
                 if normalize_license_number(lic.license_number) == wanted), None)


def rodtep_sanctioned(number: str) -> Decimal | None:
    from django.db.models import Sum

    from apps.core.models import RodtepScrollEntry

    wanted = normalize_license_number(number)
    rows = [r for r in RodtepScrollEntry.objects.values("script_no").annotate(total=Sum("sanctioned_amount"))
            if normalize_license_number(r["script_no"]) == wanted]
    return rows[0]["total"] if rows else None


def check_debits(debits: list[dict], items: list[dict], boe_date, shipment_id=None) -> list[dict]:
    """Warnings for a BOE's licence debits. `debits`: dicts with item (1-based),
    license_type, license_number and Decimal-or-None qty / value / duty;
    `items`: the BOE items (for HSN)."""
    out = []
    planned: dict = {}
    for d in debits:
        key = (d["license_type"], normalize_license_number(d["license_number"]))
        acc = planned.setdefault(key, {"qty": ZERO, "value": ZERO, "duty": ZERO})
        acc["qty"] += d.get("qty") or ZERO
        acc["value"] += d.get("value") or ZERO
        acc["duty"] += d.get("duty") or ZERO
    for (ltype, number), plan in planned.items():
        done = debited(ltype, number, exclude_shipment_id=shipment_id)
        if ltype == "ADVANCE":
            lic = advance_licence(number)
            if lic is None:
                out.append({"check": "licence_unknown", "line": None,
                            "message": f"Advance licence {number} is not in the licence ledger - add its file, or check the number."})
                continue
            if boe_date and lic.import_validity_date and boe_date > lic.import_validity_date:
                out.append({"check": "licence_expired", "line": None,
                            "message": f"Advance licence {number}'s import validity ended {lic.import_validity_date:%d-%m-%Y}, before this BOE."})
            left = (lic.cif_value_authorized or ZERO) - done["value"]
            if plan["value"] > left:
                out.append({"check": "licence_exceeded", "line": None,
                            "message": f"Advance licence {number} has {left} CIF value left; this BOE debits {plan['value']}."})
        else:
            sanctioned = rodtep_sanctioned(number)
            if sanctioned is None:
                out.append({"check": "licence_unknown", "line": None,
                            "message": f"RoDTEP scrip {number} is not in the scrip ledger - add its file, or check the number."})
                continue
            left = sanctioned - done["duty"]
            if plan["duty"] > left:
                out.append({"check": "licence_exceeded", "line": None,
                            "message": f"RoDTEP scrip {number} has {left} left; this BOE debits {plan['duty']}."})
    for d in debits:
        if d["license_type"] != "ADVANCE":
            continue
        lic = advance_licence(d["license_number"])
        item_no = d.get("item")
        item = items[item_no - 1] if isinstance(item_no, int) and 0 < item_no <= len(items) else None
        hsn = re.sub(r"\D", "", (item or {}).get("hsn") or "")[:4]
        codes = {re.sub(r"\D", "", m.itchs_code or "")[:4] for m in lic.materials.all()} if lic else set()
        if lic and hsn and codes and hsn not in codes:
            out.append({"check": "licence_item", "line": item_no,
                        "message": f"Item {item_no}: HSN {item['hsn']} is not among Advance licence {lic.license_number}'s materials."})
    return out


def boe_debits_summary(license_type: str, number: str, plant_codes=None) -> dict:
    """For a ledger row: the totals debited on approved BOEs (company-wide -
    a licence's balance is one company fact) and the debits themselves at
    the plants the reader may see."""
    qs = _active(license_type, number).select_related("shipment_line__shipment__plant", "shipment_line__po_line__purchase_order")
    rows, totals = [], {"qty": ZERO, "value": ZERO, "duty": ZERO}
    for d in qs:
        totals["qty"] += d.qty or ZERO
        totals["value"] += d.value_inr or ZERO
        totals["duty"] += d.duty_foregone or ZERO
        sh = d.shipment_line.shipment
        if plant_codes is None or sh.plant.code in plant_codes:
            rows.append({"boeNumber": sh.boe_number, "boeDate": sh.boe_date.isoformat() if sh.boe_date else None,
                         "plant": sh.plant.code, "poNumber": d.shipment_line.po_line.purchase_order.po_number,
                         "description": d.shipment_line.po_line.description, "qty": d.qty, "value": d.value_inr,
                         "duty": d.duty_foregone})
    return {"totals": totals, "debits": rows}
