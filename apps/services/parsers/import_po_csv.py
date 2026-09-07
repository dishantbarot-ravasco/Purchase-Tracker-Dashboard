"""
apps/services/parsers/import_po_csv.py — parses a plant's "Imports
Purchase Data" master CSV (Master_HRS_SILVASSA_Imports_Purchase_Data.csv /
Master_RTP_Achhad_Imports_Purchase_Data.csv /
Master_RTP_VAPI_Imports_Purchase_Data.csv) into one record per PO, ready
to upsert into <Plant>ImportPurchaseOrder/<Plant>ImportPOLineItem.

Reused as-is for all three plants - confirmed live (2026-09-04) that all
three files share a byte-for-byte identical header, same reasoning po_csv.py
already relies on for the domestic CSVs.

Exact header, in order (verified against the live RTP-Vapi file this
session - no trailing-space quirks here, unlike the domestic CSV's "HSN "/
"Currency "):
PO Drive Folder Name, PO Number, PO Created Date, Vendor Name,
Vendor Address, Vendor GSTIN, Vendor Email, Vendor Code, Billing Address,
ShipTo, Item Id, Material Description, HSN,
QTY (As Per PO), QTY (As Per BOE), UOM, Delivery Date, Payment Terms,
IncoTerms, Currency (As Per PO), Net Price, Net Value,
Total Value (As per PO), Tax Type, Currency (After Taxes), Exchange Rate,
Total Inclusive Value (Final Bill Paid to get shipment from Port), REMARKS,
BOE Number, Bill Of Lading Number, Laden on Board Date, Country of Origin,
License Type, License Number, PO Number | Item Id

**"PO Number | Item Id" moved to the last column (2026-09), matching the
domestic PO CSV's own convention (po_csv.py's EXPECTED_HEADER already has it
last) - the live Drive CSVs for all 3 plants were moved to match. This
column's cell values are never actually read into any field (there's no
`row["PO Number | Item Id"]` access anywhere below, and no model column
backs it) - EXPECTED_HEADER only uses it for the strict header-equality
check (HeaderMismatch), so moving it is purely a position change, not a
data-shape one. If the live Drive CSV and this list ever disagree on
position, every plant's imports sync breaks immediately with HeaderMismatch
- keep them in lockstep.

Reconciliation note: `qty_as_per_boe` (Bill of Entry quantity - what
customs recorded as actually clearing/arriving) is the quantity compared
against MIR downstream, not `qty_as_per_po` (only the originally ordered
amount, which can legitimately differ from what arrives on a partial or
split shipment). Both are still captured here since the parser's job is to
preserve the source data faithfully; the choice of which one to compare
lives in the matching modules, not here.
"""

import csv
import io
from dataclasses import dataclass, field

from apps.services.parsers.common import to_date, to_decimal, to_str

# ── Column layout constant ──────────────────────────────────────────────────

EXPECTED_HEADER = [
    "PO Drive Folder Name", "PO Number", "PO Created Date", "Vendor Name", "Vendor Address",
    "Vendor GSTIN", "Vendor Email", "Vendor Code", "Billing Address", "ShipTo", "Item Id",
    "Material Description", "HSN", "QTY (As Per PO)", "QTY (As Per BOE)",
    "UOM", "Delivery Date", "Payment Terms", "IncoTerms", "Currency (As Per PO)", "Net Price",
    "Net Value", "Total Value (As per PO)", "Tax Type", "Currency (After Taxes)", "Exchange Rate",
    "Total Inclusive Value (Final Bill Paid to get shipment from Port)", "REMARKS", "BOE Number",
    "Bill Of Lading Number", "Laden on Board Date", "Country of Origin", "License Type",
    "License Number", "PO Number | Item Id",
]


# ── Parsed-row shape ─────────────────────────────────────────────────────────

@dataclass
class ParsedImportLineItem:
    item_id: str
    description: str
    hsn: str
    qty_as_per_po: object
    qty_as_per_boe: object
    uom: str
    delivery_date: object
    delivery_date_raw: str
    net_price: object
    net_value: object
    tax_type: str
    currency_after_taxes: str
    exchange_rate: object
    total_inclusive_value: object
    boe_number: str
    bill_of_lading_number: str
    laden_on_board_date: object
    country_of_origin: str
    license_type: str
    license_number: str


@dataclass
class ParsedImportPurchaseOrder:
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
    remarks: str
    items: list = field(default_factory=list)


class HeaderMismatch(Exception):
    """Raised when the CSV's header row doesn't match what this parser was
    built against - better to fail loudly than silently misread columns."""


# ── Public entry point ───────────────────────────────────────────────────────

def parse_import_po_csv(csv_text: str) -> list[ParsedImportPurchaseOrder]:
    """Parses the Imports PO master CSV into one ParsedImportPurchaseOrder
    per distinct PO Number, with its line items grouped underneath. Shared
    verbatim across all three plants (see module docstring) - the caller
    supplies plant-specific model classes when persisting the result, this
    function stays plant-agnostic. Raises HeaderMismatch immediately on a
    header mismatch; rows with a blank PO Number are skipped."""
    reader = csv.DictReader(io.StringIO(csv_text))
    # Trailing blank-named columns (e.g. two stray "" headers past the last
    # real one) are a spreadsheet-export artifact, not a real schema change -
    # confirmed 2026-09-07 on a live RTP-Vapi sync failure: someone had
    # added/removed columns in the source Sheet, leaving 2 headerless trailing
    # columns with no data intent behind them. None of this parser's row
    # access below ever reads a blank-named column, so dropping them before
    # the equality check is safe - a REAL header change (a renamed/reordered/
    # removed real column) still raises HeaderMismatch exactly as before.
    fieldnames = list(reader.fieldnames or [])
    while fieldnames and not (fieldnames[-1] or "").strip():
        fieldnames.pop()
    if reader.fieldnames is None or [h.strip() for h in fieldnames] != [h.strip() for h in EXPECTED_HEADER]:
        raise HeaderMismatch(
            f"CSV header does not match expected schema.\nExpected: {EXPECTED_HEADER}\nGot: {reader.fieldnames}"
        )

    orders_by_po: dict[str, ParsedImportPurchaseOrder] = {}
    order_sequence: list[str] = []

    for row in reader:
        po_number = to_str(row["PO Number"])
        if not po_number:
            continue

        if po_number not in orders_by_po:
            orders_by_po[po_number] = ParsedImportPurchaseOrder(
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
                payment_terms=to_str(row["Payment Terms"]),
                incoterms=to_str(row["IncoTerms"]),
                currency=to_str(row["Currency (As Per PO)"]),
                total_value=to_decimal(row["Total Value (As per PO)"]),
                remarks=to_str(row["REMARKS"]),
            )
            order_sequence.append(po_number)

        delivery_date_str = to_str(row["Delivery Date"])
        parsed_delivery_date = to_date(row["Delivery Date"])

        orders_by_po[po_number].items.append(
            ParsedImportLineItem(
                item_id=to_str(row["Item Id"]),
                description=to_str(row["Material Description"]),
                hsn=to_str(row["HSN"]),
                qty_as_per_po=to_decimal(row["QTY (As Per PO)"]),
                qty_as_per_boe=to_decimal(row["QTY (As Per BOE)"]),  # the one compared to MIR downstream - see module docstring
                uom=to_str(row["UOM"]),
                delivery_date=parsed_delivery_date,
                # Free text like "End Mar/Early Apr 2026" fails to_date() and
                # comes back None - keep the verbatim source string so the
                # frontend can still show it instead of a blank, and so
                # delivery_date_status/F7 can tell "genuinely blank" apart
                # from "present but unparseable".
                delivery_date_raw="" if parsed_delivery_date else delivery_date_str,
                net_price=to_decimal(row["Net Price"]),
                net_value=to_decimal(row["Net Value"]),
                tax_type=to_str(row["Tax Type"]),
                currency_after_taxes=to_str(row["Currency (After Taxes)"]),
                exchange_rate=to_decimal(row["Exchange Rate"]),
                total_inclusive_value=to_decimal(
                    row["Total Inclusive Value (Final Bill Paid to get shipment from Port)"]
                ),
                boe_number=to_str(row["BOE Number"]),
                bill_of_lading_number=to_str(row["Bill Of Lading Number"]),
                laden_on_board_date=to_date(row["Laden on Board Date"]),
                country_of_origin=to_str(row["Country of Origin"]),
                license_type=to_str(row["License Type"]),
                license_number=to_str(row["License Number"]),
            )
        )

    return [orders_by_po[po] for po in order_sequence]
