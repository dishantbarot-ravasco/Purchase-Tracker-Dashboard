/**
 * Role storage - reads/writes the "Purchase Tracker - User Roles" Google
 * Sheet that Dishant created and shared with the service account as
 * Editor. This is the entire admin panel's data store: no separate
 * database needed for ~20 users. Sheet layout (row 1 = header):
 *   Email | Role | Plants
 * Plants is a comma-separated list, e.g. "HRS,RTP-Vapi".
 *
 * The five valid roles, matching Dishant's spec (2026-07-31):
 *   admin, viewer-all, viewer-hrs, viewer-rtp-vapi, viewer-rtp-achhad
 *
 * Uses the Sheets REST API directly via fetch (same reasoning as
 * drive.js - avoids the heavy "googleapis" package).
 */
'use strict';

const { getAccessToken } = require('./googleAuth');

const VALID_ROLES = ['admin', 'viewer-all', 'viewer-hrs', 'viewer-rtp-vapi', 'viewer-rtp-achhad'];
const SHEET_RANGE = 'A2:C'; // skip header row
const SHEETS_SCOPE = 'https://www.googleapis.com/auth/spreadsheets';

function sheetsUrl(pathSuffix) {
  return `https://sheets.googleapis.com/v4/spreadsheets/${process.env.ROLES_SHEET_ID}${pathSuffix}`;
}

async function authedFetch(url, options) {
  const token = await getAccessToken(SHEETS_SCOPE);
  const res = await fetch(url, Object.assign({}, options, {
    headers: Object.assign({ Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, (options && options.headers) || {})
  }));
  if (!res.ok) throw new Error(`Sheets API request failed (${res.status}): ${await res.text()}`);
  return res.json();
}

/** Returns every row as { email, role, plants: [...] }. */
async function getAllUsers() {
  const data = await authedFetch(sheetsUrl(`/values/${SHEET_RANGE}`), { method: 'GET' });
  const rows = data.values || [];
  return rows
    .filter(r => r[0]) // skip blank rows
    .map(r => ({
      email: (r[0] || '').trim().toLowerCase(),
      role: (r[1] || '').trim(),
      plants: (r[2] || '').split(',').map(p => p.trim()).filter(Boolean)
    }));
}

/** Looks up one user's access by email, or returns a zero-access record. */
async function getUserAccess(email) {
  const users = await getAllUsers();
  const match = users.find(u => u.email === email.toLowerCase());
  if (!match) return { email, role: 'none', plants: [] };
  return match;
}

/**
 * Adds a new user row or overwrites an existing one (matched by email).
 * Rewrites the whole sheet body rather than trying to patch a single row -
 * simplest correct approach at this scale (well under Sheets API limits
 * for ~20 rows), and avoids off-by-one row-index bugs entirely.
 */
async function upsertUser(email, role, plants) {
  if (VALID_ROLES.indexOf(role) === -1) {
    throw new Error('Invalid role "' + role + '" - must be one of: ' + VALID_ROLES.join(', '));
  }
  const users = await getAllUsers();
  const normalizedEmail = email.trim().toLowerCase();
  const existingIndex = users.findIndex(u => u.email === normalizedEmail);
  const newRow = { email: normalizedEmail, role, plants };
  if (existingIndex === -1) users.push(newRow);
  else users[existingIndex] = newRow;
  await writeAllUsers(users);
}

/** Removes a user's row entirely (matched by email). */
async function removeUser(email) {
  const users = await getAllUsers();
  const normalizedEmail = email.trim().toLowerCase();
  const filtered = users.filter(u => u.email !== normalizedEmail);
  await writeAllUsers(filtered);
}

async function writeAllUsers(users) {
  // Clear the body range first so a shrinking list (e.g. after a removal)
  // doesn't leave stale rows behind past the new, shorter list.
  await authedFetch(sheetsUrl(`/values/${SHEET_RANGE}:clear`), { method: 'POST', body: '{}' });
  const values = users.map(u => [u.email, u.role, u.plants.join(',')]);
  if (values.length) {
    await authedFetch(sheetsUrl('/values/A2?valueInputOption=RAW'), {
      method: 'PUT',
      body: JSON.stringify({ values })
    });
  }
}

module.exports = { getAllUsers, getUserAccess, upsertUser, removeUser, VALID_ROLES };
