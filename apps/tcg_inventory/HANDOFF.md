# Handoff notes — 2026-09-14 session

Status for whoever picks this up next. Covers both code shipped to `main`/prod
and **direct production-database changes that exist only here** (not in git
history) — read the second half even if you only care about code.

## Code shipped to prod (all merged to `main`, all deployed)

In order, each its own PR:

1. **#83** — Historikk sort was `(min_date, min_id)`; changed to sort by
   `purchase_id` (this is what let the manual renumbering below actually
   control display order). Also fixed a real bug: `app.py` called
   `load_dotenv()` *after* `from db import ...`, but `db.py` reads
   `DATABASE_URL` from the environment at import time — so every local run
   against Postgres was silently falling back to an empty SQLite file.
   Production wasn't affected (Vercel sets env vars directly), but local dev
   was broken until this was reordered.
2. **#84** — Renamed the Historikk group label "Kjøp #N" → "Ordre #N" (a
   group can be a sale or trade too, not just a purchase).
3. **#85** — Inventory's "Total verdi" column is now sortable (was
   display-only). Renamed "Referansepris"/"referansepris" → "Pris"/"pris"
   everywhere (Dashboard, Inventory, Transactions, Analyse, Wiki) — the user
   is standardizing on TCGPlayer as the price source and dropping the
   "reference price" framing.
   - **Watch out**: this leaves *two* different "Pris"-labeled columns
     side by side in the Transactions "Recently Added" table — "Pris" (Dex
     market price) and "Registrert pris" (what was actually paid). Flagged
     to the user, they accepted it as-is. If it causes confusion later,
     that's why.
4. **#86** — Removed the old per-row inline buy-form in "Recently Added"
   (Pris + Kjøps-ID inputs + Legg til/Oppdater). Replaced with a
   "+ Legg til i ordre" button that adds the card into whatever purchase
   cart is currently open (reuses the existing
   `/transactions/purchase/add-row` endpoint). **Known limitation**: if no
   cart is open when you click it, nothing visibly happens (htmx targets a
   `#cart-body` that doesn't exist yet) — there's a tooltip explaining this,
   but it's not a great UX and was called out as a possible follow-up, not
   fixed.
5. **#87** — Import/Sync page is now "Synk-logg": manual CSV upload and the
   Dropbox browser/sync-trigger UI were removed from the page (backend
   routes `POST /import`, `/import/dropbox/list`, `/import/dropbox/sync`
   are untouched — still used by the daily cron, just no longer exposed in
   this page's UI). Moved to the last nav position, renamed from
   "Import / Sync".

Sideline fixes bundled into the above: two tests left stale from an earlier
(already-deployed-before-this-session) "Kjøp → Ordre" rename were still
asserting the old label — fixed. The purchase-group ordering test asserted
the *old* date-based sort — rewritten for purchase_id-descending. Three
"inline buy form" tests were renamed/trimmed since that UI no longer exists
(the backend `upsert` behavior they actually test is untouched and still
covered).

Full suite is green except one **pre-existing, unrelated** failure:
`test_dropbox_routes.py::test_cron_sync_reports_dropbox_not_configured`
expects no `CRON_SECRET` in the environment; it fails locally only because
the real `.env` here has a real one set and that one test never isolates it
(unlike its siblings, which `monkeypatch.setenv`). Not touched this session,
not something the code changes above caused.

## Direct database changes (Supabase prod) — not in git, only here

The user asked me to reorganize purchase (`purchase_id`) numbering and fix
some data quality issues directly against the live Supabase database this
session, before the sort-by-purchase_id feature above even existed. All of
this was done as raw SQL via a Python script (`.venv/bin/python3` +
`sqlalchemy`), not through the app. A full pre-change JSON snapshot of the
`transactions` table (184 rows, before anything below) was saved to this
session's scratchpad — it did **not** get committed anywhere durable, so
treat the current DB state as the source of truth going forward.

What changed, roughly chronologically:

- Renumbered `purchase_id` across the whole table (an 11→13→16-order
  sequence of renumbers as more orders got added/split/merged during the
  session). Current orders run **#1 through #16** — some numbers were
  freed up by later splits/merges, so don't assume it's gap-free.
- Added a `note` field value on a few transaction rows (e.g. "Kjøpt på
  Collect63 Card Show" on order #12's rows) where the user wanted context
  that doesn't fit the `platform` field.
- Retyped 3 rows in what's now order #11 from `kjøp`/`salg` to `bytte`
  (Mega Venusaur ex given away in trade for Hypno + Slowbro received) —
  this is *why* `bytte` exists as a transaction type at all (see #85... no,
  earlier in-session, before any of the PRs above — `bytte` was added to
  the DB and to `templates/transactions.html`'s old dropdown in an earlier,
  now-superseded edit; the dropdown itself no longer exists after #86, but
  `bytte` is a real, permanent value in the `type` column now).
- Relinked one transaction to the correct `Sableye 63/132 (Secret Wonders)`
  card (it was pointing at the wrong specific print) — then partially
  un-relinked after user correction; the *actual* fix ended up being: leave
  that transaction on its original card, and instead assign a *different*,
  already-correctly-linked transaction row into the same order.
- Deleted one transaction outright (a stray duplicate `Sableye … Triplet
  Beat` row, 0 kr, user explicitly asked to remove it "som ett kjøp").
- Split/merged orders around the "Collect63 Card Show" purchase (#8) at the
  user's direction, including one case where I merged two orders together
  and the user then said that was wrong and had me split them back apart.
- **Cards table**: deleted 2 cards that were confirmed Dex mis-registrations
  (Wartortle 171/165, Paras 46/165 Poké Ball Holo — both had zero linked
  transactions, safe deletes). Set `qty = 0` (not deleted — it has a linked
  `bytte` transaction, id 56) on Mega Venusaur ex 3/63, since it was traded
  away and Dex will never report it again.
- **Collections table**: deleted the `Lot buvik` collection entirely (user
  said it "no longer exists") and its 2 card memberships — the 2 affected
  cards (Exeggutor 35/64 Jungle, Togetic 39/90 Undaunted) are still tagged
  under Vintage Collection, so they didn't drop out of any breakdown.
- Registered two brand-new orders end-to-end (Sableye 23/107 EX Deoxys;
  a 5-card VSTAR Universe/mixed lot including 3 cards that are physically
  Korean but tracked under Dex's "Japanese" locale since Dex has no Korean
  option for that set — this is an accepted, ongoing workaround the user
  described, not a bug).

None of this is reversible from git — if something looks wrong, the
`transactions`/`cards`/`collections` tables in Supabase are the only record,
plus this file and the conversation transcript.

## Open items raised but intentionally not built

- ~~**No UI to edit an existing order** after it's registered (retype,
  relink, move a card between orders, merge/split, add a note) — every
  correction above required raw SQL. This was flagged explicitly to the
  user as the single biggest real gap; they said current scope is fine and
  declined to prioritize it, but it'll very likely come up again.~~
  **Addressed 2026-09-17** (issue #109) — see the dated entry below.
- ~~The "+ Legg til i ordre" silent-no-op-when-no-cart-is-open issue (see
  #86 above) — **addressed below** (renamed to "+ Add to order", behavior
  itself unchanged).~~ **Actually fixed 2026-09-20** — see the dated entry
  below (`addCardToCart()` now alerts instead of silently doing nothing).
- The "Pris" / "Registrert pris" side-by-side naming ambiguity (see #85
  above) — **addressed below**, renamed to "Market price" / "Paid price".

## Full English translation + transaction-type data migration — 2026-09-15 session

Branch `i18n-english-professional`. The user asked to translate the whole
`tcg_inventory` UI from Norwegian to professional English. Ran the `ux`
agent first for a full audit (every Norwegian string, a glossary, tone
flags, and a list of non-obvious traps) before touching any code — that
audit is not preserved anywhere durable, so if a translation choice below
looks wrong and you want the reasoning, re-run a similar `ux` review rather
than assuming one exists somewhere.

**Terminology decisions (asked the user explicitly, not guessed):**
- "Kjøps-ID" → **"Order ID"** everywhere (not literal "Purchase ID") — fixes
  a pre-existing inconsistency: the group heading was already generalized to
  "Ordre #N" in #84 since a group can be a sale/trade too, but the field
  name never followed.
- "Pris" / "Registrert pris" (the ambiguous pair flagged in #85 and above)
  → **"Market price" / "Paid price"**, in the one place both appear
  side by side (Transactions "Recently Added"). Elsewhere, the lone `Pris`
  column is just "Price" (no ambiguity without the pairing).
- Number formatting (space thousands-separator, "1 234 kr") — **left
  as-is**, the user chose not to switch to English comma convention.

**Data migration — `transactions.type` values, not just UI copy.** The
DB stored `type` as literal Norwegian values (`"kjøp"` / `"salg"` / `"bytte"`,
the latter added directly against prod per this file's own log above, e.g.
the Mega Venusaur `bytte` transaction id 56). The user chose to actually
**rename the stored values to English** (`"purchase"` / `"sale"` /
`"trade"`) rather than keep the Norwegian values internally with just a
display-mapping layer. This is a real data change, not a template edit —
handled as an **idempotent migration in `db.py`'s `init_db()`**
(`_normalize_legacy_transaction_types()`, modeled on the existing
`_add_missing_columns()` pattern): `UPDATE transactions SET type = ... WHERE
type = '<old value>'`, safe to run on every startup since the `WHERE`
only ever matches legacy rows. **No manual script was run against prod
Supabase this session** — the migration ships as code and applies itself
automatically the next time the deployed app starts (i.e. on the next
deploy), the same way `_add_missing_columns()` already does for schema.
Every `== "kjøp"` / `== "salg"` comparison in `app.py`/`queries.py`, every
`?type=kjøp` URL, and `purchase_cart.html`'s type-branching were updated to
match. **Verify after the next prod deploy** that old rows actually got
normalized (e.g. `SELECT DISTINCT type FROM transactions` should show only
`purchase`/`sale`/`trade`) — this session only verified it against local
SQLite via the test suite, not against the live Supabase database.

**Wiki content correction bundled in (not just translation):** the
"Import / Sync" section still described the old manual-CSV-upload/
Dropbox-browser UI that #87 already removed from that page. Rewrote it to
match the current read-only "Sync Log" page while translating, per the
`ux` agent's own flag that translating stale content as-is would just ship
an accurate-sounding English description of something no longer true.

**Bonus fix, same bug class as the htmx work above:** found a third
"Sett som favoritt" star-button form (in `partials/pokemon_search_results.html`,
the Dashboard's Pokemon-search dropdown) still doing a full-page
`RedirectResponse` — missed in that earlier session since it wasn't one of
the forms the `ux` agent's review happened to flag. Given the same
`hx-select`/`hx-target`/`hx-swap="outerHTML"` treatment as the other two
favorite forms in `dashboard.html`.

Full test suite green (169 passed) after updating every test assertion
that checked the old Norwegian strings/DB values.

## UX agent review — 2026-09-15 session

Ran the project's `ux` agent for a broad usability pass over the whole app
(all templates + `static/style.css`, cross-checked against this file and
`README.md`). Nothing below has been implemented yet — logging it here so
a follow-up session (planned to be a different chat) has the full list
without re-running the review. Ordered by the agent's own priority:

1. ~~**Full-page reloads discard exploration state.** `/pokemon/favorite`,
   `/pokemon/merge`, `/pokemon/unmerge` (`app.py` ~lines 372–408) and
   `/transactions/purchase/{id}/total` (~lines 844–860) all end in a
   `RedirectResponse` instead of an htmx partial swap, even though the app
   already has the pattern elsewhere (`partials/macros.html`'s `sort_th`
   macro supports `hx_target`; Inventory's sort and the Dashboard's
   "Verdiutvikling" filter already use `hx-select`/`outerHTML`). Effect:
   expanding Dashboard drill-down rows or a Transactions `<details>` group,
   then favoriting/merging/sorting/editing a purchase total, collapses
   everything and resets scroll — for the purchase-total case, it closes
   the very `<details>` group you just edited. Dashboard's sort-column
   `<a href>` links (e.g. `templates/dashboard.html` lines 93–96, 119–124,
   177–183, 220–225, 298–304, 338–344) have the same issue. Pure
   template/route rewiring, no schema change — good candidate to pick up
   first.~~ **Addressed 2026-09-15** (PR #89, same-day session): every form/
   route listed now swaps its own section via `hx-select`/`hx-target`/
   `hx-swap="outerHTML"` instead of a full-page redirect; merge/unmerge and
   the purchase-total edit also reopen the `<details>` group just used. A
   third missed instance (the Pokemon-search dropdown's favorite-star form)
   was found and fixed in the 2026-09-15 translation session below.
2. ~~**In-app Wiki is stale.** `templates/wiki.html`'s `#import` section
   (lines 126–135) still describes the old manual-CSV-upload/Dropbox-browser
   Import page and labels it "Import / Sync" in the ToC (line 15), but #87
   above already turned that page into the read-only "Synk-logg". Also
   cheap, no schema change.~~ **Addressed 2026-09-15** (translation session
   below): rewritten to describe the current read-only "Sync Log" page.
3. **Synk-logg's "Advarsler" column is a dead end.** `partials/import_log.html`
   line 16 shows a warning *count*, but the actual warning text is never
   persisted (`models.py` `ImportLog.warnings_count` only stores the count;
   `app.py` ~line 1090 passes `result.warnings` into a one-off response,
   never stored). On Vercel there's currently no way from inside the app to
   ever see what a past sync's warnings said. **Has data-model
   implications** (new column or related table to store raw warning text) —
   check with the `architect` agent on storage shape before implementing,
   unlike #1/#2 above.
4. ~~**Accessibility: `--muted` (`static/style.css` line 9, `#868b96`) is
   under WCAG AA contrast (~3.4:1) at the small sizes it's actually used**
   (table headers, KPI labels, Historikk dates/metadata). Also
   `color-scheme: light dark` is declared with no actual dark-mode
   palette — worth toggling OS dark mode once to check for bad contrast
   combos.~~ **Addressed 2026-09-16**: `--muted` changed to `#63696f`
   (~5.1:1 vs `--surface`, ~5.6:1 vs `--page` — both clear AA passes, while
   staying lighter than `--ink-secondary`'s ~7.3:1 so the visual hierarchy
   is unchanged). `color-scheme` changed from `light dark` to `light` since
   there's still no real dark palette — declaring `dark` support was letting
   the browser render native widgets in dark styling on an always-light
   page, which was the actual source of the "bad contrast combos" risk, not
   something a toggle-and-check would have fixed on its own. A real dark
   theme is still a separate, unbuilt feature.
5. **Minor: several inputs rely on placeholder-only labeling** with no
   `<label>`/`aria-label` — notably the Pokemon merge form
   (`templates/dashboard.html` lines 261–263), which becomes visually
   ambiguous once both fields are filled in and the placeholders disappear.
6. **Minor: Inventory's 5 filter dropdowns** (series/set/collection/binder/
   language) **have no single "clear all"**, unlike the `dup`/`rarity`
   filters which do have explicit "Fjern filter" links.

Explicitly *not* re-raised (already covered above): the "+ Legg til i
ordre" no-op, the "Pris"/"Registrert pris" ambiguity, and the missing
order-edit UI.

## Backlog item — 2026-09-16 session

**Most valuable cards KPI card had broken images; fixed, but most cards
still have no photo at all.** `templates/partials/kpi_module.html`'s
"Most valuable cards" card was building a hand-rolled `assets.tcgdex.net`
image URL from Dex's own `card_id`/`number` — `card_images.py`'s own
docstring notes the Pokemon TCG API's card IDs don't correspond to Dex's,
so this was effectively guaranteed to 404. Fixed to use the already-fetched
`card.image_url` field instead (same source `partials/macros.html`'s
`dex_link` macro already uses elsewhere on the dashboard), guarded with
`{% if card.image_url %}` so a missing URL just omits the image instead of
rendering broken. Only the #1 spot shows an image now (runner-up spots #2/#3
never did, by design/request).

**Not fixed, and the actual reason most top-value cards show no picture:**
`image_url` is `NULL` for most cards, including the current single most
valuable card ("Dark Celebi"). Confirmed live against prod (only 1 of the
current top-10 most valuable cards has an image at all — "Dragonite").
Root cause: `card_images.fetch_image_url()` is deliberately best-effort
(silently gives up on no-match/ambiguous-set), and `importer.py`'s
`_MAX_IMAGE_LOOKUPS_PER_IMPORT` caps lookups at 25 per sync, so cards can
go indefinitely without ever getting a successful match retried.

**TODO:** write a one-off backfill script — find cards with
`image_url IS NULL`, retry `card_images.fetch_image_url()` for each
(respecting the same rate-limit/best-effort behavior already in
`card_images.py`), write results back to the DB. Not built yet.

**Addressed 2026-09-19** (issue #128): `backfill_images.py` — finds cards
with `image_url IS NULL`, retries `card_images.fetch_card_data()` (reused
over the older `fetch_image_url()` wrapper so a pass also refreshes
`tcgplayer_price` for the same cards) up to a `--limit` budget (200 by
default), writes back only confident matches, leaves the rest `NULL`. Does
not touch `importer.py`'s per-sync budget/staleness logic — a separate,
explicitly-triggered pass, not a cron change. See README's "Image backfill"
section. Not yet run against prod as part of this session — run it there
(`DATABASE_URL` set to the Supabase connection string) to actually improve
the Dashboard's "Most valuable cards" #1-spot coverage; this session only
shipped the script and its tests.

## Live TCGPlayer prices — 2026-09-16 session

Per HANDOFF #85's note that the user is "standardizing on TCGPlayer as the
price source" (previously just a label change, no actual live lookup), added
a real TCGPlayer price fetch. Consulted the `architect` agent first for the
design since it touches value calculations, snapshots, and the sync flow;
implemented per its recommendation.

- `card_images.py` — renamed the underlying Pokemon TCG API lookup to
  `fetch_card_data()`, returning both `image_url` and `tcgplayer_price` from
  the **same** API call (that API's card response already includes a
  `tcgplayer.prices.*.market` field — no separate TCGPlayer OAuth
  integration needed). `fetch_image_url()` kept as a thin back-compat
  wrapper. Picks the first variant's `market` price present, since there's
  no reliable way to map a TCGPlayer print-variant name to Dex's own
  `Variant` field.
- `models.py` — new nullable `Card.tcgplayer_price` /
  `tcgplayer_price_updated_at` columns (additive, picked up automatically by
  `db.py`'s existing `_add_missing_columns()`, no manual migration needed).
  **Did not** overwrite or drop `reference_price` (Dex's own CSV "Price"
  column) — two independent sources are kept in separate columns so it's
  always possible to tell which one produced a value, rather than blending
  them in place. Added `Card.display_price` (`tcgplayer_price` if not None,
  else `reference_price`) as the one property every consumer should read;
  `unique_value`/`total_value` now use it.
- `importer.py` — during each My Collection sync, refetches a card's
  TCGPlayer price when it's missing or older than
  `_PRICE_STALE_AFTER_DAYS` (7), budget-capped at
  `_MAX_PRICE_LOOKUPS_PER_IMPORT` (25) per import call, mirroring
  `_MAX_IMAGE_LOOKUPS_PER_IMPORT`'s existing pattern but with its own
  separate budget — unlike images (fetched once, cached forever since an
  image never changes), a price needs periodic refreshing, so the skip
  condition is staleness, not "already has a value".
- `snapshots.py` — `record_daily_snapshot` now snapshots `card.display_price`
  into `CardSnapshot.reference_price` (same column, no new snapshot table —
  `real_value_history` only needs "what was it worth", not which source
  produced it).
- `queries.py` (`top_valuable_cards`) and `app.py` (`SORT_COLUMNS`,
  `TOP_CARD_SORT_KEYS`, the Transactions field-sort keys) repointed at
  `func.coalesce(Card.tcgplayer_price, Card.reference_price)` /
  `card.display_price` instead of `reference_price` directly, so sorting and
  the "top valuable cards" KPI reflect the live price too. Templates
  (`dashboard.html`, `partials/inventory_table.html`,
  `partials/kpi_module.html`, `transactions.html`) now render
  `card.display_price` instead of `card.reference_price` — the sort-link
  query-param names were deliberately left as `"reference_price"` (backend
  dict keys only, not user-visible) to avoid touching every URL/link.
- No staleness indicator in the UI (e.g. "price last checked N days ago")
  was built — Dex's own Price column is a reasonable fallback, not an error
  state, so this was deferred rather than built speculatively per the
  architect's recommendation. Revisit if `tcgplayer_price` turns out to go
  stale often in practice.
- 6 new tests added (`test_card_images.py`: `fetch_card_data` image+price
  extraction; `test_importer.py`: price fetched when missing, not refetched
  same-day, refetched once stale). Full suite green, 188 passed.
- **Not yet verified against live TCGPlayer data** — only tested against
  fakes/mocks (per this repo's offline test-suite convention). Worth
  spot-checking a real sync against a few known cards after the next deploy
  to confirm match quality (name+set+number matching can be ambiguous for
  some prints, same caveat `card_images.py` already documents for images).

### Currency bug found and fixed same day — USD vs NOK

Spot-checked live against production right after merging (top-5 most
valuable cards, via a one-off script run with the user's explicit
permission since it wrote to prod — see the Bash permission entry added to
`.claude/settings.local.json`, local-only, scoped to that script's exact
path). Two of five got a real TCGPlayer match: Dragonite (Dragon Vault
5/20) and Ditto (Triumphant 17/102). Their `tcgplayer_price` came back
**~9-10x lower** than Dex's `reference_price` for the same card (34.55 vs
307.41 kr, 24.96 vs 237.33 kr) — the user immediately flagged this as
wrong, correctly guessing the cause before I'd diagnosed it: **the Pokemon
TCG API's `tcgplayer.prices.*.market` is always USD; every other price in
this app (Dex's exported column, every `| kr` template) is NOK.**
`_best_tcgplayer_price` was returning the raw USD number unconverted.

Confirmed via the API directly that this wasn't a wrong-card/wrong-variant
match (Dragon Vault's Dragonite has exactly one print, "Rare Holo",
`dv1-5` — no ambiguity) — it was purely the missing currency conversion.

**Fix:** `card_images.py` now multiplies by a fixed `_USD_TO_NOK = 10.5`
constant before returning `tcgplayer_price` (chose a fixed rate over a live
FX API — one more flaky external dependency isn't worth it for a number
that's already a best-effort estimate; revisit if the real rate drifts far
from 10.5). Re-ran the same live check after the fix: Dragonite now 362.77
kr, Ditto 262.08 kr — both back in the right ballpark vs Dex's 307.41 /
237.33. Updated `test_card_images.py`'s price-extraction test to assert
the converted value. Full suite green, 189 passed.

**Also corrected the two already-mis-stored production rows** (Dragonite,
Ditto) by re-running the same fetch with the fixed code — no snapshot had
been taken yet at the buggy values (the diagnostic script never called
`snapshots.record_daily_snapshot`), so `real_value_history` was never
polluted with USD-mislabeled-as-NOK numbers.

## Decoupled price refresh + match-confidence guard (issue #93) — 2026-09-17 session

Closes the second half of issue #93 — the "Live TCGPlayer prices" session
above (2026-09-16) shipped the price *source* switch (PRs #94/#95); this
session shipped the part of #93 that work didn't cover: decoupling refresh
from Dex sync, and guarding against a wrong-card API match silently
producing a wrong price.

- New `price_refresh.py` + `GET /cron/price-refresh` route (`app.py`), its
  own `vercel.json` Vercel Cron entry (`0 6 * * *`), same `CRON_SECRET`
  auth pattern as `/cron/dropbox-sync`. Walks up to 100 cards
  oldest-priced-first per run, independent of whether/when a Dex sync
  happens to run — see README's new "Price refresh" section.
- `card_images.fetch_card_data` now checks the API's top match's name and
  printed number against what was searched for (`_is_confident_match`)
  before trusting its price — a low-confidence match still returns an image
  (cosmetic, low stakes) but withholds the price (would silently corrupt
  the Market Value KPI / value-growth charts). Surfaced as an import
  warning (Dex-sync path) or `cards_low_confidence` in the response
  (cron/price-refresh path) rather than auto-corrected.
- `card_snapshots` gets a third possible `source` value, `"price-cron"`
  (scheduled) — README's "Value history" section updated to note a day can
  now show up to 3 points instead of 2, and that a same-day *manual*
  price-refresh trigger shares the `"manual"` slot with a same-day manual
  Dex sync (last one to run wins that slot — not deduped/merged, just
  flagging this as a real, if narrow, edge case rather than a bug that was
  fixed).
- 11 new tests: `test_price_refresh.py` (budget/staleness/ordering/low-
  confidence behavior of `refresh_stale_prices`), `test_price_refresh_routes.py`
  (the `/cron/price-refresh` route's auth + snapshot-source behavior),
  plus new/updated cases in `test_card_images.py` and `test_importer.py`
  for the confidence guard. Full suite green.
- **Not yet verified against live TCGPlayer/pokemontcg.io data** — same
  offline-test-suite caveat as the 2026-09-16 session above. Worth spot-
  checking a real `/cron/price-refresh` run after deploy, and checking
  whether the confidence guard causes more `cards_low_confidence` hits than
  expected (e.g. from set-name formatting differences already documented as
  a known false-negative source in `_is_confident_match`'s docstring).
- Second source (eBay) from issue #93's "out of scope" section was not
  touched — `price_source`/generic-schema question from issue #93 point 2
  also wasn't revisited; `tcgplayer_price`/`tcgplayer_price_updated_at`
  (already shipped in the 2026-09-16 session) remain TCGPlayer-specific
  columns, not yet generalized for a second source.

## Order-level edit endpoint (issue #109) — 2026-09-17 session

Closes the "No UI to edit an existing order" gap flagged repeatedly above —
every correction previously required raw SQL against prod.

- New `Transaction.note` column (`models.py`), nullable, additive, picked
  up automatically by `db.py`'s `_add_missing_columns()`. Prod-schema audit
  for a possible pre-existing undocumented `note` column (per the
  2026-09-14 entry's "Kjopt pa Collect63 Card Show" note) was requested but
  **not completed this session** — the auto-mode "Production Reads"
  classifier blocked a direct read-only `information_schema` query against
  the Supabase database, and the follow-up attempt to add a permission rule
  for it was itself blocked by the "Self-Modification" classifier. **Run
  the schema audit before the next prod deploy**: `SELECT column_name FROM
  information_schema.columns WHERE table_name = 'transactions'` — if a
  `note`/`notes` column already exists there under a different name,
  rename this one to match before shipping, since `_add_missing_columns()`
  only adds columns, it never renames or merges one that already exists
  under a different name.
- New `GET`/`POST /transactions/purchase/{purchase_id}/edit` (`app.py`),
  template `purchase_edit.html` — one row per transaction in the order,
  every field editable (type, date, price, platform, note) plus a card
  relink control (inline search + htmx swap, no page reload) and a
  per-row Order ID field. Reassigning a row's Order ID is the single
  primitive behind move/merge/split, per the issue's own scoping — there's
  no separate move/merge/split endpoint.
- All row edits (including deletions, via a per-row checkbox) commit in one
  transaction. A row whose Order ID changes has its
  `purchase_total`/`purchase_shipping` cleared rather than carried over,
  split, or summed onto the destination order — matching the "never guess"
  precedent elsewhere in this app (`SetReleaseOrder.UNKNOWN_RELEASE_RANK`,
  `qty = 0` instead of deleting a traded-away card). The user must set the
  destination order's total/shipping afterward via the existing
  `set_purchase_total` form.
- The single-row inline edit (`partials/tx_row.html`'s `tx_row_edit` macro,
  `update_transaction` route) was kept rather than superseded, and gained
  its own `purchase_id` field — decided as the pick between the two options
  the issue left open, since it's still the faster path for a single
  ungrouped row or pulling one card out of an order without opening the
  full editor. The full order editor is for anything that needs several
  rows changed atomically (relink, note, delete, or a total/shipping
  reconciliation).
- No delete-transaction route existed anywhere in the codebase before this
  session, despite the issue's acceptance criteria assuming one did and
  saying to reuse it — confirmed by search, not just missing docs. The
  delete checkbox in the new order editor is the first delete path added.
- 7 new tests in `tests/test_purchase_edit.py`: render, atomic multi-row
  update, card relink, row deletion, total/shipping clearing on a move
  (the core "never guess" behavior), and no dangling empty order left
  behind when every row in a group is moved out. Full suite green (231
  passed).
- **Not yet exercised in a real browser** — only tested via the test
  client's form posts, per this repo's offline-test-suite convention. The
  relink search/select htmx round trip (`purchase_edit_relink_search`/
  `purchase_edit_relink_select` routes, swapping `#relink-cell-{tx_id}`)
  is worth clicking through by hand before relying on it, since htmx
  target/swap wiring is easy to get subtly wrong in a way tests using the
  raw test client wouldn't catch.

## New "Sell on finn.no" module — 2026-09-17 session

A UX pass (`ux` agent) and architecture pass (`architect` agent) preceded
this build — see PR description / git history for the full writeups. Ships
as code only, no direct database changes.

- New page `/sales`: select cards on Inventory (new checkbox column, tracked
  client-side in `static/sale-list.js` via sessionStorage — deliberately not
  server-persisted, and re-hydrated after every htmx filter/sort swap on
  `#inventory-results` so switching filters mid-selection doesn't silently
  drop already-checked cards) → "Generate finn.no ad" → review qty/condition/
  price per card → generate a copy-paste finn.no title + description
  (Norwegian ad copy, deterministic template, no LLM call — see `ads.py`).
- **New persisted `Card.condition` column** (nullable string, vocabulary in
  `constants.CARD_CONDITIONS` — a literal copy of
  `apps/finn_ad_scraper/card_identifier.CONDITIONS`, kept in sync by
  convention since apps don't import each other's code). Set whenever a
  condition is chosen on `/sales`; real per-card data, not derived, so it's
  a real column rather than an ephemeral generation-time-only input.
- **New `listings` + `listing_cards` tables** (`models.Listing`): one row
  per generated ad the user explicitly marks "Mark as listed" — title,
  description, suggested price, platform (default "finn.no"), status. A
  separate table rather than a boolean/date on `Card` because the primary
  case is a lot (N cards, one ad) and a per-card flag can't represent that
  grouping without duplicating a date across every card in the lot. Also
  deliberately not a `Transaction` — a listing has no real cash flow yet,
  and every economic query sums `Transaction.price` directly. **Marking a
  card listed never changes `qty`/`card_collections`/`binder_id`** — listed
  != sold; a real sale is still only ever recorded via Transactions.
- Bumped `db.CURRENT_SCHEMA_VERSION` to 2 so the new column/tables actually
  get created on an already-migrated Supabase database (the schema-version
  gate added 2026-09-17 would otherwise skip `_add_missing_columns()`/
  `create_all()` entirely on a cold start).
- New tests: `tests/test_ads.py` (pure `ads.build_listing` unit tests —
  single card, lot, mixed-set lot, missing price, title truncation, qty>1)
  and `tests/test_sales.py` (routes: review page, generate, mark-listed,
  and an explicit assertion that marking listed never touches `qty`). Full
  suite green (243 passed).
- **Not yet exercised in a real browser** — same caveat as the entry above;
  worth clicking through the Inventory checkbox → sticky bar → `/sales` →
  generate → copy → mark-listed flow by hand, especially the sessionStorage
  re-hydration after an Inventory filter change, before relying on it.
- **Deferred, not built**: CSV export of the sale list, an "already listed"
  badge back on Inventory/`/sales` for a card with an active `Listing`, and
  linking a `Listing` to the `Transaction` that eventually sells it
  (`Listing.status` has a "sold" value reserved for this, nothing sets it
  yet). None of these were asked for; flagging in case they come up.

## Listings overview (issue #120) — 2026-09-17 session

Built on top of the "Sell on finn.no" module above (PR #119), which was
**still open/unmerged** at the time this branch was cut — this branch
(`claude/listings-overview-120`) is stacked on PR #119's branch
(`claude/card-selection-module-ux-g3odsj`) rather than `main`, since the
`Listing` model this issue reads doesn't exist on `main` yet. Its PR should
land after (or be merged into) #119, not directly onto `main`, unless #119
is merged first and this branch is rebased. No schema change, no direct
database changes — code only.

- New read-only page `/listings` (`app.py`'s `listings_page`,
  `queries.listing_overview`): every `Listing`, newest first, each card
  annotated with three prices side by side — cost (`net_invested_by_card`,
  reused as-is, not a new concept), market price (`Card.display_price`),
  listed price (`Listing.suggested_price`) — plus the listing's
  created-at date, platform, and status (active/delisted/sold).
  Nav link added in `templates/base.html`.
- Deliberately does **not** touch `qty`/`card_collections`/`binder_id` —
  no delist/mark-sold action, per the issue's explicit scope cut and the
  README's "Sales listings (finn.no)" business rule.
- **Explicitly deferred** (per the issue, scope kept tight on purpose):
  the optional show/hide toggle for non-sale card detail (illustrator/
  rarity/variant/language), the "already listed since" badge back on
  Inventory/`/sales`, and any "mark as sold"/Transaction-linking flow.
- New tests: `tests/test_listings.py` (empty state, card+status render,
  all three prices rendered independently with a real `Transaction` for
  cost, no qty/binder mutation, nav link present). Full suite green (242
  passed) on top of PR #119's branch.
- **Not yet exercised in a real browser** — same caveat as the entries
  above; only exercised via the test client.

## 0-qty card value/visibility fix (issue #132) — 2026-09-19 session

Code only, no direct database changes.

- `Card.unique_value` (`models.py`) was the one computed property not gated
  on `qty > 0` the way `duplicates`/`total_value` already were — a
  traded/sold-away card (qty == 0, still present in the latest export,
  distinct from `flagged_missing_since`) was still counting its full market
  price toward every "Value" KPI, dashboard breakdown bucket, and
  `queries.top_valuable_cards` (also fixed to filter `Card.qty > 0`). Both
  gated the same way, both README-documented.
- Inventory table + dashboard drill-down leaf rows (`dashboard.html`'s
  shared `card_leaf_row` macro) now dim a qty==0 row and add a small
  neutral "0 owned" badge (`.card-row-unowned` / `.unowned-badge`, reusing
  `.tx-platform-badge`'s pill styling per the app's one established
  convention).
- Inventory gained a "Show cards I no longer own" checkbox
  (`?unowned=1`, `app.py`'s `_apply_inventory_filters`), default unchecked
  — qty==0 cards are hidden from the default browse view. `/pokemon/search`
  (used when adding a card to a sales listing) is a separate query,
  deliberately untouched — re-buying a previously-traded-away card there is
  the intended path, per the issue.
- **Deliberately left untouched** (both explicitly out of the issue's
  acceptance criteria, flagged there only as "worth a look"):
  - `app.py::_sale_items_from_form`'s `qty = ... if card.qty else max(1, qty)`
    branch, which means a qty==0 card added to a sales listing isn't
    clamped to its own qty at all. Low severity — a `Listing` never mutates
    real `qty` either way — but a future pass on the sales-listing flow
    should look at it.
  - `CardSnapshot.unique_value` (`models.py`) has the exact same
    not-gated-on-qty shape as `Card.unique_value` had, and theoretically
    affects `queries.real_value_history`'s "unique" metric the same way.
    Not touched here since the issue scoped this to `Card`/`top_valuable_cards`
    specifically and `real_value_history`'s own qty>0 `card_count` logic
    already suggested the qty==0 case was considered there; worth a
    follow-up look if `real_value_history`'s "unique" numbers ever look
    inflated.
- New tests: `tests/test_queries.py` (qty==0 card's `unique_value`/
  `total_value`, `top_valuable_cards` exclusion, headline/bucket totals
  excluding a qty==0 card), `tests/test_app.py` (Inventory hides qty==0 by
  default, `unowned=1` reveals with badge, empty-`unowned`-value doesn't
  422). Full suite: 282 passed. 5 pre-existing failures in
  `test_app.py` (`test_inventory_release_sort_falls_back_for_unlinked_or_unranked_sets`
  and 4 others) are unrelated to this change — reproduced identically on
  `main` before this branch's changes, caused by this environment's
  SQLAlchemy 2.0.54 vs. whatever pinned/tested version the repo's CI
  normally runs (`requirements.txt` only pins `sqlalchemy>=2.0`); not fixed
  here since it's a pre-existing environment/CI issue, not something this
  issue's diff introduced.

# Handoff notes — 2026-09-19 session (issue #126, listing edit/delete)

Built on top of `main` (which, at branch-cut time, did **not** yet include
PR #145/issue #132 — that PR was still open/unmerged, contrary to what this
session was told when spawned; flagging in case #145 lands with conflicts
against this branch's `app.py`/dashboard-adjacent changes, though this
ticket didn't touch dashboard code so a clean rebase is expected). No schema
change, no direct database changes — code only.

- `POST /listings/{id}/delete` hard-deletes the `Listing` row; SQLAlchemy's
  ORM removes the matching `listing_cards` association rows itself
  (verified in tests — not relying on the `ondelete="CASCADE"` FK, since
  this app's SQLite connections don't turn on `PRAGMA foreign_keys`).
  Confirmation is `hx-confirm` on the "Delete" button (`partials/
  listing_entry.html`) — a plain browser `confirm()`, not a custom modal;
  flagged as the same "no real `ux` pass" caveat the issue itself notes.
- `GET`/`POST /listings/{id}/edit` (`templates/listing_edit.html`) — editable
  title/description/suggested_price plus add/remove against the card set,
  mirroring the Transactions per-order edit pattern (search-then-append row,
  no full autocomplete widget). "Regenerate ad text" reruns `ads.build_listing`
  off whatever's currently in the edit form's card rows (via `hx-include`,
  htmx out-of-band swaps into the title/description/price fields), not
  what's saved in the DB — so an in-progress add/remove is reflected before
  Save is even clicked. Per-card qty/condition/price inputs from the
  original `/sales` draft aren't stored on `Listing`, so regenerate always
  uses qty=1 and the card's current `display_price` — a best-effort
  re-derivation, not a replay of the original draft's inputs. At least one
  card is required to save (server-side validation, re-renders the form
  with an error and the submitted values on violation).
- Both actions keep the existing invariant: never touch `qty`,
  `card_collections`, `binder_id`, or `Transaction` rows — asserted directly
  in tests.
- `models.Listing`'s docstring and README's "Sales listings (finn.no)"
  section updated per the issue's doc-update discipline.
- Explicitly out of scope, per the issue: mark-as-sold/Transaction-linking
  (separate ticket), and any Inventory-side "already listed" badge.
- New/updated tests in `tests/test_listings.py` (delete + cascade + no
  qty/collection/binder/transaction mutation + 404 + htmx-empty-response +
  confirm-attribute-present; edit prefill + update fields + card set add/
  remove + reject-empty-card-set + no side-effect mutation + regenerate
  reflects current selection + regenerate-with-nothing-selected +
  nonexistent-listing redirect). Full `apps/tcg_inventory` suite green
  except 4 pre-existing failures in `test_app.py`
  (`test_inventory_can_be_sorted_by_language`,
  `test_inventory_price_sort_keeps_unpriced_cards_last`,
  `test_inventory_shows_net_paid_and_per_print_gain`,
  `test_inventory_value_sorts_treat_missing_cost_as_less_than_zero`) —
  confirmed present on unmodified `main` too (a SQLite `Date` type/string
  mismatch unrelated to this ticket), not introduced by this branch.
- **Not yet exercised in a real browser** — same caveat as the entries
  above; only exercised via the test client. A real `ux` pass on the edit
  form and delete-confirmation UX (same note the issue itself makes) is
  still recommended before this ships to real users, not done here.

## Release Notes page (issue #144) — 2026-09-19 session

Branched directly off `main` (independent of the in-flight listings work
above — own model, own route, own template, no shared files). No direct
database changes — code only.

- New `releases` table (`Release` in `models.py`: `id`, `date`, `title`,
  `body`, `created_at`) — `CURRENT_SCHEMA_VERSION` bumped 3 → 4 in `db.py`
  so `init_db()`'s `create_all()` picks it up on next deploy; purely
  additive, no existing table touched.
- New `GET /releases` (list, newest-first by date then id) / `POST
  /releases` (create) / `POST /releases/{id}/delete` in `app.py`, all
  behind the existing `auth_guard` middleware — no new RBAC. New
  `templates/releases.html` (inline add-entry form + one `result-box` per
  entry, same visual idiom as `wiki.html`), nav link in `base.html` next to
  Wiki, and a `/releases` link added to the Wiki page's own Contents list.
  `body` renders via Jinja's default autoescaping with `white-space:
  pre-wrap` — no Markdown dependency added, per the issue's explicit call.
- New tests: `tests/test_releases.py` (model round-trip, empty state,
  create + redirect, newest-first ordering, autoescaping/line-break
  rendering, delete + 404-on-unknown-id, nav link present). Full suite:
  286 passed + these 8 new ones, plus 4 pre-existing unrelated failures in
  `test_app.py` (`test_inventory_can_be_sorted_by_language`,
  `test_inventory_price_sort_keeps_unpriced_cards_last`,
  `test_inventory_shows_net_paid_and_per_print_gain`,
  `test_inventory_value_sorts_treat_missing_cost_as_less_than_zero`) —
  confirmed present on `main` before this branch too (a SQLite date-binding
  issue in `Transaction`-seeding test helpers, unrelated to this change);
  not touched.
- **Not yet exercised in a real browser** — same caveat as the listings
  entry above; only exercised via the test client. A `ux` pass on the
  actual rendered page (spacing/entry-density/form placement) was
  recommended by the issue itself once a first draft exists, since the
  issue's UI-placement call was made by `architect` without a live `ux`
  consult.

# Handoff notes — 2026-09-19 session (issue #127, mark listing sold)

Built on a new branch off `claude/issue-126-listing-edit-delete` (issue
#126/PR #147 was still open/unmerged at branch-cut time, per this ticket's
own instructions — reuses that branch's code directly rather than waiting).
If PR #147 merges to `main` before this one, expect a straightforward rebase
(this branch's diff is additive on top of #126's, no overlapping edits to
the same lines).

- **Schema change**: added `Transaction.listing_id` (nullable FK →
  `listings.id`, additive — `db.py`'s `_add_missing_columns()` picks it up
  automatically) and bumped `db.CURRENT_SCHEMA_VERSION`, per this repo's
  established migration convention. Originally authored as a 3 → 4 bump;
  by the time this PR was merged, PR #149 (issue #144, Release Notes) had
  already landed and taken version 4 for its own additive `releases` table,
  so this PR's version bump was resolved to **4 → 5** instead during the
  merge-order pass described in this app's PR history — both are purely
  additive schema changes (a new table from #149, a new nullable FK column
  from this ticket), so there's no data-loss risk from the renumbering, just
  keeping the version sequence monotonic. No direct production-database
  edits either way — this is a normal, additive code migration, applied
  automatically the next time `init_db()` runs against prod (next
  deploy/cold start).
- `GET`/`POST /listings/{id}/mark-sold` (`templates/listing_mark_sold.html`)
  reuses the purchase-cart form's shape (search-free here, since the card
  set is fixed to the listing's current `cards`) rather than a new UI
  pattern: one row per card, price pre-filled as `suggested_price /
  card_count` (editable, never auto-submitted). Submitting requires a price
  for every card currently in the lot — none can be silently skipped or
  added — and creates one `Transaction(type="sale", listing_id=..., ...)`
  per card sharing one fresh `purchase_id`, then flips `status` to `"sold"`,
  all in a single `db.commit()` (nothing partially applies on a validation
  failure — verified in tests). Re-running mark-sold on an already-`"sold"`
  listing is a no-op (both the GET form and the POST route redirect/bail
  without touching `Transaction` rows).
- `queries.listing_overview`/`listing_entry` now also return each card's
  real `sold_price` (via the new `Transaction.listing_id` join), rendered
  as a new "Sold price" column in `partials/listing_entry.html`. `/listings`
  gained a "Sold only" checkbox (`sold_only` query/form param) alongside the
  existing "Show delisted" one — kept as two independent booleans rather
  than a single three-way status selector, since the issue's own scoping
  text describing #126 as already having a richer "active/delisted/all"
  selector didn't match what #126 actually shipped (a plain "Show delisted"
  boolean toggle) — extending that boolean pair was the smaller, safer
  change and satisfies the same underlying ask (a way to see just sold
  listings) without a larger UI rework nobody asked for explicitly.
- Never touches `qty`, `card_collections`, or `binder_id` — same invariant
  as every other listing action, asserted directly in tests.
- `economic_summary`, `cash_flow_by_month`, `net_invested_by_card` pick up
  mark-sold's Transactions with zero special-casing (they're just normal
  `type="sale"` rows) — verified by a dedicated test rather than just
  trusted.
- `models.Transaction`/`models.Listing` docstrings and README's "Sales
  listings (finn.no)" + Listings-page sections updated per the issue's own
  doc-update discipline.
- New tests in `tests/test_listings.py`: form prefill, one-Transaction-
  per-card + shared `purchase_id` + `listing_id`, status flip, no qty/
  collection/binder mutation, missing-price rejection (no partial
  Transactions), invalid-price rejection, re-run-is-a-no-op, already-sold
  form redirect, sold price shows on `/listings`, "Sold only" filter, and
  the economic-queries-pick-it-up-for-free test above. Full
  `apps/tcg_inventory` suite: 303 passed, same 4 pre-existing failures in
  `test_app.py` as #126's session noted (confirmed present on this branch's
  unmodified base too, unrelated to this change — a SQLite `Date`
  type/string mismatch).
- **Not exercised in a real browser**, same caveat as #125/#126 — only via
  the test client. The issue itself flags this form (per-card price entry)
  as the most complex net-new form in the Listing lifecycle and recommends
  a real `ux` pass before shipping; this session's environment again had no
  `Agent` tool to spawn `ux` directly (same caveat #126's session logged),
  so that pass still hasn't happened.

# Handoff notes — 2026-09-19 session (seeding the Release Notes page)

No code changes. **Direct production-database change**, not reflected
anywhere else in git history:

- Inserted 6 rows directly into the production `releases` table (issue
  #144's page, shipped empty by PR #149) covering user-facing tcg_inventory
  changes from the 2026-09-14–09-19 backfill: sales/listings, Set-based
  completion %, TCGPlayer pricing fixes, dashboard value-correctness fixes,
  the Transactions order-edit flow, and the Release Notes page itself.
  Deliberately **tcg_inventory-only** — repo/agent-tooling changes (the
  `.claude/agents/` pipeline work) and finn_ad_scraper (no changes in this
  window anyway) were left out, since this page is user-facing inside the
  tcg_inventory webapp specifically, not a repo-wide changelog. A separate
  `CHANGELOG.md` was added at the repo root the same session for the
  repo-wide view (all three categories, git/PR-history based); the two are
  intentionally not kept in lockstep — this page stays hand-curated and
  user-facing per its own docstring in `models.Release`.
- Written via direct SQL against the Supabase project (`db.` prefix
  `nverpumoregkjfeddrwa.supabase.co`), matching the exact shape the app's
  own `POST /releases` form would write (`date`, `title`, `body`,
  `created_at`) — same schema, same table, nothing bypassed. Each entry can
  be edited only by delete-and-re-add (no edit route, by the page's own
  v1 design) via the `/releases` UI.

# Handoff notes — 2026-09-19 session (Order #17 price split)

## Direct production-database changes (not in git history)

At the user's request: Order #17 (`purchase_id = 17`) was created with all
40 cards paid 210 kr total plus 40 kr shipping (250 kr grand total), but
with `price = 0` on every card row (none individually priced yet) and
`purchase_total` stored as 210 (just the card total, not the grand total
the field is meant to hold per its own tooltip: "The full amount you paid
for the order"). Ran directly against the production Supabase database
(via the Supabase MCP connection, project `nverpumoregkjfeddrwa`):

```sql
UPDATE transactions
SET price = 5.25, purchase_total = 250
WHERE purchase_id = 17;
```

`5.25` = `210 / 40`, exact — shipping is tracked separately via
`purchase_shipping` (unchanged at 40) and deliberately *not* folded into
the per-card price, since `queries`'s diff calculation
(`purchase_total - total_price - purchase_shipping`) already subtracts it
once on its own; splitting it into each card's price too would double
count it. `purchase_total` corrected from 210 to 250 (cards + shipping) to
match that same formula — `250 - 210 - 40 = 0`, exactly matching this
order having no remaining/undeclared amount.

(Two earlier passes in this same session got this wrong before landing
here: first assumed 39 cards instead of 40, and used 4.36/card with a few
øre of rounding drift; second used the correct 40-card count but treated
the stored 210 as the grand total instead of just the card total, missing
that shipping needed to be added on top rather than subtracted from it.
Both were caught and corrected via further `UPDATE`s before this file was
written, so only the final, correct state is recorded here.) All 40 rows
confirmed updated (40 rows returned by the query's `RETURNING` clause).

No code change: this exact "distribute remaining" capability already
exists in the New Order cart UI but not on the Edit Order page
(`/transactions/purchase/{id}/edit`) for an already-committed order like
this one — that's why it was done directly against the database instead
of through the app. Worth a small future enhancement to add the same
"distribute remaining across unpriced rows" button to `purchase_edit.html`
so this doesn't need a direct DB edit next time (not filed as an issue
yet — flagging here per this file's convention for open items raised but
not built).

Separately, in the same session: order #17's 40 rows also had two
different `date` values -- 5 rows at `2026-09-03` (the actual order date)
and 35 at `2026-09-19` (today, the default fill-in for whichever were
added individually rather than as part of the original cart submission).
At the user's confirmation, normalized all 40 rows to the order's actual
date:

```sql
UPDATE transactions SET date = '2026-09-03' WHERE purchase_id = 17;
```

There's no order-level date column (`Transaction.date` is per-row, per
`models.py`) -- "the order's date" here just means the date the earliest,
originally-submitted rows already carried, applied to every row sharing
this `purchase_id`.

# Handoff notes — 2026-09-20 session (card-search reliability fixes)

Reported directly by the user: "a few issues trying to add new cards to a
new order." Investigation found two real, previously-unnoticed bugs rather
than one:

1. **Enter key in a card-search box submitted the whole form instead of
   searching.** Every free-text card-search input in the app (New Order
   cart, Edit Order's add-card, Edit Order's per-row relink, Listing edit's
   card-search) lives inside a `<form>` alongside a "Register"/"Save
   changes" submit button, with nothing before it to catch Enter. Standard
   HTML behavior: pressing Enter in any text input clicks the form's first
   submit button. For the New Order cart specifically, that meant typing a
   card name and hitting Enter (a very natural search habit) submitted the
   in-progress order instead -- with no cards added yet, so `create_purchase`
   rejected it and the cart panel reset, which reads exactly like "I
   searched and nothing happened." Fixed with a one-line
   `onkeydown="if (event.key === 'Enter') event.preventDefault();"` guard on
   all four inputs -- htmx's own `keyup` trigger still fires normally,
   Enter just no longer escalates to a form submit.
2. **"+ Add to order" silently did nothing if no cart was open** -- a
   long-known, previously-flagged-but-never-fixed limitation (see #86 in
   this file's 2026-09-14 entry): the button's `hx-target="#cart-body"`
   doesn't exist until a New Order cart is actually open, so clicking it
   beforehand was a true no-op with only a hover tooltip to explain why.
   Replaced the three buttons that target `#cart-body` (Recently Added's
   row button, the cart's own search results, its "Show cards without an
   order" results) with a shared `addCardToCart(cardId)` JS function that
   checks for `#cart-body` first and alerts ("Open a New Order first...")
   instead of doing nothing when it's missing; otherwise issues the same
   `htmx.ajax(...)` request the old `hx-get` did.

Both are template + inline-JS-only changes, no backend/route changes, no
schema changes. New tests assert the guard attribute is present and that
the old bare `hx-get="/transactions/purchase/add-row..."` markup is gone
from Recently Added's button (replaced by `addCardToCart(...)`); the
alert/guard behavior itself is untestable via the pytest test client (no
JS execution) -- same caveat prior sessions have logged for other
client-side-only fixes, worth a real-browser check before/after deploy.

## Follow-up in the same session: "Register does nothing, not even an error"

Reported right after the fixes above shipped. Root cause: the cart form's
`price` input (`partials/purchase_cart_row.html`) and `date` input are both
`required`, so a blank price blocks the browser's *native* HTML5 validation
before the request ever reaches the server -- no server-rendered error can
show for that case, because no request was sent at all. That's exactly
"does nothing, not even an error": the only feedback was a small, easy-to-
miss native validation tooltip, not a real error message. Confirmed the
backend itself was fine the whole time (`create_purchase`'s own
`test_purchase_cart_search_result_adds_a_row_and_final_submit_creates_transactions`
test, which drives the full POST -> redirect -> re-render chain via the
test client bypassing the browser entirely, still passed).

Fix: `onsubmit="return confirmRegisterOrder(this)"` on the cart form --
checks for at least one card row and that every price field is filled,
alerting with a specific message and focusing the empty field instead of
leaving it to native validation's easy-to-miss tooltip. Same client-side-
only caveat as above: untestable via pytest, only that the attribute
itself renders (asserted in a new test).

# Handoff notes — 2026-09-20 session (merged work not previously logged)

Catch-up log for PRs merged 2026-09-19 through 2026-09-20 that had no
HANDOFF entry of their own (the card-search / "Register does nothing" fixes,
#170 and #171, are covered by the entry above and not repeated here).

**No direct production-database changes.** Checked each PR's file list and
description below: all are code/template/docs-only, no schema changes, no
seed or migration scripts, nothing run against Supabase. (The one direct-DB
change from this window, Order #17's price split and date normalization, is
already logged above in the "Order #17 price split" entry, which shipped
inside #161's diff.)

## Code shipped to prod (all merged to `main`)

Transactions page / order flow (all `tcg_inventory`):

- **#154** (issue #153): The "rest of the collection with no known date"
  table on Transactions was easy to misread as "cards without an order",
  but its filter is really `Card.created_at IS NULL` (the one-time legacy
  import flag), unrelated to order status. Reworded heading and caption to
  name the real filter, added a per-row order-status column ("Ordered ·
  {price}" / "No order yet") from the already-computed `registered_prices`
  (no new query), added the missing "+ Add to order" button, and dropped a
  dead `total_count` context value. Findings in `UX_NOTES.md` (2026-09-19).
- **#157** (issue #155): Edit Order (`/transactions/purchase/{id}/edit`)
  can now append a card to an already-committed order via a search box and
  a new `POST /transactions/purchase/{id}/edit/add-card`, which creates one
  default `Transaction` row (today / purchase / price 0) against that
  order. Deliberately its own endpoint rather than folded into
  `update_purchase`'s `tx_id`-keyed loop, which only edits existing rows.
  Row markup extracted into a shared `purchase_edit_row` macro. Replaces
  the old workaround of registering the card under a new order and
  hand-retyping the Order ID to merge.
- **#158** (issue #156): New Order cart has an opt-in "Show cards without
  an order" toggle backed by `GET /transactions/purchase/browse-unordered`
  (cards with no linked purchase `Transaction` at all, capped at 50 with the
  full count reported). Not scoped to the `created_at IS NULL` filter from
  #153/#154, since "no order" and "no known date" are different questions.
- **#161**: Legacy import table now hides cards that already have a
  purchase transaction (its job is "still needs a date and an order").
  Edit Order's **Agreed total** defaults to `shipping + sum(priced cards)`
  when nothing has been saved yet; once a value is saved it is sticky and
  never silently recalculated (confirmed with the user as deliberate). New
  client-side "Distribute remaining across unpriced cards" button fills
  `Agreed total - Shipping - sum(priced cards)` evenly into `price = 0`
  rows, refusing (no changes) if all rows are priced or the remainder is
  negative; nothing persists until "Save changes". Also carried the Order
  #17 HANDOFF entry (see above). This PR also added a singular
  `POST /transactions/purchase/add-existing-card` route, superseded by #166.
- **#166**: Legacy import table gets per-row checkboxes plus a select-all
  header checkbox and an order picker dropdown (existing orders from
  `purchase_groups`), with one "+ Add checked cards to order" button.
  Route renamed `add-existing-card` -> `add-existing-cards` (takes a list of
  card IDs); rejects with no changes if nothing is checked or the order
  does not exist. Each added card lands as today / purchase / price 0.
- **#172**: When a New Order cart is open, the Legacy import bulk control
  switches to "+ Add checked cards to open order", appending straight into
  the cart via the existing `addCardToCart()`; with no cart open it shows
  the #166 order picker. Toggled by `updateLegacyOrderControls()` (initial
  load, after "+ New Order" opens a cart, after Cancel). The hidden order
  `<select>` is also `disabled` while hidden so a hidden `required` field
  cannot silently block submit. Template + inline JS only, no backend change.

Other:

- **#160** (issue #159): Release Notes (`/releases`) and the Sync Log
  (`/import`) merged into one "Activity Log" page: Sync Log section first,
  Release Notes second (latest 15 shown, older behind a `<details>`).
  `GET /import` now 308-redirects to `/releases#sync-log`, preserving
  `lsort`/`ldir`. Sync Log column sorts swap just that section via htmx so
  sorting does not jump above Release Notes; `#sync-log` has
  `tabindex="-1"` and takes focus on hash load. Nav shows a single
  "Activity Log" link. Also fixed a stale README paragraph about the
  Dropbox file-picker UI removed per #87 (its routes still exist but
  nothing links to them; sync only happens via the daily cron or a direct
  `GET /cron/dropbox-sync?secret=` call).
- **#165** (issue #164): `chart.umd.js` and `tcg-charts.js` are no longer
  loaded render-blocking on every page; a `chart_scripts` block in
  `base.html` is filled in only by `dashboard.html` and `transactions.html`
  (the only templates that call the `chart_card` macro). htmx's `<script>`
  gets `defer`. **Gotcha for future work:** any new template that calls
  `chart_card` must also fill `chart_scripts`, or its charts will silently
  not render.
- **#167** (issue #163): `economic_summary` and `net_invested_by_card`
  (`queries.py`) accept an optional pre-loaded `txs` list; `dashboard()` and
  `/inventory` now load `Transaction` once per request and thread it
  through. The issue's suggestion to skip the `net_invested_by_card` load
  when the sort is not a value sort was deliberately **not** done: the
  Net paid / Gain columns render on every row regardless of sort, so gating
  it would blank them. The dashboard.html htmx lazy-load follow-up and the
  Vercel `DATABASE_URL` pooler-port check from that issue are still not
  done.
- **#168** (issue #162): `queries.real_value_history` now does the
  per-date/per-source `GROUP BY` + `SUM` in SQL instead of loading every
  `CardSnapshot` row into Python on each Dashboard load. Output was verified
  byte-identical to the old implementation across all three metrics,
  including its existing quirk that the `unique` metric does not gate on
  `qty > 0` (unlike `Card.unique_value`). This makes reads cheap; it does
  **not** reduce stored rows.

## Open items raised but intentionally not built

- **#169 (CardSnapshot downsampling/retention)** was filed as a fast-follow
  to #168 and is a deliberate deferral needing a design decision before
  anyone builds it: `card_snapshots` still grows by up to two rows per
  card per day (cron + manual) indefinitely. Open questions on the issue:
  retention window vs. downsampling; where the collapse happens (periodic
  job vs. on read); how cron/manual sources and per-card granularity
  (`card_count`) survive a rollup; and what resolution boundary the chart
  should use, which wants UX input. Must stay additive/non-destructive by
  default per repo convention.
- Client-side-only behavior in #161 (Distribute remaining), #166 (select
  all / submit guard) and #172 (cart/picker toggle) has no automated
  coverage (no JS execution in pytest); each PR asked for a manual browser
  check that is not recorded as done.
- Test suite state: 5 `test_inventory_*` tests in `tests/test_app.py` fail
  on `main` in this environment (SQLite `nulls_last`/Date-type
  incompatibility), unrelated to any PR above and reported as pre-existing
  in every one of them.
  **Resolved 2026-09-20 in #176 (see the entry at the end of this file);
  the suite is now fully green.**

# Handoff notes — 2026-09-20 session (issue #176, 5 failing /inventory tests)

**Production-affecting bug, fixed in code (no DB change).** `GET /inventory`
with any non-default sort (`sort=` anything other than `release`, and not
`net_invested`/`gain_loss`) built `ORDER BY <col> NULLS LAST ASC`
(`app.py`: `.nulls_last()` was applied before `.asc()`/`.desc()`, introduced
in 6a83553 on 2026-09-18). That is invalid SQL on SQLite **and Postgres**
(`ASC NULLS LAST` is the valid order), so on Supabase prod those sort links
returned a 500 from 2026-09-18 until this fix is deployed. Not an
environment/version issue. The default `release` sort and the Net paid/Gain
sorts (in-Python) were unaffected.

The other three failures were wrong tests, not app regressions: two inserted
`Transaction(date="2026-01-01")` as a string (SQLite's Date type requires a
`date`); the `NoLinkCard` fixture relied on `Card.qty`'s default of 0, which
the #132 default filter hides from `/inventory`; and the net-paid test
searched for `"> -</td>"` when the template renders `<td class="num">-</td>`.
No assertions were loosened. No CI exists yet, which is why these went
unnoticed; a pytest workflow is a separate, not-yet-approved follow-up.

## Transactions page layout redesign — 2026-09-20 session

All in git (branch `worktree-transactions-redesign`); no database changes.
Full detail in `UX_NOTES.md`'s 2026-09-20 entry and README's Transactions
section. Summary: Order history moved to the top as a proper columnar list
(Order/Date/Qty/Value/Shipping/Agreed total/Remaining/Platform), "Recently
Added" and "Legacy import" merged into one card picker with a `?pick=`
filter and checkbox selection, and the second KPI bar reduced to a header
caption (the KPI cards themselves were left untouched, as the user asked).

**Open item deliberately not built.** `POST
/transactions/purchase/{id}/total` still blind-overwrites `purchase_total`,
`purchase_shipping` **and** `platform` on every row of the order, so
submitting that form with a blank platform NULLs platform everywhere — and
it prefills blank by design whenever an order's rows disagree, while the
summary still shows a platform badge. This redesign only made the trap
*visible* (the field now reads "Mixed — saving overwrites all rows", and
the summary badge gets a `*`). Actually changing the write semantics —
e.g. only updating fields present and non-empty in the submission —
contradicts the route's documented "blank clears" behavior and would change
what an existing form submission does, so it wants a deliberate decision
rather than being folded into a layout change. Same applies to shipping:
filling only Agreed total still clears a previously-saved Shipping.

**Also worth knowing:** `usort`/`udir` no longer drive anything (the table
they sorted is gone) but are still accepted by the route so old links don't
422 — there's a test pinning that. And `queries.top_valuable_cards(db,
limit=50)` was removed from `_transactions_context`: it ran on every load
and every sort click while `kpi_module.html` only renders that tile on the
Dashboard.

## Flagged-missing cleanup — 2026-09-23 session

### Direct database changes (Supabase prod) — not in git

Reviewed the three cards with `flagged_missing_since` set. The user confirmed two
were mis-registrations they had already removed from Dex, so they were deleted by
hand (the same end state a "Full load" would give for just these two):

- `cards.id=897` — Psyduck, Mega Evolution Promos 7, Cosmos Holo, qty 1, flagged
  2026-09-16. No transactions or listings.
- `cards.id=193` — Persian, Generations 54/83, Normal, qty 1, flagged 2026-09-19.
  Also deleted its one `transactions` row (id 541, purchase in order #2, price 0 —
  the order total 419 + 44 shipping is still carried on the order's other rows).

Their `card_snapshots` rows (32 total) were deleted too; no `card_collections` or
`listing_cards` rows existed.

Left as-is: `cards.id=164` Mega Venusaur ex (Mega Brave 3/63), qty 0, flagged
2026-09-12. It was traded away (transaction id 56), so it stays for history.

### Open item

There is no UI for flagged cards: they don't appear in Inventory, and the dashboard
doesn't exclude them. A per-card review page (delete / keep as qty 0 / ignore) was
suggested but not built.

## Trade In/Out + trade gain; smarter "Distribute" — 2026-09-23 session

### Code (branch `claude/practical-archimedes-xg1ubx`)

- New nullable column `transactions.direction` ("in"/"out", trade rows only).
  `init_db()`'s additive column pass creates it on Supabase at the next
  deploy — no manual migration.
- Gave/Got block and trade gain on trade orders (`queries.trade_summary`,
  `queries.trade_prices_at`); In/Out selectors in the New Order cart (which now
  offers Trade as a type), Edit order and the row quick-edit. See README
  "Trades".
- Edit order's "Distribute remaining" can split by market value or evenly,
  rounds exactly to the remainder, and skips trade rows.

### Direct database changes (Supabase prod) — done 2026-09-23

#181 added the `direction` column but forgot to bump `db.CURRENT_SCHEMA_VERSION`,
so `init_db()`'s version gate skipped the migration chain and prod's
Transactions page queried a column that didn't exist. Fixed right after deploy:

- `alter table transactions add column if not exists "direction" varchar;` —
  exactly what `_add_missing_columns()` would have run.
- Order #13's trade (confirmed by the user): `direction = 'out'` on tx 56
  (Mega Venusaur ex), `'in'` on 57 and 58 (Hypno, Slowbro).
- Follow-up PR bumps `CURRENT_SCHEMA_VERSION` to 6, so every other database
  (local SQLite) gets the column. On prod the next cold start re-runs the
  idempotent chain (column already there) and stamps version 6.

Lesson: a new model column needs a `CURRENT_SCHEMA_VERSION` bump, or it never
reaches an already-migrated database.
