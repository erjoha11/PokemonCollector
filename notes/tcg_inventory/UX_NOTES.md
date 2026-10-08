# UX notes

A running log of usability/design findings from the `ux` agent (see
`.claude/agents/ux.md`), so its analysis survives past the chat session it
came from. The `ux` agent itself never writes here — it's read-only by
design — whichever session consulted it appends the entry afterward.

Each entry: date, what was reviewed, what was found, and its status. Mark an
entry `Addressed` (with a short note on the fix, or a PR/commit reference)
once it's dealt with, rather than deleting it — a resolved entry is a record
that the issue was seen and handled, not just silence.

---

<!-- Example entry shape:

## 2026-09-16 — Transactions purchase-cart flow

**Reviewed:** templates/transactions.html, partials/purchase_cart*.html

**Findings:**
- ...

**Status:** Open
-->

## 2026-10-04 — Facebook wins inbox → New Order cart (design review, #309)

**Reviewed:** (from source; nothing built or rendered yet) the proposed flow for
importing Facebook auction wins from `fb_auction_watcher` into the existing New
Order cart: templates/partials/purchase_cart.html, static/orders-cart.js,
app.py's purchase routes, templates/dashboard.html. Consulted by `architect`
during intake of issue #309.

**Found:**
- **Reuse the New Order cart; don't build a new review page.** A separate page
  would have to redo Total, Shipping, Remaining, Distribute and validation, which
  took several rounds to get right (#228, #254). The cart holds one order at a
  time, and Register replaces `#main-content`. So the list of imported wins must
  come from the server, or it's wiped after each seller.
- **Keep suggested cards outside the form.** As hidden inputs inside it,
  unticked suggestions either get registered by mistake or trip the
  `require_same_length` check (card/price lists), which shows a confusing error.
  Linked items should become normal cart rows. Any new per-row field (item ID,
  note) must stay lined up with `card_id`/`price`.
- **Searching from an item needs to know which item it's for.** The current
  search buttons (`addCardToCart(id)`) add a card with no item attached, so the
  item would quietly come back on the next send.
- **Existing rounding bug:** `distributeRemaining()` (static/orders-cart.js
  ~183–197) rounds each share on its own: 100 kr over 3 cards becomes
  33.33 × 3, leaving 0.01 kr under Remaining. The last row should take the
  rounding.
- **Unlinked items must never vanish silently.** Show "N of M items not linked
  (kr X)" above Register and ask for confirmation. When imported items are
  unlinked or unpriced, require a Total or warn, because with no Total there's
  no Remaining figure. Remaining is Total minus prices minus shipping (app.py
  ~1946), so the prefilled Total must agree with how shipping is entered.
- **Each item needs "Ignore permanently"** (a cancelled or duplicate win). A lot
  linked to fewer cards than it holds needs a way to stay pending.
- **Upload vs. paste:** if a file is ever used, a file picker beats a paste box.
  Moot while the extension sends directly.
- **Dashboard:** don't show Incoming as a collection row; show one "On the way"
  line that links to the existing Inventory filter. On the Purchased tab, add
  "Incoming cards not on any order: N" to catch a forgotten registration.
- **Minor:** a cart that was only prefilled still asks "Discard the in-progress
  order?" when switching seller (`cartHasUnsavedWork()`). "Start new order" on
  Edit order uses an order number fixed when the page loads (already noted
  2026-09-20).
- **Follow-up:** `ux` wants to see the item panel once it's built, especially
  with 10+ items from one seller.

**Status:** Open (design input for #309; the rounding bug is independent and
could be fixed on its own).

## 2026-09-20 — Transactions page layout redesign

**Reviewed:** templates/transactions.html, partials/{purchase_cart,
purchase_cart_row,purchase_cart_search_results,purchase_cart_unordered_results,
tx_row,kpi_module,transactions_charts,macros}.html, static/style.css, and
app.py's `_transactions_context` / `list_transactions` /
`_group_transactions_by_purchase` / `_cards_with_known_added_date` /
`set_purchase_total`. Prompted by the user asking for a layout redesign;
`ux` was consulted twice — once open-ended, then again against the layout
the user chose (Order history first, one merged card table, selection-based
adding).

**Findings:**
- **A column-sort click silently destroyed an in-progress order.** The New
  Order cart is DOM-only until Register (rows in `#cart-body`, prices in
  unsubmitted inputs), while every `sort_th` on this page renders a plain
  `<a href>` full-page navigation. Add 12 cards, type prices, click "Name"
  to find the next card → everything gone, no warning. Same failure family
  as the sale-list selection loss solved in `static/sale-list.js`, and
  caused by the layout itself: the picker tables with sort controls sat on
  the same page as, and two screens below, a cart that only existed in the
  DOM. **Addressed** — a page-level `beforeunload` guard (guarding the page
  once, rather than each link, so a link added later can't reintroduce it),
  plus `sessionStorage` persistence for the card picker's own selection.
- **The inline order form silently wiped platform.** `POST
  /transactions/purchase/{id}/total` blind-overwrites all three fields on
  every row; `_group_transactions_by_purchase` deliberately leaves
  `group.platform` blank when an order's rows disagree, while the summary
  still shows a `summary_platform` badge. So: badge says "finn.no", input
  looks empty, you edit a total, every row's platform becomes NULL.
  **Partly addressed** — the field now says "Mixed — saving overwrites all
  rows" and the summary badge gets a `*` marker, so it no longer looks
  innocently empty. The underlying write semantics are unchanged (blank
  clears, per the route's docstring); changing that is an architect/user
  call, not a UI fix. **Addressed 2026-09-29 (#202):** blank Shipping/
  Platform now leave the rows untouched (only Total's blank clears, back
  to the automatic sum); the placeholder reads "Mixed — blank keeps
  per-card".
- **Three value displays, two identical, one contradicting.** The KPI card's
  "Market Value" (unique + duplicates), `.tx-kpi-bar`'s "Current value"
  (unique only) and the charts' `market_value_stats` trio all answered
  "what's it worth" within one screen, unlabelled as to basis. **Addressed**
  — the user asked to keep the KPI cards as-is, so the bar was reduced to
  the two figures that appear nowhere else (Net invested, Paper gain/loss)
  as a header caption; "Current value" is gone.
- **Three card-picking surfaces feeding one destination, with overlapping
  contents and two different selection models.** Legacy import ⊂
  browse-unordered, and the unpriced part of Recently Added ⊂
  browse-unordered; ~40 lines of JS existed solely to keep two
  mutually-exclusive submit paths in sync depending on whether a cart
  happened to be open. **Addressed** — one merged table, one sort pair, one
  selection model, one always-valid "Adding to" target. The "click + Add to
  order with no cart open and nothing happens" dead end is now structurally
  unreachable rather than better-worded, and `updateLegacyOrderControls()`
  and friends are deleted.
- **A merged table must not apply both tables' inclusion rules.** Recently
  Added listed every dated card whether or not it had an order; Legacy
  listed only cards without one. Membership is now "every card" with the
  distinction as an explicit `?pick=` filter. A first implementation got
  this wrong (the legacy pre-filter ran before the merge, so "All" wasn't
  all) — caught by a test.
- **Order history had no date and no scannable "needs work" signal.**
  `group.min_date` was computed and never rendered, so collapsed orders
  couldn't be told apart; and Remaining only rendered when nonzero, making
  "settled" and "no agreed total set" both render as nothing. **Addressed**
  — Date is a column, Remaining is a real column with ✓ / — / red diff.
- **`top_cards` was dead work on this route.** `queries.top_valuable_cards(
  db, limit=50)` ran on every load and every sort click, but
  `kpi_module.html` only renders that tile on the Dashboard. **Addressed** —
  removed from `_transactions_context`.
- Not addressed, noted for later: the per-order `<summary>` rows still
  carry heading weight without heading semantics (the app-wide gap logged
  2026-09-19), and `/transactions/charts`'s known `metric` pill desync
  (logged 2026-09-16) is untouched by this work.

**Status:** Addressed except the platform-overwrite write semantics and the
two pre-existing items named above.

---

## 2026-09-17 — New "Sell on finn.no" module

**Reviewed:** README.md, models.py, templates/inventory.html,
templates/partials/{dropbox_files,inventory_table,macros,purchase_cart*}.html,
templates/transactions.html, and apps/finn_ad_scraper/card_identifier.py's
CONDITIONS vocabulary (prompted by adding a card-selection → finn.no ad
generation feature — see HANDOFF.md's matching entry for the shipped code).

**Findings:**
- The app's UI is English throughout (not Norwegian, despite README's
  Dropbox section quoting a stale Norwegian button label that no longer
  matches `dropbox_files.html`'s actual "Fetch selected files and sync" —
  worth a one-line doc fix, not done here since it's unrelated to this
  feature).
- Checkbox multi-select has a direct precedent (`dropbox_files.html`'s
  checkbox column feeding a form submit) and the search/select → review →
  submit shape has a direct precedent in the purchase cart — both reused
  for the new selection/review flow rather than inventing new patterns.
- Risk specific to this feature: Inventory's filter/sort controls fully
  replace `#inventory-results` on every change (`hx-swap="innerHTML"`), so
  a naive checkbox selection would be silently dropped the moment the user
  narrows the filter to find more cards to sell — same failure family as
  the already-documented "+ Add to order" no-op. Addressed in the shipped
  code via `static/sale-list.js` (sessionStorage-backed selection,
  re-hydrated on `htmx:afterSwap`), not by persisting selection
  server-side.
- Selling a duplicate should default "quantity to sell" to 1, never to the
  card's full owned `qty` — defaulting to "sell everything you own" is the
  wrong default for a collector tool. Shipped as an explicit, editable
  per-row input on `/sales`, capped server-side at the card's `qty`.

**Status:** Addressed — see HANDOFF.md's "New 'Sell on finn.no' module —
2026-09-17 session" entry for what shipped.

## 2026-09-16 — Full-app review (value charts + card image rollout)

**Reviewed:** every template in templates/ and templates/partials/, static/style.css, static/tcg-charts.js, card_images.py, and the app.py routes feeding them. Prompted by the current uncommitted diff (value-growth/KPI chart rework + new `card.image_url` thumbnails).

**Findings:**
- Transactions' two charts ("Collection value growth" and "Real value history") share a single `metric` query param via `hx-push-url`, but each has its own metric-filter pills that only swap their own card (`hx-select`/`hx-target`/`outerHTML`). Clicking a filter on one card desyncs it from the URL and from the other card; a refresh/bookmark then silently re-renders the stale card with the wrong metric. Fix is template/route-level (e.g. separate `metric`/`metric2` params, or swap both cards together) — no schema implications.
- `partials/purchase_cart_row.html` and `purchase_cart_search_results.html` still use the old plain-text Dex link instead of the new `dex_link(card)` macro/thumbnail now used everywhere else (inventory_table, transactions, dashboard) — the one place a photo matters most for disambiguating a print before registering a purchase.
- Dashboard's "Most valuable cards" tile (`partials/kpi_module.html` lines 33, 45) still guesses a tcgdex.net image URL from card_id/number instead of using the new, verified `card.image_url` — two independent, inconsistent image mechanisms now coexist.
- `templates/wiki.html`'s Transactions section (lines 112-118) describes the old inline per-row buy-form removed in PR #86; should describe the current "+ Add to order" button and its open-cart caveat instead. (Second instance of the same stale-Wiki-content bug class already fixed once for the Import/Sync section.)
- `static/tcg-charts.js` formats kr values via `toLocaleString("nb-NO")` on raw floats (no rounding), while every other kr value in the app goes through `_format_kr` (rounded to whole kr). A chart tooltip could show cents while the details table below it shows a rounded value for the same point. Fix: `Math.round(v)` before formatting.
- `models.py`'s new `image_url` column comment says lookups are "never retried automatically," which contradicts `importer.py`'s actual retry-on-next-sync behavior (and comment) for cards still missing an image. Doc-only fix.
- Judgment calls needing a visual check (no browser/screenshot access): whether 32px `.card-thumb` images cause row-height jitter in dense inventory/transaction tables when a row has no image; whether the new fixed-height (220px) Chart.js canvases render proportionally on Dashboard's compact card vs. Transactions' full-width one.

**Status:** Open

---

## 2026-09-19 — Transactions "rest of the collection with no known date" table

**Reviewed:** templates/transactions.html (lines ~149-179, plus surrounding
"Recently Added"/"History" sections for context), app.py's
_cards_with_known_added_date() (~1133-1148) and _transactions_context()
(~1254+), importer.py (~line 224), static/style.css.

**Findings:**
- Root cause: filters on `Card.created_at IS NULL`, a legacy data-migration
  flag — unrelated to whether a card has a linked order.
  `importer.py:224` sets `created_at` on every newly created Card, so this
  bucket is fixed/shrinking, not an ongoing state — copy should say "legacy
  import" rather than reading like a queue.
- Heading/position reads as "cards without an order" (sits right below the
  order tables) but isn't. Reword the `<summary>` to name the actual filter
  (e.g. "Legacy import — cards from before 'date added' tracking (N
  cards)") and add a `.muted` sub-caption like "Recently Added" has,
  explicitly noting this is unrelated to order status.
- Missing "+ Add to order" button is a real, cheap gap: "Recently Added"'s
  button (transactions.html:66-70) keys off `card.id` directly via
  `/transactions/purchase/add-row` (app.py:1403-1412) — no search step, no
  new route needed, same markup drops into this table's rows verbatim.
- No per-row "already has an order" indicator, and this table is exactly
  where one would be useful (cleanup/archaeology on old data). The dict
  this needs (`registered_prices`, keyed by card.id) is already computed
  and passed to the template (used today only in "Recently Added") —
  reusing it here is a template-only change, no new query.
- `total_count` (app.py:1325) computed, never rendered — dead code, wire in
  or remove.
- Accessibility: all `<details class="collapsible">` sections on this page
  (this table and "View charts") label themselves via `<summary>` text
  only, no real `<h2>`/`<h3>` inside, unlike the rest of the page — invisible
  to screen-reader heading navigation despite matching heading-level visual
  weight (static/style.css:206-215). App-wide pattern, not unique to this
  table; fix both collapsibles together or file as a small a11y pass.
- Empty-state copy ("None.", line 174) is terser than its siblings on the
  same page ("No cards with a known date yet.", "No individually registered
  transactions.") — align voice, and use the copy to reinforce that this
  bucket should shrink to zero over time.
- Verified NOT a bug: the `unknown_cards | length` count in the `<summary>`
  and the rendered `<tbody>` rows share the same sorted list object — no
  mismatch.
- No rendered-page access this session — visual/density calls (badge
  placement in the 0.7rem-font `.inventory-table`, heading-wrapped
  `<summary>` visual parity) should be eyeballed in a real browser before
  implementing.

**Status:** Open

## 2026-09-20 — Transactions page usability/visual review

**Reviewed:** templates/transactions.html, purchase_edit.html, and every
partial they include (purchase_cart.html, purchase_cart_row.html,
purchase_cart_search_results.html, purchase_cart_unordered_results.html,
tx_row.html, tx_row_edit.html, tx_row_view.html, purchase_edit_row.html,
purchase_edit_card_cell.html, purchase_edit_new_row.html,
purchase_edit_relink_cell.html, purchase_edit_relink_results.html,
purchase_edit_add_card_results.html, kpi_module.html,
transactions_charts.html, macros.html), the relevant app.py routes
(transactions_page, create_purchase/add-row/search, purchase_edit_form/
update_purchase/add-card/relink routes, /transactions/{id}/edit,
/transactions/purchase/{id}/total, /transactions/charts), and
static/style.css (`.tx-*`, `.kpi-*`, `.autocomplete*`, `form.filters`,
`button`, `details.collapsible`). Prompted by the user's "every function is
a little bit stocky... professional page with a smooth workflow" ask.

**Note:** the app's UI is fully English (HANDOFF.md: full translation done
2026-09-15) — any brief assuming Norwegian labeling for this page is stale.

**Findings:**

*Top finding — structural:* "Edit order" (transactions.html:141) is a plain
`<a href>` full-page navigation to purchase_edit.html, and `update_purchase`
(app.py:1705-1763) saves via classic form POST → 303 redirect → another
full-page load — even though every other control on this page (order
total/shipping, add-to-cart, relink, add-card) is htmx partial-swap. This
mismatch (2 full reloads to fix a typo on one row) is the single biggest
contributor to the "stocky/bolted-on" feel. The order-level atomic-commit
semantics (README) don't require abandoning htmx — an
`hx-select="#main-content" hx-target="#main-content" hx-swap="outerHTML"`
on the edit form (same pattern already used by the New Order cart's
Register button) would keep one-commit-per-save while dropping both full
reloads. Template/route change only, no data-model change.

Quick wins (small, high-value, mostly CSS/markup):
1. Every button (New Order, Register, Distribute evenly, Cancel, Edit
   order) shares one flat style (`style.css:775`) — no visual hierarchy
   between primary actions and secondary/destructive ones. A
   `button.secondary` class already exists (`style.css:788`) but is unused
   on this page; applying it to Cancel/Edit-order/Distribute-evenly would
   read as far more deliberate.
2. `purchase_cart_row.html` (the row you build every purchase in) is a bare
   unstyled `<table>` — the only table on the page with zero styling hooks,
   while Recently Added/History/Legacy tables all have real padding/borders/
   hover states. Give it `.tx-table`-equivalent treatment.
3. ~15 inline `style="..."` attributes in transactions.html alone (ad-hoc
   widths/margins, no consistent spacing scale), repeated in
   purchase_edit.html/purchase_edit_row.html for input widths that visibly
   don't line up across rows. Pull into a few reusable classes
   (`.tx-input-sm`, `.tx-input-date`, a spacing utility) — mechanical pass.
4. Legacy Import's checkbox-vs-order-select mode switch
   (`updateLegacyOrderControls()`) has no visible explanation for why the
   control changed shape when a cart is open — add a one-line `.muted` hint.
5. No htmx-indicator anywhere on the page (search-as-you-type, add-to-order,
   update total/shipping all give zero pending-state feedback) — cheap fix,
   `hx-indicator` + a small `.htmx-indicator` opacity rule.

Medium (real workflow friction, no schema change):
6. Registering an order swaps all of `#main-content`, so scroll position
   and any other manually-expanded order `<details>` reset/collapse — only
   the just-registered order's group reopens (`open_order`). Worth
   narrowing the swap target if feasible without complicating server-side
   `purchase_groups` recomputation.
7. "Distribute remaining across unpriced cards" (cart + edit-order) is
   arguably the most powerful, least-discoverable control on the page and
   looks identical to an ordinary filter input. A bordered/backgrounded
   "Pricing helper" box around it would raise discoverability
   proportionately (no tutorial/onboarding needed).
8. Order-summary line (`tx-order-summary`) gives Value/Fees/Shipping/Total/
   Remaining equal visual weight except Remaining (red). Given the app's
   own "never silently recalculate a user-entered value" principle, Total
   (user-entered) vs Value (derived sum) should be visually distinguished
   (e.g. accent color on Total) so the derived-vs-truth distinction doesn't
   require already knowing the convention.

Correctness note, low-severity: `purchase_edit_row.html`'s "Start new
order" button hardcodes `next_purchase_id` computed once at page-load
(`app.py:1616`). Clicking "Start new order" on two different rows in one
Edit-Order session, intending two separate new orders, silently merges both
into the same new order instead. One-line fix if it ever bites (increment a
client-side counter after first use).

**Suggested priority:** (1) button hierarchy + cart-row table styling, (2)
htmx-indicator, (3) Edit Order → htmx partial-swap conversion (biggest
"smooth workflow" win), (4) inline-style cleanup, (5) distribute-remaining
callout + order-summary hierarchy.

**Status:** Quick wins (1-5) Addressed 2026-09-20, same session. Top finding
(Edit Order htmx conversion) Addressed 2026-09-20, follow-up session —
`transactions.html`'s "Edit order" button and `purchase_edit.html`'s form
now use `hx-get`/`hx-post` with `hx-select="#main-content"
hx-target="#main-content" hx-swap="outerHTML"`, the exact pattern already
used by the New Order cart's Register button (`purchase_cart.html`) and
`create_purchase`/`set_purchase_total` — opening and saving Edit Order are
now in-place partial swaps, no full-page navigation or reload. `Cancel` was
also converted to an `hx-get` (`purchase_edit.html`), consistent with
`tx_row_edit.html`'s existing pattern for other in-place edits on this page.
`update_purchase` (app.py) is unchanged beyond this: it still does one
`db.commit()` for the whole order edit (retype/relink/move/merge/split all
still atomic) and still responds with a `RedirectResponse` to
`/transactions?open_order={id}` — htmx (like the browser) follows that
redirect and applies `hx-select` to the final page, so `open_order`
reopening the just-edited group's `<details>` still works. Verified via
curl (POST with `HX-Request: true` + `-L`, following the 303) that the edit
persists atomically and the response contains the reopened
`<details id="order-999" open>` group; full `python -m pytest
apps/tcg_inventory` run shows no new failures (same 5 pre-existing,
unrelated SQLite/SQLAlchemy-version failures as before). Medium items (6-8)
still Open, out of scope for this pass:
1. `button.secondary` now applied to Cancel (transactions.html,
   purchase_edit.html, tx_row.html's inline edit form), Edit order, Distribute
   evenly / Distribute remaining across unpriced cards, Remove (cart row),
   Start new order — Register/Save changes/Update total/shipping stay the
   one primary action per form.
2. `purchase_cart_row.html`'s table now uses `class="tx-table"`, matching
   Recently Added/History/Legacy.
3. Inline `style="..."` widths/margins across transactions.html,
   purchase_edit.html, purchase_edit_row.html and purchase_cart*.html
   replaced with reusable classes in style.css (`.tx-input-xs/-sm/-md/-date/
   -lg/-xl`, `.tx-search`/`.tx-search-sm`, `.mt-*`/`.mb-*` spacing utilities,
   `.tx-header-row`, `.inventory-table-wrap.compact`) on a 0.5/0.75/1/1.5/2rem
   scale — mechanical, no layout behavior changes. The one remaining inline
   `style="display: none"` (`#legacy-open-order-controls`) is left as-is
   since it's toggled directly by `updateLegacyOrderControls()`, not a static
   layout value.
4. Legacy Import's mode switch now has a `.muted` hint next to each variant
   ("Using the currently open new order." / "No new order is open — pick an
   existing one instead.").
5. `hx-indicator` added to cart search, "+ Add to order" (via
   `htmx.ajax`'s `indicator` option), Register, "+ New Order", relink search,
   add-card search, and order total/shipping update, backed by a new
   `.htmx-indicator`/`.tx-search-spinner` rule in style.css (opacity-fade,
   htmx's standard convention, `includeIndicatorStyles` already on by
   default).

## 2026-09-30 — Multi-source pricing (epic #213, mainly #210/#212)

**Reviewed:** templates only (nothing rendered), during `architect`'s intake
of the multi-source pricing design. Findings feed #210 (labels, badge,
per-source table) and #212 (flags).

1. **Price movers must exclude source switches.** Cards whose price source
   changed within the period drop out of Price movers, with the caption
   gaining "· N source changes left out". Value-history and per-card price
   charts keep those points but mark them with a tooltip
   ("Source: Dex → Cardmarket") so charts still reconcile with the KPIs.
2. **Price labels are already wrong/inconsistent (fix in #210):**
   `inventory.html:5` and `card_picker.html:24` call the price "Dex's"
   (false since TCGplayer became preferred). Standalone card prices are
   "Price" in `inventory_table.html:51`, `card_detail.html:24`,
   `dashboard.html:78` but "Market price" in `card_picker.html:55`,
   `listing_entry.html:34` — use "Market price" everywhere a price stands
   alone, keep "Price" for transaction prices. Rename the `reference_price`
   sort key to `market_price`. Spell "TCGplayer" (not "TCGPlayer",
   `card_detail.html:46`).
3. **Clear-filter trap:** the hand-built "Clear filter" URL at
   `inventory.html:58` enumerates every filter param; a new "Price needs a
   look" filter must be added there or clearing another filter silently
   drops it.
4. **Where source/age/flags show:** card detail gets a line under the price
   ("TCGplayer via Dex · 2 d ago") plus flag chips, and a per-source table
   (Source, native price, NOK, Fetched, "Used" marker) inside a `<details>`
   open by default only when flagged, replacing the one-line Prices entry
   (lines 45-46). Inventory rows, Most valuable, Price movers, listing
   market price and the sale prefill get only a "!" marker (with `title` +
   `aria-label`) when flagged; the sale prefill also gets a note since a
   wrong price there costs real money. Nothing on Transactions; no source
   column in Inventory.
5. **Flag styling:** one amber/muted style for all flags, red only for "no
   price". "Price needs a look" checkbox next to "Duplicates only"
   (`inventory.html:40-50`); dashboard gets one line "N prices need a
   look →" only when N > 0, not a KPI card.
6. **"Variant uncertain" isn't persisted** — it only lives in the refresh
   result (`card_images.py:49`), so it must be stored before the UI can
   show it (#210).

**Built (#210 part 2), where it differs from the above:** the chart tooltip
uses the full source labels and, on the Market Value chart, a card count
("Source: TCGplayer via pokemontcg.io → TCGplayer via Dex (208 cards)"),
since that chart aggregates many cards. The per-source table's last column
is "Status" (the "Used" marker, "Lookup failed <date>", and the row's own
flag chips); native price only shows for non-NOK sources. The card price
history table gained a Source column. Items 2 (labels, sort key), 4 (card
page, movers `title`) and 6 are done; items 3, 5 and the "!" markers remain
for #212.

## 2026-10-01 — #255 Orders shell UX pass

**Reviewed:** the ux agent's review on issue #255
(https://github.com/erjoha11/PokemonCollector/issues/255#issuecomment-5930505661),
from source only: `base.html` nav, `transactions.html`,
`partials/{card_picker,purchase_cart,kpi_module,listing_entry,listings_results}.html`,
`listings.html`, `listing_mark_sold.html`, `purchase_edit.html`, and the
`/transactions`, `/listings`, mark-sold, add-existing-cards and
`POST /transactions/{tx_id}` routes.

**Decisions:**
- **Header per tab.** Purchased keeps the KPI band + Net invested / Paper
  gain caption + View charts (Net invested's tooltip now says "paid minus
  sales received"). Sold shows only figures read off the sale rows — Sold
  for · Fees & shipping · Net received (#254's net proceeds, which landed
  first) · N sales / N cards — and a muted "Card quantities update at the next Dex sync." No
  gain there until #256: `economic_summary`'s net invested is bought −
  sold while `qty` lags until the sync, so Paper gain jumps after Mark sold
  and would read as realized profit on a "Sold" tab. Listings: no money
  header, just "Sell on finn.no", the filters and "N active".
- **Manual sale kept** as a secondary "+ Record sale without listing" on
  Sold (in-person / other-platform sales have no listing). Its cart has a
  hidden `type=sale` input instead of the select and no "Show cards
  without an order"; the Purchased cart loses its Sale option.
- **Tab label and markup.** "Purchased" (not "Purchased & acquired") with a
  muted subtitle "Purchases, trades and ripped packs" and `?type=` pills.
  One h1 "Orders", `<nav class="tabs" aria-label="Orders">` of plain links
  (no `role="tablist"` — no arrow-key behaviour to promise), active tab
  `aria-current="page"` + weight + bottom border, all inside
  `#main-content`. The "Orders" nav item also lights up on `/sales`,
  listing edit/mark-sold and Edit order.
- **Silent-break pitfalls flagged (all handled in the build):** hardcoded
  `/transactions` in every `sort_th` call and `_pick_url` (a sort click on
  Sold would 308 to Purchased); one function deciding an order's tab for
  both the list and every redirect, Sold only when every row is a sale
  (else the total form's `hx-select="#order-N"` swaps in nothing); cart
  Register / Edit order landing on the other tab must `HX-Redirect`, not
  swap; the old `/listings` "any HX-Request = fragment" heuristic must key
  on `HX-Target == "listings-results"`; the picker's "Adding to" listed
  sale orders and add-existing-cards wrote purchase rows into them (now
  filtered + rejected server-side); in-app links must point at the order's
  own tab rather than lean on the 308; mark-sold → `/orders/sold?open_order=N`,
  Cancel keeps the Listings filters; the cart JS read
  `select[name="type"]`, which throws with a hidden input (now
  `form.elements.type`, moved to `static/orders-cart.js`); a single-row
  type edit that moves a row says "Moved to …".

**Status:** Addressed in #255 (PR "Orders shell: route-based tabs,
redirects, single nav item"). Realized gain on Sold is #256.

## 2026-10-01 — One error pattern for rejected form saves (#228 a)

**Reviewed:** a `ux` review of how a server-side validation error should
reach the user on the htmx forms (cart, Order history total/shipping, row
edit, Edit order) and the plain listing forms. Before, htmx dropped any 4xx/5xx,
so a rejected save looked like a button that did nothing. Only Edit order
had its own inline `hx-on::response-error`.

**Chosen pattern (built in #228 part a):**
- One global `htmx:responseError` listener in `static/form-errors.js`
  (loaded from `base.html`). It finds the slot with
  `elt.closest('form').querySelector('[data-form-error]')`, or else the one in
  the nearest `.card`. On a 422 it unhides the slot and sets
  `textContent` to the server's plain-text message. Any other status gets
  the generic "Could not save (error N), nothing was changed..." text. Then
  `scrollIntoView({block: 'center'})`. The slot is cleared and re-hidden
  on the next `htmx:beforeRequest`. GET requests inside the same form,
  such as the cart's card search, are ignored so they neither wipe a shown
  error nor report "Could not save".
- Nothing is swapped, so the user's input is preserved (the row edit stays
  in edit mode, and the cart keeps its rows).
- Slot markup: `<p class="warnings" role="alert" hidden data-form-error></p>`.
  It reuses `.warnings` with no new class. Inside a `form.filters` flex row
  it gets an inline `flex-basis: 100%` so it takes its own line. Slots:
  above Register in the cart, in the order total/shipping form, inside the
  row-edit `<form>`, above Save changes on Edit order (replacing
  `#save-order-error`), and in the ad builder and Mark as listed forms.
- Server messages are short and English (the UI language) and name the
  field and row, e.g. "Price on row 3 must be a number of 0 or more."
  FastAPI's own JSON 422 is converted to plain text for HX requests, so the
  slot never shows raw JSON.
- Plain (non-htmx) forms keep their pattern: re-render with
  `{% if error %}<p class="warnings" role="alert">`, status 422, every
  submitted value filled back in.
- Pitfall handled: `confirmRegisterOrder` turns the cart's beforeunload
  guard off before the request. `static/orders-cart.js` turns it back on
  after `htmx:responseError`/`htmx:sendError`, so a rejected Register
  can't later lose the cart without a warning.

**Status:** Addressed in #228 (part a). Not yet checked by hand in a
browser, only through route and template tests.

## 2026-10-02 — Card page as modal (#280)

**Reviewed:** the `ux` input to the architect's design for issue #280
(open the card page as a window over the current page instead of
navigating away), covering templates/card_detail.html, partials/macros.html
(`card_link`, `card_view_attrs`, `chart_card`), partials/card_viewer.html +
static/card-viewer.js, partials/kpi_module.html, collection.html,
partials/missing_cards.html, partials/card_delete_missing.html,
partials/missing_card_delete_form.html and base.html.

**Findings:**
- **Native `<dialog>` with progressive enhancement.** Use `showModal()`
  (focus trap, Esc, inert page for free) and keep `/cards/{id}` a real full
  page. Intercept only a plain left click (button 0, no ctrl/meta/shift/alt):
  ctrl/cmd-click, shift-click and middle-click must still open the full page
  in a new tab/window, and links must work with JS off.
- **One body partial, a separate `/panel` URL.** The modal shows the full
  card content from the same partial as the page, so the two can't drift.
  Serve it at `/cards/{id}/panel`, not `/cards/{id}` varied on
  `HX-Request`: same-URL variants risk the browser cache serving the
  fragment as the full page (Back/restore showing an unstyled fragment).
- **Hook on `card_link`, not a generic selector.** Add `data-card-modal`
  in the macro and convert the hand-written card links (collection gallery,
  price movers, best card, Missing from Dex, Recently added). A generic
  `a[href^="/cards/"]` would also match `/cards/{id}/delete-missing`.
- **Fold the photo lightbox into the modal.** Photo clicks open the same
  detail modal (their data attributes paint the placeholder); on the card
  page the photo zooms in place. Never a dialog inside a dialog.
- **No `pushState`.** htmx 1.9.12's popstate handler restores its own
  snapshot for any `htmx:true` history entry, and Inventory's filters push
  those, so popping a modal entry would re-swap the page and lose exactly
  the state the feature exists to keep. Android Back already fires `cancel`
  on a modal dialog and closes it; the iOS edge-swipe gap (it navigates) is
  accepted. An "Open full page" link covers sharing/bookmarking.
- **Charts.** `chart_card`'s inline `initTcgChart(...)` throws on pages
  without Chart.js: guard it. Lazy-load Chart.js once, *after*
  `showModal()` (a hidden canvas measures 0 wide), and destroy charts and
  empty the body on close so stale ids/instances don't linger.
- **Stale responses.** Quick successive clicks: an `AbortController` must
  cancel the previous request so card A's late response can't overwrite
  card B.
- **Backdrop close.** Close only when both mousedown and click land on the
  backdrop: a scrollbar click or a text selection dragged out of the dialog
  must not close it.
- **Focus and structure.** Focus moves in on open and returns to the
  triggering link on close (explicitly, Safari doesn't); `aria-labelledby`
  on the `<h2>` title; page scroll locked while open; sticky header with
  title, set/variant/language, Open full page and close X.
- **Small screens and overflow.** At 560px and below a full-screen sheet
  (`100dvh`, no radius) with the image capped so the KPIs are visible
  without scrolling; the transactions table scrolls horizontally; `info()`
  tooltips open downward inside the modal so they aren't clipped.
- **Loading and error states.** Instant placeholder (photo data or the link
  text) with `aria-busy`. 404: "This card no longer exists" + Close.
  Network/5xx: "Could not load the card" + Open full page + Close. Expired
  session: the fetch follows `auth_guard`'s 303 to `/login`; detect it and
  navigate to `/cards/{id}` rather than showing the login form in the modal.
- **Delete-missing inside the modal.** A successful delete sends
  `HX-Trigger: cardDeleted` so the row underneath on `/sync-status`
  disappears; the panel's message is "Card deleted." with Close. The delete
  form lacked a `data-form-error` slot (a gap in the #228 convention), so a
  failed delete was silent on both page and modal.

**Status:** Addressed in this PR (#280). Built as described; the photo
lightbox was removed (product decision noted in the PR — can be brought
back on request). Verified through route/template tests only; the
browser-only behaviour (focus return, backdrop/scrollbar handling, Android
Back, chart sizing, mobile sheet) has not been checked by hand.

## 2026-10-02 — Sync connector / aborted-sync override UX (#277/#275)

**Reviewed:** `ux` guidance for the Dropbox sync connector and the
override path when a manual sync aborts on its safety check (issues #275
and #277), against the existing htmx 1.9.12 setup on `/sync-status`.

**Findings:**
- **htmx 1.9.12 drops 4xx responses**, so a handled outcome (sync done,
  aborted by the safety check, nothing to do) must come back as a 200 with
  the rendered state, not an error status — otherwise the button looks like
  it did nothing. Add a timeout/network-error fallback message for the
  cases that really fail.
- **The safety-check override appears only right after a manual run
  aborts**, in that run's result, never as a standing control on the page.
- **Confirmation is a required-checkbox form, not `confirm()`**, the same
  pattern as the Missing from Dex delete.
- **The override posts `expected_missing`** (the count the user approved),
  and the server refuses if the export now would flag more cards than that,
  so a changed export between abort and override can't flag more cards than
  were approved.

**Status:** Open (design guidance for #275/#277, not yet built).

## 2026-10-08 — #366 master set / Sets & lists module (ux, via architect)

Status: **partly superseded by #378** (see the 2026-10-08 #378 entry below). The user overruled the "separate set page" and "don't bring back `/collections`" advice: the master-set content now lives on `/collections/{id}`, and "Sets & lists" became "Collections". The other findings still apply.

Pass 1 (set page):
- *(Superseded by #378.)* New page `/sets/{language}/{set_code}`, not a tab on the collection page: a collection is a tag, not a checklist, and one set can span several collections. Entry points: a "Master set" link on `collection.html` set headers and a set-name link on `card_body.html`'s Series/set line, shown only when a checklist exists. Nothing on the Dashboard (#241 removed completion there by the user's choice).
- Progress as separate tracks, each a strict X/Y over checklist slots (Main 1-165, Secret 166-210, Poké Ball, overall Master set), so it can't go above 100%. Show "X / Y" before the %, with units explained in `info()`. Relabel `collection.html`'s per-set "N% complete" (e.g. "X/210 numbers") so two different 151 completion figures don't sit side by side.
- Must-have: an "Unmatched" list of owned cards that don't map to a checklist slot, or the page silently disagrees with the collection.
- Missing tiles are ghosted, not links (no Card id, so `card_link` would give `/cards/None`), and must look different from the existing "0 owned" (sold) style.
- `ads.py` prints `card.language`, so Korean cards logged as Japanese were advertised as "JP" (addressed by #367).

Pass 2 (lists module):
- Spares must be counted per master_card (sum(qty) − 1), not per Dex row; otherwise the set page and a sale list's "Not enough spares" can disagree.
- *(Superseded by #378.)* Don't name the new nav item "Collection" (clashes with Dex collections). Use "Sets & lists" (`/collecting`, active on `/sets/` and `/lists/`); don't bring back the `/collections` index removed in #252.
- Bulk add from the set page: filter-aware, server-side "Add N missing/spares to [list ▾]" buttons with a real count and a result line ("Added 31, 3 already on list"). No third sessionStorage multi-select; the htmx target must always exist so it can't fail silently like "+ Add to order".
- The sale list is the intent stage feeding the existing `/sales` → `/listings` flow ("Make finn.no ad from this list"; "Listed" badge read from `listing_cards`). It must not become a second ad generator.
- List totals: want = "Est. cost to complete", sale = "Est. value of spares" (capped at spares). Copy-as-text uses the checklist display name.
- A want item for a print that's owned but Unmatched would wrongly show "Missing". Surface it as "Possibly owned (unmatched)" or link to the set page's Unmatched list.

## 2026-10-08 — #378 Collections as the entry point (architect + ux design, built by developer)

Context: after seeing "Sets & lists" the user said he wanted the old collections page ("i wanted /collections/6") and a page per collection to check progress, missing cards and duplicates. He thinks in Dex collections, not sets. This knowingly reverses #252 (removed the `/collections` index) and the "separate set page / no `/collections`" advice in the #366 entry above, which is marked superseded.

Design as built:
- Nav: "Sets & lists" → **Collections** (`/collections`), active on `/collections`, `/collections/…`, `/lists/…` and the `/sets/…` fallback. Inventory stops lighting up on `/collections/`, so only one item is ever active.
- `/collections` overview: one table (Collection, Cards owned, Total value, Master set X/Y · % for a set's home collection else "–", Duplicates), then the want/sale lists and "New list". Empty collections aren't hidden; stale data (e.g. "Venter", #375) is fixed in the data, not by the UI.
- **Home rule**: a checklisted set's home is the collection tagged on the most of its owned cards, ties to the lowest id. Computed per request, nothing stored. Only the home section shows the full master-set block. Other collections holding the set show their gallery plus one line ("Master set 275/363 → <home>"), so a small Illustrator collection never grows a 363-tile grid.
- **Counts are set-wide**, labelled in the heading ("Master set · all your sv2a cards"). Counting only the tagged cards would show a print filed in another Dex folder as Missing, which would then go on a want list and could be bought twice. Owned tiles not tagged with the collection get an "in: <other collection>" badge.
- **Duplicates** is the user's word, so it's the label, with the per-print definition (#366's spares) and the tooltip "every copy beyond the first of each print". A new Show = Duplicates pill. In checklisted home sections the per-row figures (qty − 1 per Dex row, Unique cards, "X/210 numbers", `?owned=0`) are dropped, so two conflicting duplicate figures never sit side by side. Sale lists keep "spares" for now.
- Functional risks handled: every id in a block is suffixed with the set key (`#set-grid-ja-sv2a`, `#set-list-add-…`, `#missing-list-text-…`, `#spares-…`, `#unmatched-…`), so hx-select / hx-target / Copy act on the right section. One active filter per page (`?set=ja:sv2a&track=…&show=…`), and the other blocks render at the defaults. Pill hrefs point at `/collections/{id}`. The no-htmx Add-to-list redirect goes back to the collection. Breadcrumbs and the list delete redirect point at `/collections`.

Status: built in #378, awaiting the user's review on the Vercel preview.

Fast-follows (not built): a manual home override if "most cards" ever picks wrongly; "Spares" → "Duplicates" on sale lists; deleting `master_set.html` once the zero-owned fallback is judged unnecessary. Not in scope: an "In transit" status for Dex's Incoming folder (see the issue's comment; the app keeps ignoring Incoming, #311).

## 2026-10-08 — "On the way" status for paid-but-not-received cards (#382, ux via architect)

**Reviewed:** (design, recorded in issue #382) where an in-transit card should show, and how it should behave on the sale-facing pages. Inventory (`partials/inventory_table.html`, `inventory.html`), the card page (`partials/card_body.html`), the collection gallery (`collection.html`), `/sales`, want and sale lists (`card_lists.py`), the master-set block (#378), the Dashboard, and the Facebook wins cart (`fb_win_candidates.html`).

**Findings / decisions:**
- Principle: an in-transit copy counts as **owned** (value, dashboard, snapshots, completion, want-list matching) but is **never available** (sales, ads, sale lists, spares).
- Badge: a `transit-badge` built on `.tx-platform-badge` (the neutral metadata pill, a normal state, not an error), reading "On the way". Tooltip: "Tagged Incoming in Dex · seen since <date> (N days)". After ~21 days it shows its age ("On the way · 24 d") in warning style, as a guard against a forgotten tag. Places: Inventory rows, the collection gallery, and the card page (Owned "N (on the way)" plus an "On the way" dt with the date).
- No "1 of 2 on the way": Dex's Incoming qty mirrors the card's total, so the status is all-or-nothing (verified on a real export).
- Inventory: an "On the way only" checkbox next to "Duplicates only". It must also be in the hand-built rarity "Clear filter" URL, or clearing drops it.
- Dashboard: one line, "N cards on the way · X kr", linking to that filter, shown only when N > 0.
- Sales and ads: **block, not warn** (unlike #257's "Listed" badge). The Inventory `.sale-select` is disabled with a tooltip for fully in-transit cards, `/sales` mutes partly in-transit rows with qty capped at in-hand, and the server clamps too.
- Want lists: a new "On the way" status (owned >= wanted but in-hand < wanted), excluded from "Copy as text" and skipped by "Remove got it". Sale lists: an "On the way" status before "short".
- Master set: in-transit tiles look owned with a small "On the way" pill. Duplicates (KPI, pill filter, table) use in-hand spares, with a muted "+1 on the way" note.
- Wins cart: badge in-transit candidates and rank those on the way since the sale ended with or before "new since the sale" (also catches a 2nd copy, which `created_at` misses). New empty-state copy: "No matching card yet — it appears after you raise its qty in Dex (tag it Incoming until it arrives) and the daily sync runs."

**Status:** Partly addressed. Pass 1 (draft PR for #382): the badge on Inventory and the card page, the filter, the Dashboard line, the sales/ads block, and the wins cart. Open until pass 2 (after #378 merges): the collection gallery badge, want-list and sale-list statuses, and master-set in-hand Duplicates.
