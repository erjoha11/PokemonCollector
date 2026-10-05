# FB Auction Watcher

Chrome extension (Manifest V3) that gives a read-only overview of auctions in a Facebook buy/sell group for Pokémon cards: every sale sorted by end time, with live countdowns and Leading/Outbid status on lots you've bid on.

**Status:** the overview works: feed scans (by hand and automatic), post reads, your Leading/Outbid/Won status, claim sales with what's still for sale, and Claude (through your Claude Code login) for what the rules can't read. Not built yet: an overlay on the post itself and a side panel. See [`docs/spec.md`](docs/spec.md) for scope and rules, and [`CLAUDE.md`](CLAUDE.md) for development guidance.

## Build

Needs Node 22.12+ (or 24+).

```bash
cd apps/fb_auction_watcher
npm ci             # first time, and after package-lock.json changes
npm run build      # → dist/
npm test           # unit tests (Vitest + happy-dom, offline)
npm run typecheck
```

## Load it in Chrome

1. `chrome://extensions` → turn on **Developer mode** (top right).
2. **Load unpacked** → pick `apps/fb_auction_watcher/dist/`.
3. Pin the extension (puzzle icon → pin) so its icon is on the toolbar.

**Clicking the icon opens a small menu** with three buttons; nothing starts until you choose one (#320):
**Open Dashboard** (the overview); **Scan feed** (scans the group feed in this tab if it's open here, otherwise
opens the feed in a new tab and scans it there once it has loaded, waiting its turn if something else is
talking to Facebook); and **Scan Post** (reads the post shown in this tab; greyed out, with a hint, on anything
that isn't a Facebook post). Below them, the auto-scan status (switch it on/off in the overview's Settings).
**Reload extension** and **Reload Facebook tabs** are on the icon's right-click menu, with **Open overview**.

After every `npm run build`: **Reload extension**, then **Reload Facebook tabs** (open tabs need the new
content script). A new permission in the manifest needs the reload icon on the extension's card in
`chrome://extensions` once instead. A tab opened before the extension was (re)loaded has no content script; the icon then shows a `!` badge.

## Read one post (with the full panel)

1. Open an auction post in the group as a single post: click the post's timestamp, or open it so it shows in a dialog.
2. Click the extension icon. A panel appears bottom right. It first switches the comments to **All comments** / **Alle kommentarer** ("Most relevant" can hide bids), then clicks "View more comments" / "View N replies" / "See more" ("Vis flere kommentarer" / "Vis N svar" / "Se mer") one at a time with pauses, and scrolls down the comments when Facebook loads more on scroll instead of with a button. Nothing else is ever clicked. A large auction can take a few minutes. **Stop** halts it.
3. When it's done, the panel shows counts and warnings, plus:
   - **Download JSON**: the raw capture (post, top-level comments with image flag, replies nested under each).
   - **Download HTML snapshot**: the post's DOM as rendered, as a sample for `samples/`.

Check against what you see on Facebook:

- Is the number of comments with an image the same as the number of lots?
- Is every bid under the right lot, with the right name and text?
- Are any comments or replies missing? Count a couple of lots by hand.
- Did the comment sort end up on All comments (shown in the panel)?
- Is long text (rules, end time) complete, not cut off?
- Do the panel's warnings make sense?

Downloads land in your Downloads folder. Move them to `apps/fb_auction_watcher/samples/` (gitignored). **Never commit them**: they contain other people's names and comments.

## The overview

**My Auctions** at the top is a to-do list for your own bidding, in three sections, with one summary
line above them ("14 need you · 12 leading (4600 kr if they hold) · 412 kr to pay"):

- **Needs you**: every lot where you have to act, across all sales, soonest ending first. Outbid,
  **Leading?** (a reply couldn't be read) and **Check** (someone claimed the same card first). One
  line each: photo, lot name, sale and seller, countdown, the highest bid (and yours). **next 190 kr+**
  is the lowest bid that counts now; click the lot's name to open its comment on Facebook in a new
  tab, and bid there yourself. The first 5 show, then **Show all**.
- **Leading**: one folded line per sale (countdown, title, seller, "12 lots · 4600 kr", which is
  what you pay if they all hold). Unfold it to see the lots. A sale that ended but hasn't been read
  since says "ended: waiting for a final read".
- **To pay**: what you've won, one folded line per seller with the total ("400 kr + shipping"), how to
  pay, and **Paid** / **Received** ticks. Unfold it for the lots and the seller's shipping and payment
  lines quoted from the post. A seller with both ticked folds away ("Show paid & received" brings them
  back). Auctions count once a full read after the end confirms the win; claim lots show "price not
  read yet" until Claude has read the photo or the seller's text.

**Mark as ended** (under a sale's end time in the table, on a Leading line, and as **Sale ended**
under a Needs you line) tells the overview a sale is over when its end time says otherwise: none
could be read, or the seller closed early. The sale moves to Ended, and your lots' statuses come
from its last full read (Leading becomes Won and shows under To pay). **Undo ended** (there, or on
the seller in To pay) takes the mark back. The mark is yours only, stored in the extension.

Lost lots and finished sales aren't listed there; they're in the table with their status. The
**Need you** and **Won lots** counters sit next to the others. Below it are the counters and the full table. Clicking a row shows or hides its lots. A
sale's name opens the post on Facebook; its **Read** button reads its lots and bids now.
A lot's name, wherever it shows (Needs you, Leading, To pay, a row's lots), opens its own comment on Facebook (where you'd bid).

**Right-click the extension icon → Open overview** (or **Open overview** in the scan panel). It lists every
auction and claim sale the scans have saved, in tabs: **New** (first seen since your last visit, or in the last 30 minutes, so a rescan
or a quick visit doesn't clear it) · **Today** · **Upcoming** (tomorrow and later) · **No end** (end time
unknown, then fixed price) · **My bids** (running, then ended) · **Ended**. A sale that ends (or that you mark
as ended) stays in the tab it was in for 30 minutes after the end, dimmed and shown as ended (not counted
as active); after that it's only under **Ended** (and My bids' own Ended part). Each tab shows how many sales it holds under the current filter and search; ← / →
switch tabs, and the overview remembers the last one. Claim sales sit in Today / Upcoming by their end
time. Countdowns under an hour turn red. Every row is two lines:
**Ends** (countdown, then end time and antisnipe), **Sale** (photo, the title without the template's
"AUKSJON/BUDRUNDE"/"FASTPRIS", then the description, cut with "…"; hover for the full text and when it was
first seen), **Seller**, **Price** (type, then the terms), **Lots** and **You**. The seller's original end text is
in the Ends tooltip; it shows in the row instead when the rules weren't sure ("?") or Claude read it. The
**Added** column shows when each sale was added ("1 d ago", then the date): when a scan first saw it, close to when
it was posted while auto-scan runs. **Mark as ended** is in a row's lots (click the row).
**Ended** is for looking back: your sales first ("Yours"), then everyone else's. **Result** shows how
it went ("9 of 12 sold · 6220 kr") and whether that's **final** (read after the end) or only **at last read**.
Click a row for each lot: "Sold 1100 kr · buyer" or "Unsold", with your Won/Lost first. Only one thing talks to Facebook at a time: if a scan or another read is running, your **Read** waits
("Queued") and starts as soon as it's done, ahead of the automatic re-reads. It reads the post (comments,
replies, bids) in a background tab that closes itself; the button says **Reading…** and the row updates
when it's done. The **My bids** tab lists the sales you're
bidding or claiming in.

**Bids and your status:** click a sale's title in the overview, or open a post on Facebook and click the
icon (it reads every comment and reply).
The overview then shows its lots and bids, and **Leading / Outbid** per lot (blue / orange edge on the
row; **Your lots** expands them). Rules: a lot is a comment with a photo from the seller; bids are replies
to it, in the order they were placed; the seller's own replies never count; a bid has to beat the highest
by the increment; bids placed under another reply (e.g. under the seller's photo) are shown but not
counted, since sellers reject them. Set your Facebook name under **⚙ Settings** (top right of the overview) if it isn't Erik Johansen.
Each read of a post is merged with the earlier ones, so a read that missed comments (a background tab, the
time limit) never hides a bid already seen; the row then says **partial read** with when the last full
read was. **Won**/**Lost** only show after a full read made after the auction ended.

**Claim sales and fixed price:** replies are read as claims ("claim Persian og Clefairy", "<seller>
marowak og feraligatr", "claim alle"). First to claim a card gets it: you get **Won** when nobody claimed
the same card (or everything) before you, and **Check** when someone was earlier. For lots you've claimed
on, and then every other lot in the sale, Claude reads the photo (prices are usually written on it) and
the replies: every card, its price, and who claimed it first. The overview shows what you won and for how
much ("You won 2 · 400 kr"), and per lot which cards are taken (crossed out) and which are still for sale
("4 of 8 available"); a lot stays open until every card is claimed ("Sold out").
A "." is someone following the lot, not a claim. Click any photo for a bigger picture; **← / →** (or the
‹ › buttons) go to the previous / next lot's photo in that sale.

Posts and reads are saved in the extension (IndexedDB). Only raw text is stored; the overview interprets
it every time it loads, so improved rules apply to old posts too.

### Stored data and retention

What's stored is mostly other people's data (sellers' posts, bidders' names and replies), so it's kept
only as long as it's useful. Once a day (a `chrome.alarms` alarm) the service worker deletes
(`src/store/retention.ts`):

- a sale's **post read** (comments, replies, names) 7 days after the sale ended, or **30 days** if you
  bid or claimed in it (so it lasts long enough to pay and follow up). A sale
  with no end time (fixed price, or an end nobody could read) counts from when it was last seen or read,
  with a 14-day window (30 if yours);
- a **post** once its read is gone, it hasn't been seen in the feed for 14 days, and it ended over 7 days
  ago (or has no end time);
- **Claude's answers** nothing kept refers to any more (answers still in use stay whatever their age:
  deleting one would only ask Claude the same question again).

A sale that hasn't ended is never touched. The last cleanup's result shows under **Settings → Stored
data**, next to **Clear stored data**, which (after an "Are you sure?") empties posts, post reads,
Claude's answers and the last-read time, and keeps your settings.

## Automatic scan

Under **Settings** in the overview: **Scan the feed automatically every 10–15 min**. It needs a **pinned**
tab with the group's feed (right-click the tab → Pin). Every 10–15 min (±20 %) it loads that tab's group
feed sorted by **New posts** (`?sorting_setting=CHRONOLOGICAL`, whatever sort the tab had) in the background, saves the newest posts and opens their "Se mer", and stops once it reaches posts it already
has. While it's on, auctions you're bidding in are also re-read every 15 min in a background tab that
opens and closes by itself, one at a time and never during a feed scan. It never uses more than one tab
at a time, skips a round while your screen is locked, you're
away, or you're looking at that tab, and is off until you switch it on. A background tab only shows the
first few posts, so if more were posted than that since the last round, the status says so: open the
feed tab and click the icon to catch up.

**Final read on time.** While auto-scan is on, each auction you're bidding in is read once more
2 minutes after it closes (its end plus antisnipe), on its own alarm rather than at the next scan, so
**Leading** turns into **Won** or **Lost** by itself. If you're away then, the next scan round tries
again (for up to 2 hours). With auto-scan off, the overview says "open it to see the result".

**Desktop notifications** (Settings, on by default): **Outbid** when a read finds someone has bid over
you (with the next valid bid), **Ends in 10 min** for a sale you're bidding in, and **Won N lots ·
X kr / Lost** once its final read is in. Each shows once; clicking it opens the lot (or the post) on
Facebook, nothing more. Outbid and Won/Lost come from reads, so they arrive by themselves only while
auto-scan is on; background re-reads run every 15 min, so an outbid can show up to 15 min late. On
macOS, Chrome must be allowed to show notifications (System Settings → Notifications → Google Chrome).

**Send wins to inventory** (under To pay, #309) sends what you've won to your tcg_inventory, where
each won sale waits under Orders → Purchased → "Facebook wins to register" until you register it.
Only your own wins leave the browser: seller, lot label, price, links, dates, the seller's shipping
and payment terms, and your Paid/Received marks; never post reads, other people's names or comments.
Set it up once under **Settings → Send wins to tcg_inventory**: the app's address (https, or
http://localhost for a local one) and the same token as its `INBOX_TOKEN`, then **Save** (Chrome
asks for permission to reach that address). Sending again is safe: nothing is duplicated, prices that
weren't known yet are filled in, and a lot you ignored or registered there stays as it is. The
result of the last send shows next to the button. Contract: `docs/spec.md` "Sending wins to
tcg_inventory".

## Claude for what the rules can't read

End times written as free text ("avsluttes søndag kveld klokka ni") and bids that aren't plain numbers
("200 sorry mente 250", "580?") are sent to **Claude Code** on this Mac (`claude -p`, your own Claude login,
no API key), batched, the smallest model (Haiku), no tools, each answer cached so it's asked only once.
Lots whose text doesn't name them (only "Mp 20kr", or nothing) are named from their photo: the seller's text
on the photo if there is some, else the card name and number as printed ("Pikachu 74/112"); up to 12 photos
per call to Sonnet, posts you're in first, not for sales that ended hours ago, each name asked once, and
each photo counts against the hourly photo cap.
Claim lots you've claimed on go one at a time with their full-size photo to Sonnet, which reads prices on
photos reliably (Haiku misread one on a real lot); the bridge downloads photos only from Facebook's CDN. The
extension reaches it through Chrome's native messaging: a small script, `native/fbaw_claude_host.py`,
which only passes the text to `claude -p` and the answer back.

Only live sales are asked about: nothing from a sale that ended more than 6 hours ago, and for sales with no
end time (fixed price) nothing once the post hasn't been seen or read for 3 days. Photo calls (Sonnet) are capped
at 20 an hour; the rest waits for later runs. If one item fails (typically a lot photo Facebook's CDN no longer
serves: its links expire), it's tried again after 15 minutes, then after an hour, and then skipped; the other
items carry on. Only a failure that affects everything (bridge not installed, `claude` missing or not logged
in, out of quota) pauses Claude, for 10 minutes. The Claude line under **Settings** shows the last run, e.g.
"Read 5 claim lot photos · 2 skipped (photo unavailable) · photo limit reached, rest later".

One-time setup:

```bash
apps/fb_auction_watcher/native/install.sh            # or: install.sh <extension ID from chrome://extensions>
apps/fb_auction_watcher/native/install.sh --uninstall
```

It writes one file, `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.erjoha.fbaw.claude.json`,
telling Chrome where the script is and that only this extension may start it. Claude runs with no tools, no
MCP servers or connectors and no user settings, so text in a Facebook post can't make it do anything but answer.

**What Claude gets:** the text of posts and replies (including commenters' names) and lot photos, from posts
you've scanned or read. It's **on by default**; switch it off under **Settings** in the overview, and those
items just stay marked unsure ("Leading?" where a reply couldn't be read). `npm run eval:claude` checks the
prompts on `src/llm/cases.ts` through the real bridge (uses your Claude plan).

## Scan the feed

Click the extension icon → **Scan feed** (on the group's feed with no post open it scans that tab; anywhere
else it opens the feed in a new tab first). If the feed isn't sorted by **New
posts**, the extension reloads it that way first (`?sorting_setting=CHRONOLOGICAL`). It then scrolls the
feed slowly by itself and saves each post as it appears (Facebook empties posts once they leave the
screen). On auction and claim-sale posts it clicks **Se mer** so the full text, with the end time, is
saved. Those are its only clicks. It stops when it reaches 5 posts in a row that an earlier scan
already saved ("caught up"), at the end of what the feed loads, after 150 posts, or when you press
**Stop scan**. Posts are saved as it goes, so the overview fills in while it runs.

Start from the top of the feed (reload the tab) to pick up the newest posts. Keep the Facebook tab
visible while it scans: Chrome barely runs background tabs, so the scan **pauses** while the tab is
hidden and continues when you come back. To watch the overview at the same time, open it in a
separate window.

**Download feed sample** saves everything scanned so far as one HTML file. Move it to `samples/`
like the others, and never commit it.

## What it doesn't do (yet)

- No overlay on the Facebook post itself, and no side panel (modules 6–7 in the spec).
- No absolute timestamps: Facebook shows relative ones ("2 t") and only shows the exact time on hover, which the extension doesn't do.
- No notifications.
