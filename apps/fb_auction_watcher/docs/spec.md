# FB Auction Watcher – spec

- **Id:** `fb-auction-watcher`
- **Type:** Chrome extension (Manifest V3)
- **Owner/user:** Erik Johansen
- **Status:** spec only, no functionality built yet

The whole app is in English: code, commits, docs, and UI. Norwegian only appears where it is
input: Facebook's own UI labels and sellers' post text (the examples below are quoted as-is).
Amounts are NOK (kr); times are 24-hour, `Europe/Oslo`.

## Problem

I buy Pokémon cards in one Facebook group with up to ~100 auctions per day. Facebook sorts
by relevance / new activity / new posts – never by end time. It is impossible to keep track
of what ends when, and where I'm leading or have been outbid.

## v1 goals

- One table of every sale in the group, grouped and sorted by end time, with a live countdown.
- Lots I have bid on are highlighted (Leading / Outbid).
- Open an auction and see its lots and bids in a clean overlay instead of the comment thread.
- Chrome on PC/Mac only. No mobile, no server, no webapp.

## Domain

```
Post = listing: overview photos of the whole auction, rules, end time
 └ Top-level comment WITH an image = lot (single or bundle)
    └ Reply under the lot comment = bid (name, amount, time)
```

- Comments without an image are chatter. Replies from the seller are never bids.
- Sale types: auction, claim (first commenter gets to buy), fixed price.
- Close rules: hard close, or soft close (a bid near the end extends it, e.g. by 5 min).
- End time and rules are free text (e.g. "slutter søndag kl 20" – "ends Sunday at 8 pm")
  → interpreted in `Europe/Oslo`.
- My Facebook name: Erik Johansen (configurable).

## Non-negotiable rules

- **READ-ONLY.** The extension never bids, claims, comments, or likes. The only clicks
  allowed: "View more comments", "View N replies", and the feed sort order. (The highest
  bid is binding.) Facebook shows these labels in the account's language (Norwegian for me:
  "Vis flere kommentarer", "Vis N svar"), so matching must not assume English.
- No headless/server-side scraping. Everything runs in my own logged-in Chrome.
- Slow pacing: feed scan every 10–15 min ±20 %, pause while the PC is locked (`chrome.idle`),
  never parallel tabs against Facebook. Must be possible to turn off.
- The seller's original text is always shown next to interpreted values.
- Raw text is stored (capture table) so interpretation can be re-run.
- Never use CSS classes as selectors (Facebook obfuscates them). Use role, aria-label,
  structure, and text patterns. Facebook virtualizes lists and is an SPA.

## Architecture

- **Content scripts:** (a) feed scan in a pinned group tab, (b) post reader + overlay (Shadow DOM).
- **Service worker:** coordination, Claude API calls for interpretation, persistence.
- **Storage:** IndexedDB (`idb`) behind a `Store` interface, so Supabase can be swapped in later.
- **Extension pages:** table page (`dashboard.html`), Chrome Side Panel.
- **Tech:** TypeScript, Vite, Preact, idb, zod. API key in `chrome.storage.local`.

## Population

- **First-time backfill:** sort by "New posts" ("Nye innlegg"), scroll slowly, save each post as
  it renders, stop at posts 3 days old. After that, incremental: stop at the first known post.
- A less frequent pass sorted by "New activity" ("Ny aktivitet") catches older posts with new bids.
- **Detail reads (lots/bids)** only: when I open a post, and automatically every 15 min for
  auctions I have bid on. Everything else is not read until opened.

## Interpretation (LLM)

- **Feed call:** post text → `type`, `title`, `endsAt` (ISO), `endsAtText`, `closeRule`,
  `softCloseMinutes`, `closeRuleText`, `increment`, `price`, `shippingText`, `soldOrWithdrawn`.
- **Detail call:** post + comments with replies →
  `lots[{commentId, kind, title, cards[], startBid, bids[{replyId, bidderName, amount, valid, note}]}]`.
  Rules:
  - `"250kr"` / `"250,-"` / `"bud 250"` = 250, `"2.5k"` = 2500
  - the seller is never a bidder
  - `"200 sorry mente 250"` ("200 sorry, meant 250") = 250
  - a bid ≤ the current highest bid = invalid
- JSON only, validated with zod, one retry on failure. Cheap model. Log token usage.
- **Post-processing in code:** `highestBid`, `myStatus` (`none`/`lead`/`outbid`), times.

## Data model

**listing:** `id`, `fbPostUrl` (unique), `sellerName`, `type`, `title`, `postedAt`, `endsAt`,
`endsAtText`, `closeRule`, `softCloseMinutes`, `closeRuleText`, `increment`, `price`,
`shippingText`, `thumbnailUrl`, `commentCount`, `commentCountPrev`, `lotCount`,
`myLotStatus{lead,outbid}`, `lifecycle`, `detailFetchedAt`, `firstSeenAt`, `lastSeenAt`

**lot:** `id`, `listingId`, `position`, `fbCommentUrl`, `kind` (`single`|`bundle`), `title`,
`cards[{name, set, number, condition}]`, `imageUrl`, `startBid`, `highestBid`,
`highestBidder`, `bidCount`, `myStatus`, `myHighestBid`

**bid:** `id`, `lotId`, `bidderName`, `amount`, `bidAt`, `rawText`, `isMe`, `valid`, `note`

**capture:** `id`, `listingId`, `kind` (`feed`|`detail`), `rawText`, `capturedAt`,
`parseStatus`, `parseError`

**settings:** `groupUrl`, `myFbName`, `scanIntervalMin` (12), `backfillDays` (3),
`captureEnabled`, `apiKey`

**userState:** `lastDashboardVisitAt`, `seenListingIds`

## Statuses

Per listing:

| UI label | Meaning |
|---|---|
| New | First seen after my last visit to the table page |
| Activity | More comments than last time (`commentCount` > `commentCountPrev`) |
| Ending soon | Less than 1 hour left |
| Unknown end time | End time could not be interpreted |
| Ended? | End time passed, but the soft close window is not over |
| Ended | End time (and any soft close window) passed |
| Sold/withdrawn | Seller marked the sale as sold or withdrawn |

Per lot: **Leading** / **Outbid**. A listing shows a summary, e.g. "Leading 2 · outbid 1".

## Design

- **Colors:** blue `#2457D6` = Leading, orange `#C2570C` = Outbid, red `#B42318` = under 1 h.
- **Fonts:** IBM Plex Sans / IBM Plex Mono.
- **Language:** English UI.

### Table page (`dashboard.html`)

- Four numbers at the top: active, within 1 h, outbid, new.
- Groups: within 1 h · today · tomorrow and later · claim/fixed price · ended.
- Columns: Ends · Sale (title, seller, New, +N comments) · Type · Lots · Bids ·
  Your status · Updated.
- Filters: All / Auction / Claim / Fixed price / My bids / New + search.
- Rows I'm active in get a colored left border and can expand to "Your lots in this
  auction" (image, highest bid, my bid, status).
- Clicking the title opens the Facebook post.

### Side panel

- Toggle Capturing / Paused.
- Counters: within 1 h, outbid, new.
- Filters: All / My bids / Within 1 h.
- Compact list + link to the full table.

### Overlay on the post

- Header: seller, title, large countdown, close rule, the seller's rules verbatim +
  interpretation, shipping, "Re-read".
- Selector: "All lots" / "Mine only".
- "Your lots" first (outbid first, blue/orange border, my bid shown), then "Other lots".
- Selected lot: bid list, highest and next valid bid, a "Bid on Facebook"
  button that hides the overlay and scrolls to the reply field under the lot (never types).
- "Show Facebook page".

## Plan

One module at a time; I test between each.

| # | Module | Scope |
|---|---|---|
| 0 | Samples | 3–5 real auction posts saved in `samples/` (gitignored) |
| 1 | Spike | Content script that expands comments/replies and produces raw JSON for one post |
| 2 | Storage | `Store` interface on IndexedDB, with tests |
| 3 | Interpretation | Claude API + zod + post-processing, tested against samples |
| 4 | Feed scan | Backfill + incremental + idle pause |
| 5 | Table page | |
| 6 | Overlay | Incl. automatic re-read every 15 min of auctions I've bid on |
| 7 | Side panel | |
| 8 | Later | Pricing, notifications, Supabase, lot view |

Build order in practice: 0 → 1 → 3 → 2 → 4 → 5 → 6 → 7 (interpretation is tested against
the spike's JSON before storage is wired in).

## Risks

| Risk | Mitigation |
|---|---|
| Meta's terms forbid automated collection | The rules above: read-only, own Chrome, slow pacing, off switch |
| Facebook changes its HTML | `samples/` as a regression test; no CSS-class selectors |
| Sellers write differently | Always show original text next to interpretation; store raw text |
| Not real-time | Show "last read" on everything |

## Working style

- Show a plan before code for each module.
- Everything in English: code, commits, docs, and UI.
- After each session: update this spec with what we've learned, and commit.
