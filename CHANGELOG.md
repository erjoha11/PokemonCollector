# Changelog

This file is new as of 2026-09-19 — there was no prior CHANGELOG/RELEASE_NOTES
in the repo. It's separate from `tcg_inventory`'s own in-app "Release Notes"
page (`/releases`, added below in this same entry, issue #144) — that page is
a user-facing changelog inside the running app itself; this file is the
repo-level record for anyone reading the source, and isn't meant to duplicate
every entry that also gets typed into `/releases` by hand.

Entries are grouped by app, since apps in this monorepo are independent.
This first entry backfills everything merged to `main` since 2026-09-14
(the earliest session `apps/tcg_inventory/HANDOFF.md` covers); it does not
attempt to reconstruct the project's earlier history. From here on, each
new entry should cover only what's merged since the previous one.

## 2026-09-23

### finn_ad_scraper

- Removed. The app was an unused experiment that mostly added confusion;
  `tcg_inventory` is now the only app. It remains in git history if it's
  ever wanted back. `tcg_inventory`'s `CARD_CONDITIONS` (previously kept in
  sync with the scraper's list by convention) is now the single source of
  truth, and `.env.example` now lists `tcg_inventory`'s env vars instead of
  the scraper's `ANTHROPIC_API_KEY`.

## 2026-09-19 — backfill covering 2026-09-14 through 2026-09-19

### tcg_inventory

**Sales / Listings ("Sell on finn.no")**
- Select cards on Inventory, generate finn.no ad copy, and track the
  resulting listing end to end: new `/sales` flow (#119), read-only
  `/listings` overview (#120, PR #121), delist action (#125, PR #131),
  edit (price/cards/ad text) and delete (#126, PR #147), and marking a
  listing sold by linking it to real `Transaction` row(s) (#127, PR #148).

**Set data & completion tracking**
- New first-class `Set` entity FK'd from `Card`, replacing the old
  string-matched `set_release_order` join (#133, PR #137); dashboard's
  series breakdown migrated onto it (#138, PR #139); `Set.total_cards`/
  `release_rank` backfilled from `api.pokemontcg.io` (#136, PR #140); the
  Dex CSV importer now writes `Card.set_id` inline during import instead of
  relying solely on a later backfill pass (#134, PR #141); set completion %
  (owned / `Set.total_cards`) now shown in the Series drilldown (#135/#142,
  PR #143).

**Pricing accuracy**
- Fixed a USD-stored-as-NOK currency bug in TCGPlayer price fetching
  (PR #95); TCGPlayer price refresh decoupled from the Dex sync cron, with
  a match-confidence guard so a low-confidence API match no longer silently
  writes a wrong price (PR #101); price matching now prefers a card's
  specific print variant when the API offers more than one (PR #105).

**Dashboard / value correctness**
- 0-qty cards (owned == 0 but still present in the latest Dex export) no
  longer inflate "unique value"/Value KPIs or `top_valuable_cards`; added an
  inventory "0 owned" badge and a filter to show/hide them (#132, PR #145).
- Backfilled missing `Card.image_url` so the Dashboard's "Most valuable
  cards" thumbnail actually renders (#128, PR #150).
- "Most valuable cards" KPI: scrollable list up to 50 entries (PR #122),
  now shows card number and language (PR #129).
- Collection Value Growth replaced with a real-history "Market Value" chart
  built from actual daily snapshots instead of today's-price-applied-
  retroactively (#114, PR #116); the chart now also shows card counts
  (PR #118).

**Transactions / order flow**
- Order-level edit endpoint: retype, relink, move/merge/split rows between
  orders, add a note — the first UI path for corrections that previously
  required raw SQL against prod (#109, PR #112).
- Bulk-edit platform across an already-registered order (#103, PR #104).
- htmx/accessibility consistency polish (#108, PR #110) and fixes for a
  shipping-field-omission bug and state loss on the order form (PR #111).
- Simplified and restyled the order summary line (#113, PR #115).
- Merged the standalone "Analyse" page into Transactions (PR #91); merged
  the "Kort lagt til" date-grouped tables into one Recently Added table
  (PR #80); turned Import/Sync into a passive, read-only "Synk-logg" page
  (PR #87).

**In-app Release Notes page**
- New `/releases` page: add a dated title/body entry, delete an existing
  one (no edit route by design — delete-and-re-add is the documented fix
  for a typo) (#144, PR #149); two `ux`-flagged delete-flow fixes
  (confirmation names the entry; delete is now idempotent) ported over
  from a superseded PR (#151, PR #152).

**Other**
- `init_db()`'s migration chain is now gated behind a schema-version check,
  so a cold start on an already-migrated database doesn't re-run every
  additive migration every time (PR #99).

### finn_ad_scraper

No merged changes in this window.

### Repo / agent tooling

Several PRs reshaped the `.claude/agents/` pipeline itself rather than
either app: added a `developer` agent with full GitHub access (PR #98),
added and then reworked an "advisor" agent into today's `project-manager`
(PRs #100, #106, #117), let `project-manager` merge ready PRs (PR #124),
and redesigned the overall pipeline so `architect` files tickets and
`project-manager` owns spawning `developer` (#123) — the arrangement this
document's own workflow now follows.
