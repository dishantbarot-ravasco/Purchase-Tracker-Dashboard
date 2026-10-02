"""
Layered access (project owner, 2026-10-02) - apps/api/permissions.py.

  1. An admin opens everything; only the owner (settings.OWNER_EMAIL) may
     make, unmake or change an admin.
  2. A user reads and works on exactly the plants in PTUser.plants - an
     empty list means none, never "all".
  3. A user also needs each page or action granted in PTUser.permissions;
     a user with none is locked.

Each test pins one of these with an account that holds the permission next
to one that does not, so it fails if the gate is removed as well as if it
is wrongly tightened.
"""

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.api.permissions import ALL_PERMISSIONS, PLANT_KEYS, Perm, requires
from apps.api.tests.factories import make_user
from apps.core.models import Document, HRSRMLot, MaterialCorrection, Plant, PTUser, PurchaseOrder
from apps.services import object_storage


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(email, permissions, plants=("hrs",)):
    return make_user(email=email, role="user", permissions=list(permissions), plants=list(plants))


# Representative reads, one per area, with the permission that opens each.
READS = [
    ("/api/purchase-orders", Perm.VIEW_DASHBOARD),
    ("/api/materials", Perm.VIEW_INVENTORY),
    ("/api/imports/purchase-orders", Perm.VIEW_DASHBOARD),
    ("/api/imports/advance-license", Perm.VIEW_DASHBOARD),
    ("/api/mir/entries", Perm.MIR_ENTRY),
    ("/api/stock/register", Perm.RM_STORE),
    ("/api/documents/po", Perm.PO_UPLOAD),
]


@pytest.mark.django_db
class TestLockedAccounts:
    @pytest.mark.parametrize("url,perm", READS)
    def test_a_locked_user_is_refused_everywhere_and_a_granted_one_is_not(self, url, perm):
        locked = make_user(email="locked@ravasco.com", role="locked")
        assert _client(locked).get(url).status_code == 403
        granted = _user("granted@ravasco.com", [perm])
        assert _client(granted).get(url).status_code == 200

    def test_a_permission_without_a_plant_opens_nothing(self):
        no_plants = _user("noplant@ravasco.com", ALL_PERMISSIONS, plants=())
        assert _client(no_plants).get("/api/purchase-orders").status_code == 403
        assert _client(no_plants).get("/api/mir/meta").status_code == 403

    def test_a_locked_user_still_learns_who_they_are(self):
        locked = make_user(email="locked2@ravasco.com", role="locked")
        res = _client(locked).get("/api/auth/me")
        assert res.status_code == 200
        assert (res.json()["permissions"], res.json()["plants"]) == ([], [])

    def test_me_lists_every_plant_and_permission_for_an_admin_and_exactly_the_grant_for_a_user(self):
        admin = make_user(email="admin@ravasco.com", role="admin")
        me = _client(admin).get("/api/auth/me").json()
        assert (me["plants"], sorted(me["permissions"])) == (list(PLANT_KEYS), sorted(ALL_PERMISSIONS))
        user = _user("buyer@ravasco.com", [Perm.PO_UPLOAD], plants=("vapi",))
        me = _client(user).get("/api/auth/me").json()
        assert (me["plants"], me["permissions"]) == (["vapi"], [Perm.PO_UPLOAD])


@pytest.mark.django_db
class TestPlants:
    def test_an_empty_plant_list_no_longer_means_every_plant(self):
        scoped = _user("vapi@ravasco.com", [Perm.VIEW_DASHBOARD], plants=("vapi",))
        assert _client(scoped).get("/api/vapi/purchase-orders").status_code == 200
        assert _client(scoped).get("/api/purchase-orders").status_code == 403
        assert _client(scoped).get("/api/achhad/purchase-orders").status_code == 403

    def test_an_admin_reaches_every_plant_even_with_a_stray_plant_list(self):
        admin = make_user(email="admin2@ravasco.com", role="admin", plants=["vapi"])
        for prefix in ("", "achhad/", "vapi/"):
            assert _client(admin).get(f"/api/{prefix}purchase-orders").status_code == 200


@pytest.mark.django_db
class TestViewPermissions:
    def test_each_view_opens_its_own_data_and_not_the_others(self):
        inventory = _client(_user("inv@ravasco.com", [Perm.VIEW_INVENTORY]))
        assert inventory.get("/api/materials").status_code == 200
        assert inventory.get("/api/purchase-orders").status_code == 403
        dashboard = _client(_user("dash@ravasco.com", [Perm.VIEW_DASHBOARD]))
        assert dashboard.get("/api/purchase-orders").status_code == 200
        assert dashboard.get("/api/materials").status_code == 403
        on_order = _client(_user("ord@ravasco.com", [Perm.VIEW_ON_ORDER]))
        assert on_order.get("/api/materials").status_code == 200
        assert on_order.get("/api/purchase-orders").status_code == 200
        assert on_order.get("/api/mir-without-po").status_code == 403

    def test_edits_need_edit_fields(self):
        viewer = _client(_user("view@ravasco.com", [Perm.VIEW_DASHBOARD, Perm.VIEW_INVENTORY]))
        assert viewer.patch("/api/purchase-orders/X1/fields", {}, format="json").status_code == 403
        editor = _client(_user("edit@ravasco.com", [Perm.EDIT_FIELDS]))
        # Past the gate: an unknown PO is a 404, not a refusal.
        assert editor.patch("/api/purchase-orders/X1/fields", {}, format="json").status_code != 403


@pytest.mark.django_db
class TestRawMaterialAnalysisIsAdminOnly:
    def test_the_reconciliation_layer_is_left_out_of_materials_for_anyone_but_an_admin(self):
        lot = HRSRMLot.objects.create(description="Natural Rubber", category="Rubber", basic_rate=Decimal("120"),
                                      opening_stock=Decimal("10"), todays_stock=Decimal("10"))
        MaterialCorrection.objects.create(plant="HRS", lot_id=lot.id, field_name="description",
                                          old_value="NR", new_value="Natural Rubber")
        admin_row = _client(make_user(email="a@ravasco.com", role="admin")).get("/api/materials").json()["materials"][0]
        assert len(admin_row["corrections"]) == 1
        assert admin_row["mirMatched"] is False

        user = _user("inv2@ravasco.com", [Perm.VIEW_INVENTORY, Perm.EDIT_FIELDS])
        row = _client(user).get("/api/materials").json()["materials"][0]
        assert (row["corrections"], row["mirStockMatches"], row["dataQualityFlags"], row["mirMatched"]) == ([], [], [], None)
        # The stock figures the plant tabs need are still there.
        assert (row["description"], row["qty"]) == ("Natural Rubber", 10.0)


@pytest.mark.django_db
class TestFilesFollowTheirKind:
    @pytest.fixture(autouse=True)
    def _no_r2(self, monkeypatch):
        monkeypatch.setattr(object_storage, "require_configured", lambda kind: None)
        monkeypatch.setattr(object_storage, "is_configured", lambda kind: True)
        monkeypatch.setattr(object_storage, "upload_file", lambda *a, **k: None)

    def _upload(self, user, kind, reference=""):
        upload = SimpleUploadedFile("f.pdf", b"%PDF-1.4 x " + kind.encode(), content_type="application/pdf")
        body = {"plant": "hrs", "poNumber": "TEST-0001", "kind": kind, "reference": reference, "file": upload}
        return _client(user).post("/api/documents/po/upload", body, format="multipart")

    def test_a_po_copy_needs_po_upload_and_an_import_paper_needs_import_docs(self):
        buyer = _user("buyer2@ravasco.com", [Perm.PO_UPLOAD])
        customs = _user("customs@ravasco.com", [Perm.IMPORT_DOCS])
        assert self._upload(buyer, "PO").status_code == 201
        assert self._upload(buyer, "BOE", "4026152").status_code == 403
        assert self._upload(customs, "BOE", "4026152").status_code == 201
        assert self._upload(customs, "PO").status_code == 403


@pytest.mark.django_db
class TestCrossPlantPoLookupHidesFiles:
    def test_another_plants_po_is_found_but_its_files_are_listed_only_to_that_plant(self):
        """Any plant may receive any plant's PO (owner rule 2026-09-28), so
        the PO itself is cross-plant - its uploaded copies are not."""
        vapi = Plant.objects.get(code="vapi")
        po = PurchaseOrder.objects.create(plant=vapi, po_number="1000009999")
        Document.objects.create(kind="PO", plant=vapi, po_number=po.po_number, revision=1, storage_key="vapi/k.pdf",
                                original_filename="k.pdf", content_type="application/pdf", size_bytes=1,
                                sha256="0" * 64, uploaded_by_email="seed@ravasco.com")
        hrs_store = _client(_user("hrs-store@ravasco.com", [Perm.MIR_ENTRY], plants=("hrs",)))
        res = hrs_store.get(f"/api/mir/purchase-orders/{po.id}")
        assert res.status_code == 200
        assert (res.json()["poFiles"], res.json()["canManage"]) == ([], False)
        vapi_store = _client(_user("vapi-store@ravasco.com", [Perm.MIR_ENTRY], plants=("vapi",)))
        assert len(vapi_store.get(f"/api/mir/purchase-orders/{po.id}").json()["poFiles"]) == 1


@pytest.mark.django_db
class TestOwnerAloneManagesAdmins:
    def setup_method(self):
        self.owner = make_user(email=settings.OWNER_EMAIL, role="admin")
        self.other_admin = make_user(email="second-admin@ravasco.com", role="admin")
        self.user = make_user(email="someone@ravasco.com", role="locked")

    def _create(self, actor, role):
        return _client(actor).post("/api/auth/users/create", {
            "email": f"new-{role}@ravasco.com", "fullName": "New", "password": "An0therStr0ngPass!", "role": role,
        }, format="json")

    def test_only_the_owner_creates_an_admin(self):
        assert self._create(self.other_admin, "admin").status_code == 403
        assert self._create(self.owner, "admin").status_code == 201

    def test_another_admin_manages_users_but_not_admins(self):
        other = _client(self.other_admin)
        res = other.patch(f"/api/auth/users/{self.user.user_id}",
                          {"plants": ["hrs"], "permissions": [Perm.MIR_ENTRY, Perm.MIR_ENTRY]}, format="json")
        assert res.status_code == 200
        assert res.json()["permissions"] == [Perm.MIR_ENTRY]
        assert other.patch(f"/api/auth/users/{self.user.user_id}", {"role": "admin"}, format="json").status_code == 403
        assert other.patch(f"/api/auth/users/{self.owner.user_id}", {"fullName": "X"}, format="json").status_code == 403
        assert _client(self.owner).patch(
            f"/api/auth/users/{self.other_admin.user_id}", {"fullName": "Second"}, format="json").status_code == 200

    def test_the_panel_catalogue_names_and_explains_every_permission(self):
        catalog = _client(self.other_admin).get("/api/auth/users").json()["permissionCatalog"]
        assert [p["key"] for p in catalog] == list(ALL_PERMISSIONS)
        assert all(p["label"] and p["hint"] and p["group"] in ("View", "Work") for p in catalog)

    def test_an_unknown_permission_is_refused(self):
        res = _client(self.owner).patch(f"/api/auth/users/{self.user.user_id}", {"permissions": ["fly"]}, format="json")
        assert res.status_code == 400

    def test_a_new_user_starts_locked(self):
        res = self._create(self.other_admin, "user")
        assert res.status_code == 201
        assert (res.json()["permissions"], res.json()["plants"]) == ([], [])


@pytest.mark.django_db
class TestMigrationLocksEveryNonAdmin:
    def test_editors_and_viewers_lose_their_access_and_are_signed_out_admins_keep_theirs(self):
        migration = importlib.import_module("apps.core.migrations.0092_ptuser_layered_permissions")
        editor = PTUser.objects.create(email="old-editor@ravasco.com", password_hash="x", role="editor",
                                       plants=["hrs"], permissions=[Perm.EDIT_FIELDS], token_version=3)
        admin = PTUser.objects.create(email="old-admin@ravasco.com", password_hash="x", role="admin",
                                      plants=["vapi"], token_version=3)

        migration.lock_non_admins(django_apps, None)

        editor.refresh_from_db()
        admin.refresh_from_db()
        assert (editor.role, editor.permissions, editor.plants, editor.token_version) == ("user", [], ["hrs"], 4)
        assert (admin.role, admin.plants, admin.token_version) == ("admin", [], 3)


def test_requires_refuses_an_unknown_permission():
    with pytest.raises(ValueError):
        requires("view_everything")
