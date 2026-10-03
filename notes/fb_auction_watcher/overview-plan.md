# Overview first: plan

Status: **approved 2026-10-03.** The user wants the overview (table of every sale sorted by
end time) before the rest. This round condenses parts of modules 2, 3, 4 and 5; the module
order in `docs/spec.md` "Plan" is changed accordingly. No Claude API (see `module-3-plan.md`).

## How sales get in

Passive feed reading **while the user scrolls the group feed themselves**: every post that
renders gets stored (seller, text, link, thumbnail). No clicks, no background scanning. The
automatic 10–15 min scan (module 4) comes later.

## Pieces

| Piece | What |
|---|---|
| Feed snapshot | Toolbar icon on the group feed → "Download feed snapshot" (done first, to get a sample). |
| Feed reader | Content script on the group page; captures posts as they render. Structure-based selectors only. |
| Rules, first slice | Post text → type, end time (the five formats in `docs/spec.md`), soft close, increment. Read-post captures → lots, bids (reply target, ID order), highest bid, my status. Unknown → "unsure" + raw text. |
| Storage | IndexedDB behind a `Store` interface, raw text kept. |
| Table page | `dashboard.html` per spec "Design": counters, groups, countdowns, my status, click → post. Filters/search later. |

## Not in this round

Automatic feed scan, overlay, side panel, any LLM.

## Order

Feed snapshot → user captures → feed reader + rules + storage → table page → user tests.
