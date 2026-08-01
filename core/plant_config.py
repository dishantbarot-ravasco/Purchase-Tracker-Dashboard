"""
Single place for every plant-specific, hand-maintained constant: Drive file
titles, PO folder IDs, sheet-name candidates.

Why this file exists: before this, the same Drive folder IDs and file
titles were duplicated across mir_stock.py and the scan_new_pos management
command. Any time a plant renamed a file or a folder got restructured, both
places had to be updated in sync, easy to miss one. Now there's exactly one
file to touch when that happens.
"""
from .models import Plant

# --- MIR / RM Stock file titles, as they exist on Drive today ---------------
# (confirmed via direct Drive search, 2026-08-01 - update here if a plant
# renames its file; nothing else in the codebase should hardcode these).
MIR_FILE_TITLES = {
    Plant.HRS: "HRS MIR FILE 2026-2027.xlsx",
    Plant.RTP_ACHHAD: "RTP ACHHAD MIR FILE 2026-27.xlsx",
    Plant.RTP_VAPI: "RTP VAPI MIR FILE 2026-27.xlsx",
}
STOCK_FILE_TITLES = {
    Plant.HRS: "HRS RAW MATERIAL STOCK.xlsx",
    Plant.RTP_ACHHAD: "RAVASCO ACHHAD RM STOCK FILE.xlsx",
    Plant.RTP_VAPI: "RAVASCO VAPI RM STOCK FILE.xlsx",
}

# The "Raw Material" MIR sheet is what gets matched against PO/Stock data.
# Engineering is a separate tab in the same workbook, kept for reference only
# (per Dishant: MIR itself is only used to cross-check PO/Stock data, not
# stored as its own system of record - Engineering isn't part of that check).
MIR_RM_SHEET_NAMES = ["MIR", "RM", "Raw Material", "MIR RM", "Sheet1"]
MIR_ENGINEERING_SHEET_NAMES = ["Engineering", "MIR Engineering", "Eng"]

# --- Domestic PO folders per plant (2026-2027 fiscal year) ------------------
DOMESTIC_PO_FOLDER_IDS = {
    Plant.HRS: "1MHeOtzEuDl7Ah9PZCcuZH9FvQXmuxUVP",
    Plant.RTP_ACHHAD: "1OZwpdKfMSdNeKzblqOCcO1593JEgqqbx",
    Plant.RTP_VAPI: "1a4YCdLgilB-Zzln7DM_urhfqhGbbZLWV",
}

# --- Import PO folders -------------------------------------------------------
# Only RTP-Vapi has one today. HRS/RTP-Achhad get probed dynamically each
# scan run (see scan_new_pos.py) rather than hardcoded here, since they don't
# exist yet - that's expected, not a bug, until Dishant sets one up.
KNOWN_IMPORT_PO_FOLDER_IDS = {
    Plant.RTP_VAPI: "1YZzjbFrqY3ni_LjBfRHhoFx2uwKv2IWA",
}

# Plants to dynamically probe for a not-yet-created "Import" subfolder,
# mapped to their own Drive root (not the domestic PO folder) so the probe
# searches the right place.
DYNAMIC_IMPORT_PROBE_PLANTS = [Plant.HRS, Plant.RTP_ACHHAD]
