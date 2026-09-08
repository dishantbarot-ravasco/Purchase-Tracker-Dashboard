"""
Tests for apps/services/plant_mismatch_report.py - the per-plant Data
Correction email sent to each plant head (not the internal admin list, but
CC'ing every active admin - see that module's own docstring). HRS only -
matches this codebase's convention of testing one plant's copy directly for
logic shared byte-for-byte across all three (see test_matching.py's own
module docstring for the same reasoning applied to the matching engines).
"""
import datetime

import pytest

from apps.api.tests.factories import make_user
from apps.core.models import (
    HRSDomesticPOLineItem,
    HRSDomesticPurchaseOrder,
    HRSMIREntry,
    HRSMirStockMatch,
    HRSPOMirMatch,
    HRSRMLot,
)
from apps.services.plant_mismatch_report import build_plant_mismatch_report, send_plant_mismatch_reports

TODAY = datetime.date.today()


def _make_po_mir_match(po_number="PO-1", qty_mismatched=False, rate_mismatched=False, dismissed=False):
    po = HRSDomesticPurchaseOrder.objects.create(po_drive_folder_name=po_number, po_number=po_number, vendor_name="Vendor A")
    item = HRSDomesticPOLineItem.objects.create(purchase_order=po, item_id="1", description="Widget")
    mir = HRSMIREntry.objects.create(mir_no=f"MIR-{po_number}", party_name="Vendor A", material_description="Widget", source_row_ref=po_number)
    return HRSPOMirMatch.objects.create(
        po_line_item=item, mir_entry=mir, tier="weighted", match_score="0.9",
        qty_mismatched=qty_mismatched, qty_diff_pct="12.00" if qty_mismatched else None,
        rate_mismatched=rate_mismatched, rate_diff_pct="8.00" if rate_mismatched else None,
        is_flagged=qty_mismatched or rate_mismatched, dismissed_by_override=dismissed,
    )


def _make_mir_stock_match(material="Gadget", qty_mismatched=False, rate_mismatched=False, dismissed=False):
    mir = HRSMIREntry.objects.create(mir_no=f"MIR-{material}", party_name="Vendor B", material_description=material, source_row_ref=material)
    lot = HRSRMLot.objects.create(description=material, party_name="Vendor B", source_row_ref=material)
    return HRSMirStockMatch.objects.create(
        mir_entry=mir, stock_lot=lot,
        qty_mismatched=qty_mismatched, qty_diff_pct="15.00" if qty_mismatched else None,
        rate_mismatched=rate_mismatched, rate_diff_pct="9.00" if rate_mismatched else None,
        is_flagged=rate_mismatched, dismissed_by_override=dismissed,
    )


@pytest.mark.django_db
class TestBuildPlantMismatchReportHRS:
    def test_po_mir_row_shows_only_the_applicable_diff(self):
        """"whichever applicable" (project owner's own phrasing) - a row
        that's only rate_mismatched must not show a stale/zero qty figure."""
        _make_po_mir_match(po_number="PO-1", rate_mismatched=True)

        report = build_plant_mismatch_report("hrs")

        [row] = report["poMismatches"]
        assert row["poNumber"] == "PO-1"
        assert row["qtyDiffPct"] is None
        assert row["rateDiffPct"] == 8.0

    def test_dismissed_matches_are_excluded(self):
        _make_po_mir_match(po_number="PO-2", qty_mismatched=True, dismissed=True)
        _make_mir_stock_match(material="Widget", rate_mismatched=True, dismissed=True)

        report = build_plant_mismatch_report("hrs")

        assert report["poMismatches"] == []
        assert report["stockMismatches"] == []

    def test_non_mismatched_matches_are_excluded(self):
        _make_po_mir_match(po_number="PO-3")  # neither qty nor rate mismatched
        _make_mir_stock_match(material="Bolt")

        report = build_plant_mismatch_report("hrs")

        assert report["poMismatches"] == []
        assert report["stockMismatches"] == []

    def test_stock_mismatch_row_shape(self):
        _make_mir_stock_match(material="Zinc Oxide", qty_mismatched=True, rate_mismatched=True)

        report = build_plant_mismatch_report("hrs")

        [row] = report["stockMismatches"]
        assert row["material"] == "Zinc Oxide"
        assert row["qtyDiffPct"] == 15.0
        assert row["rateDiffPct"] == 9.0

    def test_email_maps_to_the_correct_plant_head(self):
        report = build_plant_mismatch_report("hrs")
        assert report["email"] == "avijit.ghosh@ravasco.com"


@pytest.mark.django_db
class TestSendPlantMismatchReports:
    def test_plant_with_nothing_flagged_is_skipped_entirely(self, mailoutbox):
        result = send_plant_mismatch_reports()
        assert result["plants_sent"] == 0
        assert result["plants_skipped_no_mismatches"] == 3
        assert len(mailoutbox) == 0

    def test_sends_to_the_plant_head_cc_ing_every_active_admin(self, mailoutbox):
        make_user(email="admin1@ravasco.com", role="admin")
        make_user(email="admin2@ravasco.com", role="admin")
        _make_po_mir_match(po_number="PO-9", qty_mismatched=True)

        result = send_plant_mismatch_reports()

        assert result["plants_sent"] == 1  # only HRS has anything flagged
        assert result["plants_skipped_no_mismatches"] == 2
        [mail] = mailoutbox
        assert mail.to == ["avijit.ghosh@ravasco.com"]
        assert set(mail.cc) == {"admin1@ravasco.com", "admin2@ravasco.com"}

    def test_no_admins_still_sends_to_the_plant_head_with_no_cc(self, mailoutbox):
        _make_po_mir_match(po_number="PO-10", rate_mismatched=True)

        send_plant_mismatch_reports()

        [mail] = mailoutbox
        assert mail.to == ["avijit.ghosh@ravasco.com"]
        assert mail.cc == []

    def test_recipient_override_redirects_all_plants_with_no_admin_cc(self, mailoutbox):
        """test_recipient (project owner, 2026-09-08: "fire all the mails to
        me only for now this is testing") must never leak a real plant
        head's or admin's address into the test send."""
        make_user(email="admin3@ravasco.com", role="admin")
        _make_po_mir_match(po_number="PO-20", qty_mismatched=True)
        _make_mir_stock_match(material="Test Material", rate_mismatched=True)

        result = send_plant_mismatch_reports(test_recipient="dishant.barot@ravasco.com")

        assert result["plants_sent"] == 1  # only HRS has anything flagged
        [mail] = mailoutbox
        assert mail.to == ["dishant.barot@ravasco.com"]
        assert mail.cc == []
        assert mail.subject.startswith("[TEST]")

    def test_email_body_has_no_system_generated_footer(self, mailoutbox):
        """Explicit project owner instruction, 2026-09-08: "remove that
        system footer from this emails" - unlike every other email in this
        app (see email_service.py's render_email(), always appended)."""
        _make_po_mir_match(po_number="PO-11", qty_mismatched=True)

        send_plant_mismatch_reports()

        [mail] = mailoutbox
        assert "system generated" not in mail.body.lower()
        assert "do not reply" not in mail.body.lower()
        assert "system generated" not in mail.alternatives[0][0].lower()

    def test_email_body_lists_po_number_and_applicable_mismatch(self, mailoutbox):
        _make_po_mir_match(po_number="PO-12", qty_mismatched=True)
        _make_mir_stock_match(material="Sulphur Powder", rate_mismatched=True)

        send_plant_mismatch_reports()

        [mail] = mailoutbox
        assert "PO-12" in mail.body
        assert "Sulphur Powder" in mail.body
        assert "12.00%" in mail.body  # the PO row's qty mismatch
        assert "9.00%" in mail.body  # the stock row's rate mismatch
