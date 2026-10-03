# CLAUDE.md – fb_auction_watcher

App-specific guidance; the repo-root `CLAUDE.md` still applies. Read `docs/spec.md` before any non-trivial change — it is the source of truth for scope, domain, and rules.

## What this is

"FB Auction Watcher" (`fb-auction-watcher`), a Manifest V3 Chrome extension for one user (Erik Johansen). It reads a single Facebook buy/sell group for Pokémon cards and shows every sale in one table sorted by end time, with live countdowns and Lead/Outbid status on lots the user has bid on. Chrome on desktop only — no mobile, no server, no webapp.

It is independent of the other apps in `apps/`: no imports to or from them, no shared database.

**Status:** module 1 (spike) built — `src/content/post/` reads one open post and exports raw JSON. Modules 2–7 are not built; show a plan and wait for the go-ahead before starting one.

## Commands

From `apps/fb_auction_watcher/` (Node 22.12+):

```bash
npm ci              # install
npm run build       # → dist/ (load unpacked in chrome://extensions)
npm test            # Vitest + happy-dom, offline
npm run typecheck
```

CI runs all of these in the `fb_auction_watcher` job of `.github/workflows/tests.yml`. Build is two Vite passes into `dist/` (`vite.config.ts`): the content script as an IIFE (content scripts can't be ES modules), the service worker as an ES module.

The click allowlist lives in `src/content/post/patterns.ts` (`isExpanderLabel`) and the last-moment guard in `expand.ts` (`isSafeToClick`). Any change there needs matching allow/reject cases in `tests/patterns.test.ts` — the reject list (Reply/Svar, Like/Liker, See more, comment sort, text boxes) must stay.

## Non-negotiable rules

These override convenience. Don't relax any of them without the user's explicit say-so:

- **Read-only.** Never bid, claim, comment, react, or post. The only clicks allowed are "View more comments", "View N replies", and switching the feed sort order — Facebook shows these in the account's language (the user's is Norwegian: "Vis flere kommentarer", "Vis N svar"), so never match on English text alone. The highest bid is binding, so a stray click costs real money. The overlay's "Bid on Facebook" button only hides the overlay and scrolls to the reply field — it never focuses, types, or submits.
- **No headless or server-side scraping.** Everything runs in the user's own logged-in Chrome.
- **Slow pacing.** Feed scan every 10–15 min ±20 % jitter, pause while the machine is locked/idle (`chrome.idle`), never more than one tab talking to Facebook at a time, and a user-facing off switch.
- **Never use CSS class names as selectors** — Facebook obfuscates them. Use `role`, `aria-label`, DOM structure, and text patterns. Expect virtualized lists and SPA navigation (no full page loads).
- **Keep raw text.** Store captured raw text so interpretation can be re-run, and always show the seller's original text next to any interpreted value.

## Architecture (planned)

| Part | Folder | Responsibility |
|---|---|---|
| Feed content script | `src/content/feed/` | Scans the pinned group tab, captures posts as they render |
| Post content script | `src/content/post/` | Reads one post's comments/replies, renders the overlay (Shadow DOM) |
| Service worker | `src/background/` | Coordination, scheduling, Claude API calls, persistence |
| Store | `src/store/` | `Store` interface over IndexedDB (`idb`); swappable for Supabase later |
| LLM | `src/llm/` | Prompts, zod schemas, one retry on invalid JSON, token logging |
| Domain | `src/domain/` | Pure logic: highestBid, myStatus, end-time math, soft close |
| Pages | `src/pages/dashboard/`, `src/pages/sidepanel/` | `dashboard.html` table view, Chrome Side Panel |
| Shared | `src/shared/` | Types and messaging between contexts |
| Static | `public/` | `manifest.json`, icons |

Stack: TypeScript, Vite, Preact, idb, zod (added per module as needed; module 1 uses none of Preact/idb/zod). The Anthropic API key lives in `chrome.storage.local`, never in the repo.

## Working style

- **Show a plan before writing code for each module**, and wait for the go-ahead. One module at a time; the user tests between modules. Module order and scope: `docs/spec.md` "Plan" (in practice 0 → 1 → 3 → 2 → 4 → 5 → 6 → 7).
- Everything in English: code, identifiers, comments, commit messages, docs, and UI text. Norwegian only appears as input (Facebook's labels, sellers' post text), which parsing must handle.
- Design tokens (colors, fonts) and page layouts are specified in `docs/spec.md` "Design" — follow them, don't invent new ones.
- After each session: update `docs/spec.md` with what was learned (e.g. DOM findings, parsing edge cases) and commit.

## Repo conventions that apply here

- This is the repo's first non-Python app. Root `python -m pytest` and `ruff` don't cover it; use the commands above.
- `tests/fixtures/post-dialog.html` is synthetic (invented names, hand-written markup). Real Facebook markup may differ — calibrate against `samples/` and keep any committed fixture anonymized.
- `samples/` (saved Facebook posts — "Webpage, complete" + screenshots — at `apps/fb_auction_watcher/samples/`) is gitignored — it contains other people's names and comments. Never commit it, and test fixtures derived from it must be anonymized.
- Notes, handoff logs, and plans go in `notes/fb_auction_watcher/`, not in this folder (see root `CLAUDE.md`).
