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
        "deliveryDate": item.delivery_date.isoformat() if item.delivery_date else None,
        "matched": item.matched,
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


def serialize_license_item(item):
    return {
        "itemType": item.item_type,
        "materialDescription": item.material_description,
        "sionNorm": item.sion_norm,
        "authorisedQty": _decimal(item.authorised_qty),
        "uom": item.uom,
        "consumedQty": _decimal(item.consumed_qty),
        "remainingQty": _decimal(item.remaining_qty()),
    }


def serialize_license_po_usage(usage):
    return {
        "poNumber": usage.purchase_order.po_number,
        "materialDescription": usage.material_description,
        "qtyUsed": _decimal(usage.qty_used),
        "valueUsed": _decimal(usage.value_used),
    }


def serialize_advance_license(license_obj):
    return {
        "licenseNumber": license_obj.license_number,
        "plant": license_obj.plant,
        "issueDate": license_obj.issue_date.isoformat() if license_obj.issue_date else None,
        "importValidityEnd": license_obj.import_validity_end.isoformat() if license_obj.import_validity_end else None,
        "exportObligationEnd": license_obj.export_obligation_end.isoformat() if license_obj.export_obligation_end else None,
        "extension1End": license_obj.extension_1_end.isoformat() if license_obj.extension_1_end else None,
        "extension2End": license_obj.extension_2_end.isoformat() if license_obj.extension_2_end else None,
        "autoExtensionEnd": license_obj.auto_extension_end.isoformat() if license_obj.auto_extension_end else None,
        "eodcStatus": license_obj.eodc_status,
        "totalAuthorisedCifValue": _decimal(license_obj.total_authorised_cif_value),
        "items": [serialize_license_item(i) for i in license_obj.items.all()],
        "poUsages": [serialize_license_po_usage(u) for u in license_obj.po_usages.select_related("purchase_order").all()],
    }


def serialize_user_access(user_access):
    return {
        "email": user_access.email,
        "role": user_access.role,
        "plants": user_access.plants,
        "isActive": user_access.is_active,
    }
