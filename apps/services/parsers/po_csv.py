"""
apps/services/parsers/po_csv.py — parses a plant's domestic PO master CSV
(Master_HRS_SILVASSA_Domestic_Purchase_Data.csv / the Achhad/Vapi
equivalents) into one record per PO (grouping the one-row-per-line-item
CSV), ready to upsert into HRSDomesticPurchaseOrder/HRSDomesticPOLineItem or the
per-plant model equivalent.

Reused as-is for all three plants - confirmed live that HRS's, Achhad's,
and Vapi's domestic PO CSVs share a byte-for-byte identical header, unlike
MIR/Stock which genuinely differ per plant. Only the target model class and
the Drive file title differ per plant's own sync_*_po_csv command; this
module itself never branches on plant.

Exact header, in order (verified against the live file this session):
PO Drive Folder Name, PO Number, PO Created Date, Vendor Name,
Vendor Address, Vendor GSTIN, Vendor Email, Vendor Code, Billing Address,
ShipTo, Item Id, Material Description, HSN , QTY, UOM, Delivery Date ,
Payment Terms , IncoTerms, Currency , Net Price, Net Value, Total Value,
Tax Type, Total Inclusive Value, Remarks, PO Number | Item Id
"""

import csv
import io
from dataclasses import dataclass, field

from apps.services.parsers.common import to_date, to_decimal, to_str

# ── Column layout constant ──────────────────────────────────────────────────

EXPECTED_HEADER = [
    "PO Drive Folder Name", "PO Number", "PO Created Date", "Vendor Name", "Vendor Address",
    "Vendor GSTIN", "Vendor Email", "Vendor Code", "Billing Address", "ShipTo", "Item Id",
    "Material Description", "HSN ", "QTY", "UOM", "Delivery Date ", "Payment Terms ", "IncoTerms",
    "Currency ", "Net Price", "Net Value", "Total Value", "Tax Type", "Total Inclusive Value",
    "Remarks", "PO Number | Item Id",
]


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedLineItem:
    item_id: str
    description: str
    hsn: str
    qty: object
    uom: str
    delivery_date: object
    net_price: object
    net_value: object


@dataclass
class ParsedPurchaseOrder:
    po_drive_folder_name: str
    po_number: str
    po_created_date: object
    vendor_name: str
    vendor_address: str
    vendor_gstin: str
    vendor_email: str
    vendor_code: str
    billing_address: str
    ship_to: str
    payment_terms: str
    incoterms: str
    currency: str
    total_value: object
    tax_type: str
    total_inclusive_value: object
    remarks: str
    is_old_format_template: bool
    items: list = field(default_factory=list)


class HeaderMismatch(Exception):
    """Raised when the CSV's header row doesn't match what this parser was
    built against - better to fail loudly than silently misread columns."""


# ── Row-level helpers ────────────────────────────────────────────────────────

def _is_old_format(po_number: str, folder_name: str) -> bool:
    """Flags a PO as coming from the legacy PO template rather than the
    current one, purely from shape of the PO number itself (a slash, or an
    'HRS'/'HO' marker) - there is no explicit template-version field in the
    source data to key off of instead. `folder_name` is accepted for a
    future refinement but unused today; heuristic, not authoritative."""
    return "/" in po_number or "HRS" in po_number.upper() or "HO" in po_number.upper()


# ── Public entry point ───────────────────────────────────────────────────────

def parse_po_csv(csv_text: str) -> list[ParsedPurchaseOrder]:
    """Parses the domestic PO master CSV into one ParsedPurchaseOrder per
    distinct PO Number, with its line items grouped underneath. Raises
    HeaderMismatch immediately if the header row doesn't match exactly what
    this parser was built against, rather than silently misreading columns.
    Rows with a blank PO Number are skipped (not a real order line)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or [h.strip() for h in reader.fieldnames] != [h.strip() for h in EXPECTED_HEADER]:
        raise HeaderMismatch(
            f"CSV header does not match expected schema.\nExpected: {EXPECTED_HEADER}\nGot: {reader.fieldnames}"
        )

    orders_by_po: dict[str, ParsedPurchaseOrder] = {}
    order_sequence: list[str] = []

    for row in reader:
        po_number = to_str(row["PO Number"])
        if not po_number:
            continue

        if po_number not in orders_by_po:
            orders_by_po[po_number] = ParsedPurchaseOrder(
                po_drive_folder_name=to_str(row["PO Drive Folder Name"]),
                po_number=po_number,
                po_created_date=to_date(row["PO Created Date"]),
                vendor_name=to_str(row["Vendor Name"]),
                vendor_address=to_str(row["Vendor Address"]),
                vendor_gstin=to_str(row["Vendor GSTIN"]),
                vendor_email=to_str(row["Vendor Email"]),
                vendor_code=to_str(row["Vendor Code"]),
                billing_address=to_str(row["Billing Address"]),
                ship_to=to_str(row["ShipTo"]),
                payment_terms=to_str(row["Payment Terms "]),
                incoterms=to_str(row["IncoTerms"]),
                currency=to_str(row["Currency "]) or "INR",
                total_value=to_decimal(row["Total Value"]),
                tax_type=to_str(row["Tax Type"]),
                total_inclusive_value=to_decimal(row["Total Inclusive Value"]),
                remarks=to_str(row["Remarks"]),
                is_old_format_template=_is_old_format(po_number, to_str(row["PO Drive Folder Name"])),
            )
            order_sequence.append(po_number)

        orders_by_po[po_number].items.append(
            ParsedLineItem(
                item_id=to_str(row["Item Id"]),
                description=to_str(row["Material Description"]),
                hsn=to_str(row["HSN "]),
                qty=to_decimal(row["QTY"]),
                uom=to_str(row["UOM"]),
                delivery_date=to_date(row["Delivery Date "]),
                net_price=to_decimal(row["Net Price"]),
                net_value=to_decimal(row["Net Value"]),
            )
        )

    return [orders_by_po[po] for po in order_sequence]
