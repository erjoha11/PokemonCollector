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
  above).
- The "Pris" / "Registrert pris" side-by-side naming ambiguity (see #85
  above).

## 2026-09-15 session — Wiki staleness fix (UX review item #2)

- Fixed `templates/wiki.html`: the "Import / Sync" ToC entry/section is now
  "Synk-logg" and moved to its actual nav position (after Analyse, before
  FAQ), matching #87's page rename. Content rewritten to describe the
  current read-only Synk-logg page (no manual-upload/Dropbox-browser UI
  anymore) instead of the old manual-CSV/Dropbox-browser flow.
- Also fixed adjacent staleness the UX review didn't call out explicitly:
  the Transactions section still described #86's old inline buy-form
  (Pris + Kjøps-ID inputs) instead of the current "+ Legg til i ordre"
  button / open-cart behavior; the Oversikt section still implied manual
  CSV upload is user-facing.
- `test_app.py::test_wiki_page_documents_the_main_features` asserted the
  stale "Import" label — updated to "Synk-logg", plus a new
  `test_wiki_page_reflects_current_nav_labels` that scrapes `base.html`'s
  actually-rendered nav labels and asserts each is documented somewhere on
  `/wiki`. That test should catch the *next* nav rename automatically
  instead of relying on someone noticing the wiki drifted.

**Process recommendation** (the user asked whether keeping the wiki current
should be "an agent"): no — a dedicated agent doesn't help here since
someone still has to remember to invoke it, same as remembering to update
the page by hand. Recommended instead:
  1. A `CLAUDE.md` convention: "a template/route rename or user-facing
     behavior change should update the matching `templates/wiki.html`
     section in the same PR" — same discipline as the existing
     importer.py/README.md rule. **Not added here** because `CLAUDE.md` is
     currently mid-edit/uncommitted in a concurrent session's main
     checkout — add it there once that lands, don't recreate the file from
     scratch in a worktree and risk clobbering that work.
  2. The mechanical test above, for the specific "label renamed, wiki still
     says the old name" failure mode — it runs on every `pytest` invocation,
     no one has to remember anything.
  3. For *behavioral* staleness (like the #86 case here, which no test can
     catch generically), the existing `ux` agent's periodic review is the
     right tool — its brief already cross-checks against `README.md`; worth
     extending it to explicitly check `templates/wiki.html` against current
     templates too, next time it's run.

## 2026-09-15 session (same day, follow-up) — Wiki visual "lift" pass

User asked for a one-time visual polish of `templates/wiki.html` on top of
the content fix above ("lifting and elevating" it) — the page looked
plain/afterthought-y next to the rest of the app's richer design language
(KPI cards, colored badges, collapsible sections). Ran the `ux` agent for a
read-only design review first rather than guessing; implemented its
ordered, reuse-existing-patterns recommendations:

- ToC: was a boxed 2-column plain `<ul>` of underlined links — replaced
  with a `.viz-filter`/`.viz-filter-pill` row (the same pill-nav component
  Analyse already uses for its metric filter), sitting directly under
  `<h1>` with no `.result-box` wrapper, matching how every other page pairs
  its `<h1>` with a boxless module underneath.
- Each of the 8 topic sections got a `.wiki-section` class + a
  `.wiki-accent-{1..5}` class (cycling) — a small colored dot before the
  `h2`, reusing the KPI cards' accent-dot CSS motif purely for scannability
  across an otherwise-identical stack of gray boxes. **These colors are
  arbitrary and carry no fixed meaning** (unlike the KPI hues, which are
  tied to a specific category per the comment at `style.css` ~216-219) —
  don't extend this pattern anywhere that implies otherwise.
- FAQ's static `<dl>` (all 4 Q&As always expanded) became 4
  `<details class="collapsible">` blocks, the exact idiom already used for
  Historikk's purchase groups and the "Resten av samlingen uten kjent
  dato" section in `transactions.html`.
- New CSS lives right after `.result-box` in `static/style.css` (~10 lines:
  `.wiki-section h2::before` + 5 `.wiki-accent-N` variables). No new
  classes invented beyond that — everything else reuses `.viz-filter`,
  `.viz-filter-pill`, and `.collapsible` verbatim.

Deliberately **not** done (flagged by the `ux` agent as content
restructuring, not decoration, and out of scope for a "visual lift"):
pulling "Oversikt" out of the boxed-section pattern into a lead paragraph,
and a sticky nav (the app has no sidebar-shell pattern anywhere to extend —
would mean inventing new page structure, not reusing one).

Verified: full test suite green (175 passed, including the two wiki tests
from the earlier session), plus a manual `python app.py` + `curl /wiki`
smoke test confirming the page still renders 200 with the expected new
markup (9 pills, 4 collapsible FAQ entries, 8 accented sections) — no
visual/screenshot review was done since this environment has no browser,
so the actual look (color cycle against 8 sections, pill-row wrap at
narrow widths) hasn't been eyeballed. Worth a quick look before/after
merging if that matters.
