# FB Auction Watcher – spec

- **Id:** `fb-auction-watcher`
- **Type:** Chrome extension (Manifest V3)
- **Owner/user:** Erik Johansen
- **Status:** module 1 (spike) built and run on real posts; overview built: feed scan by hand
  and automatic (pinned tab, background), rule-based listing and bid interpretation, Claude
  Code (`claude -p`) through a native-messaging bridge for what the rules can't read, IndexedDB
  store of raw posts and post reads, table page with your Leading/Outbid status. See
  `notes/fb_auction_watcher/overview-plan.md`. Automatic re-reads of auctions you're in
  (every 15 min, plus a final read on its own alarm 2 min after the close) and desktop notifications
  (outbid, ends in 10 min, won/lost; `src/background/watch.ts`, `notify.ts`; each message starts
  with the sale's type in the badge's words, "Auction · Gengar · …", #322) are built. Not built: overlay, side panel.

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
- Chrome on PC/Mac only. No mobile, no server, no webapp. One exception (2026-10-04, #309): it
  may send **my own wins, nothing else**, to my own tcg_inventory (see "Sending wins to
  tcg_inventory").

## Domain

```
Post = listing: overview photos of the whole auction, rules, end time
 └ Top-level comment WITH an image = lot (single or bundle)
    └ Reply under the lot comment = bid (name, amount, time)
```

- **The post is the lot** (an auction with no lot comments, 2026-10-05): the post's photo and
  text are the one lot, top-level comments under the post are its bids, and a reply under
  someone's comment counts as "under another reply" (not counted). Start bid and raise come from
  the post ("Minstepris", "Minimum budøkning"). `postAsLot` in `src/domain/bids.ts`.
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
  As built: a scan stops after 5 saved posts in a row ("caught up") or 150 posts (60 for the
  automatic scan), not at an age.
- **Continue to older posts** (2026-10-06): a scan stopped early (Stop, its post limit, a hidden
  tab) leaves a gap, and later scans stop at the saved posts above it. When a scan you started is
  done, its panel offers "Continue to older posts": the service worker takes the Facebook slot,
  and the same tab scans on from where it is (no reload), past saved posts without reopening
  their "Se mer", until the end of the feed, 400 posts, or Stop (`CONTINUE_SCAN`). Not offered
  when the feed ended or a "Se mer" opened a dialog. Progress shows how many posts were new.
- **Dropped** (decided 2026-10-06): a pass sorted by "Nylig aktivitet" (recent activity) to catch
  older posts with new bids. "Continue to older posts" covers the need, so it won't be built.
- **Detail reads (lots/bids)** only: when I open a post, and automatically every 15 min for
  auctions I have bid on. Everything else is not read until opened.

## Interpretation (LLM)

**Decided 2026-10-03:** no Claude API and no downloaded models. Rules in code
(`src/domain/`) interpret everything they can; only what they mark unsure goes to Claude Code
on the user's Mac (`claude -p`, their own login) through Chrome native messaging
(`native/fbaw_claude_host.py`), batched, Haiku, no tools, answers cached. The original design
below is kept for reference.

**What goes to Claude, and what doesn't** (2026-10-06): the rules read first; Claude gets only
what they leave, and never other people's names.
- **Times** (Haiku, text): a post whose end the rules can't read, or whose "Startid:" line they
  can't read; the answer is `{ endsAt, startsAt }` (older answers are the end alone, as a string).
- **Unsure bids** (Haiku, text): the reply's text, with the seller's tag as "@Seller"
  (`tagAsSeller`). No seller name.
- **Claim lots**: the photo is read **once** (Sonnet, the photo and the lot's own text, no
  replies): the cards in it and their prices (`claimPhotoRequest`, keyed by the photo's path and
  the lot text, so a new reply doesn't read it again). Who got which card is matched **by the
  rules** (`matchClaims`: first claim wins, misspellings within two letters, "alle", a bare
  claim on a one-card lot). Only claims the rules can't place for sure (they name no card, or
  fit two different cards) go to Claude, as text (Haiku, batched, `claimMatchRequest`), with the
  claimers as "Me", "Claimer 1", "Claimer 2"… and mapped back after. Every claim lot is still
  read, for "N of M available" (decided 2026-10-06).
- **Lot names**: from the lot's own text first (`lotTextInfo`: every line, prices and the
  condition taken out, "Holo"/"Promo" alone isn't a name). An auction lot whose text names
  nothing goes to Claude with its text and photo (Sonnet, batched). A claim lot is named from
  its photo read's cards, with no second look at the photo.

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
  Failure records are kept in the store's meta (`claudeFailures`) for 7 days. A claim photo's key
  is the photo and the lot text only (read once); a claim match's key includes the claims, so a
  new claim the rules can't place is a new question with fresh tries.

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
`myName` (default "Erik Johansen"), `useClaude` (default on), `notify` (default on), `inboxUrl`
and `inboxToken` (tcg_inventory's address and its `INBOX_TOKEN`, empty until set up; see
"Sending wins to tcg_inventory"). The group is the pinned feed tab for auto-scan, and the
constant `GROUP_FEED_URL` (pokemonkortnorge) for the menu's Scan feed in a new tab;
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
  `bidAnswerKey`, `claimPhotoAnswerKey`, `claimMatchAnswerKey`, `lotNameAnswerKey`); no age limit on answers still in use, since deleting one
  would only re-ask Claude.
- A sale that hasn't ended is never touched.
- "Clear stored data" in the overview's Settings empties the store (posts, reads, answers, meta)
  after a confirm step; settings in `chrome.storage.local` stay. The last cleanup's result is
  shown next to it (`cleanupState` in `chrome.storage.local`).

## Sending wins to tcg_inventory

Decided 2026-10-04 (#309): what I win should reach `apps/tcg_inventory` and become a normal
purchase order there, instead of being re-typed from To pay. The extension is the **producer**
of this contract, so it's defined here; tcg_inventory's reader is `won_inbox.py` (README
"Facebook wins inbox").

- **What leaves the browser:** only my own wins: one item per won lot, with the seller, sale
  type, end date, links, the lot's label and price, the seller's quoted shipping/payment text,
  and my Paid/Received marks. **Never** raw captures (post reads), other bidders' or claimers'
  names, comments, or photos.
- **How:** the overview's **Send selected to inventory** (under To pay, by hand for now: you tick
  **Send** on each won auction you want, or "Tick all not sent"; sent ones show "sent 2 h ago",
  kept as `inboxSent`, post ID → when) asks the
  service worker (`src/background/inbox.ts`) to POST the payload to `<inboxUrl>/inbox/fb-wins`
  with `Authorization: Bearer <inboxToken>` (tcg_inventory's `INBOX_TOKEN`), no cookies, no
  redirects followed, 30 s timeout. The result (when, ok/error, how many, what the server said)
  is kept as `inboxState` in `chrome.storage.local` and shown next to the button. Nothing here
  talks to Facebook, so the slot and pacing rules don't apply; nothing is clicked.
- **Never the database directly:** no Supabase client, no Data API (prod has it off). tcg_inventory
  stages the items in its own `won_items` table and only the user turns them into orders.
- **Permission:** `optional_host_permissions` (`https://*/*`, `http://localhost/*`,
  `http://127.0.0.1/*`), so the extension holds no extra host access until Settings → "Send wins
  to tcg_inventory" → Save asks Chrome for that one origin (https, or http on localhost for a local
  `python app.py`). A service-worker fetch to an origin with host permission isn't subject to CORS.
- **Which lots:** exactly To pay's (`wonBySeller`): auction lots once a complete read after the
  end confirms the win, claim lots you claimed first, minus lots you marked **Outbid** (#329). All of them on every send; tcg_inventory
  upserts, so re-sending is safe and a price that was unknown is filled in later. Retention deletes
  a sale's read 30 days after it ends, so a win must be sent within that window (once sent, the
  inbox keeps it).

### Contract, version 1

Built by the pure `buildWonPayload(rows, wonState, sentAt)` (`src/inbox/payload.ts`):

```json
{
  "format": "fbaw-won",
  "version": 1,
  "sent_at": "2026-10-04T20:00:00.000Z",
  "items": [
    {
      "external_ref": "fbaw:<postId>:<commentId>",
      "seller": "Selger Testesen",
      "sale_type": "auction",
      "ended_on": "2026-10-04",
      "post_url": "https://www.facebook.com/groups/<g>/posts/<postId>/",
      "lot_url": "https://www.facebook.com/groups/<g>/posts/<postId>/?comment_id=<commentId>",
      "label": "1. Gengar 151 reverse holo",
      "price": 50,
      "shipping_text": "50kr med sporing",
      "payment_text": "Vipps eller bank",
      "paid_at": "2026-10-04T21:00:00.000Z",
      "received_at": null
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `external_ref` | The item's identity, stable across sends: `fbaw:<postId>:<commentId>`, or `fbaw:<postId>:pos<n>` (the lot's position) when the lot has no comment ID |
| `seller` | The seller's name, or null |
| `sale_type` | `auction`, `claim` or `fixed` |
| `ended_on` | The sale's end date in Europe/Oslo (`YYYY-MM-DD`): its end time, or when I marked it ended if that came first; null when it has neither |
| `post_url` / `lot_url` | The sale post; the lot's comment (the post when it has no comment ID) |
| `label` | "`<position>. <lot title>`"; for a claim lot, the cards I got (or named) |
| `price` | What I pay in kr: the winning bid, or the claimed cards' prices (Claude's reading of the photo, else the lot text's price); **null** when not known yet, never 0 |
| `shipping_text` / `payment_text` | The seller's own terms from the post ("Sender med post …", "Betalingsalternativ: …"), as written, or null |
| `paid_at` / `received_at` | My Paid / Received marks (ISO), or null |

- **Claim lots are always one lot-level item**, even when Claude priced each card: per-card refs
  would change once the photo is read and leave duplicates behind. A multi-card lot is linked to
  several cards in tcg_inventory.
- **Versioning:** `version` is the major version. Adding an optional field is compatible
  (tcg_inventory ignores unknown fields); renaming, removing or changing the meaning of one is a
  new major version, and tcg_inventory refuses a version it doesn't know with a readable error.
- **Guard:** `tests/won-inbox.test.ts` builds the payload from invented posts and writes it to
  `tests/fixtures/won-inbox.v1.json` at the repo root (a Vitest file snapshot: rewritten by a local
  `npm test`, a mismatch fails in CI); the root `tests/test_cross_app_won_inbox.py` parses it with
  tcg_inventory and checks it carries exactly these fields. The fixture is anonymized; never
  build it from `samples/`.

## Statuses

Per listing:

| UI label | Meaning |
|---|---|
| New | First seen after my last visit to the table page, **or** first seen less than 30 min ago (2026-10-05, #320: a rescan or a quick visit inside that window doesn't clear it; `NEW_WINDOW_MS` in `model.ts`) |
| Activity | More comments than last time (`commentCount` > `commentCountPrev`) |
| Ending soon | Less than 1 hour left |
| Unknown end time | End time could not be interpreted |
| Ended? | End time passed, but the soft close window is not over |
| Ended | End time (and any soft close window) passed |
| Sold/withdrawn | Seller marked the sale as sold or withdrawn |

Per lot: **Leading** / **Outbid**. A listing shows a summary, e.g. "Leading 2 · outbid 1".

## Design

- **Colors:** blue `#2457D6` = Leading, orange `#C2570C` = Outbid, red `#B42318` = under 1 h.
- **Sale-type colors** (added 2026-10-05, #322; `--type-*` in `dashboard.css`): purple `#8250DF` =
  Auction, teal `#1B7C83` = Claim, gold `#9A6700` = Fixed price; Unknown uses the muted grey with a
  dashed border. Dark theme: `#a371f7` / `#39c5cf` / `#d29922`. New tokens because reusing blue/orange
  would read as Leading/Outbid; the badges are outlined (text and border in the colour) so they never
  look like the filled status pills.
- **Fonts:** IBM Plex Sans / IBM Plex Mono.
- **Language:** English UI.

### Table page (`dashboard.html`)

- My Auctions above the numbers (redesigned 2026-10-04; it had become one card per sale with
  every lot, 26 equal cards for one busy bidder): a summary line, then **Needs you** (lots where
  you must act: outbid, unclear, check; across sales, soonest first, 5 then "Show all"; each links
  to the lot's comment with the lowest bid that counts, the extension never bids), **Leading** (one
  folded line per sale, what it costs if it holds) and **To pay** (one folded line per seller, with
  Paid / Received). Lost and ended lots live in the table. Rules: `needsYou`, `leadingBySale`,
  `wonBySeller` in `model.ts`. Under To pay: a **Send** tick per won auction, **Send selected to inventory** and the last send's
  result (2026-10-04, #309; see "Sending wins to tcg_inventory").
- **Mark as ended** (2026-10-04): you can mark a sale as ended yourself (an end time nobody
  could read, a seller who closed early), and undo it. Stored as `endedMarks` (post ID → when) in
  `chrome.storage.local`. A marked sale is ended from that moment, and its last full read counts as
  final (`readAfterEnd`): your word replaces "read after end + antisnipe". Retention ignores marks.
- **Outbid** (2026-10-06, #329): each lot under To pay (and a won lot in the expanded table) has
  **Outbid**, for a win the rules got wrong (e.g. your bid came after the end and the seller said
  so under it). Stored as `notWonMarks` (`<post ID>:<comment ID>`, or `:pos<n>` for a lot without
  one, → when) in `chrome.storage.local`. A marked lot is outbid whatever the rules read
  (`lotStatus` in `model.ts`: key `lost`, as when a bid beat yours, shown "Outbid (your mark)"): it
  leaves To pay, so it's never in the inbox payload, and it's listed under To pay's folded "Marked
  outbid by you" with **Undo outbid**. A lot already
  sent to tcg_inventory stays in its inbox (sends upsert, never delete): ignore it there.
- Numbers at the top: active, within 1 h, need you (lots), won lots, new. Ended sales never count,
  including ones still shown in an active tab for their 30 min (#320).
- **Tabs** (2026-10-04; they replaced stacked, foldable groups): New (first seen since the last
  visit or under 30 min ago, see Statuses; not ended, except just ended below) · Today (including anything within the hour) · Upcoming (was "Tomorrow and
  later") · No end (end time unknown, then fixed price) · My bids (running, then ended) · Ended
  (Yours, then Everyone else). Claim sales sit in Today / Upcoming by end time. Counts follow the
  filter and search; the tab is remembered (localStorage `fbaw-tab`); ← / → move between tabs.
  `tabs()` in `model.ts`.
  **Just ended** (2026-10-05, #320): a sale that ended (end time + antisnipe window passed, or you
  marked it ended) stays in the active tabs it was in for less than 30 min after it ended
  (`ENDED_GRACE_MS`, `Row.justEnded` / `Row.endedAtMs`), dimmed and shown as ended (Ends says
  "Ended …", Result replaces Price, the header reads "Price / result"), and it doesn't count in the
  numbers at the top (active, within 1 h, new). At 30 min it drops out and is only in Ended; it is in
  Ended from the moment it ends, as before. Readings chosen: "active tabs" = every tab but Ended
  (New, Today, Upcoming, No end, My bids); the tab is the one its state just before the end put it in,
  judged at the moment it ended: a sale ending by its end time was in Today; one you marked ended
  early stays in Today or Upcoming by where it was at the mark; one with no end time (or fixed price)
  stays under No end; New keeps it while its own New window lasts (and the 30 min since the end);
  in My bids it stays under "Running" for the 30 min, then moves to My bids' own "Ended" part (which
  was already there, so My bids keeps showing your ended sales). The 30 min count from when it
  became ended: the end of the antisnipe window, or your mark if that came first, so a mark long
  after the end doesn't restart them, and a sale with no end time counts from the mark.
  ("Within 1 h" was its own group until 2026-10-04; merged into Today: the countdown turns
  red under an hour, and the "Within 1 hour" counter stays.)
- Columns (redesigned 2026-10-04: every row two lines, columns line up across groups):
  Ends (countdown / end time · antisnipe) · Sale (photo; title without the template's type words,
  `saleLines` in `model.ts`, and New / Reading…; description, cut with "…") · Seller · Price (type /
  terms) · Lots · You. Type and Seen are no longer columns (first seen came back as Added).
- **Sale-type badge** (2026-10-05, #322): every sale's name starts with its type, as a badge with
  text (`typeBadge` in `badge.ts`, labels `SALE_TYPE_LABEL` in `src/domain/listing.ts`): Auction /
  Claim / Fixed price, the filter's words, then New. Same badge on My Auctions' Needs you, Leading and
  To pay lines. Price is now terms only ("Min 10 kr · +5", "2000 kr", "Price per item"): the type line
  it used to start with duplicated the badge. **Unknown**: a post whose type the rules can't tell but
  that is laid out as a sale (`isUntypedSale`: a description, minimum price, increment or end line)
  stays in the table as type `other`, shown "Unknown", never guessed; until then those were dropped
  with the chatter. Its Price shows only what the post states (min price, increment) or "See the
  post"; its lots are read the default (bid) way, and it gets no Claude end-time read or
  notifications (both are for auctions/claims). Wanted, trade and chatter posts are still left out.
- **Seller's original end text** (decided 2026-10-04, relaxing "Keep raw text" for the table): in the
  Ends tooltip when the rules are sure; in the row, flagged, when they weren't or Claude read it.
  Raw text is still stored and shown in full in the lots.
- **Added** column (2026-10-04, after Ends): when a scan first saw the post ("1 d ago" / the date).
  Ends shows only the end: "No end" for fixed price, "Unknown" (and "cut off") otherwise. Mark as ended: in the expanded row, for every sale.
- **Ended group** (reviewed 2026-10-04: it's for looking back: how your bids went, what things sold
  for, and whether that's final). Your sales first under "Yours", then "Everyone else", newest first
  in each (`splitEnded`). Ends says "Ended 3 h ago" (muted; it was red, a ticker bug). Result
  replaces Price: "9 of 12 sold" / "6220 kr · final" or "at last read <when>" (`saleResult`: lots
  with a valid bid or a claim, the sum of winning bids or Claude-priced claimed cards; final =
  `readAfterEnd`), "Not read" if never read. Expanded lots: "Sold 1100 kr · <bidder>" or "Unsold",
  yours first with Won/Lost, flagged "at last read" when not final.
- Filters: All / Auction / Claim / Fixed price + search (My bids and New are tabs). The same words as
  the type badges (a test checks it); Unknown sales show under All only.
- Rows I'm active in get a colored left border and can expand to "Your lots in this
  auction" (image, highest bid, my bid, status).
- Clicking the title opens the Facebook post.

### Toolbar menu (`popup.html`)

Decided 2026-10-05 (#320): four buttons (Open Dashboard, Scan feed, Scan Post, Reload extension);
nothing starts until you pick one. Which button does
what for the active tab is the pure `popupActions()` in `src/pages/popup/actions.ts`.

- **Open Dashboard:** opens (or focuses) `dashboard.html`.
- **Scan feed:** the active tab is the group feed → scan it there (as before, re-sorted to "New
  posts" first). Otherwise → the service worker takes the Facebook slot first (waiting up to 5 min,
  like other menu actions), then opens the group feed sorted by "New posts" in a new, active tab
  (a scan needs a visible tab), waits for it to load, and starts the same scan there
  (`MSG_SCAN_FEED_NEW_TAB`, `scanFeedInNewTab` in `src/background/index.ts`). The slot is held for
  "no tab yet" and moved to the new tab once it exists, so no second tab talks to Facebook. Which
  group (changed 2026-10-05 on the user's word, replacing an inference chain of active tab's group →
  open feed tab → last seen post's group): always https://www.facebook.com/groups/pokemonkortnorge,
  the named constant `GROUP_FEED_URL` in `src/shared/urls.ts`, opened as
  `?sorting_setting=CHRONOLOGICAL` (`newPostsUrl`), the same parameter the in-tab scan uses. So the
  button is never disabled. A constant rather than a setting: one user, one group, and the planned
  `groupUrl` setting was never built (see "settings" above). "The group feed" for scanning here is
  still any `isGroupFeedUrl` tab (vanity slug or numeric ID, any query string).
- **Scan Post:** reads the post shown in the active tab (the full panel, as before). Reading chosen:
  when the active tab isn't a Facebook post (`isPostUrl` in `src/shared/urls.ts`: a group post or
  permalink, a profile/page post, `permalink.php` / `story.php`) it's disabled with "Open a post on
  Facebook to read it." instead of doing nothing or navigating.
- **Progress, worded alike** (2026-10-06): the read-post panel and the quiet-read overlay (bottom
  right, a sale opened from the overview; `pill.ts`) are both titled "FB Auction Watcher: read
  post" and show the same line as the feed scan's "Scanning… 12 posts saved, 3 scrolls, 2 "Se mer"
  opened.": "Reading… 84 comments, 212 replies loaded, 30 scrolls, 12 expanded." (counted as the
  read counts them, `countLoaded` in `extract.ts`), then "Done: 84 comments, 212 replies saved ·
  3 lots · 13 bids · Leading 1". The extension's name is "FB Auction Watcher" everywhere (manifest
  `name` and `short_name`, toolbar menu, panels).
- Not buttons: the auto-scan status line (on/off, next run, last outcome). Its **off switch stays in
  the dashboard's Settings** (pacing rule: a user-facing off switch); the menu says where.
- **Reload extension:** `chrome.runtime.reload()`, the same call the menu's button made before #320.
  #320 first moved it to the right-click menu only; it came back the same day because the user asked
  for it (it's the step after every `npm run build`). Same on every tab, so not in `popupActions()`.
- **Reload Facebook tabs** stays off the menu: it's on the icon's right-click menu (with Open
  overview and Reload extension, which is there too), where it already was.

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
- **Lot text** (2026-10-06): a lot's text often names the card and gives its price and condition
  on one line ("Iron Jugulis 216/182 – Illustration Rare | MP: 20", "Umbreon VMAX 215/203 NM -
  1200kr"), sometimes the name on line 2 under the price. The title is the name with the price
  parts out, then "· NM" when the text gives a condition. "MP" is the minimum price, except after
  "Tilstand:" or on its own before a price in a claim lot ("MP - 250kr"): then it's the condition.
- **Claim-sale template** (2026-10-06): "Claim-salg (Tagg deg selv i kommentarfeltet om du ønsker
  å delta)" then "Startid:", "Sluttid (maks 24 timer):", "Objektbeskrivelse:", "Tilstand:". The
  bracketed instruction isn't a name, so "Objektbeskrivelse" names the sale. "Startid" is
  optional (claim sales only); sellers usually don't post the lots until then, so before it the
  overview shows "Starts … · lots not posted yet". Claims and bids aren't judged by it.
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
