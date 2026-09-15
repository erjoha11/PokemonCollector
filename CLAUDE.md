# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository structure

This is a monorepo of small, independent Python apps for buying and collecting Pokemon cards, each under `apps/`. Apps do not import from each other. Each has its own `requirements.txt` and README with app-specific detail — read the relevant app's README before making non-trivial changes there.

- `apps/finn_ad_scraper/` — scrapes a finn.no ad (title/description/price/photos) and identifies Pokemon cards in the photos via Claude vision.
- `apps/tcg_inventory/` — FastAPI + Jinja2/HTMX webapp tracking a physical card collection (replaces an Excel workbook). Runs locally on SQLite or deployed on Vercel + Supabase.

## Setup and common commands

From repo root:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # installs both apps' requirements + pytest
playwright install chromium              # only needed for finn_ad_scraper's headless-browser fallback
cp .env.example .env                     # then fill in ANTHROPIC_API_KEY
```

Run the whole suite (both apps, offline — no network, API keys, or Playwright install required):

```bash
python -m pytest
```

Run a single app's tests, or a single test file/case:

```bash
python -m pytest apps/tcg_inventory
python -m pytest apps/tcg_inventory/tests/test_importer.py
python -m pytest apps/tcg_inventory/tests/test_importer.py::test_some_case -v
```

Run each app directly:

```bash
python -m finn_ad_scraper.cli "https://www.finn.no/recommerce/forsale/item/123456789"   # from apps/finn_ad_scraper's parent on the path, or via the package
cd apps/tcg_inventory && python app.py    # serves http://localhost:8000, SQLite auto-created
```

## Architecture notes

### finn_ad_scraper

Two-step pipeline, both steps independently testable against fixtures/fake clients:

1. `fetch_finn_ad(url)` (`finn_ad.py`) — plain HTTP GET first (finn.no embeds a JSON-LD `Product` block that needs no JS); falls back to headless Chromium via Playwright only if that fails.
2. `identify_cards(images, ad_context)` (`card_identifier.py`) — sends ad photos to Claude vision, returns each card's name/set/number/holo/condition.

### tcg_inventory

FastAPI app with flat imports (`from db import ...`, not a relative package) so `python app.py` works standalone — `tests/conftest.py` adds the app directory to `sys.path` to mirror this for the test suite. Key modules:

- `db.py` — engine/session setup. SQLite locally by default; set `DATABASE_URL` for Postgres (Supabase). On a read-only host (e.g. Vercel) with no `DATABASE_URL`, it fails fast with an actionable error rather than a cryptic write failure later. `init_db()` only ever creates missing tables/columns (`CREATE TABLE IF NOT EXISTS` + an additive `ALTER TABLE ADD COLUMN` pass) — it never drops/renames/retypes, so a real schema change needs a deliberate migration path, not just editing a model.
- `auth.py` — Supabase Auth (GoTrue) over plain HTTP, no supabase-js. Verifies the session JWT against Supabase's JWKS endpoint first, falling back to `SUPABASE_JWT_SECRET` only for legacy HS256 projects. Auth is skipped entirely when `SUPABASE_URL`/`SUPABASE_ANON_KEY` aren't set, which is what keeps local `python app.py` login-free.
- `models.py` — SQLAlchemy models. `duplicates`, `total_value`, `unique_value` are **never stored**, always computed (`Card.duplicates` etc., and the dashboard aggregates in `queries.py`) — a stored, independently-maintained `duplicates` drifting from `qty` was a real bug in the Excel system this replaces.
- `constants.py` + `importer.py` — encode the business rules migrated from the Excel system (Dex category routing to collection/binder, primary-collection priority for dashboard credit, sync-vs-full-load semantics). These rules are documented in detail in `apps/tcg_inventory/README.md` — **do not change the routing/priority logic without updating both the code and that doc.**
- `queries.py` — dashboard aggregation queries.
- `snapshots.py` — writes one `card_snapshots` row per card (qty + `reference_price`) each time the daily cron completes a sync, so `queries.real_value_history` can report actual historical value instead of `collection_value_growth`'s today's-price-applied-retroactively approximation. See `apps/tcg_inventory/README.md` "Value history". Backend-only so far — no dashboard/Analyse UI consumes it yet.
- `dropbox_client.py` / `dropbox_setup.py` — read-only Dropbox integration for pulling Dex CSV exports directly, optional.
- `api/index.py` + `vercel.json` — Vercel entrypoint; `api/index.py` just re-exports `app` from `app.py`, all routes live in the one place. `vercel.json` also schedules the daily Dropbox auto-sync cron (`GET /cron/dropbox-sync`), which only ever does a normal sync (flags missing cards, never deletes).
- `templates/` + `static/` — Jinja2/HTMX frontend, no build step, no CDN dependency (HTMX is vendored).

Data model: `cards`, `collections`, `card_collections` (many-to-many), `binders`, `transactions`, `set_release_order` (chronological-sort lookup table, ships empty until seeded).

Tests use an in-memory or temp-file SQLite database (`tests/conftest.py`'s `db_session`/`client` fixtures) — the real `tcg_inventory.db` is never touched. The `client` fixture monkeypatches `db.engine`/`db.SessionLocal` and reloads `app` so routes bind to the throwaway DB.

There is no UI yet to edit an already-registered order (retype/relink/move/merge/split/add note) — this is a known, intentionally-deferred gap; corrections currently require raw SQL against the live database. See `apps/tcg_inventory/HANDOFF.md` for the most recent session's direct production-database changes (not reflected in git history) and any other open items before assuming the DB matches what a migration or seed script would produce.

## Agents and multi-session handoff

Besides the default coding agent, two project-scoped advisory agents live in `.claude/agents/` — read-only (no Edit/Write), so they analyze and recommend rather than implement:

- **architect** — system-architecture-level thinking: module/app boundaries, data flow, deployment topology, coupling, design tradeoffs. Consult before a change that ripples across the system or touches a documented decision (see e.g. the computed-vs-stored discussion above).
- **ux** — usability/visual-design review of `tcg_inventory`'s Jinja2/HTMX templates and CSS: page flows, interaction consistency, accessibility, aesthetic polish.

Neither agent edits files — their output is analysis for the default agent (or the user) to act on. When a task needs input from both (e.g. a UX change with schema implications), the orchestrating session keeps each agent's spawned instance alive and relays findings between them rather than re-explaining context from scratch each time.

Since `ux` can't write files, whichever session consults it is responsible for logging findings worth keeping to `apps/tcg_inventory/UX_NOTES.md` (dated entries, same spirit as `HANDOFF.md` below) — otherwise the analysis only exists in that one chat transcript and is gone once it ends.

**Handoff log convention.** A session that changes something not fully captured by git — direct production-database edits, in-session decisions later reversed, deliberately deferred work — should append a dated entry to that app's `HANDOFF.md` (create one if the app doesn't have it yet). `apps/tcg_inventory/HANDOFF.md` is the working example: it separates "code shipped to prod" (in git, just a summary) from "direct database changes" (not in git anywhere else) and "open items raised but intentionally not built." Read an app's `HANDOFF.md` before trusting that its schema/data matches what the code and migrations alone would produce.
