"""
Live readers for each plant's MIR and RM Stock xlsx files on Drive.

Deliberately NOT stored in Postgres as their own tables (per Dishant: MIR is
only ever used to cross-check PO data and surface discrepancies, it isn't a
system of record we need to keep historically - RM Stock is the one that
needs daily history, via StockSnapshot, since it has no date column at all).

Column layouts still differ per plant until the standardized MIR/RM Stock
templates are rolled out everywhere, so each plant gets a small adapter here
mapping ITS raw column names to one common shape. When a plant migrates to
the standard template, only its adapter needs updating - the reconciliation
logic that consumes normalize_mir_rows()/normalize_stock_rows() never has to
change.
"""
import io

from openpyxl import load_workbook

from . import drive
from .plant_config import MIR_FILE_TITLES, MIR_RM_SHEET_NAMES, STOCK_FILE_TITLES


def _find_folder_file(plant, titles_map):
    """Lists ALL of the plant's Drive root children and matches the exact
    file name in Python - previously this narrowed the search server-side
    with `name contains '<title prefix>'`, which turned out to be the actual
    cause of every "MIR/RM Stock file isn't available" error so far, not a
    real Drive access problem. Drive's `contains` operator for the name
    field does word/token-boundary matching rather than a true substring
    match, so a truncated 20-character prefix that happened to cut off
    mid-word could fail to match a file that was right there the whole time
    (confirmed directly: an unfiltered list_children found every expected
    file by its exact name on the first try). See the identical fix and
    fuller explanation in drive.py's find_file_by_title."""
    root_id = _plant_root_id(plant)
    target_title = titles_map[plant]
    children = drive.list_children(root_id)
    for f in children:
        if f["name"] == target_title:
            return f
    return None


def _plant_root_id(plant):
    from django.conf import settings
    return settings.DRIVE_PLANT_ROOTS[plant]


def _load_workbook_for(plant, titles_map):
    file_meta = _find_folder_file(plant, titles_map)
    if not file_meta:
        raise FileNotFoundError(f"Could not find the expected file for {plant} in its Drive folder.")
    raw_bytes = drive.download_file_bytes(file_meta["id"])
    # read_only=True matters here, not just for memory: these files have
    # heavily merged header cells, and openpyxl's normal (non-read_only)
    # reader builds full Border/Style objects for every merged range while
    # parsing, which has a known bug where certain border combinations
    # trigger runaway recursive __eq__/__ne__ comparisons (openpyxl
    # deduplicating style objects) - this can spin a worker until it's
    # killed rather than raising a normal Python exception. read_only mode
    # streams raw cell values without building those style objects at all,
    # which is also all this module actually needs (values, not styling).
    return load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True), file_meta


def _first_matching_sheet(wb, candidate_names):
    for name in candidate_names:
        if name in wb.sheetnames:
            return wb[name]
    return wb[wb.sheetnames[0]]  # best-effort fallback


def count_data_rows(ws, header_row=1):
    """Best-effort count of non-empty rows below the header - used for the
    quick dashboard stat cards, not the full reconciliation."""
    count = 0
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if any(cell not in (None, "") for cell in row):
            count += 1
    return count


def get_mir_rm_row_count(plant):
    wb, _ = _load_workbook_for(plant, MIR_FILE_TITLES)
    try:
        ws = _first_matching_sheet(wb, MIR_RM_SHEET_NAMES)
        return count_data_rows(ws)
    finally:
        wb.close()  # read_only workbooks hold a zip file handle open until closed


def get_stock_row_count(plant):
    wb, _ = _load_workbook_for(plant, STOCK_FILE_TITLES)
    try:
        ws = wb[wb.sheetnames[0]]
        return count_data_rows(ws)
    finally:
        wb.close()


def read_stock_rows_for_snapshot(plant):
    """Returns a list of dicts ready to become StockSnapshot rows for today.
    NOTE: column positions below were read directly off each plant's current
    file during the Drive audit (2026-08) - re-verify if a plant's layout
    changes before the standard RM Stock template rolls out there."""
    wb, _ = _load_workbook_for(plant, STOCK_FILE_TITLES)
    try:
        ws = wb[wb.sheetnames[0]]
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]

        def col(*names):
            for n in names:
                for i, h in enumerate(header):
                    if h and n.lower() in str(h).lower():
                        return i
            return None

        idx_code = col("SAP Code", "SAP ITEM CODE", "Item Code")
        idx_desc = col("NAME OF MATERIAL", "Description")
        idx_cat = col("Category")
        idx_qty = col("Closing", "Today Stock")
        idx_rate = col("RATE", "Basic Rate")
        idx_value = col("Value")

        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if idx_desc is None or idx_desc >= len(row) or not row[idx_desc]:
                continue
            rows.append(
                {
                    "material_code": str(row[idx_code]) if idx_code is not None and row[idx_code] else "",
                    "description": str(row[idx_desc]),
                    "category": str(row[idx_cat]) if idx_cat is not None and row[idx_cat] else "",
                    "qty": row[idx_qty] if idx_qty is not None else None,
                    "rate": row[idx_rate] if idx_rate is not None else None,
                    "value": row[idx_value] if idx_value is not None else None,
                }
            )
        return rows
    finally:
        wb.close()
