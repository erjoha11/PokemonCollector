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
- **Transactions** (`/transactions`) — a purchase/sale log per card, plus a
  compact economic snapshot (net invested, current value, paper gain/loss)
  and a collapsible "Vis grafer" section with the value-growth and cash-flow
  charts (formerly the standalone Analyse page).
- **Sync Log** (`/import`) — read-only history of past syncs (daily cron,
  or a manual Dropbox sync). There is no manual CSV-upload page; see
  "Dropbox import setup" below for the only way to sync outside the cron.

## Data model

`cards`, `collections`, `card_collections` (many-to-many), `binders`,
`transactions`, `card_snapshots` (see "Value history" below), plus
`set_release_order` (a lookup table for chronological sorting — see
"Chronological sorting" below).

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
     Vintage Collection export together — the Dropbox picker lets you
     select multiple files at once for exactly this reason.
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

Dropbox is how card data gets into the app at all — there is no manual
CSV-upload page (removed; nobody used it). This pulls CSV files directly
from a Dropbox folder (read-only: `files.metadata.read` +
`files.content.read`), either via the daily cron or the manual Dropbox
picker below, so setting this up is required before the app has any data
to show.

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
5. Restart `python app.py`. The Dropbox picker (reachable at
   `/import/dropbox/list`) now lists CSV files from that folder with
   checkboxes — select the ones for this sync (main export + Vintage
   export together, per the sync-semantics rule above) and click "Hent
   valgte filer og synk".

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

### Price refresh (Vercel Cron)

Each card's `tcgplayer_price` (see `models.Card.display_price`) is normally
refreshed as a side effect of a Dex sync — but that means pricing only gets
fresher when a sync happens to run. `vercel.json` schedules a second,
independent cron job, `GET /cron/price-refresh` (`0 6 * * *`, one hour after
the Dropbox sync — edit `vercel.json` to change it), so pricing keeps moving
on its own schedule regardless of Dex sync frequency. It walks up to 100
cards oldest-priced (and never-priced) first per run — see
`price_refresh.py` — using the same `CRON_SECRET` auth pattern as
`/cron/dropbox-sync` (see that section above for setup) and writing its own
`card_snapshots` row (`source="price-cron"`) right after refreshing.

The underlying `pokemontcg.io` lookup (`card_images.fetch_card_data`, also
used for card images) does fuzzy name matching, so its top result isn't
guaranteed to be the exact card searched for. A returned card's name and
printed number must match exactly before its price is trusted — a
low-confidence match still yields an image (cosmetic, low stakes) but never
a price (would silently corrupt the Market Value KPI and value-growth
charts). Low-confidence matches are skipped and listed in the response's
`cards_low_confidence` (also surfaced as an import warning when triggered
via a Dex sync instead) — worth a manual look, not auto-corrected.

A confidently-matched card can still have more than one print (normal,
holofoil, reverse holofoil, 1st edition, ...), each with its own
`tcgplayer.prices` entry and potentially a very different market price.
`fetch_card_data` tries to match Dex's own `Variant` field to the right
print, but only for the unambiguous cases ("Normal", "Reverse Holo", "1st
Edition ...") — a bare "Holo" is intentionally left unmapped, since it
could mean any of several prints. When a card has multiple priced prints
and the variant can't be confidently matched, the first one present is
still used (better than no price) but flagged — listed in the response's
`cards_variant_uncertain` (or as an import warning via a Dex sync) — worth
a manual look, unlike a low-confidence match this still updates the price
rather than withholding it, since it's still the right card, just possibly
the wrong print.

### Value history

`collection_value_growth` (Transactions' "View charts" section, top chart) is an
*approximation*: it applies today's price retroactively to each card's
`created_at` month, because Dex gives no historical prices. `card_snapshots`
fixes that going forward — every sync writes one row per card (`qty` +
`reference_price` as of that day) right after it completes, so
`queries.real_value_history` can report what the collection was *actually*
worth on a given date, not an estimate. It's rendered as its own "Real value
history" chart, right below the approximation, using the same
unique/duplicates/total metric filter. It's empty until snapshots
accumulate (starts from whenever this table was added — there's no way to
backfill history for dates before it existed).

Up to two points per day from the Dex-sync side: the scheduled cron run
(`CardSnapshot.source="cron"`) and, separately, the latest off-schedule sync
that day (`source="manual"` — a manual Dropbox sync, or `/cron/dropbox-sync`
hit by hand with `?secret=` instead of the real Vercel cron header).
Re-running either one again the same day overwrites that same slot rather
than adding a third point. `/cron/price-refresh` (see "Price refresh" above)
writes its own independent slot the same way (`source="price-cron"` when
scheduled, `"manual"` when triggered by hand — note this can collide with a
same-day manual Dex sync's slot; a real day with both shows only the later
one's total under the shared "manual" label), so a day can have up to three
points if both cron jobs and a manual sync all land on it. There is no
manual CSV-upload page in the app (removed — the only sync entry points are
the Dropbox-based ones above and the price-refresh cron).

## Project layout

- `app.py` — FastAPI app, routes, entrypoint (`python app.py`).
- `db.py` — SQLite (local) / Postgres (Supabase, via `DATABASE_URL`) engine
  and session setup.
- `auth.py` — Supabase Auth login + JWT verification.
- `models.py` — SQLAlchemy models + computed properties.
- `constants.py` — the Dex category → binder/collection/priority mapping.
- `importer.py` — CSV parsing and sync logic.
- `queries.py` — dashboard aggregation queries.
- `snapshots.py` — writes daily `card_snapshots` rows (see "Value history").
- `price_refresh.py` — standalone TCGPlayer price refresh, decoupled from Dex
  sync (see "Price refresh" above).
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
  - `init_db()` gates its migration chain (`create_all()` →
    `_add_missing_columns()` → `_normalize_legacy_transaction_types()` →
    `_widen_card_snapshot_source_constraint()`) behind a single-row
    `schema_meta` table + `db.CURRENT_SCHEMA_VERSION` constant, so a
    serverless cold start against an already-migrated Supabase database
    does one `SELECT` and returns instead of a chain of round trips on
    every single request-serving process boot. Every migration function
    stays idempotent regardless — the version check is a fast path
    *around* the chain, not a replacement for it. A new migration is still
    added as a new function appended to the chain, gated by bumping
    `CURRENT_SCHEMA_VERSION`. If a schema/data change is ever made by hand
    against prod (see `HANDOFF.md`) instead of through this chain, also
    bump `schema_meta`'s stored version accordingly — otherwise this gate
    will skip a migration that should still run.
- Silent session refresh — an expired Supabase session redirects to
  `/login` instead of refreshing quietly in the background.
