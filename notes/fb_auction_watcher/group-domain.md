# FB Auction Watcher – Domain Spec

**Group:** Pokemon-kort Norge – Kjøp/selg/bytt dine kort her (`facebook.com/groups/pokemonkortnorge`, id `512023078988607`)
**Source:** group About page, Guides (ad templates + "Forklaring av mal-punkter"), pinned admin posts, and a sample of ~27 live posts (5 Oct 2026).
**Purpose:** Describe how the group works so the extension can model and parse it correctly. The rules are human conventions, not enforced by Facebook. Treat all parsed data as *best effort*.

> **Status (2026-10-05):** background research written in a separate Claude-in-Chrome session, not the app's spec. `apps/fb_auction_watcher/docs/spec.md` and the code stay the source of truth; where this doc disagrees with them, see §11 before changing anything. Member names from the sampled posts are replaced with placeholders (`@Seller`, `Bidder A`…).

---

## 1. Overview

- Private group, ~32,100 members, all Norwegian.
- Activity: ~4,600 new posts/day and ~6,500 comments/day, covering all post types. That is far more than ~100 auctions/day, so the extension cannot index the whole group. It should focus on posts the user sees or follows.
- Every ad **must** use one of the group's templates. Each template has fixed fields in a fixed order and ends with a hashtag.
- Admins approve posts manually.

### Flow (from the extension's point of view)

```
FB feed / post DOM
   → classify post type (hashtag / header / fields)
   → parse header fields (min price, end time, antisnipe …)
   → parse lots (seller's top-level comments)
   → parse bids/claims (replies)
   → compute state + effective end time
   → overview table / side panel / overlay
```

---

## 2. Domain model

```
Post (1) ──< Lot (n) ──< Bid / Claim (n)
```

| Entity | Description |
|---|---|
| **Post** | The ad itself. Holds the header fields (type, end time, rules). |
| **Lot** | One item for sale. In single-item posts the post *is* the lot. In multi-item posts each lot is a **top-level comment by the seller** with an image, a card name and its own minimum/price. |
| **Bid** | A reply to a lot (auction). Contains a tag of the seller + an amount. |
| **Claim** | A reply to a lot (claim sale). The first valid "claim" wins. |

---

## 3. Post types

| Type | Hashtag | Header text | Buying mechanic | Watch in extension? |
|---|---|---|---|---|
| Auction | `#Auksjon` | `AUKSJON/BUDRUNDE-annonse` | Bids in replies, highest wins at end time | **Yes (core)** |
| Claim sale | `#Claimsalg` | `Claim-salg` | First "claim" per lot wins | **Yes (core)** |
| Fixed price | `#Fastpris` | `FASTPRIS-annonse` | DM deal, post edited to `SOLGT` | Yes (simple) |
| Wanted | `#Ønskeskjøpt` | `ØNSKES KJØPT-annonse` | – | Optional |
| Trade | `#Byttes` | `BYTTE-annonse` | – | Optional |
| Giveaway | `#Gisbort` | `GIS BORT-annonse` | Optional draw | Optional |

### 3.1 Templates (verbatim fields)

**Auction**
```
AUKSJON/BUDRUNDE-annonse
Minstepris:
Minimum budøkning:
Sluttid:
Antisnipe 5 min:
Objektbeskrivelse:
Tilstand:
Sender med post (pris m/emballasje):
Betalingsalternativ:
Bekreftelse: Jeg har lest og forstått reglene før jeg poster annonsen i gruppen.
#Auksjon
```

**Claim sale**
```
Claim-salg (Tagg deg selv i kommentarfeltet om du ønsker å delta)
Fastpris: Blir oppgitt over hvert bilde i kommentarfeltet
Startid: (minst 30 minutter frem i tid)
Sluttid (maks 24 timer):
Objektbeskrivelse:
Tilstand:
Sender med post (pris m/emballasje):
Betalingsalternativ:
Bekreftelse: …
#Claimsalg
```

**Fixed price**
```
FASTPRIS-annonse
Fastpris:
Objektbeskrivelse:
Tilstand:
Sender med post (pris m/emballasje):
Betalingsalternativ:
Bekreftelse: …
#Fastpris
```

**Wanted:** `Ønskes Kjøpt:`, `Maxpris:`, `Tilstand:`, `#Ønskeskjøpt`
**Trade:** `Ønsker å bytte kort:`, `Kort jeg ønsker å bytte til meg:`, `Kort jeg bytter bort:`, `Tilstand:`, `Sendes med post:`, `#Byttes`
**Giveaway:** `Objektbeskrivelse:`, `Tilstand:`, `Trekning (Ja/Nei – klokkeslett):`, `Sender med post …`, `#Gisbort`

### 3.2 Field glossary

| Field (NO) | English | Notes |
|---|---|---|
| Minstepris (MP) | Minimum price | **Binding**: seller must sell if reached. Can be per post, per card, or per lot. |
| Minimum budøkning | Minimum bid increment | At least 5 kr. |
| Sluttid | End time | Required on all auctions. Date + time. |
| Startid | Start time | Claim sales only, and optional. Sellers usually don't post the lots until then (confirmed by the user, 2026-10-06). |
| Antisnipe 5 min | Anti-sniping | `Ja`/`Nei`. See §4.2. |
| Fastpris | Fixed price | |
| Tilstand | Condition | Mint / NM / LP / MP / HP / D (TCGplayer scale). Note: **MP is also used as an abbreviation for Minstepris** in lot comments. Disambiguate by context. |
| Objektbeskrivelse | Item description | Card name, number (e.g. 13/102), set, language. |
| Sender med post | Shipping | Yes/no + price incl. packaging. |
| Betalingsalternativ | Payment | Vipps, bank transfer, PayPal, cash … |
| hbo | "høystbydende over" = highest bidder above X | e.g. `hbo 700 kr` |
| SOLGT | Sold | Edited into post/comment text when sold. |

---

## 4. Rules and lifecycle

### 4.1 Auction lifecycle

```
posted → open (bidding) → [antisnipe window] → ended → winner pays within 24 h → sold
```

- **Bids are binding.** Winner must be able to pay within 24 hours.
- Breaking a binding bid/claim → 28-day suspension; second offence → removed from group.
- **End time is strict, minute resolution, Facebook time.** End 20:00 → a bid at 19:59 is the last valid one; a bid shown at 20:00 is too late.
- Minimum increment ≥ 5 kr (seller may set higher, e.g. 10 or 50).

### 4.2 Antisnipe (when `Antisnipe 5 min: Ja`)

- A bid in the last 5 minutes moves the end time to **bid time + 5 min**. This repeats until no new bid arrives before the current end.
- Example from the rules: end 18:00, bid at 17:58 → new end 18:03. Another bid before 18:03 → extended again.
- Example 2: end 20:00, bid 19:59 → new end 20:04; the last valid minute is 20:03.
- Antisnipe applies **per lot** in multi-lot auctions (each lot has its own bid stream).
- **Consequence:** the effective end time can only be computed from the bid timestamps. The extension needs absolute comment timestamps, not just "2 t".

```
effectiveEnd(lot) =
  end = post.sluttid
  for bid in lot.bids ordered by time:
     if bid.time < end and antisnipe and bid.time >= end - 5min:
        end = bid.time + 5min
  return end
```

### 4.3 Claim sale lifecycle

```
posted (Startid ≥ 30 min ahead) → people comment "." to follow
  → seller posts first image/price → countdown to Sluttid starts (max 24 h)
  → first "claim" reply per lot wins → after end, post marked "Solgt"
```

- Claims are binding (same penalty as bids).
- "Claim" only counts in claim sales. Writing "claim" on a fixed-price ad means nothing.

### 4.4 Fixed price

- Free negotiation via DM. Sold items are edited to `SOLGT` in the post or comment.

### 4.5 Other rules worth knowing

- **No price-bashing/hijacking:** negative comments on someone's ad → 28-day suspension. The extension must never post anything automatically.
- No sale of unopened products before their release date (§14). Temporary rule: popular new sealed English products may only be sold 12 weeks after release (not for businesses).
- Businesses must register with admins: max 1 ad at a time, fixed price only, must link to their webshop.
- Middleman service ("Mellommanntjeneste") exists for safer high-value trades.
- Feedback thread + feedback list (seller/buyer reputation). Possible future feature: show seller feedback.

---

## 5. Real-world structure (what the DOM actually looks like)

### 5.1 Multi-lot auction (the most common case)

The post is only the header. Each lot is a **top-level comment by the seller**:

```
Post: "LYN AUKSJON/BUDRUNDE-annonse | Minstepris: Står under hvert bilde | Minimum budøkning: 10kr | Sluttid: ikveld 21.00 – 05/10-2026 | Antisnipe 5 min: Ja …"
 ├─ Comment (seller): "Roaring Moon ex 251/182 | MP: 100"   [image]
 │    ├─ Reply (Bidder A): "@Seller 100"
 │    ├─ Reply (Bidder B): "@Seller 150"
 │    └─ Reply (Bidder C): "@Seller …"
 ├─ Comment (seller): "Wailord 162/159 – Journey Together | MP: 40"
 │    └─ Reply (Bidder D): "@Seller 100"
 └─ Comment (seller): "Gir en like til de som blir overbydd"   ← NOT a lot
```

Parsing notes:
- **Bid text = tagged seller name + amount.** Strip the mention (link element) before parsing the number.
- The seller's own replies (often image-only, empty text) are not bids.
- Seller comments without a price/card are chatter, not lots.
- Some sellers put images in the post itself and list lots in comments; others put everything in comments.

### 5.2 Claim sale

- Many top-level comments containing only `.` (users following the post). Ignore them.
- After start: seller comments image + price per lot; replies containing "claim" are claims. First one by timestamp wins.
- The template's first line carries an instruction in brackets ("Claim-salg (Tagg deg selv i kommentarfeltet om du ønsker å delta)"); it isn't the sale's name, `Objektbeskrivelse` is.
- A lot's text, when there is one, often gives the name, price and condition on one line ("Umbreon VMAX 215/203 NM - 1200kr"); "MP - 250kr" there means Moderately Played, not minimum price.

### 5.3 DOM hooks (observed, fragile, verify before relying on them)

| Need | Hook |
|---|---|
| Feed items | `div[role=feed] > div` (virtualized: items are removed while scrolling) |
| Post text | `div[data-ad-rendering-role=story_message]` / `div[data-ad-preview=message]` |
| Post URL | `a[href*="/posts/"]`, `a[href*="/permalink/"]` |
| Comment / reply | `div[role=article]` with `aria-label` `"Kommentar fra <Name> for … siden"` (comment) or `"Svar fra <Name> på <Name> sin kommentar …"` (reply) |
| Expand truncated text | `div[role=button]` with text `Se mer` |
| Expand replies | buttons with text `Se N svar` |
| Comment sorting | Default is "Mest relevante" (most relevant), which hides comments. Switch to `Alle kommentarer` (all comments). |
| Opened post | Often rendered in `div[role=dialog]` on top of the feed |

- Class names are obfuscated and change often. Use roles, aria-labels and text instead.
- Timestamps are relative ("2 t", "for omtrent en time siden"). Absolute time requires hovering or another source. This is needed for antisnipe and claim order.
- The UI language is Norwegian for this user. Labels differ if Facebook is set to English.

---

## 6. Parsing rules and edge cases

### 6.1 Post type detection (in priority order)

1. Hashtag (`#Auksjon`, `#Claimsalg`, `#Fastpris`, …)
2. Header text (`AUKSJON`, `BUDRUNDE`, `Claim-salg`, `FASTPRIS`, `ØNSKES KJØPT`, `BYTTE`, `GIS BORT`)
3. Field presence (`Sluttid` + `Minstepris` → auction, `Startid` → claim sale)

Observed header variants: `LYN AUKSJON/BUDRUNDE` (flash auction, short end time), `Mega Lyn`, `VINTAGE AUKSJON/BUDRUNDE`, `AUKSJON 30th Celebrations Kort`, `AUKSJON/BUDRUNDE` without "-annonse", posts starting with the card name before the template. Some posts have no hashtag.

### 6.2 End time (`Sluttid`) – real examples

| Raw text | Interpretation |
|---|---|
| `idag 5/10 kl. 21.00` | today, 5 Oct, 21:00 |
| `ikveld 21.00 – 05/10-2026` | tonight, 21:00 |
| `Tirsdag klokken 23, 6 oktob` | Tue 6 Oct, 23:00 (truncated month) |
| `Onsdag 07.10 kl 22:00` | Wed 7 Oct, 22:00 |
| `Fredag 7. august kl. 21:00` | Fri 7 Aug, 21:00 |
| `Mandag 5/10 kl. 21:00` | Mon 5 Oct, 21:00 |

Rules:
- Keywords: `idag`/`i dag` (today), `ikveld`/`i kveld` (tonight), `imorgen`/`i morgen` (tomorrow), weekday names `mandag … søndag`.
- Date formats: `5/10`, `05/10-2026`, `07.10`, `7. august`, `6 oktob`. Year is usually missing → assume the next occurrence after the post time.
- Time formats: `21:00`, `21.00`, `kl 22`, `klokken 23`.
- Relative words are relative to **post time**, not now.
- If weekday and date disagree, trust the date and flag it.
- Timezone: Europe/Oslo.
- Always store `confidence` (high/medium/low) and the raw text. Show low-confidence values clearly in the UI.

### 6.3 Minimum price (`Minstepris`) variants

| Raw | Meaning |
|---|---|
| `4000kr`, `17 000kr` | one amount (handle spaces as thousands separator) |
| `20kr pr kort, om ikke annet er oppgitt i kommentarfeltet` | default per lot, overridden per lot |
| `Står under hvert bilde` / `under hvert bilde i kommentarfeltet` | per lot, read from lot comment |
| `Alle kort starter på 5kr` | default per lot |
| `hbo 700 kr` | highest bid above 700 |
| `høystbydende over 0kr per kort` | no minimum |
| `1kr!!!` | 1 kr |

Lot-level minimum: `MP: 20`, `MP 100`, `MP:100`.

### 6.4 Bid parsing

- Amount = the last number in the reply after removing the mention. Accept `150`, `150kr`, `150,-`, `1 500`.
- Validate: amount ≥ lot minimum, and ≥ previous highest + increment. Mark invalid bids instead of dropping them.
- Ignore the seller's own replies.
- Bids are also edited or deleted sometimes. Re-parse on each visit.

### 6.5 Sold detection

- `SOLGT` / `Solgt` in post text or lot comment.
- For auctions: past effective end time → "ended" (winner = highest valid bid).

---

## 7. Suggested state per lot

| State | Condition |
|---|---|
| `scheduled` | Claim sale before Startid |
| `open` | Now < effective end |
| `ending_soon` | < 15 min left (configurable) |
| `antisnipe` | Extended past original Sluttid |
| `ended` | Now ≥ effective end |
| `sold` | `SOLGT` found |
| `unknown` | End time could not be parsed |

Useful per-lot data for the UI: current highest bid, bidder, user's own bid, "outbid" flag, time left, seller, link.

---

## 8. Known limits and risks

- **Best effort:** the group's rules are conventions. Many posts deviate from the template. Always show raw text + confidence.
- **Fragile DOM:** Facebook changes markup often. Keep selectors in one module so they're easy to update.
- **Visibility:** comments are hidden by default (relevance sorting, folded replies). Data is only as complete as what has been expanded.
- **Timestamps:** relative times are too coarse for antisnipe. Absolute times need extra work.
- **Facebook terms:** automated scraping is against Facebook's terms. Keep the extension read-only, reading pages the user opens. Never auto-bid or auto-comment (also breaks the group's rules).
- **Volume:** thousands of posts per day. Don't attempt full indexing.

---

## 9. Test fixtures (from real posts, 5 Oct 2026)

```
AUKSJON/BUDRUNDE-annonse
Minstepris: 20kr pr kort, om ikke annet er oppgitt i kommentarfeltet
Minimum budøkning: 10
Sluttid: idag 5/10 kl. 21.00
Antisnipe 5 min: ja
Objektbeskrivelse: hits fra forskjellige set
Tilstand: m/nm
Sender med post (pris m/emballasje): Sender mot porto
Betalingsalternativ: vipps
#Auksjon
```

```
LYN AUKSJON/BUDRUNDE-annonse
Minstepris: Står under hvert bilde.
Minimum budøkning: 10kr
Sluttid: ikveld 21.00 – 05/10-2026
Antisnipe 5 min: Ja
Objektbeskrivelse: Diverse IR/ SIR
Tilstand: NM/M
Sender med post: 46kr brevpost eller 76kr for pakke med sporing.
Betalingsalternativ: Vipps
#Auksjon
```

```
30th Celebration Lugia selges
Minstepris: hbo 700 kr
Minimum budøkning: 10kr
```

```
AUKSJON/BUDRUNDE-annonse
Minstepris: 4000kr
Minimum budøkning: 50kr
Sluttid: Tirsdag klokken 23, 6 oktob…
```

```
AUKSJON/BUDRUNDE
Minstepris: 17 000kr
Minimum budøkning: 10 kr
Sluttid: Onsdag 07.10 kl 22:00
Antisnipe …
```

```
Claim-salg
(Tagg deg selv i kommentarfeltet om du ønsker å delta)
Fastpris: Blir oppgitt over hvert bilde i kommentarfeltet
Startid: Mandag 5/10 kl. 21:00
Sluttid: Tirsdag 6/10 kl. 21:00
Objektbeskrivelse: Moderne, psa slab, graded guards og sealed booster box og packs
Tilstand: Alle kort er NM, psa 9 og 10 slab, booster box fin tilstand
```

Lot comments: `Iron Jugulis 216/182 – Illustration Rare | MP: 20`, `Morpeko 206/182 | MP 100`
Bid replies: `@Seller 100`, `@Seller 150` (the seller's full name as a mention, then the amount)

---

## 10. Open items

- The full rulebook ("REGELVERKET" guide) could not be loaded. Check it for extra rules (e.g. maximum auction length, rules for retracting bids).
- Confirm how sellers mark auction winners (e.g. `SOLGT` on the lot comment or a reply).
- Decide how to get absolute comment timestamps.

---

## 11. Compared with the code (2026-10-05, `main` at 97728ed)

- **Antisnipe is not chained in the code.** §4.2 says every bid in the last 5 min moves the end to bid time + 5 min, repeatedly. The code closes a sale at a fixed `endsAt + softCloseMinutes` (`src/background/watch.ts:81`, `src/background/index.ts:188`, `src/pages/dashboard/model.ts:92`). A bidding war running past that point would be shown as ended and get its final read too early, possibly with the wrong winner. Worth a ticket.
- **Type detection order differs, and the code is right.** §6.1 puts the hashtag first; `src/domain/listing.ts` deliberately reads the headline first, because sellers get the hashtag wrong more often (e.g. "Claimsalg" with `#Fastpris`). Keep the code's order.
- **End-time examples (§6.2) run through `parseEndTime`:** 5 of 6 parse correctly. `Tirsdag klokken 23, 6 oktober` gives no end time: the rules know `kl`/`kl.` but not `klokken` (`findTime` in `src/domain/endTime.ts`), so such posts depend on the `claude -p` fallback. Small fix: accept `klokken` too.
- **Lot-level minimum (§6.4, "MP: 20" etc.) was read more narrowly than written** (fixed 2026-10-07, #352). The code stripped "Pris", "Start", "Mp." and a mid-line "200kr" out of a lot's name but read the start bid only from "MP"/"Minstepris"/"Startbud" or a line holding only an amount, so lots written as "Lot 1 - Pris: 200kr" showed "Lot N" and no start bid. Both now share one label list in `src/domain/bids.ts`; see `docs/spec.md` "Lot texts that are only a number and a price".
- **"MP" is now disambiguated by an explicit rule** (2026-10-07, #352), per §3.2 ("disambiguate by context") and §5.2: after "Tilstand:"/"Condition:" or written "moderately played" it's the condition; in a claim or fixed-price lot always the condition; followed by an amount it's the start bid; bare (no amount) it's the condition only when no other condition is given. Free-text lot comments (name, condition and price in any order) are parsed by `lotTextInfo` in `src/domain/bids.ts`; see `docs/spec.md` "Free-text lot comments" and "MP: start bid or condition".
