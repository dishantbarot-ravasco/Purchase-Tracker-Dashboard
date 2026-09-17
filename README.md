# Purchase Tracker Dashboard

A Django service that reconciles **Purchase Orders ↔ MIR (Material Inward Register) ↔ Raw
Material Stock** for Ravasco's three plants — **HRS, RTP-Achhad, and RTP-Vapi**.

Source data lives in Google Drive (PO master CSVs, MIR and Stock xlsx files). This app syncs it
into Postgres on a schedule, runs the reconciliation there, and stores the results — so every
viewer opens the same pre-computed numbers instead of re-parsing Drive on each page load, and
nobody needs their own Drive session.

- **Docs**: [CLAUDE.md](CLAUDE.md) for conventions, rationale, and the gotchas that have already
  bitten this project. [ARCHITECTURE.md](ARCHITECTURE.md) for the request/auth flow diagrams.
- **Status**: all three plants are live end-to-end (domestic POs, import POs, MIR, stock,
  matching, dashboard), with RoDTEP scrip and Advance Licence ledgers alongside the import view.

## Architecture at a glance

- **Django + DRF + Postgres + WhiteNoise**, dependencies managed with `uv`.
- `apps/core` — models and migrations only. `apps/api` — HTTP views. `apps/services` — Drive
  access, parsers, matching engines, and auth-adjacent services.
- **Frontend is static HTML + vanilla JS** served straight out of `frontend/` — no bundler, no
  build step.
- **Per-plant models, not a shared schema.** Each plant's MIR/Stock spreadsheets have genuinely
  different column layouts, so each plant gets its own models, parsers, matcher, and router. See
  CLAUDE.md before "fixing" this.
- **Drive access uses a service account**, not a user OAuth session, so syncs run unattended.
  Human login is separate: device-aware 2FA with JWTs in httpOnly cookies.
- **Syncing is scheduled** (django-q2, hourly 9 AM–8 PM IST) and can also be triggered from the
  dashboard or the admin panel.

## How matching works

Each plant has its own matcher (`apps/services/matching*.py`) sharing one approach:

- **Vendor name is a hard gate**, never a scored factor — two different vendors are never the same
  PO. Names are normalised (legal suffixes stripped) and compared by containment, not equality.
- **PO ↔ MIR** is a weighted score: material description overlap 30%, quantity 20%, rate 20%,
  pre-tax value 30%. An exact PO-number hit is a free shortcut when present — but MIR's PO-number
  field is unreliable (~30% blank at HRS, 100% blank at Vapi), so it is never the primary key.
  Below a 0.55 threshold a line item is left unmatched rather than forced onto a poor candidate.
- **MIR ↔ Stock** is gated on (material, vendor) for HRS and Vapi; Achhad's Stock sheet has no
  vendor column at all, so it gates on material alone — a materially weaker guarantee, documented
  as such rather than treated as equivalent.

**Every match is a suggestion, not a fact.** Accuracy has not been measured against labelled
ground truth. Don't wire any automatic downstream action off a match without a human in the loop.

## Local setup

```bash
uv sync
cp .env.example .env   # fill in DB, Google service account, SMTP, and OAuth details
uv run python manage.py migrate
uv run python manage.py createcachetable

# Bootstrap the first account - nobody can log in without at least one.
uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin

uv run python manage.py runserver
# -> http://127.0.0.1:8000/login.html
```

Read `.env.example`'s comments before filling it in — the service-account JSON and Windows-path
entries both have parsing traps that have cost real debugging time (detailed in CLAUDE.md).

If this repo sits inside a OneDrive-synced folder, exclude it from sync before putting real
secrets in `.env`. `.env` is gitignored, but OneDrive doesn't respect `.gitignore`.

### Docker (optional)

Runs Postgres + the app + a worker together, matching Render's Python 3.12/gunicorn/qcluster
setup. Local dev only — it does not replace `render.yaml`'s deploy pipeline.

```bash
cp .env.example .env
docker compose up -d
docker compose exec app uv run python manage.py create_pt_user --email you@ravasco.com --password '...' --role admin
# -> http://localhost:8000/login.html
```

`docker-compose.yml` points the containers at its own `db` service, so no `.env` changes are
needed. Check `docker compose logs app` for tracebacks, and confirm `/static/...` assets load —
a broken `collectstatic` shows up exactly there.

## Everyday commands

```bash
uv run pytest                         # test suite (real Postgres)
uv run ruff check .                   # lint gate (also in CI)
uv run python manage.py ensure_schedules

# Per-plant pipeline: sync the three sources, then match.
uv run python manage.py sync_po_csv && uv run python manage.py sync_mir \
  && uv run python manage.py sync_stock && uv run python manage.py match_hrs
```

Achhad and Vapi use the same shape (`sync_achhad_*`/`match_achhad`, `sync_vapi_*`/`match_vapi`).
Every `sync_*` command takes `--file <path>` to parse a local copy instead of hitting Drive. The
full command list is in CLAUDE.md.

## Pages

| Page | What it is |
| --- | --- |
| `login.html` | Password + email OTP, or Sign in with Google |
| `home.html` | Landing page with a live cross-plant KPI row |
| `index.html` (`/`) | The reconciliation dashboard — Purchase Orders and Raw Material Analysis |
| `search-po.html` | Look up a PO number across all three plants at once |
| `review.html` | Match-accuracy review queue (Correct / Incorrect / Unsure) |
| `admin.html` | Admin only — sync status, user management, activity overview |

Roles are `admin`, `editor`, `viewer`. Writes (inline field corrections, dismissing a flag, user
management) are role-gated and audited; `PTUser.plants` can additionally scope an account to
specific plants, where an empty list means all of them.
