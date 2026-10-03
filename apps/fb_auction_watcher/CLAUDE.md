# CLAUDE.md – fb_auction_watcher

App-specific guidance; the repo-root `CLAUDE.md` still applies. Read `docs/spec.md` before any non-trivial change — it is the source of truth for scope, domain, and rules.

## What this is

"FB Auction Watcher" (`fb-auction-watcher`), a Manifest V3 Chrome extension for one user (Erik Johansen). It reads a single Facebook buy/sell group for Pokémon cards and shows every sale in one table sorted by end time, with live countdowns and Lead/Outbid status on lots the user has bid on. Chrome on desktop only — no mobile, no server, no webapp.

It is independent of the other apps in `apps/`: no imports to or from them, no shared database.

**Status:** scaffold only. No functionality is built yet — don't add any until the user asks.

## Non-negotiable rules

These override convenience. Don't relax any of them without the user's explicit say-so:

- **Read-only.** Never bid, claim, comment, react, or post. The only clicks allowed are "Vis flere kommentarer" / "View more comments", "Vis N svar" / "View N replies", and switching the feed sort order. The highest bid is binding, so a stray click costs real money.
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

Stack: TypeScript, Vite, Preact, idb, zod. The Anthropic API key lives in `chrome.storage.local`, never in the repo.

## Repo conventions that apply here

- This is the repo's first non-Python app. Root `python -m pytest` and `ruff` don't cover it; its own build/test commands go here once `package.json` exists.
- `samples/` (captured Facebook HTML/text) is gitignored — it contains other people's names and comments. Never commit it, and test fixtures derived from it must be anonymized.
- Notes, handoff logs, and plans go in `notes/fb_auction_watcher/`, not in this folder (see root `CLAUDE.md`).
