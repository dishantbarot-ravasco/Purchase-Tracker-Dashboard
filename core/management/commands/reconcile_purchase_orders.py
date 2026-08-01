"""
Runs the real PO-vs-MIR-vs-Stock reconciliation (core/reconciliation.py) for
every plant's domestic POs and writes the results back to Postgres:
PurchaseOrder.status ("Ordered"/"Material Inwarded"/"Received in Inventory"),
POItem.matched, and POFlag rows (source="po_vs_mir") for quantity/rate
discrepancies over 10%.

Reads MIR + RM Stock live off Drive once per plant per run - this is exactly
the kind of Drive call that must NOT happen inside a web request (see
PlantFileStatus's docstring for why), so it only ever runs here, on a
schedule (recommended: every few hours, same cadence as scan_new_pos), not
on every dashboard page load.

Safe to rerun: PurchaseOrder.status and POItem.matched are simply
overwritten with the latest computed values, and old po_vs_mir POFlag rows
for a PO are cleared before its fresh discrepancy notes (if any) are
recreated, so re-running never piles up duplicate or stale flags.
"""
import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from core import mir_stock
from core.models import Plant, POFlag, PurchaseOrder
from core.reconciliation import reconcile_plant_pos

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Reconciles domestic POs against each plant's live MIR and RM Stock files (Ordered/Inwarded/Received in Inventory + discrepancy flags)."

    def handle(self, *args, **options):
        for plant, _ in Plant.choices:
            self.stdout.write(f"Reconciling {plant}...")
            try:
                self._reconcile_one_plant(plant)
            except Exception:
                logger.exception("Reconciliation failed for %s", plant)
                self.stderr.write(self.style.ERROR(f"  Failed for {plant} - see server logs for detail."))

    @transaction.atomic
    def _reconcile_one_plant(self, plant):
        pos = list(
            PurchaseOrder.objects.filter(plant=plant, doc_type="domestic").prefetch_related("items")
        )
        if not pos:
            self.stdout.write(f"  No domestic POs for {plant} - nothing to reconcile.")
            return

        try:
            mir_rows = mir_stock.read_mir_rows(plant)
        except Exception as e:
            self.stderr.write(self.style.WARNING(f"  Could not read {plant}'s MIR file ({e}) - skipping."))
            return

        latest_stock_by_desc = self._latest_stock_by_description(plant)

        discrepancies = reconcile_plant_pos(pos, mir_rows, latest_stock_by_desc)

        item_updates = []
        for po in pos:
            po.save(update_fields=["status"])
            for item in po.items.all():
                item_updates.append(item)
        if item_updates:
            from core.models import POItem
            POItem.objects.bulk_update(item_updates, ["matched"])

        # Clear this run's stale po_vs_mir flags before writing fresh ones,
        # so repeated runs never accumulate duplicates for the same PO.
        POFlag.objects.filter(purchase_order__in=pos, source="po_vs_mir").delete()
        for po in pos:
            notes = discrepancies.get(po.po_number, [])
            for note in notes:
                POFlag.objects.create(purchase_order=po, flag_text=note, source="po_vs_mir")

        matched_pos = sum(1 for po in pos if po.status != "ordered")
        stocked_pos = sum(1 for po in pos if po.status == "received_in_inventory")
        self.stdout.write(
            self.style.SUCCESS(
                f"  {plant}: {len(pos)} PO(s) checked, {matched_pos} inwarded or better, "
                f"{stocked_pos} received in inventory, {sum(len(v) for v in discrepancies.values())} discrepancy flag(s)."
            )
        )

    def _latest_stock_by_description(self, plant):
        """{description.lower(): qty} from the latest StockSnapshot per
        material - mirrors the same "latest snapshot per material" dedupe
        used by the Materials view (core/views.py's plant_materials)."""
        from core.models import StockSnapshot

        latest_by_material = {}
        for snap in StockSnapshot.objects.filter(plant=plant).order_by("material_code", "-snapshot_date"):
            latest_by_material.setdefault(snap.material_code, snap)
        return {
            (snap.description or "").strip().lower(): snap.qty
            for snap in latest_by_material.values()
            if snap.description
        }
