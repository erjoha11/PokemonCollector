# FB Auction Watcher – spec

- **Id:** `fb-auction-watcher`
- **Type:** Chrome extension (Manifest V3)
- **Owner/user:** Erik Johansen
- **Status:** module 1 (spike) built and run on real posts; overview built: feed scan by hand
  and automatic (pinned tab, background), rule-based listing and bid interpretation, Claude
  Code (`claude -p`) through a native-messaging bridge for what the rules can't read, IndexedDB
  store of raw posts and post reads, table page with your Leading/Outbid status. See
  `notes/fb_auction_watcher/overview-plan.md`. Automatic re-reads of auctions you're in
  (every 15 min, plus one after the end) are built. Not built: overlay, side panel.

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
- A top-level comment of just "." is someone tagging themselves to follow the sale (they get
  notified of new activity). Not a lot, not a bid. The count of them is a rough "watchers"
  number. Claim sales ask for this explicitly ("Tagg deg selv i kommentarfeltet").
- Sale types: auction, claim (first commenter gets to buy), fixed price.
- Close rules: hard close, or soft close (a bid near the end extends it, e.g. by 5 min).
- End time and rules are free text (e.g. "slutter søndag kl 20" – "ends Sunday at 8 pm")
  → interpreted in `Europe/Oslo`.
- My Facebook name: Erik Johansen (configurable).

## Non-negotiable rules

- **READ-ONLY.** The extension never bids, claims, comments, or likes. The only clicks
  allowed: "View more comments", "View N replies", "See more" (cut-off text), switching a
  post's comment sort to "All comments", and the feed sort order. (The highest bid is
  binding.) Facebook shows these labels in the account's language (Norwegian for me:
  "Vis flere kommentarer", "Vis N svar", "Se mer", "Alle kommentarer"), so matching must not
  assume English.
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

**Decided 2026-10-03:** no Claude API and no downloaded models. Rules in code
(`src/domain/`) interpret everything they can; only what they mark unsure goes to Claude Code
on the user's Mac (`claude -p`, their own login) through Chrome native messaging
(`native/fbaw_claude_host.py`), batched, Haiku, no tools, answers cached. The original design
below is kept for reference.

**Limits and failures** (2026-10-03, review M2; `src/background/claudeQueue.ts`):
- Only live sales: nothing from a sale that ended more than 6 h ago (late bids and claims are
  read after the end, and Won/Lost needs a read after it, so a few hours' grace), and for sales
  with no known end (fixed price, or an end nobody could read) nothing once the post hasn't been
  seen or read for 3 days. The end is the one the overview shows: rules, else Claude's answer.
- Photos sent to Sonnet are capped at 20 per rolling hour, counted in `chrome.storage.local` so
  a worker restart doesn't reset it: one per claim lot call (at most 5 per run), and each photo of
  a lot-name batch (up to 12 per call, at most 24 per run; claim lots go first, lot names get what
  the cap leaves). A lot-name photo that failed is retried in a call of its own, so one expired
  photo URL can't keep failing the batch it was in. Lot-name answers are kept by retention for as
  long as the post read that shows them.
- A failure that hits everything (bridge not installed, `claude` missing or not logged in, out of
  quota) stops the run and pauses Claude for 10 min. Any other failure (a lot photo the CDN no
  longer serves: signed URLs expire, `oe=`, HTTP 403/404; a timeout; no JSON) counts against
  that item only: retried after 15 min, then 1 h, skipped after 3 tries, and the run goes on.
  Failure records are kept in the store's meta (`claudeFailures`) for 7 days. A claim lot's key
  includes its replies, so a new reply makes it a new question with fresh tries.

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

**settings** (as built, `src/shared/settings.ts`): `autoScan` (the off switch, default off),
`myName` (default "Erik Johansen"), `useClaude` (default on). The group is the pinned feed tab;
the interval is fixed at 10–15 min ±20 %. Originally planned: `groupUrl`, `scanIntervalMin`,
`backfillDays`, `captureEnabled`, `apiKey` (no API key is used).

**userState:** `lastDashboardVisitAt`, `seenListingIds`

### Retention (privacy)

Decided 2026-10-03 (review M6): stored data is mostly other people's (sellers' posts, bidders'
names and replies), so it's kept only while useful, by a daily cleanup in the service worker
(`src/store/retention.ts`, pure and tested; reuses the overview's listing/lot rules):

- Post reads (captures): deleted 7 days after the sale closed (end time + antisnipe), 30 days if
  it's in My Auctions (you bid or claimed). No end time (fixed price, unread end): counted from
  when it was last seen or read, 14 days (30 if yours).
- Posts: deleted once their read is gone, unseen in the feed for 14 days, and ended over 7 days
  ago (or no end time). Until then the slim row (text, seller, link) stays as history.
- Claude's answers: deleted when nothing kept refers to them (keys from `endTimeAnswerKey`,
  `bidAnswerKey`, `claimLotAnswerKey`); no age limit on answers still in use, since deleting one
  would only re-ask Claude.
- A sale that hasn't ended is never touched.
- "Clear stored data" in the overview's Settings empties the store (posts, reads, answers, meta)
  after a confirm step; settings in `chrome.storage.local` stay. The last cleanup's result is
  shown next to it (`cleanupState` in `chrome.storage.local`).

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

- My Auctions above the numbers (redesigned 2026-10-04; it had become one card per sale with
  every lot, 26 equal cards for one busy bidder): a summary line, then **Needs you** (lots where
  you must act: outbid, unclear, check; across sales, soonest first, 5 then "Show all"; each links
  to the lot's comment with the lowest bid that counts, the extension never bids), **Leading** (one
  folded line per sale, what it costs if it holds) and **To pay** (one folded line per seller, with
  Paid / Received). Lost and ended lots live in the table. Rules: `needsYou`, `leadingBySale`,
  `wonBySeller` in `model.ts`.
- **Mark as ended** (2026-10-04): you can mark a sale as ended yourself (an end time nobody
  could read, a seller who closed early), and undo it. Stored as `endedMarks` (post ID → when) in
  `chrome.storage.local`. A marked sale is ended from that moment, and its last full read counts as
  final (`readAfterEnd`): your word replaces "read after end + antisnipe". Retention ignores marks.
- Numbers at the top: active, within 1 h, need you (lots), won lots, new.
- Groups: today (including anything within the hour) · tomorrow and later · claim/fixed price ·
  ended. ("Within 1 h" was its own group until 2026-10-04; merged into Today: the countdown turns
  red under an hour, and the "Within 1 hour" counter stays.)
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

## Module 1 status (spike)

Built, unit-tested against a synthetic fixture, smoke-tested in Chromium, and run against
real Facebook on 2026-10-03 (two posts in the group, nb locale: one auction with 24
comments / 13 lots / 5 replies, one claim sale with 27 comments / 19 lots / 16 replies).
Comment/reply structure, IDs, nesting, images, and sort detection all matched the page; no
warnings, no orphan replies.

- Trigger: toolbar icon → service worker → content script in the active tab.
- Order: switch comment sort to "All comments" (sort control → menu item, `sort.ts`), then
  expand "View more comments" / "View N replies" / "See more" until none are left (`expand.ts`).
- Post root: the topmost `role="dialog"` holding the post's own parts
  (`data-ad-rendering-role="story_message"` / `data-ad-preview="message"`), else `role="main"`
  on a `/groups/<g>/posts/<id>` or `/permalink/<id>` URL. Facebook opens posts in a dialog even
  from a direct link; finding it by `role="article"` (comments) missed posts with no comments
  and read the feed behind instead (fixed 2026-10-03).
- Comment vs. reply: the timestamp link's `comment_id` / `reply_comment_id` query params
  (language-independent), falling back to the article's aria-label ("Comment by" / "Kommentar
  fra", "Reply by" / "Svar fra"). Replies attach to their parent by `comment_id`.
- Lot candidate: top-level comment with a photo (`img` ≥ 64 px, or inside a photo link).
- Output: `PostCapture` JSON (`src/shared/capture.ts`) + an HTML snapshot of the post for `samples/`.

DOM findings from the real run (2026-10-03):

- Post author: the `h3` heading link is the **group name**; the poster is the
  `/groups/<g>/user/<id>/` link in the header (inside `data-ad-rendering-role="profile_name"`).
  The `h2` is a screen-reader title ("<Name> sitt innlegg", sometimes first name only).
  `readPost` now prefers the `/user/` link.
- Post timestamp: Facebook scrambles the header's time text (anti-scraping; the `meta` block
  reads like `p0xiQP.com…`) and the post link has no plain href, so `post.timeText` is `null`.
  Expected: end time comes from the post text ("Sluttid: …"), not the post's age.
- Replies can be image-only (empty `text`, one image): sellers reply to a lot with a close-up
  or a "sold" photo. Lot detection must only look at top-level comments, as it does.
- Bids in replies start with the tagged name ("<Seller> 100", "<Seller> 50kr"); the seller's
  post-auction replies ("Sendt PM …") sit in the same threads. Module 3 has to handle both.
- Not every top-level comment with an image is a lot: in the claim sale, a comment with an
  image plus a tag list ("Denne kommer også …") was a late addition and arguably a lot, so
  "image = lot" is a candidate signal, not a rule.
- Loading comments: a post opened in a dialog shows ~10 comments and loads the rest **on
  scroll**, with no "Vis flere kommentarer" button. The first build only clicked buttons and
  stopped at 10. `expand.ts` now scrolls the last loaded comment into view (never a click)
  when no expander is left, and stops after a few rounds with no new comments.
- Post types seen: "AUKSJON/BUDRUNDE" (bids, `MP`/`MB` = minimum price/minimum increment
  per lot, "Antisnipe 5 min") and "Claim salg" (fixed price per lot, first to claim). Both
  use the group's template ("Sluttid:", "Betalingsalternativ:", …).

- **The group feed is virtualized:** posts that scroll off screen are emptied to
  `data-virtualized="true"` placeholders, so the DOM only ever holds the 2–3 posts near the
  viewport. The feed reader has to record each post when it renders (MutationObserver), never
  read the feed in one pass. The full post text is in the DOM even while Facebook shows
  "… Se mer" (in `data-ad-rendering-role="description"`), so the feed reader doesn't need
  to click it.
- **A feed post has no `role="article"`;** only the preview comments under it do. A post is a
  direct child of the feed holding `data-ad-rendering-role` parts (`profile_name`,
  `story_message`, `description`, `title`, `meta`). Its ID is in comment permalinks
  (`/posts/<id>/?comment_id=…`) or photo links (`set=gm.<id>` for one photo,
  `set=pcb.<id>` for several).
- **The full text (`description`) is usually scrambled** in the feed (random characters, like
  the timestamp), readable in only a few posts. The visible `story_message` is real but cut
  at "… Se mer". In a 52-post sample: 26 auctions, 4 claim sales, 16 fixed price, 6 wanted or
  trade posts. The end time was visible before the cut in 12 of 26 auctions and 3 of 4 claim
  sales; in the rest it sits just past "Se mer".
- Saved snapshots don't round-trip through an HTML5 parser (happy-dom, browsers): Facebook
  nests links inside links, and re-parsing moves content around. Check feed structure on
  the saved file with a non-fixing parser (Python `html.parser`), not by re-loading it.

Findings from a busy live auction (36 lots, 264 replies) and a second claim sale, 2026-10-03:

- **Reply target:** the reply's aria-label says what it answers: "Svar fra A på B sin
  **kommentar**" = a reply to the lot, "… på B sitt **svar**" = a reply to another reply
  (typically the seller's photo reply under the lot). Sellers can reject bids placed under the
  wrong reply ("kan du legge det under hovedbudet"), so the target matters for validity. The
  capture keeps it in `ariaLabel`; module 3 must use it.
- **Bids under another reply don't count:** sellers reject them ("bud blir bare godtatt under
  hovedbildet"), so the overview shows them but leaves them out of the highest bid. When no
  bid reaches a lot's start bid, the highest is still shown, flagged: a seller accepted one
  ("den er grei").
- **Claims** (claim sales, fixed price): every reply from someone other than the seller that
  isn't "." or a question is a claim; it names cards ("claim Persian og Clefairy", "<seller>
  eevee, slowbro og slowpoke"), "alle", or nothing (the lot). One photo often holds several
  cards claimed by different people, so a claim is only contested by an earlier claim of the
  same card, of "alle", or of the unnamed lot.
- **Prices on photos** (claim sales, fixed price): the price is often a note on the photo, one
  per card. Only a model can read it: Claude via the bridge, with the full-size photo (the CDN
  URL's `ctp=p240x240` asks for a 240 px crop; without it the photo is 540×960). Tried on a
  real lot: Sonnet 4/4 cards and prices right in 7 s, Haiku 3/4 in 56 s.
- **Reads are merged, not replaced** (2026-10-03, review H2): a read can miss replies (hidden
  background tab, 4-minute cap, sort switch failed), so each read of a post is merged into the
  stored one by comment/reply ID (`src/domain/captures.ts`). A read counts as *complete* when
  sorted by "All comments", expanding finished, and it saw at least as many comments and replies
  as before; "Won"/"Lost" need a complete read after the end. Known limit: a bid the seller
  deletes on Facebook stays in the overview (can't tell it from one a partial read missed),
  which errs towards "Outbid".
- **Lot names from photos** (2026-10-04): many lots' text is only a price ("Mp 20kr") or empty;
  the photo is all there is. Facebook's image descriptions don't help ("Kan være et bilde av
  tekst" on 227 of 272 lot photos, never the text itself), so Claude names the lot from the
  photo: the seller's text on it if any, else the printed card name and number. Tried on 12 real
  lots in one call: Sonnet named all 12 with set numbers in 6 s, Haiku without numbers in 20 s.
- **Order:** Facebook shows replies out of time order (replies-to-replies first). Reply IDs
  increase with time, so sort by ID to get the order bids were placed. `timeText` ("18 t")
  is too coarse for ordering.
- **Bid text:** almost always "<Seller name> 250" (a tag), sometimes a bare number ("850"),
  sometimes "250kr". Also seen: "580?" (a question, which the seller accepted below the
  minimum price, "den er grei"), and "<Seller> ." (following a single lot, not a bid).
- **Minimum price per lot:** "Mp 10kr" (lower case, no colon) as well as "MP: 1400"; one lot
  had a bare "700kr".
- **End time formats:** "Sluttid: 2026-10-02 22.00", "Sluttid (Lørdag 3. oktober 23.59):",
  "Sluttid: 03.10 Lørdag kl22:00", "Sluttid: Søndag 04.10 kl 21:00", and "Slutt: 05.10.26
  kl 21:00" (label "Slutt", not "Sluttid").
- **Claim sales:** the lot comment can be just an image, with the price written on the
  image ("Pris: … står på kortet"). One image can hold several cards, claimed by name:
  "claim Persian og Clefairy", "clame salazzle", "claim alle", or just the names. Prices on
  images can't be read from text.
- **Other chatter:** top-level comments that are only a person's name (tagging a friend),
  "Sjekk pm", and the seller's own notices ("Da var alle kortene ute!", "starter om 6 min").

One Facebook slot (2026-10-03, review H6): every activity that talks to Facebook (the automatic
scan, post reads from the overview or background re-reads, scans and reads started from the
toolbar menu) takes one slot in the service worker first (`src/background/slot.ts`,
`chrome.storage.session`) and frees it when done, when its tab closes, or after a time limit
(10 min; 25 for a scan from the menu). The automatic scan skips a round when it's busy; reads
queue (your clicks first, no pause); menu actions wait up to 5 min.

Automatic scan (2026-10-03): Chrome doesn't render background tabs, so the feed doesn't load
more posts there. The automatic scan reloads the pinned feed tab instead, records the newest
posts that render at the top, opens their "Se mer", and stops (`whenHidden: "stop"`); a
foreground scan catches up if more came in than that.

Decided (2026-10-03): the feed scan clicks "Se mer" on auction and claim-sale posts (only in
the post's own text) to get the full text with the end time, and scrolls the feed itself. For
now the scan is started by hand from the toolbar icon; the scheduled scan with idle pause
(module 4) comes with storage.

Decided (2026-10-03): "See more" and switching the comment sort to "All comments" were
added to the allowed clicks, since long rules get cut off and "Most relevant" can hide bids.

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
