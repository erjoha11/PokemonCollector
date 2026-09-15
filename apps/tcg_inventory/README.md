# tcg_inventory

A webapp that replaces an Excel workbook for tracking a physical Pokémon
card collection. FastAPI + Jinja2/HTMX — no build step. Runs two ways:

- **Locally**: `python app.py`, SQLite, no accounts, no setup.
- **Deployed**: Vercel (hosting) + Supabase (Postgres + login) — see
  "Deploying to Vercel + Supabase" below.

## Running it locally

```bash
cd apps/tcg_inventory
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000. Data lives in `tcg_inventory.db` (a single
SQLite file next to `app.py`, git-ignored, created automatically on first
run).

## Pages

- **Dashboard** (`/`) — headline totals, Collection/Bulk breakdown, by
  series, top 10 most valuable cards, by rarity.
- **Inventory** (`/inventory`) — full searchable/filterable/sortable card
  table.
- **Transactions** (`/transactions`) — a purchase/sale log per card.
- **Import / Sync** (`/import`) — pull Dex CSV exports straight from Dropbox,
  or upload them manually.

## Data model

`cards`, `collections`, `card_collections` (many-to-many), `binders`,
`transactions`, plus `set_release_order` (a lookup table for chronological
sorting — see "Chronological sorting" below).

`duplicates`, `total_value`, and `unique_value` are **never stored** —
they're computed live (`Card.duplicates` / `Card.total_value` /
`Card.unique_value` in `models.py`, and the dashboard aggregates in
`queries.py`). A stored, independently-maintained `duplicates` value going
out of sync with `qty` was a real bug in the Excel version this app
replaces — the fix is to never store it at all.

## Business rules (from the Excel system this replaces)

These are encoded in `constants.py` and `importer.py`. Do not change them
without updating both the code and this doc.

1. **Dex's "My Collection" is the physical inventory.** Every other Dex
   category is a tag on a subset of the same physical cards, never a
   separate set of cards. "Wishlist" and any category starting with
   "151 Fullarts" are always fully ignored.
2. **`duplicates = max(qty - 1, 0)`**, always derived, never stored.
3. **Primary collection.** When a card belongs to more than one collection,
   only one gets "credit" in dashboard summaries (so a card is never
   double-counted). Priority, highest first:
   1. Illustrator collections (Tomokazu Komiya, Shinji Kanda, Yuka Morii,
      Saya Tsuruta)
   2. Vintage Collection
   3. Collection (generic Dex folder)
   4. Scarlet & Violet: 151 JP/KR

   Any other/unknown collection name defaults to the lowest priority. This
   ranking **only affects the dashboard's "which collection gets credit"
   calculation** — `card_collections` itself always keeps every real tag.
4. **Binder tags.** "Illustrator Binder", "Vintage Binder", "151 Binder",
   and "Tradebinder" route to `binder_id` instead of becoming a collection.
5. **Sync semantics.**
   - A normal sync flags cards missing from the new "My Collection" export
     (`flagged_missing_since` set to today, only if not already flagged —
     the date marks when it was *first* noticed missing, not the last sync
     that still didn't see it) but never deletes them.
   - "Full load" (only when explicitly requested, e.g. to clean up bad
     data) actually deletes cards missing from the export.
   - Every sync is expected to include both the main export and the
     Vintage Collection export together — the Import page lets you select
     multiple files at once for exactly this reason.
   - A collection's tag membership (e.g. Vintage Collection) is only
     touched for categories actually present in that sync's uploaded
     files. A category absent from the current batch is left completely
     untouched — this is what stops a sync without a fresh Vintage export
     from wiping existing Vintage Collection tags.
6. **Chronological sorting.** Sets should be sortable by actual release
   order, not alphabetically. `set_release_order` (`Series`, `Set`,
   `release_rank`) is the lookup table for this — it ships **empty**. The
   ~100-row table from the Excel work needs to be supplied separately to
   populate it (ask for it / provide a CSV and it can be loaded directly
   into that table). Until then, sorting falls back to name/series/set
   order in the Inventory table.

## CSV import format

Dex export, semicolon-separated:

```
Type;Category;Locale;Series;Set;Id;Number;Name;Variant;Rarity;Illustrator;Quantity;Price;Note 1;Note 2;Note 3;Note 4;Note 5
```

Each file is one Dex folder/category export (the `Category` column is
constant per file). Upload as many category files as you have for one sync
— `Note 1`–`Note 5` are concatenated into `notes` when present, and `Locale`
(which language/region print, e.g. `ENG`/`JPN`) is stored as `language` and
shown/filterable/sortable as "Language" in Inventory. Routing (My Collection /
binder / collection / excluded) is applied per the rules above based on
each row's `Category` value. `Type` is read but unused — every real Dex
export sets it to the constant `Card` on every row, so it carries no
per-card information.

## Dropbox import setup

The Import page can list and pull CSV files directly from a Dropbox folder
(read-only: `files.metadata.read` + `files.content.read`), so you don't
have to download from Dropbox and re-upload by hand. Manual upload still
works with no setup at all — Dropbox is optional.

One-time setup:

1. Create an app at [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
   → "Create app" → **Scoped access** → **App folder** or **Full Dropbox**
   (your choice — App folder is more restrictive and usually enough if you
   put your Dex exports there). Under **Permissions**, enable
   `files.metadata.read` and `files.content.read`, then save.
2. Note the app's **App key** and **App secret** (Settings tab).
3. From `apps/tcg_inventory/`, run:
   ```bash
   python dropbox_setup.py
   ```
   It prints a URL — open it, approve access, paste the code back into the
   terminal. It then prints `DROPBOX_APP_KEY` / `DROPBOX_APP_SECRET` /
   `DROPBOX_REFRESH_TOKEN` values.
4. Copy `.env.example` to `.env` in `apps/tcg_inventory/` and paste those
   three values in, plus `DROPBOX_FOLDER` (the path to the folder you save
   Dex exports to, e.g. `/Dex Exports`).
5. Restart `python app.py`. The Import page now lists CSV files from that
   folder with checkboxes — select the ones for this sync (main export +
   Vintage export, same rule as manual upload) and click "Hent valgte
   filer og synk".

The refresh token doesn't expire, so this is a one-time setup. Nothing is
ever written back to Dropbox.

## Deploying to Vercel + Supabase

Moving off `localhost` means two things change: the SQLite file needs to
become a real database (Vercel's filesystem is read-only/ephemeral —
`db.py` refuses to start on Vercel without `DATABASE_URL` set, rather than
silently failing on writes), and the app is now reachable by anyone with
the URL, so it needs a login. Both are optional until you set the matching
env vars — nothing here changes local `python app.py` behavior.

### 1. Supabase (database + login)

1. Create a project at [supabase.com](https://supabase.com/dashboard).
2. **Database**: Settings → Database → **Connection string** → copy the
   **Transaction pooler** one (port 6543, not the direct 5432 one — the
   pooler is what keeps a serverless app from exhausting Postgres'
   connection limit across many short-lived invocations). This is
   `DATABASE_URL`.
3. **Auth**: Authentication → Users → **Add user** → create yourself an
   email/password. There's no public signup route in this app on purpose
   — you create your own account here, once.
4. Grab two more values from Settings → API:
   - **Project URL** → `SUPABASE_URL`
   - **anon key**, the legacy JWT one (starts with `eyJhbGci...`, under the
     "Legacy anon, service_role API keys" tab — not the newer
     `sb_publishable_...` key) → `SUPABASE_ANON_KEY`

   `SUPABASE_JWT_SECRET` is optional (see "How the login works" below) —
   only set it if the project is still on the legacy HS256 secret
   (Settings → JWT Keys → "Legacy JWT Secret" tab).

### 2. Vercel (hosting)

1. Import this repo as a new Vercel project.
2. **Settings → General → Root Directory** → set to `apps/tcg_inventory`
   (this is a monorepo; Vercel needs to know the app doesn't live at the
   repo root).
3. **Settings → Environment Variables** → add `DATABASE_URL`,
   `SUPABASE_URL`, `SUPABASE_ANON_KEY` (and `SUPABASE_JWT_SECRET` if you
   grabbed it above), and — if you're also using Dropbox import —
   `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`,
   `DROPBOX_FOLDER`.
4. Deploy. `vercel.json` + `api/index.py` route every request to the same
   FastAPI app (`api/index.py` just re-exports `app` from `app.py` — all
   the actual routes are unchanged).
5. Open the deployed URL → you'll land on `/login` → sign in with the user
   you created in Supabase.

### How the login works

`auth.py` talks to Supabase's Auth API (GoTrue) directly over HTTP — no
supabase-js, no client-side JS. `/login` posts email/password, gets back a
short-lived access token, and stores it in an `httpOnly` cookie. Every
other route is gated by a middleware (`auth_guard` in `app.py`) that
verifies the cookie's JWT. Verification tries Supabase's public JWKS
endpoint first (`/auth/v1/.well-known/jwks.json`) — this is Supabase's
current default: an asymmetric signing key (e.g. ES256), so the *public*
key can be published and fetched instead of a shared secret. If a project
is still on the legacy shared HS256 secret, JWKS won't have a matching
key and verification falls back to `SUPABASE_JWT_SECRET`. The access token
expires after Supabase's default (1 hour); there's no silent refresh yet,
so an expired session just bounces back to `/login`. Auth is skipped
entirely whenever `SUPABASE_URL`/`SUPABASE_ANON_KEY` aren't both set —
that's what keeps local `python app.py` login-free.

### Automatic daily sync (Vercel Cron)

Once Dropbox import is set up (see "Dropbox import setup" above), the
deployed app can sync itself automatically instead of anyone clicking
"Hent valgte filer og synk" — `vercel.json` schedules a
[Vercel Cron Job](https://vercel.com/docs/cron-jobs) that hits
`GET /cron/dropbox-sync` once a day (`0 5 * * *`, i.e. 05:00 UTC — edit
the `crons` entry in `vercel.json` to change it). That route pulls every
CSV currently in the configured `DROPBOX_FOLDER` and runs a normal sync
(never full load — an unattended job should never delete cards, only flag
missing ones).

To turn it on:

1. Make sure `DROPBOX_APP_KEY`/`DROPBOX_APP_SECRET`/`DROPBOX_REFRESH_TOKEN`/
   `DROPBOX_FOLDER` are already set in Vercel (see "Dropbox import setup").
2. Add a `CRON_SECRET` environment variable in Vercel (any random string —
   Vercel automatically sends it back as `Authorization: Bearer
   <CRON_SECRET>` on its own cron requests, and `/cron/dropbox-sync`
   checks it). Without this set, the endpoint runs unauthenticated, which
   still works but means anyone who finds the URL could trigger a sync.
3. Redeploy. Vercel's dashboard (Project → Cron Jobs) shows each run and
   its response — `cards_created`/`cards_updated`/etc. and any warnings,
   the same summary the manual sync page shows.

Keep your Dropbox folder holding the *current* full set of exports (main
collection + Vintage + whatever else you track) — each cron run syncs
whatever's in there at the time, same as selecting every file on the
Import page manually.

## Project layout

- `app.py` — FastAPI app, routes, entrypoint (`python app.py`).
- `db.py` — SQLite (local) / Postgres (Supabase, via `DATABASE_URL`) engine
  and session setup.
- `auth.py` — Supabase Auth login + JWT verification.
- `models.py` — SQLAlchemy models + computed properties.
- `constants.py` — the Dex category → binder/collection/priority mapping.
- `importer.py` — CSV parsing and sync logic.
- `queries.py` — dashboard aggregation queries.
- `dropbox_client.py` — list/download CSV files from Dropbox (read-only).
- `dropbox_setup.py` — one-time CLI to obtain a Dropbox refresh token.
- `api/index.py`, `vercel.json` — Vercel deployment entrypoint/config.
- `templates/`, `static/` — Jinja2 + HTMX frontend (HTMX is vendored in
  `static/htmx.min.js`, no CDN dependency, works fully offline).
- `tests/` — pytest, offline, no network or real DB file touched.

Modules use flat imports (`from db import ...`, not a relative-import
package) on purpose, so `python app.py` works standalone as required.
`tests/conftest.py` adds this directory to `sys.path` so the test suite can
import the same modules directly.

## Testing

```bash
python -m pytest apps/tcg_inventory
```

Runs entirely offline against an in-memory/temp-file SQLite database — no
network access or the app's real `tcg_inventory.db` involved.

## Not built yet (out of scope for this pass)

- OneDrive fetching (Dropbox is supported — see "Dropbox import setup").
- The `set_release_order` seed data (see "Chronological sorting" above).
- `classification` and `location` are plain nullable text fields with no
  UI to edit them yet beyond what's shown in the Inventory table.
- Database migrations — `init_db()` only ever creates missing tables
  (`CREATE TABLE IF NOT EXISTS`, via SQLAlchemy). A schema change later
  will need a real migration (e.g. Alembic) rather than editing a live
  Supabase table by hand.
- Silent session refresh — an expired Supabase session redirects to
  `/login` instead of refreshing quietly in the background.
