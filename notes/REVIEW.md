# PokemonCollector — professional repo review

**Date:** 2026-09-30 · **Reviewer:** `reviewer` agent (read-only, whole repo) · **Baseline:** `main` @ `61f4b53`

This is a point-in-time snapshot. Findings cite `path:line` as of that commit, so line numbers will drift as code changes. Findings worth building go through `/new_fix`.

The reviewer changed nothing. It read every Python module in both apps, the tests, the requirements files, `vercel.json`, `.gitignore`, both `.env.example` files, `.claude/settings.json`, the READMEs, HANDOFF and UX_NOTES, and the git history. It sampled the templates and JS for escaping and injection problems.

**Tests:** `python -m pytest` gives **520 passed, 0 failed, 1858 warnings, 41.7 s**. Almost all the warnings are `datetime.utcnow()` deprecations.

**Tools not run:** ruff, pyflakes and mypy aren't installed. A read-only AST scan found no unused imports and four functions over 120 lines.

---

## 1. Overall verdict

Compared with what a small professional team would ship, this is well above average in intent and documentation, and below average in guardrails.

The good parts are real:
- Business rules are explicit and tested.
- The importer runs as one atomic transaction.
- External lookups are careful to "never guess".
- There are 520 fast offline tests.
- The "why" is written down unusually well.

The biggest gap is the safety net around production. There are no backups and no restore path. Changes are made by hand against the live database. No CI runs the tests on `main`. Dependencies aren't pinned. Agents run with every permission check turned off. Money-affecting inputs are barely validated on the server.

The code mostly does the right thing when inputs are sane. Very little stops it, or you, when they aren't.

---

## 2. Top priorities (ranked by real-world impact)

### P1. No backups or restore path, while prod is edited by hand and by agents with no permission checks
**What:**
- No backup or restore procedure exists anywhere. `grep` for backup, pg_dump or PITR in `apps/tcg_inventory/README.md` and `HANDOFF.md` finds nothing.
- HANDOFF.md's 2026-09-14 entry records raw SQL against prod: renumbering, deletes, retyping. It says the pre-change snapshot "did **not** get committed anywhere durable… None of this is reversible."
- `.claude/settings.json:3` commits `"defaultMode": "bypassPermissions"` and also allows `Read(**/.env)`. `apps/tcg_inventory/.env` holds the prod `DATABASE_URL` (per CLAUDE.md).
- Migrations run on their own at app startup (`db.py:350-355`).

**Why it matters:** The Transactions table (what you paid) is the one dataset you can't rebuild from Dex. A bad SQL statement, a buggy migration, or an agent misunderstanding "clean up" can wipe it, and there's nothing to restore from. "Show the user the exact change first" (CLAUDE.md) is a convention the model tries to follow. It is not an enforced control, and bypassPermissions removes the one prompt that would have been one.

**How a professional would fix it:**
- Check what your Supabase plan actually gives you (daily backups vs. PITR, and whether you can download them).
- Add a scheduled `pg_dump` of at least `transactions`, `cards`, `collections`, `listings` and `releases` to storage you control. A monthly GitHub Action writing to a private artifact or Dropbox is enough.
- Do one restore drill against a local Postgres.
- Take a dump before every direct prod edit, and write the dump's location in HANDOFF.
- Remove `bypassPermissions` from the committed settings. Keep it in `settings.local.json` if you want it personally. At minimum, deny Bash commands that reference `DATABASE_URL`, `psql` or `execute_sql`.
- Consider a separate read-only Postgres role for agents.

**Effort:** small to medium.

### P2. No CI: tests never run automatically on PRs or on `main`
**What:**
- There is no `.github/` on main.
- A `pytest` workflow exists only in commit `9709d3b` ("Add pytest CI workflow for PRs and main"), on the unmerged branch `worktree-auto-issue-pipeline`.
- `gh run list` shows only two Copilot runs from July. No test runs at all.
- Meanwhile `project-manager` and `developer` are allowed to merge PRs they judge "checks green", and there are no checks.

**Why it matters:** CI (continuous integration) means a machine runs the tests on every PR and blocks merging if they fail. Without it, "the tests pass" depends on each agent remembering to run them locally, in whatever venv it has. HANDOFF already records a test failing only because of one developer's local `.env`.

**How a professional would fix it:**
- Merge a minimal GitHub Actions workflow: checkout, set up Python at the same version Vercel uses, `pip install -r requirements-dev.txt`, `python -m pytest`.
- Turn on branch protection for `main` so the check has to pass before merging.
- Later, add `ruff check` to the same job.

**Effort:** small.

### P3. A "full load" deletes cards and, through cascades, their purchase history and value history
**What:**
- `importer.py:302-306` does `db.delete(card)` for every card missing from the My Collection export when `full_load=True`.
- `models.py:145-147` sets `cascade="all, delete-orphan"` on `Card.transactions`, so the ORM deletes every purchase, sale and trade row for that card.
- `models.py:217-219` has `ondelete="CASCADE"` on `card_snapshots`. On Postgres this also erases the card's value history, which rewrites past points on the "Real value history" chart.
- The route still accepts `full_load` (`app.py:2392,2410`), and `templates/partials/dropbox_files.html:36` still has the checkbox. The page UI was removed, but the endpoint and partial remain.
- There's no sanity check. A truncated or empty-bodied My Collection file with only a header row would delete the whole collection's financial history.
- The natural key `(card_id, variant)` is free text from Dex. If Dex renames a variant ("Pokeball Holo" to "Poké Ball Holo"), the old row is flagged and a new cost-less card appears. A later full load then deletes the old row and the money attached to it.

**Why it matters:** Cards can be re-synced from Dex. Transactions can't. A delete that silently cascades into money records is the classic way small apps lose data.

**How a professional would fix it:**
- Never hard-delete a card that has transactions or listings. Refuse, or soft-delete it (for example, keep `flagged_missing_since`). Change the cascade to `RESTRICT` for transactions.
- Add a circuit breaker: abort the import if it would delete or flag more than about 5% of cards, or if My Collection has zero rows.
- Consider deleting the unused `full_load` route and the partial entirely.

**Effort:** small to medium.

### P4. Cron endpoints fail open, accept the secret in the URL, and repeat the auth code four times
**What:**
- All four `/cron/*` paths skip login (`app.py:165`).
- Each route does `authorized = not cron_secret or …` (`app.py:2447`, `2517`, `2573`, `2606`). If `CRON_SECRET` is missing or empty in Vercel, anyone on the internet can trigger a Dropbox sync, a price refresh, or 300-card image backfills.
- `.env.example` labels `CRON_SECRET` "Optional… Leave empty to run that endpoint unauthenticated". `tests/test_dropbox_routes.py:149` asserts that behaviour.
- `?secret=` puts the secret in query strings, which end up in Vercel request logs and browser history.
- The comparison is a plain `==`, not constant-time.
- The four copies have already drifted: two tell scheduled runs from manual ones, two don't.

**Why it matters:** "Fail open" means that when configuration is missing, the default is to allow access. For security settings the default should be to deny. This is a documented decision, and the reviewer argues against it: the only thing failing closed costs you is having to set one env var once.

**How a professional would fix it:**
- Write one helper or FastAPI dependency, `require_cron_secret(request)`.
- It returns 503 or 401 when no secret is configured, unless you're in local dev (auth not configured).
- It compares with `hmac.compare_digest`.
- Drop `?secret=`, or accept it only in local dev. Issue #199's in-app "Run now" button (behind login) is the right replacement for manual triggers.

**Effort:** small.

### P5. Dependencies aren't pinned and there's no lockfile; this has already broken prod once
**What:**
- Every line in `apps/*/requirements.txt` is `>=` only.
- No Python version is pinned for Vercel: no `.python-version`, and no runtime in `vercel.json`. The local venv is Python 3.14.
- `db.py:42-55` documents the result: SQLAlchemy 2.1 changed its default Postgres driver, and "a fresh Vercel build then crashed" (PR #201, a prod 500).
- The local venv has `anthropic 1.5.0` while `finn_ad_scraper/requirements.txt` requires `>=1.8`. It isn't even what the file says, and the tests hide this because they use fakes.
- `dropbox_setup.py:13` imports `requests`, which isn't in tcg_inventory's requirements. It only works because the dropbox package pulls it in.

**Why it matters:** Every Vercel deploy installs whatever the newest versions are that day. A reproducible build means the same code always produces the same installed environment.

**How a professional would fix it:**
- Keep the `>=` files as inputs.
- Generate pinned lockfiles with `pip-compile` or `uv pip compile`, commit them, and have Vercel and CI install from them.
- Pin the Python version.
- Bump versions on purpose, with CI green (P2).

**Effort:** small.

### P6. Server-side validation of money and type inputs is almost absent
**What:**
- **NaN and infinity are accepted as prices.** Pydantic parses `"nan"`/`"inf"` for `float` form fields (`TypeAdapter(float).validate_python('nan')` returns `nan`). The mark-sold check `if p <= 0` at `app.py:1382` lets NaN through, because `nan <= 0` is `False`. Other routes (`app.py:1914`, `2129`, `2196`, `2280`) don't check at all. One NaN makes Net invested, Gain and every bucket sum show `nan kr`.
- **`type` is stored unchecked.** See `create_purchase` (`app.py:1932`), `update_purchase` (`2164`), `create_transaction` (`2236`) and `update_transaction` (`2301`). Money queries only count `purchase`/`sale` (`queries.py:1109-1114`), so a typo'd type silently drops out of every figure.
- **Bad input causes 500s.** `dt.date.fromisoformat(date)` is unguarded at `app.py:1927`, `2166`, `2230`, `2237` and `2300`. `update_purchase` indexes parallel lists (`app.py:2163-2170`) and throws IndexError if they're misaligned. htmx silently ignores error responses, so the user sees nothing happen. That's the same "silent no-op" class UX_NOTES keeps finding.
- **`update_purchase` deletes any transaction id you post** (`app.py:2155-2159`), without checking it belongs to that order.
- **Order IDs can collide.** `purchase_cart_start` computes `max+1` when the cart opens (`app.py:1806`, `1775-1780`), and `create_purchase` trusts the posted `purchase_id` (`app.py:1910`). Open a cart, mark a listing sold in another tab (which takes `max+1`, `app.py:1391`), then save the cart: your purchase gets merged into the sale's order.

**Why it matters:** The server is the last line of defence for the numbers the whole app reports. Browser-side checks can be skipped, and htmx hides failures.

**How a professional would fix it:**
- Validate at the boundary with Pydantic or FastAPI form models: `type: Literal["purchase","sale","trade","ripped"]`, finite non-negative prices (`math.isfinite`), `date` typed as `dt.date`, equal-length lists.
- Return a 422 with a readable message, rendered through `HX-Retarget` or an error partial.
- Assign `purchase_id` on the server at save time, not when the cart opens.
- In `update_purchase`, filter deletes and edits by `Transaction.purchase_id == purchase_id`.

**Effort:** medium.

### P7. FX fallback silently writes prices at the rate #209 fixed as wrong
**What:**
- `fx_rates.py:48` keeps `USD: 10.5` as the last-resort rate. The comments call it "~10% above the real rate", and #209 exists to remove it.
- On a cold start while Norges Bank is unreachable, `get_rates()` returns `"fallback"` (`fx_rates.py:170-175`).
- `card_images.py:127` and `price_refresh.py:161-170` still store every price at that rate, marked fresh for 7 days. The source is only printed in the cron JSON and log.

**Why it matters:** This brings back exactly the inflated-value bug you just paid to fix with `--reprice-all`. Nobody reads the cron response.

**How a professional would fix it:** When the source is `"fallback"`, skip the price-write phase. Or at least don't advance `tcgplayer_price_updated_at`, so the next run retries. Make the run's status "degraded" rather than "ok". Optionally persist the last successful rate in the DB so "last-known" survives cold starts.

**Effort:** small.

### P8. Tests only run on SQLite, but prod runs Postgres
**What:**
- `tests/conftest.py:99-130` builds every test DB on SQLite.
- `db.py:191-192` returns early unless Postgres, so `_widen_card_snapshot_source_constraint` has never been executed by a test.
- The history shows dialect bugs reaching prod or review: `NULLS LAST ASC` (#176, see the comment at `app.py:723-728`) and the psycopg driver switch (#201).
- SQLite also doesn't enforce FK cascades by default. So the P3 cascade behaves differently in tests (orphaned snapshots) than in prod (deleted snapshots).

**Why it matters:** A green suite tells you the SQLite behaviour is right. The money is in Postgres.

**How a professional would fix it:** In CI (P2), add a Postgres service container and parametrize the `client`/`db_session` fixtures to also run against it. Even running only the migration, importer and query tests there would catch this whole class of bug.

**Effort:** medium.

### P9. `app.py` is a 2,786-line module with 57 routes
**What:**
- `app.py` is touched by 106 commits, by far the hottest file.
- `dashboard` is 141 lines, `inventory` 151, `_transactions_context` 133. `importer.import_dex_csv_files` is 220.
- `db = get_db_session(); try: … finally: db.close()` is repeated 50 times, while `db.get_db()` (`db.py:125-130`), the standard FastAPI dependency for this, exists and is never used.
- The same card search is copy-pasted four times (`app.py:1172-1180`, `1819-1827`, `2046-2053`, `2083-2090`).
- The cron auth is copy-pasted four times (see P4).

**Why it matters:** Every feature edits the same file, so merges conflict, reviews are hard, and duplicated logic drifts. The cron copies already have.

**How a professional would fix it:**
- Split into `APIRouter` modules (`routes/dashboard.py`, `inventory.py`, `transactions.py`, `listings.py`, `cron.py`, `auth.py`), included from `app.py`. This works fine with flat imports.
- Use `db: Session = Depends(get_db)`.
- Pull out shared helpers such as `search_cards(db, q)`.
- Do it incrementally, one router per PR, with CI green.

**Effort:** medium to large, done incrementally.

### P10. Auth accepts any user in the Supabase project
**What:** `auth.py:102-116` only checks the signature and `aud == "authenticated"`. No email or `sub` allowlist. Whether this matters depends on your Supabase setting "Allow new users to sign up", which is ON by default. If it's on, anyone who obtains your project URL and anon key can sign up and gets full read/write access. By design the anon key is meant to be public, so don't count on it staying secret.

**Also, lower priority:**
- The access token cookie lasts only `expires_in` (1 h, `app.py:2771`) and is never refreshed, so you're logged out hourly.
- `/logout` is a GET (`app.py:2776`).
- `auth.py:70` calls `response.json()` on a possibly non-JSON error body, which would give a 500.

**How a professional would fix it:** Disable signups in Supabase (this can't be verified from the repo). Add `ALLOWED_USER_EMAILS` or `ALLOWED_USER_IDS` and check the claim in `verify_access_token`. Optionally store the refresh token and refresh silently.

**Effort:** small.

---

## 3. Other findings by area

### Correctness
- [medium] **An old export can overwrite a newer one.** If the Dropbox folder ever holds two exports of the same Dex category, the oldest one wins. `dropbox_client.py:82` sorts files newest first, `importer.py:186-191` appends rows in that order, and `importer.py:232-256` overwrites card fields row by row, so the last row (from the oldest file) sticks. The README says to "keep the folder holding current exports", but the code doesn't enforce it. Fix: per category, use only the newest file, and warn about duplicates.
- [medium] **The price cron has no time budget.** Up to 100 cards × 2 attempts × 5 s timeout (`price_refresh.py:42`, `card_images.py:21,191`) could exceed Vercel's function limit, and `vercel.json` sets no `maxDuration`. The day's snapshot is written after the refresh (`app.py:2527`), so a timeout means no snapshot for that day. Images are time-boxed; prices should be too, and the snapshot should run first or in its own try block.
- [medium] **Up to 50 HTTP lookups run inside the importer's open DB transaction** (`importer.py:262-294`, commit at `375`). That makes transactions long, and a timeout loses the whole sync. Better: import first, commit, then do lookups (price_refresh/backfill already do this).
- [low] **Money is stored as `Float`** (`models.py:89,97,255,268,272`). Floats can't represent most decimal fractions exactly (0.1 + 0.2 ≠ 0.3), so sums pick up tiny errors. Here values are shown rounded to whole kr (`app.py:91-92`), so the practical risk is small. Use `Numeric(12,2)` and `Decimal` if you ever do exact reconciliation.
- [low] **"Today" is server time,** which is UTC on Vercel: `dt.date.today()` at `app.py:1724` etc. Between midnight and 02:00 Oslo time, the default date is yesterday. Also, naive `utcnow()` is used everywhere; that's where the 1858 warnings come from.
- [low] `_like_pattern` (`app.py:595-596`) doesn't escape `%`/`_` in searches. Cosmetic.
- [low] `init_db()` can run concurrently on two cold starts. The chain is idempotent, but `_set_schema_version` could race. Acceptable at this scale.

### Security
- [low, verified fine] **SQL:** the ORM uses parameterized queries everywhere. The only f-string SQL (`db.py:152`) uses model metadata, not user input.
- [low, verified fine] **XSS:** Jinja autoescape is on, `|safe` is used only on template-authored strings (`macros.html:81,135`, `inventory_table.html:70`), and JS handler arguments are index-based (`dashboard.html:149-392`).
- [low] **CSRF:** there are no CSRF tokens. The `samesite="lax"` cookie (`app.py:2770`) blocks cross-site form POSTs in modern browsers, which is adequate for a one-user app.
- [low] **finn_ad_scraper doesn't check that the URL is finn.no** (`finn_ad.py:45-61`). The Playwright fallback (`:81`) would open `file://` or internal URLs. It's a local CLI, so the risk is low; validate the host anyway.
- [low] **Secrets in git history:** checked `sk-ant-`, JWT prefixes, `.env` and `.db` files. Only a fake test key turned up (`f0eaf78`, test_auth.py). Clean.

### Data safety
- [high] See P1 and P3.
- [medium] **`init_db()` fast-path gate:** a manual prod schema edit "must also bump schema_meta" (`db.py:345-349`). That's easy to forget, and it's the kind of state a migration tool (Alembic) tracks for you. Worth moving to Alembic once schema changes stop being purely additive. #210 (the `card_prices` table) is a natural point.
- [low] `db.py:28-31` creates `tcg_inventory.db` as a side effect of *importing* the module, so tests and scripts create it too.

### Testing
- [medium] P8 (SQLite only).
- [medium] **Some risky paths have no tests:** full-load cascading into transactions and snapshots (only "card count == 1" is asserted, `test_importer.py:562-570`); NaN or invalid prices and types; the order-ID collision; a duplicate category across files; the FX fallback path writing prices.
- [low] **Auth tests:** only `/` is checked for "requires login" (`test_auth_routes.py:21`). The middleware design makes coverage structural, but one parametrized test over `app.routes` would catch a future `_PUBLIC_PATHS` slip.
- [low] **Fail-open is locked in by a test:** `tests/test_dropbox_routes.py:149` asserts that the cron works with no secret configured.
- [low] **finn_ad_scraper tests import by accident:** they only work through pytest's implicit rootdir insertion of `apps/`. There's no `pyproject.toml` or `pytest.ini`. Add a minimal `[tool.pytest.ini_options]` with `testpaths` and `pythonpath`.
- [low] 1858 deprecation warnings drown out any real warning. Fix `utcnow()`, or filter the warning.

### Maintainability
- [medium] P9.
- [medium] **Comments narrate history instead of intent.** Docstrings are extremely long and full of issue numbers and HANDOFF references. `models.Listing` is 50 lines (`models.py:302-352`), and the comment block at `app.py:1641-1668` is similar. History belongs in commits and PRs; comments should say what the code does and why, briefly. Otherwise they go stale and make modules hard to scan.
- [low] **Leftover Norwegian** despite the English UI:
  - `queries.py:285,289,351` ("(uten serie)", "(uten rarity)")
  - period labels "1U", "1Å", "Alt" (`queries.py:544-549`)
  - importer warnings (`importer.py:182,189,219,335`)
- [low] **Dead-ish code:** `seed_set_release_order.py` writes to `set_release_order`, which `models.py:493-499` says nothing should write to any more.
- [low] Type hints are partial, e.g. `run_backfill(db, limit=…)` and several `db` params without a type.

### Error handling and observability
- [medium] **Nobody finds out when crons fail.** Logging is all `print` (`app.py:2471,2495,2532-2541`, `db.py:333`), with no `logging`, no global exception handler, and no alerting. Price refresh and set sync return `"status": "ok"` or HTTP 200 even when every lookup failed. ImportLog stores only `warnings_count` (`models.py:539`); UX note #3 is still open. Fix: use `logging`, return non-200 on a degraded run, persist run summaries for the price and set crons like ImportLog, and add a free dead-man's-switch (e.g. healthchecks.io) or Sentry.

### Dependencies
- [high] P5.
- [low] tcg_inventory ships both `httpx` and (via anthropic) `httpx2`; fine. Vendored `htmx.min.js`/`chart.umd.js` have no recorded version number, so upgrades are guesswork. Note the version in a comment or filename.

### Project hygiene and practice
- [high] P2 (no CI), plus the bypassPermissions setting in P1.
- [medium] **No linter or formatter config.** Add ruff (lint + format) in `pyproject.toml`, and optionally a pre-commit hook.
- [low] **Docs are very long:** README 968 lines, HANDOFF 1358 lines. HANDOFF is valuable but should be trimmed to what's still true, with resolved items archived.
- [low] **finn_ad_scraper README** (`apps/finn_ad_scraper/README.md`) doesn't say to run the CLI from `apps/`.
- [low] **Unverified Claude API usage:** `card_identifier.py:21,28,130-138` uses model `claude-opus-5`, a server-side-fallback beta, `output_config` and `fallbacks=`. These weren't checked against current API docs, and the local anthropic 1.5.0 is below the required 1.8.
- [low] Git history is healthy: small PRs, descriptive messages, issue links. 76 of 342 commits are fixes, concentrated in `app.py`, `dashboard.html` and `transactions.html`, which matches P9.

### Architecture
- [low] **The copied masterdata rules** (`finn_ad_scraper/card_ids.py` vs `tcg_inventory/masterdata.py`) are documented and reasonable at this size. The weakness is that the "keep in sync" rule isn't enforced: `test_card_ids.py:25` hardcodes expectations rather than comparing with masterdata. Cheapest guard: one root-level test that imports both and asserts the vocabularies are equal. Same for `CARD_CONDITIONS` vs `CONDITIONS`.
- [low] **What will hurt first:**
  - P9's single module.
  - Per-request full-table loads (`queries.all_cards_with_collections`, `db.query(Transaction).all()` repeated across routes). Fine at about 1,000 cards; they'll show up as cold-start latency long before correctness issues.
  - Snapshot growth (#169).

---

## 4. What's already done well
- **Business rules as code plus tests:** category routing, primary-collection priority, and "a category absent from the sync is untouched" (`importer.py:321-370`, `test_importer.py:573`).
- **Normal syncs are safe.** They never delete, only flag (`importer.py:307-309`), and the whole import commits once, atomically (`importer.py:375`).
- **Computed-never-stored values** (`models.py:153-181`) keep derived numbers from drifting. It's a good principle, applied consistently.
- **External data is treated sceptically:** confidence checks before trusting a price (`card_images.py:142-163`), a "wrong image is worse than none" policy, retry and backoff windows, and the FX source reported.
- **Idempotent, additive migrations with a version gate** (`db.py`), plus the bulk backfill rewritten after a real timeout (`masterdata.py:196-254`). That's learning from incidents.
- **The test suite:** 520 tests, fully offline, 42 s, with network stubbed at the `httpx` boundary so the real parsing still runs (`conftest.py:57-73`), and throwaway DBs per test.
- **Secure defaults where you thought about them:** HttpOnly, Secure (on Vercel) and SameSite cookies, JWKS verification, autoescaping, an ORM everywhere, and no secrets in git history.
- **finn_ad_scraper is clean and small.** It has structured outputs with enum-constrained schemas, an injectable client for tests, and a clear fetch → parse → identify split.
- **Writing down why, and HANDOFF discipline.** It's rare, and it's why this review could tell deliberate decisions from accidents.

---

## 5. Suggested learning path
1. **Backups, restores and migrations:** `pg_dump`/`pg_restore`, what your Supabase plan provides (daily backups vs. PITR), and Alembic for versioned schema migrations.
2. **CI with GitHub Actions and reproducible builds:** a pytest workflow with a Postgres service container, branch protection, and lockfiles via `pip-tools` or `uv`.
3. **Validating input at the boundary in FastAPI:** Pydantic models for form data, `Literal` types, `math.isfinite`, returning 422s that htmx can show, and `Decimal`/`Numeric` for money.
4. **Structuring a FastAPI app:** `APIRouter`, `Depends()` for DB sessions and auth (including the cron secret), and a thin route layer over service functions.
5. **Observability for unattended jobs:** Python's `logging` module, fail-closed configuration, returning honest status codes, and a cron heartbeat or alerting service.

---

**Tickets worth filing** (via `/new_fix` or `project-manager`): P1, P2, P3, P4, P5 and P7 are each small, self-contained tickets. P6 and P8 are medium. P9 is an epic to do incrementally.
