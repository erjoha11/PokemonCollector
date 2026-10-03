# FB Auction Watcher

Chrome extension (Manifest V3) that gives a read-only overview of auctions in a Facebook buy/sell group for Pokémon cards: every sale sorted by end time, with live countdowns and Leading/Outbid status on lots you've bid on.

**Status:** module 1 (spike) built: reads one open post and exports it as raw JSON. See [`docs/spec.md`](docs/spec.md) for scope and rules, and [`CLAUDE.md`](CLAUDE.md) for development guidance.

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

After every `npm run build`, **right-click the extension icon → "Reload extension and Facebook tabs"**. It reloads the extension from `dist/` and then every open Facebook tab, which needs the new content script. (The manual way: the reload icon on the extension's card in `chrome://extensions`, then reload the Facebook tabs.) A tab opened before the extension was (re)loaded has no content script; the icon then shows a `!` badge.

## Test module 1: read one post

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

**Right-click the extension icon → Open overview** (or **Open overview** in the scan panel). It lists every
auction and claim sale the scans have saved, grouped by end time (within 1 hour · later today · tomorrow
and later · end time unknown · claim and fixed price · ended), with live countdowns. Each end time shows
the seller's original text next to it; a "?" means the rules weren't sure. Click a title to open the post.

Posts are saved while a scan runs (IndexedDB, in the extension). Only raw text is stored; the overview
interprets it every time it loads, so improved rules apply to old posts too. Bids and your Leading/Outbid
status are not in it yet.

## Scan the feed

Open the group's feed (no post open) and click the extension icon. The extension then scrolls the
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

## What module 1 does not do

- No interpretation: no amounts, bid validity, or end times. That's module 3.
- No storage, no feed scan, no dashboard.
- No absolute timestamps: Facebook shows relative ones ("2 t") and only shows the exact time on hover, which the extension doesn't do.
