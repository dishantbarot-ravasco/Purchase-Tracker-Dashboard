"""Seeds the three plants and the MIR mismatch reasons (2026-09-28).

Plant GST state codes are read from the PO sheets, not assumed: vendors whose
GSTIN shares the plant's state are the ones billed CGST+SGST/UGST there -
HRS 26 (Dadra & Nagar Haveli, a union territory, hence UGST), Achhad 27
(Maharashtra), Vapi 24 (Gujarat).

Idempotent (update_or_create on the natural key), so a re-run changes
labels without duplicating rows."""

from django.db import migrations

PLANTS = [
    # code, name, state_code, union territory, MIR prefix
    ("hrs", "HRS - Hindustan Rubbers, Silvassa", "26", True, "HRS"),
    ("achhad", "RTP - Achhad", "27", False, "ACH"),
    ("vapi", "RTP - Vapi", "24", False, "VAPI"),
]

REASONS = [
    # code, kind, label, closes_line, note_required
    # The one list of MIR reasons. Migration 0076 re-runs seed() so rows added
    # here reach a database that already applied this migration; the root
    # conftest.py re-seeds from here for tests. Reasons are never deleted: a
    # posted mismatch points at its reason with PROTECT.
    ("PARTIAL_BALANCE_DUE", "QTY_SHORT", "Partial delivery - balance to come", False, False),
    ("QC_REJECTED", "QTY_SHORT", "Rejected at quality check - replacement due", False, False),
    ("VENDOR_SHORT_CLOSE", "QTY_SHORT", "Vendor short-supplied - close the line", True, False),
    ("WEIGHBRIDGE_SHORT_CLOSE", "QTY_SHORT", "Weighbridge variance on weighed material - close the line", True, False),
    ("TRANSIT_LOSS", "QTY_SHORT", "Transit loss or damage - close the line", True, True),
    ("BALANCE_NOT_NEEDED", "QTY_SHORT", "Balance no longer needed - close the line", True, True),
    ("SHORT_OTHER", "QTY_SHORT", "Other (explain in the note)", False, True),
    ("WEIGHBRIDGE_VARIANCE", "QTY_OVER", "Weighbridge variance on weighed material", False, False),
    ("PACK_SIZE_ROUNDING", "QTY_OVER", "Rounded up to the bag, drum or roll size", False, False),
    ("EXCESS_ACCEPTED", "QTY_OVER", "Vendor sent extra - accepted", False, False),
    ("EXCESS_TO_RETURN", "QTY_OVER", "Vendor sent extra - to be returned", False, False),
    ("PO_QTY_AMENDMENT_PENDING", "QTY_OVER", "PO quantity amendment pending", False, False),
    ("OVER_OTHER", "QTY_OVER", "Other (explain in the note)", False, True),
    ("REJ_QUALITY", "REJECTION", "Failed the quality check or test report", False, False),
    ("REJ_DAMAGED", "REJECTION", "Damaged, wet or leaking on arrival", False, False),
    ("REJ_WRONG_MATERIAL", "REJECTION", "Wrong material, grade or specification", False, False),
    ("REJ_EXPIRED", "REJECTION", "Expired or too little shelf life left", False, False),
    ("REJ_PACKING", "REJECTION", "Packing or labelling not as ordered", False, False),
    ("REJ_OTHER", "REJECTION", "Other (explain in the note)", False, True),
    ("PO_RATE_OUTDATED", "RATE", "PO rate out of date - amendment pending", False, False),
    ("VENDOR_BILLING_ERROR", "RATE", "Vendor billed the wrong rate - debit/credit note", False, False),
    ("PRICE_ESCALATION", "RATE", "Price escalation agreed", False, True),
    ("DISCOUNT_NOT_APPLIED", "RATE", "Agreed discount not applied on the invoice", False, False),
    ("RATE_INCLUDES_FREIGHT", "RATE", "Rate includes freight or packing", False, False),
    ("RATE_OTHER_UNIT", "RATE", "Invoice rate is per another unit - converted", False, True),
    ("FREE_REPLACEMENT", "RATE", "Free or replacement material", False, False),
    ("RATE_OTHER", "RATE", "Other (explain in the note)", False, True),
    ("GST_HSN_DIFFERENT", "GST_RATE", "Vendor used another HSN code / GST slab", False, False),
    ("GST_RATE_CHANGED", "GST_RATE", "GST rate changed after the PO was raised", False, False),
    ("GST_VENDOR_ERROR", "GST_RATE", "Vendor charged the wrong GST - debit/credit note", False, False),
    ("GST_OTHER", "GST_RATE", "Other (explain in the note)", False, True),
    ("VERBAL_ORDER_PO_LATER", "INVOICE_DATE", "Verbal order - the PO was raised after the invoice", False, False),
    ("ADVANCE_BILLING", "INVOICE_DATE", "Vendor billed in advance of the PO", False, False),
    ("PO_DATE_WRONG", "INVOICE_DATE", "The PO date on the sheet looks wrong - purchase to check", False, False),
    ("INVOICE_DATE_OTHER", "INVOICE_DATE", "Other (explain in the note)", False, True),
    ("INVOICE_EXTRA_CHARGES", "INVOICE_TOTAL", "Freight, packing or other charges on the invoice", False, True),
    ("INVOICE_DISCOUNT", "INVOICE_TOTAL", "Discount given on the invoice total", False, False),
    ("INVOICE_ROUND_OFF", "INVOICE_TOTAL", "Round-off on the invoice is more than Re 1", False, False),
    ("INVOICE_ARITHMETIC_ERROR", "INVOICE_TOTAL", "Arithmetic error on the invoice", False, False),
    ("INVOICE_OTHER", "INVOICE_TOTAL", "Other (explain in the note)", False, True),
    ("VENDOR_WRONG_TAX", "TAX_TYPE", "Vendor charged the wrong tax type", False, False),
    ("PLACE_OF_SUPPLY_OTHER_STATE", "TAX_TYPE", "Place of supply is another state", False, True),
    ("VENDOR_OTHER_STATE_BRANCH", "TAX_TYPE", "Supplied from the vendor's branch in another state", False, False),
    ("BILL_SHIP_DIFFERENT_STATE", "TAX_TYPE", "Bill-to and ship-to are in different states", False, False),
    ("TAX_OTHER", "TAX_TYPE", "Other (explain in the note)", False, True),
]


def seed(apps, schema_editor):
    Plant = apps.get_model("core", "Plant")
    Reason = apps.get_model("core", "MirReasonCode")
    for code, name, state, ut, prefix in PLANTS:
        Plant.objects.update_or_create(
            code=code, defaults={"name": name, "state_code": state, "is_union_territory": ut, "mir_prefix": prefix},
        )
    for order, (code, kind, label, closes, note) in enumerate(REASONS):
        Reason.objects.update_or_create(
            code=code,
            defaults={"kind": kind, "label": label, "closes_line": closes, "note_required": note, "sort_order": order},
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0067_procurement_mir")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
