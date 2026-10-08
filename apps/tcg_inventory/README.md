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
  Dashboard, Inventory and the Orders page's Purchased tab, ordered by what a collector wants
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
  The Price movers tile is a **manual carousel** (issue #279,
  `static/kpi-carousel.js`) with a second slide, **Recently added**
  (`queries.recently_added`): the 10 owned cards (qty > 0) with the newest
  `Card.created_at` — the date the card first came in from a Dex
  sync/import, the same added date the Orders card picker's
  `?pick=recent` filter uses. Cards with no known added date (from before
  that column existed) are left out; ties break on id, newest first. Each
  row shows a thumbnail and name (both open the card modal, see Card
  page below) and language, set · number · variant · added date, and today's
  price. Switch with the arrows or the dots under the tile (dots are a
  tablist: arrow keys/Home/End move between them); there's no
  auto-rotation. The chosen slide is remembered per browser in
  `localStorage` (default Price movers). Without JavaScript the tile is
  just Price movers.
  The collection/series cards share one stat grid: Unique value |
  Unique Cards, Duplicate value | Total Duplicates, then Net invested and Gain.
- **Card page** (`/cards/{id}`) — one card: image, collections, binder,
  variant, language, prices, gain, every transaction (with order links) and
  its price history (`queries.card_price_history`, one point per snapshot
  day). Every card name in the app links here (`card_link` in
  `partials/macros.html`); Dex is a link on this page. Replaced name → Dex
  links (24.09.2026).
  **Opens as an in-page modal** (issue #280): a plain left click on a card
  name or photo anywhere in the app opens the card in one shared native
  `<dialog id="card-modal">` (in `base.html`) instead of navigating, so
  closing it (X, Esc, a backdrop click, Android Back) leaves the page
  exactly as it was — scroll, filters, open rows, an in-progress edit or
  picker selection. `/cards/{id}` stays a real full page: ctrl/cmd/shift/
  middle-click, bookmarks, the modal header's "Open full page" link and
  no-JS all go there. How it fits together:
  - `partials/card_body.html` is the one body both shells render.
    `card_detail.html` adds the breadcrumb/`<h1>`; `partials/card_panel.html`
    (`GET /cards/{id}/panel`) adds the modal's sticky header (title,
    set · variant · language, Open full page, close X). Both share
    `app._card_detail_context`. A separate URL rather than `/cards/{id}`
    varied on `HX-Request`, so the browser cache can never serve the
    fragment as the full page.
  - `static/card-modal.js` intercepts clicks on `[data-card-modal]` only —
    added by the `card_link` macro and `card_view_attrs` (photos, which also
    carry name/meta/price/image for an instant placeholder). It never
    matches `a[href^="/cards/"]`, which would also catch
    `/cards/{id}/delete-missing`, so **every card link must go through
    `card_link`** (a test scans the templates for hand-written ones). It
    fetches the panel (an `AbortController` drops a stale response),
    inserts it and runs `htmx.process()`; 404 shows "This card no longer
    exists", a network error/5xx "Could not load the card" with Open full
    page, and an expired session (the fetch followed `auth_guard`'s 303 to
    `/login`) navigates to `/cards/{id}` instead.
  - No `pushState`/`hx-push-url`: htmx 1.9's popstate handler would re-swap
    Inventory's own history snapshot and lose the page state. Android Back
    closes the dialog via `cancel`; iOS edge-swipe still navigates (accepted).
  - Chart.js is loaded lazily after `showModal()` on pages that don't load
    it (the inline `initTcgChart` in `chart_card` is guarded); charts are
    destroyed and the body emptied on close.
  - The delete-missing form (see Sync status → Missing from Dex) works in
    the modal; a successful delete sends `HX-Trigger: {"cardDeleted":
    {"id": N}}` and the script removes that card's row from `/sync-status`
    underneath. The panel's success message is "Card deleted." with Close.
  - **The old `#card-viewer` photo lightbox is removed**
    (`partials/card_viewer.html`, `static/card-viewer.js`): a photo click
    opens the card modal, and on the card page itself (full page and modal)
    the photo zooms in place (`data-card-zoom`), so there's never a dialog
    inside a dialog.
  - Layout: about `min(1000px, 100vw - 32px)` wide and scrolling inside the
    dialog on desktop; a full-screen sheet at 560px and below with the photo
    capped so the KPIs show; tables scroll horizontally; `info()` tooltips
    open downward inside the modal.
- **Collection gallery** (`/collections/{id}`) — reached from the collection
  links in Inventory and on the Card page (the `/collections` index page and
  its nav item were removed in #252; the Dashboard's Inventory table shows
  the same membership rows). A per-collection gallery grouped by set with value,
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
- **Orders** (`/orders/purchased`, `/orders/sold`, `/orders/listings`;
  issue #255) — one nav item ("Orders") for what used to be three
  (Transactions, Sell on finn.no, Listings). One h1 and a tab strip
  (`partials/orders_tabs.html`: `<nav class="tabs" aria-label="Orders">`,
  plain links so the in-progress-cart `beforeunload` guard fires, active
  tab `aria-current="page"`, inside `#main-content` so swaps keep it). The
  nav item is also active on the pages reached from the tabs (`/sales`,
  `/listings/{id}/edit`, `/listings/{id}/mark-sold`,
  `/transactions/purchase/{id}/edit`).

  **Which tab an order is on** is decided by one function,
  `app.order_tab`: **Sold** only if *every* row is a `sale`, otherwise
  **Purchased** — mixed orders included, so nothing ever drops off both
  tabs. An individually registered row follows the same rule by its own
  type. The tab lists and every post-write redirect (total/shipping form,
  cart Register, add-existing-cards, Edit order, `POST /transactions`,
  mark-sold) use it via `_order_redirect`, which lands on
  `/orders/<tab>?open_order=N`; when an htmx write comes from a different
  tab than the order now lives on (`HX-Current-URL`), it answers
  `HX-Redirect` instead of swapping Sold content into a Purchased URL. A
  single-row edit (`POST /transactions/{id}`) that moves a row to the
  other tab says "Moved to Sold/Purchased" in the row.

  **Old URLs** 308 to the tabs with the full query string:
  `/transactions` → `/orders/purchased` (`?open_order=N` survives),
  `/listings` → `/orders/listings` (`show_delisted`/`sold_only` survive),
  `/transactions/charts` → `/orders/charts`, `/analyse` and `/orders` →
  `/orders/purchased`. The write routes (`/transactions/purchase/...`,
  `/transactions/{id}`, `/listings/{id}/...`) keep their paths.

  - **Purchased** — purchases, trades and ripped packs: the KPI band, the
    Net invested ("paid minus sales received") / Paper gain caption, the
    order list (its per-order Gain column is labelled **Paper gain**),
    individually registered rows, the card picker (its "Adding to" list
    offers only Purchased-tab orders) and "View charts". `?type=` pills
    (All · Purchases · Trades · Ripped, each with its order count; an
    order with any trade row is a trade, all-ripped is ripped, anything
    else a purchase — `app.order_kind`); an unknown value means All. The
    "+ New Order" cart offers Purchase (default), Trade and Ripped — no
    Sale.
  - **Sold** — sale orders only. Header: Sold for · Fees & shipping ·
    Net received (`queries.net_proceeds`, #254: price − fees − seller-paid
    shipping share) · Realized gain · N sales / N cards, plus "Card
    quantities update at the next Dex sync." Never Paper gain — it would
    jump right after Mark sold, since net invested already subtracts the
    proceeds while qty only drops at the next sync.

    **Realized gain** (#256, `queries.realized_gains`, computed at page
    load, never stored) is per sale row: net proceeds minus the cost of the
    copy sold. The copy sold is the oldest acquisition of that card
    (purchase, ripped, trade "in"; by date, then id) acquired on or before
    the sale date and not already used by an earlier disposal — sales and
    trade "out" rows both use one up (FIFO). That is the same convention
    as `held_acquisition_ids`, which treats the newest copies as held, so
    a copy that shows as sold on its purchase order is the one costed
    here. A purchase copy costs price + fees + its shipping share (what it
    added to Net invested); a ripped copy costs 0. Unknown cost — no
    matching acquisition, a purchase still at price 0, or a traded-in copy
    (its cost was cards, not cash) — shows "unknown cost" and is left out
    of every sum rather than counting full proceeds as gain; a sum that
    left rows out is marked "*" with a tooltip. Choosing a specific copy
    per sale is out of scope (a later additive `cost_basis_tx_id` column
    could do it).

    Sold rows: Order, Date, Listing (title linked to the Listings tab,
    "—" for sales without one), Qty, Sold for, Fees & shipping, Cost
    basis, Realized gain (sign spelled out, not colour alone), Platform.
    Expanded orders (and the individually registered section) add a
    per-card table: sold for, fees & shipping, which copy was used (type,
    date, linked order) and its cost and gain. "Sell on finn.no" (link to
    `/sales`) and "+ Record sale without listing" (the cart with
    `?type=sale`: hidden type input, no "Show cards without an order") for
    in-person/other-platform sales. A sale order's Total field is the
    amount received. The "Individually registered" section is hidden when
    empty.
  - **Listings** — see "Listings" below; no money header, a "Sell on
    finn.no" button and an "N active" count.

  The cart + card picker JS lives in `static/orders-cart.js` (shared by
  both order tabs; reads the cart type with `form.elements.type`, which
  works for the Purchased select and the Sold hidden input alike), with
  the even split it shares with the Facebook wins panel in
  `static/money-split.js` (DOM-free, so `tests/test_money_split.py` runs it
  under node).

- **Orders → Purchased** (`/orders/purchased`, was `/transactions`) — laid out as **Order history first**,
  then individually-registered rows, then one card picker, then a
  collapsible "View charts" section with the value-growth and cash-flow
  charts (formerly the standalone Analyse page).

  **Facebook wins to register** (#309) sits between the KPI cards and Order
  history whenever the inbox holds pending items (see "Facebook wins inbox"
  below): one entry per won sale (a sale becomes one order, so it's keyed by
  sale, not seller; the seller is shown for context), with its end date,
  item count, the known total plus "+ ?" when some prices aren't known, the
  seller's shipping/payment terms, and each lot linked to its Facebook
  comment. Each lot has **Ignore** (a cancelled or duplicate win): it leaves
  the list for good, and later sends never bring it back. **Open in cart**
  on a sale opens it in the New Order cart, prefilled, to link its lots to
  cards and register it as one order (see "Facebook wins inbox" below for
  the cart's panel). A lot registered as "not complete" stays listed with
  "lot not complete · order #N" and a **Lot complete** button; one whose
  order no longer exists shows "order #N missing" instead of disappearing.

  **Order history** is the page's primary content, directly under the KPI
  cards: one row per order with the numbers that describe the deal — Order
  #, Date, Qty, Value (sum of recorded per-card prices, trades excluded),
  Shipping, Total, Paper gain, Platform. **Total** is the amount paid for
  the whole order (`purchase_total`, stored on every row of the order).
  When none has been typed/saved, the column shows the automatic
  `Value + Shipping` (`auto_total` from `_group_transactions_by_purchase`),
  muted and tagged "auto" — display-only, never stored — and no Remaining
  is shown for it: an automatic total is by definition fully accounted
  for, so ✓ would claim a reconciliation nobody did. A typed Total
  overrides it, is saved, stays put (never recalculated), and drives
  Remaining: ✓ when card prices + shipping add up to it, otherwise the
  flagged difference. Remaining used to be its own summary column; since
  #246 it shows inside the expanded order, next to the Total form (the
  backend `diff` is unchanged). Net invested and the headline gain never
  used the Total, so they're unaffected either way.

  **Gain** (#246, `queries.order_gain`) is the order's paper gain/loss:
  today's market value (`display_price`) of the order's copies you still
  own, minus the Total the row shows (typed, else auto). One transaction
  row is one copy; when a card has more acquisition rows (purchase,
  ripped, trade "in") than its current `qty`, the newest rows are the ones
  treated as still held (`queries.held_acquisition_ids`, disposals assumed
  oldest-first), so a copy sold or traded away contributes no value and
  its order shows its cost as a loss. A trade order adds the expanded
  order's "Trade gain" (got − gave + cash, today's prices). An order
  mixing sale and purchase rows (listed on Purchased) shows "—", as does an
  order where none of the held cards has a market price; all-sale orders
  are on the Sold tab, which shows realized gain instead; when only some lack
  one they count as 0 and the figure gets a "*" with a tooltip. Gains
  don't sum exactly to the headline Paper gain/loss, which also counts
  copies with no order row (individually registered cards, qty beyond the
  registered rows, cards with no transaction at all), credits sale
  proceeds, includes fees, and uses recorded card prices + shipping rather
  than a typed Total; it also values trade-in cards at full value with no
  cost rather than netting off what was given.

  Expanding a row reveals that order's cards, its
  total/shipping/platform form (Total left empty when nothing's saved,
  with the auto figure as its placeholder, so "Set total/shipping" never
  persists the auto sum by accident) and an "+ Add cards to this order"
  button. In that form a blank Total clears it (back to auto), but a blank
  Shipping or Platform means "leave as is" — only a typed value overwrites
  every row of the order (enter 0 to zero shipping; per-card platforms are
  set in Edit order). So a mixed-platform order, whose Platform field
  prefills blank and reads "Mixed — blank keeps per-card", keeps each
  card's platform when you save a Total. The New Order cart works the same way: its Total field's
  placeholder shows the live Σ(card prices) + Shipping as "auto" while
  it's blank (trade/ripped prices aren't cash, so only shipping counts
  there); only a value you actually type is submitted, and clearing the
  field goes back to auto. Each order stays a `<details id="order-N">`: that's
  load-bearing, since `?open_order=N` deep-links by rendering `open` on it
  and the total/shipping form swaps that same element
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
  by reassigning Order ID ("Start new order" blanks a row's Order ID;
  the new order's ID is assigned on save, see "Order IDs" below) — all
  edits in a group commit atomically, and
  moving a row out of an order clears that row's Total/shipping
  rather than guessing how to split it (set the destination order's
  total/shipping afterward). Facebook wins items registered on the order
  follow its rows (see "Facebook wins inbox" below). Save keeps you on the edit page with a
  "Saved ✓" note and a "← Back to Transactions" link (unless every row
  was moved out, which lands on Transactions); a failed save shows an
  error above Save changes instead of silently doing nothing (a rejected
  value names the field and row, see "Form validation" below). Total defaults to shipping + the
  cards already priced (price 0 = not priced yet) when nothing's been
  saved yet, but a saved value is a real number the user typed and is
  never silently recalculated back to the sum. (Save changes submits
  whatever's in that field, so saving the edit page with the default
  untouched does persist it as the order's Total — unlike Order history's
  and the cart's blank-means-auto field.) A "Distribute remaining
  across unpriced cards" button (client-side, same pattern as the New
  Order cart's "Distribute evenly", which splits evenly with the last
  empty row taking the rounding so the shares add up exactly, #312:
  100 over 3 is 33.33 / 33.33 / 33.34, `static/money-split.js`) fills `Total − Shipping −
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
  `Total − Σ(already-priced cards)`: in a lot, the cards you priced
  keep their price and the unpriced ones absorb the rest *including*
  shipping. It then sets Shipping to 0 in the form, since shipping now
  lives in those cards' prices and would otherwise be counted twice in Net
  invested. With it off, shipping stays recorded on the order, and each
  purchase row carries a share of it split by price (`queries.shipping_shares`; evenly when nothing in the
  order is priced yet). That share is shown under the row's price, and it
  counts in the card's Net paid (`net_invested_by_card`) and in Net
  invested, so a 25 kr card with 38 kr shipping shows as having cost 63 kr.
  **Sales count at net proceeds** (issue #254): a sale row brings in
  `price − fees − its share of the order's seller-paid shipping`
  (`queries.net_proceeds`), and that, not the gross price, is what
  `economic_summary`, `net_invested_by_card`, `cash_flow_by_month` and
  `net_invested_at_dates` subtract. Despite its name, `purchase_shipping`
  carries **any** order's shipping — on a sale order it's the shipping the
  seller paid. It's split by `shipping_shares` exactly like a purchase
  order's: across the order's purchase and sale rows by price (trade/ripped
  rows get none), evenly when none is priced, a 0-price row among priced
  ones gets nothing. (No separate column or rename — `init_db()` is
  additive-only.) Every Net invested figure is built from the one rule
  `queries.net_invested_amount`, so `sum(net_invested_by_card) ==
  economic_summary["net_invested"]` still holds and the cash-flow chart's
  cumulative line ends on the same number (purchase shipping now counts
  there too). The New Order cart has an optional order-level **Fees**
  field (purchase and sale orders; hidden and ignored for trade/ripped),
  split across the rows' per-row `fees` by price in whole øre with the
  leftover øre going to the rows with the largest rounding remainders, so
  the stored fees add up to exactly what was typed. A single ungrouped
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
  from the Listings tab (see below), which is the only listing action that
  writes `Transaction` rows. Reached from the Orders page's "Sell on
  finn.no" buttons, which carry no selection of their own: if Inventory's
  selection is still in sessionStorage (`tcg-sale-list`), `/sales` offers
  a "Continue with the N cards selected on Inventory" link.
- **Listings** (`/orders/listings`, was `/listings`) — overview of every recorded `Listing`: its
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
  Only a request with `HX-Target: listings-results` (the filter form's own
  hx-gets) gets the bare results fragment — any other request, htmx or
  not, gets the full tab, so an `hx-select="#main-content"` swap of it
  works. Empty states tell "no listings yet" from "nothing matches these
  filters". Mark sold lands on `/orders/sold?open_order=N#order-N`; its
  Cancel (and edit/delist/delete without htmx) return to the Listings tab
  with `show_delisted`/`sold_only` kept.
- **Sync status** (`/sync-status`, issue #264; formerly the Activity Log
  at `/releases`) — nothing runs from this page. First an
  at-a-glance block with one card per background job (Dex sync, Price
  refresh, Set sync, Image backfill): its last successful run (time,
  trigger, and a one-line summary — the card counts for a Dex sync), plus
  its last empty/aborted/failed run *only* when that is newer than the last
  success. Then **Missing from Dex**: every card a sync has flagged
  missing, with a guarded delete for one registered in error (see "Sync
  status" below). Then the **Run log**: the 30 most recent runs of any job,
  sortable per column (`lsort`/`ldir`; sorting swaps just the log via
  `GET /sync-status/log`). Dex-sync rows show files, created/updated/flagged
  counts, warnings (click the count to see the warning text) and touched
  collections/binders; other rows show their summary or error under
  Details. See "Sync status" below for what gets recorded. `GET /releases`
  and `GET /import` 308-redirect here and `GET /releases/sync-log` to
  `/sync-status/log`, keeping any query string. There is no manual
  CSV-upload page or Dropbox file picker; see "Dropbox import setup" below
  for the only way to sync outside the cron.

## Money figures (glossary)

One place for what each money figure means. This used to live on the in-app
Wiki page (removed in issue #264). The sections linked below have the
implementation detail.

- **Net invested**: everything paid for cards (card prices, shipping and
  fees included) minus what sales brought in, counted at net proceeds
  (`price − fees − its share of seller-paid shipping`, #254). One rule,
  `queries.net_invested_amount`, feeds every Net invested figure (KPI band,
  Orders caption, card page, cash-flow chart, the chart's Net invested
  line). Trade and ripped rows carry no cash.
- **Gain / loss** (also **Paper gain/loss** on Orders): total value,
  duplicates included, minus Net invested. This is the one gain definition
  app-wide: the Market Value hero, every table's Gain/loss column,
  Inventory's per-card Gain, the card page and the Most valuable
  collection/series cards all use it. The Market Value chart's stat row
  only shows it on the Total metric.
- **No purchase price**: an owned card with no registered transaction has
  no known cost, so its whole value lands in the gain. The line under the
  gain equation (and every bucket-level Gain) says how many such cards
  there are and how much of the gain they make up
  (`no_cost_count`/`no_cost_value`).
- **Per-order Paper gain** (Orders → Purchased, `queries.order_gain`):
  today's market value of the order's copies you still own, minus the
  order's Total (typed, else auto). Copies sold or traded away count as 0;
  when a card was bought in several orders, the newest orders are the ones
  treated as still holding it. Trade orders add their trade gain. "—" when
  none of the held cards has a market price (or the order mixes sale and
  purchase rows). When only some lack a price, they count as 0 and the
  figure gets a "*". Per-order gains don't add up to the headline Paper
  gain/loss; see "Orders → Purchased" above for why.
- **Above / below cost**: each owned card's value today (all copies) vs
  what you paid for it, since purchase. Only cards with a registered cost
  count; a ripped card counts as up by its full value.
- **Price movers**: price per copy today vs the daily snapshot from the
  start of the period (30 days back, or the earliest snapshot while history
  is shorter). That's a different baseline from Above / below cost, so the
  up/down counts differ. The % is hidden for moves under 10 kr.
- **Recently added** (Price movers tile's second slide): today's market
  price per copy, not a change — no baseline.
- **Market Value chart change**: first to last point of the chart
  ("since first snapshot DATE" on All), not since you bought the cards.

## Shared UI conventions

- **Sorting**: every table sorts the same way. Click a header for
  ascending, again for descending (▲/▼ shows the direction). In tables with
  expandable rows (collections, series, rarity) a sort only reorders the
  cards inside a row; the rows themselves keep their fixed order. Flat top
  lists (Pokemon Top 10) reorder the rows. Most valuable cards has no sort:
  always market price, highest first. A column whose value isn't
  unambiguous per row (e.g. Set/Series on a Pokemon folder spanning several
  sets, shown as "Multiple" with the list on hover) is plain text, not a
  sort link.
- **Pokemon folders** (Dashboard): the Pokemon breakdown groups cards by
  Pokemon name, not by print. A starred favorite always shows in the
  Favorites table, top 10 or not. "Put Pokemon in the same folder" merges
  several names (alternate print names like Celebi / Dark Celebi, or a
  whole evolution family) into one row. Merging only changes this display,
  never cards, prices or exports, and every merge has an "Undo".
- **Duplicates** are `qty − 1` per physical card, keyed on (card id,
  variant): a card's "Normal" and "Poké Ball Holo" prints are two cards,
  not duplicates of each other.

### Form validation (issue #228)

Every route that writes a transaction type, a price/amount, or a date
validates it at the boundary (`form_validation.py`) before anything is
written:

- **Type** must be one of `models.TRANSACTION_TYPES` (`purchase`, `sale`,
  `trade`, `ripped`). The money queries only count `purchase`/`sale`, so a
  typo would otherwise silently drop out of every figure.
- **Prices and amounts** (card prices, Total, Shipping, Fees, asking and
  suggested prices) must be finite and 0 or more. `nan`/`inf`, which a plain
  `float` field accepts, are rejected, and so are negatives. A decimal comma
  is accepted. Mark sold is stricter and requires each sold price to be above 0.
- **Date** must be an ISO date (`YYYY-MM-DD`).
- **Parallel row lists** (cart rows, Edit order rows, the ad builder's rows)
  must line up one-to-one. Edit order only checks the rows being kept, so a
  row ticked for deletion is never blocked by its own typo.

A rejection is a **422** that the user can read:

- **htmx forms** (New Order cart, Order history's total/shipping form, a
  transaction row's inline edit, Edit order, the ad builder and Mark as
  listed) get a short plain-text message naming the field, e.g. "Price on
  row 2 must be a number of 0 or more." `static/form-errors.js` (one global
  `htmx:responseError` listener) puts it into the form's
  `<p class="warnings" role="alert" hidden data-form-error>` slot. Nothing
  is swapped, so the input stays as typed. Any other error status shows the
  generic "Could not save (error N)" text. FastAPI's own validation errors
  (e.g. a non-numeric Order ID) are turned into plain text for htmx requests
  too, and stay JSON for everything else.
- **Plain-form pages** (Edit listing, Mark sold) re-render with the message
  in a `role="alert"` box and every submitted value filled back in.

### Order IDs (issue #228)

A new order's ID (`purchase_id`) is assigned by the server **when the order
is saved**, never when a form opens. That covers registering the New Order
cart, Mark sold on a listing, and "Start new order" on Edit order:

- The cart's heading reads "(Order ID assigned on Register)". It no longer
  shows or posts a reserved number, and the server ignores any posted
  `purchase_id`.
- On Edit order, "Start new order" blanks the row's Order ID field
  (placeholder "New"). Every row left blank in one save moves into the same
  new order. A typed number still moves or merges the row into that order.
- The ID is one past the highest in use, computed inside the save
  transaction (`app._allocating_order_id`). A lock is held until commit: a
  process-level lock on SQLite and a transaction-scoped
  `pg_advisory_xact_lock` on Postgres, which covers several Vercel
  instances. Two saves that overlap, such as a cart in one tab and Mark sold
  in another, get two different orders instead of merging into one.

Edit order only edits and deletes rows that belong to the order being
edited. If a posted row id belongs to another order, or to no order, the
save is rejected with a 422 ("Transaction N belongs to order #M, not order
#K -- nothing was saved. Reload the page and try again.") and nothing is
written. A row id that no longer exists, for example one already deleted in
another tab, is skipped as before.

## Data model

`cards`, `collections`, `card_collections` (many-to-many), `binders`,
`transactions`, `card_snapshots` (see "Value history" below),
`listings`/`listing_cards` (many-to-many, see "Sales listings (finn.no)"
below), `sets` (real Set entity, FK'd from `Card.set_id` — see
"Chronological sorting" below), plus `set_release_order` (the older lookup
table `sets` replaces — kept in place, unused going forward), `releases`
(the removed in-app Release Notes page's table, unused since #264 and kept
so its rows aren't lost — `notes/CHANGELOG.md` is the record of changes
now), `import_log` (one row per background-job run, see "Sync status"), `master_cards`/`master_card_ids`
(masterdata, see below), `card_prices`/`fx_rates` (per-source prices
and stored exchange rates, see "Pricing" below), `won_items` (Facebook
wins waiting to be registered, see "Facebook wins inbox" below), and
`job_locks` (one row per background job currently running, see "Sync
status" → "Single-flight guard").

### Masterdata (card identity across catalogs)

There is no official per-card ID for Pokemon cards, and every catalog (Dex,
pokemontcg.io, TCGdex, TCGplayer, Cardmarket, Collectr, ...) uses its own.
`master_cards` holds one canonical identity per printed card + variant,
keyed on what's printed on the card: `(language, set_code, number,
variant)`. `master_card_ids` maps any number of external IDs onto that
identity, at most one per `source`, each with a `matched_by`
(`exact_id` / `derived` / `heuristic` / `verified` / `verified_number` /
`manual`). A `manual` mapping is
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
  built. A `derived` ID never overwrites one that isn't `derived` (issue
  #349): when Dex's ID isn't pokemontcg.io's (`sv35-27` vs `sv3pt5-27`), the
  price refresh finds the real one by search and stores it as `heuristic`,
  and re-linking a card keeps it.
- `tcgdex` (issue #211) is written by the TCGdex price refresh, and only
  after a verified match (see "Pricing" → "TCGdex"): `verified` = set,
  printed number, printed set size and name agree; `verified_number` =
  the same without the name, for Japanese cards (TCGdex names them in
  Japanese). Later TCGdex refreshes look the card up by this ID only.
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
   separate set of cards. "Wishlist", "Incoming", and any category starting
   with "151 Fullarts" are always fully ignored: never a collection, a
   binder, or an importer warning. "Incoming" is the Dex folder of won cards
   that haven't arrived yet (added at qty 0, removed on arrival). Its
   qty-0 rows never become cards (see "Quantity-0 rows" under Sync
   semantics), so what's on the way is tracked in Dex and the Facebook wins
   inbox, not here (#311). Ignoring a category never touches an existing
   collection of the same name: an "Incoming" collection created by an
   earlier sync keeps its old tags until it is cleaned up by hand.
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
     that still didn't see it) but never deletes them. **No sync deletes
     cards.** The old "Full load" mode (hard-delete cards missing from the
     export) was removed deliberately in #225: deleting a card cascaded to
     its purchase/sale/trade transactions and its value-history snapshots,
     which Dex can't give back, and a truncated export or a Dex variant
     rename was enough to trigger it. A card that's genuinely gone just
     stays flagged. (`Card.transactions` also no longer has an ORM delete
     cascade, so deleting a card with transactions fails loudly instead.)
   - Flagged cards still count everywhere. They are listed under
     **Missing from Dex** on `/sync-status`, where one with no order rows
     and no listings can be deleted by hand (e.g. a card registered in
     error and then removed from Dex) — the only way the app deletes a
     card. See "Sync status" → "Missing from Dex".
   - **Circuit breaker** (`importer._check_circuit_breaker`, runs before
     anything is written; the whole sync aborts with no changes and a
     readable error):
     - No "My Collection" rows while one of the selected files has no data
       rows (empty or header-only) — that file may be the My Collection
       export. A sync that simply doesn't include My Collection, with every
       file containing data, still runs as a category-only sync.
     - The sync would newly flag more than `MISSING_ABORT_FRACTION` (5%) of
       all cards as missing, and more than `MISSING_ABORT_MIN_CARDS` (10) —
       almost always a truncated export. The daily cron never overrides: it
       returns HTTP 409 with `"status": "aborted"` and records an `aborted`
       row on Sync status (see "Automatic daily sync"). The importer's
       `allow_mass_missing` override still exists for a deliberate big
       clear-out, but no page offers it since the manual Dropbox picker was
       removed (#264).
   - **Quantity-0 rows (#340).** Dex can export "all variants", which
     lists every unowned variant with Quantity 0 (one real export had 823
     of its 1,686 My Collection rows at 0, and importing them all ran the
     sync past Vercel's 300 s limit). The importer skips a qty-0 My
     Collection row whose `(Id, Variant)` isn't already a card: no card is
     created, nothing is linked or looked up. A card that **is** already in
     the database and comes back at qty 0 is updated exactly as before (qty
     set to 0, price updated, un-flagged if it was flagged missing), so a
     sold card still goes to 0 rather than being flagged. In every other
     category, a qty-0 row that matches no card is skipped without a
     warning (it used to be one "finnes ikke i databasen" warning per row);
     a row with a quantity that matches no card still warns as before.
     Rows that match a card keep their tagging behaviour unchanged. The
     number skipped is `ImportResult.unowned_rows_skipped`, returned by
     `/cron/dropbox-sync` as `unowned_rows_skipped` and shown in the run's
     Details column on Sync status ("Skipped N rows with quantity 0 ...").
   - Every sync is expected to include both the main export and the
     Vintage Collection export together — the cron sync takes every CSV
     in the Dropbox folder at once for exactly this reason.
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
   matching card's `set_id` — writing only cards whose `set_id` actually
   differs, and nothing at all when every card is already linked (#340: it
   used to UPDATE every card on every cold start, which row-locked `cards`
   and deadlocked a concurrent Dex sync); since #134 this is no longer the primary
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

   The mark-sold form also takes optional order-level **Fees** and
   **Shipping you paid** (issue #254). Fees are split across the sale rows'
   `fees` by price (same whole-øre split as the cart's Fees field);
   shipping is written to every row's `purchase_shipping`, once per order
   like a purchase's, and split at read time by `queries.shipping_shares`.
   Both come off the sale's net proceeds in Net invested, paper gain and
   the cash-flow chart (see the Transactions section). Blank stores
   nothing; a negative or non-numeric value is rejected without writing.

   A card in an `"active"` listing shows a **Listed** badge after its name
   on Inventory and `/sales`, linking to the newest such listing on
   the Listings tab (`/orders/listings`) ("Listed ×N" if it's in several); delisted and sold listings
   don't count. `/sales` also warns, above the review table and again next
   to "Mark as listed", when selected cards are already in an active
   listing — a warning only, listing a card twice is still allowed. The
   lookup is `queries.active_listings_by_card`, one query per request; it's
   read-only and never touches `qty`, `card_collections`, or `binder_id`.
8. **Korean cards: a "KR" note (issue #367).** Dex has no Korean catalog
   for some sets (151, VSTAR Universe, ...), so Korean cards are logged in
   Dex as the Japanese print. Mark such a card with the token `KR` in any of
   its Dex notes (`Note 1`–`Note 5`) and the importer stores
   `language = "Korean"` instead of Dex's `Locale`
   (`constants.physical_language`). Any set. Case-insensitive, whole word
   (`constants.KOREAN_NOTE_RE`): `KR`, `kr`, `KR; mint`, `bought KR lot`
   match; `KRAKEN`, `okr`, `KRW` don't. The card then shows "KR" in
   Inventory (and its language filter), on the card page, in badges, the
   collection gallery and generated finn.no ad text.
   - **Only the language changes.** `card_id`, the master card
     (`(ja, sv2a, n, variant)`), `master_card_ids`, prices and images all
     key off `card_id`, so a Korean card keeps its Japanese print's
     identity and prices. There is no Korean price source (#345/#346), so
     the Japanese price is shown as a proxy and labelled honestly:
     "TCGplayer via Dex (JP price)", "Cardmarket via TCGdex (JP price)" on
     the card page and in price tooltips (`pricing.card_source_label`,
     display only; `pricing.CHAIN` is unchanged).
   - **Temporary fallback:** `constants.PHYSICAL_LOCALE_OVERRIDES =
     {"jpn_sv2a": "Korean"}` (keyed on the Dex `card_id` prefix before the
     `-`) makes every 151 card Korean, note or not, until the user has
     tagged all of them with "KR" in Dex. **To remove it, delete that one
     entry** (the next sync then uses the notes alone). While it's there a
     genuine Japanese sv2a card would also show as Korean (the user owns
     none).
   - **Notes follow Dex.** `notes` is rewritten from the export on every
     sync, so a note removed in Dex is cleared here too (it used to be kept
     forever) and the language reverts to Dex's `Locale`. Nothing in the
     app edits `notes`. No backfill or schema change: the next sync
     recomputes `notes` and `language` for every exported row.

## CSV import format

Dex export, semicolon-separated:

```
Type;Category;Locale;Series;Set;Id;Number;Name;Variant;Rarity;Illustrator;Quantity;Price;Note 1;Note 2;Note 3;Note 4;Note 5
```

Each file is one Dex folder/category export (the `Category` column is
constant per file). Upload as many category files as you have for one sync
— `Note 1`–`Note 5` are concatenated into `notes` (cleared when all are
empty), and `Locale` (which language/region print, e.g. `ENG`/`JPN`) is
stored as `language` (unless a "KR" note makes it Korean, rule 8) and
shown/filterable/sortable as "Language" in Inventory. Routing (My Collection /
binder / collection / excluded) is applied per the rules above based on
each row's `Category` value. `Type` is read but unused — every real Dex
export sets it to the constant `Card` on every row, so it carries no
per-card information.

## Sync status

`/sync-status` (issue #264) shows what every background job did, read from
the `import_log` table (`ImportLog` in `models.py`; `sync_status.py` writes
and reads it). Before #264 only a successful Dex sync left a row; now each
of these does:

| Job (`job`) | Recorded outcomes (`status`) | Written by |
|---|---|---|
| `dex-sync` | `ok` (with the warning text), `empty` (no CSV files in the folder), `aborted` (import circuit breaker, #225), `failed` (Dropbox or unexpected error, or "Interrupted": a run killed before it finished, #340) | `importer._log_import` (ok), `jobs.run_dex_sync` (the rest), `job_locks` (interrupted) |
| `price-refresh` | `ok` (one-line summary: TCGplayer/TCGdex counts, images, snapshot, FX), `degraded` (no prices written, only the FX fallback constant was available, #229; shown as a problem), `failed` | `jobs.run_price_refresh` |
| `set-sync` | `ok`, `failed` (API call failed, or an error) | `jobs.run_set_sync` |
| `image-backfill` | `ok`, `failed` | `jobs.run_image_backfill` |

`source` is the run's **trigger**, i.e. who started it:

| `source` | Meaning |
|---|---|
| `cron` | The real scheduled Vercel call (it carries `Authorization: Bearer <CRON_SECRET>`; checked by `cron_auth.require_cron_secret`, #226) |
| `manual` | A person: a `/cron/*` call with `?secret=`, a local run, later #199's Run-now buttons |
| `connector` | The Claude connector (#276, not built yet) |

Older rows may say `dropbox` (the removed manual picker). An interrupted
run's row carries the trigger its lock recorded.

**One code path per job (issue #274).** Every job body lives in
`jobs.py` (`run_dex_sync`, `run_price_refresh`, `run_image_backfill`,
`run_set_sync`), each taking `trigger=` and returning a result dataclass;
the `/cron/*` routes are thin wrappers that only map that result to JSON
and status codes (aborted → 409 `{"status": "aborted"}`, a run in
progress → 409 `{"status": "already_running"}`, a Dropbox error → 502,
`degraded` stays a 200). The trigger is kept apart from the *snapshot*
source (`card_snapshots.source`), which stays `cron` / `price-cron` /
`manual`: a `connector` run snapshots as `manual`, so
`queries._SNAPSHOT_SOURCE_ORDER` and the one-point-per-(day, source) rule
are unchanged. The circuit breaker is the same for every trigger: only
`run_dex_sync(..., allow_mass_missing=True)` skips the mass-missing check,
and no `/cron/*` route can pass it. An abort's `importer.ImportAborted`
carries `newly_missing`, `total_cards`, `limit_fraction` and the first 10
`sample_names`, so callers never parse its message.

**Schema (additive only).** No new table: `import_log` gained four nullable
columns — `job`, `status`, `message` (summary or error/abort reason) and
`warnings_text` (the import's warnings, one per line; only the count was
kept before). `init_db()` adds them via its additive `ALTER TABLE` pass,
and `db._backfill_import_log_defaults` (schema version 12) sets
`job='dex-sync'`, `status='ok'` on older rows, since every pre-#264 row was
a successful Dex sync. Readers still treat NULL as those defaults.
`cards_deleted` stays in the table for old rows but is no longer shown (no
sync deletes since #225). Being an existing table, `import_log` is already
covered by `init_db()`'s RLS pass (#239).

**Aborts survive the rollback.** `sync_status.record_run` runs after the
caller has committed (success) or rolled back (abort/failure) and commits
its row on its own, so an aborted import's rollback never takes the log row
with it. It never raises: a failure to write the log is printed and rolled
back rather than masking the job's real response.

### Single-flight guard (issues #340, #274)

Only one run of each job runs at a time (`jobs._single_flight`, every
job since #274; the Dex sync since #340). Two overlapping Dex syncs (a
manual trigger plus a retry) deadlocked on `cards`, and a run killed by
Vercel's 300 s limit left no trace at all. Different jobs don't block each
other (a price refresh can run during a Dex sync, as before).

- **`job_locks` table** (`models.JobLock`: `job` primary key,
  `started_at`, `trigger`, `token`; schema version 14). A run inserts its
  job's row (`dex-sync`, `price-refresh`, `set-sync`, `image-backfill`)
  before doing anything (before Dropbox or any API) and deletes it when it
  ends, whatever the outcome. `job_locks.py` holds the logic; the lock
  uses its own session, so its commits never mix with the sync's.
- **Already running.** A second run while the row is live gets HTTP 409
  `{"status": "already_running", "job", "started_at", "trigger", "error"}`
  and writes nothing (no `import_log` row, no Dropbox or API call).
- **Killed runs.** A row older than `job_locks.STALE_AFTER` (15 min,
  well above Vercel's limit) belongs to a run that died. The next run takes
  it over, and the dead run gets a `failed` row on Sync status, dated when
  it started, with "Interrupted: ... no result recorded". Loading
  `/sync-status` does the same cleanup (`job_locks.reap_stale`), so a
  killed run shows up there without waiting for the next sync. Writes are
  conditional on the row's random `token`, so racing instances record a
  dead run once, and a slow run's late release never drops a newer run's
  lock.
- **Not** a session-level `pg_advisory_lock`: with NullPool and Supabase's
  transaction-mode pooler it isn't held reliably for a whole run, and an
  advisory lock couldn't make a killed run visible anyway. Works the same
  on SQLite and Postgres. A new table, so `init_db()` creates it and its
  RLS pass covers it.

### Missing from Dex

The section between the at-a-glance block and the Run log lists every card
with `flagged_missing_since` set (`missing_cards.flagged_cards`, oldest flag
first): name (linked to the card page), set, number, variant, qty, flagged
since, and how many order rows (`transactions`) and listings
(`listing_cards`) reference it. Empty state when none.

A card with zero order rows and zero listings gets a **Delete…** action
(`POST /cards/{id}/delete-missing`); the card page shows the same form
when the card is flagged. The confirmation is a required checkbox in the
form (no native `confirm()`). `missing_cards.delete_missing_card` deletes
only when **all** of these hold, re-checked in the same transaction:

1. the confirmation checkbox was ticked (`confirm` posted non-empty);
2. the card exists;
3. it is flagged missing (`flagged_missing_since` is set);
4. it has zero `transactions` rows;
5. it is in zero listings (`listing_cards`).

Otherwise nothing changes and the reason is shown. Every handled outcome
(deleted or refused) is a 200 re-rendering the section (or, from the card
page, its delete block), since htmx 1.9 doesn't swap a 4xx. A delete also
removes the card's `card_collections` tags, `card_snapshots` rows and
`card_prices` rows — intended for a card that should never have been
registered. Snapshots are deleted explicitly, since SQLite only honours
`ON DELETE CASCADE` with `PRAGMA foreign_keys` on. A flagged card with
orders or listings stays listed without a delete action: deleting it would
destroy its order history. The route is behind the normal login (not in
`_PUBLIC_PATHS`). No schema change.

The in-app **Release Notes** section (`/releases`, #144) was removed in the
same change: `notes/CHANGELOG.md` is the record of changes now. Its
`releases` table and `Release` model are left in place, unused.

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
   `GET /cron/dropbox-sync?secret=<CRON_SECRET>` directly (deprecated, see
   "Cron endpoint auth" below; `curl -H "Authorization: Bearer
   <CRON_SECRET>"` works too and keeps the secret out of logs). There is no
   in-app file picker: its UI was removed earlier (HANDOFF.md #87) and its
   leftover `/import/dropbox/list` and `/import/dropbox/sync` routes in
   #264 (#199's "Run now" is the planned replacement).

The refresh token doesn't expire, so this is a one-time setup. Nothing is
ever written back to Dropbox.

## Deploying to Vercel + Supabase

Moving off `localhost` means two things change: the SQLite file needs to
become a real database (Vercel's filesystem is read-only/ephemeral —
`db.py` refuses to start on Vercel without `DATABASE_URL` set, rather than
silently failing on writes), and the app is now reachable by anyone with
the URL, so it needs a login. Both are optional until you set the matching
env vars — nothing here changes local `python app.py` behavior. Once prod
holds real data, set up the weekly encrypted backup too: see "Backups and
restore" below.

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

### Database access hardening

As defence in depth, prod's data is only read through direct Postgres
connections (the app's `DATABASE_URL`, and `backup_reader` for backups),
never through Supabase's API keys (since 2026-09-30):

- The Supabase **Data API** (PostgREST) is disabled in the dashboard.
  Nothing in this repo uses it: the app talks to Postgres directly, and
  `auth.py` only calls the Auth API (GoTrue, `/auth/v1/...`).
- **Row-level security is enabled on every `public` table**, with no
  policies, and the `anon`/`authenticated` roles have no table, sequence
  or function privileges in `public` (default privileges revoked too).
  The app connects as `postgres` (table owner, `BYPASSRLS`), so it's
  unaffected.
- Auth signups are turned off; the only account is the one created by hand
  in section 1, step 3 above.

**Any new table must keep it that way**: RLS on, no `anon`/`authenticated`
grants. Since #239 (schema version 11), `init_db()` enables RLS on every
table in `Base.metadata` on Postgres as part of its migration chain (see
"Database migrations" under "Not built yet"), so a deploy that adds a table
no longer needs a manual step. Grants are still not automated: a new table
picks up whatever `public`'s default privileges say. The original hand-run
SQL is in `HANDOFF.md` (2026-09-30, prod RLS hardening).

### Automatic daily sync (Vercel Cron)

Once Dropbox import is set up (see "Dropbox import setup" above), the
deployed app syncs itself automatically — `vercel.json` schedules a
[Vercel Cron Job](https://vercel.com/docs/cron-jobs) that hits
`GET /cron/dropbox-sync` once a day (`0 5 * * *`, i.e. 05:00 UTC — edit
the `crons` entry in `vercel.json` to change it). That route pulls every
CSV currently in the configured `DROPBOX_FOLDER` and runs a normal sync
(flags missing cards, never deletes — no sync does since #225). If the
import circuit breaker trips (empty/header-only My Collection, or a mass
drop above 5%), nothing is written, no snapshot is taken, and the route
returns HTTP 409 with `{"status": "aborted", "error": ...}` so the Vercel
cron run shows as failed; fix the export and the next run (or a manual
sync) picks up normally. Every outcome (synced, empty folder, aborted,
Dropbox error) also lands on the Sync status page (see "Sync status").

To turn it on:

1. Make sure `DROPBOX_APP_KEY`/`DROPBOX_APP_SECRET`/`DROPBOX_REFRESH_TOKEN`/
   `DROPBOX_FOLDER` are already set in Vercel (see "Dropbox import setup").
2. Add a `CRON_SECRET` environment variable in Vercel (any long random
   string — Vercel automatically sends it back as `Authorization: Bearer
   <CRON_SECRET>` on its own cron requests, and every `/cron/*` route
   checks it). **Required on a deploy:** without it, every `/cron/*`
   request is refused with HTTP 503 (see "Cron endpoint auth" below).
3. Redeploy. Vercel's dashboard (Project → Cron Jobs) shows each run and
   its response — `cards_created`/`cards_updated`/etc. and any warnings.
   The in-app Sync status page (`/sync-status`) shows the same runs.
   If a sync is already running (e.g. a manual trigger overlapping the
   cron), the route returns HTTP 409 with `{"status": "already_running"}`
   and changes nothing (see "Sync status" → "Single-flight guard").

Keep your Dropbox folder holding the *current* full set of exports (main
collection + Vintage + whatever else you track) — each cron run syncs
whatever's in there at the time. If the same Dex category turns up in more
than one file, only the newest file (by Dropbox `client_modified`) is read
for it and the run gets a warning naming the skipped files (issue #351; it
used to let an older export overwrite a newer one). Re-reading an unchanged
export doesn't make its prices fresh: they're dated at the export (see
"Pricing").

### Cron endpoint auth (issue #226)

The four `/cron/*` routes (`dropbox-sync`, `price-refresh`,
`image-backfill`, `set-sync`) skip the login (a scheduled call has no
session) and share one check, `cron_auth.require_cron_secret`:

| `CRON_SECRET` | Login configured (`SUPABASE_*`) | Result |
|---|---|---|
| set | either | Only a matching secret runs the job (constant-time compare); anything else is 401 |
| missing/empty | yes (a deploy) | **Every request refused, 503** — fails closed |
| missing/empty | no (local `python app.py`) | Open, as before |

- **`Authorization: Bearer <CRON_SECRET>`** is the primary path. Vercel
  Cron sends it itself, and it's the only one recorded as `source=cron`
  (and the `cron`/`price-cron` snapshot slot).
- **`?secret=<CRON_SECRET>` is deprecated.** It lands in request logs and
  browser history, but until #199's logged-in Run-now buttons exist it's
  the only way to start a job by hand from a browser, so it's still
  accepted (recorded as `manual`). Removing it is one line
  (`cron_auth.ACCEPT_QUERY_SECRET = False`), planned with #199. From a
  terminal, prefer `curl -H "Authorization: Bearer <CRON_SECRET>" ...`.

### Facebook wins inbox (issue #309)

`apps/fb_auction_watcher` (the Chrome extension) can send the lots you've
won on Facebook to `POST /inbox/fb-wins` ("Send wins to inventory" under
its To pay list). They're staged in the `won_items` table (`won_inbox.py`,
schema version 13) and listed on Orders → Purchased under "Facebook wins to
register" until you register them. The endpoint never writes transactions
or cards — only you do, in the cart.

- **Contract:** the versioned JSON `{"format": "fbaw-won", "version": 1,
  "sent_at", "items": [...]}`, defined by the producer in
  `apps/fb_auction_watcher/docs/spec.md` "Sending wins to tcg_inventory".
  One item per won lot, keyed `external_ref` (`fbaw:<postId>:<commentId>`,
  or `fbaw:<postId>:pos<n>`). An unknown major version is refused with a
  readable error. The root test `tests/test_cross_app_won_inbox.py` runs
  `won_inbox.parse_payload` over the extension's committed fixture
  (`tests/fixtures/won-inbox.v1.json`), so the two can't drift apart
  silently.
- **Upsert by `external_ref`:** a new ref becomes a `pending` row; a
  pending row is refreshed (e.g. a price that was unknown becomes known);
  a `registered` or `ignored` row is never changed. Re-sending is always
  safe. Totals are computed from the items, never stored.
- **Auth: `INBOX_TOKEN`, fail-closed.** The route skips the login (the
  extension has no session; it's in `_PUBLIC_PATHS` like the cron routes)
  and takes only `Authorization: Bearer <INBOX_TOKEN>`, compared in
  constant time — never a query-string secret. It's a separate secret from
  `CRON_SECRET` on purpose: a leaked token can only add or refresh pending
  inbox rows. With login configured (`SUPABASE_*`) and `INBOX_TOKEN`
  unset, the endpoint refuses everything (503); only a local no-login run
  accepts sends without a token, and if you set one locally it's required
  there too. (The `/cron/*` routes work the same way since #226, see "Cron endpoint auth".)
- **Limits:** bodies over 512 KB are refused (413), as is anything that
  isn't valid JSON (400) or any item that doesn't check out (422: unknown
  sale type, a non-Facebook or non-https link, a negative price, a bad
  date, a duplicate ref, more than 1000 items). Any refusal writes nothing.
- **RLS:** `won_items` gets row-level security from `init_db()` like every
  table (see "Database access hardening"); grants stay as they are.

**Registering a won sale (the link flow).** One won sale becomes one order
(several sales from one seller paid together can be merged afterwards with
Edit order). The workflow: you win (the card goes into Dex's Incoming at
qty 0, which Dex doesn't export, so it isn't in tcg_inventory yet); you pay
and it arrives; you raise its qty in Dex; after the next daily sync it
exists here, and you open the sale with **Open in cart** and register it.
So linking always happens after the cards exist.

- **Prefill.** "Open in cart" (`GET /orders/fb-wins/{item_id}/cart`)
  renders the normal New Order cart in one response: date = the sale's end
  date, platform "Facebook", Total = the sum of the known prices (blank if
  none is known), Shipping blank with the seller's shipping text shown
  muted beside it. While the prefilled Total is untouched, Shipping you
  type is added to it, so Remaining (Total − card prices − shipping, the
  same formula Order history uses) stays exactly the known price of what
  isn't linked; once you edit the Total yourself it's left alone. A cart
  that was only prefilled doesn't ask "Discard the in-progress order?" when
  you open another sale.
- **Imported items panel**, above the form and deliberately outside it (its
  checkboxes have no name and are never submitted). Per item: its label
  linked to the Facebook comment, its price or "price unknown", candidate
  cards as **unticked** checkboxes (never auto-linked), **Link selected**,
  and an item-scoped **Not listed? Search…**. Candidates are a fuzzy name
  match (`won_inbox.candidates`: a distinctive word of the card's name in
  the label, small typos allowed, its printed number adds to the score),
  ranked cards with no purchase/ripped/trade row first, then cards first
  synced on or after the sale ended ("new since the sale"); a card already
  on an order says "already on order #N". With none: "No matching card yet
  — it appears after it arrives, you raise its qty in Dex, and the daily
  sync runs."
- **Link selected** adds normal cart rows through
  `/transactions/purchase/add-row`, which takes the item's ref
  (`won_item_id`), adds its note ("<label> · <seller>") and a prefilled
  price. A lot linked to several cards gets its price split evenly, the
  last card taking the rounding (`static/money-split.js`, shared with
  Distribute). Every cart row posts `won_item_id` and `note` (blank when
  not linked), index-aligned with `card_id`/`price`; `create_purchase`
  checks all four line up and stores the note on the transaction. Removing
  a row unlinks its item.
- **Nothing is dropped silently.** Above Register: "N of M items not linked
  (kr X) — they stay in the inbox", the live Remaining, and a warning when
  some items have no known price or there's no Total. Register asks before
  leaving items unlinked, and warns when there's no Total while items are
  unlinked or unpriced.
- **Register** marks each linked item `registered` with the new order's
  `purchase_id`, in the same commit as the transactions. Unlinked items stay
  pending. A lot linked to fewer cards than it holds can be kept pending
  with its "Lot not complete — keep it in the inbox" box: it stays listed,
  pointing at the order, until you add its other cards with Edit order and
  click **Lot complete**. An item registered or ignored meanwhile (another
  tab) makes Register refuse with a 422 and write nothing. Ignore stays
  available throughout.
- **Edit order carries the items along** (#317). `won_items.purchase_id`
  has no per-transaction link, so when Edit order saves (move, merge,
  split, delete), `won_inbox.follow_order_edit` re-points that order's
  registered and pending items in the same commit. An item's own rows are
  the ones carrying the note Register gave them ("<label> · <seller>", as
  it was before the save). The item follows where its own rows went: all
  in one order → that order (so merging a seller's sales, the settled
  workflow, takes every item to the merged order); split across orders →
  it stays on the edited order if some are still there, otherwise the
  order with most of them (lowest ID on a tie). An item with none of its
  own rows left (note edited, rows deleted, or a lot's later cards added
  with no note) goes by all of the order's rows the same way: it stays
  while the order still has rows and follows a merge or whole move. If
  every row is deleted it keeps pointing at the old ID. Ignored items are
  left alone.
- **"Order missing"** covers registered items too (#317): an item
  registered on an order that no longer has any transaction comes back into
  the list with "order #N missing", and can be opened in the cart and
  registered again (or kept pending as "not complete") or ignored. One
  registered on an order that exists can't be relinked or ignored.
- **Leftovers of a partly registered sale** (#317, decided 2026-10-06).
  Opening a sale whose other items are already on an order (registered,
  or a lot kept "not complete") prefills a Total that leaves out what those
  earlier orders already count, so the same money is never in two orders'
  Totals: the known price of a lot already linked to cards on an existing
  order, plus the earlier orders' positive Remaining (Total − card prices −
  shipping, as Order history shows it), capped at the leftovers' known
  prices. If the first order kept the whole-sale prefill, its Remaining is
  exactly the leftovers and the new Total is 0; if its Total was set to
  cover only its own cards (Remaining 0, or no Total), nothing is left out.
  When an earlier order also holds another sale (merged), its Remaining may
  include that sale's leftovers too, so check the Total there. The cart
  names the earlier order(s) and how much it left out.

To turn it on in prod: set `INBOX_TOKEN` in Vercel (a long random string,
e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`),
redeploy, then in the extension's Settings enter the app's address and the
same token, and Save (Chrome asks for permission to reach that address).

### Pricing

Every price is stored per source in `card_prices` (one row per card +
source, latest value only -- no per-source history, see #169), and one
resolver picks the displayed price (issue #210, `pricing.py`).

**Sources**, in display priority (`pricing.CHAIN`, a module constant --
TCGplayer-first by the owner's choice, epic #213):

| # | `source` | What | Currency | Written by |
|---|---|---|---|---|
| 1 | `dex` | Dex CSV `Price` cell (Dex is set to TCGplayer) | NOK | `importer.py`, every sync |
| 2 | `tcgdex_tcgplayer` | TCGdex's TCGplayer `marketPrice` | USD | `tcgdex_prices.py` (#211) |
| 3 | `pokemontcg` | pokemontcg.io's TCGplayer market price | USD | `price_refresh.py` (daily, by ID, #349) |
| 4 | `tcgdex_cardmarket` | TCGdex's Cardmarket `trend` (else `avg30`) | EUR | `tcgdex_prices.py` (#211) |

Dex is TCGplayer-sourced for Japanese cards too, but it's the only
TCGplayer source they have (pokemontcg.io has no Japanese cards, and
TCGdex carries no TCGplayer data for them). Cardmarket via TCGdex, last in
the chain, is their one independent fallback: it's what a Japanese card
shows when its Dex price is missing or stale.

A row keeps the native price, currency, the FX rate used, `price_nok`,
which print it priced (`variant_key`), `fetched_at`, any per-source flags,
and `lookup_failed_at` (the failed-lookup backoff, see "Price refresh"). A
Dex sync whose `Price` cell is empty keeps the card's last known Dex price
(it used to wipe it).

**A Dex price is as old as its export (issue #351).** Dex exports are made
by hand, but the daily cron re-reads whatever CSV is in Dropbox. The `dex`
rows' `fetched_at` is therefore the export's date (the My Collection file's
Dropbox `client_modified`, as a UTC date, capped at today), not the day of
the sync that re-read it. An unchanged export goes stale `FRESH_DAYS` after
it was made, and the chain falls through to the live sources
(`tcgdex_tcgplayer`, `pokemontcg`, then `tcgdex_cardmarket`) until the next
export makes Dex fresh again. With no file date (`import_dex_csv_files`
called without `file_dates`, e.g. in tests) the sync day is used. The age
shown under the price ("TCGplayer via Dex · 9 d ago") is the export's age.
`fetched_at` was chosen over `source_updated_at` because resolution, the
`stale` flag and `market_price_as_of` all read `fetched_at`, and for a
mirrored source the export *is* the fetch. Expect most cards to switch
source at once when an export passes 14 days, and back on the next export
(the charts mark both as a source switch); see the HANDOFF entry for the
prod estimate.

**Resolution** (`pricing.resolve`): the first *fresh* price in chain order
wins -- fresh means fetched within **14 days** (`FRESH_DAYS`), deliberately
longer than the refresh cadences (daily for `pokemontcg`, weekly for TCGdex)
so prices don't expire right before their refresh and flap between sources. If no source is fresh, the most
recently fetched price is kept and flagged `stale`: a price never drops to
0 or blank once one ever existed. Only a card with no price from any source
is flagged `no_price`. The winning row's own flags carry over
(`variant_price_uncertain`: pokemontcg.io had several prints and the Dex
variant couldn't be matched to one). A `pokemontcg` row also records
TCGplayer's own last update (`tcgplayer.updatedAt`) in `source_updated_at`.

**Materialized on `cards`**: the result is written to `cards.market_price`,
`market_price_source`, `market_price_as_of` and `price_flags`, and every
consumer reads that (`Card.display_price` in Python; the `market_price`
column in SQL, so Inventory sorts and pages by it). This is a deliberate,
documented exception to the "computed, never stored" rule (see
`models.Card.display_price`): the resolved price is a time-dependent
decision, not a same-row derivation, and it's fully re-derivable by
re-running the resolver. It is resolved (`pricing.resolve_cards`, bulk):

- after every write to `card_prices`, in the same transaction (Dex sync,
  price refresh);
- in a full DB-only pass at the end of each sync/cron (`/cron/dropbox-sync`,
  `/cron/price-refresh`, manual Dropbox sync), right before its snapshot --
  this is how a source going stale is applied to cards nobody re-priced.

**Legacy columns.** `cards.reference_price` and `cards.tcgplayer_price`
(+ `tcgplayer_price_updated_at`) are kept as mirrors of the `dex` and
`pokemontcg` rows (`init_db()` never drops columns), but nothing reads them
for a displayed price. `cards.price_lookup_failed_at` (#216) is deprecated
and no longer written; its state moved to `card_prices.lookup_failed_at`.

**Migration / backfill.** Schema version 10. On first start after deploy,
`init_db()` seeds `card_prices` from those legacy columns and resolves
every card (`pricing.backfill_from_legacy`, run by `_backfill_card_prices`
like `_backfill_master_cards`: a few set-based `INSERT ... SELECT`s and
chunked `UPDATE ... CASE`s, never fatal, a single `SELECT` once done). The
seeded `dex` rows are dated at the last Dex sync (or the day a card went
missing from Dex); `pokemontcg` rows keep `tcgplayer_price_updated_at` and
have no native USD value or rate (not recorded before #210).

**In the UI** (part 2 of #210). The price is labelled "Market price"
everywhere it stands alone ("Price" is kept for transaction prices). Source
labels come from `pricing.SOURCE_LABELS`: `dex` is "TCGplayer via Dex" for
every language, Japanese included (Dex uses TCGplayer prices for Japanese
cards too). A Korean card priced from its Japanese print gets "(JP price)"
after the label (business rule 8). The card page shows the source and age under the price
("TCGplayer via Dex · 2 d ago") with a chip per flag (`pricing.FLAG_LABELS`),
and a "Price sources" table with every `card_prices` row in chain order,
open by default only when the card is flagged. Charts keep source-switch
points (Price movers leaves them out) and say so in the tooltip: the card
price chart per point (`card_price_history`'s `source_note`), the Market
Value chart per day with a card count (`queries.history_source_notes`,
window functions in SQL; pre-#210 snapshots get their source inferred like
Price movers). Price movers' change `title` names the source. The price sort
key is `market_price` (Inventory `sort`, card picker `gsort`; Dashboard's
Most valuable cards has no sort since #245 -- always market price
descending, a stale `tsort` is ignored); the pre-#210 `reference_price` is still accepted as an alias.

#### TCGdex (issue #211)

`tcgdex_prices.py` asks TCGdex (`api.tcgdex.net/v2/{en|ja}/cards/{id}`,
free, no key) once per card; that one response carries both TCGdex sources.
International cards use the `en` catalog, Japanese cards `ja`; zh-hans
cards aren't covered.

**IDs, verified, never guessed.** A price only ever comes from a TCGdex ID
that was verified; `card_images`' image lookup may guess-then-check, a
price lookup may not (see `backfill_images.py`'s tcgdex-guess incident).
The set is found in TCGdex's own set list (Dex's `sv2a` is TCGdex's `SV2a`,
pokemontcg.io's `sv3`/`sv3pt5` and Dex's `sv35` are `sv03`/`sv03.5`), the
card in that set's card list by printed number (exactly one hit, else no
match), and the fetched card must agree with Dex on set, printed number,
printed set size (`/165` vs the set's official count) and, for English
cards, name. Then the ID goes into `master_card_ids` (source `tcgdex`,
`matched_by` `verified` or `verified_number`, see "Masterdata") and every
later refresh fetches by it, with no search. A stored `manual` mapping is
respected.

**Which Cardmarket number is "the price".** `trend` (Cardmarket's own
smoothed price), falling back to `avg30` when `trend` is missing or 0
(Cardmarket reports 0 rather than null on thin markets). `avg7`/`avg1` are
left out because they swing with single sales on cheap cards. Chosen on the
2026-09-30 sample (see HANDOFF): of `trend`, `avg30`, `avg7` and `avg`,
`trend` tracked Dex's price closest (median Dex/Cardmarket 1.56 for
Japanese cards vs 1.79-2.29 for the others). TCGplayer via TCGdex uses
`marketPrice`, as pokemontcg.io does.

**Which print.** TCGdex lists a card's prints (`variants_detailed`), each
with its own Cardmarket product, and Cardmarket's `-holo` fields are the
foil (reverse-holo) copy of a product. Dex's Variant is mapped with the
same unambiguous-only policy as pokemontcg.io's (`card_images._match_variant_key`):

| Dex Variant | Cardmarket price used |
|---|---|
| Normal | the `normal` print, plain fields |
| Holo | the `holo` print, plain fields (a holo rare's own product) |
| Reverse Holo | the plain `reverse` print's `-holo` fields; if TCGdex lists no reverse print at all, the card's own `-holo` fields |
| Poké Ball / Master Ball / other "Ball Holo" | the reverse print with that foil (its own product), `-holo` fields, **unless** the Poké Ball product is priced at or above the Master Ball one |
| blank | the only priced print, if there is exactly one |

Anything else (no such print, a variant with no rule, an inverted ball
pair) falls back to the card's first regular print and is flagged
`variant_price_uncertain`, never silently guessed. The ball rule exists
because TCGdex had the Poké Ball and Master Ball products swapped on 3 of
9 Pokémon Card 151 cards sampled (a Poké Ball Slowbro at 59 EUR). Stamped
and oversized promo prints are ignored. For TCGplayer via TCGdex, the keys
(`normal`, `reverse-holofoil`, `holofoil`, ...) go through the same
`_match_variant_key` rules as pokemontcg.io's (see "Price refresh"): a ball
pattern or other print TCGplayer has no key for gets no TCGplayer price.

**Conversion.** EUR and USD are converted at Norges Bank's daily rate
(`fx_rates`, same as pokemontcg's USD); each row keeps the native price,
currency and the rate used. A price that TCGdex itself last updated more
than 30 days ago isn't used (so a frozen upstream can't pass as fresh).

**Refresh.** Inside `/cron/price-refresh`, after the pokemontcg pass and
before the re-resolve + snapshot: up to 125 cards (`MAX_LOOKUPS_PER_RUN`,
~870 cards on a 7-day cadence), time-boxed to 90 s (`jobs.TCGDEX_SECONDS`;
~0.75 s per card), sequential with a 0.2 s pause between requests. Resolving
a new ID costs one set-list request per set per run on top. Order: cards
with no market price at all first, then stale TCGdex prices (oldest first),
then never-tried cards, then retries. A card is due when neither TCGdex
source has a price from the last 7 days; one where TCGdex had no match or
no price is stamped `lookup_failed_at` on its `tcgdex_cardmarket` row and
retried after 14 days.

**Failures never cost a price.** A 429/5xx/timeout is retried once after a
back-off (honouring `Retry-After`, capped). If it persists, the card is
skipped without stamping anything and is tried again next run. A persisting
429 ends the run for the day, and so do 3 errors in a row. A card that's gone,
unverifiable or unpriced keeps its stored price (pricing's stale rule shows
it); only `lookup_failed_at` is set. The whole pass is contained, so an
unexpected error is logged and the cron still resolves and snapshots. The
cron response has a `tcgdex` summary (checked, priced, `ids_matched`,
`cards_unmatched` with reasons, `cards_variant_uncertain`, `stopped`,
`http_calls`, `status`/`degraded_reason`). With only the FX fallback
constant available, the pass is skipped and reported as degraded (see
"Currency" under "Price refresh").

By hand, same `DATABASE_URL` convention as `price_refresh.py`:

```bash
cd apps/tcg_inventory
python tcgdex_prices.py --dry-run      # how many cards are due
python tcgdex_prices.py [--limit N]    # one refresh pass
```

### Price refresh (Vercel Cron)

`vercel.json` schedules `GET /cron/price-refresh` (`0 6 * * *`, one hour
after the Dropbox sync — edit `vercel.json` to change it), with the same
`CRON_SECRET` auth pattern as `/cron/dropbox-sync` (see that section above
for setup). It runs the pokemontcg.io pass below, then the TCGdex pass (see
"TCGdex" under "Pricing"), then re-resolves every card's market price and
writes its own `card_snapshots` row (`source="price-cron"`), then a small
image pass. The Dex sync no longer looks up pokemontcg.io prices at all
(issue #349): it writes the Dex price and fills images, nothing else.

**pokemontcg.io prices by stored ID, daily, in batches** (issue #349,
`price_refresh.py` + `pokemontcg_client.py`):

- **Which cards.** Every international card (masterdata language `int`)
  with a `pokemontcg` ID in `master_card_ids`, a print TCGplayer has a key
  for (see the variant table below), and no `pokemontcg` price fetched
  today. Dex's international `card_id` is the pokemontcg.io ID (stored as
  `derived` by masterdata). Japanese and zh-hans cards aren't on
  pokemontcg.io and are never asked for.
- **How.** The due cards' distinct IDs (variants of one printed card share
  one) are asked for 50 at a time (`pokemontcg_client.CHUNK_SIZE`):
  `GET /v2/cards?q=id:"a" OR id:"b" ...&select=id,name,number,tcgplayer`.
  Prod's 472 international cards (452 distinct IDs) are 10 requests a day,
  ~6 s each (measured 2026-10-07). Each
  card then picks its own print (`card_images._choose_tcgplayer_price`).
- **Verified.** A returned card must have the requested ID, Dex's printed
  number and a name that overlaps Dex's (the same check as the by-ID image
  lookup). A price TCGplayer itself last updated more than 30 days ago
  (`tcgplayer.updatedAt`, same limit as TCGdex) isn't used.
- **Fallback search.** An ID missing from a successful response isn't on
  pokemontcg.io under Dex's ID (Dex writes `sv35-27` for pokemontcg.io's
  `sv3pt5-27`, `sv65-*` for `sv6pt5-*`). For that card the old name + set
  name + number search runs once; a confident hit (exact name and number)
  is priced and its ID stored in `master_card_ids` as `heuristic`, so the
  next runs go by ID. The Dex sync never reverts it (a `derived` ID never
  overwrites a non-`derived` one, see "Masterdata"). A `manual` mapping is
  never searched past. At most 40 searches per run
  (`MAX_FALLBACK_SEARCHES_PER_RUN`); the rest wait for the next day.
- **No match.** A card is stamped `card_prices.lookup_failed_at` and listed
  in `cards_unmatched` (with the reason) only when a successful response
  says there's nothing usable: its ID is missing and the search found
  nothing confident, it failed verification, or it has no usable TCGplayer
  price. Its old price is kept. A stamped card isn't searched again for 14
  days (`PRICE_RETRY_AFTER_DAYS`), but it stays in the daily ID batch
  (that costs nothing), so a price that appears is picked up the next day
  and clears the stamp.
- **Outages.** A timeout, 429 or 5xx is retried once after a 3 s back-off
  (honouring `Retry-After`, capped). If it persists, nothing is written for
  that request's cards: no price, no stamp, they're due again tomorrow (or
  on a manual re-run). 3 such failures in a row, or one persisting 429,
  stop the pass for the day (`stopped: "errors"` / `"rate_limited"`), and
  so does the time budget, 100 s (`jobs.POKEMONTCG_SECONDS`, `stopped:
  "time"`). The cron still runs TCGdex, resolves and snapshots.
- **Reported.** The cron JSON has `requests` (HTTP requests, retries
  included), `batch_requests`, `fallback_searches`, `cards_checked`,
  `cards_updated`, `cards_unmatched`, `cards_low_confidence` (a search hit
  that wasn't a confident match), `cards_variant_uncertain`,
  `cards_backed_off`, `cards_deferred` (due but not reached), `ids_found`,
  `transient_errors` and `stopped`. The Sync status line reads e.g.
  "TCGplayer (pokemontcg.io): 10 requests, priced 452 of 470, 18 unmatched,
  0 transient errors".

**Currency.** TCGplayer prices come back in USD and are stored in NOK,
converted at **Norges Bank's daily USD/NOK spot rate** (`fx_rates.py`,
`EXR/B.USD+EUR.NOK.SP`, no API key). The rate is fetched once per run,
cached in-process and stored in the `fx_rates` table (date, currency,
`rate_nok`), so another invocation within 6 hours reuses it without a
request. If Norges Bank can't be reached, the last rate fetched in that
process is reused, else the latest rate in `fx_rates`, and only if there is
none it falls back to the old fixed constants (USD 10.5, EUR 11.5). The cron
response reports `usd_to_nok`, `fx_source` (`live` / `last-known` /
`stored` / `fallback`) and `fx_as_of`.

**A price is never stored at the fallback constant** (issue #229; 10.5 is
the ~10%-inflated rate #209 removed). When the only rate available is
`fallback` (Norges Bank has never answered and `fx_rates` is empty), or a
currency was filled in from the constant (`FxRates.usable()`), every price
write path skips: `price_refresh` (cron and `--reprice-all`) requests
nothing, and the TCGdex pass stops before any request (`stopped:
"fx_unavailable"`). The Dex sync needs no rate (it writes NOK prices only).
Nothing is stamped, neither a price nor
`lookup_failed_at`, so freshness doesn't advance and every card stays due
for the next run. The run is reported as degraded, not "ok":
`/cron/price-refresh` returns HTTP 200 with `"status": "degraded"`, a
`degraded_reason` and `cards_skipped`, and its `tcgdex` summary has its own
`status`/`degraded_reason`. A degraded price refresh is
logged on Sync status as `degraded`. The snapshot and image passes still
run. EUR/NOK comes
in the same request and is used for Cardmarket via TCGdex (see "TCGdex"
above). Each `pokemontcg` row records the native USD price
and the rate it was converted at (`card_prices.price`/`fx_rate`); rows
seeded from before #210 have only the NOK value. Prices stored before
issue #209 were converted at the fixed 10.5, ~10% too high, until re-fetched.

**Forcing a full re-price** (every card that already has a `pokemontcg`
price, whatever its date, through the same batch path; no fallback search;
a card with nothing usable keeps its old price and date and is not stamped;
stored values are never rescaled):

```bash
cd apps/tcg_inventory
python price_refresh.py --reprice-all --dry-run   # cards, IDs, requests + the FX rate it would use
python price_refresh.py --reprice-all             # [--limit N distinct IDs, oldest-priced first]
```

Same `DATABASE_URL` convention as `seed_set_release_order.py`. Without
`--reprice-all` it runs one normal daily pass (no time budget).

The fallback name search (`card_images._search_card`, also the Dex sync's
image fallback) does fuzzy name matching, so its top result isn't
guaranteed to be the exact card searched for. Its name and printed number
must match exactly (`_is_confident_match`) before its price is trusted;
otherwise the card is listed in `cards_low_confidence` and stamped, never
priced from a guess.

A confidently-matched card can still have more than one print (normal,
holofoil, reverse holofoil, 1st edition, ...), each with its own
`tcgplayer.prices` entry and potentially a very different market price.
`card_images._match_variant_key` picks the print by Dex's Variant as a
masterdata code (`masterdata.normalize_variant`), never by substring, and
only takes a key that is exactly that print (issue #350). Keys are compared
lowercased with hyphens removed, so TCGdex's `reverse-holofoil` is
pokemontcg.io's `reverseHolofoil`. The same rule serves both TCGplayer
sources (`pokemontcg` and `tcgdex_tcgplayer`):

| Variant code | TCGplayer key |
|---|---|
| `normal` | `normal`; on WotC sets `unlimited` |
| `holo` | `holofoil` only. WotC's `unlimitedHolofoil` / `1stEditionHolofoil` stay unmatched |
| `reverse_holo` | `reverseHolofoil` |
| `first_edition` | `1stEdition`; on a holo-only WotC card its one 1st Edition key |
| `first_edition_holo` | `1stEditionHolofoil` |
| `unspecified` (blank) | nothing to match |
| anything else: `poke_ball_holo`, `master_ball_holo`, any `*_ball_holo`, `cosmos_holo`, `cracked_ice_holo`, `expansion_stamp`, `shadowless`, any new code | **no TCGplayer price at all** |

The last row matters most. TCGplayer (as pokemontcg.io and TCGdex expose
it) has no key for those prints, so any price there belongs to another
print of the card (e.g. Prismatic Evolutions' Poké Ball / Master Ball
cards only have `normal` / `holofoil` / `reverseHolofoil`). Such a card
gets nothing from either TCGplayer source, not even when the card has just
one priced print, so the chain falls through to Dex or Cardmarket (which
prices ball patterns as their own product). The pokemontcg refresh doesn't
ask for those cards at all (`price_refresh._load_cards`).
Rows stored before #350 by the old rule are deleted at the start of each
price cron (`pricing.drop_other_print_tcgplayer_rows`, run by both
`refresh_stale_prices` and `refresh_tcgdex_prices`, which also clears the
`tcgplayer_price` mirror and re-resolves the cards), so already-stored
rows are corrected with no manual database write.

For a TCGplayer-keyed variant (or a blank one) on a card with one priced
print, that print is used. With several priced prints and no exact key
(a blank variant, or a WotC "Holo"), the first one present is still used
(better than no price) but flagged — listed in the response's
`cards_variant_uncertain` — worth
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
  (time-boxed to ~50 s; call again while `remaining` > 0; better with
  `Authorization: Bearer <CRON_SECRET>`, `?secret=` is deprecated, see
  "Cron endpoint auth");
- by hand: `python backfill_images.py [--limit N]` (same `DATABASE_URL`
  convention as `seed_set_release_order.py`).

A Dex sync (`importer.py`) also tries the by-id lookup for new cards.

### Value history

`card_snapshots` records real history going forward — every sync writes one
row per card (`qty`, `reference_price` and `price_source` as of that day)
right after it completes, so `queries.real_value_history` can report what the collection
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
  and last day's per-card rows are read.
- **Card count line** (issue #243): the number of cards per day for the
  selected metric — unique cards owned (qty > 0) / extra copies
  (qty − 1) / all copies — the same `card_count` `real_value_history`
  returns alongside each day's value, so it follows the metric and period
  pills. Drawn blue, dotted and stepped (a count holds until the next
  snapshot day) on its own right-hand, whole-number axis fitted
  separately from the kr axis; on by default and toggled with the
  "Cards" pill (which doubles as its legend). It replaced the old orange
  "card count changed that day" point markers. The tooltip shows the count
  and its delta; with the line hidden, the value point's tooltip still
  lists it ("no change — price only" otherwise). "View as table" has the
  same count in its Cards column.
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

**Column semantics.** Despite its name, `card_snapshots.reference_price`
holds the card's *resolved market price* that day (`Card.display_price`,
see "Pricing") — the column predates the resolver and `init_db()` can't
rename it. `card_snapshots.price_source` (issue #210) records which source
that price came from; it's `NULL` on rows written before #210 and on cards
with no price. Price movers (Dashboard) leaves out cards whose source
differs between the period's start snapshot and today and says so in its
caption ("· N source changes left out") — a switch from Dex to
pokemontcg.io isn't a market move. For pre-#210 snapshots the source is
inferred (TCGplayer via pokemontcg.io if the card has such a price, else
Dex — the old display rule). The value-history and per-card price charts
don't leave switches out, so they keep matching the key figures.

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
points if both cron jobs and a manual sync all land on it.

Snapshots are never rewritten after the fact: days snapshotted before the
Norges Bank rate replaced the fixed 10.5 USD/NOK (issue #209) keep their
~10% too high TCGplayer-derived values, so the chart can show a one-off
drop as cards get re-priced. See `HANDOFF.md`. There is no
manual CSV-upload page in the app (removed — the only sync entry points are
the Dropbox-based ones above and the price-refresh cron).

## Backups and restore

Issue #223. `transactions` (what was actually paid) is the one dataset that
can't be rebuilt from a Dex export, and prod is sometimes edited directly
with SQL, so prod gets an **encrypted weekly `pg_dump`** in a Dropbox folder
we control, taken by the GitHub Actions workflow
`.github/workflows/prod-backup.yml`. This repo is public, so the workflow
is built so that nothing unencrypted reaches its logs or artifacts: see the
header comment in the workflow file.

### What Supabase itself provides

From Supabase's docs
([Database backups](https://supabase.com/docs/guides/platform/backups),
checked 2026-09-30):

| Plan | Automatic daily backups | Retention | Downloadable |
|---|---|---|---|
| Free | **None.** Supabase tells Free projects to export their own data regularly (`supabase db dump` / `pg_dump`) and keep it off-site | — | — |
| Pro | Yes | 7 days | No, for projects on the newer *physical* backups (Postgres 15.8.1.079 and later, which covers prod's Postgres 17): "they are not available for direct download". Restore-in-place only |
| Team | Yes | 14 days | Same as Pro |
| Enterprise | Yes | up to 30 days | Same as Pro |

- **PITR** (point-in-time recovery, 2-minute RPO) is a paid add-on on
  Pro/Team/Enterprise, needs at least the Small compute add-on, and costs
  about $100/month for 7 days' retention (up to about $400/month for 28
  days). With PITR on, Supabase stops taking daily backups.
- A dashboard restore overwrites the **whole project**, and the project is
  down while it runs. It can't restore a single table, and it doesn't
  restore custom roles' passwords.
- Backups don't include Storage API objects (this app stores none).

**Prod (`nverpumoregkjfeddrwa`) is on the Free plan** (confirmed by the
user, 2026-09-30), so Supabase keeps no backups of it at all and the
workflow below is the only backup. If the project ever moves to Pro, the
workflow stays useful as a second, downloadable, off-Supabase copy that can
restore individual tables.

### How the backup works

Weekly (Sundays 03:17 UTC) and on demand (`gh workflow run prod-backup.yml`):

1. `pg_dump` 17 (from the PGDG apt repo, since `pg_dump` must be at least
   the server's major version and prod runs Postgres 17) dumps the whole
   `public` schema, which covers every app table (`transactions`, `cards`,
   `collections`, `listings`, `releases`, `card_snapshots`, masterdata, ...).
   It uses `--format=custom --no-owner --no-privileges`.
2. The dump is piped straight into `age -r <public key>`, so plaintext never
   touches the runner's disk. CI only has the age **public** key: it can
   create backups but can't read them.
3. The `.dump.age` file is uploaded to Dropbox under
   `tcg_inventory-YYYYMMDDTHHMMSSZ.dump.age` (`mode: add`, `autorename`, so
   it never overwrites). The log shows only the file name, the encrypted
   size, and the Dropbox path.

If any required secret is missing, the job logs a warning that names the
missing secrets and ends green without doing anything. That's the state
until the setup below is done.

**Connection string: use the Session pooler.** GitHub-hosted runners are
IPv4-only. Supabase's direct connection host (`db.<ref>.supabase.co`) is
IPv6-only unless you pay for the IPv4 add-on, and the shared pooler accepts
IPv4. Use the **Session pooler** string (port **5432** on
`aws-N-<region>.pooler.supabase.com`, username
`<role>.nverpumoregkjfeddrwa`), not the transaction pooler on 6543 that the
app itself uses (see "Deploying" above). pg_dump needs a real session.

### One-time setup (user)

1. **age keypair**, on your own machine (`brew install age`):
   ```bash
   age-keygen -o tcg-backup-key.txt
   # prints "Public key: age1..."  <- this is BACKUP_AGE_RECIPIENT
   ```
   `tcg-backup-key.txt` is the **private** identity and the only thing that
   can decrypt a backup. Store it **outside this repo**: in a password
   manager (as a secure note) plus one offline copy (e.g. a USB stick or a
   printout). Then delete the loose file. If it's lost, every backup is
   unreadable. It never goes into GitHub or Dropbox.
2. **Dropbox app for backups.** This is a *separate* app from the Dex
   import one. Go to [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
   → Create app → **Scoped access** → **App folder** (the app then only
   sees its own `Apps/<app name>/` folder, so it can't touch the Dex export
   folder and vice versa). Under **Permissions**, enable
   `files.content.write`, then submit. Note the App key and App secret.
   Get a refresh token (`dropbox_setup.py` only requests the read scopes,
   so do this by hand):
   ```bash
   # 1. Open in a browser, approve, and copy the code:
   #    https://www.dropbox.com/oauth2/authorize?client_id=<APP_KEY>&response_type=code&token_access_type=offline
   # 2. Exchange it (the response's "refresh_token" is what you need):
   curl -s https://api.dropbox.com/oauth2/token \
     -d grant_type=authorization_code -d code=<CODE> -u '<APP_KEY>:<APP_SECRET>'
   ```
3. **Read-only Postgres role.** This is a **write to prod**: run it
   yourself (Supabase SQL editor) or confirm it before an agent does, and
   log it in `HANDOFF.md`. A read-only inspection on 2026-09-30 found every
   `public` table, sequence, and index owned by `postgres`, which is who
   `init_db()` connects as, so the default-privileges lines below name
   `postgres`. That way tables `init_db()` adds later are covered
   automatically.
   ```sql
   CREATE ROLE backup_reader WITH LOGIN BYPASSRLS PASSWORD '<generate-a-strong-password>';
   GRANT CONNECT ON DATABASE postgres TO backup_reader;
   GRANT USAGE ON SCHEMA public TO backup_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_reader;
   GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_reader;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO backup_reader;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_reader;
   ```
   `BYPASSRLS` is needed because row-level security is enabled on every
   `public` table (since 2026-09-30, see "Database access hardening"
   above). Without it, `pg_dump` aborts with "query
   would be affected by row-level security policy" (reproduced in the drill
   below). The role still has only `SELECT`. Supabase's `postgres` role
   has `BYPASSRLS` and `CREATEROLE`, which Postgres 16+ requires to grant
   the attribute. The pooler picks up a new role automatically, with no
   separate registration step.
4. **GitHub secrets** (Settings → Secrets and variables → Actions, or
   `gh secret set NAME` with no value argument so it prompts and the value
   stays out of shell history):

   | Secret | Value |
   |---|---|
   | `BACKUP_DATABASE_URL` | Session pooler URL as `backup_reader`: `postgresql://backup_reader.nverpumoregkjfeddrwa:<password>@aws-1-eu-west-1.pooler.supabase.com:5432/postgres` (copy the host from Dashboard → Connect → Session pooler) |
   | `BACKUP_AGE_RECIPIENT` | the `age1...` public key from step 1 |
   | `BACKUP_DROPBOX_APP_KEY` | backup Dropbox app's key |
   | `BACKUP_DROPBOX_APP_SECRET` | backup Dropbox app's secret |
   | `BACKUP_DROPBOX_REFRESH_TOKEN` | refresh token from step 2 |

   Optional **variable** (not a secret) `BACKUP_DROPBOX_PATH`: the
   destination folder, default `/backups`. With an App-folder app this is
   relative to `Apps/<app name>/`.
5. **Test it:** `gh workflow run prod-backup.yml`, then
   `gh run watch` / `gh run list --workflow prod-backup.yml`. The run
   summary shows the Dropbox path. Then do the restore below once with that
   real file.

### Restoring a backup

Decrypted dumps contain everything, including every price paid, so do this
**outside the repo** (e.g. in `~/tcg-restore/`) and delete the plaintext
afterwards.

```bash
# 1. Download the .dump.age file from Dropbox, then decrypt it:
age -d -i /path/to/tcg-backup-key.txt tcg_inventory-20261004T031700Z.dump.age > restore.dump

# 2. A throwaway local Postgres 17 (any of these):
docker run -d --name tcg-restore -e POSTGRES_PASSWORD=restore -p 55432:5432 postgres:17
#    or without Docker: the EDB zip binaries / Postgres.app, then initdb + pg_ctl
export PGPASSWORD=restore
createdb -h localhost -p 55432 -U postgres tcg_restore

# 3. Restore. The dump contains `CREATE SCHEMA public`, which a fresh
#    database already has, so skip that one entry (otherwise pg_restore
#    reports "schema public already exists" and exits non-zero):
pg_restore -l restore.dump | grep -v ' SCHEMA - public ' > restore.list
pg_restore -h localhost -p 55432 -U postgres --no-owner --no-privileges \
  --exit-on-error -L restore.list -d tcg_restore restore.dump

# 4. Sanity check:
psql -h localhost -p 55432 -U postgres -d tcg_restore -c "
  SELECT (SELECT count(*) FROM transactions) AS transactions,
         (SELECT count(*) FROM cards)        AS cards,
         (SELECT count(*) FROM collections)  AS collections,
         (SELECT count(*) FROM listings)     AS listings,
         (SELECT count(*) FROM releases)     AS releases,
         (SELECT max(version) FROM schema_meta) AS schema_version;"

# 5. Optional: run the app against it
cd apps/tcg_inventory && DATABASE_URL=postgresql://postgres:restore@localhost:55432/tcg_restore python app.py

# 6. Clean up the plaintext
rm restore.dump restore.list && docker rm -f tcg-restore
```

**Real disaster (restore into Supabase):** either restore into a **new**
Supabase project (steps 3–4 against its Session pooler URL as `postgres`,
then point Vercel's `DATABASE_URL` at it) or, to repair specific tables in
the existing project, restore just those with `pg_restore -t <table>
--data-only` after emptying them (or restore into a scratch local DB and
copy the rows you need across). Take a fresh backup of the current state
first. Restoring over prod is itself a direct prod write (see the root
`CLAUDE.md`, "Prod database access").

### Restore drill

**2026-09-30, local, synthetic data (no prod data involved):** Postgres
17.11 (EDB macOS binaries), age 1.3.2, SQLAlchemy 2.0.52/psycopg2 2.9.13.
Schema created by `init_db()` against an empty Postgres DB (schema version
10, 20 tables), then seeded with 25 cards, 3 collections, 25
`card_collections`, 20 transactions, 1 listing (2 `listing_cards`), and 3
releases (incl. non-ASCII `æøå` text). The role SQL above was applied
as-is (with the database name swapped), then a table was created *after*
the grants and RLS was enabled on `transactions`, to exercise the
default-privileges and `BYPASSRLS` lines. The workflow's own dump and
Dropbox-upload shell steps were extracted and run as the `backup_reader`
role (upload against a local mock of Dropbox's two endpoints). Then came
`age -d` and the `pg_restore` above into a second, fresh database.
**Result: pass.** Every table's row count matched source and restore
(including the post-grant table), sequences carried over
(`transactions_id_seq` = 20), the non-ASCII text survived, and the app's
`/`, `/transactions`, `/listings` and `/releases` pages all returned 200
against the restored DB. Also confirmed: without `BYPASSRLS`, `pg_dump`
fails on the RLS-enabled table.

**Prod drill: pending.** "One real backup produced and restored" needs the
setup above and one successful workflow run. After that, restore that real
file locally following the steps above and add a dated line here.

### Follow-ups (not built)

- **Retention/pruning:** nothing deletes old backups. At prod's current
  size a weekly file is well under a few MB, so it's not urgent. Prune by
  hand, or add a pruning step later (`files/list_folder` +
  `files/delete_v2`, which needs `files.content.write` plus
  `files.metadata.read`).
- A backup failing silently: a failed run shows as a red scheduled run in
  Actions (and GitHub emails the workflow's last editor), but nothing else
  alerts.

## Project layout

- `app.py` — FastAPI app, routes, entrypoint (`python app.py`).
- `db.py` — SQLite (local) / Postgres (Supabase, via `DATABASE_URL`) engine
  and session setup.
- `auth.py` — Supabase Auth login + JWT verification.
- `models.py` — SQLAlchemy models + computed properties.
- `constants.py` — the Dex category → binder/collection/priority mapping.
- `importer.py` — CSV parsing and sync logic.
- `queries.py` — dashboard aggregation queries.
- `form_validation.py` — boundary validation of type/price/date/row-list
  form inputs and the 422 messages (see "Form validation" above).
- `masterdata.py` — canonical card identity + external ID mapping (see
  "Masterdata" above).
- `snapshots.py` — writes daily `card_snapshots` rows (see "Value history").
- `won_inbox.py` — the Facebook wins inbox: checks fb_auction_watcher's
  payload and stages it in `won_items`, and the link flow's candidate
  matching and Register bookkeeping (see "Facebook wins inbox" above).
- `job_locks.py` — single-flight guard for background jobs (`job_locks`
  table; see "Sync status" → "Single-flight guard").
- `sync_status.py` — records background-job runs in `import_log` and builds
  the `/sync-status` at-a-glance block (see "Sync status").
- `pricing.py` — per-source prices (`card_prices`) and the resolver that
  materializes `cards.market_price` (see "Pricing" above).
- `price_refresh.py` — standalone TCGplayer price refresh, decoupled from Dex
  sync (see "Price refresh" above). Also the `--reprice-all` CLI.
- `tcgdex_prices.py` — TCGdex's TCGplayer + Cardmarket prices with
  verified `tcgdex` IDs, run inside `/cron/price-refresh` (see "Pricing" →
  "TCGdex" above). Also a CLI.
- `fx_rates.py` — Norges Bank daily USD/EUR→NOK rates, cached per process
  and stored in `fx_rates`, with last-known/stored/constant fallback (see
  "Price refresh" above).
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
    `_widen_card_snapshot_source_constraint()` →
    `_backfill_import_log_defaults()` (#264) →
    `_enable_row_level_security()`) behind a single-row
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
  - `_enable_row_level_security()` (#239, Postgres only, no-op on SQLite)
    runs `ALTER TABLE public."<name>" ENABLE ROW LEVEL SECURITY` for every
    table in `Base.metadata.sorted_tables` that doesn't have RLS yet
    (`pg_class.relrowsecurity`, #340). Idempotent and adds no policies: Supabase's `anon`/`authenticated` roles
    get no rows, while the app connects as `postgres` (`BYPASSRLS`) and is
    unaffected. Because it's version-gated, a new table gets RLS on the
    first start after the deploy that bumps `CURRENT_SCHEMA_VERSION` for
    it, which every table-adding change already does.
  - `_backfill_sets()` (see "Chronological sorting" above) is one
    exception to that gate (so are `_backfill_master_cards()` and
    `_backfill_card_prices()`, both a single cheap `SELECT` once done) — it runs on every `init_db()` call regardless
    of `schema_meta`'s stored version. Since issue #134, `importer.py`
    links `Card.set_id` inline as it syncs, so this is no longer the
    primary mechanism keeping cards linked — it's an ongoing catch-all for
    any card that ends up unlinked some other way, not one-time
    schema/data cleanup like the rest of the chain.
  - **A cold start against an up-to-date database writes nothing** (#340,
    guarded by `tests/test_db.py::test_cold_start_against_up_to_date_database_writes_nothing`).
    The every-start backfills only touch rows that still need it, and the
    version-gated chain's lock-taking DDL (`_enable_row_level_security()`,
    the `card_snapshots.source` ALTERs) only runs for a table/column not
    already in its final state, since even a no-op `ALTER TABLE` takes an
    `ACCESS EXCLUSIVE` lock on Postgres. A new every-start backfill must
    keep that property: a write on each cold start locks rows a running
    sync may be updating.
- Silent session refresh — an expired Supabase session redirects to
  `/login` instead of refreshing quietly in the background.
