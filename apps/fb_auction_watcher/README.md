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

## Record a feed sample

For building the overview's feed reader. Open the group's feed (no post open) and click the extension
icon: the panel starts **recording**. Scroll the feed slowly; the panel counts the posts saved. Facebook
empties posts once they leave the screen, which is why it records as you go rather than taking one
snapshot. When you have 20–30 posts, click **Download feed sample**. Nothing is clicked or scrolled by
the extension. Move the file to `samples/` like the others, and never commit it.

## What module 1 does not do

- No interpretation: no amounts, bid validity, or end times. That's module 3.
- No storage, no feed scan, no dashboard.
- No absolute timestamps: Facebook shows relative ones ("2 t") and only shows the exact time on hover, which the extension doesn't do.
