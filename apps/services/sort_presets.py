"""
apps/services/sort_presets.py - Validation and persistence for per-user sort
presets (apps.core.models.SortPreset), behind apps/api/routers/preferences_views.py.

A preset is data a browser sends, so nothing in it is trusted: the name is
trimmed and length-checked, and every sort level must name a column key the
view actually sorts on. The keys mirror the frontend's column lists -
"materials" is material-sort.js's MAT_SORT_COLUMNS, "purchase_orders" is
po-sort.js's PO_SORT_COLUMNS, "import_purchases" is import-sort.js's
IMPORT_SORT_COLUMNS, and the Raw Material modal's "material_lots" /
"material_open_pos" are material-sort.js's MAT_LOTS_SORT_COLUMNS /
MAT_OPEN_PO_SORT_COLUMNS - keep them in step (test_sort_presets.py
checks); a key the frontend offers but this set lacks makes "Save preset"
fail with a 400, never store a column the page cannot sort on.

Errors are ValueError with a message, which apps/api/exceptions.py passes to
the user as a 400.
"""

from django.db import transaction

from apps.core.models import SortPreset

SORT_KEYS_BY_VIEW = {
    SortPreset.View.MATERIALS: {
        "latest", "material", "category", "subCategory", "stock", "value",
        "rate", "daysLeft", "pending", "pipeline",
    },
    SortPreset.View.PURCHASE_ORDERS: {
        "created", "poNumber", "vendor", "material", "category", "subCategory",
        "delivery", "value", "status",
    },
    SortPreset.View.IMPORT_PURCHASES: {
        "created", "poNumber", "vendor", "material", "category", "subCategory",
        "plant", "country", "delivery", "value", "blNumber", "stage",
    },
    SortPreset.View.MATERIAL_LOTS: {
        "plant", "vendor", "received", "category", "subCategory", "qty", "rate", "value",
    },
    SortPreset.View.MATERIAL_OPEN_POS: {
        "delivery", "created", "poNumber", "vendor", "plant", "qtyToCome", "valueToCome", "status",
    },
}
MAX_LEVELS = 5
MAX_NAME_LENGTH = 60
MAX_PRESETS_PER_VIEW = 25


def clean_view(view) -> str:
    if view not in SORT_KEYS_BY_VIEW:
        raise ValueError("Unknown view for sort presets.")
    return view


def clean_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Give the preset a name.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"A preset name can be at most {MAX_NAME_LENGTH} characters.")
    return name


def clean_levels(view: str, levels) -> list[dict]:
    """1 to MAX_LEVELS levels, each a known key sorted asc or desc, no key
    twice (a second level on the same column could never change the order)."""
    if not isinstance(levels, list) or not levels:
        raise ValueError("A preset needs at least one sort level.")
    if len(levels) > MAX_LEVELS:
        raise ValueError(f"A preset can have at most {MAX_LEVELS} sort levels.")
    allowed = SORT_KEYS_BY_VIEW[view]
    cleaned, seen = [], set()
    for level in levels:
        if not isinstance(level, dict):
            raise ValueError("Each sort level needs a column and a direction.")
        key, direction = level.get("key"), level.get("dir")
        if key not in allowed:
            raise ValueError("A sort level names a column this view cannot sort on.")
        if direction not in ("asc", "desc"):
            raise ValueError("A sort direction must be ascending or descending.")
        if key in seen:
            raise ValueError("Each column can appear only once in a sort.")
        seen.add(key)
        cleaned.append({"key": key, "dir": direction})
    return cleaned


def preset_dict(preset: SortPreset) -> dict:
    return {"id": preset.id, "view": preset.view, "name": preset.name, "levels": preset.levels}


def list_presets(user, view) -> list[dict]:
    view = clean_view(view)
    return [preset_dict(p) for p in SortPreset.objects.filter(user=user, view=view)]


@transaction.atomic
def save_preset(user, view, name, levels) -> tuple[dict, bool]:
    """Create the preset, or overwrite the levels of the user's preset of the
    same name in that view - Excel's "save over" behaviour; the page asks
    before it sends a name that already exists. Returns (preset, created)."""
    view = clean_view(view)
    name = clean_name(name)
    levels = clean_levels(view, levels)
    # Locks the user's row, so two tabs saving at once cannot both pass the
    # count check and land one preset over the cap.
    type(user).objects.select_for_update().filter(pk=user.pk).first()
    existing = SortPreset.objects.filter(user=user, view=view, name__iexact=name).first()
    if existing:
        existing.name = name
        existing.levels = levels
        existing.save(update_fields=["name", "levels", "updated_at"])
        return preset_dict(existing), False
    if SortPreset.objects.filter(user=user, view=view).count() >= MAX_PRESETS_PER_VIEW:
        raise ValueError(f"You can keep at most {MAX_PRESETS_PER_VIEW} presets here - delete one first.")
    preset = SortPreset.objects.create(user=user, view=view, name=name, levels=levels)
    return preset_dict(preset), True


def update_preset(preset: SortPreset, name=None, levels=None) -> dict:
    """Rename and/or replace the levels of one of the user's own presets."""
    fields = []
    if name is not None:
        name = clean_name(name)
        clash = SortPreset.objects.filter(user=preset.user, view=preset.view, name__iexact=name).exclude(pk=preset.pk)
        if clash.exists():
            raise ValueError("You already have a preset with that name.")
        preset.name = name
        fields.append("name")
    if levels is not None:
        preset.levels = clean_levels(preset.view, levels)
        fields.append("levels")
    if not fields:
        raise ValueError("Nothing to change.")
    preset.save(update_fields=fields + ["updated_at"])
    return preset_dict(preset)
