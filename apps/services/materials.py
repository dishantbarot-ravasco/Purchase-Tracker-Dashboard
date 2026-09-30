"""
apps/services/materials.py - the material master (apps/core/models/
procurement.py's Material, 2026-09-29).

A material exists once, company-wide, keyed on material_identity.
material_key() of its name (normalize_material(), with a fabric roll's
length, roll count and weight dropped), and holds its category and sub-category. PO lines point at it; a MIR
line reads its category through its PO line, so one material can never be
filed two ways on two receipts.

Where a category comes from, in order:
  1. MaterialCategoryReference (the plant manager's list), when it knows the
     material - by name, the same key the dashboard files stock lots by, or
     failing that by SAP item code when exactly one row of the list has it;
  2. otherwise the first MIR against the material: the clerk picks it once
     (set_category()), and every later receipt shows it read-only.

A material also holds its base unit (2026-09-30): KG, L, NOS or M, the unit
its RM stock is kept in. It starts as the base of the unit it was first seen
in (base_unit_for()); an editor may change it and enter pack factors (1 ROLL =
660 M) with set_units(). unit_factor() is what a MIR receipt converts by.
"""

import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.services import procurement_rules, stock_rules
from apps.services.material_identity import material_key, material_name
from apps.services.parsers.common import normalize_material


def base_unit_for(raw_uom: str) -> str:
    """The base unit (KG, L, NOS, M) a unit converts into exactly, from a PO
    line's unit or the reference list's "Kilogram (KG)"; "" when none."""
    code = procurement_rules.canonical_uom(raw_uom)[0]
    if not stock_rules.base_of(code):
        m = re.search(r"\(([^)]+)\)\s*$", raw_uom or "")
        code = procurement_rules.canonical_uom(m.group(1))[0] if m else code
    return stock_rules.base_of(code)


def unit_factor(material, uom: str) -> tuple[str, Decimal]:
    """(unit a MIR receipt of `material` in `uom` is held in, factor):
    stock_rules.to_base() with the material's base unit and pack factors."""
    if material is None:
        return (uom or "").strip().upper(), Decimal("1")
    factors = {f.uom: f.factor for f in material.unit_factors.all()}
    return stock_rules.to_base(uom, material.base_uom, factors)


def _reference_for(key):
    from apps.core.models import MaterialCategoryReference

    return MaterialCategoryReference.objects.filter(normalized_description=key).first()


def _reference_by_code(item_code):
    """The reference row for an SAP item code, only when exactly one row
    carries it - the list, like the PO sheets, can reuse a code."""
    from apps.core.models import MaterialCategoryReference

    code = (item_code or "").strip()
    if not code:
        return None
    rows = list(MaterialCategoryReference.objects.filter(sap_item_code=code)[:2])
    return rows[0] if len(rows) == 1 else None


def material_for(description: str, item_code: str = "", uom: str = "", hsn: str = ""):
    """The Material for a PO line's description, created on first sight
    (categorised from the reference list when it knows it). None for a
    blank description."""
    from apps.core.models import Material

    key = material_key(description)
    if not key:
        return None
    material = Material.objects.filter(name_key=key).first()
    if material is not None:
        # A material first seen without a unit takes its base unit from the
        # first PO line that has one; a base unit already set is never changed here.
        base = "" if material.base_uom else base_unit_for(uom)
        if base:
            material.base_uom = base
            material.save(update_fields=["base_uom", "updated_at"])
        return material
    ref = _reference_for(normalize_material(description)) or _reference_for(key) or _reference_by_code(item_code)
    return Material.objects.create(
        name=material_name(description)[:500], name_key=key, item_code=(item_code or "").strip()[:50],
        uom=(uom or "")[:20], hsn=(hsn or "").strip()[:20], base_uom=base_unit_for(uom),
        category=(ref.category or "").strip() if ref else "", subcategory=(ref.subcategory or "").strip() if ref else "",
    )


def set_category(material, category: str, subcategory: str, user) -> None:
    """File an uncategorised material, from the first MIR against it. A
    material that already has a category is left alone: changing a filed
    material is a master-data decision, not a receipt's."""
    if material is None or material.category:
        return
    material.category, material.subcategory = category, subcategory
    material.category_set_by_email = getattr(user, "email", "")
    material.category_set_at = timezone.now()
    material.save(update_fields=["category", "subcategory", "category_set_by_email", "category_set_at", "updated_at"])


def sync_from_reference() -> int:
    """Bring every material the reference list knows in line with it (the
    list wins: it is the plant manager's decision). Run after
    load_material_category_reference. Returns how many materials changed."""
    from apps.core.models import Material, MaterialCategoryReference

    changed = 0
    for ref in MaterialCategoryReference.objects.all():
        key = ref.normalized_description
        if not key:
            continue
        category, sub = (ref.category or "").strip(), (ref.subcategory or "").strip()
        material, created = Material.objects.get_or_create(name_key=key, defaults={
            "name": ref.description.strip()[:500], "item_code": (ref.sap_item_code or "").strip()[:50],
            "uom": (ref.uom or "").strip()[:20], "hsn": (ref.hsn_code or "").strip()[:20],
            "category": category, "subcategory": sub, "base_uom": base_unit_for(ref.uom),
        })
        if created:
            changed += 1
        elif (material.category, material.subcategory) != (category, sub):
            material.category, material.subcategory = category, sub
            material.category_set_by_email, material.category_set_at = "", None
            material.save(update_fields=["category", "subcategory", "category_set_by_email", "category_set_at", "updated_at"])
            changed += 1
    return changed


class MaterialError(ValueError):
    pass


def change_category(material, category: str, subcategory: str, reason: str, user) -> None:
    """Correct a filed material's category (a wrong first pick, or a
    reference-list mistake). From the reference list's categories only,
    with a reason, logged in MaterialChange. The list still wins on its next
    reload, so a list that is itself wrong must be fixed there too."""
    from django.db import transaction

    from apps.core.models import Material, MaterialChange
    from apps.services.mir_service import category_options

    category, subcategory, reason = (category or "").strip(), (subcategory or "").strip(), (reason or "").strip()
    options = category_options()
    if not reason:
        raise MaterialError("Say why the category is being changed.")
    if not category or (options and category not in options):
        raise MaterialError("Choose a category from the list.")
    if subcategory and options and subcategory not in options[category]:
        raise MaterialError(f"Not a sub-category of {category}.")
    with transaction.atomic():
        material = Material.objects.select_for_update().get(pk=material.pk)
        if (material.category, material.subcategory) == (category, subcategory):
            raise MaterialError("That is already this material's category.")
        for field, new in (("category", category), ("subcategory", subcategory)):
            old = getattr(material, field)
            if old != new:
                MaterialChange.objects.create(material=material, field=field, old_value=old, new_value=new,
                                              reason=reason, changed_by=user, changed_by_email=getattr(user, "email", ""))
        material.category, material.subcategory = category, subcategory
        material.category_set_by_email = getattr(user, "email", "")
        material.category_set_at = timezone.now()
        material.save(update_fields=["category", "subcategory", "category_set_by_email", "category_set_at", "updated_at"])



def _plain(value: Decimal) -> str:
    """660, not 6.6E+2 or 660.000000."""
    return format(value.normalize(), "f")


def set_units(material, base_uom: str, factors, reason: str, user) -> None:
    """Set a material's base unit and its pack factors ({unit: base units per
    one}, a blank factor removing that unit), with a reason, logged in
    MaterialChange. Only MIRs posted afterwards convert by the new setting;
    receipts already in the store keep the unit they came in with."""
    from django.db import transaction

    from apps.core.models import Material, MaterialChange, MaterialUnitFactor

    base = (base_uom or "").strip().upper()
    reason = (reason or "").strip()
    if base and base not in stock_rules.BASE_UNITS:
        raise MaterialError("The base unit is KG, L, NOS or M.")
    if not reason:
        raise MaterialError("Say why the units are being changed.")
    wanted = {}
    for raw, value in (factors or {}).items():
        unit = procurement_rules.canonical_uom(raw)[0]
        if not unit:
            continue
        if stock_rules.base_of(unit):
            raise MaterialError(f"{unit} converts exactly already - no factor is needed.")
        text = str(value if value is not None else "").strip().replace(",", "")
        if not text:
            wanted[unit] = None
            continue
        try:
            factor = Decimal(text)
        except InvalidOperation:
            raise MaterialError(f"The factor for {unit} is not a number.") from None
        if not factor.is_finite() or factor <= 0 or factor != factor.quantize(Decimal("0.000001")):
            raise MaterialError(f"The factor for {unit} must be more than zero, at most 6 decimal places.")
        wanted[unit] = factor
    if any(v is not None for v in wanted.values()) and not base:
        raise MaterialError("Choose the base unit the factors convert into.")
    who = {"changed_by": user, "changed_by_email": getattr(user, "email", "")}
    with transaction.atomic():
        material = Material.objects.select_for_update().get(pk=material.pk)
        changed = False
        if material.base_uom != base:
            MaterialChange.objects.create(material=material, field="base_uom", old_value=material.base_uom, new_value=base,
                                          reason=reason, **who)
            material.base_uom = base
            material.save(update_fields=["base_uom", "updated_at"])
            changed = True
        existing = {f.uom: f for f in material.unit_factors.all()}
        for unit, factor in wanted.items():
            old = existing.get(unit)
            if factor is None:
                if old:
                    MaterialChange.objects.create(material=material, field=f"factor {unit}", old_value=_plain(old.factor),
                                                  new_value="", reason=reason, **who)
                    old.delete()
                    changed = True
            elif old is None or old.factor != factor:
                MaterialChange.objects.create(material=material, field=f"factor {unit}",
                                              old_value=_plain(old.factor) if old else "", new_value=_plain(factor),
                                              reason=reason, **who)
                MaterialUnitFactor.objects.update_or_create(material=material, uom=unit, defaults={
                    "factor": factor, "updated_by": user, "updated_by_email": getattr(user, "email", "")})
                changed = True
        if not changed:
            raise MaterialError("Nothing changed.")
