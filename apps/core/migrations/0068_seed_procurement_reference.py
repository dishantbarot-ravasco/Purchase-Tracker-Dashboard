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
    ("PARTIAL_BALANCE_DUE", "QTY_SHORT", "Partial delivery - balance to come", False, False),
    ("QC_REJECTED", "QTY_SHORT", "Rejected at quality check - replacement due", False, False),
    ("VENDOR_SHORT_CLOSE", "QTY_SHORT", "Vendor short-supplied - close the line", True, False),
    ("TRANSIT_LOSS", "QTY_SHORT", "Transit loss or damage - close the line", True, True),
    ("SHORT_OTHER", "QTY_SHORT", "Other (explain in the note)", False, True),
    ("WEIGHBRIDGE_VARIANCE", "QTY_OVER", "Weighbridge variance on weighed material", False, False),
    ("EXCESS_ACCEPTED", "QTY_OVER", "Vendor sent extra - accepted", False, False),
    ("EXCESS_TO_RETURN", "QTY_OVER", "Vendor sent extra - to be returned", False, False),
    ("PO_QTY_AMENDMENT_PENDING", "QTY_OVER", "PO quantity amendment pending", False, False),
    ("OVER_OTHER", "QTY_OVER", "Other (explain in the note)", False, True),
    ("PO_RATE_OUTDATED", "RATE", "PO rate out of date - amendment pending", False, False),
    ("VENDOR_BILLING_ERROR", "RATE", "Vendor billed the wrong rate - debit/credit note", False, False),
    ("PRICE_ESCALATION", "RATE", "Price escalation agreed", False, True),
    ("DISCOUNT_NOT_APPLIED", "RATE", "Agreed discount not applied on the invoice", False, False),
    ("FREE_REPLACEMENT", "RATE", "Free or replacement material", False, False),
    ("RATE_OTHER", "RATE", "Other (explain in the note)", False, True),
    ("INVOICE_EXTRA_CHARGES", "INVOICE_TOTAL", "Invoice carries charges not entered on the lines", False, True),
    ("INVOICE_ARITHMETIC_ERROR", "INVOICE_TOTAL", "Arithmetic error on the invoice", False, False),
    ("INVOICE_OTHER", "INVOICE_TOTAL", "Other (explain in the note)", False, True),
    ("VENDOR_WRONG_TAX", "TAX_TYPE", "Vendor charged the wrong tax type", False, False),
    ("PLACE_OF_SUPPLY_OTHER_STATE", "TAX_TYPE", "Place of supply is another state", False, True),
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
