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

**Status:** Quick wins (1-5) Addressed 2026-09-20, same session — Top
finding (Edit Order htmx conversion) and Medium items (6-8) still Open,
deliberately out of scope for that pass:
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
