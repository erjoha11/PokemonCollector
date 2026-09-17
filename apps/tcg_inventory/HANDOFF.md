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

- **No UI to edit an existing order** after it's registered (retype,
  relink, move a card between orders, merge/split, add a note) — every
  correction above required raw SQL. This was flagged explicitly to the
  user as the single biggest real gap; they said current scope is fine and
  declined to prioritize it, but it'll very likely come up again.
- The "+ Legg til i ordre" silent-no-op-when-no-cart-is-open issue (see #86
  above) — **addressed below** (renamed to "+ Add to order", behavior
  itself unchanged).
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
