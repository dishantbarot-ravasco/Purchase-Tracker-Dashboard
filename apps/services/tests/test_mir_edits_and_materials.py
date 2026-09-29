"""
The material master (apps/services/materials.py, material_identity.py) and
the limited editing of a posted MIR (mir_service.edit_mir() /
record_rejection()), 2026-09-29.

Editing is narrow on purpose: paperwork and a line's department/remarks for
EDIT_WINDOW_DAYS, the SAP GRN number any time, a later rejection within
REJECTION_WINDOW_DAYS - never a quantity received, a rate, GST, a discount,
the tax type or the invoice total. Every change needs a reason and is kept.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.tests.factories import make_user
from apps.core.models import Material, MaterialCategoryReference, MirChange, MirMismatch
from apps.services import materials, mir_service
from apps.services.material_identity import material_key, material_name
from apps.services.mir_service import MirValidationError
from apps.services.tests.test_mir_service import _line, _ln, _payload, _po


@pytest.fixture
def user(db):
    return make_user(email="store@ravasco.com", role="editor")


def _posted(user, **line_extra):
    line = _line(_po())
    return mir_service.post_mir(_payload([_ln(line, **line_extra)]), user)


def _age(mir, days):
    """Pretend the MIR was entered `days` ago."""
    then = timezone.localdate() - datetime.timedelta(days=days)
    type(mir).objects.filter(pk=mir.pk).update(created_at=timezone.now() - datetime.timedelta(days=days),
                                               mir_date=then, invoice_date=then)
    mir.refresh_from_db()


class TestMaterialIdentity:
    def test_a_fabric_rolls_delivery_details_are_not_its_identity(self):
        a = "EE-160 fabric roll, width 148cm, GSM 590, length 768m, 1 roll, total weight 670.618"
        b = "EE-160 fabric roll, width 148cm, GSM 590, length 512m, 1 roll, total weight 447.078"
        assert material_name(a) == "EE-160 fabric roll, width 148cm, GSM 590"
        assert material_key(a) == material_key(b)
        assert material_key(a) != material_key("EE-160 fabric roll, width 67cm, GSM 590, length 51m, 3 rolls")

    def test_other_descriptions_keep_every_word(self):
        assert material_name("Reclaim Rubber - 6 MPA") == "Reclaim Rubber - 6 MPA"
        assert material_key("Reclaim Rubber - 6 MPA") != material_key("Reclaim Rubber - 7 MPA")
        assert material_name("Zinc oxide, 25 kg bag") == "Zinc oxide, 25 kg bag"


@pytest.mark.django_db
class TestMaterialMaster:
    def test_one_material_per_name_whatever_the_item_code(self):
        a = materials.material_for("Reclaim Rubber - 6 MPA", "22001840")
        b = materials.material_for("Reclaim Rubber - 7 MPA", "22001840")
        assert a != b and a == materials.material_for("RECLAIM RUBBER 6 MPA")

    def test_a_new_material_is_filed_from_the_reference_list(self):
        MaterialCategoryReference.objects.create(description="Zinc Oxide", normalized_description="zinc oxide",
                                                 category="Rubber Chemicals & Additives", subcategory="Activator")
        m = materials.material_for("ZINC OXIDE")
        assert (m.category, m.subcategory) == ("Rubber Chemicals & Additives", "Activator")
        assert materials.material_for("Unknown thing").category == ""

    def test_an_sap_item_code_files_it_only_when_one_reference_row_has_it(self):
        MaterialCategoryReference.objects.create(description="Reclaim 6MPA grade", normalized_description="reclaim 6mpa grade",
                                                 category="Reclaimed / Crumb Rubber", sap_item_code="22001840")
        MaterialCategoryReference.objects.create(description="Oil A", normalized_description="oil a", category="Oils", sap_item_code="9")
        MaterialCategoryReference.objects.create(description="Oil B", normalized_description="oil b", category="Solvent", sap_item_code="9")
        assert materials.material_for("Reclaim Rubber - 6 MPA", "22001840").category == "Reclaimed / Crumb Rubber"
        assert materials.material_for("Some oil", "9").category == ""

    def test_set_category_files_once_and_never_overwrites(self, user):
        m = materials.material_for("Unknown thing")
        materials.set_category(m, "Carbon Black", "N330", user)
        materials.set_category(m, "Silica", "", user)
        m.refresh_from_db()
        assert (m.category, m.subcategory, m.category_set_by_email) == ("Carbon Black", "N330", user.email)

    def test_the_reference_list_wins_on_reload(self, user):
        m = materials.material_for("Unknown thing")
        materials.set_category(m, "Carbon Black", "", user)
        MaterialCategoryReference.objects.create(description="Unknown thing", normalized_description="unknown thing",
                                                 category="Silica", subcategory="Precipitated")
        assert materials.sync_from_reference() == 1
        m.refresh_from_db()
        assert (m.category, m.subcategory, m.category_set_by_email) == ("Silica", "Precipitated", "")

    def test_po_lines_are_linked_by_the_projection(self, tmp_path):
        from apps.services.tests.test_procurement_po_sync import _lines, _row, _sync
        _sync(tmp_path, [_row("1", "EE-160 fabric roll, width 148cm, GSM 590, length 768m, 1 roll", 10, 1),
                         _row("2", "EE-160 fabric roll, width 148cm, GSM 590, length 512m, 1 roll", 10, 1)])
        first, second = _lines()
        assert first.material == second.material
        assert first.material.name == "EE-160 fabric roll, width 148cm, GSM 590"
        assert Material.objects.count() == 1


@pytest.mark.django_db
class TestEditMir:
    def test_paperwork_is_edited_and_every_change_kept(self, user):
        mir = _posted(user)
        mir_service.edit_mir(mir, user, {"vehicle_no": "DN09 AB 1234", "invoice_no": "INV-042B"},
                             {1: {"dept_use": "Mixing"}}, "Typed from the wrong challan")
        mir.refresh_from_db()
        assert (mir.vehicle_no, mir.invoice_no, mir.invoice_key) == ("DN09 AB 1234", "INV-042B", "INV-42B")
        assert mir.lines.get().dept_use == "Mixing"
        changes = {(c.field, c.old_value, c.new_value) for c in MirChange.objects.filter(mir=mir)}
        assert changes == {("vehicle_no", "", "DN09 AB 1234"), ("invoice_no", "INV-42", "INV-042B"), ("dept_use", "", "Mixing")}
        assert all(c.reason == "Typed from the wrong challan" and c.changed_by_email == user.email for c in MirChange.objects.all())

    @pytest.mark.parametrize("field", ["invoice_total", "tax_type", "tcs_amount", "mir_date", "plant"])
    def test_figures_are_never_edited(self, user, field):
        mir = _posted(user)
        with pytest.raises(MirValidationError) as exc:
            mir_service.edit_mir(mir, user, {field: "1"}, {}, "Mistake")
        assert "Cancel the MIR" in exc.value.errors[0]["message"]

    @pytest.mark.parametrize("field", ["qty_received", "rate", "gst_rate", "discount"])
    def test_line_figures_are_never_edited(self, user, field):
        mir = _posted(user)
        with pytest.raises(MirValidationError):
            mir_service.edit_mir(mir, user, {}, {1: {field: "1"}}, "Mistake")

    def test_a_reason_is_required_and_an_empty_edit_refused(self, user):
        mir = _posted(user)
        with pytest.raises(MirValidationError):
            mir_service.edit_mir(mir, user, {"vehicle_no": "X"}, {}, " ")
        with pytest.raises(MirValidationError):
            mir_service.edit_mir(mir, user, {"vehicle_no": ""}, {}, "No change")

    def test_the_invoice_date_stays_on_or_before_the_mir_date(self, user):
        mir = _posted(user)
        with pytest.raises(MirValidationError):
            mir_service.edit_mir(mir, user, {"invoice_date": (mir.mir_date + datetime.timedelta(days=1)).isoformat()}, {}, "x")

    def test_after_the_window_only_the_grn_can_be_filled(self, user):
        mir = _posted(user)
        _age(mir, mir_service.EDIT_WINDOW_DAYS + 1)
        with pytest.raises(MirValidationError) as exc:
            mir_service.edit_mir(mir, user, {"vehicle_no": "X"}, {}, "Late fix")
        assert "edit window closed" in exc.value.errors[0]["message"]
        mir_service.edit_mir(mir, user, {"sap_grn_number": "5000123456"}, {}, "Booked in SAP")
        mir.refresh_from_db()
        assert mir.sap_grn_number == "5000123456"

    def test_a_cancelled_mir_is_not_edited(self, user):
        mir = _posted(user)
        mir_service.cancel_mir(mir, user, "Entered twice")
        with pytest.raises(MirValidationError):
            mir_service.edit_mir(mir, user, {"sap_grn_number": "1"}, {}, "x")


@pytest.mark.django_db
class TestLateRejection:
    def test_it_lowers_the_accepted_quantity_and_opens_a_mismatch(self, user):
        mir = _posted(user)
        line = mir.lines.get()
        assert mir_service.accepted_by_line([line.po_line_id])[line.po_line_id] == Decimal("100")
        mir_service.record_rejection(line, user, "8", "REJ_QUALITY", "Lab report 12")
        line.refresh_from_db()
        assert line.qty_rejected == Decimal("8")
        assert mir_service.accepted_by_line([line.po_line_id])[line.po_line_id] == Decimal("92")
        mm = MirMismatch.objects.get(mir_line=line, kind="QTY_REJECTED")
        assert (mm.actual, mm.status, mm.reason.code, mm.note) == (Decimal("8"), "OPEN", "REJ_QUALITY", "Lab report 12")
        assert MirChange.objects.get(mir_line=line).new_value == "8"
        # The invoice was billed in full: amounts do not move.
        assert line.line_total == mir.lines.get().line_total

    def test_a_second_rejection_reopens_the_same_mismatch(self, user):
        mir = _posted(user)
        line = mir.lines.get()
        mir_service.record_rejection(line, user, "5", "REJ_QUALITY", "")
        mm = MirMismatch.objects.get(mir_line=line, kind="QTY_REJECTED")
        mir_service.resolve_mismatch(mm, user, "Debit note 17")
        mir_service.record_rejection(line, user, "9", "REJ_DAMAGED", "")
        mm.refresh_from_db()
        assert (mm.actual, mm.status, mm.resolution_note) == (Decimal("9"), "OPEN", "")
        assert MirMismatch.objects.filter(mir_line=line, kind="QTY_REJECTED").count() == 1

    @pytest.mark.parametrize("qty", ["0", "101"])
    def test_it_only_goes_up_and_never_past_what_was_received(self, user, qty):
        mir = _posted(user)
        with pytest.raises(MirValidationError):
            mir_service.record_rejection(mir.lines.get(), user, qty, "REJ_QUALITY", "")

    def test_it_needs_a_rejection_reason(self, user):
        mir = _posted(user)
        with pytest.raises(MirValidationError):
            mir_service.record_rejection(mir.lines.get(), user, "5", "PARTIAL_BALANCE_DUE", "")

    def test_it_closes_after_the_rejection_window(self, user):
        mir = _posted(user)
        _age(mir, mir_service.REJECTION_WINDOW_DAYS + 1)
        with pytest.raises(MirValidationError) as exc:
            mir_service.record_rejection(mir.lines.get(), user, "5", "REJ_QUALITY", "")
        assert "Rejections can be recorded up to" in exc.value.errors[0]["message"]


@pytest.mark.django_db
class TestEditApi:
    def _client(self, **kw):
        client = APIClient()
        client.force_authenticate(user=make_user(**kw))
        return client

    def test_edit_and_reject_are_editor_only_and_scoped_to_the_receiving_plant(self, user):
        mir = _posted(user)
        body = {"header": {"vehicle_no": "X"}, "reason": "fix"}
        assert self._client(email="v@ravasco.com", role="viewer").post(f"/api/mir/entries/{mir.id}/edit", body, format="json").status_code == 403
        assert self._client(email="a@ravasco.com", role="editor", plants=["achhad"]).post(
            f"/api/mir/entries/{mir.id}/edit", body, format="json").status_code == 403
        res = self._client(email="h@ravasco.com", role="editor", plants=["hrs"]).post(f"/api/mir/entries/{mir.id}/edit", body, format="json")
        assert res.status_code == 200 and res.json()["vehicleNo"] == "X" and res.json()["history"][0]["field"] == "vehicle_no"
        res = self._client(email="h2@ravasco.com", role="editor", plants=["hrs"]).post(
            f"/api/mir/entries/{mir.id}/lines/1/reject", {"qtyRejected": "3", "reason": "REJ_QUALITY"}, format="json")
        assert res.status_code == 200 and res.json()["lines"][0]["qtyRejected"] == "3.000"

    def test_the_detail_says_what_can_still_be_edited(self, user):
        mir = _posted(user)
        data = self._client(email="h@ravasco.com", role="editor").get(f"/api/mir/entries/{mir.id}").json()
        assert data["canEdit"] and data["canEditGrn"] and data["canReject"]
        assert data["editUntil"] == (timezone.localdate() + datetime.timedelta(days=mir_service.EDIT_WINDOW_DAYS)).isoformat()
