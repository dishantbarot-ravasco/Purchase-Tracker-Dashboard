# Purchase Tracker Dashboard

Purchase Order / MIR / RM Stock / Advance License dashboard for Ravasco
Transmission and Packing (HRS, RTP-Achhad, RTP-Vapi). Backend: Django +
Postgres. Frontend: vanilla HTML/CSS/JS (`public/index.html`), served
directly by Django, no build step.

An earlier Node/Express prototype (`server.js`, `package.json`, `lib/`)
lives alongside this for reference and is not used by the Django app -
safe to ignore, harmless to leave in the repo.

## What this replaces, and why

- **PO data**: previously 6 CSVs on Drive (one per plant, domestic +
  imports), edited by hand which risked Google Sheets creating duplicate
  copies on open. Now lives in Postgres (`PurchaseOrder`/`POItem`/`POFlag`),
  with the CSVs read-only during the transition.
- **RM Stock history**: the raw Stock file has no date column and gets
  overwritten daily by factory staff, so there was no way to do
  consumption trend analysis. `StockSnapshot` captures one row per
  material per plant per day, safe to rerun (unique constraint on
  plant+material+date).
- **MIR**: intentionally NOT stored as its own table. It's read live off
  Drive (`core/mir_stock.py`) purely to cross-check PO/Stock data and
  surface qty/rate discrepancies - Dishant's call, since MIR itself isn't
  a system of record that needs history.
- **Advance Licenses**: previously re-parsed from the license PDF live on
  every dashboard load, with usage against POs best-effort guessed from
  linked PO text. Now a real ledger (`AdvanceLicense`/`LicenseItem`/
  `LicensePOUsage`), so "which PO used this license, for how much, what's
  left" is a stored fact, not a re-computation.
- **Extraction (new POs / license letters)**: does NOT call the Anthropic
  API directly (cost reasons). Instead: `scan_new_pos` finds new PO
  folders on Drive and writes a uniquely-named request batch file to a
  shared Drive folder; a Claude scheduled task (set up separately, outside
  this codebase) reads that batch, extracts the documents, and writes a
  uniquely-named result batch back; `ingest_extraction_results` reads that
  and upserts into Postgres. `ExtractionQueue` in Postgres is the real
  source of truth for "what's still pending" - the Drive files are just
  the hand-off inbox/outbox, never mutated in place, so there's no race
  between the two sides.

## Project layout

```
manage.py
purchase_tracker/          Django project (settings, urls, wsgi)
core/
  models.py                 All persisted tables (see its module docstring)
  plant_config.py            One place for Drive folder IDs / file titles / sheet names
  drive.py                   Service-account Drive access (read + hand-off file writes)
  mir_stock.py                Live MIR/RM Stock file readers, per-plant column adapters
  auth_views.py               Google Sign-In, restricted to @ravasco.com
  decorators.py                Server-side login/role/plant-access enforcement
  serializers.py                 Model -> JSON dict shaping, kept out of views.py
  views.py                        Request handling, thin - delegates to the above
  admin.py                         Django admin (admin-only fallback, not the plant-staff UI)
  management/commands/
    snapshot_rm_stock.py           Daily RM Stock snapshot job
    scan_new_pos.py                 Extraction pipeline, step 1 (queue + request batch)
    ingest_extraction_results.py     Extraction pipeline, step 2 (ingest result batch)
    test_drive_access.py             One-off: confirms the service account can read Drive
public/index.html            Frontend (vanilla JS, no build step)
secrets/service_account.json  Git-ignored - Drive service account key (never commit)
```

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env              # then fill in real values, see below
python manage.py migrate
python manage.py test_drive_access   # confirms the service account can see each plant's Drive folder
python manage.py runserver
```

## Required secrets / config (see `.env.example` for the full list)

- `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_REDIRECT_URI`:
  from Google Cloud Console, used only for "Sign in with Google" -
  restricted server-side to `@ravasco.com` emails (see `core/auth_views.py`).
- `secrets/service_account.json`: a separate Google Cloud service account
  key, distinct from the OAuth client above. This is what reads Drive
  regardless of who's logged in - the signed-in user's own Drive
  permissions are never used. Must be shared as Viewer on the "Purchase
  Orders HO" Drive folder (permissions cascade to everything below it, so
  the 3 plant folders don't need separate sharing).
- `DATABASE_URL`: Postgres connection string. Render provides this
  automatically once a Postgres instance is attached to the service.
- `DRIVE_EXTRACTION_QUEUE_FOLDER`: Drive folder ID used for the
  extraction request/result hand-off files (a subfolder of "Purchase
  Orders HO", already shared via the parent).

**Never commit `.env` or `secrets/service_account.json`** - both are
git-ignored. On Render, set env vars in the service's Environment tab and
upload the service account key as a Secret File, not a plain env var (it's
multi-line and easy to mangle as one).

## Deploying on Render (from scratch)

1. New > PostgreSQL. Once created, Render exposes an Internal Database URL.
2. New > Web Service, connect this GitHub repo.
   - Environment: Python 3
   - Build Command: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
   - Start Command: `gunicorn purchase_tracker.wsgi`
3. In the service's Environment tab, set every variable listed in
   `.env.example` with real values, plus `DATABASE_URL` from step 1.
4. Upload `service_account.json` as a Secret File; set
   `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` to the mounted path Render shows you
   (typically `/etc/secrets/service_account.json`).
5. Once deployed, add the real `https://<your-app>.onrender.com/auth/google/callback`
   redirect URI back in Google Cloud Console's OAuth client settings.

## Daily/scheduled jobs

Not wired to a scheduler yet - run these on whatever cadence you choose
(Render Cron Jobs, or an external scheduler hitting a protected endpoint):

- `python manage.py snapshot_rm_stock` - once daily, e.g. ~5pm, matching
  the old EOD timing.
- `python manage.py scan_new_pos` - as often as you want new POs picked up
  for extraction (e.g. every few hours).
- `python manage.py ingest_extraction_results` - runs after the Claude
  scheduled task has had a chance to process a batch (e.g. an hour after
  `scan_new_pos`).

## Security model

- No local passwords anywhere - login is Google Sign-In only, restricted
  server-side to `@ravasco.com` (see `core/auth_views.py`'s module
  docstring for why the `hd` OAuth hint alone isn't sufficient).
- 2FA is inherited from your Google Workspace admin console's own
  enforcement (Security > 2-Step Verification) - this app doesn't
  implement 2FA itself, confirm that setting is actually on.
- Plant-level access is re-checked server-side on every API call (see
  `core/decorators.py`) - never just hidden in the frontend.
- Every login and every data-changing action is written to `AuditLog`.
