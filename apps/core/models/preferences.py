"""
apps/core/models/preferences.py - Per-user saved view preferences: sort presets.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


class SortPreset(models.Model):
    """A named, multi-level sort a user saved for one list view (2026-09-28,
    project owner: "sort presets like we have in Excel and Sheets", saved to
    the user's account so it follows them to any device).

    Owned by exactly one user and visible only to them - every endpoint
    filters on `user=request.user`. `levels` is an ordered list of
    {"key": <column key>, "dir": "asc"|"desc"}; the keys a view accepts are
    validated by apps/services/sort_presets.py, never trusted from the body.
    A preset holds no plant data, so it has no plant scope."""

    class View(models.TextChoices):
        MATERIALS = "materials", "Raw Material Analysis"

    user = models.ForeignKey("PTUser", on_delete=models.CASCADE, related_name="sort_presets")
    view = models.CharField(max_length=30, choices=View.choices)
    name = models.CharField(max_length=60)
    levels = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "view", "name"], name="uniq_sort_preset_name_per_user_view"),
        ]

    def __str__(self):
        return f"{self.user_id}/{self.view}/{self.name}"
