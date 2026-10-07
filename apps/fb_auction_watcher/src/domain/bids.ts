import type { CapturedComment, CapturedReply, PostCapture } from "../shared/capture";
import { parseAmount } from "./amount";
import { saleLines } from "./saleLines";

// Lots and bids from a post read with the toolbar icon (module 1's capture), by rules, with
// optional answers from Claude for replies the rules can't read. Findings this relies on are in
// docs/spec.md: a lot is a top-level comment with an image from the seller; bids are replies;
// reply IDs increase with time (Facebook shows them out of order); a reply's aria-label says
// whether it answers the lot ("… sin kommentar") or another reply ("… sitt svar"); the seller's
// own replies are never bids; most bids are "<Seller> 250", "250kr" or a bare number.

export type BidReading =
  | { kind: "bid"; amount: number }
  /** Probably a bid, but the rules aren't sure (e.g. "580?", or a number in other text). */
  | { kind: "unsure"; amount: number | null }
  | { kind: "none" };

export type Bid = {
  replyId: string | null;
  bidder: string;
  amount: number | null;
  rawText: string;
  timeText: string | null;
  isMe: boolean;
  /** Counts toward the highest bid. */
  valid: boolean;
  /** Why it doesn't count, or what to double-check. */
  note: string | null;
  /** Placed as a reply to another reply, not to the lot itself; sellers may not accept it. */
  underReply: boolean;
  /** The amount came from Claude, not the rules. */
  viaClaude: boolean;
};

/**
 * Your status on an auction lot. "unclear": you'd be leading, but a reply the rules couldn't
 * read (or one without an ID, so its order is unknown) may be a higher bid placed after yours.
 */
export type MyStatus = "none" | "lead" | "outbid" | "unclear";

/** A claim on a claim-sale or fixed-price lot: "claim Persian og Clefairy", "<Seller> marowak". */
export type Claim = {
  replyId: string | null;
  claimer: string;
  rawText: string;
  /** Item names claimed, lower case ("persian", "clefairy"); empty = the lot as a whole. */
  items: string[];
  /** "alle" / "all": everything in the photo. */
  all: boolean;
  isMe: boolean;
  /** Someone else claimed the same thing (or everything) before this claim. */
  contested: boolean;
  underReply: boolean;
};

/** Your claim on a lot: first on what you named (you won it), someone was earlier, or no claim. */
export type MyClaim = "none" | "claimed" | "check";

/** A claim lot's cards: from Claude's reading of the photo, who got each by the rules (or Claude on the text), null = for sale. */
export type ClaimCards = { card: string; price: number | null; claimedBy: string | null; isMe: boolean }[];

/** Claude's reading of a lot photo: the cards in it and their prices (null = can't tell). Read once per photo; no names. */
export type PhotoCards = { card: string; price: number | null }[];

/**
 * Claims the rules couldn't match to the photo's cards, for Claude (text only). No names: you're
 * "Me", the others "Claimer 1", "Claimer 2"… in the order they first claimed; the seller's tag is "@Seller".
 */
export type ClaimMatchInput = { cards: string[]; claims: { who: string; text: string }[] };
/** Who got each card ("Me", "Claimer N"), in the order of `cards`; null = still for sale. */
export type ClaimMatchAnswer = (string | null)[];

export type Lot = {
  /** The post itself is the lot (no lot comments; bids right under the post). */
  wholePost?: boolean;
  commentId: string | null;
  position: number;
  title: string;
  rawText: string;
  imageUrl: string | null;
  startBid: number | null;
  increment: number | null;
  bids: Bid[];
  highestBid: number | null;
  highestBidder: string | null;
  myHighestBid: number | null;
  myStatus: MyStatus;
  /** Replies that may be bids but couldn't be read (shown with "?", sent to Claude). */
  unsureCount: number;
  /** No bid reached the start bid; the highest is shown anyway (sellers sometimes accept it). */
  belowStart: boolean;
  /** Claim-sale and fixed-price lots: claims instead of bids. */
  claims: Claim[];
  myClaim: MyClaim;
  /** Every card in the photo, its price, and who got it (null = still for sale); null until the photo is read and the claims matched. */
  claimCards: ClaimCards | null;
  /** The photo is read but the rules couldn't match the claims to its cards: this goes to Claude (text only). */
  claimMatchInput?: ClaimMatchInput | null;
  /** Cards still for sale (from claimCards), or null when Claude hasn't read the lot yet. */
  available: number | null;
  /**
   * Claim/fixed-price lots: the price written in the lot's own text ("5kr per stk", "NM - 1200kr"),
   * per card when it says so (`perCard`). Null when the text has none (it's on the photo, if anywhere).
   */
  textPrice: { kr: number; perCard: boolean } | null;
  /** The lot's text didn't name it (only a price, or nothing): `title` is "Lot N" or Claude's name for the photo. */
  untitled: boolean;
  /** `title` came from Claude reading the photo. */
  namedByClaude: boolean;
};

export const normalizeName = (s: string | null | undefined) =>
  (s ?? "").normalize("NFC").replace(/\s+/g, " ").trim().toLowerCase();

/** Removes a leading tag of the seller ("Ola Nordmann 250" → "250"); also the first name alone. */
function stripSellerTag(text: string, seller: string | null): string {
  let t = text.trim();
  if (!seller) return t;
  const full = seller.trim();
  const first = full.split(/\s+/)[0];
  for (const name of [full, first]) {
    if (name && t.toLowerCase().startsWith(name.toLowerCase())) {
      t = t.slice(name.length).replace(/^[\s,:;-]+/, "");
      break;
    }
  }
  return t;
}

const AMOUNT = String.raw`\d{1,3}(?:[ .]\d{3})+|\d+(?:[.,]\d+)?\s*k|\d+`;
const PLAIN_BID = new RegExp(String.raw`^(?:bud\s*:?\s*)?(${AMOUNT})\s*(?:kr\.?|,-|nok|kroner)?\s*[!.]*$`, "i");
const QUESTION_BID = new RegExp(String.raw`^(?:bud\s*:?\s*)?(${AMOUNT})\s*(?:kr\.?|,-|nok|kroner)?\s*\?+$`, "i");

/** Reads one reply's text as a bid, by rules only. */
export function readBid(text: string, seller: string | null): BidReading {
  const t = stripSellerTag(text, seller);
  if (!t || /^[.\s]+$/.test(t)) return { kind: "none" };
  const plain = t.match(PLAIN_BID);
  if (plain) {
    const amount = parseAmount(plain[1]);
    return amount !== null ? { kind: "bid", amount } : { kind: "none" };
  }
  const question = t.match(QUESTION_BID);
  if (question) return { kind: "unsure", amount: parseAmount(question[1]) };
  return /\d/.test(t) ? { kind: "unsure", amount: null } : { kind: "none" };
}

/** Reply IDs are large numbers that increase with time; compare without losing precision. No ID sorts last. */
function compareIds(a: string | null, b: string | null): number {
  if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
  if (a.length !== b.length) return a.length - b.length;
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * A claim/fixed-price lot's price from its own text (sellers who write "Fastpris: Blir oppgitt
 * over hvert bilde" put it there): "Holo/rev.holo 5kr per stk", "EX/V/IR10kr per stk",
 * "10 kr pr kort" (per card), or "NM - 1200kr" / "200kr" (the lot, usually one card).
 */
export function lotTextPrice(text: string): { kr: number; perCard: boolean } | null {
  const perCard = text.match(/(?<!\d)(\d[\d .]*?)\s*(?:kr|,-)\.?\s*(?:per|pr\.?|\/)\s*(?:stk|stykk|kort|card)/i);
  if (perCard) {
    const kr = parseAmount(perCard[1]);
    if (kr !== null) return { kr, perCard: true };
  }
  const amount = text.match(/(?<!\d)(\d[\d .]*?)\s*(?:kr|,-)(?![\p{L}])/iu);
  const kr = amount ? parseAmount(amount[1]) : null;
  return kr !== null ? { kr, perCard: false } : null;
}

// A lot's start bid (minimum price) and bid step, as sellers label them. `lotStartBid` and
// `lotTextInfo` (via PRICE_PARTS) share these, so a price the title leaves out is always the one
// read as the start bid (#352: "Pris: 200", "Mp. 200", "Startpris 200kr" were taken out of the
// name but never read as a price, so the lot showed "Lot N" and no start bid).
// "Min. bud" / "Minimum budøkning" is the group's bid step; "Minstebud" is the minimum bid.
const START_LABEL = String.raw`m\.?p|minstepris|minimumspris|min(?:imum)?\.?\s*pris|start\s*(?:pris|bud)|minstebud|start|fastpris|pris`;
const STEP_LABEL = String.raw`mb|min(?:imum)?\.?\s*bud(?:økning)?|budøkning`;
/** "Lot 1:", "Nr. 2 -", "#3" at the start of a line: the lot's number, not a name or a price. */
const LOT_NUMBER = /^\s*(?:lot|nr\.?|#)\s*\d+\s*[:.)\-–]?\s*/i;
const REFERENCE_PRICE = /(?<![\p{L}])(?:verdi|markeds?(?:pris|verdi)|market|tcg\s*player|cardmarket|pricecharting|solgt\s+for)/iu;

/**
 * "MP: 1400", "Mp 10kr", "Mp. 200", "Holo, mp 30kr", "Minstepris 500", "Startpris: 200kr",
 * "Pris 150,-", else an amount in kr anywhere ("Charizard 4/102 - 200kr", "700kr" on its own
 * line) that isn't the bid step ("MB 10kr"). A bare number with no label and no "kr" is not
 * read: it may be a card number.
 */
export function lotStartBid(text: string): number | null {
  const lines = text.split("\n").map((l) => l.replace(LOT_NUMBER, ""));
  const labelled = new RegExp(String.raw`(?<![\p{L}\d])(?:${START_LABEL})[ \t]*[:.=]?[ \t]*(?:kr\.?[ \t]*)?(\d[^\n]*)`, "iu");
  for (const line of lines) {
    const m = line.match(labelled);
    const kr = m ? parseAmount(m[1]) : null;
    if (kr !== null) return kr;
  }
  const step = new RegExp(String.raw`(?<![\p{L}\d])(?:${STEP_LABEL})[ \t]*[:.=]?[ \t]*\d[\d .,]*(?:[ \t]*(?:kr\.?|,-|nok))?`, "giu");
  for (const line of lines) {
    if (REFERENCE_PRICE.test(line)) continue; // "Markedspris 900kr", "TCGplayer $40": what it's worth, not the start bid.
    const m = line.replace(step, " ").match(/(?<![\p{L}\d/.,])(\d{1,3}(?:[ .]\d{3})+|\d+)\s*(?:kr\.?|,-|nok)(?![\p{L}])/iu);
    const kr = m ? parseAmount(m[1]) : null;
    if (kr !== null) return kr;
  }
  return null;
}

/** The lot's title: the name in its own text, else Claude's name (from the text and photo), else "Lot N"; then the condition. */
function lotTitleFor(c: CapturedComment, position: number, options: LotOptions, photoCards?: PhotoCards): { title: string; untitled: boolean; namedByClaude: boolean } {
  const { name, condition } = lotTextInfo(c.text);
  const photo = c.images[0]?.src;
  // A claim lot's photo read lists its cards: they name it ("Marowak, Kingler", "8 cards").
  const fromCards = photoCards?.length ? (photoCards.length <= 3 ? photoCards.map((x) => x.card).join(", ") : `${photoCards.length} cards`) : undefined;
  const fromClaude = !name && photo ? (fromCards ?? options.lotName?.(fullSizePhoto(photo), c.text)) : undefined;
  const title = name ?? fromClaude ?? `Lot ${position}`;
  return { title: condition ? `${title} · ${condition}` : title, untitled: !name, namedByClaude: !name && !!fromClaude };
}

/** Lots whose text doesn't name them but which have a photo: Claude names them from the text and the photo. */
export function untitledLotPhotos(capture: PostCapture): { imageUrl: string; text: string }[] {
  const seller = sellerOf(capture);
  return capture.comments
    .filter((c) => isLot(c, seller) && c.images[0]?.src && !lotTextInfo(c.text).name)
    .map((c) => ({ imageUrl: fullSizePhoto(c.images[0].src), text: c.text }));
}

// What a lot's text says besides its name (2026-10-06): prices and bid steps ("MP: 20", "mp 30kr",
// "MB 10", "Startbud 100", "200kr", "5kr per stk"), the condition ("NM", "M/NM", "LP", "PSA 10"),
// and words that describe but don't name it ("Holo", "Rev holo", "Promo"). "MP" is the group's
// minimum price, not Moderately Played, unless it follows "Tilstand:".
const AMOUNT_TEXT = String.raw`\d[\d .,]*(?:\s*k\b)?\s*(?:kr\.?|,-|nok)?`;
const PRICE_PARTS = new RegExp(
  String.raw`(?<![\p{L}\d])(?:${START_LABEL}|${STEP_LABEL})\s*[:.=]?\s*(?:kr\.?\s*)?${AMOUNT_TEXT}` +
    String.raw`|(?<![\p{L}\d/])\d[\d .,]*\s*(?:kr\.?|,-|nok)(?:\s*(?:per|pr\.?|/)\s*(?:stk|stykk|kort|card)\.?)?(?![\p{L}])`,
  "giu",
);
const CONDITION = /(?<![\p{L}\d])(?:(?:psa|cgc|bgs|tag|beckett)\s*\d{1,2}(?:[.,]5)?|m\/nm|nm\/m|mint|nm|lp|hp|dmg|damaged|tilstand\s*:?\s*(?:mp|m|ex|gd))(?![\p{L}\d])/giu;
const GENERIC = /^(?:(?:rev(?:erse)?|reverse|rev\.?)?\s*\.?\s*holo|holo|promo|lot|kort|card|cards|stk|bulk|div(?:erse)?|og|and|[&+/,.()\-–|:\s])*$/iu;

/** A lot's text, read by rules: the name it gives (null if none: only a price, generic words, or nothing) and the condition. */
export function lotTextInfo(text: string): { name: string | null; condition: string | null } {
  let name: string | null = null;
  let condition: string | null = null;
  for (const raw of text.split("\n")) {
    let line = raw.replace(LOT_NUMBER, ""); // "Lot 1: Charizard" → "Charizard".
    line = line.replace(PRICE_PARTS, " ");
    line = line.replace(CONDITION, (m) => {
      condition ??= m.replace(/^tilstand\s*:?\s*/i, "").replace(/\s+/g, " ").toUpperCase().replace(/^(PSA|CGC|BGS|TAG|BECKETT)\s*/, "$1 ");
      return " ";
    });
    line = line
      .replace(/\(\s*\)/g, " ")
      .replace(/\s*([|,\-–:])(?:\s*[|,\-–:])+/g, " $1")
      .replace(/\s+/g, " ")
      .replace(/^[\s|,\-–:.]+|[\s|,\-–:]+$/g, "")
      .trim();
    // A condition left on its own once the price is out ("MP - 250kr", "LP+"): not a name.
    if (/^(?:mp|lp|nm|ex|gd|hp)\+?$/i.test(line)) {
      condition ??= line.toUpperCase();
      continue;
    }
    if (!name && /\p{L}/u.test(line) && !GENERIC.test(line)) name = line;
  }
  return { name, condition };
}

/** "MB: 10", also mid-line ("Holo, mp 30kr, mb 20"): the minimum increment for this lot. */
function lotIncrement(text: string): number | null {
  const m = text.match(/(?<![\p{L}\d])(?:mb|min(?:imum)?\.?\s*bud(?:økning)?)\s*:?\s*(\d[^\n]*)/iu);
  return m ? parseAmount(m[1]) : null;
}

export type LotOptions = {
  myName: string;
  /** Claim sales and fixed-price posts: replies are claims, not bids. */
  claims?: boolean;
  /** From the post: used when a lot doesn't state its own. */
  listingIncrement: number | null;
  listingMinPrice: number | null;
  /** Claude's name for a lot (its full-size photo URL and its text), when the lot's text doesn't name it. */
  lotName?: (imageUrl: string, text: string) => string | null | undefined;
  /** Claude's reading of a claim lot's photo (see claimPhotoInput), or undefined when not read yet. */
  claimPhoto?: (input: ClaimPhotoInput) => PhotoCards | undefined;
  /** Claude's matching of claims the rules couldn't match (see ClaimMatchInput), or undefined when not asked yet. */
  claimMatch?: (input: ClaimMatchInput) => ClaimMatchAnswer | undefined;
  /** Claude's reading of a reply the rules weren't sure about: an amount, null (not a bid), or undefined (not asked yet). */
  answer?: (seller: string | null, text: string) => number | null | undefined;
};

function isLot(c: CapturedComment, seller: string | null): boolean {
  if (!c.hasImage) return false;
  return !seller || normalizeName(c.author) === normalizeName(seller);
}

/**
 * The seller: the post's author, unless nobody by that name posted an image comment (e.g. a
 * capture taken before the author fix named the group). Then whoever posted most of them.
 */
export function sellerOf(capture: PostCapture): string | null {
  const author = capture.post.author;
  const withImage = capture.comments.filter((c) => c.hasImage && c.author);
  if (!author || withImage.some((c) => normalizeName(c.author) === normalizeName(author))) return author;
  const counts = new Map<string, number>();
  for (const c of withImage) counts.set(c.author!, (counts.get(c.author!) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1])[0]?.[0] ?? author;
}

/**
 * An auction whose post is itself the one lot (2026-10-05, e.g. "LYNAUKSJON… Div pokemon kort
 * (bulk)… Minstepris: 200kr"): no lot comments from the seller, and the bids written as comments
 * directly under the post. Mirrors a lot comment: the post is the lot, its comments are the
 * replies to it, and a reply to someone's comment is "under another reply". Null when the post has
 * no comments from anyone but the seller.
 */
function postAsLot(capture: PostCapture, seller: string | null): { lot: CapturedComment; title: string; replies: CapturedReply[]; underReply: Set<string | null> } | null {
  const others = capture.comments.filter((c) => !seller || normalizeName(c.author) !== normalizeName(seller));
  if (others.length === 0) return null;
  const underReply = new Set<string | null>();
  const replies: CapturedReply[] = [];
  for (const c of capture.comments) {
    replies.push(c);
    for (const r of c.replies) {
      replies.push(r);
      underReply.add(r.id);
    }
  }
  const lines = capture.post.text.split("\n").map((l) => l.trim()).filter(Boolean);
  const title = saleLines(lines[0] ?? "", lines[1] ?? null).title || "The post";
  const lot: CapturedComment = {
    id: null, url: null, author: capture.post.author, text: capture.post.text, timeText: capture.post.timeText, ariaLabel: null,
    images: capture.post.images, truncated: capture.post.truncated, rawText: capture.post.text, index: -1, hasImage: capture.post.images.length > 0, replies: [],
  };
  return { lot, title, replies: replies.sort((a, b) => compareIds(a.id, b.id)), underReply };
}

export function interpretLots(capture: PostCapture, options: LotOptions): Lot[] {
  const seller = sellerOf(capture);
  const me = normalizeName(options.myName);
  const lots: Lot[] = [];
  const lotComments = capture.comments.filter((c) => isLot(c, seller));
  // No lot comments in an auction: the post itself may be the lot, with bids right under it.
  const whole = !options.claims && lotComments.length === 0 ? postAsLot(capture, seller) : null;
  for (const c of whole ? [whole.lot] : lotComments) {
    const startBid = lotStartBid(c.text) ?? options.listingMinPrice;
    const increment = lotIncrement(c.text) ?? options.listingIncrement;
    const replies = whole ? whole.replies : [...c.replies].sort((a, b) => compareIds(a.id, b.id));
    const bids: Bid[] = [];
    let unsureCount = 0;
    /** Replies that may be bids but couldn't be counted: their IDs (null = order unknown). */
    const doubtful: (string | null)[] = [];
    const claims = options.claims ? readClaims(replies, seller, me) : [];
    const mineClaims = claims.filter((x) => x.isMe);
    let myClaim: MyClaim = mineClaims.length === 0 ? "none" : mineClaims.some((x) => !x.contested) ? "claimed" : "check";
    // Claude has read the photo (which cards, what they cost): the rules decide who got what, and
    // Claude (text only, no names) only when they can't match a claim to a card.
    let claimCards: ClaimCards | null = null;
    let claimMatchInput: ClaimMatchInput | null = null;
    const photoInput = options.claims ? claimPhotoInput(c) : null;
    const photoCards = photoInput ? options.claimPhoto?.(photoInput) : undefined;
    const textPrice = options.claims ? lotTextPrice(c.text) : null;
    if (photoCards) {
      let owners = matchClaims(photoCards, claims);
      if (!owners) {
        claimMatchInput = toClaimMatchInput(photoCards, claims, seller);
        const answer = options.claimMatch?.(claimMatchInput);
        if (answer) owners = fromClaimMatchAnswer(answer, photoCards.length, claims);
      }
      if (owners) {
        // A card whose price Claude couldn't read from the photo takes the lot's per-card text price.
        const fallback = textPrice?.perCard || photoCards.length === 1 ? (textPrice?.kr ?? null) : null;
        claimCards = photoCards.map((x, k) => ({
          card: x.card,
          price: x.price ?? fallback,
          claimedBy: owners[k],
          isMe: !!me && !!owners[k] && normalizeName(owners[k]) === me,
        }));
        if (mineClaims.length > 0) myClaim = claimCards.some((x) => x.isMe) ? "claimed" : "check";
      }
    }
    for (const r of options.claims ? [] : replies) {
      const bid = toBid(r, seller, me, options, !!whole && whole.underReply.has(r.id));
      if (bid === "unsure") {
        unsureCount++;
        if (normalizeName(r.author) !== me) doubtful.push(r.id);
      } else if (bid) {
        if (bid.replyId === null && !bid.isMe) doubtful.push(null); // Can't tell if it came before or after yours.
        bids.push(bid);
      }
    }
    // Validity, in the order bids were placed: on the lot itself (sellers reject bids placed
    // under another reply: "bud blir bare godtatt under hovedbildet"), at least the start bid,
    // and at least the current highest plus the increment (above it when none is stated).
    let highest: Bid | null = null;
    for (const b of bids) {
      if (b.amount === null) continue;
      if (b.underReply) {
        b.valid = false;
      } else if (startBid !== null && b.amount < startBid) {
        b.valid = false;
        b.note = `below the start bid (${startBid})`;
      } else if (highest && b.amount < (highest.amount ?? 0) + (increment ?? 1)) {
        b.valid = false;
        b.note = increment ? `less than ${increment} over the highest bid` : "not over the highest bid";
      } else {
        highest = b;
      }
    }
    // Nobody reached the start bid: take the best of the rest, flagged, since sellers sometimes
    // accept it ("den er grei"). Same order and increment rule, without the start bid.
    const belowStart = !highest && bids.some((b) => b.amount !== null && !b.underReply);
    if (belowStart) {
      for (const b of bids) {
        if (b.amount === null || b.underReply) continue;
        if (!highest || b.amount >= (highest.amount ?? 0) + (increment ?? 1)) {
          highest = b;
          b.valid = true;
          b.note = `below the start bid (${startBid})`;
        }
      }
    }
    const mine = bids.filter((b) => b.isMe && b.amount !== null);
    const myHighestBid = mine.length ? Math.max(...mine.map((b) => b.amount!)) : null;
    lots.push({
      commentId: c.id,
      position: lots.length + 1,
      ...(whole ? { title: whole.title, untitled: false, namedByClaude: false } : lotTitleFor(c, lots.length + 1, options, photoCards)),
      wholePost: !!whole,
      rawText: c.text,
      imageUrl: c.images[0]?.src ?? null,
      startBid,
      increment,
      bids,
      highestBid: highest?.amount ?? null,
      highestBidder: highest?.bidder ?? null,
      myHighestBid,
      myStatus: myStatusFor(mine, highest, doubtful),
      unsureCount,
      belowStart,
      claims,
      myClaim,
      claimCards,
      claimMatchInput,
      available: claimCards ? claimCards.filter((x) => !x.claimedBy).length : null,
      textPrice,
    });
  }
  return lots;
}

const CLAIM_WORDS = /^(?:claim(?:er)?|clame|claimer|tar|kjøper|vil\s+ha|ønsker)\b[\s:,-]*/i;

/** The items a claim names: "claim Persian og Clefairy" → ["persian", "clefairy"]; "alle" → all. */
export function claimItems(text: string, seller: string | null): { items: string[]; all: boolean } | null {
  let t = stripSellerTag(text, seller).trim();
  if (!t || /^[.\s]+$/.test(t)) return null; // "." = following, not a claim.
  if (/\?\s*$/.test(t)) return null; // A question.
  t = t.replace(CLAIM_WORDS, "").replace(/[.!]+$/, "").trim();
  if (/^(alle|all|hele|everything)\b/i.test(t) || t === "") return { items: [], all: /^(alle|all|hele|everything)\b/i.test(t) };
  const items = t
    .split(/\s*(?:,|&|\+|\/|\bog\b|\band\b)\s*/i)
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  return { items, all: false };
}

/** What Claude reads a claim lot's photo from: the full-size photo and the seller's text with it (prices are often there). No replies, no names. */
export type ClaimPhotoInput = { imageUrl: string; lotText: string };

/** The full-size version of a Facebook CDN photo: the `ctp` parameter asks for a small crop. */
export const fullSizePhoto = (url: string) => url.replace(/([?&])ctp=[^&]*&?/, "$1").replace(/[?&]$/, "");

export function claimPhotoInput(c: CapturedComment): ClaimPhotoInput | null {
  const photo = c.images[0]?.src;
  return photo ? { imageUrl: fullSizePhoto(photo), lotText: c.text } : null;
}

/** Every lot photo in a claim sale, the ones you claimed on first: read once each, for what's taken and what's still for sale. */
export function claimLotsToRead(capture: PostCapture, myName: string): ClaimPhotoInput[] {
  const seller = sellerOf(capture);
  const me = normalizeName(myName);
  const lots = capture.comments.filter((c) => isLot(c, seller));
  const mine = (c: CapturedComment) => !!me && c.replies.some((r) => normalizeName(r.author) === me && claimItems(r.text, seller));
  return [...lots.filter(mine), ...lots.filter((c) => !mine(c))].flatMap((c) => claimPhotoInput(c) ?? []);
}

/** Card and claim names compared loosely: no accents, case or punctuation. */
const looseName = (s: string) =>
  s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^\p{L}\d ]+/gu, " ").replace(/\s+/g, " ").trim();

/** Letters to change to turn one word into the other (for misspellings: "feraligator" → "feraligatr"). */
function editDistance(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = row;
  }
  return prev[b.length];
}

/** Does a claimed item name this card? "marowak" → "Marowak", "the zard"… no; "feraligator" → "Feraligatr" (two letters off at most, long words only). */
export function namesCard(card: string, item: string): boolean {
  const c = looseName(card);
  const i = looseName(item);
  if (i.length < 3) return false;
  if (c.includes(i) || i.includes(c)) return true;
  const words = c.split(" ");
  return i.split(" ").every((w) => words.some((x) => x === w || (w.length >= 5 && x.length >= 5 && editDistance(x, w) <= 2)));
}

/**
 * Who got each card, by the rules: claims in the order placed (not those under another reply),
 * first claim on a card wins, "alle" takes everything still free, a claim naming nothing takes a
 * one-card lot. Null when a claim can't be matched for sure (names no card, or two different
 * cards): then Claude matches them from the text.
 */
export function matchClaims(cards: PhotoCards, claims: Claim[]): (string | null)[] | null {
  const owners: (string | null)[] = cards.map(() => null);
  for (const cl of claims) {
    if (cl.underReply) continue;
    if (cl.all || (cl.items.length === 0 && cards.length === 1)) {
      cards.forEach((_, k) => (owners[k] ??= cl.claimer));
      continue;
    }
    if (cl.items.length === 0) return null;
    for (const item of cl.items) {
      const hits = cards.map((_, k) => k).filter((k) => namesCard(cards[k].card, item));
      // Copies of the same card are interchangeable; a claim that fits two different cards isn't sure.
      if (hits.length === 0 || new Set(hits.map((k) => looseName(cards[k].card))).size > 1) return null;
      const free = hits.find((k) => owners[k] === null);
      if (free !== undefined) owners[free] = cl.claimer;
    }
  }
  return owners;
}

/** Claimers' labels for Claude, in the order they first claimed: you're "Me", the others "Claimer 1", "Claimer 2"… */
function claimLabels(claims: Claim[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const cl of claims) {
    const name = normalizeName(cl.claimer);
    if (!labels.has(name)) labels.set(name, cl.isMe ? "Me" : `Claimer ${[...labels.values()].filter((l) => l !== "Me").length + 1}`);
  }
  return labels;
}

/** The seller's name in a reply, as "@Seller": Claude needs to know it's the tag, not who it is. */
export function tagAsSeller(text: string, seller: string | null): string {
  if (!seller?.trim()) return text;
  const names = [seller.trim(), seller.trim().split(/\s+/)[0]].map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return text.replace(new RegExp(`(?<![\\p{L}])(?:${names.join("|")})(?![\\p{L}])`, "giu"), "@Seller");
}

function toClaimMatchInput(cards: PhotoCards, claims: Claim[], seller: string | null): ClaimMatchInput {
  const labels = claimLabels(claims);
  return {
    cards: cards.map((x) => x.card),
    claims: claims.filter((cl) => !cl.underReply).map((cl) => ({ who: labels.get(normalizeName(cl.claimer))!, text: tagAsSeller(cl.rawText, seller) })),
  };
}

/** Claude's labels back to the claimers' names; null if the answer doesn't fit the lot. */
function fromClaimMatchAnswer(answer: ClaimMatchAnswer, cardCount: number, claims: Claim[]): (string | null)[] | null {
  if (!Array.isArray(answer) || answer.length !== cardCount) return null;
  const byLabel = new Map([...claimLabels(claims)].map(([name, label]) => [label, claims.find((cl) => normalizeName(cl.claimer) === name)!.claimer]));
  return answer.map((label) => (label && byLabel.get(label)) || null);
}

const sameItem = (a: string, b: string) => a === b || a.includes(b) || b.includes(a);

/**
 * Claims in the order they were placed. A claim is contested when someone else claimed earlier
 * the same item, everything ("alle"), or the lot without naming items.
 */
function readClaims(replies: CapturedReply[], seller: string | null, me: string): Claim[] {
  const claims: Claim[] = [];
  for (const r of replies) {
    const claimer = r.author ?? "";
    if (seller && normalizeName(claimer) === normalizeName(seller)) continue;
    const parsed = claimItems(r.text, seller);
    if (!parsed) continue;
    const earlier = claims.filter((x) => normalizeName(x.claimer) !== normalizeName(claimer) && !x.underReply);
    const contested = earlier.some(
      (x) => x.all || x.items.length === 0 || parsed.all || parsed.items.length === 0 || x.items.some((i) => parsed.items.some((j) => sameItem(i, j))),
    );
    claims.push({
      replyId: r.id,
      claimer,
      rawText: r.text,
      items: parsed.items,
      all: parsed.all,
      isMe: !!me && normalizeName(claimer) === me,
      contested,
      underReply: /\bsitt svar\b|'s reply\b/i.test(r.ariaLabel ?? ""),
    });
  }
  return claims;
}

/** Leading only when nothing unreadable could be a higher bid after yours: otherwise "unclear". */
function myStatusFor(mine: Bid[], highest: Bid | null, doubtful: (string | null)[]): MyStatus {
  if (mine.length === 0) return "none";
  if (!highest?.isMe) return "outbid";
  const myLast = [...mine].sort((a, b) => compareIds(a.replyId, b.replyId)).at(-1)!.replyId;
  const after = doubtful.some((id) => id === null || myLast === null || compareIds(id, myLast) > 0);
  return after ? "unclear" : "lead";
}

function toBid(r: CapturedReply, seller: string | null, me: string, options: LotOptions, placedUnderReply = false): Bid | "unsure" | null {
  const bidder = r.author ?? "";
  if (seller && normalizeName(bidder) === normalizeName(seller)) return null; // The seller never bids.
  const reading = readBid(r.text, seller);
  if (reading.kind === "none") return null;
  let amount: number | null = reading.kind === "bid" ? reading.amount : null;
  let viaClaude = false;
  if (reading.kind === "unsure") {
    const answer = options.answer?.(seller, r.text);
    if (answer === undefined) return "unsure";
    if (answer === null) return null;
    amount = answer;
    viaClaude = true;
  }
  // "Svar fra A på B sitt svar" (to a reply) vs "… sin kommentar" (to the lot).
  const underReply = placedUnderReply || /\bsitt svar\b|'s reply\b/i.test(r.ariaLabel ?? "");
  return {
    replyId: r.id,
    bidder,
    amount,
    rawText: r.text,
    timeText: r.timeText,
    isMe: !!me && normalizeName(bidder) === me,
    valid: true,
    note: underReply ? "placed under another reply, not the lot (sellers usually don't count these)" : null,
    underReply,
    viaClaude,
  };
}

/** Unsure replies (for Claude), with the seller they tag. */
export function unsureReplies(capture: PostCapture): { seller: string | null; text: string }[] {
  const seller = sellerOf(capture);
  const out: { seller: string | null; text: string }[] = [];
  for (const c of capture.comments) {
    if (!isLot(c, seller)) continue;
    for (const r of c.replies) {
      if (seller && normalizeName(r.author) === normalizeName(seller)) continue;
      if (readBid(r.text, seller).kind === "unsure") out.push({ seller, text: r.text });
    }
  }
  return out;
}

export type LotSummary = {
  lots: number;
  bids: number;
  lead: number;
  outbid: number;
  /** "Leading?": see MyStatus. */
  unclear: number;
  unsure: number;
  claims: number;
  /** Lots where your claim was first. */
  claimed: number;
  /** Lots where someone claimed the same before you. */
  check: number;
};

export function summarizeLots(lots: Lot[]): LotSummary {
  return {
    lots: lots.length,
    bids: lots.reduce((n, l) => n + l.bids.length, 0),
    lead: lots.filter((l) => l.myStatus === "lead").length,
    outbid: lots.filter((l) => l.myStatus === "outbid").length,
    unclear: lots.filter((l) => l.myStatus === "unclear").length,
    unsure: lots.reduce((n, l) => n + l.unsureCount, 0),
    claims: lots.reduce((n, l) => n + l.claims.length, 0),
    claimed: lots.filter((l) => l.myClaim === "claimed").length,
    check: lots.filter((l) => l.myClaim === "check").length,
  };
}

/** The post ID a capture belongs to, from its URL or its comments' permalinks. */
export function capturePostId(capture: PostCapture): string | null {
  const fromUrl = (u: string | null | undefined) => u?.match(/\/(?:posts|permalink)\/(\d+)/)?.[1] ?? null;
  return fromUrl(capture.pageUrl) ?? fromUrl(capture.post.url) ?? capture.comments.map((c) => fromUrl(c.url)).find(Boolean) ?? null;
}
