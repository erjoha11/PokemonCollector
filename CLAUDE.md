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

- `db.py` — engine/session setup. SQLite locally by default; set `DATABASE_URL` for Postgres (Supabase). On a read-only host (e.g. Vercel) with no `DATABASE_URL`, it fails fast with an actionable error rather than a cryptic write failure later. `init_db()` only ever creates missing tables/columns (`CREATE TABLE IF NOT EXISTS` + an additive `ALTER TABLE ADD COLUMN` pass) — it never drops/renames/retypes, so a real schema change needs a deliberate migration path, not just editing a model. That chain is gated by `CURRENT_SCHEMA_VERSION` in `db.py`: **adding a column to a model also needs that constant bumped**, or an already-migrated database (prod) never gets the column.
- `auth.py` — Supabase Auth (GoTrue) over plain HTTP, no supabase-js. Verifies the session JWT against Supabase's JWKS endpoint first, falling back to `SUPABASE_JWT_SECRET` only for legacy HS256 projects. Auth is skipped entirely when `SUPABASE_URL`/`SUPABASE_ANON_KEY` aren't set, which is what keeps local `python app.py` login-free.
- `models.py` — SQLAlchemy models. `duplicates`, `total_value`, `unique_value` are **never stored**, always computed (`Card.duplicates` etc., and the dashboard aggregates in `queries.py`) — a stored, independently-maintained `duplicates` drifting from `qty` was a real bug in the Excel system this replaces.
- `constants.py` + `importer.py` — encode the business rules migrated from the Excel system (Dex category routing to collection/binder, primary-collection priority for dashboard credit, sync-vs-full-load semantics). These rules are documented in detail in `apps/tcg_inventory/README.md` — **do not change the routing/priority logic without updating both the code and that doc.**
- `queries.py` — dashboard aggregation queries.
- `masterdata.py` — `master_cards` (one canonical identity per printed card + variant, keyed `(language, set_code, number, variant)` parsed from Dex's `card_id` + a normalized variant code) and `master_card_ids` (external IDs per source — dex, pokemontcg, later tcgplayer/collectr/...). `Card.master_card_id` links a physical card to it; linked at import and backfilled by `init_db()`. Identity only, never touches qty/collections/binders. See `apps/tcg_inventory/README.md` "Masterdata".
- `snapshots.py` — writes one `card_snapshots` row per card (qty + `reference_price`) each time the daily cron completes a sync, so `queries.real_value_history` can report actual historical value instead of `collection_value_growth`'s today's-price-applied-retroactively approximation. Rendered as its own "Real value history" chart in Transactions' "View charts" section. See `apps/tcg_inventory/README.md` "Value history".
- `dropbox_client.py` / `dropbox_setup.py` — read-only Dropbox integration for pulling Dex CSV exports directly, optional.
- `api/index.py` + `vercel.json` — Vercel entrypoint; `api/index.py` just re-exports `app` from `app.py`, all routes live in the one place. `vercel.json` also schedules the daily Dropbox auto-sync cron (`GET /cron/dropbox-sync`), which only ever does a normal sync (flags missing cards, never deletes).
- `templates/` + `static/` — Jinja2/HTMX frontend, no build step, no CDN dependency (HTMX is vendored).

Data model: `cards`, `collections`, `card_collections` (many-to-many), `binders`, `transactions`, `master_cards`/`master_card_ids` (masterdata), `set_release_order` (chronological-sort lookup table, ships empty until seeded).

Tests use an in-memory or temp-file SQLite database (`tests/conftest.py`'s `db_session`/`client` fixtures) — the real `tcg_inventory.db` is never touched. The `client` fixture monkeypatches `db.engine`/`db.SessionLocal` and reloads `app` so routes bind to the throwaway DB.

An already-registered order can be edited (retype/relink/move/merge/split/add note) via `/transactions/purchase/{id}/edit` — see `apps/tcg_inventory/README.md`'s Transactions section. See `apps/tcg_inventory/HANDOFF.md` for the most recent session's direct production-database changes (not reflected in git history) and any other open items before assuming the DB matches what a migration or seed script would produce.

## Agents and multi-session handoff

Besides the default coding agent, project-scoped agents live in `.claude/agents/`. Claude Code supports subagents spawning further subagents (up to 3 layers deep by default, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` in `.claude/settings.json` to change it), and this repo's agents deliberately use that: it's not just the user driving every step.

- **architect** — the entry point for developing a feature or a bigger/new idea, as well as standalone system-architecture-level thinking: module/app boundaries, data flow, deployment topology, coupling, design tradeoffs. It spawns `ux` itself when an idea touches `tcg_inventory`'s UI, and files the resulting ticket on GitHub itself (`gh issue create` — its only GitHub access). From there it either hands the ticket to `project-manager` or reports it back to the user, whichever fits. Consult before a change that ripples across the system, touches a documented decision (see e.g. the computed-vs-stored discussion above), or when an idea needs to be thought through before anyone writes code. Invoked directly, or via `/new_feature`.
- **ux** — usability, functional, and visual-design review of `tcg_inventory`'s Jinja2/HTMX templates and CSS (scoped exclusively to `tcg_inventory`, not `finn_ad_scraper`): page flows, interaction consistency, functional correctness (broken/silent-no-op interactions, state loss, mismatched data), accessibility, aesthetic polish. Spawnable by anyone — the user, `architect`, `project-manager`, or `developer` — though `architect` spawning it during feature intake is the standard path. Can read and comment on issues (`gh issue view`/`gh issue comment`) but never create, edit, or close.
- **project-manager** — the project-management assistant: maintains an overview of everything in flight across both apps (open issues, open/draft PRs, CI status, stale branches), triages and prioritizes the backlog, turns ideas or bug reports into tracked GitHub issues, and is the **only** agent allowed to spawn `developer` to actually build tracked work. Has full issue admin (create/edit/label/close/delete) and can merge (a PR it judges genuinely ready — checks green, no unresolved review comments, not a draft) or close PRs — still short of `developer`'s force-push/branch-delete/repo-settings access. Consult for a status/standup-style read of the project (`/pm_report`), for backlog triage, for merging a ready PR, for a bug/small change that should be tracked and built (`/new_fix`), or when a feature idea needs shaping into a concrete plan before `developer` builds it.
- **developer** — full Edit/Write and full GitHub read/write (including merge/close/force-push/delete). Spawnable directly by the user for a quick, untracked fix, or by `project-manager` to build a ticket. Has no `Agent` tool itself — it never spawns `ux`, `architect`, `project-manager`, or another `developer`; if it decides mid-task that it needs one of those, it says so in its report to whoever spawned it instead. When `project-manager` spawned it, it reports back to `project-manager`, not the user.

`architect`, `ux`, and `project-manager` have no Edit/Write tools — they never implement application code themselves, only `developer` does. `developer` and `project-manager` are the two agents in this repo able to merge/close PRs. When a task needs input from more than one agent, the orchestrating session (or an agent that spawned another, per the roles above) keeps each spawned instance alive and relays findings between them rather than re-explaining context from scratch each time.

Since `ux` can't write files, whichever session consults it is responsible for logging findings worth keeping to `apps/tcg_inventory/UX_NOTES.md` (dated entries, same spirit as `HANDOFF.md` below) — otherwise the analysis only exists in that one chat transcript and is gone once it ends.

**Git housekeeping goes to `developer`.** When the user asks to merge branches, clean up branches, or similar repo housekeeping, the orchestrating session spawns `developer` to do it end to end — including the cleanup afterwards (deleting branches that are fully merged, e.g. `worktree-*` branches whose PR was squash-merged) — rather than doing it inline and stopping to ask about each stale branch. `.claude/settings.json` allows `git push origin --delete *` for this.

**Slash commands** (`.claude/commands/`) front the common entry points: `/new_feature <idea>` spawns `architect`; `/new_fix <bug or change>` spawns `project-manager`, which tracks it and spawns `developer`; `/pm_report` spawns `project-manager` for a status overview (recent work, active issues, branch/repo sync).

**Handoff log convention.** A session that changes something not fully captured by git — direct production-database edits, in-session decisions later reversed, deliberately deferred work — should append a dated entry to that app's `HANDOFF.md` (create one if the app doesn't have it yet). `apps/tcg_inventory/HANDOFF.md` is the working example: it separates "code shipped to prod" (in git, just a summary) from "direct database changes" (not in git anywhere else) and "open items raised but intentionally not built." Read an app's `HANDOFF.md` before trusting that its schema/data matches what the code and migrations alone would produce.
