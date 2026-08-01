/**
 * Drive/data access layer - reads live PO/MIR/Stock data straight from
 * Google Drive using the service account (never a viewer's own login).
 *
 * Deliberately uses the Drive REST API directly via fetch + an access
 * token from google-auth-library, instead of the full "googleapis" npm
 * package - that package bundles API definitions for hundreds of Google
 * products and is slow to load/require in constrained environments; the
 * plain REST calls below need only the two endpoints this app actually
 * uses, so they start up fast and have a much smaller dependency footprint.
 */
'use strict';

const XLSX = require('xlsx');
const { getAccessToken } = require('./googleAuth');

// Same plant folder ids and file titles documented in the Apps Script
// version and the Cowork artifact's memory - keep these three in sync if
// anything on Drive ever gets renamed or moved.
const PLANT_FOLDERS = {
  'HRS': '1mmZrMukdhuYyHQGEfH6ci_LIiNOEQ8Qp',
  'RTP-Achhad': '1mviBKPDyZLVLfGVZ780BX_xPR9fLxdtH',
  'RTP-Vapi': '1kzWf8sf9UfXBG34WalJxgu7djX5fwNUU'
};

const PLANT_FILES = {
  'HRS': {
    domesticCsvTitle: 'Master_HRS_SILVASSA_Domestic_Purchase_Data.csv',
    mirTitle: 'HRS MIR FILE 2026-2027.xlsx',
    stockTitle: 'HRS RAW MATERIAL STOCK.xlsx'
  },
  'RTP-Achhad': {
    domesticCsvTitle: 'Master_RTP_Achhad_Domestic_Purchase_Data.csv',
    mirTitle: 'RTP ACHHAD MIR FILE 2026-27.xlsx',
    stockTitle: 'RAVASCO ACHHAD RM STOCK FILE.xlsx'
  },
  'RTP-Vapi': {
    domesticCsvTitle: 'Master_RTP_VAPI_Domestic_Purchase_Data.csv',
    mirTitle: 'RTP VAPI MIR FILE 2026-27.xlsx',
    stockTitle: 'RAVASCO VAPI RM STOCK FILE.xlsx'
  }
};

const DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.readonly';

/**
 * Finds the most recently modified file matching an exact title inside a
 * specific folder - scoped search, not Drive-wide (correctness + good
 * habit, even though the service account's own access is already limited
 * to what's been shared with it).
 */
async function findFileInFolder(token, folderId, title) {
  const escapedTitle = title.replace(/'/g, "\\'");
  const q = encodeURIComponent(`'${folderId}' in parents and name = '${escapedTitle}' and trashed = false`);
  const url = `https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id,name,modifiedTime)&orderBy=modifiedTime desc&pageSize=1`;
  const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
  if (!res.ok) throw new Error(`Drive search failed (${res.status}) for "${title}": ${await res.text()}`);
  const data = await res.json();
  const file = data.files && data.files[0];
  if (!file) throw new Error(`"${title}" not found in folder ${folderId} - it may have been renamed, or the service account was not given access to this folder.`);
  return file;
}

async function downloadFileBuffer(token, fileId) {
  const url = `https://www.googleapis.com/drive/v3/files/${fileId}?alt=media`;
  const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
  if (!res.ok) throw new Error(`Drive download failed (${res.status}) for file ${fileId}: ${await res.text()}`);
  const arrayBuffer = await res.arrayBuffer();
  return Buffer.from(arrayBuffer);
}

/** Parses CSV bytes into an array of row objects keyed by the header row. */
function parseCsvBuffer(buffer) {
  const text = buffer.toString('utf8');
  const lines = text.split(/\r?\n/).filter(l => l.length > 0);
  if (!lines.length) return [];
  const parseLine = (line) => {
    // Minimal quoted-CSV parser - handles quoted fields with embedded commas,
    // which is the one thing a naive split(',') gets wrong.
    const out = [];
    let cur = '';
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQuotes) {
        if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (ch === '"') { inQuotes = false; }
        else { cur += ch; }
      } else {
        if (ch === '"') inQuotes = true;
        else if (ch === ',') { out.push(cur); cur = ''; }
        else cur += ch;
      }
    }
    out.push(cur);
    return out;
  };
  const headers = parseLine(lines[0]).map(h => h.trim());
  return lines.slice(1).map(line => {
    const cells = parseLine(line);
    const obj = {};
    headers.forEach((h, i) => { obj[h] = cells[i] !== undefined ? cells[i] : ''; });
    return obj;
  });
}

/** Parses xlsx bytes and returns the first sheet as a 2D array of rows. */
function parseXlsxBuffer(buffer) {
  const wb = XLSX.read(buffer, { type: 'buffer' });
  const firstSheetName = wb.SheetNames[0];
  const sheet = wb.Sheets[firstSheetName];
  return XLSX.utils.sheet_to_json(sheet, { header: 1, blankrows: false });
}

/**
 * Returns a basic summary for one plant - PO count, total PO value, MIR row
 * count, RM Stock row count. Matches the Phase 1 scope already shipped in
 * the Apps Script version; full reconciliation logic is a later phase.
 * Each section fails independently so one missing/renamed file doesn't
 * blank out the whole plant card.
 */
async function getPlantSummary(plantKey) {
  const folderId = PLANT_FOLDERS[plantKey];
  const files = PLANT_FILES[plantKey];
  if (!folderId || !files) throw new Error('Unknown plant: ' + plantKey);

  const token = await getAccessToken(DRIVE_SCOPE);
  const result = { plant: plantKey, poCount: 0, totalValue: 0, mirRowCount: 0, stockRowCount: 0, errors: [] };

  try {
    const csvFile = await findFileInFolder(token, folderId, files.domesticCsvTitle);
    const buffer = await downloadFileBuffer(token, csvFile.id);
    const rows = parseCsvBuffer(buffer);
    result.poCount = new Set(rows.map(r => r['PO Number'])).size;
    result.totalValue = rows.reduce((sum, r) => {
      const v = parseFloat((r['Total Inclusive Value'] || '0').toString().replace(/,/g, ''));
      return sum + (isNaN(v) ? 0 : v);
    }, 0);
  } catch (e) {
    result.errors.push('Domestic PO data: ' + e.message);
  }

  try {
    const mirFile = await findFileInFolder(token, folderId, files.mirTitle);
    const buffer = await downloadFileBuffer(token, mirFile.id);
    const rows = parseXlsxBuffer(buffer);
    result.mirRowCount = Math.max(rows.length - 1, 0);
  } catch (e) {
    result.errors.push('MIR data: ' + e.message);
  }

  try {
    const stockFile = await findFileInFolder(token, folderId, files.stockTitle);
    const buffer = await downloadFileBuffer(token, stockFile.id);
    const rows = parseXlsxBuffer(buffer);
    result.stockRowCount = Math.max(rows.length - 1, 0);
  } catch (e) {
    result.errors.push('RM Stock data: ' + e.message);
  }

  return result;
}

module.exports = { getPlantSummary, PLANT_FOLDERS };
