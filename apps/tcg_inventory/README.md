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

- **Dashboard** (`/`) — headline totals, collection breakdown
  (`queries.collection_membership_breakdown`: one row per collection with
  every card carrying its tag, Bulk, a deduplicated Collections row and a
  deduplicated Total row — collection rows can sum to more than Total, a
  multi-tagged card is marked "shared with N" in drilldown), by series
  (with completion, see below), most valuable cards (scrollable list), by
  rarity.
- **Overview KPI band** — a full-width card at the top of the KPI row on
  Dashboard, Inventory and Transactions, ordered by what a collector wants
  to know first:
  1. **Market Value** (widest): the duplicate-inclusive total as the hero,
     with Gain / loss (`queries.gain_summary`, kr and %, colored by sign) as
     a pill beside it. Gain is that same total minus Net invested — an
     equation row, "Total value − Paid = gain", spells it out — and a bar
     splits the total into unique vs duplicate value. (Transactions' "Paper
     gain/loss" uses the same total.) **This is the one gain definition app-wide**
     — every table's Gain/loss (`Bucket.gain_loss`) and Inventory's per-card
     Gain are total value − net invested too. A "No purchase price" line under
     the equation shows how many owned cards have no registered transaction and
     their value (`gain_summary`'s `no_cost_count`/`no_cost_value`), since that
     value lands in the gain in full.
  2. **Above / below cost** (formerly "Cards up / down"): an up-vs-down bar plus the best and worst card
     (with thumbnail), each card's value of all copies owned vs what it
     cost. Only owned cards with a registered transaction count
     (an unregistered card has no known cost); a ripped card counts as up by
     its full value.
  3. **Total Cards**: physical cards, then unique cards and duplicates.

  Sections sit side by side, go 1 + 2 on a tablet and stack on a phone. The
  remaining KPI cards (Price movers, Most valuable collection/series) share
  the row below it and grow to fill it (`.kpi-grid` is flex, not grid).
  **Price movers** (`queries.price_movers`) lists the owned cards whose price
  rose and fell the most in kr per copy, comparing the daily snapshot from
  30 days ago (or the earliest one, while history is shorter) with today's
  price. It says so in its caption ("price today vs. snapshot DATE"), and
  hides the % for moves under `queries.PRICE_MOVE_PCT_MIN_KR` (10 kr).
  The collection/series cards share one stat grid: Unique value |
  Unique Cards, Duplicate value | Total Duplicates, then Net invested and Gain.
- **Card page** (`/cards/{id}`) — one card: image, collections, binder,
  variant, language, prices, gain, every transaction (with order links) and
  its price history (`queries.card_price_history`, one point per snapshot
  day). Every card name in the app links here (`card_link` in
  `partials/macros.html`); Dex is a link on this page, and the photo viewer
  has a "Card details" button. Replaced name → Dex links (24.09.2026).
- **Collections** (`/collections`, `/collections/{id}`) — the membership
  table as an index, and a per-collection gallery grouped by set with value,
  duplicates, "shared" badges and completion (`queries.collection_detail`:
  per set, distinct numbers / `total_cards`; the collection total covers only
  known-size sets). `assign_bucket_investment` also fills
  `no_cost_count`/`no_cost_value` so every bucket-level Gain can say how much
  of it is cards with no purchase price.
- **Inventory** (`/inventory`) — full searchable/filterable/sortable card
  table, paginated 100 per page (`page`, `page_size=0` = all; sliced after
  sorting, so value sorts stay correct; sort links drop `page`). Collection
  filter has "Bulk / no collection" (`collection=__none__`), and a
  "Duplicates only" checkbox (`dup=1`). Column chooser
  (`static/inventory-columns.js`, localStorage); Classification/Location/
  Notes are omitted server-side when empty in the current result. A qty == 0 card (traded/sold away, but still present in the latest
  Dex export — distinct from `flagged_missing_since`, which is a card absent
  from the export entirely) is hidden by default and shown dimmed with a
  small "0 owned" badge when the "Show cards I no longer own" toggle is
  checked (`?unowned=1`) — the search used to add a card to a sales listing
  (`/pokemon/search`) is a separate query and is unaffected, since re-buying
  a previously-traded-away card there is the intended path.
- **Transactions** (`/transactions`) — laid out as **Order history first**,
  then individually-registered rows, then one card picker, then a
  collapsible "View charts" section with the value-growth and cash-flow
  charts (formerly the standalone Analyse page).

  **Order history** is the page's primary content, directly under the KPI
  cards: one row per order with the numbers that describe the deal — Order
  #, Date, Qty, Value (sum of recorded per-card prices, trades excluded),
  Shipping, Agreed total, Remaining, Platform. Remaining is a real column
  rather than a badge that only appears when nonzero, so "settled" (✓) and
  "no agreed total set yet" (—) are distinguishable at a glance instead of
  both rendering as nothing. Expanding a row reveals that order's cards,
  its agreed-total/shipping/platform form and an "+ Add cards to this
  order" button. Each order stays a `<details id="order-N">`: that's
  load-bearing, since `?open_order=N` deep-links by rendering `open` on it
  and the agreed-total form swaps that same element
  (`hx-select`/`hx-target="#order-N"` + `outerHTML`). Column alignment
  comes from a shared CSS grid on the header row and every `<summary>`
  (`.orders-row` in `style.css`) — keep those two in sync.

  **Trades.** A trade row (`type == "trade"`) carries a `direction`:
  `in` (a card you got) or `out` (a card you gave). It's set per card in
  the New Order cart when the order's type is Trade (an In/Out column
  appears), in Edit order, or in a row's quick-edit. On a trade row
  `price` is any cash that moved with the card — paid on `in`, received
  on `out` — and 0 when none did. An order with trade rows shows a
  **Gave / Got** block above its cards, with each side's value and the
  trade's gain (`queries.trade_summary`):
  `value got − value gave + cash received − cash paid`. "Today" uses each
  card's current `display_price`; the bracketed figure uses its price on
  the trade date (latest `card_snapshots` row on or before it,
  `queries.trade_prices_at`) and only appears when every card on both
  sides has one. Trade rows with no direction (recorded before the
  column existed) are listed as needing In/Out and left out of the
  totals rather than guessed. Trades, including their cash, stay out of
  Value, Net invested and paper gain/loss, same as before.

  **Ripped.** A card you pulled from a pack yourself is recorded as a
  `ripped` transaction: pick "Ripped (pulled myself)" as the New Order
  cart's type (the Price column disappears), or set a row's Type to Ripped
  in Edit order / quick-edit. A ripped row is always price 0 (the server
  forces it) and, like a trade, never counts toward an order's Value or
  Remaining, Net invested, or a card's Net paid — the pack cost isn't
  tracked. It does count as the card being accounted for, so ripped cards
  don't show up under "Show cards without an order", and Inventory marks
  them with a green "Ripped" badge. Edit order's Distribute skips ripped
  rows.

  Net invested and paper gain/loss render as a caption on the Order history
  header, not as a second KPI block. The page deliberately does **not**
  show a "Current value" figure: it was `headline.unique_value`, which the
  Market Value KPI card already shows as its "Unique value" row, sitting a
  few hundred pixels from that same card's duplicate-inclusive "Market
  Value" total — three names, two numbers, one screen.

  **Cards** (`partials/card_picker.html`) is the single card-picking table,
  merging what used to be two separate tables ("Recently Added" and a
  collapsed "Legacy import"). Both existed only to feed the same order, and
  they applied *different* inclusion rules — Recently Added listed every
  dated card whether or not it already had an order, Legacy only cards
  still missing one. In the merged table membership is "every card" and
  that distinction is an explicit `?pick=` filter instead: `unordered`
  (default — no purchase transaction yet), `recent` (has a known added
  date), `all`. Cards imported before added-date tracking show "no date"
  and sort to the bottom (`_sorted_rows` buckets null-key rows last — do
  not "fix" this with an `or datetime.min` default, which would scatter
  them through the list). An "Order" column names the order(s) a card is
  already on, so merging doesn't lose the "does this still need an order?"
  signal the old two-table split encoded positionally. One sort pair
  (`gsort`/`gdir`) covers the whole table; `usort`/`udir` are retired but
  still accepted so old links don't 422.

  Adding cards is selection-based: a checkbox per row, a header select-all,
  and a sticky action bar (shown only once something is selected) with an
  "Adding to" target — "New order" plus every existing order. That target
  is always a valid choice, which removes the old "click + Add to order
  with no cart open and nothing happens" dead end structurally rather than
  by wording its alert better. Picking an existing order is a plain POST to
  `/transactions/purchase/add-existing-cards`; picking "New order" opens
  the cart first and appends into `#cart-body` **in the swap callback**
  (`htmx.ajax` is async — appending on the next line no-ops), one request
  at a time, since concurrent appends to the same target drop rows. The
  selection survives sort/filter clicks via `sessionStorage`, the same way
  `/sales` does it — those links are full-page navigations and would
  otherwise silently discard a half-built selection.

  The page also guards the New Order cart against navigation
  (`beforeunload` whenever `#cart-body` has rows or a total/shipping has
  been typed): the cart is DOM-only until Register, so re-sorting the card
  table mid-order used to wipe every row and typed price with no warning.

  Each order group has an "Edit order" link
  (`/transactions/purchase/{id}/edit`) for retyping,
  relinking a card, adding a note, deleting a row, adding a new card
  to the order (search below the table — created immediately against
  this order, defaulted to today/purchase/0 and editable in place, not
  deferred until Save), or moving/merging/splitting rows between orders
  by reassigning Order ID — all edits in a group commit atomically, and
  moving a row out of an order clears that row's agreed total/shipping
  rather than guessing how to split it (set the destination order's
  total/shipping afterward). Save keeps you on the edit page with a
  "Saved ✓" note and a "← Back to Transactions" link (unless every row
  was moved out, which lands on Transactions); a failed save shows an
  error instead of silently doing nothing. Agreed total defaults to shipping + the
  cards already priced (price 0 = not priced yet) when nothing's been
  saved yet, but a saved value is a real number the user typed and is
  never silently recalculated back to the sum. A "Distribute remaining
  across unpriced cards" button (client-side, same pattern as the New
  Order cart's "Distribute evenly") fills `Agreed total − Shipping −
  Σ(already-priced cards)` into the still-unpriced rows — useful
  for a lot where a few cards' values are known and the rest should
  absorb the remainder. A "Split" choice picks **By market value** (each
  card's share is proportional to its current `display_price`; a card
  with no price gets the average share) or **Evenly**. Shares use
  largest-remainder rounding, so they always add up to the remainder to
  the øre and the order lands on ✓. Trade rows and rows ticked for
  deletion are skipped. It rejects if every card is already priced or the
  result would be negative. **Include shipping** (on by default) folds
  the order's shipping into that remainder too, i.e. it distributes
  `Agreed total − Σ(already-priced cards)`: in a lot, the cards you priced
  keep their price and the unpriced ones absorb the rest *including*
  shipping. It then sets Shipping to 0 in the form, since shipping now
  lives in those cards' prices and would otherwise be counted twice in Net
  invested. With it off, shipping stays recorded on the order, and each
  purchase row carries a share of it split by price (`queries.shipping_shares`; evenly when nothing in the
  order is priced yet). That share is shown under the row's price, and it
  counts in the card's Net paid (`net_invested_by_card`) and in Net
  invested, so a 25 kr card with 38 kr shipping shows as having cost 63 kr. A single ungrouped
  row can still be edited in place via its own quick-edit form, including
  its Order ID. The "+ New Order" cart's search box has a "Show cards
  without an order" toggle next to it — browses cards with no linked
  purchase transaction at all (capped at 50 with a total count) instead of
  requiring a typed query, for picking cards to price straight into the
  order being built.
  Every free-text card-search box in the app (this one, Edit Order's
  add-card and per-row relink, Listing edit's card search) guards against
  Enter submitting the enclosing form instead of just searching — they all
  share a form with a "Register"/"Save changes" submit button and nothing
  before them to catch it otherwise. The cart's own search results and its
  "Show cards without an order" results append via `addCardToCart()`, which
  alerts if no cart is open rather than silently doing nothing. (The Cards
  picker no longer needs that guard — its target is always an explicit
  choice — but those two callers still do.)
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
(see "Release Notes" below), and `master_cards`/`master_card_ids`
(masterdata, see below).

### Masterdata (card identity across catalogs)

There is no official per-card ID for Pokemon cards, and every catalog (Dex,
pokemontcg.io, TCGdex, TCGplayer, Cardmarket, Collectr, ...) uses its own.
`master_cards` holds one canonical identity per printed card + variant,
keyed on what's printed on the card: `(language, set_code, number,
variant)`. `master_card_ids` maps any number of external IDs onto that
identity, at most one per `source`, each with a `matched_by`
(`exact_id` / `derived` / `heuristic` / `manual`). A `manual` mapping is
never overwritten automatically, so that's how a wrong match gets fixed.

- The key is parsed from Dex's `card_id` (`sv2-109` → `int`/`sv2`/`109`,
  `jpn_sv2a-168` → `ja`/`sv2a`/`168`, `scn_csv9-79` → `zh-hans`/…) and
  Dex's Variant normalized to a fixed code (`Reverse Holo` →
  `reverse_holo`, `Poké Ball Holo` → `poke_ball_holo`). An unknown variant
  gets its slug as code instead of being dropped. Add it to
  `masterdata.VARIANT_LABELS` once it's confirmed.
- `Card.master_card_id` links a physical card to its identity. The
  importer links new cards inline, and `init_db()` backfills any card still
  unlinked on every start (`_backfill_master_cards`, same pattern as
  `_backfill_sets`). A `card_id` that can't be parsed stays unlinked.
- Seeded automatically: `dex` (the Dex ID) for every card, and
  `pokemontcg` (`derived`, same ID) for international prints. Other sources
  get added via `masterdata.set_external_id()` as those integrations are
  built. Price lookups don't read this table yet.
- `master_card_ids` is deliberately not unique on `(source, external_id)`:
  pokemontcg.io has one ID per print with variants inside it, so Normal and
  Reverse Holo of the same print share it.
- Identity only. Masterdata never touches `qty`, collections or binders. A
  `master_cards` row with no `Card` pointing at it is valid, which is what a
  future wishlist or set-completion view would build on.

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
   `Card.primary_collection` picks one by priority, highest first:
   1. Illustrator collections (Tomokazu Komiya, Shinji Kanda, Yuka Morii,
      Saya Tsuruta)
   2. Vintage Collection
   3. Collection (generic Dex folder)
   4. Scarlet & Violet: 151 JP/KR

   Any other/unknown collection name defaults to the lowest priority.
   `card_collections` itself always keeps every real tag. **Since Phase 1
   (24.09.2026) the dashboard no longer uses this for its collection rows**:
   crediting a multi-tagged card to one collection made it vanish from the
   others (Vintage showed 142 of its 151 cards), so each row now counts real
   membership and a separate deduplicated Total row does the "never
   double-counted" job instead. The ranking itself is unchanged.
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
   **Since 24.09.2026** it also runs monthly as Vercel Cron
   (`/cron/set-sync`, CRON_SECRET-gated) — until then it had never been run
   against prod, so every set's `total_cards` was null and Dashboard
   completion showed "unknown" everywhere. It now always writes
   `total_cards` but only fills `release_rank` where it's null
   (`--overwrite-ranks` for the old behaviour): prod's existing ranks are a
   different scale from the API's, and overwriting only the matched sets
   would interleave the two against the unmatched JP/KR sets. Completion
   (`Bucket.completion_pct`) counts distinct card numbers owned
   (`owned_numbers`), not rows, so variants/languages of one number count
   once; a series row aggregates its known-size sets (`series_completion`).
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

### Card images

`Card.image_url` is looked up by Dex's own `card_id`
(`card_images.fetch_image_by_card_id`), which works for far more cards than
the old name/set/number search:

- International prints: Dex's id *is* the Pokemon TCG API's id (`ex5-4`,
  `dv1-5`, …), so the card is fetched directly by id.
- Japanese prints (`jpn_<set>-<number>`): TCGdex's Japanese catalog
  (`api.tcgdex.net/v2/ja`), set code capitalized (`jpn_sv2a-168` →
  `SV2a-168`). The Pokemon TCG API has no Japanese cards, so its name search
  is never used for these (it could only return the wrong card).

No URL is ever built by hand: an image is only stored when the API returned
that card and its number (and, for international cards, a word of its name)
matches Dex's. Anything else stays `NULL`.

Filled in by `backfill_images.py`: most valuable owned cards first; a card
with no match is stamped `image_lookup_failed_at` and retried after 30 days
instead of blocking the queue. It runs:

- daily, after prices, inside `/cron/price-refresh` (up to 60 cards, 25 s);
- on demand via `GET /cron/image-backfill?secret=<CRON_SECRET>[&limit=N]`
  (time-boxed to ~50 s; call again while `remaining` > 0);
- by hand: `python backfill_images.py [--limit N]` (same `DATABASE_URL`
  convention as `seed_set_release_order.py`).

A Dex sync (`importer.py`) also tries the by-id lookup for new cards.

### Value history

`card_snapshots` records real history going forward — every sync writes one
row per card (`qty` + `reference_price` as of that day) right after it
completes, so `queries.real_value_history` can report what the collection
was *actually* worth on a given date, not an estimate. It's rendered as the
**"Market Value" chart** (`market_value_card` in `macros.html`, context from
`app._market_value_context`) on both Dashboard and Transactions' "View
charts" section, as a portfolio-style chart:

- **One point per day** on a real time axis — that day's last snapshot
  (`cron` → `price-cron` → `manual`), with today's point replaced by the
  live value so the line always ends on the key figures' Current value.
- **Metric** pills (`?metric=` unique / duplicates / total, default total)
  and **period** pills (`?period=` 1U / 1M / 3M / 6M / 1Å / Alt, default
  Alt); both are server-side links that keep every other query param, so a
  direct load with either param renders the same state.
- The y-axis is fitted to the period's min/max (not from 0); the period's
  change in kr and % is shown above the chart (green/red, first to last
  day shown), and the tooltip gives each day's value and change from the
  day before.
- **Price vs. card count**: under the period's change,
  `queries.value_change_breakdown` splits it into *Price development*
  (cards already owned at the start: copies × price change) and *More /
  Fewer cards* (copies added or removed since, at today's prices, plus the
  net card delta). The two add up exactly to the change. Only the first
  and last day's per-card rows are read. Days where the card count changed
  get an orange marker, and the tooltip shows the count and its delta
  ("no change — price only" otherwise).
- A dashed **Net invested** line (cumulative, `queries.net_invested_at_dates`)
  can be toggled on for Unique/Total — off by default, since showing it
  widens the y-axis.
- The stat row (Net invested / Current value / Gain-loss) follows the
  metric: Current value is today's unique / duplicate / total value.
  Gain-loss is only shown on Total (total value − Net invested, the one
  app-wide definition). Duplicates shows "–" for Net invested too:
  purchase cost is recorded per card, not per copy, so there's no honest
  split between a card's first copy and its extra copies.
- The change beside the value is first-to-last point of the chart, labelled
  "since first snapshot DATE" on All (it used to say "all time", which read
  as "since purchase").

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

Snapshots are stored per source (the chart then keeps each day's last one):
up to two per day from the Dex-sync side: the scheduled cron run
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
- `masterdata.py` — canonical card identity + external ID mapping (see
  "Masterdata" above).
- `snapshots.py` — writes daily `card_snapshots` rows (see "Value history").
- `price_refresh.py` — standalone TCGPlayer price refresh, decoupled from Dex
  sync (see "Price refresh" above).
- `backfill_images.py` — standalone, manually-triggered backfill for cards
  with a `NULL` `image_url` (see "Card images" above).
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
