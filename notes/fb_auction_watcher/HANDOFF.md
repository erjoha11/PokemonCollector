# fb_auction_watcher handoff log

Things not fully captured by git: decisions, deferred work, and anything changed outside the code.
Newest first.

## 2026-10-06: late bids counted as wins (#329)

**Code (in git):** PR "mark a won lot as Not won" (part 1: `notWonMarks`), and PR "late bids don't
count" (parts 2 + 3: sellers' "too late" replies, bid time vs the chained antisnipe end, the
"klokken" end-time fix). Rules in `apps/fb_auction_watcher/docs/spec.md` "Late bids don't count".
No database or tcg_inventory changes; nothing was sent for the lots in question (prod `won_items`
has only 2 unrelated rows).

**Open items, deliberately not built:**
- A seller reply at the lot level (not under a bid) such as "Auksjonen er avsluttet" isn't used to
  close the lot for bids after it. Only replies under a bid (or tagging its bidder) reject that bid.
- The exact wording the seller used on lots 60/63/64 is unknown; the rules cover the phrasings
  listed in the issue plus "ugyldig", "teller ikke", "too late". Anything else under your own bid
  goes to `claude -p`. No `seller-reply` cases were added to `src/llm/cases.ts` (the opt-in eval).
- Minute-level ages ("8 min") with a ±1 min slack can't prove a bid placed inside a chained
  antisnipe extension on time to the minute: such bids count but flag the lot "check". If that's
  noisy in practice, the slack (`TIME_SLACK_MS`) is the knob.
- Retention still counts a sale's age from end + the antisnipe window, not the chained end (days
  matter there, not minutes).
