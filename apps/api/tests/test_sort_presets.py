"""
Per-user sort presets (apps/api/routers/preferences_views.py, validated by
apps/services/sort_presets.py) - Raw Material Analysis's "Save preset".

What is pinned here:
  - a preset belongs to its user: another account can neither list, rename
    nor delete it (404, as if it did not exist);
  - viewers may save their own presets - a display preference, not a write
    to business data;
  - nothing in a preset is trusted: unknown columns, bad directions, a
    column twice, too many levels and blank names are refused with a 400;
  - saving under a name that exists saves over it (case-insensitively)
    rather than forking a second preset of the same name;
  - the per-view cap holds;
  - presets are kept per view (Raw Material, its modal's two tables,
    Purchase Orders, Import Purchases, the PO modals' lines and receipts);
  - a level's picked values and "show only these" round-trip, only on a
    column that offers them, and are checked like everything else;
  - each list's sortable (and pickable) columns and the server's agree.
"""

import re
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import SortPreset
from apps.services import sort_presets

URL = "/api/sort-presets"
LEVELS = [{"key": "category", "dir": "asc"}, {"key": "subCategory", "dir": "asc"}]


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _save(client, name="By category", levels=None, view="materials"):
    return client.post(URL, {"view": view, "name": name, "levels": LEVELS if levels is None else levels}, format="json")


@pytest.mark.django_db
class TestOwnership:
    def test_a_viewer_saves_and_lists_their_own_preset(self):
        user = make_user(email="v1@ravasco.com", role="viewer")
        client = _client(user)
        res = _save(client)
        assert res.status_code == 201
        assert res.data["levels"] == LEVELS
        listed = client.get(URL, {"view": "materials"}).data["presets"]
        assert [p["name"] for p in listed] == ["By category"]

    def test_another_user_cannot_see_rename_or_delete_it(self):
        owner = make_user(email="owner@ravasco.com")
        other = make_user(email="other@ravasco.com", role="admin")
        preset_id = _save(_client(owner)).data["id"]
        intruder = _client(other)
        assert intruder.get(URL, {"view": "materials"}).data["presets"] == []
        assert intruder.patch(f"{URL}/{preset_id}", {"name": "Mine now"}, format="json").status_code == 404
        assert intruder.delete(f"{URL}/{preset_id}").status_code == 404
        assert SortPreset.objects.get(id=preset_id).name == "By category"

    def test_the_owner_renames_updates_and_deletes(self):
        client = _client(make_user(email="o2@ravasco.com"))
        preset_id = _save(client).data["id"]
        res = client.patch(f"{URL}/{preset_id}", {"name": "Cat", "levels": [{"key": "value", "dir": "desc"}]}, format="json")
        assert res.status_code == 200
        assert res.data["name"] == "Cat" and res.data["levels"] == [{"key": "value", "dir": "desc"}]
        assert client.delete(f"{URL}/{preset_id}").status_code == 204
        assert not SortPreset.objects.filter(id=preset_id).exists()

    def test_unauthenticated_is_refused(self):
        assert APIClient().get(URL, {"view": "materials"}).status_code in (401, 403)


@pytest.mark.django_db
class TestValidation:
    @pytest.mark.parametrize("levels", [
        [],
        [{"key": "password_hash", "dir": "asc"}],
        [{"key": "category", "dir": "sideways"}],
        [{"key": "category", "dir": "asc"}, {"key": "category", "dir": "desc"}],
        [{"key": k, "dir": "asc"} for k in ("latest", "material", "category", "subCategory", "stock", "value")],
        "category",
        ["category"],
    ])
    def test_bad_levels_are_refused(self, levels):
        client = _client(make_user(email="val@ravasco.com"))
        res = _save(client, levels=levels)
        assert res.status_code == 400
        assert not SortPreset.objects.exists()

    @pytest.mark.parametrize("name", ["", "   ", "x" * 61, None])
    def test_bad_names_are_refused(self, name):
        res = _client(make_user(email="name@ravasco.com")).post(URL, {"view": "materials", "name": name, "levels": LEVELS}, format="json")
        assert res.status_code == 400

    def test_an_unknown_view_is_refused(self):
        client = _client(make_user(email="view@ravasco.com"))
        assert _save(client, view="purchase_orders_secret").status_code == 400
        assert client.get(URL, {"view": "nope"}).status_code == 400

    def test_renaming_onto_another_preset_name_is_refused(self):
        client = _client(make_user(email="clash@ravasco.com"))
        _save(client, name="A")
        b = _save(client, name="B").data["id"]
        assert client.patch(f"{URL}/{b}", {"name": "a"}, format="json").status_code == 400


@pytest.mark.django_db
class TestSaveOver:
    def test_the_same_name_saves_over_rather_than_duplicating(self):
        client = _client(make_user(email="over@ravasco.com"))
        first = _save(client, name="Weekly review")
        second = _save(client, name="weekly  REVIEW", levels=[{"key": "daysLeft", "dir": "asc"}])
        assert first.status_code == 201 and second.status_code == 200
        assert second.data["id"] == first.data["id"]
        assert SortPreset.objects.count() == 1
        assert SortPreset.objects.get().levels == [{"key": "daysLeft", "dir": "asc"}]

    def test_two_users_may_use_the_same_name(self):
        _save(_client(make_user(email="u1@ravasco.com")), name="Mine")
        assert _save(_client(make_user(email="u2@ravasco.com")), name="Mine").status_code == 201

    def test_the_per_view_cap_holds(self, monkeypatch):
        monkeypatch.setattr(sort_presets, "MAX_PRESETS_PER_VIEW", 2)
        client = _client(make_user(email="cap@ravasco.com"))
        assert _save(client, name="1").status_code == 201
        assert _save(client, name="2").status_code == 201
        assert _save(client, name="3").status_code == 400
        # Saving over an existing one is still allowed at the cap.
        assert _save(client, name="2").status_code == 200


@pytest.mark.parametrize("js_file, const, view", [
    ("material-sort.js", "MAT_SORT_COLUMNS", SortPreset.View.MATERIALS),
    ("po-sort.js", "PO_SORT_COLUMNS", SortPreset.View.PURCHASE_ORDERS),
    ("import-sort.js", "IMPORT_SORT_COLUMNS", SortPreset.View.IMPORT_PURCHASES),
    ("material-sort.js", "MAT_LOTS_SORT_COLUMNS", SortPreset.View.MATERIAL_LOTS),
    ("material-sort.js", "MAT_OPEN_PO_SORT_COLUMNS", SortPreset.View.MATERIAL_OPEN_POS),
    ("po-reconcile.js", "PO_LINES_SORT_COLUMNS", SortPreset.View.PO_LINES),
    ("po-reconcile.js", "PO_RECEIPTS_SORT_COLUMNS", SortPreset.View.PO_RECEIPTS),
])
def test_frontend_sort_columns_match_the_server_keys(js_file, const, view):
    """Each list offers exactly the columns the server accepts for its view,
    and marks `pick: true` exactly the columns the server lets pick values -
    a column the page offers but the server lacks would make "Save preset"
    fail."""
    js = (Path(__file__).resolve().parents[3] / "frontend" / "js" / js_file).read_text(encoding="utf-8")
    block = re.search(r"const " + const + r" = \[(.*?)\n\];", js, re.S)
    assert block, f"{const} not found in {js_file}"
    keys = set(re.findall(r"key: '(\w+)'", block.group(1)))
    assert keys == sort_presets.SORT_KEYS_BY_VIEW[view]
    pickable = set(re.findall(r"key: '(\w+)'[^\n]*pick: true", block.group(1)))
    assert pickable == sort_presets.PICK_KEYS_BY_VIEW[view]


@pytest.mark.django_db
class TestPickedValues:
    """A level can pick values to put first (Category -> Sub Category ->
    Material), and "show only these"; the server stores exactly that and
    refuses anything it could not mean."""

    def test_picked_values_and_only_round_trip(self):
        client = _client(make_user(email="picks@ravasco.com"))
        levels = [
            {"key": "category", "dir": "asc", "values": ["Polymers", "Chemicals"], "only": True},
            {"key": "subCategory", "dir": "asc", "values": ["Accelerators"]},
            {"key": "material", "dir": "asc"},
        ]
        res = _save(client, name="Pinned", levels=levels)
        assert res.status_code == 201
        assert res.data["levels"] == levels
        listed = client.get(URL, {"view": "materials"}).data["presets"][0]["levels"]
        assert listed == levels

    def test_an_empty_pick_list_is_dropped_with_its_only(self):
        """No values means no pick at all, so a stray "only" cannot hide
        every row."""
        client = _client(make_user(email="emptypick@ravasco.com"))
        res = _save(client, levels=[{"key": "category", "dir": "asc", "values": [], "only": True}])
        assert res.status_code == 201
        assert res.data["levels"] == [{"key": "category", "dir": "asc"}]

    @pytest.mark.parametrize("level", [
        {"key": "value", "dir": "desc", "values": ["1"]},                     # not a pick column
        {"key": "category", "dir": "asc", "values": "Polymers"},              # not a list
        {"key": "category", "dir": "asc", "values": [""]},                    # empty value
        {"key": "category", "dir": "asc", "values": [7]},                     # not text
        {"key": "category", "dir": "asc", "values": ["x" * 201]},             # too long
        {"key": "category", "dir": "asc", "values": ["A", "A"]},              # repeated
        {"key": "category", "dir": "asc", "values": [str(i) for i in range(51)]},  # too many
        {"key": "category", "dir": "asc", "values": ["A"], "only": "yes"},    # only not a bool
    ])
    def test_bad_picks_are_refused(self, level):
        res = _save(_client(make_user(email="badpick@ravasco.com")), levels=[level])
        assert res.status_code == 400
        assert not SortPreset.objects.exists()

    def test_a_view_without_pick_columns_refuses_picks(self):
        res = _save(_client(make_user(email="nopick@ravasco.com")), view="po_receipts",
                    levels=[{"key": "mirNo", "dir": "asc", "values": ["M1"]}])
        assert res.status_code == 400


@pytest.mark.django_db
class TestPerView:
    def test_presets_are_kept_per_view(self):
        """A Purchase Orders preset never shows on Raw Material, and a PO
        column is refused on the materials view (and vice versa)."""
        client = _client(make_user(email="views@ravasco.com"))
        po_levels = [{"key": "vendor", "dir": "asc"}, {"key": "created", "dir": "desc"}]
        assert _save(client, name="By vendor", levels=po_levels, view="purchase_orders").status_code == 201
        assert client.get(URL, {"view": "materials"}).data["presets"] == []
        assert [p["name"] for p in client.get(URL, {"view": "purchase_orders"}).data["presets"]] == ["By vendor"]
        assert _save(client, name="x", levels=po_levels, view="materials").status_code == 400
        assert _save(client, name="y", levels=[{"key": "daysLeft", "dir": "asc"}], view="purchase_orders").status_code == 400

    def test_the_same_name_may_exist_once_per_view(self):
        client = _client(make_user(email="sameview@ravasco.com"))
        assert _save(client, name="Mine", view="materials").status_code == 201
        assert _save(client, name="Mine", view="purchase_orders").status_code == 201
        assert SortPreset.objects.count() == 2
