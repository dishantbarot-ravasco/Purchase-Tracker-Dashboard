/**
 * ===========================================================================
 * PURCHASE TRACKER - HOSTED DASHBOARD (Render)
 * ===========================================================================
 * Ravasco Transmission and Packing. Built 2026-07-31, after the Apps Script
 * route hit an org-level Workspace restriction that blocked its deployed
 * URL from ever loading. This is a fully independent, custom-hosted
 * alternative with the same goals: real Google sign-in (inheriting your
 * Workspace's 2FA), per-user role gating (admin / viewer-all / viewer-hrs /
 * viewer-rtp-vapi / viewer-rtp-achhad), and Drive data that stays private -
 * no viewer ever gets their own Drive folder access. A dedicated Google
 * service account reads Drive on the app's behalf; the role list itself
 * lives in a Google Sheet ("Purchase Tracker - User Roles") that admins can
 * edit through this app's own Admin screen - no code edits needed to add
 * or change a person, unlike the Apps Script version's hardcoded ROLE_MAP.
 *
 * See README.md in this folder for the full one-time setup + Render
 * deployment steps.
 * ===========================================================================
 */
'use strict';

const path = require('path');
const express = require('express');
const session = require('express-session');
const { OAuth2Client } = require('google-auth-library');

const { getPlantSummary, PLANT_FOLDERS } = require('./lib/drive');
const { getAllUsers, getUserAccess, upsertUser, removeUser, VALID_ROLES } = require('./lib/roles');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// ---------------------------------------------------------------------------
// Session - identifies who's signed in on this browser. MemoryStore (the
// default) is fine for ~20 users on a single Render instance; the only
// downside is everyone gets signed out if the service restarts/redeploys,
// which is a reasonable tradeoff for this scale rather than adding a
// separate session database.
// ---------------------------------------------------------------------------
app.use(session({
  secret: process.env.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: { httpOnly: true, secure: true, sameSite: 'lax', maxAge: 1000 * 60 * 60 * 12 } // 12h
}));

const oauth2Client = new OAuth2Client(
  process.env.GOOGLE_CLIENT_ID,
  process.env.GOOGLE_CLIENT_SECRET,
  process.env.GOOGLE_REDIRECT_URI
);

// ---------------------------------------------------------------------------
// Auth routes
// ---------------------------------------------------------------------------

// Starts the Google sign-in flow. Only asks for 'openid email profile' -
// deliberately NOT any Drive/Sheets scope, since the signed-in user's own
// permissions are never used to read Drive - only the service account's
// are. That keeps the consent screen minimal and low-friction for everyone
// signing in.
app.get('/auth/google', (req, res) => {
  const url = oauth2Client.generateAuthUrl({
    access_type: 'online',
    scope: ['openid', 'email', 'profile'],
    hd: 'ravasco.com' // hints Google to show only ravasco.com accounts; verified again server-side below regardless
  });
  res.redirect(url);
});

app.get('/auth/google/callback', async (req, res) => {
  try {
    const { tokens } = await oauth2Client.getToken(req.query.code);
    const ticket = await oauth2Client.verifyIdToken({
      idToken: tokens.id_token,
      audience: process.env.GOOGLE_CLIENT_ID
    });
    const payload = ticket.getPayload();
    const email = (payload.email || '').toLowerCase();

    // Server-side domain check - never trust the client-side 'hd' hint
    // alone, since that only affects which accounts Google's picker shows,
    // it does not restrict who could technically complete the flow.
    if (!email.endsWith('@ravasco.com')) {
      return res.status(403).send('This app is restricted to Ravasco Google accounts.');
    }

    req.session.email = email;
    req.session.name = payload.name || email;
    res.redirect('/');
  } catch (e) {
    console.error('OAuth callback failed:', e);
    res.status(500).send('Sign-in failed: ' + e.message);
  }
});

app.post('/auth/logout', (req, res) => {
  req.session.destroy(() => res.json({ ok: true }));
});

// ---------------------------------------------------------------------------
// Access control helpers
// ---------------------------------------------------------------------------

function requireLogin(req, res, next) {
  if (!req.session.email) return res.status(401).json({ error: 'Not signed in.' });
  next();
}

async function requireAdmin(req, res, next) {
  const access = await getUserAccess(req.session.email);
  if (access.role !== 'admin') return res.status(403).json({ error: 'Admin access required.' });
  next();
}

// ---------------------------------------------------------------------------
// API routes
// ---------------------------------------------------------------------------

app.get('/api/me', requireLogin, async (req, res) => {
  try {
    const access = await getUserAccess(req.session.email);
    res.json(access);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/dashboard', requireLogin, async (req, res) => {
  try {
    const access = await getUserAccess(req.session.email);
    if (access.role === 'none') return res.json({ access, plants: [] });
    const plants = await Promise.all(access.plants.map(p => getPlantSummary(p)));
    res.json({ access, plants });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// --- Admin-only: manage the user/role list ---

app.get('/api/admin/users', requireLogin, requireAdmin, async (req, res) => {
  try {
    res.json({ users: await getAllUsers(), validRoles: VALID_ROLES, plants: Object.keys(PLANT_FOLDERS) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/admin/users', requireLogin, requireAdmin, async (req, res) => {
  try {
    const { email, role, plants } = req.body;
    if (!email || !role) return res.status(400).json({ error: 'email and role are required.' });
    await upsertUser(email, role, Array.isArray(plants) ? plants : []);
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

app.delete('/api/admin/users/:email', requireLogin, requireAdmin, async (req, res) => {
  try {
    await removeUser(req.params.email);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Fallback - serve the SPA shell for any other GET so the frontend's own
// routing (there isn't any real routing here, just this one screen) never
// 404s on a refresh.
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('Purchase Tracker listening on port ' + PORT));
