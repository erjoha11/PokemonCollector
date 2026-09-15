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
