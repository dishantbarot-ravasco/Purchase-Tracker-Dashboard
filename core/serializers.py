"""
Shape-for-the-API functions, kept separate from views.py so each view stays
focused on "handle the request, enforce access" while the "turn a model
instance into a JSON-friendly dict" concern lives in one place. Field names
here are camelCase (frontend convention), model fields are snake_case
(Python/Django convention) - this module is the one place that translates
between the two.
"""


def _decimal(value):
    """Django DecimalFields aren't JSON-serializable directly - convert to
    float at the boundary, and only at the boundary, so nothing upstream of
    this has to think about it."""
    return float(value) if value is not None else None


def serialize_po_item(item):
    return {
        "description": item.description,
        "itemCode": item.item_code,
        "hsn": item.hsn,
        "qty": _decimal(item.qty),
        "uom": item.uom,
        "netPrice": _decimal(item.net_price),
        "netValue": _decimal(item.net_value),
    }


def serialize_po_flag(flag):
    return {"text": flag.flag_text, "source": flag.source, "resolved": flag.resolved}


def serialize_purchase_order(po, include_items=True, include_flags=True):
    data = {
        "poNumber": po.po_number,
        "docType": po.doc_type,
        "vendorName": po.vendor_name,
        "vendorGstin": po.vendor_gstin,
        "createdDate": po.created_date.isoformat() if po.created_date else None,
        "totalValue": _decimal(po.total_value),
        "taxType": po.tax_type,
        "taxAmount": _decimal(po.tax_amount),
        "totalInclTax": _decimal(po.total_incl_tax),
        "paymentTerms": po.payment_terms,
        "incoterms": po.incoterms,
        "deliveryMode": po.delivery_mode,
        "remarks": po.remarks,
        "status": po.status,
        "extractionConfidence": po.extraction_confidence,
    }
    if include_items:
        data["items"] = [serialize_po_item(it) for it in po.items.all()]
    if include_flags:
        data["flags"] = [serialize_po_flag(f) for f in po.flags.all()]
    return data


def serialize_stock_snapshot(snapshot):
    return {
        "date": snapshot.snapshot_date.isoformat(),
        "materialCode": snapshot.material_code,
        "description": snapshot.description,
        "category": snapshot.category,
        "qty": _decimal(snapshot.qty),
        "rate": _decimal(snapshot.rate),
        "value": _decimal(snapshot.value),
    }


def serialize_dashboard_plant_card(plant, po_count, total_value, mir_row_count, stock_row_count, errors):
    return {
        "plant": plant,
        "poCount": po_count,
        "totalValue": total_value,
        "mirRowCount": mir_row_count,
        "stockRowCount": stock_row_count,
        "errors": errors,
    }


def serialize_user_access(user_access):
    return {
        "email": user_access.email,
        "role": user_access.role,
        "plants": user_access.plants,
        "isActive": user_access.is_active,
    }
