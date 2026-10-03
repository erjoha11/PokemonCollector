# fb_auction_watcher – UX notes

Dated findings from usability reviews of the overview (`dashboard.html`). Newest first.

## 2026-10-03 – "Do you easily see all won lots?"

Reviewed with a realistic evening-after scenario in the preview (one ended auction with 3 lots
won and 1 lost, one claim sale with 2 cards won; real feed data otherwise).

**Answer then: no.** Of 5 won lots, 2 were visible at first glance.

Findings:
1. Won auction lots were hidden in the collapsed "Ended" list: a sale moves there exactly when it
   becomes "Won".
2. No "what I won / what I owe" view: paying is per seller, and needed opening every sale, adding
   up lots, and digging out shipping/payment lines from the post text.
3. The "Won … kr" total only counted sales that hadn't ended (400 kr shown, 412 kr real).
4. One lost lot made a mostly-won sale's card orange (looked like a problem).
5. Won lots read "Your bid 1 kr · highest 1 kr" instead of what you pay.
6. Claim/fixed-price sales never end, so a won claim stayed in the active list forever.
7. The counters had no "won".
8. No way to tick off paid/received.
9. Lots without a description show as "Lot 8" (meaningless in a list of wins) — still open.

Built (branch `fbaw-won-section`): a **Won** section at the top of My Auctions, per seller, with
total, shipping/payment lines quoted from the post, links, and Paid / Received marks (stored in
`chrome.storage.local`, `wonState`); a "Won lots" counter; won lots read "You pay X kr", lost
"Sold for X kr · yours Y kr"; card tone shows what still needs you (orange), else green if anything
was won, grey if only lost; sales where everything of yours is won live under Won instead of the
active list. Checked in the preview: "4 lots · 412 kr · to pay 412 kr to 2 sellers"; ticking Paid
updates "to pay"; ticking both folds the seller away.

Still open: card names for auction lots without a description (item 9; could come from Claude
reading the lot photo); shipping cost isn't added to the total (sellers write it in free text).
