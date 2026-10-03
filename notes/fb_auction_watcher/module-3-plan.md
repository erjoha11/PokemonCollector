# Module 3 – Interpretation: plan

Status: **proposed 2026-10-03, waiting for go-ahead.** Builds on module 1's `PostCapture`
JSON. Spec sections: "Interpretation (LLM)", "Data model", "Statuses".

**Decided 2026-10-03: no Claude API, so no token costs.** Interpretation is rule-based
parsing in code. This replaces the spec's "Interpretation (LLM)" section, which gets rewritten
as part of this module.

## Goal

Turn one raw capture into interpreted data: the listing (type, end time, close rule, prices)
and its lots, bids, highest bid and your status per lot. Anything a rule can't read with
confidence is marked **unsure** and shown with the seller's original text, never guessed. You
try it from the existing panel on a post.

## Why rules can work here

The group requires a posting template. Both captured posts follow it:

```
AUKSJON/BUDRUNDE-annonse … | Claim salg-annonse …   → type
Sluttid: 2026-10-02 22.00 | Sluttid (Lørdag 3. oktober 23.59):   → endsAt
Antisnipe 5 min: Ja                                  → soft close, 5 min
Minstepris / Minimum budøkning: Står over bildene   → per lot
Sender med post …: 32/50/86/150                      → shippingText (raw)
```

Lots: `MP: 1400` / `MB: 10` (minimum price / minimum increment) in auctions, `NM - 1200kr`
(fixed price) in claim sales. Bids are short replies: `100`, `50kr`, `Martin K. Hermansen 55`.
Top-level comments of just `.` are people following the sale: counted as watchers, never lots
or bids.

## What gets built

| Piece | Where | What |
|---|---|---|
| Text helpers | `src/domain/text.ts` | Normalize Norwegian text and amounts: `250kr`, `250,-`, `bud 250`, `2.5k`, `1 400`, `1.400`. Strip a leading tagged name ("<Seller> 100" → 100). |
| Listing parser | `src/domain/listing.ts` | Template fields by label: type, `endsAt` + `endsAtText`, close rule + `softCloseMinutes`, increment, price, shipping. Each field carries `confidence: "sure" \| "unsure"` and the raw line it came from. |
| End time | `src/domain/time.ts` | Dates in `Europe/Oslo`: ISO-like (`2026-10-02 22.00`), Norwegian day and month names ("Lørdag 3. oktober 23.59"), and weekday-only ("søndag kl 20", resolved against the capture time). Anything else → unsure. |
| Lot parser | `src/domain/lots.ts` | A top-level comment with an image is a lot candidate. Reads title/condition (raw), `startBid` from `MP:` or a fixed price from `… - 1200kr`, and the per-lot increment from `MB:`. |
| Bid parser | `src/domain/bids.ts` | For each reply under a lot: the seller is never a bidder; a reply that is just an amount (optionally with a tagged name or "kr") is a bid; a correction like "200 sorry mente 250" → 250; anything else (text, "Sendt PM …", photos) is not a bid. If a reply mixes an amount with other text → unsure. |
| Status | `src/domain/status.ts` | Validity (above the current highest + increment, and ≥ the start bid), `highestBid`, `myStatus` (`none`/`lead`/`outbid`), `Ended?` / `Ended` from end time + soft close. |
| Settings | `src/pages/options/` | Your Facebook name (default "Erik Johansen"). No API key. |
| Try-it UI | `panel.ts` | After a read, an **Interpret** button. It shows each lot with highest bid, bidder, your status and any unsure items, next to the seller's original text. **Download JSON** includes the result. |

All pure functions, no network. It runs instantly, so interpretation can always be re-run on
the stored raw text when rules improve.

## Tests (offline, in CI)

- Anonymized fixtures derived from your two captures: names replaced, image URLs removed.
- A table-driven unit test per parser: amounts, dates, template lines, bid replies, including
  every real variant seen in `samples/` so far.
- An unsure-case test: text the rules don't recognize must come out unsure, not wrong.

## The tradeoff

Rules only cover what they've seen. When a seller writes something new (a different date
format, "bud: 300 + frakt"), it shows up as unsure until a rule is added. The fix is cheap:
capture the post, add the case to the tests, extend the rule. The more posts you capture, the
better it gets. The spec's "Keep raw text" rule makes this safe: raw text is always stored, so
old posts can be re-interpreted.

## Decisions for you

1. **Unsure handling.** I suggest unsure items are shown clearly (e.g. a "?" and the raw text)
   and never count towards Leading/Outbid. You'd see "Leading 2 · 1 unsure" rather than a
   wrong status.
2. **Try-it UI in the panel** (an Interpret button) rather than waiting for the table page.
3. **More samples.** Two posts is a thin base for rules. Before or during the build, capture
   3–5 more posts, ideally a few different sellers and one auction that is still running.

## Out of scope

Storage (module 2), feed scan (4), the table (5), the overlay (6).
