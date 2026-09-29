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
"""

from django.utils import timezone

from apps.services.material_identity import material_key, material_name
from apps.services.parsers.common import normalize_material


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
        return material
    ref = _reference_for(normalize_material(description)) or _reference_for(key) or _reference_by_code(item_code)
    return Material.objects.create(
        name=material_name(description)[:500], name_key=key, item_code=(item_code or "").strip()[:50],
        uom=(uom or "")[:20], hsn=(hsn or "").strip()[:20],
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
            "category": category, "subcategory": sub,
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

