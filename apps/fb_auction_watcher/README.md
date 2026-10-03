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

After every `npm run build`, click the reload icon on the extension's card, **and reload any open Facebook tabs**. A tab opened before the extension was (re)loaded has no content script; the icon then shows a `!` badge.

## Test module 1: read one post

1. Open an auction post in the group as a single post: click the post's timestamp, or open it so it shows in a dialog.
2. If the comments say **Most relevant** / **Mest relevante**, switch to **All comments** / **Alle kommentarer** yourself. The extension never changes the comment sort, and "Most relevant" can hide comments.
3. Click the extension icon. A panel appears bottom right and starts clicking only "View more comments" / "View N replies" ("Vis flere kommentarer" / "Vis N svar"), one at a time with pauses. A large auction can take a few minutes. **Stop** halts it.
4. When it's done, the panel shows counts and warnings, plus:
   - **Download JSON**: the raw capture (post, top-level comments with image flag, replies nested under each).
   - **Download HTML snapshot**: the post's DOM as rendered, as a sample for `samples/`.

Check against what you see on Facebook:

- Is the number of comments with an image the same as the number of lots?
- Is every bid under the right lot, with the right name and text?
- Are any comments or replies missing? Count a couple of lots by hand.
- Do the panel's warnings make sense?

Downloads land in your Downloads folder. Move them to `apps/fb_auction_watcher/samples/` (gitignored). **Never commit them**: they contain other people's names and comments.

## What module 1 does not do

- No interpretation: no amounts, bid validity, or end times. That's module 3.
- No storage, no feed scan, no dashboard.
- No "See more" clicks: long text that Facebook cuts off stays cut off and is flagged as `truncated`. "See more" isn't on the allowed-click list in the spec.
- No absolute timestamps: Facebook shows relative ones ("2 t") and only shows the exact time on hover, which the extension doesn't do.
