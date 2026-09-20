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
  series, most valuable cards (scrollable list), by rarity.
- **Inventory** (`/inventory`) — full searchable/filterable/sortable card
  table. A qty == 0 card (traded/sold away, but still present in the latest
  Dex export — distinct from `flagged_missing_since`, which is a card absent
  from the export entirely) is hidden by default and shown dimmed with a
  small "0 owned" badge when the "Show cards I no longer own" toggle is
  checked (`?unowned=1`) — the search used to add a card to a sales listing
  (`/pokemon/search`) is a separate query and is unaffected, since re-buying
  a previously-traded-away card there is the intended path.
- **Transactions** (`/transactions`) — a purchase/sale log per card, plus a
  compact economic snapshot (net invested, current value, paper gain/loss)
  and a collapsible "Vis grafer" section with the value-growth and cash-flow
  charts (formerly the standalone Analyse page). Each order group has an
  "Edit order" link (`/transactions/purchase/{id}/edit`) for retyping,
  relinking a card, adding a note, deleting a row, adding a new card
  to the order (search below the table — created immediately against
  this order, defaulted to today/purchase/0 and editable in place, not
  deferred until Save), or moving/merging/splitting rows between orders
  by reassigning Order ID — all edits in a group commit atomically, and
  moving a row out of an order clears that row's agreed total/shipping
  rather than guessing how to split it (set the destination order's
  total/shipping afterward). Agreed total defaults to shipping + the
  cards already priced (price 0 = not priced yet) when nothing's been
  saved yet, but a saved value is a real number the user typed and is
  never silently recalculated back to the sum. A "Distribute remaining
  across unpriced cards" button (client-side, same pattern as the New
  Order cart's "Distribute evenly") fills `Agreed total − Shipping −
  Σ(already-priced cards)` evenly into the still-unpriced rows — useful
  for a lot where a few cards' values are known and the rest should
  absorb the remainder; rejects if every card is already priced or the
  result would be negative. A single ungrouped
  row can still be edited in place via its own quick-edit form, including
  its Order ID. The "+ New Order" cart's search box has a "Show cards
  without an order" toggle next to it — browses cards with no linked
  purchase transaction at all (both "Recently Added" and "Legacy import"
  cards, capped at 50 with a total count) instead of requiring a typed
  query, for picking cards to price straight into the order being built.
  Every free-text card-search box in the app (this one, Edit Order's
  add-card and per-row relink, Listing edit's card search) guards against
  Enter submitting the enclosing form instead of just searching — they all
  share a form with a "Register"/"Save changes" submit button and nothing
  before them to catch it otherwise. Every "+ Add to order" button that
  targets the open cart (`#cart-body`) — Recently Added's row button, the
  cart's own search results, its "Show cards without an order" results —
  alerts if no cart is actually open yet instead of silently doing nothing
  (`addCardToCart()` in `transactions.html`).
  The collapsed "Legacy import" table itself only lists cards that still
  have neither a date nor an order — a card drops off it the moment either
  gets set — and has its own bulk control: a checkbox per row (with a
  header checkbox to select all), an order picker populated from existing
  orders, and one "+ Add checked cards to order" button that adds every
  checked card directly into the chosen order without needing a cart open
  first (`POST /transactions/purchase/add-existing-cards`, same
  default-row creation as the Edit Order page's add-card above).
- **Sell on finn.no** (`/sales`) — check cards on Inventory (a new leading
  checkbox column, selection tracked client-side and cleared on refresh —
  see `static/sale-list.js`), click "Generate finn.no ad", then set
  quantity/condition/asking price per card and generate a copy-paste
  finn.no title + description (Norwegian ad copy — see "Sales listings"
  below). "Mark as listed" records the ad but never changes `qty`; a real
  sale is only ever recorded once the resulting `Listing` is marked sold
  from `/listings` (see below), which is the only listing action that
  writes `Transaction` rows.
- **Listings** (`/listings`) — overview of every recorded `Listing`: its
  card(s), status (active/delisted/sold), and prices side by side per card
  so a listing's margin is visible at a glance — cost
  (`queries.net_invested_by_card`, same figure used everywhere else),
  market price (`Card.display_price`), listed price
  (`Listing.suggested_price`), and — once marked sold — the real per-card
  sold price. Each not-yet-sold listing has a "Mark sold" action
  (`GET`/`POST /listings/{id}/mark-sold`) that requires confirming each
  card's actual sold price before creating anything — see "Sales listings
  (finn.no)" below for the full flow. Each active listing also has a
  "Remove listing" control (`POST /listings/{id}/delist`, htmx
  partial-swap, no confirm dialog) that sets its status to `"delisted"`;
  excludes delisted listings by default, with a "Show delisted" toggle to
  reveal them, and a separate "Sold only" toggle to narrow to just sold
  listings. Delisting never changes `qty`/`card_collections`/`binder_id`.
- **Activity Log** (`/releases`, merged with the former standalone Sync
  Log page, issue #159) — two stacked sections: **Sync Log** first (a
  read-only history of past syncs — daily cron or a manual Dropbox sync;
  there is no manual CSV-upload page, see "Dropbox import setup" below for
  the only way to sync outside the cron), then **Release Notes** (a small,
  hand-authored log of user-facing changes, newest first, capped to the 15
  most recent with older entries tucked into a "Show N older entries"
  toggle — see "Release Notes" below). `GET /import` redirects here
  (`#sync-log`, preserving any `lsort`/`ldir` query string) for old
  bookmarks/links.

## Data model

`cards`, `collections`, `card_collections` (many-to-many), `binders`,
`transactions`, `card_snapshots` (see "Value history" below),
`listings`/`listing_cards` (many-to-many, see "Sales listings (finn.no)"
below), `sets` (real Set entity, FK'd from `Card.set_id` — see
"Chronological sorting" below), plus `set_release_order` (the older lookup
table `sets` replaces — kept in place, unused going forward), `releases`
(see "Release Notes" below).

`duplicates`, `total_value`, and `unique_value` are **never stored** —
they're computed live (`Card.duplicates` / `Card.total_value` /
`Card.unique_value` in `models.py`, and the dashboard aggregates in
`queries.py`). A stored, independently-maintained `duplicates` value going
out of sync with `qty` was a real bug in the Excel version this app
replaces — the fix is to never store it at all. All three are gated on
`qty > 0`, so a card traded/sold away (qty == 0, but still present in the
latest export) contributes nothing to any "Value" KPI, breakdown bucket, or
`queries.top_valuable_cards` — `Card.unique_value` wasn't originally gated
this way (issue #132) even though `duplicates`/`total_value` always were.

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
   order, not alphabetically. `models.Set` (`series`, `name`, nullable
   `release_rank`, nullable `total_cards`) is a real entity, one row per
   distinct set, unique on `(series, name)` — `Card.set_id` is a nullable
   FK to it. `importer.py`'s Dex CSV sync path get-or-creates a `Set` row
   (via `db.py`'s shared `get_or_create_set()` helper) and links `Card.set_id`
   inline as it writes each card (issue #134), so a freshly-synced card is
   linked immediately, not just eventually — a set encountered for the
   first time gets a real, unranked `Set` row on the spot rather than being
   silently skipped. `db.py`'s `_backfill_sets()` (part of `init_db()`,
   re-run on every app startup, not just once) does the same get-or-create
   for every distinct `(series, set)` pair seen on `cards` and links every
   matching card's `set_id`; since #134 this is no longer the primary
   linking mechanism, just a catch-all/safety net for cards that predate
   that change or otherwise reached the database unlinked (nothing needs
   to be imported or seeded by hand for the link itself to exist either
   way). `release_rank` used to be
   null for most sets and only ever set by hand once a set's actual release
   date was researched (never guessed) — `set_sync.py` (issue #136,
   fast-follow to #133) is a deliberate, explicitly-approved change to that
   rule: it's a one-off/occasional script (`python set_sync.py`, same
   "run it locally or against Supabase via `DATABASE_URL`" shape as
   `seed_set_release_order.py`) that calls api.pokemontcg.io's `/v2/sets`
   (~166 sets, one call) and, for every `Set` row it can confidently match
   by (series, name) — see its module docstring for the matching
   strategy — writes `release_rank` as an ordinal rank over the API's own
   published `releaseDate` (earliest first) and `total_cards` from the
   API's per-set card count. "Never guessed" is satisfied a different way
   now: real published data instead of a manual estimate, not abandoned.
   A set the script can't confidently match — notably Japanese/Korean sets,
   which that API doesn't cover yet (e.g. this collection's own "Scarlet &
   Violet: 151 JP/KR") — is left exactly as it was (typically null) rather
   than guessed; `queries.sets_missing_release_rank()` lists which sets
   still have no `release_rank`, grouped with each one's own card count, so
   that gap stays visible instead of only showing up as a sort artifact.
   Safe to re-run any time; only ever touches rows it actually matches.
   Every release-order UI surface in the app — the Inventory table's
   default "release order" sort, the Dashboard's series breakdown,
   Inventory's collapsed KPI module, and the Transactions KPI module (all
   four via `queries.by_series_breakdown()`) — reads `Card.set_id ->
   Set.release_rank`; a card with no linked `Set` row, or a linked one with
   a null `release_rank`, sorts after every ranked set/series
   (`UNKNOWN_RELEASE_RANK` in app.py, mirrored as `queries._UNKNOWN_RELEASE_RANK`)
   rather than before, falling back to name/series/set order among
   themselves. `queries.unlinked_set_cards()` lists `(series, set)` pairs
   with cards that have no `set_id` linked yet, so drift (e.g. a card with
   a null `series`/`set` to begin with) is visible instead of only
   silently falling back.

   `set_release_order` (`Series`, `Set`, `release_rank`) is the older
   lookup table `Set` replaces — kept in the schema (nothing drops/renames
   tables, see "Database migrations" below) but no longer read anywhere in
   the app; `_backfill_sets()` only reads it once per `(series, set)` pair,
   to carry an existing `release_rank` row over onto the new matching `Set`
   row. Nothing should write to `set_release_order` going forward — edit
   `Set.release_rank` directly instead (e.g. via a script or a future admin
   UI; none exists yet).
7. **Sales listings (finn.no).** `Card.condition` is real per-card data
   (nullable, vocabulary in `constants.CARD_CONDITIONS`) — deliberately the
   same values as `apps/finn_ad_scraper/card_identifier.CONDITIONS`, kept
   in sync by convention since apps never import from each other. A
   generated ad the user marks "Mark as listed" (`/sales`) is recorded as a
   `Listing` row (+ `listing_cards`), not a flag on `Card` — the common case
   is a lot (several cards, one ad), which a per-card boolean/date can't
   represent without duplicating a date across every card in it. Marking a
   listing **never** changes `qty`, `card_collections`, or `binder_id` —
   listed is not sold; a real sale is only ever recorded as a `Transaction`.
   An active listing can be removed from `/listings` (`POST
   /listings/{id}/delist`, htmx, no confirm dialog), which sets `status =
   "delisted"` and, like marking listed, never touches `qty`,
   `card_collections`, `binder_id`, or Transactions — delisting only changes
   the `Listing` row's own status. `/listings` excludes delisted listings by
   default; a "Show delisted" toggle reveals them.

   A listing can also be edited (`GET`/`POST /listings/{id}/edit`) —
   `title`, `description`, `suggested_price`, and the card set
   (`listing_cards`) are all real CRUD against existing columns, no schema
   change. A "Regenerate ad text" action reruns `ads.build_listing` off the
   cards currently selected in the edit form (including not-yet-saved
   additions/removals), so title/description don't go stale relative to an
   edited card set. Separately, `POST /listings/{id}/delete` hard-deletes
   the `Listing` row (SQLAlchemy's ORM removes the matching `listing_cards`
   rows itself); the "Delete" control requires a client-side confirmation
   step first since, unlike delist, this is irreversible. Both actions keep
   the same invariant as delist: `qty`, `card_collections`, `binder_id`, and
   `Transaction` rows are never touched.

   **Marking a listing sold** (`GET`/`POST /listings/{id}/mark-sold`) is
   the one listing action that *does* write `Transaction` rows — a real,
   completed sale. `Transaction` is strictly per-card and its `price` is
   real cash flow every economic query (`economic_summary`,
   `cash_flow_by_month`, `net_invested_by_card`) sums directly and
   unconditionally, while `Listing` covers a lot at one lot-level
   `suggested_price` with no per-card price captured anywhere — so a lot of
   N cards sold together needs N `Transaction` rows, one per card, each
   with its own realized price and cost basis, not a single
   `sold_transaction_id` FK a `Listing` could point at instead. The
   relationship is `Transaction.listing_id` (many `Transaction` rows → one
   `Listing`), a plain nullable/additive FK.

   The mark-sold form reuses the purchase-cart UI/route pattern
   (`/transactions/purchase/start` + `.../add-row`) rather than a new cart
   UI, pre-filled with the listing's current cards and each row defaulted
   to `suggested_price / card_count` as an editable starting guess — never
   auto-submitted, since these become permanent cost-basis history the
   moment they're saved. Submitting creates one
   `Transaction(type="sale", listing_id=<listing>.id, purchase_id=<one
   fresh id shared by every row>, ...)` per card, then flips
   `Listing.status` to `"sold"` only after every row commits, all in one
   `db.commit()` — a failure partway (e.g. a missing/invalid price) leaves
   neither orphaned `Transaction` rows nor a `status` stuck between
   `"active"` and `"sold"`. Every card currently in the lot must get a
   price; nothing can be silently skipped or added beyond the lot's current
   card set. Re-running mark-sold against an already-`"sold"` listing is a
   no-op — no duplicate `Transaction` rows. Like every other listing
   action, mark-sold never touches `qty`, `card_collections`, or
   `binder_id`; qty is driven solely by the Dex CSV sync (`importer.py`),
   never by any `Transaction`, sale-linked or otherwise. `/listings` then
   shows each card's real sold price (via the `Transaction.listing_id`
   join) alongside cost/market/listed price, and a "Sold only" toggle
   narrows the page to just sold listings, alongside the existing "Show
   delisted" toggle.

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

## Release Notes

`/releases` (issue #144) is a small in-app log of user-facing changes,
backed by a `releases` table (`Release` in `models.py`), not a
`CHANGELOG.md` file and not something generated from git/PR history at
build/deploy time:

- **Not a file** — `db.py` already treats "is this host's filesystem
  writable" as a first-class distinction (its `DB_PATH.touch()` probe and
  fail-fast error). A file works fine for reading on Vercel (baked into the
  deploy), but an in-app authoring form could never write to it there —
  only locally — forcing prod authoring back through a git commit +
  redeploy, which is exactly the friction this feature removes for the rest
  of the app's data.
- **Not generated from git/PR history** — `templates`/`static` explicitly
  ship with no build step (see repo `CLAUDE.md`), and `api/index.py` is a
  bare re-export with no pipeline to hang generation off. Raw commit/PR
  history also mixes internal refactors with user-facing changes, so it'd
  need the same curation step anyway.
- **A DB table** fits the existing data-model pattern, uses the same
  additive-migration convention as everything else (`Base.metadata.create_all`
  + `CURRENT_SCHEMA_VERSION` bump in `db.py`), and behaves identically on
  local SQLite and prod Postgres.

`GET /releases` lists entries newest-first (by `date`, then `id`), with an
inline form at the top (`POST /releases`: date, title, body) to add one and
a "Delete" button per entry (`POST /releases/{id}/delete`) to remove a
mistaken one — there is no edit-in-place for v1; delete and re-add instead.
Both routes pass through the same `auth_guard` middleware as every other
non-public route — no separate admin check. `body` is rendered as plain,
Jinja-autoescaped text with `white-space: pre-wrap` (no Markdown parser) —
one owner writing a few sentences per entry doesn't justify a templating
dependency. Since issue #159, this section shares the page with Sync Log
(see "Pages" above) — only the 15 most recent entries render directly, with
anything older tucked into a collapsed "Show N older entries" `<details>`,
so an ever-growing hand-written list doesn't push Sync Log further down the
page over time.

## Dropbox import setup

Dropbox is how card data gets into the app at all — there is no manual
CSV-upload page (removed; nobody used it). This pulls CSV files directly
from a Dropbox folder (read-only: `files.metadata.read` +
`files.content.read`), via the daily cron (see "Automatic daily sync"
below) or a manual off-schedule run of that same endpoint
(`GET /cron/dropbox-sync?secret=...`), so setting this up is required
before the app has any data to show.

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
5. Restart `python app.py`. The daily cron (see "Automatic daily sync"
   below) picks up every CSV currently in that folder automatically — for
   an immediate off-schedule sync instead of waiting for it, hit
   `GET /cron/dropbox-sync?secret=<CRON_SECRET>` directly. There is no
   in-app file picker for this — `/import/dropbox/list` and
   `/import/dropbox/sync` are backend routes with no page pointing at them
   (the browser-based picker UI was removed, see HANDOFF.md #87).

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

### Image backfill (one-off, manual)

`Card.image_url` (used for the Dashboard's "Most valuable cards" #1-spot
thumbnail, see `templates/partials/kpi_module.html`) is only ever set
best-effort during a Dex sync (`importer.py`, capped at
`_MAX_IMAGE_LOOKUPS_PER_IMPORT` lookups per sync), so a card that missed its
budget slot or had no confident match on a given day can stay `NULL`
indefinitely — the normal sync never retries it. `backfill_images.py` is a
standalone script to retry those:

```bash
python backfill_images.py            # up to 200 lookups (default budget)
python backfill_images.py --limit 50 # smaller/larger pass
```

Same `DATABASE_URL` convention as `seed_set_release_order.py` — run it
locally against SQLite, or with `DATABASE_URL` set to the Supabase
connection string to backfill prod. Only ever fills a `NULL` `image_url`
in from a confident `card_images.fetch_card_data` match; cards with no
confident match are left `NULL` (never guessed — a wrong image is worse
than no image, see the 2026-09-16 `assets.tcgdex.net` incident in
`HANDOFF.md`). It's a manual, explicitly-triggered pass, not part of the
daily Dropbox/price crons.

### Value history

`card_snapshots` records real history going forward — every sync writes one
row per card (`qty` + `reference_price` as of that day) right after it
completes, so `queries.real_value_history` can report what the collection
was *actually* worth on a given date, not an estimate. It's rendered as the
**"Market Value" chart** on both Dashboard and Transactions' "View charts"
section, using the unique/duplicates/total metric filter, with a stat row
(Net invested / Current value / Gain-loss, `queries.economic_summary` +
`headline_summary`) built into the chart card itself (`chart_card`'s
`stats` param in `macros.html`) rather than off in a separate KPI tile.

Note: "Market Value" is also the name of an existing KPI tile
(`headline.total_value`, `kpi_module.html`) showing today's snapshot value —
the chart is that same number's history over time, not a different metric.
This was a deliberate naming choice, accepted despite the two elements
sharing a label on the same page.

Empty until snapshots accumulate (starts from whenever `card_snapshots` was
added — there's no way to backfill history for dates before it existed).
There is no longer an approximation chart (the old `collection_value_growth`,
which applied today's price retroactively to each card's `created_at`
month) rendered anywhere in the UI — the function itself is still in
`queries.py` and unit-tested, just unused by any route now.

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
- `backfill_images.py` — standalone, manually-triggered backfill for cards
  with a `NULL` `image_url` (see "Image backfill" above).
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
  - `_backfill_sets()` (see "Chronological sorting" above) is the one
    exception to that gate — it runs on every `init_db()` call regardless
    of `schema_meta`'s stored version. Since issue #134, `importer.py`
    links `Card.set_id` inline as it syncs, so this is no longer the
    primary mechanism keeping cards linked — it's an ongoing catch-all for
    any card that ends up unlinked some other way, not one-time
    schema/data cleanup like the rest of the chain.
- Silent session refresh — an expired Supabase session redirects to
  `/login` instead of refreshing quietly in the background.
